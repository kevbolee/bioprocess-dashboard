"""
Gilson autosampler SQL client (JORMUNGAND_DB on MLAB1807MAST\SQL64GILSON2012).

Connects with the dashboard_reader login (db_datareader on JORMUNGAND_DB).
Credentials come from .env: GILSON_SQL_SERVER, GILSON_SQL_DATABASE,
GILSON_SQL_USER, GILSON_SQL_PASSWORD.

Schema notes (Trilution LC):
  - SAMPLETRACK.RUNID  → RECORD.ID  (NOT RUN.ID)
  - RECORD.NAME        = run timestamp encoded as MMDDYYYY-HHMMSS or YYYYMMDD-HHMMSS
  - RECORD.CREATEDDATE = run start time
  - SAMPLETRACK.ORDER  = step counter within the run (bracketed — reserved word)
  - Method name via:   RUN → APPLICATIONMETHODMAP → METHOD → RECORD.NAME
  - No per-step timestamps; no stored run-end time.
  - Active detection:  most recent run started within ACTIVE_WINDOW_HOURS.
  - ETA:               estimated from historical avg inter-run gap and step ratio.
"""

import asyncio
import logging
import os
import threading
from datetime import datetime, timedelta
from functools import partial
from typing import Optional

logger = logging.getLogger(__name__)

ACTIVE_WINDOW_HOURS = 24   # runs started within this window are considered potentially active

_tls = threading.local()
_conn_str: Optional[str] = None
_conn_str_lock = threading.Lock()


def _build_conn_str() -> str:
    from dotenv import load_dotenv
    load_dotenv()
    return (
        f"DRIVER={{ODBC Driver 17 for SQL Server}};"
        f"SERVER={os.environ['GILSON_SQL_SERVER']};"
        f"DATABASE={os.environ['GILSON_SQL_DATABASE']};"
        f"UID={os.environ['GILSON_SQL_USER']};"
        f"PWD={os.environ['GILSON_SQL_PASSWORD']};"
        "TrustServerCertificate=yes;"
        "ConnectTimeout=10;"
    )


def _get_conn():
    global _conn_str
    import pyodbc

    with _conn_str_lock:
        if _conn_str is None:
            _conn_str = _build_conn_str()
    cs = _conn_str

    conn = getattr(_tls, "connection", None)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            conn = None

    if conn is None:
        conn = pyodbc.connect(cs, timeout=10)
        _tls.connection = conn
        logger.info("Connected to Gilson SQL (thread %s)", threading.current_thread().name)

    return conn


def _dt_to_iso(val):
    return val.isoformat() if hasattr(val, "isoformat") else val


# ---------------------------------------------------------------------------
# Run status
# ---------------------------------------------------------------------------

def _query_run_status_sync() -> dict:
    """
    Return current Gilson run status derived from RECORD + SAMPLETRACK.

    Fields:
      status              'running' | 'idle'
      run_name            str — encoded timestamp name of the most recent run
      method_name         str — Trilution LC method (e.g. 'MAST_NSLOT')
      started_at          str (ISO) — run start time
      steps_done          int — steps logged so far for current run
      steps_estimated     int — avg steps across historical runs
      pct_complete        float | None
      elapsed_sec         int
      eta                 str (ISO) | None — estimated completion
      remaining_sec       int | None
      avg_run_duration_sec int | None
      recent_runs         list[dict] — last 10 runs summary
    """
    conn = _get_conn()
    now = datetime.now()

    # Most recent run: SAMPLETRACK.RUNID = RECORD.ID, RECORDTYPEID=9 for run records
    cur = conn.execute("""
        SELECT TOP 1
            rec.ID,
            rec.NAME,
            rec.CREATEDDATE,
            COUNT(st.ID)    AS steps_done
        FROM RECORD rec
        JOIN SAMPLETRACK st ON st.RUNID = rec.ID
        WHERE rec.RECORDTYPEID = 9
        GROUP BY rec.ID, rec.NAME, rec.CREATEDDATE
        ORDER BY rec.CREATEDDATE DESC
    """)
    row = cur.fetchone()
    if not row:
        return {"status": "idle", "note": "No runs found in JORMUNGAND_DB."}

    run_id, run_name, started_at, steps_done = row
    started_at_iso = _dt_to_iso(started_at)
    elapsed_sec = int((now - started_at).total_seconds()) if started_at else None
    age_hours = elapsed_sec / 3600 if elapsed_sec is not None else 9999
    status = "running" if age_hours <= ACTIVE_WINDOW_HOURS else "idle"

    # Method name for this run via RUN → APPLICATIONMETHODMAP → METHOD → RECORD
    cur = conn.execute("""
        SELECT TOP 1 mrec.NAME
        FROM RUN r
        JOIN RECORD rrec ON rrec.ID = r.RECORDID
        JOIN APPLICATIONMETHODMAP amm ON amm.APPLICATIONID = r.APPLICATIONID
        JOIN METHOD m ON m.ID = amm.METHODID
        JOIN RECORD mrec ON mrec.ID = m.RECORDID
        WHERE rrec.ID = ?
    """, [run_id])
    mrow = cur.fetchone()
    method_name = mrow[0] if mrow else "MAST_NSLOT"

    # All historical runs: steps and start times for avg calculations
    cur = conn.execute("""
        SELECT
            rec.ID,
            rec.NAME,
            rec.CREATEDDATE,
            COUNT(st.ID) AS steps
        FROM RECORD rec
        JOIN SAMPLETRACK st ON st.RUNID = rec.ID
        WHERE rec.RECORDTYPEID = 9
        GROUP BY rec.ID, rec.NAME, rec.CREATEDDATE
        ORDER BY rec.CREATEDDATE DESC
    """)
    history = cur.fetchall()   # (id, name, createddate, steps)

    steps_estimated = None
    eta = None
    remaining_sec = None
    pct_complete = None
    avg_duration_sec = None

    if len(history) >= 2:
        step_counts = [h[3] for h in history]
        steps_estimated = int(sum(step_counts) / len(step_counts))

        # Use inter-run gaps as proxy for run duration (ignore idle gaps > 24h)
        gaps = []
        for i in range(len(history) - 1):
            gap = (history[i][2] - history[i + 1][2]).total_seconds()
            if 0 < gap < 86400:
                gaps.append(gap)
        if gaps:
            avg_duration_sec = int(sum(gaps) / len(gaps))

    if steps_estimated and steps_done:
        pct_complete = round(min(steps_done / steps_estimated * 100, 100), 1)

    if status == "running" and avg_duration_sec and started_at and elapsed_sec is not None:
        remaining = avg_duration_sec - elapsed_sec
        if remaining > 0:
            remaining_sec = remaining
            eta = (started_at + timedelta(seconds=avg_duration_sec)).isoformat()

    recent_runs = [
        {
            "run_name": h[1],
            "started_at": _dt_to_iso(h[2]),
            "steps": h[3],
        }
        for h in history[:10]
    ]

    return {
        "status": status,
        "run_name": run_name,
        "method_name": method_name,
        "started_at": started_at_iso,
        "steps_done": steps_done,
        "steps_estimated": steps_estimated,
        "pct_complete": pct_complete,
        "elapsed_sec": elapsed_sec,
        "eta": eta,
        "remaining_sec": remaining_sec,
        "avg_run_duration_sec": avg_duration_sec,
        "recent_runs": recent_runs,
    }


# ---------------------------------------------------------------------------
# Schema discovery (debugging)
# ---------------------------------------------------------------------------

def _discover_tables_sync() -> list[dict]:
    conn = _get_conn()
    try:
        cur = conn.execute("""
            SELECT c.TABLE_NAME, c.COLUMN_NAME, c.DATA_TYPE
            FROM INFORMATION_SCHEMA.COLUMNS c
            JOIN INFORMATION_SCHEMA.TABLES t
                ON c.TABLE_NAME = t.TABLE_NAME AND t.TABLE_TYPE = 'BASE TABLE'
            ORDER BY c.TABLE_NAME, c.ORDINAL_POSITION
        """)
        result: dict = {}
        for tname, cname, dtype in cur.fetchall():
            result.setdefault(tname, []).append({"col": cname, "type": dtype})
        return [{"table": k, "columns": v} for k, v in sorted(result.items())]
    except Exception as e:
        logger.warning("Gilson table discovery failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Public async API
# ---------------------------------------------------------------------------

def _ping_sync() -> bool:
    try:
        _get_conn().execute("SELECT 1")
        return True
    except Exception:
        return False


async def ping() -> bool:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _ping_sync)


async def get_gilson_run_status() -> dict:
    """Return current Gilson run status: active/idle, method, progress, ETA."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _query_run_status_sync)


async def get_gilson_status() -> dict:
    """Alias for get_gilson_run_status() — kept for API compatibility."""
    return await get_gilson_run_status()


async def get_gilson_rs232() -> dict:
    """
    Trilution LC (JORMUNGAND_DB) does not log RS-232 events to SQL.
    Returns a stub so existing API callers don't break; connected=null means
    link state is indeterminate (no data to check against).
    """
    ok = await ping()
    return {
        "connected": None,
        "last_ok_time": None,
        "last_error": None,
        "recent_errors": [],
        "comm_table": None,
        "status_table": None,
        "status_rows": [],
        "note": "Gilson SQL reachable — RS-232 log not available in Trilution LC schema." if ok
                else "Gilson SQL unreachable.",
        "db_reachable": ok,
    }


async def discover_gilson_tables() -> list[dict]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _discover_tables_sync)

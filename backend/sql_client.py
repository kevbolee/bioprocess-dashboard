"""
MAST SQL Server client for analytical instrument results.

Queries MAST_SP on 192.168.137.10 for:
  - Roche Cedex BioHT results (cell density, viability, metabolites)

Join chain (confirmed from schema exploration):
  BioHtTestHeaders  (root — SampleId links to SampleData.SampleID)
    → BioHtSampleResultWithLotInformations  (TestHeaderId FK)
      → BioHtTestResultData  (ResultPacketId FK)   ← actual numeric result
      ← BioHtTestsLookup  (TestId — analyte name)
  LEFT JOIN SampleData  (SampleID = BioHtTestHeaders.SampleId)
      → VesselId, ExperimentID, Start timestamp
"""

import asyncio
import logging
import os
import threading
from functools import partial
from typing import Optional

logger = logging.getLogger(__name__)

# Thread-local storage so each thread pool worker gets its own pyodbc connection.
# A single shared connection is NOT thread-safe: concurrent queries from different
# asyncio tasks (dispatched to different threads) cause "Connection is busy" errors.
_tls = threading.local()
_conn_str: Optional[str] = None
_conn_str_lock = threading.Lock()


def _build_conn_str() -> str:
    from dotenv import load_dotenv
    load_dotenv()
    return (
        f"DRIVER={{ODBC Driver 17 for SQL Server}};"
        f"SERVER={os.environ['MAST_SQL_SERVER']};"
        f"DATABASE={os.environ['MAST_SQL_DATABASE']};"
        f"UID={os.environ['MAST_SQL_USER']};"
        f"PWD={os.environ['MAST_SQL_PASSWORD']};"
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
        logger.info("Connected to MAST_SP SQL Server (thread %s)", threading.current_thread().name)

    return conn


# ---------------------------------------------------------------------------
# Synchronous queries (run in thread pool — pyodbc is blocking)
# ---------------------------------------------------------------------------

_BIOHT_SELECT = """
    SELECT
        COALESCE(sd.VesselId,   th.SampleId) AS vessel_id,
        COALESCE(sd.ExperimentID, '')         AS experiment_id,
        COALESCE(sd.Start,      th.CreateDateTime) AS sample_time,
        th.SampleId                           AS sample_id,
        sr.TestString                         AS test_abbrev,
        COALESCE(tl.TestName,   sr.TestString) AS test_name,
        TRY_CAST(trd.Result AS FLOAT)         AS result_value,
        trd.Unit                              AS unit,
        sr.DateTimeStamp                      AS result_time,
        sr.ValidationStatus                   AS validation_status
    FROM BioHtTestHeaders th
    JOIN BioHtSampleResultWithLotInformations sr
        ON sr.TestHeaderId = th.Id
    JOIN BioHtTestResultData trd
        ON trd.Id = sr.ResultPacketId
    LEFT JOIN BioHtTestsLookup tl
        ON tl.TestId = sr.TestId
    LEFT JOIN SampleData sd
        ON sd.SampleID = th.SampleId
"""


def _dt_to_iso(val):
    return val.isoformat() if hasattr(val, "isoformat") else val


def _query_bioht_sync(
    vessel_id: Optional[str],
    days_back: int = 30,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> list[dict]:
    conn = _get_conn()

    if since and until:
        sql = _BIOHT_SELECT + " WHERE COALESCE(sd.Start, th.CreateDateTime) >= ? AND COALESCE(sd.Start, th.CreateDateTime) <= ?"
        params: list = [since, until]
    else:
        sql = _BIOHT_SELECT + " WHERE COALESCE(sd.Start, th.CreateDateTime) >= DATEADD(day, -?, GETDATE())"
        params = [days_back]

    if vessel_id:
        sql += " AND COALESCE(sd.VesselId, th.SampleId) = ?"
        params.append(vessel_id)

    sql += " ORDER BY COALESCE(sd.Start, th.CreateDateTime) DESC"

    cur = conn.execute(sql, params)
    cols = [col[0] for col in cur.description]
    results = []
    for row in cur.fetchall():
        d = {k: _dt_to_iso(v) for k, v in zip(cols, row)}
        results.append(d)
    return results


def _count_bioht_sync(
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> int:
    """Return COUNT(*) from the same MAST BioHT join used by _query_bioht_sync."""
    conn = _get_conn()
    sql = """
        SELECT COUNT(*)
        FROM BioHtTestHeaders th
        JOIN BioHtSampleResultWithLotInformations sr ON sr.TestHeaderId = th.Id
        JOIN BioHtTestResultData trd ON trd.Id = sr.ResultPacketId
        LEFT JOIN SampleData sd ON sd.SampleID = th.SampleId
    """
    if since and until:
        sql += " WHERE COALESCE(sd.Start, th.CreateDateTime) >= ? AND COALESCE(sd.Start, th.CreateDateTime) <= ?"
        params: list = [since, until]
    else:
        sql += " WHERE COALESCE(sd.Start, th.CreateDateTime) >= DATEADD(day, -90, GETDATE())"
        params = []
    try:
        cur = conn.execute(sql, params)
        row = cur.fetchone()
        return row[0] if row else 0
    except Exception as e:
        logger.warning("MAST BioHT count query failed: %s", e)
        return 0


def _query_vessels_sync() -> list[str]:
    conn = _get_conn()
    sql = """
        SELECT DISTINCT COALESCE(sd.VesselId, th.SampleId) AS vessel_id
        FROM BioHtTestHeaders th
        JOIN BioHtSampleResultWithLotInformations sr ON sr.TestHeaderId = th.Id
        LEFT JOIN SampleData sd ON sd.SampleID = th.SampleId
        ORDER BY vessel_id
    """
    cur = conn.execute(sql)
    return [row[0] for row in cur.fetchall()]


def _query_analytes_sync(vessel_id: Optional[str]) -> list[dict]:
    """Distinct analytes that have at least one result for the given vessel (or all vessels)."""
    conn = _get_conn()
    sql = """
        SELECT DISTINCT
            sr.TestString                          AS test_abbrev,
            COALESCE(tl.TestName, sr.TestString)   AS test_name,
            trd.Unit                               AS unit
        FROM BioHtTestHeaders th
        JOIN BioHtSampleResultWithLotInformations sr ON sr.TestHeaderId = th.Id
        JOIN BioHtTestResultData trd               ON trd.Id = sr.ResultPacketId
        LEFT JOIN BioHtTestsLookup tl              ON tl.TestId = sr.TestId
        LEFT JOIN SampleData sd                    ON sd.SampleID = th.SampleId
    """
    params: list = []
    if vessel_id:
        sql += " WHERE COALESCE(sd.VesselId, th.SampleId) = ?"
        params.append(vessel_id)
    sql += " ORDER BY test_abbrev"

    cur = conn.execute(sql, params)
    cols = [col[0] for col in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# InstrumentData generic results (Nova II_1, future instruments)
# Structure: SampleID / InstrumentName / Type ('Parameter'|'Result') / Name / Value
# Nova results will appear here as InstrumentName='Nova II_1', Type='Result'
# ---------------------------------------------------------------------------

_INSTRUMENT_DATA_SQL = """
    SELECT
        id.SampleID                   AS sample_id,
        COALESCE(sd.VesselId, '')     AS vessel_id,
        COALESCE(sd.ExperimentID, '') AS experiment_id,
        sd.Start                      AS sample_time,
        id.InstrumentName             AS instrument,
        id.Name                       AS analyte,
        id.Value                      AS result_value,
        id.Type                       AS record_type
    FROM InstrumentData id
    LEFT JOIN SampleData sd ON sd.SampleID = id.SampleID
    WHERE id.Type = 'Result'
      AND sd.Start >= DATEADD(day, -?, GETDATE())
"""


def _query_instrument_data_sync(
    instrument: Optional[str], vessel_id: Optional[str], days_back: int
) -> list[dict]:
    conn = _get_conn()
    sql = _INSTRUMENT_DATA_SQL
    params: list = [days_back]
    if instrument:
        sql += " AND id.InstrumentName = ?"
        params.append(instrument)
    if vessel_id:
        sql += " AND sd.VesselId = ?"
        params.append(vessel_id)
    sql += " ORDER BY sd.Start DESC"
    cur = conn.execute(sql, params)
    cols = [col[0] for col in cur.description]
    return [{k: _dt_to_iso(v) for k, v in zip(cols, row)} for row in cur.fetchall()]


def _query_instruments_sync() -> list[str]:
    conn = _get_conn()
    sql = """
        SELECT DISTINCT InstrumentName
        FROM InstrumentData
        WHERE Type = 'Result'
        ORDER BY InstrumentName
    """
    cur = conn.execute(sql)
    return [row[0] for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Public async API
# ---------------------------------------------------------------------------

async def count_bioht_results(
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> int:
    """Return the MAST BioHT row count quickly without fetching all data."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(_count_bioht_sync, since, until))


async def get_bioht_results(
    vessel_id: Optional[str] = None,
    days_back: int = 30,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> list[dict]:
    """Return BioHT analytical results, optionally filtered by vessel and date range."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, partial(_query_bioht_sync, vessel_id, days_back, since, until)
    )


async def get_vessels() -> list[str]:
    """Return all vessel IDs that have at least one BioHT result."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _query_vessels_sync)


async def get_analytes(vessel_id: Optional[str] = None) -> list[dict]:
    """Return distinct analytes (test_abbrev, test_name, unit) with results."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(_query_analytes_sync, vessel_id))


async def get_instrument_results(
    instrument: Optional[str] = None,
    vessel_id: Optional[str] = None,
    days_back: int = 30,
) -> list[dict]:
    """Return generic instrument results from InstrumentData (covers Nova, future instruments).

    Nova results appear here as instrument='Nova II_1', Type='Result' once Nova
    integration is active and results are being received.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, partial(_query_instrument_data_sync, instrument, vessel_id, days_back)
    )


async def get_instruments_with_results() -> list[str]:
    """Return list of instrument names that have Result rows in InstrumentData."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _query_instruments_sync)


# ---------------------------------------------------------------------------
# MAST status: instruments registry, active samples, alarms, schedule
# ---------------------------------------------------------------------------

def _query_instruments_registry_sync() -> list[dict]:
    """Return all rows from the Instruments registration table (id, name, type, isActive, …)."""
    conn = _get_conn()
    try:
        cur = conn.execute("SELECT * FROM Instruments ORDER BY IsActive DESC, Name")
        cols = [c[0] for c in cur.description]
        return [{k: _dt_to_iso(v) for k, v in zip(cols, row)} for row in cur.fetchall()]
    except Exception as e:
        logger.warning("Instruments registry query failed: %s", e)
        return []


def _table_exists_sync(table_name: str) -> bool:
    conn = _get_conn()
    try:
        cur = conn.execute(
            "SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_NAME = ? AND TABLE_TYPE = 'BASE TABLE'",
            [table_name],
        )
        return cur.fetchone() is not None
    except Exception:
        return False


def _query_table_top_sync(table_name: str, limit: int = 50, date_filter_days: Optional[int] = None) -> tuple[bool, list[dict]]:
    """
    Query an arbitrary table.  Returns (exists, rows).
    If date_filter_days is set, auto-detects the first datetime column and filters to that window.
    """
    if not _table_exists_sync(table_name):
        return False, []
    conn = _get_conn()
    try:
        # Auto-detect datetime column for optional date filter
        date_col = None
        if date_filter_days:
            ci = conn.execute(
                "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_NAME = ? AND DATA_TYPE IN ('datetime','datetime2','date') "
                "ORDER BY ORDINAL_POSITION",
                [table_name],
            ).fetchone()
            if ci:
                date_col = ci[0]

        if date_col and date_filter_days:
            sql = (f"SELECT TOP ({limit}) * FROM [{table_name}] "
                   f"WHERE [{date_col}] >= DATEADD(day, -{date_filter_days}, GETDATE()) "
                   f"ORDER BY [{date_col}] DESC")
            cur = conn.execute(sql)
        else:
            sql = f"SELECT TOP ({limit}) * FROM [{table_name}]"
            cur = conn.execute(sql)

        cols = [c[0] for c in cur.description]
        return True, [{k: _dt_to_iso(v) for k, v in zip(cols, row)} for row in cur.fetchall()]
    except Exception as e:
        logger.warning("Query on %s failed: %s", table_name, e)
        return True, []


def _query_active_samples_sync() -> dict:
    """
    Return currently open SampleData rows (no Stop/End timestamp).
    Falls back to the 5 most recent rows if the end-column name is unknown.
    """
    conn = _get_conn()
    for end_col in ("Stop", "End", "EndTime", "FinishTime", "Finish"):
        try:
            cur = conn.execute(
                f"SELECT TOP 20 * FROM SampleData WHERE [{end_col}] IS NULL "
                f"AND Start IS NOT NULL ORDER BY Start DESC"
            )
            cols = [c[0] for c in cur.description]
            rows = [{k: _dt_to_iso(v) for k, v in zip(cols, row)} for row in cur.fetchall()]
            return {"found": True, "data": rows, "end_col": end_col}
        except Exception:
            continue
    # Fallback: most recent rows regardless of end status
    try:
        cur = conn.execute("SELECT TOP 5 * FROM SampleData ORDER BY Start DESC")
        cols = [c[0] for c in cur.description]
        rows = [{k: _dt_to_iso(v) for k, v in zip(cols, row)} for row in cur.fetchall()]
        return {"found": True, "data": rows, "end_col": None, "note": "End column not identified"}
    except Exception as e:
        return {"found": False, "data": [], "error": str(e)}


_ALARM_TABLE_CANDIDATES = [
    # Ignition 8.x alarm journal defaults
    "ALARM_EVENTS", "alarm_events", "AlarmEvents",
    # Other common SCADA names
    "Alarms", "AlarmHistory", "SystemAlarms", "AlarmLog",
    "ActiveAlarms", "EventLog", "AlarmJournal", "Events",
]

_cached_alarm_table: Optional[str] = None


def _discover_alarm_table_sync() -> Optional[str]:
    """Find the alarm journal table by name probe then INFORMATION_SCHEMA fallback."""
    global _cached_alarm_table
    if _cached_alarm_table is not None:
        return _cached_alarm_table

    for name in _ALARM_TABLE_CANDIDATES:
        if _table_exists_sync(name):
            _cached_alarm_table = name
            logger.info("Alarm table found: %s", name)
            return name

    # Fallback: any table with a uniqueidentifier column whose name contains alarm/event
    conn = _get_conn()
    try:
        cur = conn.execute("""
            SELECT DISTINCT c.TABLE_NAME
            FROM INFORMATION_SCHEMA.COLUMNS c
            JOIN INFORMATION_SCHEMA.TABLES t
                ON c.TABLE_NAME = t.TABLE_NAME AND t.TABLE_TYPE = 'BASE TABLE'
            WHERE c.DATA_TYPE = 'uniqueidentifier'
              AND (LOWER(c.TABLE_NAME) LIKE '%alarm%' OR LOWER(c.TABLE_NAME) LIKE '%event%')
            ORDER BY c.TABLE_NAME
        """)
        row = cur.fetchone()
        if row:
            _cached_alarm_table = row[0]
            logger.info("Alarm table found via schema scan: %s", _cached_alarm_table)
            return _cached_alarm_table
    except Exception as e:
        logger.warning("Alarm table GUID scan failed: %s", e)

    logger.info("No alarm table found in MAST_SP")
    return None


def _query_alarms_sync(days_back: int = 7, limit: int = 100) -> dict:
    """
    Query the Ignition alarm journal (or any discovered alarm table).
    Returns {found, table, columns, data}.

    Ignition alarm journal columns (typical SQL Server storage):
      eventtime, eventid, alarmpath/displaypath, eventtype/eventstate,
      priority, systemevent, ackby, eventvalue, label, currentstate
    """
    tname = _discover_alarm_table_sync()
    if not tname:
        return {"found": False, "table": None, "columns": [], "data": []}

    conn = _get_conn()
    try:
        # Detect datetime column for ordering/filtering
        ci = conn.execute(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_NAME = ? AND DATA_TYPE IN ('datetime','datetime2','date','datetimeoffset') "
            "ORDER BY ORDINAL_POSITION",
            [tname],
        ).fetchone()
        date_col = ci[0] if ci else None

        if date_col:
            sql = (
                f"SELECT TOP ({limit}) * FROM [{tname}] "
                f"WHERE [{date_col}] >= DATEADD(day, -{days_back}, GETDATE()) "
                f"ORDER BY [{date_col}] DESC"
            )
        else:
            sql = f"SELECT TOP ({limit}) * FROM [{tname}]"

        cur = conn.execute(sql)
        cols = [c[0] for c in cur.description]
        rows = [{k: _dt_to_iso(v) for k, v in zip(cols, row)} for row in cur.fetchall()]
        return {"found": True, "table": tname, "columns": cols, "data": rows}
    except Exception as e:
        logger.warning("Alarm query on %s failed: %s", tname, e)
        return {"found": True, "table": tname, "columns": [], "data": [], "error": str(e)}


def _query_mast_status_sync() -> dict:
    """
    Assemble MAST system status. Tries several plausible table names for alarms
    and schedule; gracefully returns found=False when a table is absent.
    """
    result: dict = {}

    # Instrument registry
    instruments = _query_instruments_registry_sync()
    result["instruments"] = {"found": bool(instruments) or _table_exists_sync("Instruments"),
                             "data": instruments, "table": "Instruments"}

    # Active samples
    result["active_samples"] = _query_active_samples_sync()

    # Alarms — delegate to dedicated discoverer
    alarms_result = _query_alarms_sync(days_back=3, limit=50)
    result["alarms"] = alarms_result

    # Schedule — try several candidate table names
    for tname in ("SamplingSchedule", "SampleQueue", "ScheduledTasks", "SamplingOrder",
                  "SampleRequests", "TaskQueue", "ScheduledSamples"):
        found, rows = _query_table_top_sync(tname, limit=20)
        if found:
            result["schedule"] = {"found": True, "data": rows, "table": tname}
            break
    else:
        result["schedule"] = {"found": False, "data": [], "table": None}

    return result


def _query_sample_data_history_sync(days_back: int = 30, limit: int = 200) -> list[dict]:
    """Return recent SampleData records, newest first."""
    conn = _get_conn()
    try:
        cur = conn.execute(
            "SELECT TOP (?) * FROM SampleData WHERE Start >= DATEADD(day, -?, GETDATE()) ORDER BY Start DESC",
            [limit, days_back],
        )
        cols = [c[0] for c in cur.description]
        return [{k: _dt_to_iso(v) for k, v in zip(cols, row)} for row in cur.fetchall()]
    except Exception as e:
        logger.warning("SampleData history query failed: %s", e)
        return []


def _discover_mast_tables_sync() -> list[dict]:
    """Return all user tables in MAST_SP with their column names (for debugging)."""
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
        logger.warning("Table discovery failed: %s", e)
        return []


# Async wrappers
async def get_instruments_registry() -> list[dict]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _query_instruments_registry_sync)


async def get_mast_status() -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _query_mast_status_sync)


async def get_sample_data_history(days_back: int = 30) -> list[dict]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(_query_sample_data_history_sync, days_back))


async def discover_mast_tables() -> list[dict]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _discover_mast_tables_sync)


async def get_alarms(days_back: int = 7, limit: int = 100) -> dict:
    """Return alarm history from the Ignition alarm journal table."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, partial(_query_alarms_sync, days_back, limit))


# ---------------------------------------------------------------------------
# Sample Pilots: status, last sample, next scheduled sample
# ---------------------------------------------------------------------------

def _query_sample_pilots_sync() -> list[dict]:
    """
    Return all sample pilots with:
      - is_online, experiment_running  (live PLC flags from SamplePilots)
      - sequence_name                  (loaded sequence config name)
      - last_sample_time               (most recent SampleCommands.execEnded for this SP)
      - last_sample_status             (SampleData.Status for that sample)
      - next_sample_time               (earliest pending SampleCommands.start for this SP)
      - sampling_interval_min          (SampleCommandData.interval for active command)

    When no experiment is running there will be no pending commands and
    next_sample_time will be None.
    """
    conn = _get_conn()
    try:
        cur = conn.execute("""
            SELECT
                sp.id,
                sp.name,
                sp.codeName,
                sp.number,
                sp.isOnline,
                sp.ExperimentRunning,
                sp.updatedOn,
                sps.name        AS sequence_name,
                sps.config      AS sequence_config,

                -- Most recent completed sample for this SP
                last_sc.execEnded   AS last_sample_time,
                last_sc.execBegan   AS last_sample_began,
                last_sd.Status      AS last_sample_status,

                -- Next pending sample (execBegan IS NULL, not deleted, start in future)
                next_sc.start       AS next_sample_time,
                next_scd.interval   AS sampling_interval_min

            FROM SamplePilots sp
            LEFT JOIN SamplePilotSequences sps
                ON sps.id = sp.lastLoadedSamplePilotSequences_id

            -- Last completed sample: most recent execEnded for this SP
            LEFT JOIN (
                SELECT scd_inner.SamplePilots_id,
                       MAX(sc_inner.id) AS max_sc_id
                FROM SampleCommands sc_inner
                JOIN SampleCommandData scd_inner ON scd_inner.id = sc_inner.SampleCommandData_id
                WHERE sc_inner.execEnded IS NOT NULL
                GROUP BY scd_inner.SamplePilots_id
            ) last_agg ON last_agg.SamplePilots_id = sp.id
            LEFT JOIN SampleCommands last_sc ON last_sc.id = last_agg.max_sc_id
            LEFT JOIN SampleData last_sd ON last_sd.SampleID = last_sc.SampleID

            -- Next pending sample: earliest future start not yet begun and not deleted
            LEFT JOIN (
                SELECT scd_inner.SamplePilots_id,
                       MIN(sc_inner.id) AS min_sc_id
                FROM SampleCommands sc_inner
                JOIN SampleCommandData scd_inner ON scd_inner.id = sc_inner.SampleCommandData_id
                WHERE sc_inner.execBegan IS NULL
                  AND sc_inner.gotDeleted IS NULL
                  AND sc_inner.start >= GETDATE()
                GROUP BY scd_inner.SamplePilots_id
            ) next_agg ON next_agg.SamplePilots_id = sp.id
            LEFT JOIN SampleCommands next_sc ON next_sc.id = next_agg.min_sc_id
            LEFT JOIN SampleCommandData next_scd ON next_scd.id = next_sc.SampleCommandData_id

            ORDER BY sp.number
        """)
        cols = [c[0] for c in cur.description]
        rows = []
        for row in cur.fetchall():
            d = dict(zip(cols, [_dt_to_iso(v) for v in row]))
            rows.append(d)
        return rows
    except Exception as e:
        logger.warning("SamplePilots query failed: %s", e)
        return []


async def get_sample_pilots() -> list[dict]:
    """Return all sample pilots with live status and next-sample scheduling."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _query_sample_pilots_sync)


def _ping_sync() -> bool:
    try:
        conn = _get_conn()
        conn.execute("SELECT 1")
        return True
    except Exception:
        return False


async def ping() -> bool:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _ping_sync)

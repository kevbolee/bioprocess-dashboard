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

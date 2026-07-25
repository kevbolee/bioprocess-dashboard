import sqlite3
import json
import os
from datetime import datetime, timedelta
from contextlib import contextmanager
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
CONFIG_PATH = BASE_DIR / "config.json"

with open(CONFIG_PATH, encoding="utf-8") as f:
    CONFIG = json.load(f)

DB_PATH = BASE_DIR / CONFIG["database"]["path"]


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS bioreactor_data (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   DATETIME NOT NULL,
                bioreactor  TEXT NOT NULL,
                parameter   TEXT NOT NULL,
                value       REAL,
                quality     TEXT DEFAULT 'Good'
            );
            CREATE INDEX IF NOT EXISTS idx_br_ts
                ON bioreactor_data (bioreactor, parameter, timestamp);

            CREATE TABLE IF NOT EXISTS opc_connection_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   DATETIME NOT NULL,
                server      TEXT NOT NULL,
                status      TEXT NOT NULL,
                message     TEXT
            );

            CREATE TABLE IF NOT EXISTS alerts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   DATETIME NOT NULL,
                bioreactor  TEXT NOT NULL,
                parameter   TEXT NOT NULL,
                value       REAL,
                threshold   REAL,
                direction   TEXT,
                acknowledged INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS bioht_results (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                sample_time  TEXT NOT NULL,
                sample_id    TEXT,
                test_abbrev  TEXT NOT NULL,
                result_value REAL,
                result_text  TEXT,
                unit         TEXT,
                status       TEXT,
                polled_at    TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_bioht_unique
                ON bioht_results (sample_time, sample_id, test_abbrev);
            CREATE INDEX IF NOT EXISTS idx_bioht_ts
                ON bioht_results (sample_time);

            CREATE TABLE IF NOT EXISTS nova_results (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                sample_time  TEXT NOT NULL,
                sample_id    TEXT,
                group_name   TEXT NOT NULL,
                analyte      TEXT NOT NULL,
                display_name TEXT,
                result_value REAL,
                unit         TEXT,
                error_status TEXT,
                polled_at    TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_nova_unique
                ON nova_results (sample_time, group_name, analyte);
            CREATE INDEX IF NOT EXISTS idx_nova_ts
                ON nova_results (sample_time);

            CREATE TABLE IF NOT EXISTS vicell_results (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                sample_id           TEXT,
                sample_date         TEXT,
                viability_pct       REAL,
                total_cells_per_ml  REAL,
                viable_cells_per_ml REAL,
                avg_diameter_um     REAL,
                avg_circularity     REAL,
                total_cells         INTEGER,
                viable_cells        INTEGER,
                dilution_factor     REAL,
                cell_type           TEXT,
                source_file         TEXT,
                imported_at         TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_vicell_unique
                ON vicell_results (sample_id, sample_date);
            CREATE INDEX IF NOT EXISTS idx_vicell_date
                ON vicell_results (sample_date);

            CREATE TABLE IF NOT EXISTS vicell_file_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path   TEXT NOT NULL UNIQUE,
                file_mtime  REAL NOT NULL,
                imported_at TEXT NOT NULL,
                inserted    INTEGER DEFAULT 0,
                skipped     INTEGER DEFAULT 0
            );
        """)


def insert_reading(bioreactor: str, parameter: str, value: float, quality: str = "Good", ts: datetime = None):
    ts = ts or datetime.utcnow()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO bioreactor_data (timestamp, bioreactor, parameter, value, quality) VALUES (?,?,?,?,?)",
            (ts, bioreactor, parameter, value, quality)
        )


def insert_readings_batch(readings: list[dict]):
    """readings: list of {bioreactor, parameter, value, quality, timestamp}"""
    with get_conn() as conn:
        conn.executemany(
            "INSERT INTO bioreactor_data (timestamp, bioreactor, parameter, value, quality) VALUES (:timestamp,:bioreactor,:parameter,:value,:quality)",
            readings
        )


def get_latest(bioreactor: str) -> dict:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT parameter, value, quality, MAX(timestamp) as timestamp
            FROM bioreactor_data
            WHERE bioreactor = ?
            GROUP BY parameter
        """, (bioreactor,)).fetchall()
    return {r["parameter"]: {"value": r["value"], "quality": r["quality"], "timestamp": r["timestamp"]} for r in rows}


def get_history(bioreactor: str, parameter: str, hours: float = 24) -> list[dict]:
    since = datetime.utcnow() - timedelta(hours=hours)
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT timestamp, value, quality
            FROM bioreactor_data
            WHERE bioreactor = ? AND parameter = ? AND timestamp >= ?
            ORDER BY timestamp ASC
        """, (bioreactor, parameter, since)).fetchall()
    return [dict(r) for r in rows]


def get_all_latest() -> dict:
    """Return latest value for every bioreactor/parameter."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT bioreactor, parameter, value, quality, MAX(timestamp) as timestamp
            FROM bioreactor_data
            GROUP BY bioreactor, parameter
        """).fetchall()
    result = {}
    for r in rows:
        br = r["bioreactor"]
        result.setdefault(br, {})
        result[br][r["parameter"]] = {
            "value": r["value"],
            "quality": r["quality"],
            "timestamp": r["timestamp"]
        }
    return result


def log_connection(server: str, status: str, message: str = ""):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO opc_connection_log (timestamp, server, status, message) VALUES (?,?,?,?)",
            (datetime.utcnow(), server, status, message)
        )


def get_connection_log(limit: int = 50) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM opc_connection_log ORDER BY timestamp DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def purge_old_data():
    days = CONFIG["database"]["retention_days"]
    cutoff = datetime.utcnow() - timedelta(days=days)
    with get_conn() as conn:
        conn.execute("DELETE FROM bioreactor_data WHERE timestamp < ?", (cutoff,))


# ---------------------------------------------------------------------------
# Nova BioProfile Flex2 results
# ---------------------------------------------------------------------------

def insert_nova_results(sample_time: str, sample_id: str, readings: list[dict], polled_at: str):
    """
    Insert a batch of analyte readings for one Nova measurement snapshot.
    Silently ignores duplicates (same sample_time + group + analyte).
    """
    with get_conn() as conn:
        conn.executemany(
            """INSERT OR IGNORE INTO nova_results
               (sample_time, sample_id, group_name, analyte, display_name,
                result_value, unit, error_status, polled_at)
               VALUES (:sample_time, :sample_id, :group_name, :analyte, :display_name,
                       :result_value, :unit, :error_status, :polled_at)""",
            [
                {**r, "sample_time": sample_time, "sample_id": sample_id, "polled_at": polled_at}
                for r in readings
            ],
        )


def insert_nova_results_counted(sample_time: str, sample_id: str, readings: list[dict], polled_at: str) -> int:
    """Same as insert_nova_results but returns the number of rows actually inserted."""
    inserted = 0
    with get_conn() as conn:
        for r in readings:
            cur = conn.execute(
                """INSERT OR IGNORE INTO nova_results
                   (sample_time, sample_id, group_name, analyte, display_name,
                    result_value, unit, error_status, polled_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (sample_time, sample_id, r["group_name"], r["analyte"], r["display_name"],
                 r["result_value"], r["unit"], r["error_status"], polled_at),
            )
            inserted += cur.rowcount
    return inserted


def get_nova_latest_sample_time() -> str | None:
    """Return the ISO timestamp of the most recently stored Nova measurement, or None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(sample_time) AS ts FROM nova_results"
        ).fetchone()
    return row["ts"] if row else None


def get_nova_results(days_back: int = 30, since: str = None, until: str = None) -> list[dict]:
    """Return Nova analyte readings, newest first.

    If since/until (ISO strings) are provided they take precedence over days_back.
    """
    if since and until:
        with get_conn() as conn:
            rows = conn.execute(
                """SELECT sample_time, sample_id, group_name, analyte, display_name,
                          result_value, unit, error_status
                   FROM nova_results
                   WHERE sample_time >= ? AND sample_time <= ?
                   ORDER BY sample_time DESC""",
                (since, until),
            ).fetchall()
    else:
        cutoff = (datetime.utcnow() - timedelta(days=days_back)).isoformat()
        with get_conn() as conn:
            rows = conn.execute(
                """SELECT sample_time, sample_id, group_name, analyte, display_name,
                          result_value, unit, error_status
                   FROM nova_results
                   WHERE sample_time >= ?
                   ORDER BY sample_time DESC""",
                (cutoff,),
            ).fetchall()
    return [dict(r) for r in rows]


def get_nova_samples() -> list[dict]:
    """Return all distinct (sample_time, sample_id) pairs, newest first."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT DISTINCT sample_time, sample_id
               FROM nova_results
               ORDER BY sample_time DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


def get_nova_sample(sample_time: str) -> list[dict]:
    """Return all analyte readings for one specific sample_time."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT sample_time, sample_id, group_name, analyte, display_name,
                      result_value, unit, error_status
               FROM nova_results WHERE sample_time = ?
               ORDER BY group_name, analyte""",
            (sample_time,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_nova_latest() -> list[dict]:
    """Return all analyte readings for the single most recent Nova measurement."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(sample_time) AS ts FROM nova_results"
        ).fetchone()
        if not row or not row["ts"]:
            return []
        rows = conn.execute(
            """SELECT sample_time, sample_id, group_name, analyte, display_name,
                      result_value, unit, error_status
               FROM nova_results WHERE sample_time = ?
               ORDER BY group_name, analyte""",
            (row["ts"],),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# BioHT (CEDEX BIO HT) local results
# ---------------------------------------------------------------------------

def insert_bioht_results_counted(rows: list[dict], polled_at: str) -> int:
    """Insert BioHT rows; returns number actually inserted (duplicates ignored)."""
    inserted = 0
    with get_conn() as conn:
        for r in rows:
            cur = conn.execute(
                """INSERT OR IGNORE INTO bioht_results
                   (sample_time, sample_id, test_abbrev, result_value, result_text,
                    unit, status, polled_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (r["sample_time"], r["sample_id"], r["test_abbrev"],
                 r.get("result_value"), r.get("result_text"),
                 r.get("unit"), r.get("status"), polled_at),
            )
            inserted += cur.rowcount
    return inserted


def get_bioht_results(days_back: int = 30, since: str = None, until: str = None) -> list[dict]:
    """Return BioHT rows newest first, optionally filtered by date range."""
    if since and until:
        with get_conn() as conn:
            rows = conn.execute(
                """SELECT sample_time, sample_id, test_abbrev, result_value,
                          result_text, unit, status
                   FROM bioht_results WHERE sample_time >= ? AND sample_time <= ?
                   ORDER BY sample_time DESC""",
                (since, until),
            ).fetchall()
    else:
        cutoff = (datetime.utcnow() - timedelta(days=days_back)).isoformat()
        with get_conn() as conn:
            rows = conn.execute(
                """SELECT sample_time, sample_id, test_abbrev, result_value,
                          result_text, unit, status
                   FROM bioht_results WHERE sample_time >= ?
                   ORDER BY sample_time DESC""",
                (cutoff,),
            ).fetchall()
    return [dict(r) for r in rows]


def get_bioht_samples() -> list[dict]:
    """Return distinct sample_ids with their most recent measurement time, newest first."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT sample_id, MAX(sample_time) AS latest_time
               FROM bioht_results
               GROUP BY sample_id
               ORDER BY latest_time DESC"""
        ).fetchall()
    return [dict(r) for r in rows]


def get_bioht_sample(sample_id: str) -> list[dict]:
    """Return all analyte rows for one sample_id."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT sample_time, sample_id, test_abbrev, result_value,
                      result_text, unit, status
               FROM bioht_results WHERE sample_id = ?
               ORDER BY sample_time, test_abbrev""",
            (sample_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_bioht_latest() -> list[dict]:
    """Return all rows for the sample_id with the most recent measurement."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT sample_id FROM bioht_results ORDER BY sample_time DESC LIMIT 1"
        ).fetchone()
        if not row:
            return []
        rows = conn.execute(
            """SELECT sample_time, sample_id, test_abbrev, result_value,
                      result_text, unit, status
               FROM bioht_results WHERE sample_id = ?
               ORDER BY sample_time, test_abbrev""",
            (row["sample_id"],),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Vi-CELL XR cell counter results
# ---------------------------------------------------------------------------

def insert_vicell_results(rows: list[dict]) -> tuple[int, int]:
    """Insert Vi-CELL results; returns (inserted, skipped) counts."""
    imported_at = datetime.utcnow().isoformat()
    inserted = 0
    skipped = 0
    with get_conn() as conn:
        for r in rows:
            cur = conn.execute(
                """INSERT OR IGNORE INTO vicell_results
                   (sample_id, sample_date, viability_pct,
                    total_cells_per_ml, viable_cells_per_ml,
                    avg_diameter_um, avg_circularity,
                    total_cells, viable_cells, dilution_factor,
                    cell_type, source_file, imported_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    r.get("sample_id"), r.get("sample_date"),
                    r.get("viability_pct"),
                    r.get("total_cells_per_ml"), r.get("viable_cells_per_ml"),
                    r.get("avg_diameter_um"), r.get("avg_circularity"),
                    r.get("total_cells"), r.get("viable_cells"),
                    r.get("dilution_factor"), r.get("cell_type"),
                    r.get("source_file"), imported_at,
                ),
            )
            if cur.rowcount:
                inserted += 1
            else:
                skipped += 1
    return inserted, skipped


_VICELL_COLS = """sample_id, sample_date, viability_pct,
                  total_cells_per_ml, viable_cells_per_ml,
                  avg_diameter_um, avg_circularity,
                  total_cells, viable_cells, dilution_factor,
                  cell_type, source_file, imported_at"""


def get_vicell_results(days_back: int = 90, sample_id_filter: str = None,
                       since: str = None, until: str = None) -> list[dict]:
    """Return Vi-CELL rows newest first.

    If since/until (ISO strings) are provided they take precedence over days_back.
    sample_id_filter is a substring match (case-insensitive).
    """
    if since and until:
        base_where = "sample_date >= ? AND sample_date <= ?"
        base_params: list = [since, until]
    else:
        cutoff = (datetime.utcnow() - timedelta(days=days_back)).isoformat()
        base_where = "sample_date >= ? OR sample_date IS NULL"
        base_params = [cutoff]

    if sample_id_filter:
        where = f"({base_where}) AND LOWER(sample_id) LIKE ?"
        params = base_params + [f"%{sample_id_filter.lower()}%"]
    else:
        where = f"({base_where})"
        params = base_params

    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT {_VICELL_COLS} FROM vicell_results WHERE {where} ORDER BY sample_date DESC",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def get_vicell_latest() -> dict | None:
    """Return the single most recent Vi-CELL measurement row, or None."""
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT {_VICELL_COLS} FROM vicell_results ORDER BY sample_date DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def get_vicell_samples(since: str = None, until: str = None) -> list[dict]:
    """Return distinct (sample_id, sample_date) rows newest first, optionally date-filtered."""
    if since and until:
        with get_conn() as conn:
            rows = conn.execute(
                """SELECT sample_id, sample_date
                   FROM vicell_results
                   WHERE sample_date >= ? AND sample_date <= ?
                   ORDER BY sample_date DESC""",
                (since, until),
            ).fetchall()
    else:
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT sample_id, sample_date FROM vicell_results ORDER BY sample_date DESC"
            ).fetchall()
    return [dict(r) for r in rows]


def get_vicell_file_log() -> dict[str, float]:
    """Return {file_path: file_mtime} for all previously imported files."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT file_path, file_mtime FROM vicell_file_log"
        ).fetchall()
    return {r["file_path"]: r["file_mtime"] for r in rows}


def upsert_vicell_file_log(file_path: str, file_mtime: float, inserted: int, skipped: int):
    """Record or update a successfully imported file."""
    imported_at = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO vicell_file_log (file_path, file_mtime, imported_at, inserted, skipped)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(file_path) DO UPDATE SET
                 file_mtime  = excluded.file_mtime,
                 imported_at = excluded.imported_at,
                 inserted    = inserted + excluded.inserted,
                 skipped     = skipped  + excluded.skipped""",
            (file_path, file_mtime, imported_at, inserted, skipped),
        )

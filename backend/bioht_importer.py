"""
CEDEX BIO HT archive TXT importer.

Parses the tab-separated archive files exported from the CEDEX BIO HT software
and inserts rows into the bioht_results SQLite table.
Duplicates are silently ignored via UNIQUE INDEX (sample_time, sample_id, test_abbrev).

File format (tab-separated):
  Row 0  — archive header, starts with "0"
  Row N  — data record, starts with "40"
            col[0]  record type (40)
            col[1]  timestamp  (YYYY-MM-DD HH:MM:SS)
            col[2]  sample ID  (e.g. J033Z1-0424-1PC)
            col[4]  sample type (SAM)
            col[6]  test abbreviation (e.g. LDH2B)
            col[8]  unit (e.g. U/L)
            col[9]  status / flag (HIGH ACT, > TEST RNG, blank …)
            col[10] result value (numeric string or "> 1000" for out-of-range)
            col[11] raw absorbance value
"""

import io
import logging
from datetime import datetime, timezone
from pathlib import Path

import database as db

logger = logging.getLogger(__name__)

_DATA_RECORD = "40"


def _parse_float(s: str):
    s = s.strip()
    if not s:
        return None
    # Remove leading "> " or "< " for out-of-range values
    clean = s.lstrip("><").strip().replace(",", "")
    try:
        return float(clean)
    except ValueError:
        return None


def _parse_row(cols: list[str]) -> dict | None:
    if len(cols) < 11:
        return None
    sample_time = cols[1].strip()
    sample_id   = cols[2].strip() or None
    test_abbrev = cols[6].strip()
    unit        = cols[8].strip()
    status      = cols[9].strip()
    result_raw  = cols[10].strip()

    if not sample_time or not test_abbrev:
        return None

    return {
        "sample_time":  sample_time,
        "sample_id":    sample_id,
        "test_abbrev":  test_abbrev,
        "unit":         unit or None,
        "status":       status or None,
        "result_text":  result_raw or None,
        "result_value": _parse_float(result_raw),
    }


def import_txt_bytes(content: bytes, source: str = "upload") -> dict:
    text = content.decode("utf-8-sig", errors="replace")
    return _import_text(text, source)


def import_txt_file(path: Path) -> dict:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    return _import_text(text, source=path.name)


def import_directory(dirpath: str | Path) -> dict:
    p = Path(dirpath)
    totals = {"files": 0, "rows_parsed": 0, "inserted": 0, "skipped": 0}
    for f in sorted(p.glob("*.txt")):
        r = import_txt_file(f)
        totals["files"]       += 1
        totals["rows_parsed"] += r["rows_parsed"]
        totals["inserted"]    += r["inserted"]
        totals["skipped"]     += r["skipped"]
        logger.info("  %s → %d rows, %d inserted, %d skipped",
                    f.name, r["rows_parsed"], r["inserted"], r["skipped"])
    return totals


def _import_text(text: str, source: str) -> dict:
    polled_at  = datetime.now(timezone.utc).isoformat()
    rows_out   = []

    for line in io.StringIO(text):
        line = line.rstrip("\n\r")
        if not line.startswith(_DATA_RECORD + "\t"):
            continue
        cols = line.split("\t")
        row = _parse_row(cols)
        if row:
            rows_out.append(row)

    inserted = db.insert_bioht_results_counted(rows_out, polled_at)
    skipped  = len(rows_out) - inserted

    logger.info("BioHT import [%s]: %d rows, %d inserted, %d skipped",
                source, len(rows_out), inserted, skipped)
    return {
        "source":      source,
        "rows_parsed": len(rows_out),
        "inserted":    inserted,
        "skipped":     skipped,
    }

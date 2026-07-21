"""
Nova BioProfile Flex2 CSV importer.

Parses the export CSV files produced by the Nova software and inserts rows
into the nova_results SQLite table.  Duplicates are silently ignored via the
UNIQUE INDEX on (sample_time, group_name, analyte).

CSV layout:
  Row 0  — column headers
  Row 1  — units (Date & Time column is empty → detected and skipped)
  Row 2+ — data
"""

import csv
import io
import logging
from datetime import datetime, timezone
from pathlib import Path

import database as db

logger = logging.getLogger(__name__)

# Map CSV column → (group_name, analyte_key, display_name, unit)
_COL_MAP = {
    "Gln":                ("Chem",              "Gln",            "Glutamine",         "mmol/L"),
    "Glu":                ("Chem",              "Glu",            "Glutamate",         "mmol/L"),
    "Gluc":               ("Chem",              "Gluc",           "Glucose",           "g/L"),
    "Lac":                ("Chem",              "Lac",            "Lactate",           "g/L"),
    "NH4+":               ("Chem",              "NH4",            "Ammonium",          "mmol/L"),
    "Na+":                ("Chem",              "Na",             "Sodium",            "mmol/L"),
    "K+":                 ("Chem",              "K",              "Potassium",         "mmol/L"),
    "Ca++":               ("Chem",              "Ca",             "Calcium",           "mmol/L"),
    "pH":                 ("Gas",               "pH",             "pH (Gas)",          ""),
    "PO2":                ("Gas",               "pO2",            "pO2 (Gas)",         "mmHg"),
    "PCO2":               ("Gas",               "pCO2",           "pCO2 (Gas)",        "mmHg"),
    "Osm":                ("Osmo",              "Osmolality",     "Osmolality",        "mOsm/kg"),
    "Total Density":      ("CellDensity",       "TotalDensity",   "Total Density",     "10^6/mL"),
    "Viable Density":     ("CellDensity",       "ViableDensity",  "Viable Density",    "10^6/mL"),
    "Viability":          ("CellDensity",       "Viability",      "Viability",         "%"),
    "Avg. Live Diameter": ("CellDensity",       "AvgLiveDiameter","Avg Live Diameter", "um"),
    "Total Live Count":   ("CellDensity",       "TotalLiveCount", "Total Live Count",  ""),
    "Total Cell Count":   ("CellDensity",       "TotalCellCount", "Total Cell Count",  ""),
    "pH @ Temp":          ("CalculatedResults", "pHCorrected",    "pH (Corrected)",    ""),
    "PCO2 @ Temp":        ("CalculatedResults", "pCO2Corrected",  "pCO2 (Corrected)",  "mmHg"),
    "PO2 @ Temp":         ("CalculatedResults", "pO2Corrected",   "pO2 (Corrected)",   "mmHg"),
    "O2 Saturation":      ("CalculatedResults", "O2Saturation",   "O2 Saturation",     "%"),
    "CO2 Saturation":     ("CalculatedResults", "CO2Saturation",  "CO2 Saturation",    "%"),
    "HCO3":               ("CalculatedResults", "HCO3",           "Bicarbonate",       "mmol/L"),
}

_DATE_FORMATS = [
    "%m/%d/%Y  %H:%M:%S",   # two spaces (standard Nova export)
    "%m/%d/%Y %H:%M:%S",    # one space
    "%Y-%m-%d %H:%M:%S",    # ISO-ish
]


def _parse_dt(s: str) -> str | None:
    s = s.strip()
    if not s:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).isoformat()
        except ValueError:
            pass
    logger.warning("Nova CSV: unrecognised date format %r", s)
    return s


def _parse_float(s: str) -> float | None:
    s = s.strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def import_csv_bytes(content: bytes, source: str = "upload") -> dict:
    """Parse uploaded CSV bytes and insert into nova_results."""
    text = content.decode("utf-8-sig", errors="replace")
    return _import_text(text, source)


def import_csv_file(path: Path) -> dict:
    """Parse a CSV file on disk and insert into nova_results."""
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    return _import_text(text, source=path.name)


def import_directory(dirpath: str | Path) -> dict:
    """Import all *.csv files in a directory."""
    p = Path(dirpath)
    totals = {"files": 0, "samples": 0, "inserted": 0, "skipped": 0}
    for csv_file in sorted(p.glob("*.csv")):
        r = import_csv_file(csv_file)
        totals["files"]    += 1
        totals["samples"]  += r["samples"]
        totals["inserted"] += r["inserted"]
        totals["skipped"]  += r["skipped"]
        logger.info("  %s → %d samples, %d inserted, %d skipped",
                    csv_file.name, r["samples"], r["inserted"], r["skipped"])
    return totals


def _import_text(text: str, source: str) -> dict:
    reader = csv.DictReader(io.StringIO(text))
    polled_at = datetime.now(timezone.utc).isoformat()

    samples_seen = inserted_total = skipped_total = 0

    first = True
    for row in reader:
        raw_dt = row.get("Date & Time", "").strip()

        # The row immediately after the header is a units row (Date & Time is empty)
        if first:
            first = False
            if not raw_dt:
                continue  # skip units row

        if not raw_dt:
            continue

        sample_time = _parse_dt(raw_dt)
        if not sample_time:
            continue

        sample_id = row.get("Sample ID", "").strip() or None

        readings = []
        for col, (group, analyte, display, unit) in _COL_MAP.items():
            val = _parse_float(row.get(col, ""))
            if val is None:
                continue
            readings.append({
                "group_name":   group,
                "analyte":      analyte,
                "display_name": display,
                "result_value": val,
                "unit":         unit,
                "error_status": None,
            })

        if not readings:
            continue

        n_inserted = db.insert_nova_results_counted(sample_time, sample_id, readings, polled_at)
        samples_seen   += 1
        inserted_total += n_inserted
        skipped_total  += len(readings) - n_inserted

    logger.info("Nova CSV import [%s]: %d samples, %d inserted, %d skipped",
                source, samples_seen, inserted_total, skipped_total)
    return {
        "source":   source,
        "samples":  samples_seen,
        "inserted": inserted_total,
        "skipped":  skipped_total,
    }

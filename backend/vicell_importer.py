"""
Vi-CELL XR xlsx importer.

Parses Beckman Coulter Vi-CELL XR export files without openpyxl
(these files have malformed styles that cause openpyxl to crash).
Uses stdlib zipfile + xml.etree to read the raw OOXML directly.

Two export formats are handled:

  Individual format (one result per file):
    Sheet 1 is a key-value list:
      Row 3:  Sample ID     | <id>
      Row 5:  Sample date   | <date string>
      Row 10: Total cells   | <n>
      ...
    Keys are matched by name so row positions do not need to be exact.

  Batch/summary format (multiple results, e.g. ad.xlsx):
    Sheet 1 has column headers at row 5:
      A=Sample ID, B=Cell type, C=Dilution factor,
      D=Sample date/time, E=Viability (%), F=Total cells/ml,
      G=Viable cells/ml, H=Avg diam, I=Avg circ
    Data rows start at row 8.
"""

import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Optional

import database as db

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

# Date formats seen in Vi-CELL exports
_DATE_FMTS = [
    "%d %b %Y  %I:%M:%S %p",
    "%d %b %Y %I:%M:%S %p",
    "%d %b %Y  %H:%M:%S",
    "%d %b %Y %H:%M:%S",
    "%m/%d/%Y %I:%M:%S %p",
    "%m/%d/%Y %H:%M:%S",
]


def _parse_date(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    s = re.sub(r" {2,}", "  ", raw.strip())
    for fmt in _DATE_FMTS:
        try:
            return datetime.strptime(s, fmt).isoformat()
        except ValueError:
            pass
    # normalise to single space and retry
    s2 = re.sub(r"\s+", " ", raw.strip())
    for fmt in _DATE_FMTS:
        try:
            return datetime.strptime(s2, fmt.replace("  ", " ")).isoformat()
        except ValueError:
            pass
    return raw.strip()  # return raw if unparseable


def _to_float(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (ValueError, TypeError):
        return None


def _to_int(v) -> Optional[int]:
    try:
        return int(float(v)) if v is not None else None
    except (ValueError, TypeError):
        return None


def _col_index(col_letter: str) -> int:
    """Convert column letter(s) to 0-based index (A=0, B=1, …)."""
    idx = 0
    for ch in col_letter.upper():
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx - 1


def _read_sheets(content: bytes) -> tuple[list[list], list[list]]:
    """
    Read first two worksheets from xlsx bytes.
    Returns (sheet1_rows, sheet2_rows) where each row is a sparse list
    indexed by column position (None for empty cells).
    """
    with zipfile.ZipFile(content if hasattr(content, "read") else __import__("io").BytesIO(content)) as z:
        # Shared strings
        strings: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            with z.open("xl/sharedStrings.xml") as f:
                ss = ET.fromstring(f.read())
            for si in ss:
                parts = [t.text or "" for t in si.iter(f"{_NS}t")]
                strings.append("".join(parts))

        sheet_files = sorted(
            [n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml", n)]
        )

        def _parse_sheet(name: str) -> list[list]:
            with z.open(name) as f:
                root = ET.fromstring(f.read())
            rows: list[list] = []
            for row_el in root.findall(f".//{_NS}row"):
                row_num = int(row_el.get("r", len(rows) + 1))
                # pad to row_num
                while len(rows) < row_num:
                    rows.append([])
                sparse: dict[int, str] = {}
                for c in row_el.findall(f"{_NS}c"):
                    ref = c.get("r", "")
                    col_str = "".join(ch for ch in ref if ch.isalpha())
                    if not col_str:
                        continue
                    ci = _col_index(col_str)
                    t = c.get("t", "")
                    v = c.find(f"{_NS}v")
                    val: Optional[str] = v.text if v is not None else None
                    if t == "s" and val is not None:
                        val = strings[int(val)]
                    sparse[ci] = val
                # expand sparse dict to list
                if sparse:
                    max_ci = max(sparse.keys())
                    row_list: list = [sparse.get(i) for i in range(max_ci + 1)]
                else:
                    row_list = []
                rows[row_num - 1] = row_list
            return rows

        s1 = _parse_sheet(sheet_files[0]) if len(sheet_files) > 0 else []
        s2 = _parse_sheet(sheet_files[1]) if len(sheet_files) > 1 else []
    return s1, s2


def _detect_format(rows: list[list]) -> str:
    """Return 'individual' or 'batch'."""
    # Batch: row 5 (index 4) has 'Sample ID', 'Viability', etc. as column headers
    if len(rows) >= 5:
        r5 = rows[4]
        text5 = " ".join(str(v) for v in r5 if v)
        if "Viability" in text5 and "Sample ID" in text5:
            return "batch"
    return "individual"


def _parse_individual(rows: list[list], source_file: str) -> list[dict]:
    """Parse key-value individual format."""
    kv: dict[str, str] = {}
    for row in rows:
        if len(row) >= 2 and row[0] and row[1] is not None:
            kv[str(row[0]).strip()] = str(row[1]).strip()

    result = {
        "sample_id":          kv.get("Sample ID"),
        "sample_date":        _parse_date(kv.get("Sample date")),
        "viability_pct":      _to_float(kv.get("Viability (%)")),
        "total_cells_per_ml": _to_float(kv.get("Total cells / ml (x10^6)")),
        "viable_cells_per_ml":_to_float(kv.get("Viable cells / ml (x10^6)")),
        "avg_diameter_um":    _to_float(kv.get("Average diameter (microns)")),
        "avg_circularity":    _to_float(kv.get("Average circularity")),
        "total_cells":        _to_int(kv.get("Total cells")),
        "viable_cells":       _to_int(kv.get("Viable cells")),
        "dilution_factor":    _to_float(kv.get("Dilution factor")),
        "cell_type":          kv.get("Cell type"),
        "source_file":        source_file,
    }
    if not result["sample_id"]:
        return []
    return [result]


def _parse_batch(rows: list[list], source_file: str) -> list[dict]:
    """Parse tabular batch export (multiple results per file)."""
    # Header is at row 5 (index 4); data starts at row 8 (index 7)
    # Columns: A=Sample ID, B=Cell type, C=Dilution factor, D=Sample date/time,
    #          E=Viability (%), F=Total cells/ml, G=Viable cells/ml,
    #          H=Avg diam (microns), I=Avg circ
    results = []
    for row in rows[7:]:  # data starts at row 8
        if not row or all(v is None for v in row):
            continue
        def g(i):
            return row[i] if i < len(row) else None

        sample_id = g(0)
        cell_type = g(1)
        dil       = _to_float(g(2))
        date_raw  = g(3)
        viab      = _to_float(g(4))
        tcpm      = _to_float(g(5))
        vcpm      = _to_float(g(6))
        diam      = _to_float(g(7))
        circ      = _to_float(g(8))

        # require at least a date or viability to be a valid data row
        if date_raw is None and viab is None:
            continue

        results.append({
            "sample_id":           sample_id,
            "sample_date":         _parse_date(date_raw),
            "viability_pct":       viab,
            "total_cells_per_ml":  tcpm,
            "viable_cells_per_ml": vcpm,
            "avg_diameter_um":     diam,
            "avg_circularity":     circ,
            "total_cells":         None,
            "viable_cells":        None,
            "dilution_factor":     dil,
            "cell_type":           cell_type,
            "source_file":         source_file,
        })
    return results


def parse_vicell_xlsx(content: bytes, filename: str) -> list[dict]:
    """
    Parse a Vi-CELL XR xlsx export.
    Returns a list of result dicts (one per measurement).
    """
    rows, _ = _read_sheets(content)
    fmt = _detect_format(rows)
    if fmt == "batch":
        return _parse_batch(rows, filename)
    return _parse_individual(rows, filename)


def import_xlsx_bytes(content: bytes, source: str) -> dict:
    """
    Parse and insert Vi-CELL results.  Skips duplicates.
    Returns a summary dict with inserted/skipped/error counts.
    """
    try:
        rows = parse_vicell_xlsx(content, source)
    except Exception as e:
        return {"status": "error", "error": str(e), "inserted": 0, "skipped": 0}

    if not rows:
        return {"status": "ok", "inserted": 0, "skipped": 0,
                "note": "No results found in file."}

    inserted, skipped = db.insert_vicell_results(rows)
    return {
        "status": "ok",
        "inserted": inserted,
        "skipped": skipped,
        "rows_parsed": len(rows),
        "source": source,
    }

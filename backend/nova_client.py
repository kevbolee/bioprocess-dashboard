"""
Nova BioProfile Flex2 OPC UA polling client.

Polls SampleResults every 2 minutes.  When the instrument's TimeStamp shows a
measurement newer than the last stored row, all analytes are read and saved to
the nova_results SQLite table.

Node-ID conventions (OPC Expert UA Server v1.5):
  Objects  → ns=2;s=OPCSystemObjects->SampleResults->...
  Variables → ns=3;s=OPCSystemObjects->SampleResults->...

Two node layouts exist under SampleResults:
  'var' — CellDensity, CalculatedResults
      Value: ns=3;s=...->{group}->{key}         (direct float Variable)
      Unit:  ns=3;s=...->{group}->{key}Units     (optional, may not exist)
  'obj' — Chem, Gas
      Value: ns=3;s=...->{group}->{key}->Result  (string Variable under Object)
      Unit:  ns=3;s=...->{group}->{key}->Units
      Error: ns=3;s=...->{group}->{key}->ErrorStatus
  'osmo' — Osmo (single analyte, same child layout as obj but no key level)
      Value: ns=3;s=...->Osmo->Result
      Unit:  ns=3;s=...->Osmo->Units
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import database as db

logger = logging.getLogger(__name__)

_CONFIG = json.loads((Path(__file__).parent.parent / "config.json").read_text())
NOVA_URL      = _CONFIG["nova"]["url"]
POLL_INTERVAL = _CONFIG["nova"]["poll_interval_seconds"]

_BASE = "OPCSystemObjects->SampleResults"

# (group, analyte_key, display_name, layout)
# layout: 'var' = direct Variable; 'obj' = ->Result child; 'osmo' = Osmo special case
_ANALYTES = [
    ("CellDensity",       "ViableDensity",   "Viable Density",    "var"),
    ("CellDensity",       "TotalDensity",    "Total Density",     "var"),
    ("CellDensity",       "Viability",       "Viability",         "var"),
    ("CellDensity",       "TotalCellCount",  "Total Cell Count",  "var"),
    ("CellDensity",       "TotalLiveCount",  "Total Live Count",  "var"),
    ("CellDensity",       "AvgLiveDiameter", "Avg Live Diameter", "var"),
    ("CalculatedResults", "pHCorrected",     "pH (Corrected)",    "var"),
    ("CalculatedResults", "pO2Corrected",    "pO2 (Corrected)",   "var"),
    ("CalculatedResults", "pCO2Corrected",   "pCO2 (Corrected)",  "var"),
    ("CalculatedResults", "O2Saturation",    "O2 Saturation",     "var"),
    ("CalculatedResults", "CO2Saturation",   "CO2 Saturation",    "var"),
    ("CalculatedResults", "HCO3",            "Bicarbonate",       "var"),
    ("Osmo",              None,              "Osmolality",        "osmo"),
    ("Gas",               "pH",              "pH (Gas)",          "obj"),
    ("Gas",               "pO2",             "pO2 (Gas)",         "obj"),
    ("Gas",               "pCO2",            "pCO2 (Gas)",        "obj"),
    ("Chem",              "Gln",             "Glutamine",         "obj"),
    ("Chem",              "Glu",             "Glutamate",         "obj"),
    ("Chem",              "Gluc",            "Glucose",           "obj"),
    ("Chem",              "Lac",             "Lactate",           "obj"),
    ("Chem",              "NH4",             "Ammonium",          "obj"),
    ("Chem",              "Na",              "Sodium",            "obj"),
    ("Chem",              "K",               "Potassium",         "obj"),
    ("Chem",              "Ca",              "Calcium",           "obj"),
]

_TS_NODE        = f"ns=3;s={_BASE}->TimeStamp"
_SAMPLE_ID_NODE = f"ns=3;s={_BASE}->StartTags->SampleInformation->SampleID"


def _result_nid(group: str, key: str | None, layout: str) -> str:
    if layout == "var":
        return f"ns=3;s={_BASE}->{group}->{key}"
    if layout == "osmo":
        return f"ns=3;s={_BASE}->Osmo->Result"
    return f"ns=3;s={_BASE}->{group}->{key}->Result"


def _units_nid(group: str, key: str | None, layout: str) -> str:
    if layout == "var":
        return f"ns=3;s={_BASE}->{group}->{key}Units"   # optional — may not exist
    if layout == "osmo":
        return f"ns=3;s={_BASE}->Osmo->Units"
    return f"ns=3;s={_BASE}->{group}->{key}->Units"


def _error_nid(group: str, key: str | None, layout: str) -> str | None:
    if layout == "var":
        return None  # 'var' nodes have no ErrorStatus child
    if layout == "osmo":
        return f"ns=3;s={_BASE}->Osmo->ErrorStatus"
    return f"ns=3;s={_BASE}->{group}->{key}->ErrorStatus"


def _to_float(val) -> float | None:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


async def _read_snapshot(client) -> tuple[str | None, str | None, list[dict]]:
    """
    Read current SampleResults from the OPC server.

    Returns:
      (sample_time_iso, sample_id, readings)
      readings: list of dicts ready to pass to db.insert_nova_results()
    """
    # --- timestamp ---
    sample_time = None
    try:
        ts_val = await client.get_node(_TS_NODE).read_value()
        if hasattr(ts_val, "isoformat"):
            sample_time = ts_val.isoformat()
        else:
            sample_time = str(ts_val)
    except Exception as e:
        logger.warning("Nova: could not read TimeStamp: %s", e)

    # --- sample ID ---
    sample_id = None
    try:
        sample_id = str(await client.get_node(_SAMPLE_ID_NODE).read_value())
    except Exception as e:
        logger.debug("Nova: could not read SampleID: %s", e)

    # --- analytes ---
    readings = []
    for group, key, display_name, layout in _ANALYTES:
        analyte_key = key if key is not None else "Osmolality"
        try:
            result_val = _to_float(await client.get_node(_result_nid(group, key, layout)).read_value())
        except Exception:
            continue  # skip unavailable analytes silently

        unit = ""
        try:
            raw_unit = str(await client.get_node(_units_nid(group, key, layout)).read_value())
            unit = raw_unit.strip("'")
        except Exception:
            pass

        error_status = None
        err_nid = _error_nid(group, key, layout)
        if err_nid:
            try:
                es = await client.get_node(err_nid).read_value()
                error_status = str(es) if es is not None else None
            except Exception:
                pass

        readings.append({
            "group_name":   group,
            "analyte":      analyte_key,
            "display_name": display_name,
            "result_value": result_val,
            "unit":         unit,
            "error_status": error_status,
        })

    return sample_time, sample_id, readings


async def _poll_once():
    """Connect, read a snapshot, store new results. Returns True if new data was stored."""
    try:
        from asyncua import Client
    except ImportError:
        logger.error("asyncua not installed — cannot poll Nova")
        return False

    try:
        async with Client(url=NOVA_URL, timeout=10) as client:
            sample_time, sample_id, readings = await _read_snapshot(client)
    except Exception as e:
        logger.warning("Nova OPC UA connection failed: %s", e)
        db.log_connection("Nova Flex2", "ERROR", str(e))
        return False

    if not sample_time or not readings:
        logger.debug("Nova: no data in snapshot (sample_time=%s, %d readings)", sample_time, len(readings))
        return False

    last_ts = db.get_nova_latest_sample_time()
    if last_ts and last_ts >= sample_time:
        logger.debug("Nova: no new result (instrument=%s, stored=%s)", sample_time, last_ts)
        return False

    polled_at = datetime.now(timezone.utc).isoformat()
    db.insert_nova_results(sample_time, sample_id, readings, polled_at)
    logger.info(
        "Nova: stored %d analytes for sample_time=%s sample_id=%s",
        len(readings), sample_time, sample_id,
    )
    db.log_connection("Nova Flex2", "OK", f"Stored {len(readings)} analytes for {sample_id}")
    return True


async def collect_forever(poll_interval: int = POLL_INTERVAL):
    """Background task — poll Nova indefinitely."""
    logger.info("Nova collector started (url=%s, interval=%ds)", NOVA_URL, poll_interval)
    while True:
        try:
            await _poll_once()
        except Exception as e:
            logger.exception("Nova collector unexpected error: %s", e)
        await asyncio.sleep(poll_interval)

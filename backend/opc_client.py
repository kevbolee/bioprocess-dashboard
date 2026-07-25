"""
OPC collection dispatcher.

Supported protocols (set config.json -> opc.protocol):
  SIMULATE  — built-in simulation, no hardware needed
  OPC_UA    — OPC Unified Architecture via asyncua library
  OPC_DA    — OPC Data Access (classic, Windows COM) via OpenOPC + pywin32
              DASware Connect uses OPC DA. Remote access requires the
              OpenOPC gateway service running on the DASware PC.
"""
import asyncio
import json
import logging
import math
import os
import random
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
with open(BASE_DIR / "config.json", encoding="utf-8") as f:
    CONFIG = json.load(f)

PROTOCOL = CONFIG["opc"]["protocol"]
POLL_INTERVAL = CONFIG["opc"]["poll_interval_seconds"]
BIOREACTORS = CONFIG["bioreactors"]

_server_map = {s["name"]: s for s in CONFIG["opc"]["servers"]}

# Thread pool for blocking OPC DA calls
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="opc-da")


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

_sim_state: dict = {}


def _init_sim_state():
    for br in BIOREACTORS:
        _sim_state[br["id"]] = {
            "temperature":       37.0,
            "ph":                7.2,
            "dissolved_oxygen":  60.0,
            "agitation":         200.0,
            "airflow":           0.5,
            "co2_exhaust":       3.0,
            "o2_exhaust":        19.5,
            "base_pump":         2.0,
            "acid_pump":         0.5,
            "feed_pump":         5.0,
            "pressure":          0.1,
            "volume":            200.0,
            "_t": 0,
        }


def _sim_tick(br_id: str) -> dict[str, float]:
    s = _sim_state[br_id]
    t = s["_t"]
    s["_t"] = t + 1

    def drift(val, center, amp, period, noise):
        return center + amp * math.sin(2 * math.pi * t / period) + random.gauss(0, noise)

    s["temperature"]       = round(max(30, min(42, drift(s["temperature"],       37.0, 0.5,   200,  0.05))), 2)
    s["ph"]                = round(max(5.0, min(8.5, drift(s["ph"],               7.2,  0.15,  300,  0.01))), 3)
    s["dissolved_oxygen"]  = round(max(20,  min(100, drift(s["dissolved_oxygen"], 60.0, 10,    150,  0.5))),  1)
    s["agitation"]         = round(max(100, min(800, drift(s["agitation"],        200,  30,    400,  1.0))),  0)
    s["airflow"]           = round(max(0.1, min(2.0, drift(s["airflow"],          0.5,  0.1,   250,  0.005))),3)
    s["co2_exhaust"]       = round(max(0.1, min(8.0, drift(s["co2_exhaust"],      3.0,  1.0,   180,  0.05))), 2)
    s["o2_exhaust"]        = round(max(16,  min(21,  drift(s["o2_exhaust"],       19.5, 1.5,   180,  0.05))), 2)
    s["base_pump"]         = round(max(0,   min(30,  s["base_pump"] + random.gauss(0, 0.1))),                 1)
    s["acid_pump"]         = round(max(0,   min(10,  s["acid_pump"] + random.gauss(0, 0.02))),                2)
    s["feed_pump"]         = round(max(0,   min(50,  s["feed_pump"] + random.gauss(0, 0.2))),                 1)
    s["pressure"]          = round(max(0,   min(1.5, drift(s["pressure"],         0.1,  0.02,  500,  0.001))),3)
    s["volume"]            = round(max(10,  min(3500,s["volume"] + random.gauss(0, 0.5))),                    0)

    return {k: v for k, v in s.items() if k != "_t"}


# ---------------------------------------------------------------------------
# OPC UA  (asyncua)
# ---------------------------------------------------------------------------

async def _read_opc_ua(server_cfg: dict, br_cfg: dict) -> dict[str, Optional[float]]:
    try:
        from asyncua import Client
    except ImportError:
        logger.error("asyncua not installed — pip install asyncua")
        return {}

    values: dict[str, Optional[float]] = {}
    try:
        client = Client(url=server_cfg["url"])
        opc_user = os.environ.get("OPC_UA_USERNAME", "")
        if opc_user:
            client.set_user(opc_user)
            client.set_password(os.environ.get("OPC_UA_PASSWORD", ""))
        async with client:
            for param, node_id in br_cfg["tags"].items():
                try:
                    val = await client.get_node(node_id).read_value()
                    values[param] = float(val)
                except Exception as e:
                    logger.warning("OPC UA read %s/%s: %s", br_cfg["id"], param, e)
                    values[param] = None
    except Exception as e:
        logger.error("OPC UA connect failed (%s): %s", server_cfg.get("url"), e)
    return values


# ---------------------------------------------------------------------------
# OPC DA  (OpenOPC / pywin32)
# ---------------------------------------------------------------------------

def _read_opc_da_sync(server_cfg: dict, br_cfg: dict) -> dict[str, Optional[float]]:
    """Blocking OPC DA read — called in a thread executor."""
    from opc_da_client import get_reader
    host = server_cfg.get("host", "localhost")
    server_name = server_cfg.get("da_server", "")
    reader = get_reader(host, server_name)

    tag_map = br_cfg["tags"]  # param -> tag_path
    tag_paths = list(tag_map.values())
    raw = reader.read(tag_paths)

    # Invert the map: tag_path -> param
    path_to_param = {v: k for k, v in tag_map.items()}
    return {path_to_param[tag]: val for tag, (val, _) in raw.items() if tag in path_to_param}


async def _read_opc_da(server_cfg: dict, br_cfg: dict) -> dict[str, Optional[float]]:
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(_executor, _read_opc_da_sync, server_cfg, br_cfg)
    except Exception as e:
        logger.error("OPC DA read async wrapper (%s/%s): %s", br_cfg["id"], server_cfg["name"], e)
        return {}


# ---------------------------------------------------------------------------
# Main collection loop
# ---------------------------------------------------------------------------

async def collect_forever(on_reading: Callable[[list[dict]], None]):
    if PROTOCOL == "SIMULATE":
        logger.info("OPC client running in SIMULATE mode")
        _init_sim_state()

    while True:
        readings: list[dict] = []
        ts = datetime.utcnow().isoformat()

        for br in BIOREACTORS:
            srv_cfg = _server_map.get(br["opc_server"])

            if PROTOCOL == "SIMULATE":
                values = _sim_tick(br["id"])
                quality = "Simulated"

            elif PROTOCOL == "OPC_UA":
                if not srv_cfg or not srv_cfg.get("enabled", True):
                    continue
                values = await _read_opc_ua(srv_cfg, br)
                quality = "Good"

            elif PROTOCOL == "OPC_DA":
                if not srv_cfg or not srv_cfg.get("enabled", True):
                    continue
                values = await _read_opc_da(srv_cfg, br)
                quality = "Good"

            else:
                logger.error("Unknown OPC protocol: %s", PROTOCOL)
                values = {}

            for param, value in values.items():
                readings.append({
                    "bioreactor": br["id"],
                    "parameter":  param,
                    "value":      value,
                    "quality":    quality if value is not None else "Bad",
                    "timestamp":  ts,
                })

        if readings:
            try:
                on_reading(readings)
            except Exception as e:
                logger.error("on_reading callback: %s", e)

        await asyncio.sleep(POLL_INTERVAL)

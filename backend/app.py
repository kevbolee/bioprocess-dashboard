"""
FastAPI backend for OPC bioreactor data visualization dashboard.
"""
import sys
from pathlib import Path
# Ensure sibling modules (database, opc_client) are importable when
# uvicorn is invoked as: uvicorn backend.app:app  (from project root)
sys.path.insert(0, str(Path(__file__).parent))

import asyncio
import json
import logging
import os
from typing import Optional

from fastapi import FastAPI, File, UploadFile, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import database as db
import bioht_importer
import nova_client
import nova_importer
import opc_client
import sql_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent
with open(BASE_DIR / "config.json") as f:
    CONFIG = json.load(f)

app = FastAPI(title="Bioreactor Dashboard", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve the frontend from the /frontend directory
FRONTEND_DIR = BASE_DIR / "frontend"
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

# ---------------------------------------------------------------------------
# WebSocket connection manager
# ---------------------------------------------------------------------------

class ConnectionManager:
    def __init__(self):
        self._connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._connections.append(ws)
        logger.info("WebSocket client connected. Total: %d", len(self._connections))

    def disconnect(self, ws: WebSocket):
        self._connections.remove(ws)
        logger.info("WebSocket client disconnected. Total: %d", len(self._connections))

    async def broadcast(self, data: dict):
        dead = []
        for ws in self._connections:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in self._connections:
                self._connections.remove(ws)


manager = ConnectionManager()

# ---------------------------------------------------------------------------
# OPC data ingestion callback
# ---------------------------------------------------------------------------

def on_readings(readings: list[dict]):
    db.insert_readings_batch(readings)
    # Fan out to WebSocket clients (fire-and-forget via event loop)
    payload = {"type": "readings", "data": readings}
    loop = asyncio.get_event_loop()
    if loop.is_running():
        asyncio.ensure_future(manager.broadcast(payload))


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup():
    db.init_db()
    logger.info("Database initialised at %s", db.DB_PATH)
    asyncio.create_task(opc_client.collect_forever(on_readings))
    logger.info("OPC collection task started (protocol=%s)", opc_client.PROTOCOL)
    asyncio.create_task(nova_client.collect_forever())
    logger.info("Nova Flex2 collector started (url=%s)", nova_client.NOVA_URL)


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    return FileResponse(str(FRONTEND_DIR / "index.html"))


@app.get("/api/config")
async def api_config():
    """Return public config (bioreactor list, parameter definitions)."""
    return {
        "bioreactors": [
            {"id": br["id"], "name": br["name"], "group": br.get("group", "")}
            for br in CONFIG["bioreactors"]
        ],
        "parameters": CONFIG["parameters"],
        "poll_interval": CONFIG["opc"]["poll_interval_seconds"],
        "opc_protocol": CONFIG["opc"]["protocol"],
    }


@app.get("/api/latest")
async def api_latest():
    """Latest reading for every bioreactor and parameter."""
    return db.get_all_latest()


@app.get("/api/latest/{bioreactor_id}")
async def api_latest_br(bioreactor_id: str):
    data = db.get_latest(bioreactor_id)
    if not data:
        raise HTTPException(status_code=404, detail="No data for bioreactor")
    return data


@app.get("/api/history/{bioreactor_id}/{parameter}")
async def api_history(bioreactor_id: str, parameter: str, hours: float = 24):
    """Time-series history for a single bioreactor + parameter."""
    if hours > 720:
        hours = 720
    rows = db.get_history(bioreactor_id, parameter, hours)
    return {"bioreactor": bioreactor_id, "parameter": parameter, "hours": hours, "data": rows}


@app.get("/api/connection-log")
async def api_connection_log():
    return db.get_connection_log()


@app.post("/api/purge-old-data")
async def api_purge():
    db.purge_old_data()
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# OPC UA tag browser  (uses asyncua to discover DASware node IDs)
# ---------------------------------------------------------------------------

async def _ua_browse_recursive(client, node, results: list, max_results: int, depth: int = 0):
    """Walk the OPC UA address space and collect all variable nodes."""
    if len(results) >= max_results or depth > 8:
        return
    try:
        children = await node.get_children()
    except Exception:
        return
    for child in children:
        if len(results) >= max_results:
            return
        try:
            node_class = await child.read_node_class()
            display_name = await child.read_display_name()
            node_id = child.nodeid.to_string()
            if node_class.value == 2:  # Variable
                results.append({"node_id": node_id, "name": display_name.Text})
            else:
                await _ua_browse_recursive(client, child, results, max_results, depth + 1)
        except Exception:
            continue


# ---------------------------------------------------------------------------
# Analytical instrument results (MAST SQL Server — BioHT, future: NOVA, UPLC)
# ---------------------------------------------------------------------------

@app.get("/api/analytical/vessels")
async def api_analytical_vessels():
    """List all vessel IDs that have at least one BioHT result in MAST_SP."""
    try:
        vessels = await sql_client.get_vessels()
        return {"vessels": vessels}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"MAST SQL unavailable: {e}")


@app.get("/api/analytical/analytes")
async def api_analytical_analytes(vessel_id: Optional[str] = None):
    """List distinct analytes with results, optionally filtered to a vessel."""
    try:
        analytes = await sql_client.get_analytes(vessel_id)
        return {"vessel_id": vessel_id, "analytes": analytes}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"MAST SQL unavailable: {e}")


@app.get("/api/analytical/bioht")
async def api_analytical_bioht(vessel_id: Optional[str] = None, days: int = 30):
    """
    Return BioHT analytical results from MAST_SP.

    Query params:
      vessel_id  — filter to one vessel (VesselId from SampleData, e.g. '1')
      days       — how many days back to fetch (default 30, max 365)
    """
    if days > 365:
        days = 365
    try:
        results = await sql_client.get_bioht_results(vessel_id=vessel_id, days_back=days)
        return {"vessel_id": vessel_id, "days": days, "count": len(results), "data": results}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"MAST SQL unavailable: {e}")


@app.get("/api/analytical/nova")
async def api_analytical_nova(vessel_id: Optional[str] = None, days: int = 30):
    """
    Return Nova BioProfile results from MAST_SP InstrumentData table.

    Results appear here as InstrumentName='Nova II_1', Type='Result' once the
    MAST Nova integration is active. Currently returns empty while Nova is offline
    (NOVA_COMM_ERR=True in MAST PLC).
    """
    if days > 365:
        days = 365
    try:
        results = await sql_client.get_instrument_results(
            instrument="Nova II_1", vessel_id=vessel_id, days_back=days
        )
        return {"vessel_id": vessel_id, "days": days, "count": len(results), "data": results}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"MAST SQL unavailable: {e}")


@app.get("/api/nova/results")
async def api_nova_results(days: int = 30, since: Optional[str] = None, until: Optional[str] = None):
    """
    Return Nova Flex2 OPC UA results from local SQLite.

    Query params (mutually exclusive sets):
      days          — how many days back to return (default 30, max 365)
      since / until — ISO date strings (e.g. 2024-07-01T00:00:00) for explicit range
    """
    if days > 365:
        days = 365
    rows = db.get_nova_results(days_back=days, since=since, until=until)
    return {"days": days, "since": since, "until": until, "count": len(rows), "data": rows}


@app.get("/api/nova/latest")
async def api_nova_latest():
    """Return all analyte readings for the single most recent Nova measurement."""
    rows = db.get_nova_latest()
    return {"count": len(rows), "data": rows}


@app.get("/api/nova/samples")
async def api_nova_samples():
    """Return all distinct (sample_time, sample_id) pairs for the sample picker."""
    samples = db.get_nova_samples()
    return {"count": len(samples), "data": samples}


@app.get("/api/nova/sample")
async def api_nova_sample(sample_time: str):
    """Return all analyte readings for one specific sample_time."""
    rows = db.get_nova_sample(sample_time)
    if not rows:
        raise HTTPException(status_code=404, detail="Sample not found")
    return {"count": len(rows), "data": rows}


@app.get("/api/bioht/results")
async def api_bioht_results(days: int = 30, since: Optional[str] = None, until: Optional[str] = None):
    if days > 365:
        days = 365
    rows = db.get_bioht_results(days_back=days, since=since, until=until)
    return {"count": len(rows), "data": rows}


@app.get("/api/bioht/samples")
async def api_bioht_samples():
    return {"data": db.get_bioht_samples()}


@app.get("/api/bioht/sample")
async def api_bioht_sample(sample_id: str):
    rows = db.get_bioht_sample(sample_id)
    if not rows:
        raise HTTPException(status_code=404, detail="Sample not found")
    return {"count": len(rows), "data": rows}


@app.get("/api/bioht/latest")
async def api_bioht_latest():
    return {"data": db.get_bioht_latest()}


@app.get("/api/bioht/all")
async def api_bioht_all(days: int = 30, since: Optional[str] = None, until: Optional[str] = None):
    """
    Merged BioHT data from both local SQLite (TXT imports) and MAST SQL Server.

    Local rows contain manual samples that were run directly on the BioHT and
    never collected by MAST (MAST collection deletes the sample from BioHT memory).
    MAST rows contain auto-collected samples.

    Deduplication key: (sample_id, test_abbrev) — if the same sample appears in
    both sources, the MAST row wins (it carries validation_status and vessel_id).
    Each row includes a 'source' field: "mast" or "local".
    """
    if days > 365:
        days = 365

    # Local SQLite rows
    local_rows = db.get_bioht_results(days_back=days, since=since, until=until)
    for r in local_rows:
        r["source"]    = "local"
        r["test_name"] = r.get("test_abbrev", "")
        r["vessel_id"] = None
        r["validation_status"] = None

    # MAST rows — gracefully degrade if MAST is offline
    mast_rows: list[dict] = []
    try:
        mast_data = await sql_client.get_bioht_results(
            vessel_id=None, days_back=days, since=since, until=until
        )
        for r in mast_data:
            r["source"]      = "mast"
            r["result_text"] = None
            vs = r.get("validation_status")
            r["status"] = str(vs) if vs is not None and vs != 0 else None
        mast_rows = mast_data
    except Exception as e:
        logger.warning("MAST BioHT unavailable for merged query: %s", e)

    # Merge: local first, then MAST overwrites on duplicate key
    by_key: dict = {}
    for r in local_rows:
        key = (r.get("sample_id") or "", r.get("test_abbrev") or "")
        by_key[key] = r
    for r in mast_rows:
        key = (r.get("sample_id") or "", r.get("test_abbrev") or "")
        by_key[key] = r  # MAST wins on conflict

    merged = sorted(by_key.values(), key=lambda r: r.get("sample_time") or "", reverse=True)
    return {"count": len(merged), "mast_count": len(mast_rows), "local_count": len(local_rows), "data": merged}


@app.get("/api/bioht/all-samples")
async def api_bioht_all_samples():
    """Distinct sample_ids from both local SQLite and MAST, newest first."""
    local_samples = db.get_bioht_samples()  # [{sample_id, latest_time}]
    local_ids = {s["sample_id"]: s for s in local_samples}

    mast_samples: list[dict] = []
    try:
        mast_rows = await sql_client.get_bioht_results(vessel_id=None, days_back=365)
        seen: dict = {}
        for r in mast_rows:
            sid = r.get("sample_id") or ""
            ts  = r.get("sample_time") or ""
            if sid not in seen or ts > seen[sid]:
                seen[sid] = ts
        mast_samples = [{"sample_id": k, "latest_time": v, "source": "mast"} for k, v in seen.items()]
    except Exception as e:
        logger.warning("MAST BioHT samples unavailable: %s", e)

    # Merge: collect all sample_ids, prefer MAST latest_time if both present
    merged: dict = {}
    for s in local_samples:
        merged[s["sample_id"]] = {"sample_id": s["sample_id"], "latest_time": s["latest_time"], "source": "local"}
    for s in mast_samples:
        sid = s["sample_id"]
        if sid in merged:
            merged[sid]["source"] = "both"
            if s["latest_time"] > merged[sid]["latest_time"]:
                merged[sid]["latest_time"] = s["latest_time"]
        else:
            merged[sid] = s

    result = sorted(merged.values(), key=lambda s: s.get("latest_time") or "", reverse=True)
    return {"count": len(result), "data": result}


@app.post("/api/bioht/import-txt")
async def api_bioht_import_txt(file: UploadFile = File(...)):
    content = await file.read()
    result = bioht_importer.import_txt_bytes(content, source=file.filename)
    return result


@app.post("/api/nova/import-csv")
async def api_nova_import_csv(file: UploadFile = File(...)):
    """
    Import a Nova BioProfile Flex2 CSV export file into the local database.
    Duplicates are silently ignored.
    """
    content = await file.read()
    result = nova_importer.import_csv_bytes(content, source=file.filename)
    return result


@app.get("/api/opc/browse-ua")
async def api_opc_browse_ua(url: str, root_node: str = ""):
    """
    Browse an OPC UA server's address space.
    Use this to discover exact node IDs on a DASware 6 machine.

    DASware 6 OPC UA endpoint format:  opc.tcp://HOSTNAME:51530/UA/connectServer
    Confirmed hostname for your Scivario machine: CTPCMO508723

    Example: GET /api/opc/browse-ua?url=opc.tcp://CTPCMO508723:51530/UA/connectServer

    Returns up to 300 variable nodes. Use root_node (OPC UA NodeId string) to
    narrow the search, e.g. root_node=ns=2;s=Plant1/Unit1
    """
    try:
        from asyncua import Client, ua
    except ImportError:
        raise HTTPException(status_code=500, detail="asyncua not installed — pip install asyncua")

    results = []
    try:
        async with Client(url=url, timeout=10) as client:
            if root_node:
                start = client.get_node(root_node)
            else:
                start = client.get_node("i=85")  # ObjectsFolder
            await _ua_browse_recursive(client, start, results, max_results=300)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OPC UA connect failed: {e}")

    return {
        "url": url,
        "root_node": root_node or "ObjectsFolder",
        "count": len(results),
        "nodes": results,
    }


@app.get("/api/opc/read-ua")
async def api_opc_read_ua(url: str, node_id: str):
    """
    Read a single OPC UA node value.
    Use this to test a node ID before adding it to config.json.

    Example: GET /api/opc/read-ua?url=opc.tcp://CTPCMO508723:51530/UA/connectServer&node_id=ns=2;s=Plant1/Unit1/Temperature/Sensor/ActualValue
    """
    try:
        from asyncua import Client
    except ImportError:
        raise HTTPException(status_code=500, detail="asyncua not installed")

    try:
        async with Client(url=url, timeout=10) as client:
            node = client.get_node(node_id)
            value = await node.read_value()
            display_name = (await node.read_display_name()).Text
            return {"node_id": node_id, "display_name": display_name, "value": value}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    # Send current snapshot immediately on connect
    try:
        snapshot = db.get_all_latest()
        await ws.send_json({"type": "snapshot", "data": snapshot})
        while True:
            # Keep alive — client sends pings
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception as e:
        logger.warning("WebSocket error: %s", e)
        manager.disconnect(ws)

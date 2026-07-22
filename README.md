# Bioreactor OPC Dashboard

A web-based real-time monitoring dashboard for bioreactors via OPC (OPC UA or simulated).

## Quick Start

1. **Run `start.bat`** on the server PC that can reach the OPC network.
2. Open a browser to `http://localhost:8000` on the server, or `http://<server-ip>:8000` from any PC on the network.

## Configuration (`config.json`)

### OPC Protocol

| Setting | Value | Description |
|---------|-------|-------------|
| `opc.protocol` | `"SIMULATE"` | Built-in simulation — use to test without OPC hardware |
| `opc.protocol` | `"OPC_UA"` | Connect to real OPC UA servers (asyncua library) |

### Adding Bioreactors

Add entries to the `bioreactors` array. For each bioreactor, specify:
- `id` — unique identifier (used in database and URLs)
- `name` — display name
- `opc_server` — must match a name in `opc.servers`
- `tags` — OPC node ID for each process parameter (e.g. `"ns=2;s=BR001.Temperature"`)

### OPC Servers

Each entry in `opc.servers` needs:
- `name` — referenced by bioreactor `opc_server` field
- `url` — OPC UA endpoint, e.g. `opc.tcp://192.168.1.100:4840`
- `enabled` — set to `false` to skip without deleting

### Parameters

`config.json` → `parameters` defines which process variables are tracked and how they display. Each parameter needs:
- `label` — display name
- `unit` — engineering unit string
- `min` / `max` — suggested chart Y-axis range
- `color` — hex color for chart lines and KPI values

To add a new parameter (e.g. `co2_offgas`):
1. Add it to `parameters` in `config.json`
2. Add the OPC tag under each bioreactor's `tags` section
3. Restart the server

## Architecture

```
start.bat
└── uvicorn backend/app.py
    ├── FastAPI REST API  (/api/...)
    ├── WebSocket  (/ws)  — pushes new readings to all browsers
    ├── opc_client.py     — polls OPC UA (or simulation) every N seconds
    ├── database.py       — SQLite via stdlib sqlite3
    └── frontend/         — served as static files
        ├── index.html
        ├── style.css
        └── app.js        — Chart.js dashboard, WebSocket client
```

## Data Retention

Set `database.retention_days` in `config.json` (default: 90 days).
Call `POST /api/purge-old-data` to run cleanup manually, or schedule it as a Windows Task.

## Network Access

The server listens on `0.0.0.0:8000` so any machine on the same network can connect using the server's IP address. If Windows Firewall blocks access, allow inbound TCP on port 8000 for the Python process or the port number you choose.

## OPC DA (Classic)

For older OPC DA (DCOM-based) servers, `asyncua` will not work. In that case:
1. Install `OpenOPC-Python3x` or use an OPC DA → OPC UA gateway (many vendors provide these).
2. Alternatively, add a thin wrapper in `opc_client.py` that uses `openopc` and returns the same dict format.

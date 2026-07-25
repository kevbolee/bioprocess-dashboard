"""
probe_opc_paths.py
Diagnostic: tests candidate node ID paths on both DASware servers.

Run this while a batch/process is actively running in DASware so that
unit nodes are published to the OPC address space.

Usage:
    python probe_opc_paths.py
"""
import asyncio
import os
import sys
from dotenv import load_dotenv

load_dotenv()

SERVERS = [
    {"name": "Scivario", "url": "opc.tcp://CTPCOG910098:51530/UA/connectServer"},
    # DASbox uses DasgipCoreServer on port 51510 (confirmed from dasboxOPC.xml)
    {"name": "DASbox",   "url": "opc.tcp://CTPCMO508723:51510/UA/DasgipCoreServer"},
]

USERNAME = os.environ.get("OPC_UA_USERNAME", "")
PASSWORD = os.environ.get("OPC_UA_PASSWORD", "")

# ── Node IDs to probe ────────────────────────────────────────────────────────
# Two key unknowns for the DasgipCoreServer:
#   1. Does pH use uppercase PH or lowercase pH?  (dasboxOPC.xml shows "pH")
#   2. Do paths include the subdomain level (e.g. Temperature/Sensor/PV)
#      or go straight to the variable (Temperature/PV)?
#
# We test Unit1 only.  Once you know which format works, update config.json.

PROBE_NODES = [
    # ── Known-good system nodes (should work on both servers) ──
    "ns=2;s=/Root/Version",
    "ns=2;s=/Root/Status",

    # ── Temperature — with and without /Sensor subdomain ──
    "ns=2;s=/Root/Unit1/Temperature/PV",
    "ns=2;s=/Root/Unit1/Temperature/Sensor/PV",

    # ── pH — uppercase PH (connectServer style) ──
    "ns=2;s=/Root/Unit1/PH/PV",
    "ns=2;s=/Root/Unit1/PH/Sensor/PV",

    # ── pH — lowercase pH (dasboxOPC.xml domain name) ──
    "ns=2;s=/Root/Unit1/pH/PV",
    "ns=2;s=/Root/Unit1/pH/Sensor/PV",

    # ── DO ──
    "ns=2;s=/Root/Unit1/DO/PV",
    "ns=2;s=/Root/Unit1/DO/Sensor/PV",

    # ── Agitation — XML shows Agitation/Actuator subdomain ──
    "ns=2;s=/Root/Unit1/Agitation/PV",
    "ns=2;s=/Root/Unit1/Agitation/Actuator/PV",

    # ── Pumps — XML shows PumpA/Actuator subdomain ──
    "ns=2;s=/Root/Unit1/PumpA/PV",
    "ns=2;s=/Root/Unit1/PumpA/Actuator/PV",

    # ── Pressure — XML shows Pressure/Sensor subdomain ──
    "ns=2;s=/Root/Unit1/Pressure/PV",
    "ns=2;s=/Root/Unit1/Pressure/Sensor/PV",

    # ── Gassing / gas flow ──
    "ns=2;s=/Root/Unit1/Gassing/F.PV",
    "ns=2;s=/Root/Unit1/Gassing/PV",

    # ── Offgas ──
    "ns=2;s=/Root/Unit1/Offgas/XCO2.PV",
    "ns=2;s=/Root/Unit1/Offgas/XO2.PV",

    # ── Reactor / volume ──
    "ns=2;s=/Root/Unit1/Reactor/VLiquid",
    "ns=2;s=/Root/Unit1/Reactor/PV",

    # ── Volume domain (also listed separately in XML) ──
    "ns=2;s=/Root/Unit1/Volume/PV",
    "ns=2;s=/Root/Unit1/Volume/Sensor/PV",
]


async def probe_server(server):
    try:
        from asyncua import Client
    except ImportError:
        print("ERROR: asyncua not installed.")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"Server: {server['name']}  ({server['url']})")
    print(f"{'='*60}")

    client = Client(url=server["url"], timeout=15)
    if USERNAME:
        client.set_user(USERNAME)
        client.set_password(PASSWORD)

    # ── Read each candidate node ──────────────────────────────
    try:
        async with client:
            print("  Connected and session activated.\n")
            ok_count = 0
            for node_id in PROBE_NODES:
                node = client.get_node(node_id)
                try:
                    value = await node.read_value()
                    print(f"  OK   {node_id}  ->  {value!r}")
                    ok_count += 1
                except Exception as e:
                    short = str(e).split("(")[0].strip()
                    print(f"  FAIL {node_id}  ->  {short}")
            print(f"\n  {ok_count} / {len(PROBE_NODES)} nodes resolved.")
    except Exception as e:
        print(f"  Connection failed: {e}")
        return

    # ── Browse /Root children ─────────────────────────────────
    print(f"\n  Browsing ns=2;s=/Root ...")
    try:
        async with client:
            root_node = client.get_node("ns=2;s=/Root")
            children = await root_node.get_children()
            print(f"  Children of /Root ({len(children)} found):")
            for child in children:
                nid = child.nodeid.to_string()
                try:
                    name = (await child.read_display_name()).Text
                except Exception:
                    name = "?"
                print(f"    {nid}  ({name})")

            # If Unit1 exists, browse one level deeper
            unit1 = next((c for c in children
                          if "Unit1" in c.nodeid.to_string()), None)
            if unit1:
                print(f"\n  Children of /Root/Unit1:")
                for child in await unit1.get_children():
                    nid = child.nodeid.to_string()
                    try:
                        name = (await child.read_display_name()).Text
                    except Exception:
                        name = "?"
                    print(f"    {nid}  ({name})")
    except Exception as e:
        print(f"\n  Could not browse /Root: {e}")


async def main():
    for server in SERVERS:
        await probe_server(server)
    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())

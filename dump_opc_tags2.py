"""
dump_opc_tags2.py  -  Targeted DASware tag dump
Tries multiple starting points to find the actual process variable nodes.
Output: opc_tags_dump2.json
"""
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

SERVERS = [
    {"name": "DASware-Scivario", "url": "opc.tcp://CTPCOG910098:51530/UA/connectServer"},
    {"name": "DASware-DASbox",   "url": "opc.tcp://CTPCMO508723:51510/UA/DasgipCoreServer"},
]

USERNAME = os.environ.get("OPC_UA_USERNAME", "")
PASSWORD = os.environ.get("OPC_UA_PASSWORD", "")

# Starting node IDs to probe — covers common DASware layouts
START_NODES = [
    "ns=2;s=/Root",
    "ns=2;s=Plant1",
    "ns=2;s=/Plant1",
    "ns=2;s=Root",
    "i=85",          # ObjectsFolder
    "i=23479",       # TagVariables (seen in your dump)
    "i=23488",       # Topics (seen in your dump)
    "i=31915",       # Locations (seen in Scivario dump)
]


async def browse_recursive(client, node, results, depth=0, max_depth=10):
    try:
        children = await node.get_children()
    except Exception as e:
        return
    for child in children:
        try:
            node_id    = child.nodeid.to_string()
            node_class = await child.read_node_class()
            name       = (await child.read_display_name()).Text
            entry = {
                "node_id":    node_id,
                "name":       name,
                "node_class": int(node_class),
                "depth":      depth,
            }
            if int(node_class) == 2:  # Variable
                try:
                    entry["value"] = str(await child.read_value())
                except Exception:
                    entry["value"] = None
            results.append(entry)
            if depth < max_depth:
                await browse_recursive(client, child, results, depth + 1, max_depth)
        except Exception as e:
            pass


async def probe_server(server):
    try:
        from asyncua import Client
    except ImportError:
        print("ERROR: asyncua not installed.")
        sys.exit(1)

    print(f"\n[{server['name']}] Connecting to {server['url']} ...")
    client = Client(url=server["url"], timeout=30)
    if USERNAME:
        client.set_user(USERNAME)
        client.set_password(PASSWORD)

    all_results = {}
    try:
        async with client:
            print(f"[{server['name']}] Connected.")
            for start_id in START_NODES:
                results = []
                try:
                    node = client.get_node(start_id)
                    name = (await node.read_display_name()).Text
                    print(f"  Browsing from {start_id} ({name}) ...")
                    await browse_recursive(client, node, results)
                    print(f"    -> {len(results)} nodes found")
                    if results:
                        all_results[start_id] = {"root_name": name, "nodes": results}
                except Exception as e:
                    print(f"    -> failed: {e}")
    except Exception as e:
        print(f"[{server['name']}] Connection failed: {e}")
        return {"name": server["name"], "error": str(e), "probes": {}}

    return {"name": server["name"], "url": server["url"], "probes": all_results}


async def main():
    output = {"timestamp": datetime.now().isoformat(), "servers": []}
    for server in SERVERS:
        result = await probe_server(server)
        output["servers"].append(result)

    out_path = Path(__file__).parent / "opc_tags_dump2.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    print(f"\nDone. Saved to: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())

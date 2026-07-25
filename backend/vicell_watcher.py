"""
Vi-CELL XR folder watcher.

Polls a configured directory for new or updated xlsx files and imports
them automatically into the local database.  Only files whose modification
time has changed since the last successful import are re-processed, so
re-running the server after a restart is safe — nothing is double-counted.

Temporary lock files created by Excel/Windows (names starting with "~$")
are silently skipped.

Config keys  (config.json → "vicell"):
  watch_folder           Path to the Vi-CELL export directory.
                         Leave empty to disable the watcher.
  poll_interval_seconds  How often to check the folder (default: 60).
"""

import asyncio
import json
import logging
import os
from pathlib import Path

import database as db
import vicell_importer

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.json"
_CFG = json.loads(_CONFIG_PATH.read_text(encoding="utf-8")).get("vicell", {})

WATCH_FOLDER    = _CFG.get("watch_folder", "").strip()
POLL_INTERVAL   = int(_CFG.get("poll_interval_seconds", 60))


def _scan_once() -> int:
    """
    Scan the watch folder for xlsx files, import any that are new or updated.
    Returns the number of files processed this scan.
    """
    folder = Path(WATCH_FOLDER)
    if not folder.is_dir():
        logger.warning("Vi-CELL watch folder not found: %s", folder)
        return 0

    known = db.get_vicell_file_log()   # {path_str: mtime}
    processed = 0

    for entry in sorted(folder.iterdir()):
        if not entry.is_file():
            continue
        if entry.suffix.lower() != ".xlsx":
            continue
        if entry.name.startswith("~$"):   # Excel temp/lock files
            continue

        path_str = str(entry.resolve())
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue

        if known.get(path_str) == mtime:
            continue   # already imported at this exact mtime

        logger.info("Vi-CELL watcher: importing %s", entry.name)
        try:
            content = entry.read_bytes()
            result  = vicell_importer.import_xlsx_bytes(content, source=entry.name)
        except Exception as exc:
            logger.error("Vi-CELL watcher: failed to import %s — %s", entry.name, exc)
            continue

        if result.get("status") == "error":
            logger.error("Vi-CELL watcher: parse error in %s — %s", entry.name, result.get("error"))
            continue

        inserted = result.get("inserted", 0)
        skipped  = result.get("skipped",  0)
        db.upsert_vicell_file_log(path_str, mtime, inserted, skipped)
        logger.info(
            "Vi-CELL watcher: %s — %d new, %d skipped",
            entry.name, inserted, skipped,
        )
        processed += 1

    return processed


async def collect_forever(poll_interval: int = POLL_INTERVAL):
    """Background task — poll the Vi-CELL folder indefinitely."""
    logger.info(
        "Vi-CELL watcher started (folder=%s, interval=%ds)",
        WATCH_FOLDER, poll_interval,
    )
    while True:
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _scan_once)
        except Exception as exc:
            logger.exception("Vi-CELL watcher unexpected error: %s", exc)
        await asyncio.sleep(poll_interval)

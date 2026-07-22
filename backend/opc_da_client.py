"""
OPC DA client using OpenOPC + pywin32.

DASware Connect exposes an OPC DA 2.0 server on the machine running DASware.
Remote access requires either:
  (a) DCOM configured on both machines (complex), OR
  (b) OpenOPC gateway service running on the DASware machine (simpler — see README).

Usage:
  reader = OpcDaReader(host="192.168.1.10", server="Eppendorf.DASwareConnect.DA.1")
  values = reader.read(["Vessel1.T_ist", "Vessel1.pH_ist"])
  tags = reader.browse()
"""
import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

_openopc_available = False
try:
    import OpenOPC
    _openopc_available = True
except ImportError:
    logger.warning("OpenOPC not available — pip install OpenOPC-Python3x")

# OpenOPC is not thread-safe and requires COM init per thread.
# Use a lock to serialize calls.
_opc_lock = threading.Lock()


class OpcDaReader:
    def __init__(self, host: str, server: str):
        self.host = host
        self.server = server
        self._opc = None

    def _connect(self):
        if not _openopc_available:
            raise RuntimeError("OpenOPC not installed. Run: pip install OpenOPC-Python3x")
        try:
            if self.host.lower() in ("localhost", "127.0.0.1"):
                opc = OpenOPC.client()
                opc.connect(self.server)
            else:
                # Remote via OpenOPC gateway service on the DASware PC.
                # The gateway (opc_gate.exe) must be running on host.
                opc = OpenOPC.open_client(host=self.host)
                opc.connect(self.server)
            self._opc = opc
            logger.info("OPC DA connected to %s on %s", self.server, self.host)
        except Exception as e:
            self._opc = None
            raise ConnectionError(f"OPC DA connect failed ({self.host} / {self.server}): {e}") from e

    def _ensure_connected(self):
        if self._opc is None:
            self._connect()

    def read(self, tag_paths: list[str]) -> dict[str, tuple[Optional[float], str]]:
        """
        Returns {tag_path: (value, quality)} for each tag.
        quality is 'Good', 'Bad', or an OPC quality string.
        """
        if not _openopc_available:
            return {t: (None, "OpenOPC not installed") for t in tag_paths}

        results: dict[str, tuple[Optional[float], str]] = {}
        with _opc_lock:
            try:
                self._ensure_connected()
                raw = self._opc.read(tag_paths, include_error=True)
                # OpenOPC returns: [(tag, value, quality, timestamp), ...]
                for item in raw:
                    tag, value, quality, *rest = item
                    try:
                        v = float(value) if value is not None else None
                    except (TypeError, ValueError):
                        v = None
                    results[tag] = (v, quality or "Unknown")
            except Exception as e:
                logger.error("OPC DA read error (%s): %s", self.host, e)
                self._opc = None  # force reconnect next call
                results = {t: (None, "Bad") for t in tag_paths}
        return results

    def browse(self, root: str = "") -> list[str]:
        """Return a flat list of all browsable OPC item paths under root."""
        if not _openopc_available:
            return []
        tags = []
        with _opc_lock:
            try:
                self._ensure_connected()
                items = self._opc.list(root + "*", recursive=True, include_type=False)
                tags = list(items) if items else []
            except Exception as e:
                logger.error("OPC DA browse error (%s): %s", self.host, e)
                self._opc = None
        return tags

    def list_servers(self) -> list[str]:
        """List OPC DA servers registered on the target host."""
        if not _openopc_available:
            return []
        with _opc_lock:
            try:
                if self.host.lower() in ("localhost", "127.0.0.1"):
                    opc = OpenOPC.client()
                    servers = opc.servers()
                else:
                    opc = OpenOPC.open_client(host=self.host)
                    servers = opc.servers()
                return list(servers)
            except Exception as e:
                logger.error("OPC DA list_servers error (%s): %s", self.host, e)
                return []

    def close(self):
        if self._opc:
            try:
                self._opc.close()
            except Exception:
                pass
            self._opc = None


# Module-level cache: one reader per (host, server)
_readers: dict[tuple, OpcDaReader] = {}


def get_reader(host: str, server: str) -> OpcDaReader:
    key = (host, server)
    if key not in _readers:
        _readers[key] = OpcDaReader(host, server)
    return _readers[key]

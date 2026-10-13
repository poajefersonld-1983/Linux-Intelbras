"""Local configuration and lifecycle for the MIT iSIC transport.

Application parameters are supplied locally, never bundled with the package.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from .p2p_transport.client import Configuration, Transport, LoopbackProxy


class P2PError(Exception):
    pass


def settings_path() -> Path:
    configured = os.environ.get("XDG_CONFIG_HOME", "")
    root = Path(configured) if configured and Path(configured).is_absolute() else Path.home() / ".config"
    return root / "simnext-linux" / "p2p.json"


def load_settings(path: Path | None = None) -> dict:
    try:
        data = json.loads((path or settings_path()).read_text(encoding="utf-8"))
        for key, maximum in (("application_key", 31), ("p2p_password", 128)):
            value = data[key]
            if not isinstance(value, str) or not value or len(value) > maximum or any(ord(c) < 32 or ord(c) > 126 for c in value):
                raise ValueError()
        return {key: data[key] for key in ("application_key", "p2p_password")}
    except (OSError, ValueError, KeyError, TypeError):
        raise P2PError("A configuração local do transporte Intelbras Cloud está indisponível.") from None


class P2PBridge:
    def __init__(self, serial: str, remote_port: int, *, settings: dict | None = None):
        settings = load_settings() if settings is None else settings
        self.transport = Transport(Configuration("intelbrasp2p.com.br", 1250,
                                   settings["application_key"], settings["p2p_password"],
                                   serial, remote_port))
        self.proxy = None
        self._lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._closed = threading.Event()

    def start(self) -> int:
        with self._start_lock:
            if self.proxy is not None and not self._closed.is_set():
                return self.proxy.port
            return self._start()

    def _start(self) -> int:
        try:
            if self._closed.is_set():
                raise P2PError("Conexão cancelada.")
            self.transport.connect(timeout=15)
            with self._lock:
                if self._closed.is_set():
                    raise P2PError("Conexão cancelada.")
                self.proxy = LoopbackProxy(self.transport)
                return self.proxy.port
        except P2PError:
            self.close()
            raise
        except Exception:
            metrics = getattr(self.transport, "metrics", {})
            self.close()
            if metrics.get("device_offers") and not metrics.get("peer_hellos"):
                raise P2PError("O Cloud encontrou o equipamento, mas a conexão direta não respondeu. As rotas direta e relay não concluíram a negociação; a senha não foi testada.") from None
            raise P2PError("Não foi possível conectar ao equipamento pelo Intelbras Cloud. Verifique se ele está online e tente novamente.") from None

    def close(self):
        self._closed.set()
        with self._lock:
            proxy, self.proxy = self.proxy, None
        if proxy is not None:
            proxy.close()
        self.transport.close()


class BridgeLease:
    """One tile's reference to a transport shared by up to four RTSP streams."""
    def __init__(self, pool, entry):
        self.pool, self.entry = pool, entry
        self.released = False

    def start(self):
        return self.entry["bridge"].start()

    def close(self):
        with self.pool.lock:
            if self.released:
                return
            self.released = True
            self.entry["users"] -= 1
            last = self.entry["users"] == 0
            if last:
                self.pool.entries.remove(self.entry)
        if last:
            self.entry["bridge"].close()


class BridgePool:
    def __init__(self):
        self.lock = threading.Lock()
        self.entries = []

    def acquire(self, serial, port):
        with self.lock:
            entry = next((item for item in self.entries
                          if item["key"] == (serial, port) and item["users"] < 4), None)
            if entry is None:
                entry = {"key": (serial, port), "bridge": P2PBridge(serial, port), "users": 0}
                self.entries.append(entry)
            entry["users"] += 1
            return BridgeLease(self, entry)


bridge_pool = BridgePool()

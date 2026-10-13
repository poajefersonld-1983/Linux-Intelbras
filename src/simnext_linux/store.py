"""Persistência local; senhas não fazem parte do esquema."""

import os
import base64
import hashlib
import secrets
from pathlib import Path
import sqlite3
from dataclasses import asdict

from .domain import Device


def default_database_path() -> Path:
    configured = os.environ.get("XDG_DATA_HOME", "")
    root = Path(configured) if configured and Path(configured).is_absolute() else Path.home() / ".local" / "share"
    return root / "simnext-linux" / "devices.sqlite3"


class DeviceStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else default_database_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            self.path.parent.chmod(0o700)
        self._db = sqlite3.connect(self.path)
        self._db.row_factory = sqlite3.Row
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2):
            self._db.close()
            raise ValueError("O cadastro foi criado por uma versão mais recente do aplicativo.")
        with self._db:
            self._db.execute("BEGIN")
            self._db.execute("""CREATE TABLE IF NOT EXISTS devices (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, host TEXT NOT NULL,
                port INTEGER NOT NULL CHECK(port BETWEEN 1 AND 65535),
                channel INTEGER NOT NULL CHECK(channel BETWEEN 1 AND 65535),
                subtype INTEGER NOT NULL CHECK(subtype IN (0, 1)),
                username TEXT NOT NULL, path TEXT NOT NULL
            )""")
            if version < 2:
                self._db.execute("ALTER TABLE devices ADD COLUMN serial TEXT NOT NULL DEFAULT ''")
                self._db.execute("ALTER TABLE devices ADD COLUMN connection TEXT NOT NULL DEFAULT 'rtsp'")
            self._db.execute("PRAGMA user_version=2")
            self._db.execute("CREATE TABLE IF NOT EXISTS app_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        if os.name == "posix":
            self.path.chmod(0o600)

    def list(self) -> list[Device]:
        devices = [Device(**dict(row)) for row in self._db.execute("SELECT * FROM devices ORDER BY name COLLATE NOCASE, id")]
        for device in devices:
            device.validate()
        return devices

    def save(self, device: Device) -> None:
        device.validate()
        with self._db:
            self._db.execute("""INSERT INTO devices (id,name,host,port,channel,subtype,username,path,serial,connection)
                VALUES (:id,:name,:host,:port,:channel,:subtype,:username,:path,:serial,:connection)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name, host=excluded.host,
                port=excluded.port, channel=excluded.channel, subtype=excluded.subtype,
                username=excluded.username, path=excluded.path,
                serial=excluded.serial, connection=excluded.connection""", asdict(device))

    def delete(self, device_id: str) -> None:
        with self._db:
            self._db.execute("DELETE FROM devices WHERE id=?", (device_id,))

    def close(self) -> None:
        self._db.close()

    def cloud_installation_id(self) -> str:
        row = self._db.execute("SELECT value FROM app_state WHERE key='cloud_installation_id'").fetchone()
        if row:
            value = row[0]
            try:
                if len(base64.b64decode(value, validate=True)) != 32:
                    raise ValueError()
            except (ValueError, TypeError):
                raise ValueError("Identificador local da conta inválido.") from None
            return value
        value = base64.b64encode(hashlib.sha256(secrets.token_bytes(32)).digest()).decode("ascii")
        with self._db:
            self._db.execute("INSERT INTO app_state VALUES ('cloud_installation_id', ?)", (value,))
        return value

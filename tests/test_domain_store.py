from pathlib import Path
import sqlite3
import tempfile
import unittest
from dataclasses import replace

from simnext_linux.domain import Device, build_rtsp_url
from simnext_linux.store import DeviceStore
from simnext_linux.__main__ import redact_media_message


class DeviceTests(unittest.TestCase):
    def setUp(self):
        self.device = Device("test-id", "Entrada", "192.168.1.20")

    def test_rtsp_encodes_credentials_and_uses_channel_one_based(self):
        device = replace(self.device, username="operador@local", channel=4, subtype=0)
        self.assertEqual(build_rtsp_url(device, "a:b@ c/#"), "rtsp://operador%40local:a%3Ab%40%20c%2F%23@192.168.1.20:554/cam/realmonitor?channel=4&subtype=0")

    def test_ipv6_and_custom_rtsp_path(self):
        device = replace(self.device, host="[2001:db8::1]", path="/live/0?stream=2", username="")
        self.assertEqual(build_rtsp_url(device, ""), "rtsp://[2001:db8::1]:554/live/0?stream=2")

    def test_invalid_addresses_cannot_change_url_authority(self):
        for host in ["rtsp://192.168.1.20", "host@evil.example", "host:554", "999.1.1.1", "bad name", "host/path", "host\\path"]:
            with self.subTest(host=host), self.assertRaises(ValueError):
                build_rtsp_url(replace(self.device, host=host), "")

    def test_invalid_ports_and_channel(self):
        for field, value in [("port", 0), ("port", 65536), ("channel", 0), ("subtype", 2), ("path", "//evil/x"), ("path", "/live%xy")]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                replace(self.device, **{field: value}).validate()

    def test_media_diagnostics_do_not_keep_credentials(self):
        message = 'Failure opening "RTSP://admin:my%40secret@192.0.2.20/live?token=abc"'
        cleaned = redact_media_message(message)
        self.assertNotIn("secret", cleaned)
        self.assertNotIn("admin", cleaned)
        self.assertNotIn("token", cleaned)
        self.assertIn("omitido", cleaned)

    def test_sqlite_roundtrip_update_delete_without_password_column(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "devices.sqlite3"
            store = DeviceStore(path)
            store.save(self.device)
            store.close()
            store = DeviceStore(path)
            self.assertEqual(store.list(), [self.device])
            store.save(replace(self.device, name="Portão"))
            self.assertEqual(store.list()[0].name, "Portão")
            columns = [row[1] for row in store._db.execute("PRAGMA table_info(devices)")]
            self.assertNotIn("password", columns)
            store.delete(self.device.id)
            self.assertEqual(store.list(), [])
            store.close()

    def test_installation_id_persists_without_account_tokens(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "devices.sqlite3"
            store = DeviceStore(path)
            identity = store.cloud_installation_id()
            self.assertEqual(len(identity), 44)
            store.close()
            store = DeviceStore(path)
            self.assertEqual(store.cloud_installation_id(), identity)
            self.assertEqual([row[0] for row in store._db.execute("SELECT key FROM app_state")], ["cloud_installation_id"])
            store.close()


if __name__ == "__main__":
    unittest.main()


class CloudPersistenceTests(unittest.TestCase):
    def test_cloud_device_roundtrip_and_no_rtsp_url(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "devices.sqlite3"
            device = Device("cloud", "Cloud", "", serial="TEST-SERIAL", connection="cloud")
            store = DeviceStore(path)
            store.save(device)
            store.close()
            store = DeviceStore(path)
            self.assertEqual(store.list(), [device])
            with self.assertRaisesRegex(ValueError, "Cloud/P2P"):
                build_rtsp_url(device, "do-not-use")
            store.close()

    def test_version_one_database_migrates_without_losing_devices(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "legacy.sqlite3"
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE devices (id TEXT PRIMARY KEY, name TEXT, host TEXT, port INTEGER, channel INTEGER, subtype INTEGER, username TEXT, path TEXT)")
                db.execute("INSERT INTO devices VALUES ('legacy','Exemplo','192.0.2.10',554,1,1,'admin','/cam/realmonitor')")
                db.execute("PRAGMA user_version=1")
            store = DeviceStore(path)
            devices = store.list()
            self.assertEqual(devices[0].id, "legacy")
            self.assertEqual(devices[0].connection, "rtsp")
            self.assertEqual(devices[0].serial, "")
            self.assertEqual(store._db.execute("PRAGMA user_version").fetchone()[0], 2)
            store.save(replace(devices[0], serial="TEST"))
            store.close()

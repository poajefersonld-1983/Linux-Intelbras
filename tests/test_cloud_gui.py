import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from PyQt6.QtCore import QEventLoop, QTimer
from PyQt6.QtWidgets import QApplication

from simnext_linux.cloud import CloudChannel, CloudDevice
from simnext_linux.cloud_gui import CloudDialog
from simnext_linux.store import DeviceStore
from simnext_linux.gui import MainWindow


class FakeAttempt:
    authorization_url = "https://example.invalid/authorize"

    def __init__(self, block=False):
        self.block = block
        self.closed = False

    def wait_for_code(self, cancel, timeout=180):
        if self.block:
            cancel.wait(2)
        return "synthetic-code"

    def close(self):
        self.closed = True


class FakeClient:
    def __init__(self, block=False):
        self.authenticated = False
        self.attempt = FakeAttempt(block)
        self.network_threads = []
        self.cancel_events = []
        self.records = [CloudDevice(id="fixture", name="Exemplo", serial="TEST-001",
                                    model="NVR de teste", host="192.0.2.10", username="admin",
                                    channels=(CloudChannel(index=1, name="Entrada"),))]

    def create_login(self):
        return self.attempt

    def set_cancel_event(self, event):
        self.cancel_events.append(event)

    def finish_login(self, attempt, code):
        self.network_threads.append(threading.get_ident())
        self.authenticated = True

    def list_devices(self, source="simnext"):
        self.last_source = source
        self.network_threads.append(threading.get_ident())
        return self.records

    def logout(self):
        self.network_threads.append(threading.get_ident())
        self.authenticated = False


class CloudGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = DeviceStore(Path(self.temp.name) / "devices.json")
        self.import_notifications = []
        self.client = FakeClient()
        self.dialog = CloudDialog(self.client, self.store, lambda: self.import_notifications.append(True))

    def tearDown(self):
        self.dialog.close()
        self.store.close()
        self.temp.cleanup()

    def finish_operation(self):
        loop = QEventLoop()
        self.dialog.worker.finished.connect(loop.quit)
        QTimer.singleShot(3000, loop.quit)
        loop.exec()
        self.app.processEvents()
        self.assertIsNone(self.dialog.worker)

    def test_login_import_logout(self):
        self.assertEqual(self.client.network_threads, [])
        with patch("simnext_linux.cloud_gui.QDesktopServices.openUrl", return_value=True):
            self.dialog.start("login")
            self.finish_operation()
        self.assertTrue(self.client.authenticated)
        self.assertEqual(self.client.last_source, "isic")
        self.assertTrue(self.client.attempt.closed)
        self.assertIsInstance(self.client.cancel_events[0], threading.Event)
        self.assertIsNone(self.client.cancel_events[-1])
        self.assertEqual(self.dialog.table.rowCount(), 1)
        self.assertTrue(all(t != threading.get_ident() for t in self.client.network_threads))
        self.dialog.table.selectRow(0)
        self.dialog.import_selected()
        self.assertEqual(len(self.store.list()), 1)
        self.assertEqual(self.import_notifications, [True])
        self.dialog.start("logout")
        self.finish_operation()
        self.assertFalse(self.client.authenticated)
        self.assertEqual(self.dialog.table.rowCount(), 0)

    def test_reopened_authenticated_dialog_refreshes_default_catalog(self):
        self.dialog.close()
        self.client.authenticated = True
        self.dialog = CloudDialog(self.client, self.store, lambda: None)
        self.app.processEvents()
        self.assertIsNotNone(self.dialog.worker)
        self.finish_operation()
        self.assertEqual(self.dialog.table.rowCount(), 1)
        self.assertEqual(self.client.last_source, "isic")

    def test_playback_login_closes_dialog_after_catalog_is_ready(self):
        self.dialog.return_after_login = True
        with patch("simnext_linux.cloud_gui.QDesktopServices.openUrl", return_value=True):
            self.dialog.start("login")
            self.finish_operation()
        self.assertTrue(self.client.authenticated)
        self.assertEqual(self.dialog.result(), CloudDialog.DialogCode.Accepted)
        self.assertEqual(len(self.dialog.records), 1)

    def test_cancel_closes_listener(self):
        self.client.attempt.block = True
        with patch("simnext_linux.cloud_gui.QDesktopServices.openUrl", return_value=True):
            self.dialog.start("login")
            self.dialog.cancel()
            self.finish_operation()
        self.assertTrue(self.client.attempt.closed)
        self.assertFalse(self.client.authenticated)
        self.assertEqual(self.dialog.table.rowCount(), 0)

    def test_cloud_only_import_preserves_serial_without_prompt(self):
        original = self.client.records[0]
        record = CloudDevice(id=original.id, name=original.name, serial=original.serial,
                             model=original.model, host=None, username=original.username,
                             channels=original.channels)
        self.dialog.populate([record])
        self.dialog.table.selectRow(0)
        with patch("simnext_linux.cloud_gui.QInputDialog.getText") as prompt:
            self.dialog.import_selected()
        prompt.assert_not_called()
        device = self.store.list()[0]
        self.assertEqual(device.host, "")
        self.assertEqual(device.serial, "TEST-001")
        self.assertEqual(device.connection, "cloud")

    def test_main_close_clears_session(self):
        window = MainWindow(self.store)
        self.client.authenticated = True
        window.cloud_client = self.client
        window.close()
        self.assertFalse(self.client.authenticated)


if __name__ == "__main__":
    unittest.main()

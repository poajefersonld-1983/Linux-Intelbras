"""Offline Qt smoke tests; never start a stream."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import struct
import unittest
from pathlib import Path
from unittest.mock import patch

from PyQt6.QtGui import QImage
from PyQt6.QtCore import Qt, QByteArray, QBuffer, QIODevice, QUrl, QEventLoop, QTimer
from PyQt6.QtMultimedia import QVideoFrame
from PyQt6.QtWidgets import QApplication

from simnext_linux.gui import DeviceDialog, MainWindow
from simnext_linux.store import DeviceStore


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = DeviceStore(Path(self.temp.name) / "devices.json")
        self.window = MainWindow(self.store)

    def tearDown(self):
        self.window.close()
        self.store.close()
        self.temp.cleanup()

    def test_dialog_and_mosaic(self):
        dialog = DeviceDialog(self.window)
        dialog.name.setText("Exemplo")
        dialog.host.setText("192.0.2.10")
        dialog.save()
        self.assertIsNotNone(dialog.result_device)
        self.store.save(dialog.result_device)
        self.window.refresh()
        self.assertEqual(self.window.devices.count(), 1)
        for index, count in [(2, 9), (1, 4), (0, 1)]:
            self.window.mosaic.setCurrentIndex(index)
            self.assertEqual(self.window.grid.count(), count)
            self.assertTrue(all(tile.device_id is None for tile in self.window.tiles))
            self.assertTrue(all(tile.audio.isMuted() for tile in self.window.tiles))

    def test_multiple_cameras_keep_occupied_tile_and_use_channel_numbers(self):
        from simnext_linux.domain import Device
        device = Device("camera", "DVR", "192.0.2.1")
        self.store.save(device); self.window.refresh()
        self.window.devices.setCurrentRow(0)
        for i in range(self.window.channels.count()):
            self.window.channels.item(i).setSelected(i in (1, 3))
        self.window.tiles[0].device_id = "already-playing"
        with patch("simnext_linux.gui.QInputDialog.getText", return_value=("", True)), patch("simnext_linux.gui.VideoTile.connect_device") as connect:
            self.window.connect_selected()
        self.assertEqual(self.window.mosaic.currentData(), 4)
        self.assertEqual(self.window.tiles[0].device_id, "already-playing")
        self.assertEqual([call.args[0].channel for call in connect.call_args_list], [2, 4])
        self.assertEqual(self.store.list()[0].channel, 1)

    def test_expansion_preserves_destination(self):
        self.window.mosaic.setCurrentIndex(1)
        self.window.target.setCurrentIndex(3)
        assignments = self.window.mosaic_assignments(list(range(1, 6)))
        self.assertEqual(self.window.mosaic.currentData(), 9)
        self.assertEqual(assignments[0][1].number, 4)
        self.assertEqual(self.window.target.currentData(), 3)

    def test_mosaic_capacity_does_not_replace_existing_video(self):
        self.window.tiles[0].device_id = "playing"
        with self.assertRaises(ValueError):
            self.window.mosaic_assignments(list(range(1, 10)))
        self.assertEqual(self.window.tiles[0].device_id, "playing")

    def test_drop_two_clients_on_specific_tiles(self):
        from simnext_linux.domain import Device
        from simnext_linux.gui import DEVICE_ROLE
        from PyQt6.QtCore import QPoint, QPointF
        from PyQt6.QtGui import QDragEnterEvent, QDropEvent
        self.store.save(Device("a", "Cliente A", "192.0.2.1"))
        self.store.save(Device("b", "Cliente B", "192.0.2.2"))
        self.window.refresh()
        self.window.all_clients.setChecked(True)
        self.window.refresh_channels()
        self.window.mosaic.setCurrentIndex(1)
        for i in range(self.window.channels.count()):
            item = self.window.channels.item(i)
            item.setSelected((item.data(DEVICE_ROLE), item.data(Qt.ItemDataRole.UserRole)) in [("a", 2), ("b", 4)])
        mime = self.window.channels.camera_mime()
        self.assertNotIn(b"password", bytes(mime.data("application/x-monitor-linux-cameras")))
        self.window.tiles[0].device_id = "preserved"
        self.window.tiles[2].device_id = "replace-this"
        tile = self.window.tiles[2]
        enter = QDragEnterEvent(QPoint(5, 5), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        self.assertTrue(tile.eventFilter(tile.cloud_image, enter))
        drop = QDropEvent(QPointF(5, 5), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        opened = []
        def connect(target, device, password):
            target.device_id = device.id
            target.channel_number = device.channel
            opened.append((target.number, device.id, device.channel))
        with patch("simnext_linux.gui.QInputDialog.getText", return_value=("secret", True)) as prompt, patch("simnext_linux.gui.VideoTile.connect_device", new=connect):
            self.assertTrue(tile.eventFilter(tile.cloud_image, drop))
            self.assertEqual(prompt.call_count, 2)
        self.assertEqual(opened, [(3, "a", 2), (4, "b", 4)])
        self.assertEqual(self.window.tiles[0].device_id, "preserved")
        self.assertEqual(self.window._passwords, {"a": "secret", "b": "secret"})
        with patch("simnext_linux.gui.QInputDialog.getText") as prompt, patch("simnext_linux.gui.VideoTile.connect_device", new=connect):
            self.window.drop_cameras(mime, 2)
            prompt.assert_not_called()

    def test_drop_cancel_keeps_video_and_invalid_payload_is_rejected(self):
        from simnext_linux.domain import Device
        from simnext_linux.gui import CAMERA_MIME
        from PyQt6.QtCore import QMimeData
        import json
        self.store.save(Device("a", "Cliente A", "192.0.2.1"))
        self.window.tiles[0].device_id = "playing"
        mime = QMimeData(); mime.setData(CAMERA_MIME, json.dumps([{"device": "a", "channel": 2}]).encode())
        with patch("simnext_linux.gui.QInputDialog.getText", return_value=("", False)), patch("simnext_linux.gui.VideoTile.connect_device") as connect:
            self.window.drop_cameras(mime, 0)
            connect.assert_not_called()
        self.assertEqual(self.window.tiles[0].device_id, "playing")
        mime.setData(CAMERA_MIME, b'[{"device":"unknown","channel":1}]')
        with patch("simnext_linux.gui.QMessageBox.information"), patch("simnext_linux.gui.VideoTile.connect_device") as connect:
            self.window.drop_cameras(mime, 0)
            connect.assert_not_called()
        self.assertEqual(self.window.tiles[0].device_id, "playing")

    def test_online_badge_requires_received_image(self):
        from simnext_linux.domain import Device
        self.store.save(Device("a", "Cliente A", "192.0.2.1")); self.window.refresh(); self.window.refresh_channels()
        self.window.tiles[0].device_id = "a"; self.window.tiles[0].channel_number = 1
        self.window.update_camera_activity()
        self.assertIn("Conectando", self.window.channels.item(0).text())
        image = QImage(20, 20, QImage.Format.Format_RGB32)
        image.fill(0xFF123456)
        self.window.tiles[0].frame_changed(QVideoFrame(image))
        self.assertIn("Ao vivo", self.window.channels.item(0).text())
        self.window.tiles[0].disconnect()
        self.assertNotIn("Ao vivo", self.window.channels.item(0).text())

    def test_single_click_opens_selected_tile_and_advances(self):
        from simnext_linux.domain import Device
        from PyQt6.QtTest import QTest
        self.store.save(Device("a", "Cliente A", "192.0.2.1")); self.window.refresh()
        self.window.show(); self.app.processEvents()
        QTest.mouseClick(self.window.tiles[2].placeholder, Qt.MouseButton.LeftButton)
        self.assertEqual(self.window.target.currentData(), 2)
        opened = []
        def connect(tile, device, password):
            tile.device_id = device.id; tile.channel_number = device.channel
            opened.append((tile.number, device.channel))
        self.window._passwords["a"] = "secret"
        with patch("simnext_linux.gui.VideoTile.connect_device", new=connect), patch("simnext_linux.gui.QInputDialog.getText") as prompt:
            item = self.window.channels.item(1)
            point = self.window.channels.visualItemRect(item).center()
            QTest.mouseClick(self.window.channels.viewport(), Qt.MouseButton.LeftButton, pos=point)
            self.assertEqual(opened, [(3, 2)])
            self.assertEqual(self.window.target.currentData(), 0)
            prompt.assert_not_called()

    def test_cloud_camera_uses_account_login_instead_of_dvr_password(self):
        from simnext_linux.domain import Device
        device = Device("a", "Cliente A", "", serial="EXAMPLE", connection="cloud")
        self.store.save(device); self.window.refresh()
        class Session:
            authenticated = True
            def device_password(self, identity): return "session-password"
            def logout(self): pass
        def login(**kwargs): self.window.cloud_client = Session()
        with patch.object(self.window, "open_cloud_account", side_effect=login) as account, patch("simnext_linux.gui.QInputDialog.getText") as prompt, patch("simnext_linux.gui.VideoTile.connect_device") as connect:
            self.window.open_cameras([(device, self.window.tiles[0])])
            account.assert_called_once(); prompt.assert_not_called()
            self.assertEqual(connect.call_args.args[1], "session-password")

    def test_cancel_cloud_login_does_not_request_dvr_password(self):
        from simnext_linux.domain import Device
        device = Device("a", "Cliente A", "", serial="EXAMPLE", connection="cloud")
        with patch.object(self.window, "open_cloud_account"), patch("simnext_linux.gui.QInputDialog.getText") as prompt, patch("simnext_linux.gui.VideoTile.connect_device") as connect:
            self.window.open_cameras([(device, self.window.tiles[0])])
            prompt.assert_not_called(); connect.assert_not_called()
        self.assertIn("Conta Intelbras", self.window.account_notice.text())

    def test_temporary_video_failure_retries_without_losing_password(self):
        from simnext_linux.domain import Device
        from unittest.mock import Mock
        tile = self.window.tiles[0]
        device = Device("a", "Cliente A", "", serial="EXAMPLE", connection="cloud")
        tile._active_device = device; tile._session_password = "secret"
        tile.device_id = device.id
        tile.cloud_video_worker = Mock(); tile.cloud_video_worker.isRunning.return_value = False
        failed = []; tile.connection_failed.connect(failed.append)
        tile.p2p_failed("O DVR não iniciou o vídeo.", tile._generation)
        self.assertEqual(failed, [])
        self.assertEqual(tile._retry_context[1], "secret")
        self.assertTrue(tile._retry_timer.isActive())
        with patch.object(tile, "connect_device") as connect:
            tile.retry_video()
            connect.assert_called_once_with(device, "secret")
        tile.disconnect()
        self.assertFalse(tile._retry_timer.isActive())
        self.assertIsNone(tile._retry_context)
        self.assertIsNone(tile._session_password)

    def test_refused_credentials_are_not_retried(self):
        from simnext_linux.domain import Device
        from unittest.mock import Mock
        tile = self.window.tiles[0]
        tile._active_device = Device("a", "Cliente A", "", serial="EXAMPLE", connection="cloud")
        tile.device_id = "a"; tile._session_password = "wrong"
        self.window._passwords["a"] = "wrong"
        tile.cloud_video_worker = Mock(); tile.cloud_video_worker.isRunning.return_value = False
        tile.p2p_failed("O DVR recusou a autenticação.", tile._generation)
        self.assertFalse(tile._retry_timer.isActive())
        self.assertNotIn("a", self.window._passwords)
        self.assertIsNone(tile._session_password)

    def test_channel_count_is_remembered_per_client(self):
        from simnext_linux.domain import Device
        self.store.save(Device("a", "Cliente A", "192.0.2.1"))
        self.store.save(Device("b", "Cliente B", "192.0.2.2")); self.window.refresh()
        self.window.devices.setCurrentRow(0); self.window.channel_limit.setValue(4)
        self.window.devices.setCurrentRow(1); self.window.channel_limit.setValue(8)
        self.window.devices.setCurrentRow(0)
        self.assertEqual(self.window.channels.count(), 4)
        reopened = MainWindow(self.store)
        self.assertEqual(reopened.channels.count(), 4)
        reopened.all_clients.setChecked(True)
        self.assertEqual(reopened.channels.count(), 12)
        reopened.close()

    def test_quality_switch_reuses_cloud_tunnel_and_credential(self):
        from simnext_linux.domain import Device
        from types import SimpleNamespace
        from unittest.mock import Mock
        tile = self.window.tiles[0]
        device = Device("a", "Cliente A", "", serial="EXAMPLE", connection="cloud")
        tile._active_device = device; tile._session_password = "fixture-password"; tile.device_id = device.id
        lease = SimpleNamespace(entry={"bridge": SimpleNamespace(proxy=SimpleNamespace(port=12345))}, close=lambda: None)
        tile.p2p_bridge = lease
        tile.cloud_video_worker = Mock(); tile.cloud_video_worker.isRunning.return_value = False
        self.window.tiles[1].device_id = "other-client"
        with patch("simnext_linux.gui.CloudVideoWorker") as factory:
            factory.return_value.isRunning.return_value = False
            tile.quality.setCurrentIndex(0); tile.quality.setCurrentIndex(1)
            self.assertIn("subtype=0", factory.call_args.args[0])
            self.assertEqual(factory.call_args.kwargs["size"], (1280, 720))
            self.assertIs(tile.p2p_bridge, lease)
            self.assertEqual(tile._session_password, "fixture-password")
            self.assertEqual(self.window.tiles[1].device_id, "other-client")

    def test_quick_quality_changes_open_only_the_latest_stream(self):
        from simnext_linux.domain import Device
        from types import SimpleNamespace
        from unittest.mock import Mock
        tile = self.window.tiles[0]
        tile._active_device = Device("a", "Cliente A", "", serial="EXAMPLE", connection="cloud")
        tile._session_password = "fixture-password"; tile.device_id = "a"
        tile.p2p_bridge = SimpleNamespace(entry={"bridge": SimpleNamespace(proxy=SimpleNamespace(port=12345))}, close=lambda: None)
        old = Mock(); old.isRunning.return_value = True
        callbacks = []; old.finished.connect.side_effect = callbacks.append
        tile.cloud_video_worker = old
        with patch("simnext_linux.gui.CloudVideoWorker") as factory:
            factory.return_value.isRunning.return_value = False
            tile.quality.setCurrentIndex(0); tile.quality.setCurrentIndex(1); tile.quality.setCurrentIndex(0)
            for callback in callbacks: callback()
            self.assertEqual(factory.call_count, 1)
            self.assertIn("subtype=1", factory.call_args.args[0])
            self.assertEqual(factory.call_args.kwargs["size"], (640, 360))

    def test_fullscreen_mosaic_preserves_tiles_quality_and_destination(self):
        from PyQt6.QtTest import QTest
        self.window.mosaic.setCurrentIndex(2); self.window.select_tile(3)
        tile = self.window.tiles[0]; tile.device_id = "playing"
        tile.quality.setCurrentIndex(1)
        generation = tile._generation
        self.window.show(); self.window.activateWindow(); self.app.processEvents()
        QTest.mouseClick(self.window.mosaic_fullscreen_button, Qt.MouseButton.LeftButton)
        self.app.processEvents()
        self.assertTrue(self.window.isFullScreen())
        self.assertTrue(self.window.sidebar.isHidden())
        self.assertEqual(self.window.grid.count(), 9)
        self.assertEqual(self.window.target.currentData(), 3)
        self.assertEqual(tile.quality.currentData(), 0)
        self.assertEqual(tile.device_id, "playing")
        self.assertEqual(tile._generation, generation)
        self.assertTrue(all(widget.isHidden() for widget in tile._chrome_widgets))
        self.assertTrue(self.window.fullscreen_exit.isHidden())
        self.window.exit_fullscreen()
        self.app.processEvents()
        self.assertFalse(self.window.isFullScreen())
        self.assertFalse(self.window.sidebar.isHidden())
        self.assertEqual(tile._generation, generation)

    def test_f11_and_escape_restore_maximized_mosaic(self):
        from PyQt6.QtTest import QTest
        self.window.showMaximized(); self.window.activateWindow(); self.app.processEvents()
        QTest.keyClick(self.window, Qt.Key.Key_F11); self.app.processEvents()
        self.assertTrue(self.window._mosaic_fullscreen)
        QTest.keyClick(self.window, Qt.Key.Key_Escape); self.app.processEvents()
        self.assertFalse(self.window._mosaic_fullscreen)
        self.assertTrue(self.window.isMaximized())
        self.assertEqual(self.window.mosaic.currentData(), 4)

    def test_new_camera_opens_hd_even_with_legacy_sd_registration(self):
        from simnext_linux.domain import Device
        from unittest.mock import Mock
        tile = self.window.tiles[0]
        tile.player = Mock()
        self.assertEqual(tile.quality.currentData(), 0)
        tile.connect_device(Device("a", "Cliente", "192.0.2.1", subtype=1), "fixture")
        self.assertEqual(tile._active_device.subtype, 0)
        self.assertIn("subtype=0", tile.player.setSource.call_args.args[0].toString())

    def test_frame_snapshot_and_redacted_error(self):
        tile = self.window.tiles[0]
        tile.device_id = "example"
        image = QImage(20, 20, QImage.Format.Format_RGB32)
        image.fill(0xFF123456)
        tile.frame_changed(QVideoFrame(image))
        self.assertEqual(tile.status.text(), "Reproduzindo")
        self.assertIs(tile.display.currentWidget(), tile.video)
        capture = str(Path(self.temp.name) / "capture.png")
        with patch("simnext_linux.gui.QFileDialog.getSaveFileName", return_value=(capture, "PNG")):
            tile.capture()
        self.assertFalse(QImage(capture).isNull())
        tile.failed(None, "rtsp://admin:secret@192.0.2.10")
        self.assertIsNone(tile.device_id)
        self.assertIsNone(tile.last_image)
        self.assertIs(tile.display.currentWidget(), tile.placeholder)
        self.assertTrue(tile.player.source().isEmpty())
        self.assertNotIn("secret", tile.status.text())

    def test_local_mjpeg_decode(self):
        # Synthetic local fixture exercises the installed multimedia decoder.
        # It contains no original software, device stream or credentials.
        image = QImage(32, 24, QImage.Format.Format_RGB32)
        image.fill(0xFF2850A0)
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        self.assertTrue(image.save(buffer, "JPEG"))
        jpeg = bytes(data)
        def chunk(tag, payload):
            return tag + struct.pack("<I", len(payload)) + payload + (b"\0" if len(payload) % 2 else b"")
        def listing(tag, payload):
            return chunk(b"LIST", tag + payload)
        header = struct.pack("<14I", 100000, len(jpeg) * 10, 0, 16, 10, 0, 1, len(jpeg), 32, 24, 0, 0, 0, 0)
        stream = struct.pack("<4s4sIHH8I4h", b"vids", b"MJPG", 0, 0, 0, 0, 1, 10, 0, 10, len(jpeg), 0xFFFFFFFF, 0, 0, 0, 32, 24)
        bitmap = struct.pack("<IiiHH4sIiiII", 40, 32, 24, 1, 24, b"MJPG", len(jpeg), 0, 0, 0, 0)
        frames = chunk(b"00dc", jpeg) * 10
        payload = b"AVI " + listing(b"hdrl", chunk(b"avih", header) + listing(b"strl", chunk(b"strh", stream) + chunk(b"strf", bitmap))) + listing(b"movi", frames)
        path = Path(self.temp.name) / "synthetic.avi"
        path.write_bytes(chunk(b"RIFF", payload))
        tile = self.window.tiles[0]
        tile.device_id = "local-test"
        loop = QEventLoop()
        tile.video.videoSink().videoFrameChanged.connect(lambda frame: loop.quit() if frame.isValid() else None)
        QTimer.singleShot(5000, loop.quit)
        tile.player.setSource(QUrl.fromLocalFile(str(path)))
        tile.player.play()
        loop.exec()
        self.assertIsNotNone(tile.last_image, "Installed Qt multimedia backend did not decode local MJPEG")
        self.assertEqual(tile.status.text(), "Reproduzindo")
        tile.disconnect()


if __name__ == "__main__":
    unittest.main()

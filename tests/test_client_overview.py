from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication
from simnext_linux.domain import Device
from simnext_linux.gui import MainWindow, VideoTile
from simnext_linux.store import DeviceStore


class ClientOverviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder = TemporaryDirectory()
        self.store = DeviceStore(Path(self.folder.name) / 'devices.sqlite3')
        for identity, name in [('a', 'Condomínio Jardim'), ('b', 'Edifício Central')]:
            self.store.save(Device(identity, name, '', serial='SYNTHETIC' + identity, connection='cloud'))
        self.window = MainWindow(self.store)

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.store.close()
        self.folder.cleanup()

    def connect_stub(self, calls):
        def connect(tile, device, password):
            calls.append((tile.number, device.id, device.channel, password))
            tile.device_id, tile.channel_number = device.id, device.channel
        return connect

    def test_startup_selects_client_without_opening_any_connection(self):
        self.assertEqual(self.window.devices.currentRow(), 0)
        self.assertTrue(all(tile.device_id is None and not tile._workers for tile in self.window.tiles))

    def test_client_mouse_click_opens_first_four_channels_in_order(self):
        calls = []
        self.window.cloud_client = SimpleNamespace(device_password=lambda identity: 'SYNTHETIC-PASSWORD', logout=lambda: None)
        self.window.resize(1280, 800); self.window.show(); self.app.processEvents()
        item = self.window.devices.item(1)
        with patch.object(VideoTile, 'connect_device', self.connect_stub(calls)):
            QTest.mouseClick(self.window.devices.viewport(), Qt.MouseButton.LeftButton,
                             pos=self.window.devices.visualItemRect(item).center())
        self.assertEqual([(tile, identity, channel) for tile, identity, channel, _ in calls],
                         [(i, 'b', i) for i in range(1, 5)])
        self.assertEqual(self.window.mosaic.currentData(), 4)
        self.assertEqual(len(self.window.channels.selectedItems()), 4)

    def test_repeated_client_click_does_not_restart_existing_audio_or_video(self):
        calls = []
        self.window.cloud_client = SimpleNamespace(device_password=lambda identity: 'SYNTHETIC-PASSWORD', logout=lambda: None)
        with patch.object(VideoTile, 'connect_device', self.connect_stub(calls)):
            self.window.open_client_cameras(self.window.devices.item(1))
            marker = object(); self.window.tiles[2].last_image = marker
            self.window.open_client_cameras(self.window.devices.item(1))
        self.assertEqual(len(calls), 4)
        self.assertIs(self.window.tiles[2].last_image, marker)

    def test_failed_authentication_preserves_existing_mosaic(self):
        self.window.mosaic.setCurrentIndex(self.window.mosaic.findData(9))
        tile = self.window.tiles[0]
        tile.device_id = 'existing-client'; marker = object(); tile.last_image = marker
        with patch.object(self.window, 'open_cloud_account'), \
             patch.object(VideoTile, 'connect_device') as connect:
            self.window.open_client_cameras(self.window.devices.item(1))
        connect.assert_not_called()
        self.assertEqual(self.window.mosaic.currentData(), 9)
        self.assertIs(tile.last_image, marker)

    def test_search_filters_clients_without_connecting(self):
        with patch.object(VideoTile, 'connect_device') as connect:
            self.window.client_search.setText('CENTRAL')
        connect.assert_not_called()
        self.assertTrue(self.window.devices.item(0).isHidden())
        self.assertFalse(self.window.devices.item(1).isHidden())
        self.assertEqual(self.window.client_count.text(), '1/2')

    def test_sidebar_remains_usable_at_1280_by_800(self):
        self.window.resize(1280, 800); self.window.show(); self.app.processEvents()
        self.assertEqual(self.window.height(), 800)
        self.assertGreaterEqual(self.window.devices.height(), 160)
        self.assertGreaterEqual(self.window.devices.sizeHintForRow(0), 68)
        self.assertTrue(self.window.options_scroll.isHidden())
        self.window.options_toggle.click(); self.app.processEvents()
        self.assertFalse(self.window.options_scroll.isHidden())
        self.assertLessEqual(self.window.height(), 800)

    def test_switching_client_replaces_the_first_four_tiles(self):
        calls = []
        self.window.cloud_client = SimpleNamespace(device_password=lambda identity: 'SYNTHETIC-PASSWORD', logout=lambda: None)
        with patch.object(VideoTile, 'connect_device', self.connect_stub(calls)):
            self.window.open_client_cameras(self.window.devices.item(0))
            self.window.open_client_cameras(self.window.devices.item(1))
        self.assertEqual(len(calls), 8)
        self.assertEqual([t.device_id for t in self.window.tiles[:4]], ['b'] * 4)

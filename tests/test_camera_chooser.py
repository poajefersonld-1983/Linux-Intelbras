import json
from unittest.mock import patch

import unittest
import test_client_overview
from simnext_linux.gui import CAMERA_STATE_ROLE, CAMERA_TILES_ROLE, CAMERA_MIME
from PyQt6.QtCore import Qt


class CameraChooserTests(unittest.TestCase):
    setUpClass = classmethod(test_client_overview.ClientOverviewTests.setUpClass.__func__)
    setUp = test_client_overview.ClientOverviewTests.setUp
    tearDown = test_client_overview.ClientOverviewTests.tearDown

    def test_numeric_filter_matches_exact_channel_and_restores_cards(self):
        self.window.camera_search.setText('3')
        visible = [self.window.channels.item(i) for i in range(self.window.channels.count())
                   if not self.window.channels.item(i).isHidden()]
        self.assertEqual([i.data(Qt.ItemDataRole.UserRole) for i in visible], [3])
        self.window.camera_search.clear()
        self.assertTrue(all(not self.window.channels.item(i).isHidden()
                            for i in range(self.window.channels.count())))

    def test_activity_distinguishes_received_video_and_pending_connection(self):
        for tile in self.window.tiles[:2]:
            tile.device_id, tile.channel_number = 'a', 1
        self.window.tiles[1].last_image = object()
        self.window.tiles[2].device_id, self.window.tiles[2].channel_number = 'a', 2
        self.window.update_camera_activity()
        self.assertEqual(self.window.channels.item(0).data(CAMERA_STATE_ROLE), 'live')
        self.assertEqual(self.window.channels.item(0).data(CAMERA_TILES_ROLE), [1, 2])
        self.assertEqual(self.window.channels.item(1).data(CAMERA_STATE_ROLE), 'connecting')
        self.assertEqual(self.window.channels.item(2).data(CAMERA_STATE_ROLE), 'idle')

    def test_clicking_open_camera_focuses_without_reconnection(self):
        tile = self.window.tiles[2]
        tile.device_id, tile.channel_number = 'a', 3
        marker = object(); tile.last_image = marker
        with patch.object(self.window, 'open_cameras') as open_cameras:
            self.window.open_clicked_camera(self.window.channels.item(2))
        open_cameras.assert_not_called()
        self.assertEqual(self.window.target.currentData(), 2)
        self.assertIs(tile.last_image, marker)

    def test_hidden_selection_is_not_dragged_or_opened(self):
        self.window.channels.clearSelection()
        self.window.channels.item(0).setSelected(True)
        self.window.channels.item(2).setSelected(True)
        self.window.camera_search.setText('3')
        payload = json.loads(bytes(self.window.channels.camera_mime().data(CAMERA_MIME)))
        self.assertEqual(payload, [{'device': 'a', 'channel': 3}])
        with patch.object(self.window, 'open_cameras') as opened:
            self.window.connect_selected()
        assignments = opened.call_args.args[0]
        self.assertEqual([device.channel for device, tile in assignments], [3])

    def test_visible_target_and_two_columns_fit_small_window(self):
        self.window.resize(1280, 800); self.window.show(); self.app.processEvents()
        self.assertTrue(self.window.target.isVisible())
        self.assertTrue(self.window.open_selected_button.isVisible())
        a = self.window.channels.visualItemRect(self.window.channels.item(0))
        b = self.window.channels.visualItemRect(self.window.channels.item(1))
        c = self.window.channels.visualItemRect(self.window.channels.item(2))
        self.assertEqual(a.top(), b.top())
        self.assertGreater(c.top(), a.top())
        self.assertGreaterEqual(a.height(), 78)

    def test_selection_button_disables_already_open_camera(self):
        self.window.channels.clearSelection()
        self.window.channels.item(0).setSelected(True)
        self.assertTrue(self.window.open_selected_button.isEnabled())
        tile = self.window.tiles[0]; tile.device_id, tile.channel_number = 'a', 1
        self.window.update_camera_activity()
        self.assertFalse(self.window.open_selected_button.isEnabled())
        with patch.object(self.window, 'open_cameras') as opened:
            self.window.connect_selected()
        opened.assert_not_called()

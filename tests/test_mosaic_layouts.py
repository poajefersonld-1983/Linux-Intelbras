import unittest
import test_client_overview
from simnext_linux.gui import MOSAICS
from PyQt6.QtGui import QImage
from PyQt6.QtMultimedia import QVideoFrame


class MosaicLayoutTests(unittest.TestCase):
    setUpClass = classmethod(test_client_overview.ClientOverviewTests.setUpClass.__func__)
    setUp = test_client_overview.ClientOverviewTests.setUp
    tearDown = test_client_overview.ClientOverviewTests.tearDown

    def test_all_models_fit_window_without_overlaps(self):
        self.window.resize(1280, 800); self.window.show(); self.app.processEvents()
        for count in MOSAICS:
            with self.subTest(count=count):
                self.window.mosaic.setCurrentIndex(self.window.mosaic.findData(count)); self.app.processEvents()
                self.assertEqual(self.window.grid.count(), count)
                self.assertEqual(self.window.target.count(), count)
                self.assertEqual((self.window.width(), self.window.height()), (1280, 800))
                tiles = self.window.tiles[:count]
                for i, tile in enumerate(tiles):
                    self.assertTrue(tile.isVisible())
                    self.assertGreaterEqual(tile.display.width(), 120)
                    self.assertGreaterEqual(tile.display.height(), 64)
                    for other in tiles[i+1:]:
                        self.assertFalse(tile.geometry().intersects(other.geometry()))

    def test_layout_change_preserves_live_tiles_and_destination(self):
        tile = self.window.tiles[2]; tile.device_id, tile.channel_number = 'a', 3
        marker = QImage(64, 36, QImage.Format.Format_RGB32); marker.fill(0xff123456)
        tile.last_image = marker
        generation = tile._generation
        self.window.select_tile(2)
        for count in (6, 8, 16, 9, 4):
            self.window.mosaic.setCurrentIndex(self.window.mosaic.findData(count))
            self.assertEqual(tile.device_id, 'a')
            self.assertIs(tile.last_image, marker)
            self.assertEqual(tile._generation, generation)
            self.assertEqual(self.window.target.currentData(), 2)

    def test_compact_controls_restore_after_fullscreen_and_four_tiles(self):
        self.window.show(); self.app.processEvents()
        self.window.mosaic.setCurrentIndex(self.window.mosaic.findData(16))
        tile = self.window.tiles[0]
        self.assertTrue(tile.tile_options.isVisible())
        self.assertTrue(tile.status.isHidden())
        self.window.toggle_mosaic_fullscreen(); self.app.processEvents()
        self.assertTrue(self.window.mosaic_toolbar.isHidden())
        self.assertTrue(all(widget.isHidden() for widget in tile._chrome_widgets))
        self.window.exit_fullscreen(); self.app.processEvents()
        self.assertTrue(tile.tile_options.isVisible())
        self.assertTrue(tile.status.isHidden())
        self.window.mosaic.setCurrentIndex(self.window.mosaic.findData(4)); self.app.processEvents()
        self.assertFalse(tile.status.isHidden())
        self.assertTrue(tile.tile_options.isHidden())
        self.assertTrue(all(not widget.isHidden() for widget in tile._regular_controls))

    def test_six_and_three_models_have_larger_first_tile(self):
        self.window.show()
        for count in (3, 6):
            self.window.mosaic.setCurrentIndex(self.window.mosaic.findData(count)); self.app.processEvents()
            first, second = self.window.tiles[:2]
            self.assertGreater(first.width(), second.width() * 1.7)
            self.assertGreater(first.height(), second.height() * 1.7)

    def test_sixteen_cameras_can_be_assigned(self):
        from dataclasses import replace
        device = self.store.list()[0]
        assignments = self.window.mosaic_assignments([replace(device, channel=i) for i in range(1, 17)])
        self.assertEqual(len(assignments), 16)
        self.assertEqual(self.window.mosaic.currentData(), 16)

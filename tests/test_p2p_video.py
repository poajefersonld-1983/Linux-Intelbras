import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEventLoop, QTimer
from PyQt6.QtWidgets import QApplication
from simnext_linux.cloud_video import CloudVideoWorker
from simnext_linux.p2p import P2PBridge, P2PError, load_settings


class FakeTransport:
    def __init__(self, config):
        self.closed = False
    def connect(self, timeout):
        if self.closed:
            raise OSError("closed")
    def close(self):
        self.closed = True


class FakeProxy:
    port = 12345
    def __init__(self, transport):
        self.closed = False
    def close(self):
        self.closed = True


class P2PTests(unittest.TestCase):
    def test_configuration_failure_does_not_expose_values(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "p2p.json"
            path.write_text(json.dumps({"application_key": "PRIVATE-KEY", "p2p_password": "bad\nPRIVATE-PASSWORD"}))
            with self.assertRaises(P2PError) as error:
                load_settings(path)
            self.assertNotIn("PRIVATE", str(error.exception))

    def test_bridge_lifecycle_and_cancellation(self):
        with patch("simnext_linux.p2p.Transport", FakeTransport), patch("simnext_linux.p2p.LoopbackProxy", FakeProxy):
            bridge = P2PBridge("SYNTHETIC", 554, settings={"application_key": "fixture", "p2p_password": "fixture"})
            self.assertEqual(bridge.start(), 12345)
            proxy = bridge.proxy
            bridge.close()
            self.assertTrue(proxy.closed)
            self.assertTrue(bridge.transport.closed)
            with self.assertRaises(P2PError):
                bridge.start()


class DecoderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_authentication_diagnostic_is_classified_without_exposing_url(self):
        worker = CloudVideoWorker("rtsp://operator:PRIVATE-PASSWORD@127.0.0.1/live")
        worker.read_diagnostics(io.BytesIO(b"rtsp://operator:PRIVATE-PASSWORD@127.0.0.1/live: method DESCRIBE failed: 401 Unauthorized\n"))
        self.assertTrue(worker.authentication_refused.is_set())
        self.assertFalse(hasattr(worker, "raw_diagnostics"))

    def test_hd_decoder_outputs_hd_frames(self):
        class Process:
            def __init__(self, command, **options):
                self.stdout = io.BytesIO(bytes([32, 64, 128, 255]) * (1280 * 720))
                self.command = command
            def poll(self): return 0
            def wait(self, timeout): return 0
        worker = CloudVideoWorker("rtsp://127.0.0.1:12345/live", size=(1280, 720))
        images = []; worker.frame.connect(images.append)
        with patch("simnext_linux.cloud_video.subprocess.Popen", Process):
            loop = QEventLoop(); worker.finished.connect(loop.quit)
            QTimer.singleShot(5000, loop.quit); worker.start(); loop.exec(); worker.wait(1000)
            self.app.processEvents()
        self.assertEqual(worker.frames, 1)
        self.assertEqual((images[0].width(), images[0].height()), (1280, 720))

    def test_decoder_frames_and_credentials_not_in_argv(self):
        captured = {}
        class Process:
            def __init__(self, command, **options):
                captured["command"] = command
                captured["source"] = os.read(options["pass_fds"][0], 4096)
                self.stdout = io.BytesIO(bytes([32, 64, 128, 255]) * (640 * 360))
            def poll(self): return 0
            def wait(self, timeout): return 0
        worker = CloudVideoWorker("rtsp://operator:PRIVATE-PASSWORD@127.0.0.1:12345/live")
        images = []
        worker.frame.connect(images.append)
        with patch("simnext_linux.cloud_video.subprocess.Popen", Process):
            loop = QEventLoop()
            worker.finished.connect(loop.quit)
            QTimer.singleShot(5000, loop.quit)
            worker.start()
            loop.exec()
            worker.wait(1000)
            self.app.processEvents()
        self.assertEqual(worker.frames, 1)
        self.assertEqual(images[0].size().width(), 640)
        self.assertNotIn("PRIVATE-PASSWORD", " ".join(captured["command"]))
        self.assertIn(b"PRIVATE-PASSWORD", captured["source"])
        self.assertEqual(worker.source, "")
        self.assertIn(b"rtsp_transport tcp", captured["source"])


class PoolTests(unittest.TestCase):
    def test_shared_transport_is_closed_only_after_last_tile(self):
        from simnext_linux.p2p import BridgePool
        with patch("simnext_linux.p2p.P2PBridge") as factory:
            factory.return_value.start.return_value = 1234
            pool = BridgePool()
            first, second = pool.acquire("example", 554), pool.acquire("example", 554)
            self.assertEqual(first.start(), second.start())
            self.assertEqual(factory.call_count, 1)
            first.close(); first.close()
            factory.return_value.close.assert_not_called()
            second.close()
            factory.return_value.close.assert_called_once()
            self.assertEqual(pool.entries, [])

    def test_more_than_four_channels_use_another_transport(self):
        from simnext_linux.p2p import BridgePool
        with patch("simnext_linux.p2p.P2PBridge") as factory:
            pool = BridgePool()
            leases = [pool.acquire("example", 554) for _ in range(9)]
            self.assertEqual(factory.call_count, 3)
            for lease in leases: lease.close()
            self.assertEqual(pool.entries, [])

import io
import unittest
from unittest.mock import patch
from PyQt6.QtWidgets import QApplication
from simnext_linux.cloud_video import CloudVideoWorker


class VideoPerformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_busy_interface_keeps_only_latest_frame(self):
        class Process:
            def __init__(self, *args, **kwargs):
                self.stdout = io.BytesIO(b"".join(bytes([value, 0, 0, 255]) * (640 * 360)
                                                for value in (10, 20, 30)))
            def poll(self): return 0
            def wait(self, timeout): return 0
        worker = CloudVideoWorker("rtsp://127.0.0.1/live")
        notifications = []
        worker.frame.connect(notifications.append)
        with patch("simnext_linux.cloud_video.subprocess.Popen", Process):
            worker.run()
        self.assertEqual(worker.frames, 3)
        self.assertEqual(len(notifications), 1)
        self.assertEqual(worker.take_frame().pixelColor(0, 0).red(), 30)
        self.assertFalse(worker._frame_pending)

    def test_failed_hardware_decoder_retries_software_privately(self):
        commands = []
        sources = []
        class Process:
            def __init__(self, command, **kwargs):
                import os
                commands.append(command)
                sources.append(os.read(kwargs["pass_fds"][0], 4096))
                self.stdout = io.BytesIO(b"" if len(commands) == 1 else
                                        bytes([12, 0, 0, 255]) * (640 * 360))
            def poll(self): return 0
            def wait(self, timeout): return 0
        worker = CloudVideoWorker("rtsp://operator:PRIVATE@127.0.0.1/live")
        with patch("simnext_linux.cloud_video.os.access", return_value=True), \
             patch("simnext_linux.cloud_video.subprocess.Popen", Process):
            worker.run()
        self.assertEqual(len(commands), 2)
        self.assertIn("vaapi", commands[0])
        self.assertNotIn("-hwaccel", commands[1])
        self.assertEqual(sources[0], sources[1])
        self.assertNotIn("PRIVATE", str(commands))
        self.assertEqual(worker.frames, 1)
        self.assertEqual(worker.source, "")

    def test_gpu_scaling_and_output_thread_limit(self):
        commands = []
        class Process:
            def __init__(self, command, **kwargs):
                commands.append(command)
                self.stdout = io.BytesIO(bytes([12, 0, 0, 255]) * (640 * 360))
            def poll(self): return 0
            def wait(self, timeout): return 0
        worker = CloudVideoWorker("rtsp://127.0.0.1/live")
        with patch.dict("os.environ", {"SIMNEXT_GPU_SCALING": "1"}), \
             patch("simnext_linux.cloud_video.os.access", return_value=True), \
             patch("simnext_linux.cloud_video.subprocess.Popen", Process):
            worker.run()
        command = commands[0]
        self.assertIn("-hwaccel_output_format", command)
        self.assertIn("scale_vaapi", command[command.index("-vf") + 1])
        self.assertEqual(command[-5:], ["-threads", "1", "-f", "rawvideo", "pipe:1"])

    def test_frame_owns_pixels_after_worker_is_released(self):
        import gc
        class Process:
            def __init__(self, *args, **kwargs):
                self.stdout = io.BytesIO(bytes([42, 17, 8, 255]) * (640 * 360))
            def poll(self): return 0
            def wait(self, timeout): return 0
        worker = CloudVideoWorker("rtsp://127.0.0.1/live")
        with patch("simnext_linux.cloud_video.subprocess.Popen", Process):
            worker.run()
        image = worker.take_frame()
        del worker
        gc.collect()
        self.assertEqual(image.pixelColor(0, 0).getRgb(), (42, 17, 8, 255))

    def test_native_nv12_frame_has_valid_planes_and_no_forced_fps(self):
        from PyQt6.QtMultimedia import QVideoFrame
        commands = []
        class Process:
            def __init__(self, command, **kwargs):
                commands.append(command)
                self.stdout = io.BytesIO(bytes([80]) * (640 * 360) + bytes([128]) * (640 * 180))
            def poll(self): return 0
            def wait(self, timeout): return 0
        worker = CloudVideoWorker("rtsp://127.0.0.1/live", native_video=True)
        with patch("simnext_linux.cloud_video.subprocess.Popen", Process):
            worker.run()
        frame = worker.take_frame()
        self.assertIsInstance(frame, QVideoFrame)
        self.assertTrue(frame.isValid())
        image = frame.toImage()
        self.assertFalse(image.isNull())
        self.assertLess(abs(image.pixelColor(100, 100).red() - image.pixelColor(100, 100).blue()), 3)
        command = commands[0]
        self.assertEqual(command[command.index("-pix_fmt") + 1], "nv12")
        self.assertEqual(command[command.index("-fps_mode") + 1], "passthrough")
        self.assertNotIn("fps=", command[command.index("-vf") + 1])

    def test_rtsp_low_latency_options_do_not_expose_credentials(self):
        import os
        commands, sources = [], []
        class Process:
            def __init__(self, command, **kwargs):
                commands.append(command)
                sources.append(os.read(kwargs["pass_fds"][0], 4096))
                self.stdout = io.BytesIO(bytes([12, 0, 0, 255]) * (640 * 360))
            def poll(self): return 0
            def wait(self, timeout): return 0
        worker = CloudVideoWorker("rtsp://operator:PRIVATE@127.0.0.1/live")
        with patch("simnext_linux.cloud_video.subprocess.Popen", Process):
            worker.run()
        self.assertIn(b"option fflags nobuffer", sources[0])
        self.assertIn(b"option analyzeduration 500000", sources[0])
        self.assertIn(b"option max_delay 100000", sources[0])
        self.assertIn("+low_delay", commands[0])
        self.assertNotIn("PRIVATE", str(commands))

    def test_paced_video_keeps_frames_from_network_bursts_in_order(self):
        class Process:
            def __init__(self, *args, **kwargs):
                self.stdout = io.BytesIO(b"".join(bytes([y]) * (640 * 360) + bytes([128]) * (640 * 180) for y in (40, 80, 120)))
            def poll(self): return 0
            def wait(self, timeout): return 0
        worker = CloudVideoWorker("rtsp://127.0.0.1/live", native_video=True, paced_video=True)
        notifications = []
        worker.frame.connect(notifications.append)
        with patch("simnext_linux.cloud_video.subprocess.Popen", Process):
            worker.run()
        self.assertEqual(worker.frames, 3)
        self.assertEqual(len(notifications), 1)
        frames = worker.take_frames()
        self.assertEqual(len(frames), 3)
        reds = [f.toImage().pixelColor(0, 0).red() for f in frames]
        self.assertLess(reds[0], reds[1])
        self.assertLess(reds[1], reds[2])
        self.assertEqual(worker.take_frames(), [])

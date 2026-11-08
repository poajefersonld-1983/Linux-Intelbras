import io
import shutil
import subprocess
import unittest

from PyQt6.QtWidgets import QApplication
from simnext_linux.cloud_video import CloudVideoWorker
from simnext_linux.playback import FramePacer


class CameraGeometryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg required')
    def test_decoded_picture_geometry(self):
        # Border pixels reveal unwanted padding or a stretched portrait.
        cases = [('960x1080', '1', True), ('960x1080', '2', True),
                 ('1920x1080', '1', True), ('1280x720', '1', True),
                 ('1080x1920', '1', False), ('720x576', '64/45', True)]
        for dimensions, sar, fills_width in cases:
            with self.subTest(dimensions=dimensions, sar=sar):
                worker = CloudVideoWorker('unused', size=(1280, 720))
                result = subprocess.run([
                    'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                    f'color=c=red:s={dimensions}:r=15,setsar={sar}',
                    '-frames:v', '1', '-filter_threads', '1',
                    '-vf', worker.video_filter(), '-pix_fmt', 'rgb24',
                    '-threads', '1', '-f', 'rawvideo', 'pipe:1'],
                    capture_output=True, check=True, timeout=15)
                self.assertEqual(len(result.stdout), 1280 * 720 * 3)
                for x in (10, 1269):
                    pixel = result.stdout[(360 * 1280 + x) * 3:][:3]
                    self.assertEqual(pixel[0] > 150, fills_width)
                center = result.stdout[(360 * 1280 + 640) * 3:][:3]
                self.assertGreater(center[0], 150)

    def test_camera_fps_is_not_replaced_by_raw_output_clock(self):
        worker = CloudVideoWorker('unused', paced_video=True)
        worker.read_diagnostics(io.BytesIO(
            b'Input #0, concat, from PRIVATE:\n'
            b' Stream #0:0: Video: hevc, 960x1080, 15 fps, 33 tbr\n'
            b'Output #0, rawvideo, to pipe:1:\n'
            b' Stream #0:0: Video: rawvideo, 1280x720, 33 fps\n'))
        self.assertEqual(worker.frame_rate, 15)
        self.assertTrue(worker.input_frame_rate_detected)
        # The real stream also advertised 14.75 fps on its raw output.
        worker.read_diagnostics(io.BytesIO(
            b'Output #0, rawvideo, to pipe:1:\n'
            b' Stream #0:0: Video: rawvideo, 1280x720, 14.75 fps\n'))
        self.assertEqual(worker.frame_rate, 15)

    def test_matching_15_fps_camera_does_not_build_delay(self):
        pacer = FramePacer(fps=15)
        index, displayed = 0, []
        for tick in range(7500):
            now = tick * 0.008
            frames = []
            while index / 15 <= now:
                frames.append(index)
                index += 1
            if frames:
                pacer.push(frames, now)
            frame = pacer.poll(now)
            if frame is not None:
                displayed.append((now, frame))
        self.assertGreater(len(displayed), 880)
        self.assertEqual(pacer.underruns, 0)
        self.assertLessEqual(len(pacer.frames), pacer.capacity)
        self.assertLess(max(b[0] - a[0] for a, b in zip(displayed, displayed[1:])), 0.08)

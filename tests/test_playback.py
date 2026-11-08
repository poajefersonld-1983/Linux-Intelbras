import unittest
from simnext_linux.playback import FramePacer


class FramePacerTests(unittest.TestCase):
    def test_periodic_network_bursts_are_presented_at_a_steady_rate(self):
        pacer = FramePacer(fps=30, delay=0.5)
        arrivals = [(n // 60 * 2 + max((n % 60) / 30, 0.35), n) for n in range(600)]
        index = 0
        displayed = []
        for tick in range(2500):
            now = tick * 0.008
            ready = []
            while index < len(arrivals) and arrivals[index][0] <= now:
                ready.append(arrivals[index][1]); index += 1
            if ready:
                pacer.push(ready, now)
            frame = pacer.poll(now)
            if frame is not None:
                displayed.append((now, frame))
        self.assertGreater(len(displayed), 560)
        intervals = [b[0] - a[0] for a, b in zip(displayed, displayed[1:])]
        self.assertLess(max(intervals), 0.05)
        self.assertGreater(min(intervals), 0.02)
        self.assertEqual(pacer.underruns, 0)
        self.assertLessEqual(len(pacer.frames), pacer.capacity)

    def test_queue_cannot_accumulate_unbounded_delay(self):
        pacer = FramePacer(fps=30)
        pacer.push(list(range(500)), 0)
        self.assertLessEqual(len(pacer.frames), 30)
        self.assertGreaterEqual(pacer.poll(0.5), 470)

    def test_late_ui_discards_old_frames_instead_of_sending_a_burst(self):
        pacer = FramePacer(fps=30)
        pacer.push(list(range(20)), 0)
        self.assertEqual(pacer.poll(0.5), 0)
        self.assertGreaterEqual(pacer.poll(0.9), 11)
        self.assertIsNone(pacer.poll(0.9001))

    def test_other_camera_rates_and_disconnect_reset(self):
        pacer = FramePacer(fps=15)
        pacer.push([1, 2, 3], 0)
        self.assertEqual(pacer.poll(0.5), 1)
        self.assertIsNone(pacer.poll(0.53))
        self.assertEqual(pacer.poll(0.57), 2)
        pacer.clear()
        self.assertIsNone(pacer.poll(1))
        pacer.push([4], 1)
        self.assertIsNone(pacer.poll(1.4))
        self.assertEqual(pacer.poll(1.5), 4)

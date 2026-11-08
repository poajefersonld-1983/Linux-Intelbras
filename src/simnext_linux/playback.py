"""Bounded presentation clock for bursty camera frames."""
from collections import deque
import math


class FramePacer:
    def __init__(self, fps=30.0, delay=0.5):
        self.delay = delay
        self.frames = deque()
        self.ready_at = None
        self.deadline = None
        self.underruns = 0
        self.set_rate(fps)

    def set_rate(self, fps):
        if 1 <= fps <= 120:
            self.fps = float(fps)
            self.interval = 1.0 / self.fps
            self.capacity = max(4, math.ceil(self.fps * (self.delay + 0.2)))

    def clear(self):
        self.frames.clear()
        self.ready_at = self.deadline = None

    def push(self, frames, now):
        if self.ready_at is None:
            self.ready_at = now + self.delay
        self.frames.extend(frames)
        while len(self.frames) > self.capacity:
            self.frames.popleft()

    def poll(self, now):
        if not self.frames:
            if self.deadline is not None and now > self.deadline + self.interval:
                self.underruns += 1
                self.ready_at = self.deadline = None
            return None
        if self.deadline is None:
            if now < self.ready_at:
                return None
            self.deadline = now
        if now + 1e-6 < self.deadline:
            return None
        # A stalled UI catches up by discarding old frames, never by a burst.
        overdue = max(0, int((now - self.deadline) / self.interval))
        for _ in range(min(overdue, len(self.frames) - 1)):
            self.frames.popleft()
        self.deadline = max(self.deadline + (overdue + 1) * self.interval, now)
        return self.frames.popleft()

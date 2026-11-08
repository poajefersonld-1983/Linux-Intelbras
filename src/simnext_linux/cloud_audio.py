"""Bounded PCM audio receiver; RTSP credentials stay in an anonymous descriptor."""
from __future__ import annotations

import math
import os
import shutil
import subprocess
import threading
from array import array

from PyQt6.QtCore import QThread, pyqtSignal


SAMPLE_RATE = 16000
CHANNELS = 1
BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * 2
MAX_BUFFER = BYTES_PER_SECOND // 2


class CloudAudioWorker(QThread):
    pcm_ready = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, source, parent=None):
        super().__init__(parent)
        self.source = source
        self.cancelled = threading.Event()
        self._lock = threading.Lock()
        self._process = None
        self._pcm = bytearray()
        self._pending = False
        self.received_bytes = 0
        self.level_dbfs = -120.0

    def take_pcm(self):
        with self._lock:
            data = bytes(self._pcm)
            self._pcm.clear()
            self._pending = False
        return data

    def publish_pcm(self, data):
        samples = array('h')
        samples.frombytes(data)
        if samples:
            rms = math.sqrt(sum(value * value for value in samples) / len(samples))
            self.level_dbfs = 20 * math.log10(rms / 32768) if rms else -120.0
        with self._lock:
            self.received_bytes += len(data)
            self._pcm.extend(data)
            if len(self._pcm) > MAX_BUFFER:
                del self._pcm[:-MAX_BUFFER]
            notify = not self._pending
            self._pending = True
        if notify:
            self.pcm_ready.emit()

    def run(self):
        descriptor = None
        process = None
        try:
            executable = shutil.which('ffmpeg')
            if executable is None:
                raise RuntimeError('Audio decoder unavailable')
            source, self.source = self.source, ''
            descriptor = os.memfd_create('monitor-audio-source', 0)
            escaped = source.replace("'", "'\\''")
            playlist = "ffconcat version 1.0\nfile '" + escaped + "'\n"
            if source.lower().startswith(('rtsp://', 'rtsps://')):
                playlist += ('option rtsp_transport tcp\noption timeout 15000000\n'
                             'option allowed_media_types audio\n'
                             'option analyzeduration 500000\noption probesize 262144\n')
            source = ''
            os.write(descriptor, playlist.encode())
            os.lseek(descriptor, 0, os.SEEK_SET)
            playlist = ''
            command = [executable, '-nostdin', '-hide_banner', '-loglevel', 'error',
                       '-threads', '1', '-f', 'concat', '-safe', '0',
                       '-protocol_whitelist', 'file,rtsp,rtsps,rtp,tcp,udp,crypto,tls',
                       '-i', f'/proc/self/fd/{descriptor}', '-map', '0:a:0', '-vn',
                       '-ac', str(CHANNELS), '-ar', str(SAMPLE_RATE),
                       '-c:a', 'pcm_s16le', '-flush_packets', '1', '-f', 's16le', 'pipe:1']
            with self._lock:
                if self.cancelled.is_set():
                    return
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                           pass_fds=(descriptor,))
                self._process = process
            os.close(descriptor)
            descriptor = None
            while not self.cancelled.is_set():
                data = process.stdout.read(BYTES_PER_SECOND // 20)
                if not data or self.cancelled.is_set():
                    break
                # s16le samples always occupy two bytes.
                if len(data) % 2:
                    data = data[:-1]
                if data:
                    self.publish_pcm(data)
            if not self.cancelled.is_set():
                self.failed.emit('Áudio encerrado.' if self.received_bytes else
                                 'Este canal não forneceu áudio. Verifique o microfone e o áudio do DVR.')
        except Exception:
            if not self.cancelled.is_set():
                self.failed.emit('Não foi possível iniciar o áudio deste canal.')
        finally:
            self.source = ''
            if descriptor is not None:
                os.close(descriptor)
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
                process.stdout.close()
            with self._lock:
                self._process = None

    def cancel(self):
        self.cancelled.set()
        self.source = ''
        with self._lock:
            self._pcm.clear()
            if self._process is not None and self._process.poll() is None:
                self._process.terminate()

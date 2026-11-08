"""Decode the RTSP TCP stream without exposing its credentials in argv/logs."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from collections import deque

from PyQt6.QtCore import QThread, pyqtSignal, QSize
from PyQt6.QtGui import QImage
from PyQt6.QtMultimedia import QVideoFrame, QVideoFrameFormat


class CloudVideoWorker(QThread):
    frame = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, source: str, parent=None, *, size=(640, 360), native_video=False, paced_video=False):
        super().__init__(parent)
        if size not in ((640, 360), (1280, 720)):
            raise ValueError("Unsupported video size")
        self.width, self.height = size
        self.native_video = native_video
        self.paced_video = paced_video
        self.frame_rate = 30.0
        self.input_frame_rate_detected = False
        self._ready_frames = deque(maxlen=120)
        self.source = source
        self.cancelled = threading.Event()
        self._lock = threading.Lock()
        self._process = None
        self._frame_lock = threading.Lock()
        self._latest_frame = None
        self._frame_pending = False
        self.frames = 0
        self.exit_code = None
        self.authentication_refused = threading.Event()

    def take_frame(self, fallback=None):
        # One queued notification per tile; discard obsolete pictures, not latency.
        with self._frame_lock:
            image = self._latest_frame
            self._latest_frame = None
            self._frame_pending = False
        return image if image is not None else fallback

    def take_frames(self):
        with self._frame_lock:
            frames = list(self._ready_frames)
            self._ready_frames.clear()
            self._latest_frame = None
            self._frame_pending = False
        return frames

    def run(self):
        source = self.source
        accelerated = os.access("/dev/dri/renderD128", os.R_OK | os.W_OK)
        try:
            self._decode(source, accelerated=accelerated, retry=accelerated)
            if (accelerated and not self.frames and not self.cancelled.is_set()
                    and not self.authentication_refused.is_set()):
                self._decode(source, accelerated=False, retry=False)
        finally:
            self.source = ""

    def video_filter(self, gpu_scaling=False):
        # Intelbras 1080N stores half the horizontal samples (960x1080).
        # Some DVRs omit SAR=2:1; restore it before fitting the display.
        # Raw NV12/RGBA carries no SAR, so produce square pixels explicitly.
        aspect = "setsar=ratio='if(eq(w,960)*eq(h,1080)*lte(sar,1),2,if(gt(sar,0),sar,1))'"
        width = f"'trunc(min({self.width},{self.height}*dar)/2)*2'"
        height = f"'trunc(min({self.height},{self.width}/dar)/2)*2'"
        if gpu_scaling:
            scale = (f"scale_vaapi=w={width}:h={height}:format=nv12:mode=fast,"
                     "hwdownload,format=nv12")
        else:
            scale = f"scale=w={width}:h={height}:flags=fast_bilinear"
        return (f"{aspect},{scale},setsar=1,"
                f"pad={self.width}:{self.height}:(ow-iw)/2:(oh-ih)/2")

    def _decode(self, source, *, accelerated, retry):
        descriptor = None
        process = None
        diagnostic_thread = None
        try:
            executable = shutil.which("ffmpeg")
            if executable is None:
                raise RuntimeError("decoder unavailable")
            descriptor = os.memfd_create("monitor-video-source", 0)
            rtsp = source.lower().startswith(("rtsp://", "rtsps://"))
            escaped = source.replace("'", "'\\''")
            playlist = "ffconcat version 1.0\nfile '" + escaped + "'\n"
            if rtsp:
                playlist += (
                    "option rtsp_transport tcp\noption timeout 15000000\n"
                    "option fflags nobuffer\noption analyzeduration 500000\n"
                    "option probesize 262144\noption max_delay 100000\n")
            os.write(descriptor, playlist.encode("utf-8"))
            os.lseek(descriptor, 0, os.SEEK_SET)
            self.source = ""
            command = [executable, "-nostdin", "-hide_banner", "-nostats", "-loglevel", "info" if self.paced_video else "error"]
            command += ["-threads", "1", "-filter_threads", "1", "-flags", "+low_delay"]
            if accelerated:
                command += ["-hwaccel", "vaapi", "-hwaccel_device", "/dev/dri/renderD128"]
                if os.environ.get("SIMNEXT_GPU_SCALING") == "1":
                    command += ["-hwaccel_output_format", "vaapi"]
            video_filter = self.video_filter(
                gpu_scaling=accelerated and os.environ.get("SIMNEXT_GPU_SCALING") == "1")
            command += ["-f", "concat", "-safe", "0", "-protocol_whitelist", "file,rtsp,rtsps,rtp,tcp,udp,crypto,tls",
                        "-i", f"/proc/self/fd/{descriptor}", "-map", "0:v:0", "-an",
                        "-vf", video_filter,
                        "-pix_fmt", "nv12" if self.native_video else "rgba", "-fps_mode", "passthrough",
                        "-flush_packets", "1", "-threads", "1", "-f", "rawvideo", "pipe:1"]
            with self._lock:
                if self.cancelled.is_set():
                    return
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, pass_fds=(descriptor,))
                self._process = process
            if getattr(process, "stderr", None) is not None:
                diagnostic_thread = threading.Thread(target=self.read_diagnostics, args=(process.stderr,), daemon=True)
                diagnostic_thread.start()
            os.close(descriptor)
            descriptor = None
            length = self.width * self.height * (3 if self.native_video else 8) // 2
            while not self.cancelled.is_set():
                # BufferedReader fills the request; avoid repeated bytearray copies.
                data = process.stdout.read(length)
                if len(data) != length or self.cancelled.is_set():
                    break
                if self.native_video:
                    frame = self.native_frame(data)
                else:
                    frame = QImage(data, self.width, self.height, self.width * 4, QImage.Format.Format_RGBA8888)
                self.frames += 1
                with self._frame_lock:
                    self._latest_frame = frame
                    if self.paced_video:
                        self._ready_frames.append(frame)
                    notify = not self._frame_pending
                    self._frame_pending = True
                if notify:
                    self.frame.emit(frame)
            if diagnostic_thread is not None:
                diagnostic_thread.join(timeout=1)
            if not self.cancelled.is_set() and (not retry or self.frames or self.authentication_refused.is_set()):
                if self.authentication_refused.is_set():
                    self.failed.emit("O DVR recusou a autenticação. Use Configurar cliente → Senha do equipamento nesta sessão.")
                else:
                    self.failed.emit("Transmissão encerrada." if self.frames else
                                     "O DVR não iniciou o vídeo. A conexão será tentada novamente.")
        except Exception:
            if not self.cancelled.is_set() and not retry:
                self.failed.emit("Não foi possível iniciar o decodificador do vídeo Cloud.")
        finally:
            self.source = ""
            if descriptor is not None:
                os.close(descriptor)
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                try:
                    self.exit_code = process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    self.exit_code = process.wait(timeout=2)
                process.stdout.close()
                if diagnostic_thread is not None:
                    diagnostic_thread.join(timeout=1)
                if getattr(process, "stderr", None) is not None:
                    process.stderr.close()
            with self._lock:
                self._process = None

    def native_frame(self, data):
        frame = QVideoFrame(QVideoFrameFormat(
            QSize(self.width, self.height), QVideoFrameFormat.PixelFormat.Format_NV12))
        if not frame.map(QVideoFrame.MapMode.WriteOnly):
            raise RuntimeError("Video buffer unavailable")
        try:
            source = memoryview(data)
            offset = 0
            for plane, rows in ((0, self.height), (1, self.height // 2)):
                pointer = frame.bits(plane)
                pointer.setsize(frame.mappedBytes(plane))
                destination = memoryview(pointer)
                stride = frame.bytesPerLine(plane)
                if stride == self.width:
                    size = self.width * rows
                    destination[:size] = source[offset:offset + size]
                else:
                    for row in range(rows):
                        start = offset + row * self.width
                        destination[row * stride:row * stride + self.width] = source[start:start + self.width]
                offset += self.width * rows
        finally:
            frame.unmap()
        return frame

    def read_diagnostics(self, stream):
        # Inspect only a short memory buffer; never expose FFmpeg's URLs/logs.
        tail = b""
        input_section = False
        try:
            while True:
                chunk = stream.readline(4096)
                if not chunk:
                    break
                data = tail + chunk
                line = chunk.lstrip()
                if line.startswith(b"Input #"):
                    input_section = True
                elif line.startswith(b"Output #"):
                    input_section = False
                # Output headers can report a guessed tbr (e.g. 33 fps) even
                # while the camera supplies 15 fps. Do not pace from that.
                if input_section and b"Video:" in chunk and not self.input_frame_rate_detected:
                    rate = re.search(rb"\b(\d+(?:\.\d+)?) fps\b", chunk)
                    if rate is not None and 1 <= float(rate[1]) <= 120:
                        self.frame_rate = float(rate[1])
                        self.input_frame_rate_detected = True
                if re.search(rb"\b(?:401|403)\b[^\r\n]*(?:unauthorized|forbidden)", data, re.IGNORECASE):
                    self.authentication_refused.set()
                tail = data[-128:]
        except (OSError, ValueError):
            pass

    def cancel(self):
        self.cancelled.set()
        with self._lock:
            process = self._process
            if process is not None and process.poll() is None:
                process.terminate()

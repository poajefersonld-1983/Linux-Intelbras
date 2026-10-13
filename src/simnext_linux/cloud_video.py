"""Decode the RTSP TCP stream without exposing its credentials in argv/logs."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QImage


class CloudVideoWorker(QThread):
    frame = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, source: str, parent=None, *, size=(640, 360)):
        super().__init__(parent)
        if size not in ((640, 360), (1280, 720)):
            raise ValueError("Unsupported video size")
        self.width, self.height = size
        self.source = source
        self.cancelled = threading.Event()
        self._lock = threading.Lock()
        self._process = None
        self.frames = 0
        self.exit_code = None
        self.authentication_refused = threading.Event()

    def run(self):
        descriptor = None
        process = None
        diagnostic_thread = None
        try:
            executable = shutil.which("ffmpeg")
            if executable is None:
                raise RuntimeError("decoder unavailable")
            descriptor = os.memfd_create("monitor-video-source", 0)
            rtsp = self.source.lower().startswith(("rtsp://", "rtsps://"))
            escaped = self.source.replace("'", "'\\''")
            playlist = "ffconcat version 1.0\nfile '" + escaped + "'\n"
            if rtsp:
                playlist += "option rtsp_transport tcp\noption timeout 15000000\n"
            os.write(descriptor, playlist.encode("utf-8"))
            os.lseek(descriptor, 0, os.SEEK_SET)
            self.source = ""
            command = [executable, "-nostdin", "-hide_banner", "-loglevel", "error"]
            command += ["-f", "concat", "-safe", "0", "-protocol_whitelist", "file,rtsp,rtsps,rtp,tcp,udp,crypto,tls",
                        "-i", f"/proc/self/fd/{descriptor}", "-map", "0:v:0", "-an",
                        "-vf", f"fps=10,scale={self.width}:{self.height}:force_original_aspect_ratio=decrease,pad={self.width}:{self.height}:(ow-iw)/2:(oh-ih)/2",
                        "-pix_fmt", "rgba", "-f", "rawvideo", "pipe:1"]
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
            length = self.width * self.height * 4
            while not self.cancelled.is_set():
                data = bytearray()
                while len(data) < length and not self.cancelled.is_set():
                    part = process.stdout.read(length - len(data))
                    if not part:
                        break
                    data.extend(part)
                if len(data) != length or self.cancelled.is_set():
                    break
                frame = QImage(bytes(data), self.width, self.height, self.width * 4, QImage.Format.Format_RGBA8888).copy()
                self.frames += 1
                self.frame.emit(frame)
            if diagnostic_thread is not None:
                diagnostic_thread.join(timeout=1)
            if not self.cancelled.is_set():
                if self.authentication_refused.is_set():
                    self.failed.emit("O DVR recusou a autenticação. Use Configurar cliente → Senha do equipamento nesta sessão.")
                else:
                    self.failed.emit("Transmissão encerrada." if self.frames else
                                     "O DVR não iniciou o vídeo. A conexão será tentada novamente.")
        except Exception:
            if not self.cancelled.is_set():
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

    def read_diagnostics(self, stream):
        # Inspect only a short memory buffer; never expose FFmpeg's URLs/logs.
        tail = b""
        try:
            while True:
                chunk = stream.read(1024)
                if not chunk:
                    break
                data = tail + chunk
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

"""Entrada do aplicativo; nenhuma conexão é iniciada durante a abertura."""

import os
import re
import sqlite3
import sys

# Necessário para a cadeia RTSP/RTP no backend FFmpeg do Qt 6.
os.environ.setdefault("QT_FFMPEG_PROTOCOL_WHITELIST", "file,rtsp,rtp,tcp,udp,crypto")
os.environ["QT_LOGGING_RULES"] = os.environ.get("QT_LOGGING_RULES", "") + "\nqt.multimedia.*=false"


def redact_media_message(message: str) -> str:
    return re.sub(r"rtsps?://[^\s\"'<>]+", "rtsp://[endereço omitido]", message, flags=re.IGNORECASE)


def main() -> int:
    from PyQt6.QtCore import qInstallMessageHandler
    from PyQt6.QtWidgets import QApplication, QMessageBox
    from .gui import MainWindow
    from .store import DeviceStore

    def qt_message_handler(_kind, context, message):
        if context.category and context.category.startswith("qt.multimedia"):
            return
        print(redact_media_message(message), file=sys.stderr)

    qInstallMessageHandler(qt_message_handler)
    app = QApplication(sys.argv)
    app.setApplicationName("Linux-Intelbras")
    app.setDesktopFileName("linux-intelbras")
    app.setOrganizationName("simnext-linux")
    try:
        store = DeviceStore()
        window = MainWindow(store)
    except (OSError, ValueError, sqlite3.Error):
        QMessageBox.critical(None, "Cadastro indisponível", "Não foi possível abrir o cadastro local. Verifique o acesso à pasta de dados do usuário.")
        return 1
    window.show()
    try:
        return app.exec()
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())

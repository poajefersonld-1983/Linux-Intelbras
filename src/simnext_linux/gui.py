"""Native Qt monitor; connections are always initiated by the operator."""
from __future__ import annotations

from uuid import uuid4
import json
import os
import threading
from dataclasses import replace

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QMimeData, QEvent, QTimer
from PyQt6.QtGui import QAction, QCloseEvent, QPixmap, QDrag
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
from PyQt6.QtMultimediaWidgets import QVideoWidget
from PyQt6.QtWidgets import (
    QApplication, QMenu, QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPushButton,
    QSpinBox, QStackedWidget, QVBoxLayout, QWidget,
)

from .domain import Device, build_rtsp_url
from .p2p import P2PBridge, P2PError, bridge_pool
from .cloud_video import CloudVideoWorker


CAMERA_MIME = "application/x-monitor-linux-cameras"
DEVICE_ROLE = int(Qt.ItemDataRole.UserRole) + 1


class CameraList(QListWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)

    def camera_mime(self):
        mime = QMimeData()
        cameras = [{"device": item.data(DEVICE_ROLE),
                    "channel": item.data(Qt.ItemDataRole.UserRole)}
                   for item in self.selectedItems()]
        mime.setData(CAMERA_MIME, json.dumps(cameras).encode("utf-8"))
        return mime

    def startDrag(self, actions):
        if not self.selectedItems():
            return
        drag = QDrag(self)
        drag.setMimeData(self.camera_mime())
        drag.exec(Qt.DropAction.CopyAction)


class P2PWorker(QThread):
    ready = pyqtSignal(int)
    failed = pyqtSignal(str)

    def __init__(self, bridge, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.cancelled = threading.Event()

    def run(self):
        try:
            port = self.bridge.start()
            if not self.cancelled.is_set():
                self.ready.emit(port)
        except P2PError as error:
            if not self.cancelled.is_set():
                self.failed.emit(str(error))
        except Exception:
            if not self.cancelled.is_set():
                self.failed.emit("Falha ao iniciar a conexão Intelbras Cloud.")
        if self.cancelled.is_set():
            self.bridge.close()

    def cancel(self):
        self.cancelled.set()
        self.bridge.close()


class DeviceDialog(QDialog):
    def __init__(self, parent=None, device: Device | None = None):
        super().__init__(parent)
        self.setWindowTitle("Editar dispositivo" if device else "Adicionar dispositivo")
        self.device_id = device.id if device else str(uuid4())
        self.result_device = None
        form = QFormLayout(self)
        self.name = QLineEdit(device.name if device else "")
        self.host = QLineEdit(device.host if device else "")
        self.host.setPlaceholderText("IP ou nome do dispositivo, sem protocolo")
        self.connection = QComboBox()
        self.connection.addItem("IP/DDNS (RTSP)", "rtsp")
        self.connection.addItem("Intelbras Cloud (serial)", "cloud")
        self.connection.setCurrentIndex(1 if device and device.connection == "cloud" else 0)
        self.serial = QLineEdit(device.serial if device else "")
        self.connection.currentIndexChanged.connect(lambda: self.host.setEnabled(self.connection.currentData() == "rtsp"))
        self.host.setEnabled(self.connection.currentData() == "rtsp")
        self.port = QSpinBox(); self.port.setRange(1, 65535)
        self.port.setValue(device.port if device else 554)
        self.channel = QSpinBox(); self.channel.setRange(1, 65535)
        self.channel.setValue(device.channel if device else 1)
        self.stream = QComboBox()
        self.stream.addItem("HD (principal)", 0); self.stream.addItem("SD (extra)", 1)
        self.stream.setCurrentIndex(0 if not device or device.subtype == 0 else 1)
        self.username = QLineEdit(device.username if device else "admin")
        self.path = QLineEdit(device.path if device else "/cam/realmonitor")
        for label, field in [("Nome", self.name), ("Conexão", self.connection), ("Serial Cloud", self.serial), ("Endereço", self.host),
                             ("Porta RTSP", self.port), ("Canal", self.channel),
                             ("Fluxo", self.stream), ("Usuário", self.username),
                             ("Caminho RTSP", self.path)]:
            form.addRow(label, field)
        note = QLabel("A senha será solicitada ao conectar e não será salva.")
        if device and device.connection == "cloud":
            note.setText("Conexão Intelbras Cloud por serial. As credenciais ficam somente na sessão.")
        note.setWordWrap(True); form.addRow(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save |
                                   QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.save); buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def save(self):
        try:
            device = Device(id=self.device_id, name=self.name.text().strip(),
                            host=self.host.text().strip(), port=self.port.value(),
                            channel=self.channel.value(), subtype=self.stream.currentData(),
                            username=self.username.text().strip(), path=self.path.text().strip(),
                            serial=self.serial.text().strip(), connection=self.connection.currentData())
            device.validate()
        except ValueError as exc:
            QMessageBox.warning(self, "Cadastro inválido", str(exc)); return
        self.result_device = device
        self.accept()


class VideoTile(QWidget):
    cameras_dropped = pyqtSignal(object, int)
    activity_changed = pyqtSignal()
    connection_failed = pyqtSignal(str)
    tile_clicked = pyqtSignal(int)

    def __init__(self, number, parent=None):
        super().__init__(parent)
        self.number = number
        self.setObjectName(f"tile_{number}")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAcceptDrops(True)
        self.channel_number = None
        self.device_id = None
        self.device_name = None
        self.last_image = None
        self.p2p_bridge = None
        self.p2p_worker = None
        self._generation = 0
        self._pending_password = None
        self._active_device = None
        self._session_password = None
        self._retry_count = 0
        self._retry_context = None
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self.retry_video)
        self._workers = []
        self.cloud_video_worker = None
        self._cloud_fullscreen = None
        self._fullscreen_image = None
        self.audio = QAudioOutput(self); self.audio.setMuted(True)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio)
        self.video = QVideoWidget(self)
        self.placeholder = QLabel("Nenhum vídeo aberto")
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setStyleSheet("background:#111827;color:#94a3b8;padding:16px;border-radius:4px")
        self.display = QStackedWidget(self)
        self.display.addWidget(self.placeholder)
        self.display.addWidget(self.video)
        self.cloud_image = QLabel()
        self.cloud_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cloud_image.setStyleSheet("background:#111827")
        self.display.addWidget(self.cloud_image)
        for surface in (self.display, self.video, self.placeholder, self.cloud_image):
            surface.setAcceptDrops(True)
            surface.installEventFilter(self)
        self.player.setVideoOutput(self.video)
        self.title = QLabel(f"Tela {number} — livre")
        self.title.installEventFilter(self)
        self.quality = QComboBox()
        self.quality.addItem("SD", 1); self.quality.addItem("HD", 0)
        self.quality.setCurrentIndex(1)
        self.quality.setToolTip("SD: fluxo extra, menor consumo. HD: fluxo principal, mais detalhes (conforme configuração do DVR).")
        self._presentation = False
        self._quality_chosen = False
        self.quality.currentIndexChanged.connect(self.change_quality)
        self.status = QLabel("Selecione um dispositivo e clique em Conectar.")
        self.status.setWordWrap(True)
        self.mute = QCheckBox("Áudio")
        self.mute.toggled.connect(lambda enabled: self.audio.setMuted(not enabled))
        stop = QPushButton("Desconectar"); stop.clicked.connect(self.disconnect)
        snapshot = QPushButton("Capturar"); snapshot.clicked.connect(self.capture)
        full = QPushButton("Tela cheia"); full.clicked.connect(self.fullscreen)
        controls = QHBoxLayout()
        for widget in [self.mute, snapshot, full, stop]: controls.addWidget(widget)
        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        header.addWidget(self.title, 1); header.addWidget(self.quality)
        layout.addLayout(header); layout.addWidget(self.display, 1)
        layout.addWidget(self.status); layout.addLayout(controls)
        self._chrome_widgets = [self.title, self.quality, self.status, self.mute, snapshot, full, stop]
        self.video.setMinimumSize(160, 100)
        self.display.setMinimumSize(160, 100)
        self.player.errorOccurred.connect(self.failed)
        self.player.mediaStatusChanged.connect(self.media_status)
        self.video.videoSink().videoFrameChanged.connect(self.frame_changed)

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(CAMERA_MIME):
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dropEvent(self, event):
        if event.mimeData().hasFormat(CAMERA_MIME):
            self.cameras_dropped.emit(event.mimeData(), self.number - 1)
            event.acceptProposedAction()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.tile_clicked.emit(self.number - 1)
        super().mousePressEvent(event)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            self.tile_clicked.emit(self.number - 1)
        if event.type() in (QEvent.Type.DragEnter, QEvent.Type.DragMove):
            self.dragEnterEvent(event)
            return event.isAccepted()
        if event.type() == QEvent.Type.Drop:
            self.dropEvent(event)
            return event.isAccepted()
        return super().eventFilter(watched, event)

    def connect_device(self, device, password):
        from PyQt6.QtCore import QUrl
        device = replace(device, subtype=self.quality.currentData() if self._quality_chosen else 0)
        self.disconnect()
        self.quality.blockSignals(True)
        self.quality.setCurrentIndex(self.quality.findData(device.subtype))
        self.quality.blockSignals(False)
        self._active_device = device
        self._session_password = password
        self.device_id = device.id; self.device_name = device.name
        self.channel_number = device.channel
        self.activity_changed.emit()
        self.title.setText(f"{device.name} — Câmera {device.channel}")
        self.status.setText("Conectando…")
        self.placeholder.setText("Conectando…")
        if device.connection == "cloud":
            self.status.setText("Conectando pelo Intelbras Cloud…")
            try:
                self.p2p_bridge = bridge_pool.acquire(device.serial, device.port)
            except P2PError as error:
                self.disconnect()
                self.status.setText(str(error))
                return
            self._pending_password = password
            generation = self._generation
            worker = P2PWorker(self.p2p_bridge, self)
            self.p2p_worker = worker
            self._workers.append(worker)
            worker.ready.connect(lambda port: self.p2p_ready(device, port, generation))
            worker.failed.connect(lambda message: self.p2p_failed(message, generation))
            worker.finished.connect(lambda: self.finish_worker(worker))
            worker.start()
            return
        self.player.setSource(QUrl(build_rtsp_url(device, password)))
        self.player.play()

    def p2p_ready(self, device, port, generation):
        if generation != self._generation or self.device_id != device.id:
            return
        from PyQt6.QtCore import QUrl
        local = replace(device, host="127.0.0.1", port=port, connection="rtsp")
        password, self._pending_password = self._pending_password, None
        self.status.setText("Túnel Cloud conectado. Abrindo vídeo…")
        size = (1280, 720) if device.subtype == 0 else (640, 360)
        worker = CloudVideoWorker(build_rtsp_url(local, password or ""), self, size=size)
        self.cloud_video_worker = worker
        self._workers.append(worker)
        worker.frame.connect(lambda image: self.cloud_frame(image, generation))
        worker.failed.connect(lambda message: self.p2p_failed(message, generation))
        worker.finished.connect(lambda: self.finish_worker(worker))
        self.mute.setEnabled(False)
        self.mute.setToolTip("Áudio por Intelbras Cloud ainda não disponível.")
        worker.start()

    def set_presentation(self, enabled):
        if enabled == self._presentation:
            return
        self._presentation = enabled
        for widget in self._chrome_widgets:
            widget.setVisible(not enabled)
        if enabled:
            self._normal_tile_layout = (self.layout().contentsMargins(), self.layout().spacing(), self.placeholder.styleSheet())
            self.layout().setContentsMargins(0, 0, 0, 0); self.layout().setSpacing(0)
            self.placeholder.setStyleSheet("background:black;color:transparent;padding:0;border:0")
        else:
            margins, spacing, style = self._normal_tile_layout
            self.layout().setContentsMargins(margins); self.layout().setSpacing(spacing)
            self.placeholder.setStyleSheet(style)

    def change_quality(self, *_):
        self._quality_chosen = True
        device = self._active_device
        if device is None and self._retry_context is not None:
            device, password, _ = self._retry_context
        else:
            password = self._session_password
        if device is None:
            return
        updated = replace(device, subtype=self.quality.currentData())
        proxy = self.p2p_bridge.entry["bridge"].proxy if self.p2p_bridge is not None else None
        if updated.connection == "cloud" and proxy is not None:
            self._retry_timer.stop(); self._retry_context = None; self._retry_count = 0
            self._generation += 1
            generation = self._generation
            self._active_device = updated
            self._pending_password = password
            self.last_image = None
            self.status.setText(f"Alterando para {self.quality.currentText()}…")
            old = self.cloud_video_worker
            started = False
            def restart():
                nonlocal started
                if started or generation != self._generation:
                    return
                started = True
                self.p2p_ready(updated, proxy.port, generation)
            if old is not None and old.isRunning():
                old.finished.connect(restart)
                old.cancel()
                if not old.isRunning():
                    restart()
            else:
                restart()
        else:
            self.connect_device(updated, password or "")

    def cloud_frame(self, image, generation):
        if generation != self._generation or not self.device_id:
            return
        first = self.last_image is None
        self.last_image = image
        if first:
            self.activity_changed.emit()
        self.cloud_image.setPixmap(QPixmap.fromImage(image).scaled(
            self.display.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))
        self.status.setText("Reproduzindo via Intelbras Cloud")
        self.display.setCurrentWidget(self.cloud_image)
        if self._fullscreen_image is not None:
            self._fullscreen_image.setPixmap(QPixmap.fromImage(image).scaled(
                self._fullscreen_image.size(), Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))

    def p2p_failed(self, message, generation):
        if generation != self._generation:
            return
        if (self._active_device is not None and self._active_device.connection == "cloud"
                and self.cloud_video_worker is not None and self.last_image is None
                and self._retry_count < 2 and "recusou a autenticação" not in message
                and "decodificador" not in message):
            context = (self._active_device, self._session_password, self._retry_count + 1)
            self.disconnect()
            self._retry_context = context
            self.device_id, self.device_name = context[0].id, context[0].name
            self.channel_number = context[0].channel
            self.title.setText(f"{self.device_name} — Câmera {self.channel_number}")
            self.status.setText(f"Aguardando o DVR liberar o vídeo… nova tentativa {context[2]}/2 em 15 segundos.")
            self._retry_timer.start(15000)
            self.activity_changed.emit()
            return
        if self.device_id is not None:
            self.connection_failed.emit(self.device_id)
        self.disconnect()
        self.status.setText(message)

    def retry_video(self):
        context = self._retry_context
        if context is None:
            return
        device, password, attempts = context
        self.connect_device(device, password)
        self._retry_count = attempts

    def finish_worker(self, worker):
        if self.p2p_worker is worker:
            self.p2p_worker = None
        if self.cloud_video_worker is worker:
            self.cloud_video_worker = None
        if worker in self._workers:
            self._workers.remove(worker)
        worker.deleteLater()

    def disconnect(self):
        from PyQt6.QtCore import QUrl
        self.player.stop(); self.player.setSource(QUrl())
        self._generation += 1
        self._retry_timer.stop()
        self._retry_context = None
        self._retry_count = 0
        self._active_device = None
        self._session_password = None
        self._pending_password = None
        if self.cloud_video_worker is not None and self.cloud_video_worker.isRunning():
            self.cloud_video_worker.cancel()
        if self.p2p_worker is not None and self.p2p_worker.isRunning():
            self.p2p_worker.cancel()
        if self.p2p_bridge is not None:
            self.p2p_bridge.close()
            self.p2p_bridge = None
        self.device_id = None; self.device_name = None; self.last_image = None
        self.channel_number = None
        self.activity_changed.emit()
        self.mute.setChecked(False); self.audio.setMuted(True)
        self.mute.setEnabled(True)
        self.mute.setToolTip("")
        self.cloud_image.clear()
        if self._cloud_fullscreen is not None:
            self._cloud_fullscreen.close()
        self.title.setText(f"Tela {self.number} — livre"); self.status.setText("Desconectado")
        self.placeholder.setText("Nenhum vídeo aberto")
        self.display.setCurrentWidget(self.placeholder)
        self.video.setFullScreen(False)

    def failed(self, *_):
        # Backend error strings can contain the source URL and credentials.
        if self.device_id is not None:
            self.connection_failed.emit(self.device_id)
        self.disconnect()
        self.status.setText("Não foi possível reproduzir. Verifique rede, credenciais, canal e suporte RTSP.")

    def media_status(self, status):
        if self.device_id and status == QMediaPlayer.MediaStatus.BufferingMedia:
            self.status.setText("Carregando…")
        elif self.device_id and status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.disconnect(); self.status.setText("Transmissão encerrada")

    def frame_changed(self, frame):
        if self.device_id and frame.isValid():
            image = frame.toImage()
            if not image.isNull():
                first = self.last_image is None
                self.last_image = image
                if first:
                    self.activity_changed.emit()
                self.status.setText("Reproduzindo")
                self.display.setCurrentWidget(self.video)

    def capture(self):
        if self.last_image is None:
            QMessageBox.information(self, "Captura", "Ainda não há imagem disponível."); return
        image = self.last_image.copy()
        path, _ = QFileDialog.getSaveFileName(self, "Salvar captura", "captura.png", "PNG (*.png)")
        if path and not image.save(path, "PNG"):
            QMessageBox.warning(self, "Captura", "Não foi possível salvar a imagem.")

    def fullscreen(self):
        if self.cloud_video_worker is not None or self.display.currentWidget() is self.cloud_image:
            if self._cloud_fullscreen is not None:
                self._cloud_fullscreen.close()
                return
            dialog = QDialog(self)
            dialog.setWindowTitle(self.device_name or "Intelbras Cloud")
            image = QLabel(dialog)
            image.setAlignment(Qt.AlignmentFlag.AlignCenter)
            image.setStyleSheet("background:black")
            layout = QVBoxLayout(dialog)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(image)
            self._cloud_fullscreen, self._fullscreen_image = dialog, image
            dialog.finished.connect(self.close_cloud_fullscreen)
            dialog.showFullScreen()
            if self.last_image is not None:
                self.cloud_frame(self.last_image, self._generation)
            return
        self.video.setFullScreen(not self.video.isFullScreen())

    def close_cloud_fullscreen(self):
        dialog, self._cloud_fullscreen = self._cloud_fullscreen, None
        self._fullscreen_image = None
        if dialog is not None:
            dialog.deleteLater()


class MainWindow(QMainWindow):
    def __init__(self, store):
        super().__init__()
        self.store = store
        self._channel_limits = self.load_channel_limits()
        self.cloud_client = None
        self._passwords = {}
        self._mosaic_fullscreen = False
        self._mosaic_geometry = None
        self._mosaic_was_maximized = False
        self.setWindowTitle("Linux-Intelbras")
        self.resize(1280, 800)
        central = QWidget(); self.setCentralWidget(central)
        root = QHBoxLayout(central)
        sidebar = QWidget(); sidebar.setMaximumWidth(300)
        self.sidebar = sidebar
        side = QVBoxLayout(sidebar)
        heading = QLabel("Clientes / DVRs"); heading.setStyleSheet("font-size:20px;font-weight:600")
        side.addWidget(heading)
        self.account_notice = QLabel("Conta Intelbras não conectada. Entre uma vez para usar as credenciais dos clientes.")
        self.account_notice.setWordWrap(True)
        side.addWidget(self.account_notice)
        self.devices = QListWidget(); side.addWidget(self.devices, 1)
        self.devices.currentItemChanged.connect(self.refresh_channels)
        side.addWidget(QLabel("Câmeras — clique para abrir ou arraste"))
        self.all_clients = QCheckBox("Câmeras de todos os clientes")
        self.all_clients.toggled.connect(self.refresh_channels)
        self.all_clients.setChecked(False)
        side.addWidget(self.all_clients)
        self.channel_limit = QSpinBox()
        self.channel_limit.setRange(1, 64)
        self.channel_limit.setValue(16)
        self.channel_limit.setPrefix("Exibir canais 1 a ")
        self.channel_limit.setToolTip("Ajuste conforme a quantidade de canais do DVR. Esta lista não detecta câmeras online.")
        side.addWidget(self.channel_limit)
        self.channels = CameraList()
        self.channels.setMaximumHeight(220)
        self.channels.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.channels.setToolTip("Arraste uma câmera; use Ctrl para selecionar várias.")
        side.addWidget(self.channels)
        self.channel_limit.valueChanged.connect(self.channel_range_changed)
        self.channels.itemClicked.connect(self.open_clicked_camera)
        account = QPushButton("Conta Intelbras")
        account.clicked.connect(lambda: self.open_cloud_account())
        side.addWidget(account)
        configure = QPushButton("Configurar cliente")
        menu = QMenu(configure)
        for label, callback in [("Senha do equipamento nesta sessão", self.configure_password),
                                ("Adicionar cliente", self.add_device), ("Editar cliente", self.edit_device),
                                ("Excluir cliente", self.delete_device), ("Sobre Linux-Intelbras", self.show_about)]:
            action = menu.addAction(label); action.triggered.connect(callback)
        configure.setMenu(menu); side.addWidget(configure)
        side.addWidget(QLabel("Mosaico"))
        self.mosaic = QComboBox()
        for n in (1, 4, 9): self.mosaic.addItem(f"{n} tela(s)", n)
        self.mosaic.setCurrentIndex(1)
        self.mosaic.currentIndexChanged.connect(self.layout_tiles); side.addWidget(self.mosaic)
        self.mosaic_fullscreen_button = QPushButton("Tela cheia do mosaico (F11)")
        self.mosaic_fullscreen_button.clicked.connect(self.toggle_mosaic_fullscreen)
        side.addWidget(self.mosaic_fullscreen_button)
        side.addWidget(QLabel("Tela de destino"))
        self.target = QComboBox(); side.addWidget(self.target)
        self.target.currentIndexChanged.connect(self.highlight_target)
        connect = QPushButton("Abrir câmeras no mosaico"); connect.clicked.connect(self.connect_selected)
        side.addWidget(connect)
        note = QLabel("Intelbras Cloud e RTSP\nClique no quadro e depois na câmera.\nOu arraste a câmera para o quadro.")
        note.setWordWrap(True); side.addWidget(note)
        root.addWidget(sidebar)
        area = QWidget(); area_layout = QVBoxLayout(area)
        self.fullscreen_exit = QPushButton("Sair da tela cheia (Esc)")
        self.fullscreen_exit.clicked.connect(self.exit_fullscreen)
        self.fullscreen_exit.hide()
        area_layout.addWidget(self.fullscreen_exit, 0, Qt.AlignmentFlag.AlignRight)
        self.grid = QGridLayout(); area_layout.addLayout(self.grid, 1)
        root.addWidget(area, 1)
        self.tiles = [VideoTile(i + 1) for i in range(9)]
        for tile in self.tiles:
            tile.cameras_dropped.connect(self.drop_cameras)
            tile.tile_clicked.connect(self.select_tile)
            tile.activity_changed.connect(self.update_camera_activity)
            tile.connection_failed.connect(lambda identity: self._passwords.pop(identity, None))
        self.setStyleSheet("QMainWindow{background:#f1f5f9} QLabel,QCheckBox{color:#172b4d} "
                           "QPushButton{padding:7px} QListWidget,QLineEdit,QComboBox{padding:5px} "
                           "QVideoWidget{background:#111827}")
        escape = QAction(self); escape.setShortcut("Escape")
        escape.triggered.connect(self.exit_fullscreen); self.addAction(escape)
        full_mosaic = QAction(self); full_mosaic.setShortcut("F11")
        full_mosaic.triggered.connect(self.toggle_mosaic_fullscreen); self.addAction(full_mosaic)
        self.refresh(); self.layout_tiles()
        if self.devices.count(): self.devices.setCurrentRow(0)

    def refresh(self):
        selected = self.selected_device()
        selected_id = selected.id if selected else None
        self.devices.clear()
        for device in self.store.list():
            item = QListWidgetItem(f"{device.name}\nIntelbras Cloud • serial" if device.connection == "cloud" else f"{device.name}\nCanal {device.channel} • {'principal' if device.subtype == 0 else 'extra'}")
            item.setData(Qt.ItemDataRole.UserRole, device); self.devices.addItem(item)
            if device.id == selected_id: self.devices.setCurrentItem(item)
        if self.devices.count() and self.devices.currentItem() is None:
            self.devices.setCurrentRow(0)

    def open_cloud_account(self, *, for_playback=False):
        from .cloud import CloudClient, CloudError
        from .cloud_gui import CloudDialog
        try:
            if self.cloud_client is None:
                self.cloud_client = CloudClient(installation_id=self.store.cloud_installation_id(), schema_callback=self.save_catalog_schema)
        except CloudError as exc:
            QMessageBox.information(self, "Conta Intelbras", str(exc))
            return
        except Exception:
            QMessageBox.warning(self, "Conta Intelbras", "Não foi possível iniciar o acesso à conta.")
            return
        CloudDialog(self.cloud_client, self.store, self.refresh, self, return_after_login=for_playback).exec()
        self._passwords.clear()
        authenticated = bool(getattr(self.cloud_client, "authenticated", False))
        self.account_notice.setText("Conta Intelbras conectada. Escolha o cliente e clique na câmera." if authenticated else "Conta Intelbras não conectada. Clique em Conta Intelbras e em Entrar.")

    def save_catalog_schema(self, schema):
        path = self.store.path.with_name("catalog-schema.json")
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(schema, output, ensure_ascii=False, indent=2)

    def selected_device(self):
        item = self.devices.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def add_device(self): self.edit(None)

    def edit_device(self):
        device = self.selected_device()
        if device: self.edit(device)

    def edit(self, device):
        dialog = DeviceDialog(self, device)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try: self.store.save(dialog.result_device)
            except Exception:
                QMessageBox.warning(self, "Cadastro", "Não foi possível salvar o dispositivo."); return
            if device:
                self._passwords.pop(device.id, None)
                for tile in self.tiles:
                    if tile.device_id == device.id: tile.disconnect()
            self.refresh()

    def delete_device(self):
        device = self.selected_device()
        if not device: return
        if QMessageBox.question(self, "Excluir dispositivo", f"Excluir {device.name}?") != QMessageBox.StandardButton.Yes: return
        try: self.store.delete(device.id)
        except Exception:
            QMessageBox.warning(self, "Cadastro", "Não foi possível excluir o dispositivo."); return
        self._passwords.pop(device.id, None)
        for tile in self.tiles:
            if tile.device_id == device.id: tile.disconnect()
        self.refresh()

    def layout_tiles(self, *_):
        count = self.mosaic.currentData()
        while self.grid.count(): self.grid.takeAt(0)
        for i in range(3):
            self.grid.setRowStretch(i, 0)
            self.grid.setColumnStretch(i, 0)
        self.target.clear()
        columns = {1: 1, 4: 2, 9: 3}[count]
        for i in range(columns):
            self.grid.setRowStretch(i, 1)
            self.grid.setColumnStretch(i, 1)
        for i, tile in enumerate(self.tiles):
            tile.setVisible(i < count)
            if i < count:
                self.grid.addWidget(tile, i // columns, i % columns)
                self.target.addItem(f"Tela {i + 1}", i)
            else: tile.disconnect()

    def load_channel_limits(self):
        try:
            data = json.loads(self.store.path.with_name("channel-limits.json").read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {}
            return {key: value for key, value in data.items()
                    if isinstance(key, str) and len(key) <= 128 and type(value) is int and 1 <= value <= 64}
        except (OSError, ValueError):
            return {}

    def channel_range_changed(self, value):
        device = self.selected_device()
        if device is not None and not self.all_clients.isChecked():
            self._channel_limits[device.id] = value
            try:
                path = self.store.path.with_name("channel-limits.json")
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                    json.dump(self._channel_limits, output)
            except OSError:
                pass
        self.refresh_channels()

    def refresh_channels(self, *_):
        if not hasattr(self, "channels"):
            return
        device = self.selected_device()
        all_clients = self.all_clients.isChecked()
        self.channel_limit.setEnabled(not all_clients)
        if device is not None and not all_clients:
            self.channel_limit.blockSignals(True)
            self.channel_limit.setValue(self._channel_limits.get(device.id, max(16, min(64, device.channel))))
            self.channel_limit.blockSignals(False)
        self.channels.clear()
        devices = self.store.list() if all_clients else ([device] if device else [])
        for current in devices:
            limit = self._channel_limits.get(current.id, 16 if all_clients else self.channel_limit.value())
            for number in range(1, limit + 1):
                item = QListWidgetItem(f"{current.name} • Câmera {number}")
                item.setData(Qt.ItemDataRole.UserRole, number)
                item.setData(DEVICE_ROLE, current.id)
                self.channels.addItem(item)
                item.setSelected(device is not None and current.id == device.id and number == device.channel)
        self.update_camera_activity()
        selected = self.channels.selectedItems()
        if selected:
            self.channels.scrollToItem(selected[0])

    def update_camera_activity(self):
        if not hasattr(self, "tiles"):
            return
        active = {(tile.device_id, tile.channel_number): tile.last_image is not None
                  for tile in self.tiles if tile.device_id is not None}
        names = {device.id: device.name for device in self.store.list()}
        for i in range(self.channels.count()):
            item = self.channels.item(i)
            key = (item.data(DEVICE_ROLE), item.data(Qt.ItemDataRole.UserRole))
            state = " • Ao vivo" if active.get(key) else " • Conectando" if key in active else ""
            item.setText(f"{names.get(key[0], 'Cliente')} • Câmera {key[1]}{state}")
            item.setToolTip("Ao vivo: imagem recebida neste mosaico." if active.get(key) else "Arraste para abrir. A disponibilidade será verificada ao conectar.")

    def camera_choices(self, items):
        devices = {device.id: device for device in self.store.list()}
        choices = []
        seen = set()
        for identity, channel in items:
            if identity not in devices or type(channel) is not int or not 1 <= channel <= 64:
                raise ValueError("Seleção de câmera inválida. Atualize a lista e tente novamente.")
            if (identity, channel) not in seen:
                choices.append(replace(devices[identity], channel=channel))
                seen.add((identity, channel))
        if not choices or len(choices) > 9:
            raise ValueError("Selecione de uma a nove câmeras para abrir no mosaico.")
        return choices

    def drop_cameras(self, mime, target):
        try:
            if len(mime.data(CAMERA_MIME)) > 8192:
                raise ValueError("Seleção de câmera inválida.")
            records = json.loads(bytes(mime.data(CAMERA_MIME)))
            if not isinstance(records, list):
                raise ValueError("Seleção de câmera inválida.")
            choices = self.camera_choices([(record["device"], record["channel"]) for record in records])
            occupied = sum(tile.device_id is not None for tile in self.tiles) - bool(self.tiles[target].device_id)
            required = occupied + len(choices)
            if required > 9:
                raise ValueError("Não há telas livres para todas as câmeras arrastadas.")
            if required > self.mosaic.currentData():
                self.mosaic.setCurrentIndex(self.mosaic.findData(next(n for n in (1, 4, 9) if n >= required)))
            count = self.mosaic.currentData()
            order = list(range(target + 1, count)) + list(range(target))
            targets = [self.tiles[target]] + [self.tiles[i] for i in order if self.tiles[i].device_id is None]
            self.open_cameras(list(zip(choices, targets)))
        except (ValueError, KeyError, TypeError, UnicodeError):
            QMessageBox.information(self, "Arrastar câmera", "Não foi possível abrir esta seleção. Use as câmeras da lista e confira as telas livres.")

    def mosaic_assignments(self, channels):
        """Allocate only free tiles, starting at the chosen destination."""
        start = self.target.currentData() or 0
        occupied = sum(tile.device_id is not None for tile in self.tiles)
        required = occupied + len(channels)
        if required > len(self.tiles):
            raise ValueError("Não há telas livres para todas as câmeras. Selecione menos câmeras ou desconecte uma tela.")
        if required > self.mosaic.currentData():
            count = next(n for n in (1, 4, 9) if n >= required)
            self.mosaic.setCurrentIndex(self.mosaic.findData(count))
        self.target.setCurrentIndex(self.target.findData(start))
        count = self.mosaic.currentData()
        order = list(range(start, count)) + list(range(start))
        available = [self.tiles[i] for i in order if self.tiles[i].device_id is None]
        return list(zip(channels, available))

    def select_tile(self, index):
        item = self.target.findData(index)
        if item >= 0:
            self.target.setCurrentIndex(item)
            self.highlight_target()

    def highlight_target(self, *_):
        if not hasattr(self, "tiles"):
            return
        for index, tile in enumerate(self.tiles):
            color = "#2563eb" if index == self.target.currentData() else "transparent"
            border = "0px" if self._mosaic_fullscreen else f"2px solid {color}"
            tile.setStyleSheet(f"QWidget#tile_{tile.number} {{border:{border};}}")

    def configure_password(self):
        device = self.selected_device()
        if device is None:
            return
        password, accepted = QInputDialog.getText(self, "Senha do equipamento", f"Senha do DVR {device.name} (não é a senha da Conta Intelbras):", QLineEdit.EchoMode.Password)
        if accepted:
            self._passwords[device.id] = password
            self.account_notice.setText(f"Credencial de {device.name} disponível nesta sessão.")

    def open_clicked_camera(self, item):
        if QApplication.keyboardModifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier):
            return
        choices = self.camera_choices([(item.data(DEVICE_ROLE), item.data(Qt.ItemDataRole.UserRole))])
        target = self.target.currentData()
        if target is None:
            return
        self.open_cameras([(choices[0], self.tiles[target])])
        if self.tiles[target].device_id == choices[0].id:
            free = next((i for i in range(self.mosaic.currentData()) if self.tiles[i].device_id is None), target)
            self.select_tile(free)

    def connect_selected(self):
        try:
            choices = self.camera_choices([(item.data(DEVICE_ROLE), item.data(Qt.ItemDataRole.UserRole))
                                           for item in self.channels.selectedItems()])
            self.open_cameras(self.mosaic_assignments(choices))
        except ValueError as error:
            QMessageBox.information(self, "Mosaico", str(error))

    def open_cameras(self, assignments):
        # Collect each client's credentials before replacing any existing video.
        passwords = {}
        for device, tile in assignments:
            if device.id in passwords:
                continue
            password = self.cloud_client.device_password(device.id) if self.cloud_client is not None and hasattr(self.cloud_client, "device_password") else None
            if password is None:
                password = self._passwords.get(device.id)
            if password is None and device.connection == "cloud":
                if not bool(getattr(self.cloud_client, "authenticated", False)):
                    self.account_notice.setText("Entre na Conta Intelbras uma vez para abrir as câmeras dos clientes.")
                    self.open_cloud_account(for_playback=True)
                    password = self.cloud_client.device_password(device.id) if self.cloud_client is not None and hasattr(self.cloud_client, "device_password") else None
                if password is None:
                    self.account_notice.setText("Para abrir este cliente, entre na Conta Intelbras e atualize a lista; ou use Configurar cliente → Senha do equipamento nesta sessão.")
                    return
            if password is None:
                password, accepted = QInputDialog.getText(self, "Autenticação", f"Senha do equipamento {device.name}:", QLineEdit.EchoMode.Password)
                if not accepted:
                    return
                self._passwords[device.id] = password
            passwords[device.id] = password
        for device, tile in assignments:
            try:
                tile.connect_device(device, passwords[device.id])
            except Exception:
                tile.disconnect()
                tile.status.setText("Não foi possível iniciar a transmissão desta câmera.")
        passwords.clear()

    def show_about(self):
        from . import __version__
        QMessageBox.information(self, "Sobre Linux-Intelbras", f"Linux-Intelbras {__version__}\nDesenvolvedor: Jeferson Lackman")

    def toggle_mosaic_fullscreen(self, *_):
        if self._mosaic_fullscreen:
            self.exit_fullscreen()
            return
        self.exit_fullscreen()
        self._mosaic_geometry = self.saveGeometry()
        self._mosaic_was_maximized = self.isMaximized()
        self._mosaic_fullscreen = True
        self.sidebar.hide()
        self.fullscreen_exit.hide()
        self._presentation_layouts = []
        widget = self.grid.parentWidget()
        layouts = [self.grid]
        while widget is not None:
            if widget.layout() is not None:
                layouts.append(widget.layout())
            if widget is self.centralWidget():
                break
            widget = widget.parentWidget()
        for layout in dict.fromkeys(layouts):
            self._presentation_layouts.append((layout, layout.contentsMargins(), layout.spacing()))
            layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(0)
        self._normal_central_style = self.centralWidget().styleSheet()
        self.centralWidget().setStyleSheet("background:black")
        for tile in self.tiles:
            tile.set_presentation(True)
        self.highlight_target()
        self.showFullScreen()

    def exit_fullscreen(self):
        if self._mosaic_fullscreen:
            self._mosaic_fullscreen = False
            self.fullscreen_exit.hide()
            for tile in self.tiles:
                tile.set_presentation(False)
            for layout, margins, spacing in self._presentation_layouts:
                layout.setContentsMargins(margins); layout.setSpacing(spacing)
            self.centralWidget().setStyleSheet(self._normal_central_style)
            self.sidebar.show()
            self.highlight_target()
            self.showNormal()
            if self._mosaic_geometry is not None:
                self.restoreGeometry(self._mosaic_geometry)
            if self._mosaic_was_maximized:
                self.showMaximized()
        for tile in self.tiles:
            tile.video.setFullScreen(False)
            if tile._cloud_fullscreen is not None:
                tile._cloud_fullscreen.close()

    def closeEvent(self, event: QCloseEvent):
        for tile in self.tiles: tile.disconnect()
        for tile in self.tiles:
            for worker in list(tile._workers):
                worker.wait(3000)
        self._passwords.clear()
        if self.cloud_client is not None:
            self.cloud_client.logout()
        event.accept()

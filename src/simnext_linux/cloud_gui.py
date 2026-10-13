"""Account dialog with network operations isolated from the Qt UI thread."""
from __future__ import annotations

import threading

from PyQt6.QtCore import QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QInputDialog, QLabel, QMessageBox, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QAbstractItemView, QComboBox,
)

from .cloud import CloudError, local_devices


class AccountWorker(QThread):
    authorization = pyqtSignal(str)
    devices_ready = pyqtSignal(object)
    failed = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, client, operation, parent=None, source="simnext"):
        super().__init__(parent)
        self.client = client
        self.operation = operation
        self.source = source
        self.cancel_event = threading.Event()
        self.attempt = None
        self._lock = threading.Lock()

    def cancel(self):
        self.cancel_event.set()
        with self._lock:
            attempt = self.attempt
        if attempt is not None:
            try:
                attempt.close()
            except Exception:
                pass

    def run(self):
        try:
            if hasattr(self.client, "set_cancel_event"):
                self.client.set_cancel_event(self.cancel_event)
            if self.operation == "login":
                attempt = self.client.create_login()
                with self._lock:
                    self.attempt = attempt
                if self.cancel_event.is_set():
                    return
                self.authorization.emit(attempt.authorization_url)
                code = attempt.wait_for_code(self.cancel_event, timeout=180)
                if self.cancel_event.is_set():
                    return
                self.progress.emit("Autorização recebida. Validando a sessão…")
                self.client.finish_login(attempt, code)
            elif self.operation == "logout":
                self.client.logout()
                self.devices_ready.emit([])
                return
            if not self.cancel_event.is_set():
                self.progress.emit("Consultando dispositivos do iSIC Lite…" if self.source == "isic" else "Consultando dispositivos do SIM Next…")
                records = self.client.list_devices(source=self.source)
                if not self.cancel_event.is_set():
                    self.devices_ready.emit(records)
        except CloudError as exc:
            if not self.cancel_event.is_set():
                self.failed.emit(str(exc))
        except Exception:
            if not self.cancel_event.is_set():
                self.failed.emit("Não foi possível concluir a operação da conta.")
        finally:
            if hasattr(self.client, "set_cancel_event"):
                self.client.set_cancel_event(None)
            with self._lock:
                attempt = self.attempt
                self.attempt = None
            if attempt is not None:
                try:
                    attempt.close()
                except Exception:
                    pass
            if self.cancel_event.is_set() and self.operation == "login":
                try:
                    self.client.logout()
                except Exception:
                    pass


class CloudDialog(QDialog):
    def __init__(self, client, store, on_import_callback, parent=None, *, return_after_login=False):
        super().__init__(parent)
        self.client = client
        self.return_after_login = return_after_login
        self.store = store
        self.on_import_callback = on_import_callback
        self.records = []
        self.worker = None
        self._close_requested = False
        self.setWindowTitle("Conta Intelbras")
        self.resize(860, 470)
        layout = QVBoxLayout(self)
        note = QLabel("Entre pelo navegador para consultar os dispositivos da sua conta. "
                      "A sessão permanece somente na memória. A importação não abre vídeo.")
        if return_after_login:
            note.setText("Entre na Conta Intelbras. Depois de carregar os clientes, esta janela fecha e a câmera escolhida abre no mosaico.")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.source_combo = QComboBox()
        self.source_combo.addItem("iSIC Lite", "isic")
        self.source_combo.addItem("SIM Next", "simnext")
        layout.addWidget(QLabel("Origem dos dispositivos"))
        layout.addWidget(self.source_combo)
        self.source_combo.currentIndexChanged.connect(self.source_changed)
        self.status = QLabel("Clique em Entrar para autorizar o acesso.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Nome", "Serial", "Modelo", "Endereço", "Canais", "Acesso"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table, 1)
        controls = QHBoxLayout()
        self.login_button = QPushButton("Entrar")
        self.refresh_button = QPushButton("Atualizar lista")
        self.logout_button = QPushButton("Sair da conta")
        self.cancel_button = QPushButton("Cancelar operação")
        self.import_button = QPushButton("Importar selecionados")
        for button in (self.login_button, self.refresh_button, self.logout_button,
                       self.cancel_button, self.import_button):
            controls.addWidget(button)
        layout.addLayout(controls)
        self.login_button.clicked.connect(lambda: self.start("login"))
        self.refresh_button.clicked.connect(lambda: self.start("refresh"))
        self.logout_button.clicked.connect(lambda: self.start("logout"))
        self.cancel_button.clicked.connect(self.cancel)
        self.import_button.clicked.connect(self.import_selected)
        self.update_buttons()
        if self.client.authenticated:
            QTimer.singleShot(0, lambda: self.start("refresh"))

    def update_buttons(self):
        busy = self.worker is not None
        self.source_combo.setEnabled(not busy)
        authenticated = self.client.authenticated
        self.login_button.setEnabled(not busy and not authenticated)
        self.refresh_button.setEnabled(not busy and authenticated)
        self.logout_button.setEnabled(not busy and authenticated)
        self.cancel_button.setEnabled(busy)
        self.import_button.setEnabled(not busy and bool(self.records))

    def start(self, operation):
        if self.worker is not None:
            return
        if operation == "logout":
            self.populate([])
        self.status.setText("Aguardando autorização no navegador…" if operation == "login" else "Processando…")
        worker = AccountWorker(self.client, operation, self, source=self.source_combo.currentData())
        self.worker = worker
        worker.authorization.connect(self.open_authorization)
        worker.devices_ready.connect(self.populate)
        worker.failed.connect(self.show_error)
        worker.progress.connect(self.status.setText)
        worker.finished.connect(self.finished)
        self.update_buttons()
        worker.start()

    def source_changed(self):
        self.populate([])
        if self.client.authenticated:
            self.start("refresh")

    def open_authorization(self, url):
        if self.worker is None or self.worker.cancel_event.is_set():
            return
        if not QDesktopServices.openUrl(QUrl(url)):
            self.status.setText("Não foi possível abrir o navegador. Tente novamente.")
            self.cancel()

    def populate(self, records):
        self.records = list(records)
        self.table.setRowCount(len(self.records))
        for row, record in enumerate(self.records):
            values = [record.name, record.serial, record.model, record.host or "—",
                      str(len(record.channels)), "IP/DDNS" if record.host else "Intelbras Cloud (serial)"]
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(str(value)))
        self.table.resizeColumnsToContents()
        self.status.setText(f"{len(self.records)} dispositivo(s) disponível(is)." if self.records
                            else "Nenhum dispositivo na lista.")
        self.update_buttons()

    def show_error(self, message):
        self.status.setText(message)

    def cancel(self):
        if self.worker is not None:
            self.worker.cancel()
            self.status.setText("Cancelando operação…")

    def finished(self):
        worker = self.worker
        self.worker = None
        if worker is not None:
            if worker.cancel_event.is_set():
                self.status.setText("Operação cancelada.")
            worker.deleteLater()
        self.update_buttons()
        if self._close_requested:
            super().reject()
        elif self.return_after_login and self.client.authenticated and self.records:
            super().accept()

    def import_selected(self):
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        if not rows:
            QMessageBox.information(self, "Importar", "Selecione um ou mais dispositivos.")
            return
        imported = 0
        for row in rows:
            record = self.records[row]
            host = None
            try:
                devices = local_devices(record, host_override=host)
                for device in devices:
                    self.store.save(device)
                    imported += 1
            except CloudError as exc:
                QMessageBox.warning(self, "Importação", str(exc))
            except Exception:
                QMessageBox.warning(self, "Importação", "Não foi possível importar este dispositivo. Verifique o endereço local.")
        if imported:
            self.on_import_callback()
            self.status.setText(f"{imported} dispositivo(s) importado(s). Cadastros Cloud preservam o serial; use Conectar para abrir o vídeo.")

    def reject(self):
        if self.worker is not None:
            self._close_requested = True
            self.cancel()
            return
        super().reject()

    def closeEvent(self, event):
        if self.worker is not None:
            self._close_requested = True
            self.cancel()
            event.ignore()
        else:
            event.accept()

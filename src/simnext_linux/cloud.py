"""Conta Intelbras: navegador externo, PKCE e catálogo; tokens só em memória.

Contratos de origem são registrados em analysis/cloud-flow.md. Não implementa
transporte P2P nem interpreta as senhas de equipamentos retornadas pelo serviço.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import ipaddress
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import secrets
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid5, NAMESPACE_URL

from .domain import Device, normalized_host


class CloudError(Exception):
    """Mensagem segura para exibir; nunca contém resposta, token ou callback."""


@dataclass(frozen=True)
class CloudConfig:
    authorize_url: str
    token_url: str
    client_id: str
    api_base: str
    devices_path: str
    capabilities_path: str
    scopes: str = "openid"
    redirect_path: str = "/"
    token_header: str = "Authorization"
    installation_header: str = "installation-id"
    version_header: str = "app-version"
    api_version: str = "1.2.1"
    token_prefix: str = ""
    login_path: str = ""
    validate_path: str = ""
    refresh_installation_header: str = ""
    isic_api_base: str = ""
    isic_devices_path: str = "/api/v1/isic/mobile/video-devices/"

    def validate(self):
        for url in (self.authorize_url, self.token_url, self.api_base) + ((self.isic_api_base,) if self.isic_api_base else ()):
            parsed = urlsplit(url)
            local = parsed.hostname in ("127.0.0.1", "localhost", "::1")
            if (parsed.scheme != "https" and not (local and parsed.scheme == "http")) or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                raise CloudError("Configuração da Conta Intelbras inválida.")
        if not self.client_id or not self.redirect_path.startswith("/") or any(c in self.redirect_path for c in "?#"):
            raise CloudError("Configuração de autenticação inválida.")
        for header in (self.token_header, self.installation_header, self.version_header, self.refresh_installation_header):
            if header and any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-" for c in header):
                raise CloudError("Configuração de autenticação inválida.")


def default_config() -> CloudConfig:
    from .cloud_config import CONFIG
    values = dict(CONFIG)
    root = os.environ.get("XDG_CONFIG_HOME", "")
    directory = Path(root) if root and Path(root).is_absolute() else Path.home() / ".config"
    try:
        local = json.loads((directory / "simnext-linux" / "cloud.json").read_text(encoding="utf-8"))
        if not isinstance(local, dict):
            raise ValueError()
        values.update(local)
        config = CloudConfig(**values)
    except (OSError, ValueError, TypeError):
        raise CloudError("Configure o acesso autorizado à Conta Intelbras em cloud.json antes de entrar.") from None
    config.validate()
    return config


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


class LoginAttempt:
    """Listener apenas loopback; parâmetros inválidos não encerram o login."""
    def __init__(self, config: CloudConfig):
        config.validate()
        self.state = secrets.token_urlsafe(32)
        self.code_verifier = secrets.token_urlsafe(32)
        self._code = None
        self._error = None
        self._closed = False
        self._used = False
        owner = self

        class CallbackHandler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(2)

            def log_message(self, *_args):
                pass

            def do_GET(self):
                parts = urlsplit(self.path)
                query = parse_qs(parts.query, keep_blank_values=True)
                states = query.get("state", [])
                codes = query.get("code", [])
                valid = parts.path == config.redirect_path and len(states) == 1 and states[0].isascii() and secrets.compare_digest(states[0], owner.state)
                if not valid:
                    self.reply(400, "Resposta de autenticação inválida. Volte ao aplicativo.")
                    return
                if "error" in query:
                    owner._error = "A autorização não foi concluída. Tente entrar novamente."
                    self.reply(200, "Autorização não concluída. Volte ao aplicativo.")
                elif len(codes) == 1 and codes[0] and len(codes[0]) <= 8192:
                    owner._code = codes[0]
                    self.reply(200, "Autorização recebida. Pode voltar ao Linux-Intelbras.")
                else:
                    self.reply(400, "Resposta de autenticação incompleta. Volte ao aplicativo.")

            def reply(self, status, message):
                body = ("<!doctype html><html lang='pt-BR'><meta charset='utf-8'>"
                        "<title>Linux-Intelbras</title><script>history.replaceState(null,'',location.pathname)</script>"
                        f"<p>{message}</p></html>").encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self._server = HTTPServer(("127.0.0.1", 0), CallbackHandler)
        self._server.timeout = 0.2
        self.redirect_uri = f"http://127.0.0.1:{self._server.server_port}{config.redirect_path}"
        parameters = {
            "response_type": "code", "client_id": config.client_id,
            "redirect_uri": self.redirect_uri, "state": self.state,
            "code_challenge": _base64url(hashlib.sha256(self.code_verifier.encode("ascii")).digest()),
            "code_challenge_method": "S256", "scope": config.scopes,
        }
        separator = "&" if urlsplit(config.authorize_url).query else "?"
        self.authorization_url = config.authorize_url + separator + urlencode(parameters)

    def wait_for_code(self, cancel: threading.Event, timeout: float = 180) -> str:
        deadline = time.monotonic() + timeout
        try:
            while not self._code and not self._error:
                if cancel.is_set() or self._closed:
                    raise CloudError("Login cancelado.")
                if time.monotonic() >= deadline:
                    raise CloudError("O tempo para entrar terminou. Tente novamente.")
                try:
                    self._server.handle_request()
                except (OSError, ValueError):
                    if cancel.is_set() or self._closed:
                        raise CloudError("Login cancelado.") from None
                    raise CloudError("Não foi possível receber a autorização local.") from None
            if cancel.is_set():
                raise CloudError("Login cancelado.")
            if self._error:
                raise CloudError(self._error)
            return self._code
        finally:
            self.close()

    def close(self):
        self._closed = True
        self._server.server_close()


@dataclass(frozen=True)
class CloudChannel:
    index: int
    name: str


@dataclass(frozen=True)
class CloudDevice:
    id: str
    name: str
    serial: str
    model: str
    host: str | None
    username: str
    channels: tuple[CloudChannel, ...]


def _text(value, max_length=512) -> str:
    return value[:max_length] if isinstance(value, str) else ""


def cloud_host(value) -> str | None:
    """Read cloud DDNS addresses without using web/service ports as RTSP ports."""
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()
    if len(candidate) > 2048 or any(c.isspace() for c in candidate):
        return None
    try:
        try:
            host = normalized_host(candidate)
        except ValueError:
            parsed = urlsplit(candidate if "://" in candidate else "//" + candidate)
            if (parsed.scheme not in ("", "http", "https", "rtsp", "rtsps")
                    or parsed.username is not None or parsed.password is not None
                    or parsed.path not in ("", "/") or parsed.query or parsed.fragment
                    or not parsed.hostname):
                return None
            if parsed.port is not None and not 1 <= parsed.port <= 65535:
                return None
            host = normalized_host(parsed.hostname)
        try:
            if ipaddress.ip_address(host).is_unspecified:
                return None
        except ValueError:
            pass
        return host
    except ValueError:
        return None


def parse_cloud_device(record: dict, source="simnext") -> CloudDevice:
    if not isinstance(record, dict) or not isinstance(record.get("data"), dict):
        raise CloudError("O catálogo retornou um dispositivo em formato desconhecido.")
    data = record["data"]
    def identifier(value):
        if type(value) is int and value >= 0:
            return str(value)
        if isinstance(value, str) and value.strip() and len(value) <= 512:
            return value.strip()
        return ""

    device_id = identifier(record.get("device_id"))
    if source == "isic":
        device_id = (device_id or identifier(record.get("id"))
                     or identifier(data.get("device_id")) or identifier(data.get("id")))
        if not device_id:
            serial = identifier(record.get("serial_number")) or identifier(data.get("serial_number"))
            if serial:
                device_id = "serial:" + serial
        if device_id:
            device_id = "isic:" + device_id
    if not device_id:
        raise CloudError("O catálogo retornou um dispositivo sem identificador.")
    host = next((selected for value in (data.get("ddns"), data.get("main_ip"), data.get("ipv4"))
                 if (selected := cloud_host(value)) is not None), None)
    channels = []
    seen = set()
    raw_channels = data.get("channels")
    if raw_channels is None:
        raw_channels = []
    if not isinstance(raw_channels, list):
        raise CloudError("O catálogo retornou canais em formato desconhecido.")
    for channel in raw_channels:
        if not isinstance(channel, dict) or type(channel.get("index")) is not int or channel["index"] < 0 or channel["index"] > 65535:
            raise CloudError("O catálogo retornou um canal inválido.")
        index = channel["index"]
        if index not in seen:
            channels.append(CloudChannel(index, _text(channel.get("name"), 120) or f"Índice {index}"))
            seen.add(index)
    # Credenciais de equipamentos não são copiadas, persistidas ou decifradas.
    return CloudDevice(device_id, _text(data.get("name"), 120) or _text(record.get("serial_number"), 120) or "Dispositivo",
                       _text(record.get("serial_number")), _text(data.get("model")), host,
                       _text(data.get("username"), 256), tuple(channels))


def local_devices(record: CloudDevice, host_override: str | None = None) -> list[Device]:
    host = host_override or record.host
    if not host and not record.serial:
        raise CloudError("O dispositivo não possui endereço direto nem serial Intelbras Cloud.")
    try:
        host = normalized_host(host) if host else ""
        # O índice do catálogo não foi demonstrado como número RTSP. Importar
        # um cadastro por equipamento; o operador confirma porta/canal depois.
        identity = str(uuid5(NAMESPACE_URL, f"intelbras-account:{record.id}"))
        device = Device(identity, record.name[:120], host, channel=1, username=record.username or "admin",
                        serial=record.serial, connection="rtsp" if host else "cloud")
        device.validate()
        return [device]
    except ValueError:
        raise CloudError("O endereço ou cadastro desse dispositivo não pode ser usado na rede local.") from None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, _req, _fp, _code, _msg, _headers, _newurl):
        return None


class _HttpFailure(CloudError):
    def __init__(self, status: int):
        self.status = status
        message = "O serviço da Conta Intelbras não aceitou a solicitação."
        if status == 401:
            message = "A sessão expirou. Entre na Conta Intelbras novamente."
        elif status == 403:
            message = "Esta conta ou aplicativo não tem permissão para acessar o catálogo."
        elif status == 429:
            message = "O serviço recebeu muitas solicitações. Aguarde e tente novamente."
        super().__init__(message)


@dataclass(repr=False)
class _Session:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at: float


class CloudClient:
    def __init__(self, config: CloudConfig | None = None, installation_id: str | None = None, schema_callback=None):
        self.config = config or default_config()
        self.config.validate()
        self._installation_id = installation_id or base64.b64encode(hashlib.sha256(secrets.token_bytes(32)).digest()).decode("ascii")
        if any(c.isspace() for c in self._installation_id):
            raise CloudError("Identificador local de instalação inválido.")
        self._session: _Session | None = None
        self._cancel_event: threading.Event | None = None
        self._opener = build_opener(_NoRedirect())
        self.schema_callback = schema_callback
        self._device_passwords = {}

    def set_cancel_event(self, event: threading.Event | None):
        self._cancel_event = event

    def _check_cancelled(self):
        if self._cancel_event and self._cancel_event.is_set():
            raise CloudError("Operação cancelada.")

    @property
    def authenticated(self) -> bool:
        return self._session is not None

    def create_login(self) -> LoginAttempt:
        return LoginAttempt(self.config)

    def _request(self, method: str, url: str, headers: dict | None = None, form: dict | None = None, *, raw=False):
        self._check_cancelled()
        body = urlencode(form).encode("ascii") if form is not None else None
        request_headers = {"Accept": "application/json", "User-Agent": "MonitorLinux/0.12.0"}
        request_headers.update(headers or {})
        if form is not None:
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = Request(url, data=body, headers=request_headers, method=method)
        try:
            with self._opener.open(request, timeout=20) as response:
                content = response.read(16 * 1024 * 1024 + 1)
        except HTTPError as error:
            status = error.code
            error.close()
            raise _HttpFailure(status) from None
        except (OSError, URLError, ValueError):
            raise CloudError("Não foi possível acessar a Conta Intelbras. Verifique internet, relógio e certificados do sistema.") from None
        self._check_cancelled()
        if len(content) > 16 * 1024 * 1024:
            raise CloudError("A resposta do catálogo excedeu o limite de leitura.")
        if raw:
            return content
        try:
            result = json.loads(content)
        except (ValueError, UnicodeError):
            raise CloudError("O serviço retornou uma resposta em formato desconhecido.") from None
        if not isinstance(result, dict):
            raise CloudError("O serviço retornou uma resposta em formato desconhecido.")
        return result

    def _set_tokens(self, payload: dict):
        access = payload.get("access_token")
        refresh = payload.get("refresh_token") or (self._session.refresh_token if self._session else "")
        expiration = payload.get("expires_in", 3600)
        if not isinstance(access, str) or not access or len(access) > 32768 or any(c.isspace() for c in access):
            raise CloudError("O serviço não retornou uma sessão válida.")
        if not isinstance(refresh, str) or len(refresh) > 32768 or any(c.isspace() for c in refresh):
            raise CloudError("O serviço retornou uma renovação inválida.")
        if type(expiration) not in (int, float) or not 0 < expiration <= 365 * 86400:
            raise CloudError("O serviço retornou uma validade de sessão inválida.")
        self._session = _Session(access, refresh, time.monotonic() + expiration)

    def _api_url(self, path: str, api_base: str | None = None) -> str:
        api_base = api_base or self.config.api_base
        target = urljoin(api_base.rstrip("/") + "/", path)
        base = urlsplit(api_base)
        parsed = urlsplit(target)
        if (parsed.scheme, parsed.hostname, parsed.port) != (base.scheme, base.hostname, base.port) or parsed.username or parsed.password or parsed.fragment:
            raise CloudError("O catálogo retornou um destino não autorizado.")
        return target

    def _headers(self) -> dict:
        if not self._session:
            raise CloudError("Entre na Conta Intelbras para consultar os dispositivos.")
        return {self.config.token_header: self.config.token_prefix + self._session.access_token,
                self.config.installation_header: self._installation_id,
                self.config.version_header: self.config.api_version,
                "Content-Type": "application/json;charset=utf-8"}

    def finish_login(self, attempt: LoginAttempt, code: str):
        if attempt._used or code != attempt._code or not attempt.code_verifier:
            raise CloudError("A autorização não é válida para esta tentativa de login.")
        attempt._used = True
        self.logout()
        try:
            tokens = self._request("POST", self.config.token_url, form={
                "grant_type": "authorization_code", "code": code,
                "redirect_uri": attempt.redirect_uri, "code_verifier": attempt.code_verifier,
                "client_id": self.config.client_id,
            })
            self._set_tokens(tokens)
            self._prepare_backend()
        except CloudError:
            self.logout()
            raise
        finally:
            attempt.code_verifier = ""
            attempt._code = None
            attempt.close()

    def _prepare_backend(self):
        # Só habilitar paths cuja finalidade foi recuperada e registrada.
        if self.config.login_path:
            self._request("POST", self._api_url(self.config.login_path), self._headers(), raw=True)
        if self.config.validate_path:
            reply = self._request("POST", self._api_url(self.config.validate_path), self._headers(), raw=True)
            valid = reply.strip() in (b"OK", b'"OK"')
            if not valid:
                try:
                    value = json.loads(reply)
                    valid = isinstance(value, dict) and any(value.get(key) == "OK" for key in ("detail", "status", "message"))
                except (ValueError, UnicodeError):
                    pass
            if not valid:
                raise CloudError("A Conta Intelbras não validou o vínculo desta sessão.")

    def _refresh(self):
        if not self._session or not self._session.refresh_token:
            self.logout()
            raise CloudError("A sessão expirou. Entre na Conta Intelbras novamente.")
        headers = {}
        if self.config.refresh_installation_header:
            headers[self.config.refresh_installation_header] = self._installation_id
        try:
            tokens = self._request("POST", self.config.token_url, headers,
                                   {"grant_type": "refresh_token", "refresh_token": self._session.refresh_token, "client_id": self.config.client_id})
            self._set_tokens(tokens)
            self._prepare_backend()
        except CloudError:
            self.logout()
            raise

    def _authorized_get(self, path: str, api_base: str | None = None, *, isic=False) -> dict:
        if not self._session:
            raise CloudError("Entre na Conta Intelbras para consultar os dispositivos.")
        if time.monotonic() >= self._session.expires_at - 30:
            self._refresh()
        target = self._api_url(path, api_base)
        def headers():
            result = self._headers()
            if isic:
                result.pop(self.config.version_header, None)
            return result
        try:
            return self._request("GET", target, headers())
        except _HttpFailure as error:
            if error.status != 401:
                raise
        self._refresh()
        try:
            return self._request("GET", target, headers())
        except _HttpFailure as error:
            if error.status == 401:
                self.logout()
            raise

    def list_devices(self, source="simnext") -> list[CloudDevice]:
        if source not in ("simnext", "isic"):
            raise CloudError("Origem do catálogo inválida.")
        isic = source == "isic"
        api_base = self.config.isic_api_base if isic else self.config.api_base
        if not api_base:
            raise CloudError("Catálogo iSIC Lite não configurado.")
        if isic:
            current = self.config.isic_devices_path
        else:
            capabilities = self._authorized_get(self.config.capabilities_path)
            page_size = capabilities.get("simnext_api_max_page_size")
            if type(page_size) is not int or not 1 <= page_size <= 10000:
                raise CloudError("O serviço retornou limites de catálogo inválidos.")
            current = self.config.devices_path.format(page_size=page_size)
        seen = set()
        records = {}
        passwords = {}
        for _ in range(1000):
            url = self._api_url(current, api_base)
            if url in seen:
                raise CloudError("O serviço repetiu uma página do catálogo.")
            seen.add(url)
            payload = self._authorized_get(url, api_base, isic=isic)
            results = payload.get("results")
            if not isinstance(results, list):
                raise CloudError("O catálogo de dispositivos tem um formato desconhecido.")
            for record in results:
                if self.schema_callback and isinstance(record, dict):
                    # Only structural metadata. Never pass field values or credentials.
                    schema = {"source": source, "fields": {str(k)[:64]: type(v).__name__ for k, v in list(record.items())[:64]}}
                    data = record.get("data")
                    if isinstance(data, dict):
                        schema["data_fields"] = {str(k)[:64]: type(v).__name__ for k, v in list(data.items())[:64]}
                    try:
                        self.schema_callback(schema)
                    except OSError:
                        pass
                device = parse_cloud_device(record, source=source)
                records[device.id] = device
                password = record["data"].get("password")
                if isinstance(password, str) and len(password) <= 512 and not any(ord(c) < 32 for c in password):
                    identity = str(uuid5(NAMESPACE_URL, f"intelbras-account:{device.id}"))
                    passwords[identity] = password
            next_url = payload.get("next")
            if not next_url:
                self._device_passwords.update(passwords)
                return list(records.values())
            if not isinstance(next_url, str):
                raise CloudError("A próxima página do catálogo tem um formato desconhecido.")
            current = urljoin(url, next_url)
        raise CloudError("O catálogo excedeu o limite de páginas. Nenhum resultado parcial foi importado.")

    def logout(self):
        self._session = None
        self._device_passwords.clear()

    def device_password(self, identity: str) -> str | None:
        """Device credentials remain in the authenticated session only."""
        return self._device_passwords.get(identity)

"""Cadastro e URLs RTSP independentes de interface e sistema operacional."""

from dataclasses import dataclass
import ipaddress
import re
from urllib.parse import quote, urlsplit


def normalized_host(host: str) -> str:
    value = host.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    if not value or any(c.isspace() for c in value) or any(c in value for c in "/@?#%\\"):
        raise ValueError("Informe somente o IP ou nome do equipamento, sem URL ou porta.")
    try:
        return ipaddress.ip_address(value).compressed
    except ValueError:
        if ":" in value or re.fullmatch(r"[\d.]+", value):
            raise ValueError("O endereço IP é inválido.") from None
    try:
        ascii_name = value.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ValueError("O nome do equipamento é inválido.") from None
    if len(ascii_name) > 253 or not all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in ascii_name.split(".")
    ):
        raise ValueError("O nome do equipamento é inválido.")
    return ascii_name


@dataclass(frozen=True)
class Device:
    id: str
    name: str
    host: str
    port: int = 554
    channel: int = 1
    subtype: int = 1
    username: str = "admin"
    path: str = "/cam/realmonitor"
    serial: str = ""
    connection: str = "rtsp"

    def validate(self) -> None:
        if not isinstance(self.id, str) or not self.id or len(self.id) > 128:
            raise ValueError("Identificador do dispositivo inválido.")
        if not self.name.strip() or len(self.name) > 120:
            raise ValueError("Informe um nome com até 120 caracteres.")
        if self.connection not in ("rtsp", "cloud"):
            raise ValueError("Tipo de conexão inválido.")
        if self.serial and not re.fullmatch(r"[A-Za-z0-9-]{1,128}", self.serial):
            raise ValueError("Serial Intelbras Cloud inválido.")
        if self.connection == "cloud":
            if not self.serial:
                raise ValueError("Informe o serial Intelbras Cloud.")
            if self.host:
                normalized_host(self.host)
        else:
            normalized_host(self.host)
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("A porta RTSP deve estar entre 1 e 65535.")
        if type(self.channel) is not int or not 1 <= self.channel <= 65535:
            raise ValueError("O canal deve estar entre 1 e 65535.")
        if type(self.subtype) is not int or self.subtype not in (0, 1):
            raise ValueError("Selecione o stream principal ou extra.")
        if len(self.username) > 256 or any(ord(c) < 32 for c in self.username):
            raise ValueError("O usuário contém caracteres inválidos.")
        if not self.path.startswith("/") or self.path.startswith("//") or len(self.path) > 2048:
            raise ValueError("O caminho RTSP deve começar com uma barra, por exemplo /cam/realmonitor.")
        if any(c.isspace() or ord(c) < 32 for c in self.path) or "#" in self.path or "\\" in self.path:
            raise ValueError("O caminho RTSP contém caracteres inválidos.")
        if re.search(r"%(?![0-9a-fA-F]{2})", self.path):
            raise ValueError("O caminho RTSP contém escape percentual inválido.")


def build_rtsp_url(device: Device, password: str) -> str:
    device.validate()
    if device.connection == "cloud":
        raise ValueError("Este dispositivo exige transporte Intelbras Cloud/P2P.")
    if any(ord(c) < 32 for c in password):
        raise ValueError("A senha contém caracteres de controle.")
    host = normalized_host(device.host)
    if ":" in host:
        host = f"[{host}]"
    auth = ""
    if device.username:
        auth = f"{quote(device.username, safe='')}:{quote(password, safe='')}@"
    path = urlsplit(device.path)
    encoded_path = quote(path.path, safe="/%-._~")
    if path.path == "/cam/realmonitor" and not path.query:
        query = f"channel={device.channel}&subtype={device.subtype}"
    else:
        query = path.query
    suffix = f"?{query}" if query else ""
    return f"rtsp://{auth}{host}:{device.port}{encoded_path}{suffix}"

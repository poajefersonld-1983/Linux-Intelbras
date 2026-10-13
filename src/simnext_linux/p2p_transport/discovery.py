"""Bootstrap and registration messages for the legacy cloud wire format.

No application keys, device credentials or service endpoints are built in.
"""

import ipaddress
import secrets
import struct
import time
from dataclasses import dataclass

from . import tea

MAGIC = 0x2012


class ProtocolError(ValueError):
    """A peer returned an invalid or unexpected datagram."""


@dataclass(frozen=True)
class Server:
    address: str
    port: int
    hostname: str = ""
    ipv6_hostname: str = ""


@dataclass(frozen=True, repr=False)
class Observation:
    address: str
    port: int
    echoed_seconds: int
    echoed_microseconds: int


def header(packet: bytes, expected_type: int) -> None:
    if len(packet) < 4 or struct.unpack_from("<HH", packet) != (MAGIC, expected_type):
        raise ProtocolError("Unexpected cloud message header")


def discovery_request(now: int | None = None) -> bytes:
    return struct.pack(
        "<HHIHHI", MAGIC, 5001, int(time.time()) if now is None else now, 1, 50, 1
    )


def discovery_response(packet: bytes) -> tuple[Server, ...]:
    header(packet, 5002)
    if len(packet) < 20:
        raise ProtocolError("Truncated discovery response")
    length = struct.unpack_from("<I", packet, 16)[0]
    if length != len(packet) - 20 or length % 8:
        raise ProtocolError("Invalid discovery payload length")
    try:
        text = tea.decrypt(packet[20:], packet[:16]).split(b"\0", 1)[0].decode("ascii")
        servers = []
        for line in text.splitlines():
            endpoint, hostname, ipv6_hostname = line.split("|")
            address, port_text = endpoint.rsplit(":", 1)
            ipaddress.IPv4Address(address)
            port = int(port_text)
            if not 1 <= port <= 65535:
                raise ValueError("Invalid port")
            servers.append(Server(address, port, hostname, ipv6_hostname))
        if not servers:
            raise ValueError("Empty server list")
        return tuple(servers)
    except (ValueError, UnicodeError) as exc:
        raise ProtocolError("Invalid discovery payload") from exc


def heartbeat_request() -> bytes:
    nanoseconds = time.time_ns()
    return struct.pack(
        "<HHII",
        MAGIC,
        1000,
        nanoseconds // 1_000_000_000,
        nanoseconds // 1000 % 1_000_000,
    )


def heartbeat_response(packet: bytes) -> Observation:
    header(packet, 1001)
    if len(packet) < 32:
        raise ProtocolError("Truncated heartbeat response")
    try:
        address = packet[4:20].split(b"\0", 1)[0].decode("ascii")
        ipaddress.IPv4Address(address)
    except (ValueError, UnicodeError) as exc:
        raise ProtocolError("Invalid observed address") from exc
    port = struct.unpack_from("<H", packet, 20)[0]
    if not port:
        raise ProtocolError("Invalid observed port")
    seconds, micros = struct.unpack_from("<II", packet, 24)
    return Observation(address, port, seconds, micros)


def registration_request(
    application_key: str, client_id: str, nat_type: int = 6
) -> bytes:
    key = application_key.encode("ascii")
    identity = client_id.encode("ascii")
    if not key or len(key) > 31 or not identity or len(identity) > 99:
        raise ValueError("Invalid key or client identity length")
    prefix = struct.pack(
        "<HHIII",
        MAGIC,
        1012,
        int(time.time()),
        secrets.randbits(32),
        secrets.randbits(32),
    )
    return (
        prefix
        + tea.encrypt(key.ljust(32, b"\0"), prefix)
        + identity.ljust(100, b"\0")
        + struct.pack("<III", 127, nat_type, 2)
    )


def registration_response(
    packet: bytes, application_key: str, *, now: int | None = None
) -> int:
    header(packet, 1013)
    if len(packet) < 52:
        raise ProtocolError("Truncated registration response")
    accepted, interval, timestamp = struct.unpack_from("<III", packet, 4)
    if not accepted:
        raise ProtocolError("Application registration rejected")
    if abs(timestamp - (int(time.time()) if now is None else now)) > 10:
        raise ProtocolError("Stale registration response")
    returned_key = tea.decrypt(packet[16:48], packet[:16]).split(b"\0", 1)[0]
    if not secrets.compare_digest(returned_key, application_key.encode("ascii")):
        raise ProtocolError("Registration key mismatch")
    if not 1 <= interval <= 3600:
        raise ProtocolError("Invalid heartbeat interval")
    return interval

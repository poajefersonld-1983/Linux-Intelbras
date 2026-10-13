"""Peer negotiation, reliable datagrams and tunneled stream framing."""

import hashlib
import ipaddress
import struct
import time
from dataclasses import dataclass

from .discovery import MAGIC, Observation, ProtocolError, header

VDT_PREFIX = b"\x12\x01\x10"
MAX_FRAME = 1_048_576
MAX_PAYLOAD = 1200
WINDOW = 64


def _text(value: str, length: int) -> bytes:
    encoded = value.encode("ascii")
    if b"\0" in encoded or len(encoded) >= length:
        raise ValueError("Invalid wire string")
    return encoded.ljust(length, b"\0")


def connection_request(
    identity: str,
    serial: str,
    password: str,
    observed: Observation,
    local_address: str,
    local_port: int,
) -> bytes:
    packet = bytearray(588)
    struct.pack_into("<HHI", packet, 0, MAGIC, 1004, 1)
    packet[8:108] = _text(identity, 100)
    packet[108:208] = _text(serial, 100)
    packet[208:224] = _text(observed.address, 16)
    struct.pack_into("<HH", packet, 224, observed.port, local_port)
    packet[228:328] = _text(local_address, 100)
    struct.pack_into("<III", packet, 352, int(time.time()), 127, 6)
    packet[468:504] = _text(
        hashlib.md5(password.encode("ascii"), usedforsecurity=False).hexdigest(), 36
    )
    return bytes(packet)


def peer_endpoints(
    packet: bytes, identity: str, serial: str
) -> tuple[tuple[str, int], ...]:
    header(packet, 1004)
    if (
        len(packet) != 588
        or packet[8:108].split(b"\0", 1)[0] != serial.encode()
        or packet[108:208].split(b"\0", 1)[0] != identity.encode()
    ):
        raise ProtocolError("Connection offer does not match this session")
    result = []
    for address_offset, port_offset in ((208, 224), (332, 348)):
        try:
            address = (
                packet[address_offset : address_offset + 16]
                .split(b"\0", 1)[0]
                .decode("ascii")
            )
            port = struct.unpack_from("<H", packet, port_offset)[0]
            if not address and not port:
                continue
            ip = ipaddress.IPv4Address(address)
            if not port or ip.is_unspecified or ip.is_multicast:
                continue
            result.append((address, port))
        except (ValueError, UnicodeError) as exc:
            raise ProtocolError("Invalid offered peer endpoint") from exc
    if not result:
        raise ProtocolError("Peer offered no usable IPv4 endpoint")
    return tuple(dict.fromkeys(result))


def peer_hello(identity: str, endpoint: tuple[str, int], sequence: int = 0, *, connection_type: int = 2) -> bytes:
    return (
        struct.pack("<HHI", MAGIC, 2000, 1)
        + _text(identity, 100)
        + struct.pack("<IIII", sequence, 127, 1, connection_type)
        + _text(f"{endpoint[0]}:{endpoint[1]}", 60)
    )


def relay_offer(packet: bytes, identity: str, serial: str):
    """Read the proxy endpoint only from a validated connection offer."""
    endpoints = peer_endpoints(packet, identity, serial)
    address = packet[332:348].split(b"\0", 1)[0].decode("ascii")
    port = struct.unpack_from("<H", packet, 348)[0]
    endpoint = (address, port)
    if not address and not port:
        return None
    if endpoint not in endpoints:
        raise ProtocolError("Invalid relay endpoint")
    return endpoint, struct.unpack_from("<I", packet, 4)[0]


def relay_registration(identity: str, serial: str, local_id: int, remote_id: int) -> bytes:
    if not 0 <= local_id <= 0xFFFFFFFF or not 0 <= remote_id <= 0xFFFFFFFF:
        raise ValueError("Invalid relay session identifiers")
    return (struct.pack("<HH", 0x2012, 3001) + _text(identity, 100)
            + struct.pack("<I", local_id) + _text(serial, 100)
            + struct.pack("<I", remote_id))


def peer_hello_reply(packet: bytes, identity: str, serial: str) -> bytes:
    header(packet, 2000)
    if len(packet) < 120 or packet[8:108].split(b"\0", 1)[0] != serial.encode():
        raise ProtocolError("Unexpected peer identity")
    if struct.unpack_from("<I", packet, 116)[0] != 1:
        raise ProtocolError("Peer does not offer VDT transport")
    reply = bytearray(packet)
    struct.pack_into("<HHI", reply, 0, MAGIC, 2001, 1)
    reply[8:108] = _text(identity, 100)
    struct.pack_into("<II", reply, 112, 127, 0)
    return bytes(reply)


def vdt_hello(sequence: int = 0, free_slots: int = WINDOW) -> bytes:
    now = time.time_ns()
    return (
        VDT_PREFIX
        + b"\0"
        + struct.pack(
            "!IIIHH",
            now // 1_000_000_000,
            now // 1000 % 1_000_000,
            sequence,
            WINDOW,
            free_slots,
        )
    )


def vdt_data(sequence: int, payload: bytes, retry: int = 0) -> bytes:
    if (
        not 1 <= sequence <= 0xFFFFFFFF
        or not 0 < len(payload) <= MAX_PAYLOAD
        or not 0 <= retry <= 255
    ):
        raise ValueError("Invalid reliable datagram")
    return (
        VDT_PREFIX
        + b"\x02"
        + struct.pack("!IHBBBBH", sequence, len(payload), 1, retry, 1, 0, 0)
        + payload
    )


def vdt_ack(
    highest: int, missing: tuple[int, ...] = (), free_slots: int = WINDOW
) -> bytes:
    return (
        VDT_PREFIX
        + b"\x03"
        + struct.pack("!HHI4x", free_slots, len(missing), highest)
        + b"".join(struct.pack("!I", sequence) for sequence in missing)
    )


def application_frame(client: int, kind: int, payload: bytes) -> bytes:
    if len(payload) > MAX_FRAME:
        raise ValueError("Application frame too large")
    return struct.pack("<HHI", client, kind, len(payload)) + payload


def open_stream_frame(client: int, port: int) -> bytes:
    if not 1 <= client < 65535 or not 1 <= port <= 65535:
        raise ValueError("Invalid tunneled stream")
    payload = (
        struct.pack("!H", client) + _text("127.0.0.1", 20) + struct.pack("!H", port)
    )
    return application_frame(65535, 0xFF01, payload)


class FrameDecoder:
    def __init__(self) -> None:
        self.buffer = bytearray()

    def feed(self, payload: bytes) -> list[tuple[int, int, bytes]]:
        self.buffer.extend(payload)
        result = []
        while len(self.buffer) >= 8:
            client, kind, length = struct.unpack_from("<HHI", self.buffer)
            if length > MAX_FRAME:
                raise ProtocolError("Application frame exceeds the buffer limit")
            if len(self.buffer) < length + 8:
                break
            result.append((client, kind, bytes(self.buffer[8 : 8 + length])))
            del self.buffer[: 8 + length]
        return result


@dataclass
class ReceiveWindow:
    """Order packets once and explicitly report gaps for selective retransmission."""

    next_sequence: int = 1

    def __post_init__(self) -> None:
        self.pending: dict[int, bytes] = {}

    def accept(self, packet: bytes) -> tuple[bytes, bytes]:
        if len(packet) < 16 or packet[:4] != VDT_PREFIX + b"\x02":
            raise ProtocolError("Invalid reliable data header")
        sequence, length = struct.unpack_from("!IH", packet, 4)
        if not sequence or length != len(packet) - 16 or length > 1400:
            raise ProtocolError("Invalid reliable data length")
        if sequence >= self.next_sequence + WINDOW:
            raise ProtocolError("Reliable packet exceeds the receive window")
        if sequence >= self.next_sequence:
            self.pending.setdefault(sequence, packet[16:])
        ordered = bytearray()
        while self.next_sequence in self.pending:
            ordered.extend(self.pending.pop(self.next_sequence))
            self.next_sequence += 1
        highest = max(self.pending, default=self.next_sequence - 1)
        missing = tuple(
            seq for seq in range(self.next_sequence, highest) if seq not in self.pending
        )
        return bytes(ordered), vdt_ack(highest, missing, WINDOW - len(self.pending))

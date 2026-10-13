"""Experimental IPv4 P2P client with a bounded, reliable byte stream.

This implementation uses Python's standard library only. Service configuration
and credentials must be supplied by the caller. It never loads vendor binaries.
"""

from __future__ import annotations

import queue
import select
import socket
import struct
import threading
import time
import uuid
from collections import Counter, deque
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Self

from .discovery import (
    ProtocolError,
    Server,
    discovery_request,
    discovery_response,
    heartbeat_request,
    heartbeat_response,
    registration_request,
    registration_response,
)
from .wire import (
    MAX_PAYLOAD,
    VDT_PREFIX,
    WINDOW,
    FrameDecoder,
    ReceiveWindow,
    application_frame,
    connection_request,
    open_stream_frame,
    peer_endpoints,
    peer_hello,
    peer_hello_reply,
    relay_offer,
    relay_registration,
    vdt_data,
    vdt_hello,
)


@dataclass(frozen=True)
class Configuration:
    server: str
    server_port: int
    application_key: str = field(repr=False)
    p2p_password: str = field(repr=False)
    serial: str = field(repr=False)
    remote_port: int = 554


class TransportError(ConnectionError):
    """A bounded operation failed or the session closed."""


class Stream:
    def __init__(self, transport: Transport, client_id: int):
        self.transport = transport
        self.client_id = client_id
        self.incoming: queue.Queue[bytes | None] = queue.Queue(maxsize=128)
        self.closed = False

    def send(self, payload: bytes) -> None:
        if self.closed:
            raise TransportError("Stream is closed")
        self.transport._send_frame(application_frame(self.client_id, 0, payload))

    def receive(self, timeout: float = 15) -> bytes:
        try:
            payload = self.incoming.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError("Tunneled stream idle timeout") from exc
        return b"" if payload is None else payload

    def _finish(self) -> None:
        self.closed = True
        # Wake a reader even when its consumer stopped draining the queue.
        try:
            self.incoming.put_nowait(None)
        except queue.Full:
            self.incoming.get_nowait()
            self.incoming.put_nowait(None)

    def close(self) -> None:
        if not self.closed:
            self._finish()
            with self.transport._condition:
                self.transport._streams.pop(self.client_id, None)
            if self.transport.alive:
                self.transport._send_frame(
                    application_frame(
                        65535,
                        0xFF02,
                        struct.pack("!H", self.client_id).ljust(24, b"\0"),
                    )
                )


class Transport:
    def __init__(self, configuration: Configuration):
        self.configuration = configuration
        self.identity = uuid.uuid4().hex
        self.metrics: Counter[str] = Counter()
        self._main: socket.socket | None = None
        self._socket: socket.socket | None = None
        self._server: tuple[str, int] | None = None
        self._peer: tuple[str, int] | None = None
        self._relays = {}
        self._relay_registration = None
        self._last_relay_registration = 0.0
        self._condition = threading.Condition()
        self._outgoing: deque[bytes] = deque()
        self._pending: dict[int, tuple[bytes, float, int]] = {}
        self._streams: dict[int, Stream] = {}
        self._next_client = 1
        self._next_packet = 1
        self._receive = ReceiveWindow()
        self._decoder = FrameDecoder()
        self._peer_window = 1
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self.failure: str | None = None

    @property
    def alive(self) -> bool:
        return (
            self._worker is not None
            and self._worker.is_alive()
            and not self._stop.is_set()
        )

    @staticmethod
    def _socket_new() -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", 0))
        return sock

    @staticmethod
    def _exchange(
        sock: socket.socket,
        endpoint: tuple[str, int],
        request: bytes,
        kind: int,
        timeout: float,
    ) -> bytes:
        deadline = time.monotonic() + timeout
        for _ in range(3):
            sock.sendto(request, endpoint)
            retry_until = min(deadline, time.monotonic() + timeout / 3)
            while time.monotonic() < retry_until:
                sock.settimeout(max(0.001, retry_until - time.monotonic()))
                try:
                    packet, sender = sock.recvfrom(65535)
                except TimeoutError:
                    break
                if (
                    sender == endpoint
                    and len(packet) >= 4
                    and struct.unpack_from("<HH", packet) == (0x2012, kind)
                ):
                    return packet
        raise TransportError("Cloud exchange timed out")

    def _discover(self) -> tuple[Server, ...]:
        """Try alternate DNS addresses when a bootstrap endpoint is unavailable."""
        config = self.configuration
        addresses = tuple(
            dict.fromkeys(
                result[4][0]
                for result in socket.getaddrinfo(
                    config.server, config.server_port, socket.AF_INET, socket.SOCK_DGRAM
                )
            )
        )[:8]
        self.metrics["bootstrap_endpoints"] = len(addresses)
        for address in addresses:
            if self._stop.is_set():
                raise TransportError("Connection cancelled")
            self.metrics["bootstrap_attempts"] += 1
            try:
                packet = self._exchange(
                    self._main,
                    (address, config.server_port),
                    discovery_request(),
                    5002,
                    2,
                )
                return discovery_response(packet)
            except (TransportError, ProtocolError):
                continue
        raise TransportError("No Cloud discovery endpoint responded")

    def connect(self, timeout: float = 15) -> None:
        if self._main is not None or self._stop.is_set():
            raise TransportError("Create a new transport for each session")
        try:
            config = self.configuration
            self._main = self._socket_new()
            servers = self._discover()
            self.metrics["discovered_servers"] = len(servers)
            for server in servers[:3]:
                if self._stop.is_set():
                    raise TransportError("Connection cancelled")
                endpoint = (server.address, server.port)
                try:
                    heartbeat_response(
                        self._exchange(
                            self._main, endpoint, heartbeat_request(), 1001, 3
                        )
                    )
                    reply = self._exchange(
                        self._main,
                        endpoint,
                        registration_request(config.application_key, self.identity),
                        1013,
                        3,
                    )
                    self._interval = registration_response(
                        reply, config.application_key
                    )
                    self._server = endpoint
                    break
                except (TransportError, ProtocolError):
                    continue
            if self._server is None:
                raise TransportError("No server accepted application registration")
            self.metrics["verified_registrations"] += 1
            if self._stop.is_set():
                raise TransportError("Connection cancelled")
            self._socket = self._socket_new()
            observed = heartbeat_response(
                self._exchange(self._socket, self._server, heartbeat_request(), 1001, 3)
            )
            route = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                route.connect(self._server)
                local_address = route.getsockname()[0]
            finally:
                route.close()
            request = connection_request(
                self.identity,
                config.serial,
                config.p2p_password,
                observed,
                local_address,
                self._socket.getsockname()[1],
            )
            self._negotiate(request, timeout)
            self._socket.setblocking(False)
            self._main.setblocking(False)
            self._last_peer = time.monotonic()
            self._last_hello = 0.0
            self._last_registration = time.monotonic()
            self._worker = threading.Thread(
                target=self._run, name="isic-transport", daemon=True
            )
            self._worker.start()
        except BaseException:
            self.close()
            raise

    def _negotiate(self, request: bytes, timeout: float) -> None:
        started = time.monotonic()
        deadline = started + timeout
        offers: set[tuple[str, int]] = set()
        next_send = 0.0
        selected = None
        while time.monotonic() < deadline and not self._stop.is_set():
            now = time.monotonic()
            if now >= next_send:
                if selected is None:
                    self._socket.sendto(request, self._server)
                    for endpoint in offers:
                        route = self._relays.get(endpoint)
                        if route is not None:
                            if now - started < 3:
                                continue
                            remote_id, registration = route
                            self._socket.sendto(registration, endpoint)
                            hello = peer_hello(self.identity, endpoint, remote_id, connection_type=3)
                        else:
                            hello = peer_hello(self.identity, endpoint)
                        self._socket.sendto(hello, endpoint)
                else:
                    self._socket.sendto(vdt_hello(), selected)
                next_send = now + 0.4
            readable, _, _ = select.select([self._main, self._socket], [], [], 0.1)
            for sock in readable:
                packet, sender = sock.recvfrom(65535)
                if len(packet) < 4:
                    continue
                magic, kind = struct.unpack_from("<HH", packet)
                if sender == self._server and magic == 0x2012 and kind == 1004:
                    self.metrics["device_offers"] += 1
                    offers.update(
                        peer_endpoints(packet, self.identity, self.configuration.serial)
                    )
                    self.metrics["peer_endpoints"] = len(offers)
                    route = relay_offer(packet, self.identity, self.configuration.serial)
                    if route is not None:
                        endpoint, remote_id = route
                        registration = relay_registration(self.identity, self.configuration.serial,
                                                          struct.unpack_from("<I", request, 4)[0], remote_id)
                        self._relays[endpoint] = (remote_id, registration)
                    continue
                if sock is not self._socket or sender not in offers:
                    continue
                if magic == 0x2012 and kind == 3002 and sender in self._relays:
                    self.metrics["relay_registrations"] += 1
                    continue
                if magic == 0x2012 and kind == 2000:
                    self.metrics["peer_hellos"] += 1
                    self._socket.sendto(
                        peer_hello_reply(
                            packet, self.identity, self.configuration.serial
                        ),
                        sender,
                    )
                    selected = sender
                    self._socket.sendto(vdt_hello(), sender)
                    continue
                if (
                    packet[:4] == VDT_PREFIX + b"\0"
                    and len(packet) == 20
                    and sender == selected
                ):
                    self._peer_window = max(
                        1, min(WINDOW, struct.unpack_from("!H", packet, 16)[0])
                    )
                    self._socket.sendto(packet[:3] + b"\x01" + packet[4:], sender)
                    self._peer = sender
                    if sender in self._relays:
                        self._relay_registration = self._relays[sender][1]
                        self.metrics["relay_sessions"] += 1
                    self.metrics["peer_sessions"] += 1
                    return
        raise TransportError("Device negotiation timed out")

    def open_stream(self) -> Stream:
        with self._condition:
            if not self.alive or len(self._streams) >= 8 or self._next_client >= 65535:
                raise TransportError("No stream is available")
            client_id = self._next_client
            self._next_client += 1
            stream = Stream(self, client_id)
            self._streams[client_id] = stream
        self._send_frame(open_stream_frame(client_id, self.configuration.remote_port))
        return stream

    def _send_frame(self, frame: bytes) -> None:
        parts = [
            frame[offset : offset + MAX_PAYLOAD]
            for offset in range(0, len(frame), MAX_PAYLOAD)
        ]
        deadline = time.monotonic() + 10
        with self._condition:
            while len(self._outgoing) + len(parts) > WINDOW * 2 and self.alive:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TransportError("Outgoing stream buffer timed out")
                self._condition.wait(remaining)
            if not self.alive:
                raise TransportError("Transport is closed")
            self._outgoing.extend(parts)

    def _send(self, packet: bytes) -> None:
        self._socket.sendto(packet, self._peer)
        self.metrics["datagrams_sent"] += 1

    def _handle(self, packet: bytes) -> None:
        if len(packet) < 4:
            return
        if packet[:3] != VDT_PREFIX:
            magic, kind = struct.unpack_from("<HH", packet)
            if (magic, kind) == (0x2012, 2000):
                self._send(
                    peer_hello_reply(packet, self.identity, self.configuration.serial)
                )
            return
        command = packet[3]
        self.metrics[f"received_command_{command}"] += 1
        if command == 0 and len(packet) == 20:
            self._peer_window = min(WINDOW, struct.unpack_from("!H", packet, 16)[0])
            self._send(packet[:3] + b"\x01" + packet[4:])
        elif command == 2:
            ordered, ack = self._receive.accept(packet)
            self._send(ack)
            self.metrics["stream_bytes_received"] += len(ordered)
            for client_id, kind, payload in self._decoder.feed(ordered):
                self.metrics[f"received_frame_{kind}"] += 1
                if kind == 0:
                    with self._condition:
                        stream = self._streams.get(client_id)
                    if stream is not None:
                        try:
                            stream.incoming.put_nowait(payload)
                        except queue.Full as exc:
                            raise TransportError(
                                "Incoming stream consumer is too slow"
                            ) from exc
                elif client_id == 65535 and kind == 0xFF02 and len(payload) >= 2:
                    client_id = struct.unpack_from("!H", payload)[0]
                    with self._condition:
                        stream = self._streams.pop(client_id, None)
                    if stream is not None:
                        stream._finish()
        elif command == 3:
            if len(packet) < 16:
                raise ProtocolError("Truncated acknowledgement")
            free_slots, count, highest = struct.unpack_from("!HHI", packet, 4)
            if (
                count > WINDOW
                or len(packet) != 16 + count * 4
                or highest >= self._next_packet
            ):
                raise ProtocolError("Invalid acknowledgement")
            missing = set(struct.unpack_from(f"!{count}I", packet, 16))
            self._peer_window = min(WINDOW, free_slots)
            for sequence in list(self._pending):
                if sequence <= highest:
                    if sequence in missing:
                        payload, _, retries = self._pending[sequence]
                        self._pending[sequence] = (payload, 0, retries)
                    else:
                        del self._pending[sequence]
            with self._condition:
                self._condition.notify_all()
        elif command == 4:
            if len(packet) >= 8:
                highest = struct.unpack_from("!I", packet, 4)[0]
                if highest < self._receive.next_sequence + WINDOW:
                    missing = tuple(
                        seq
                        for seq in range(self._receive.next_sequence, highest + 1)
                        if seq not in self._receive.pending
                    )
                    from .wire import vdt_ack

                    self._send(
                        vdt_ack(max(highest, self._receive.next_sequence - 1), missing)
                    )
        elif command == 5 and len(packet) >= 16:
            self._send(packet[:3] + b"\x06" + packet[4:16])

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if now - self._last_peer > 20:
                    raise TransportError("Peer stopped responding")
                if self._relay_registration is not None and now - self._last_relay_registration >= 1:
                    self._socket.sendto(self._relay_registration, self._peer)
                    self._last_relay_registration = now
                if now - self._last_hello >= 3:
                    self._send(vdt_hello())
                    self._last_hello = now
                if now - self._last_registration >= min(20, self._interval):
                    self._main.sendto(
                        registration_request(
                            self.configuration.application_key, self.identity
                        ),
                        self._server,
                    )
                    self._last_registration = now
                with self._condition:
                    while self._outgoing and len(self._pending) < min(
                        16, self._peer_window
                    ):
                        if self._next_packet >= 0xFFFFFFFF:
                            raise TransportError("Session packet sequence exhausted")
                        sequence = self._next_packet
                        self._next_packet += 1
                        payload = self._outgoing.popleft()
                        self._pending[sequence] = (payload, now, 0)
                        self._send(vdt_data(sequence, payload))
                        self.metrics["stream_bytes_sent"] += len(payload)
                    self._condition.notify_all()
                for sequence, (payload, sent, retries) in list(self._pending.items()):
                    if now - sent >= 0.4:
                        if retries >= 12:
                            raise TransportError(
                                "Reliable packet retransmission limit reached"
                            )
                        self._send(vdt_data(sequence, payload, retries + 1))
                        self._pending[sequence] = (payload, now, retries + 1)
                        self.metrics["retransmissions"] += 1
                readable, _, _ = select.select([self._main, self._socket], [], [], 0.03)
                for sock in readable:
                    packet, sender = sock.recvfrom(65535)
                    if sock is self._socket and sender == self._peer:
                        self._last_peer = time.monotonic()
                        self.metrics["datagrams_received"] += 1
                        self._handle(packet)
                    elif (
                        sock is self._main
                        and sender == self._server
                        and packet[:4] == struct.pack("<HH", 0x2012, 1013)
                    ):
                        registration_response(
                            packet, self.configuration.application_key
                        )
                        self.metrics["verified_registrations"] += 1
        except (OSError, ValueError, TransportError) as exc:
            self.failure = (
                str(exc)
                if isinstance(exc, (ProtocolError, TransportError))
                else type(exc).__name__
            )
        finally:
            self._stop.set()
            with self._condition:
                for stream in self._streams.values():
                    stream._finish()
                self._streams.clear()
                self._condition.notify_all()

    def close(self) -> None:
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
        if self._worker is not None and self._worker is not threading.current_thread():
            self._worker.join(timeout=2)
        for sock in (self._socket, self._main):
            if sock is not None:
                sock.close()

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.close()


class LoopbackProxy:
    """Expose the tunneled port only on IPv4 loopback for a local RTSP client."""

    def __init__(self, transport: Transport):
        self.transport = transport
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self._listener.settimeout(0.2)
        self.port = self._listener.getsockname()[1]
        self._stop = threading.Event()
        self._clients: set[socket.socket] = set()
        self._lock = threading.Lock()
        self._worker = threading.Thread(
            target=self._accept, name="isic-loopback", daemon=True
        )
        self._worker.start()

    @property
    def alive(self) -> bool:
        return not self._stop.is_set() and self._worker.is_alive()

    def _accept(self) -> None:
        while not self._stop.is_set() and self.transport.alive:
            try:
                client, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with self._lock:
                if len(self._clients) >= 8:
                    client.close()
                    continue
                self._clients.add(client)
            threading.Thread(target=self._bridge, args=(client,), daemon=True).start()

    def _bridge(self, client: socket.socket) -> None:
        stream = None
        try:
            client.settimeout(1)
            stream = self.transport.open_stream()

            def downstream() -> None:
                try:
                    while not self._stop.is_set():
                        payload = stream.receive(timeout=20)
                        if not payload:
                            break
                        client.sendall(payload)
                except (OSError, TransportError):
                    pass
                finally:
                    with suppress(OSError):
                        client.shutdown(socket.SHUT_RDWR)

            pump = threading.Thread(target=downstream, daemon=True)
            pump.start()
            while not self._stop.is_set() and self.transport.alive:
                try:
                    payload = client.recv(4096)
                except TimeoutError:
                    # A playing RTSP client may send no requests for a long time
                    # while continuing to consume interleaved media downstream.
                    continue
                if not payload:
                    break
                stream.send(payload)
        except (OSError, TransportError):
            pass
        finally:
            if stream is not None:
                with suppress(TransportError):
                    stream.close()
            client.close()
            with self._lock:
                self._clients.discard(client)

    def close(self) -> None:
        self._stop.set()
        self._listener.close()
        with self._lock:
            for client in self._clients:
                with suppress(OSError):
                    client.shutdown(socket.SHUT_RDWR)
        self._worker.join(timeout=1)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_) -> None:
        self.close()

"""UDP socket shared by many RUDP connections, demultiplexed by ``session_id``."""

from __future__ import annotations

import asyncio
import logging
import socket
import time

from streamlink.crypto.session import PacketCipher
from streamlink.protocol.packet import PacketError, decode
from streamlink.transport.config import TransportConfig
from streamlink.transport.rudp import Connection

log = logging.getLogger(__name__)


class Endpoint(asyncio.DatagramProtocol):
    """Owns one UDP socket, routes datagrams to connections and drives their timers."""

    def __init__(self, config: TransportConfig | None = None) -> None:
        self.config = config or TransportConfig()
        self.connections: dict[int, Connection] = {}
        self.transport: asyncio.DatagramTransport | None = None
        self.invalid_packets = 0
        self.unknown_session = 0
        self.socket_errors = 0
        self.closed = False
        self._tick_task: asyncio.Task | None = None

    @classmethod
    async def bind(
        cls, host: str = "0.0.0.0", port: int = 0, config: TransportConfig | None = None
    ) -> Endpoint:
        loop = asyncio.get_running_loop()
        endpoint = cls(config)
        await loop.create_datagram_endpoint(lambda: endpoint, local_addr=(host, port))
        return endpoint

    @property
    def local_address(self) -> tuple:
        return self.transport.get_extra_info("sockname") if self.transport else ("", 0)

    # asyncio.DatagramProtocol -------------------------------------------------

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]
        sock = transport.get_extra_info("socket")
        if sock is not None:
            for option in (socket.SO_RCVBUF, socket.SO_SNDBUF):
                try:
                    sock.setsockopt(socket.SOL_SOCKET, option, self.config.socket_buffer)
                except OSError:
                    pass
        self._tick_task = asyncio.ensure_future(self._tick_loop())

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        try:
            header, raw_header, payload = decode(data)
        except PacketError:
            self.invalid_packets += 1
            return
        conn = self.connections.get(header.session_id)
        if conn is None:
            self.unknown_session += 1
            return
        try:
            conn.handle_datagram(header, raw_header, payload, addr[:2])
        except Exception:
            log.exception("error while handling datagram from %s", addr)

    def error_received(self, exc: Exception) -> None:
        self.socket_errors += 1
        log.debug("UDP socket error: %s", exc)

    def connection_lost(self, exc: Exception | None) -> None:
        self.closed = True

    # connections ----------------------------------------------------------------

    def create_connection(
        self,
        session_id: int,
        cipher: PacketCipher,
        peer_addr: tuple | None = None,
        label: str = "",
    ) -> Connection:
        old = self.connections.get(session_id)
        if old is not None:
            old.close("replaced", send_bye=False)
        conn = Connection(session_id, cipher, self.config, self._sendto, peer_addr, label)
        conn._detach = self._detach
        self.connections[session_id] = conn
        return conn

    def _detach(self, conn: Connection) -> None:
        if self.connections.get(conn.session_id) is conn:
            del self.connections[conn.session_id]

    def _sendto(self, data: bytes, addr: tuple) -> None:
        if self.transport is None or self.transport.is_closing():
            return
        try:
            self.transport.sendto(data, addr)
        except OSError as exc:
            self.socket_errors += 1
            log.debug("sendto %s failed: %s", addr, exc)

    async def _tick_loop(self) -> None:
        interval = self.config.tick_interval
        while not self.closed:
            await asyncio.sleep(interval)
            now = time.monotonic()
            for conn in list(self.connections.values()):
                try:
                    conn.on_tick(now)
                except Exception:
                    log.exception("error in timer of connection %s", conn.label)

    def close(self, reason: str = "shutdown") -> None:
        for conn in list(self.connections.values()):
            conn.close(reason)
        self.closed = True
        if self._tick_task is not None:
            self._tick_task.cancel()
        if self.transport is not None:
            self.transport.close()

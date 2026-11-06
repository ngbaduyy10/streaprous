"""StreamLink server: TCP handshake listener plus the UDP endpoint that serves every client."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from pathlib import Path

from streamlink.crypto.session import PacketCipher
from streamlink.protocol.constants import DEFAULT_TCP_PORT, DEFAULT_UDP_PORT
from streamlink.server.file_relay import FileRelay
from streamlink.server.handshake import HandshakeHandler
from streamlink.server.router import Router
from streamlink.server.sessions import Session, SessionRegistry
from streamlink.server.users import UserStore
from streamlink.transport.config import TransportConfig
from streamlink.transport.endpoint import Endpoint
from streamlink.transport.rudp import Connection

log = logging.getLogger("streamlink.server")


class StreamLinkServer:
    """TCP listener for logins plus one UDP endpoint shared by all client sessions."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        tcp_port: int = DEFAULT_TCP_PORT,
        udp_port: int = DEFAULT_UDP_PORT,
        users: UserStore | None = None,
        config: TransportConfig | None = None,
        max_clients: int = 64,
    ) -> None:
        self.host = host
        self.tcp_port = tcp_port
        self.udp_port = udp_port
        self.users = users or UserStore(None)
        self.config = config or TransportConfig()
        self.max_clients = max_clients
        self.registry = SessionRegistry()
        self.relay = FileRelay(self.registry)
        self.router = Router(self.registry, self.relay)
        self.endpoint: Endpoint | None = None
        self._tcp_server: asyncio.base_events.Server | None = None
        self._shutting_down = False
        self._handshake = HandshakeHandler(
            users=self.users,
            new_session_id=self.registry.new_session_id,
            create_session=self._create_session,
            user_list=self.registry.user_list,
            can_accept=lambda: len(self.registry) < self.max_clients,
            udp_port=lambda: self.udp_port,
            heartbeat=self.config.heartbeat_interval,
        )

    async def start(self) -> None:
        self.endpoint = await Endpoint.bind(self.host, self.udp_port, self.config)
        self.udp_port = self.endpoint.local_address[1]
        self._tcp_server = await asyncio.start_server(
            self._handshake.handle, self.host, self.tcp_port
        )
        self.tcp_port = self._tcp_server.sockets[0].getsockname()[1]
        log.info(
            "listening on %s (TCP handshake :%d, UDP data :%d, congestion=%s)",
            self.host,
            self.tcp_port,
            self.udp_port,
            self.config.congestion,
        )

    async def close(self) -> None:
        self._shutting_down = True
        if self._tcp_server is not None:
            self._tcp_server.close()
            await self._tcp_server.wait_closed()
        if self.endpoint is not None:
            self.endpoint.close("server shutdown")

    # session lifecycle ------------------------------------------------------------

    def _create_session(
        self, session_id: int, username: str, cipher: PacketCipher, tcp_addr: tuple
    ) -> Session:
        assert self.endpoint is not None
        previous = self.registry.by_name(username)
        if previous is not None:
            log.info(
                "user %r logged in again, closing old session %08x", username, previous.session_id
            )
            previous.conn.close("replaced by a new login")
        conn = self.endpoint.create_connection(session_id, cipher, label=username)
        session = Session(session_id, username, tcp_addr[:2], conn)
        conn.on_message = self.router.handle
        conn.on_bound = self._on_bound
        conn.on_closed = self._on_closed
        self.registry.add(session)
        return session

    def _on_bound(self, conn: Connection) -> None:
        session = self.registry.get(conn.session_id)
        if session is None:
            return
        session.active = True
        log.info(
            "user %r is online at UDP %s:%s (%d online)",
            session.username,
            *conn.peer_addr,
            len(self.registry.active()),
        )
        self.router.send_user_list(session)
        self.router.announce(session, "join")

    def _on_closed(self, conn: Connection, reason: str) -> None:
        session = self.registry.remove(conn.session_id)
        if session is None:
            return
        log.info(
            "user %r disconnected: %s (%d online)",
            session.username,
            reason,
            len(self.registry.active()),
        )
        if self._shutting_down:
            return
        if session.active:
            session.active = False
            self.router.announce(session, "leave", reason)
        self.relay.on_session_closed(session)

    # monitoring ---------------------------------------------------------------------

    def status(self) -> dict:
        endpoint = self.endpoint
        return {
            "online": len(self.registry.active()),
            "sessions": len(self.registry),
            "messages_routed": self.router.messages_routed,
            "active_transfers": len(self.relay.transfers),
            "files_completed": self.relay.completed,
            "bytes_relayed": self.relay.bytes_relayed,
            "invalid_packets": endpoint.invalid_packets if endpoint else 0,
            "unknown_session_packets": endpoint.unknown_session if endpoint else 0,
        }


async def _status_loop(server: StreamLinkServer, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        log.info("status %s", server.status())


async def run(args: argparse.Namespace) -> None:
    config = TransportConfig(congestion=args.cc)
    users = UserStore(args.users, allow_register=not args.no_register)
    server = StreamLinkServer(
        args.host, args.tcp_port, args.udp_port, users, config, args.max_clients
    )
    await server.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass  # Windows: fall back to KeyboardInterrupt
    status_task = asyncio.ensure_future(_status_loop(server, args.status_interval))
    try:
        await stop.wait()
    finally:
        status_task.cancel()
        log.info("shutting down")
        await server.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m streamlink.server", description="StreamLink server"
    )
    parser.add_argument(
        "--host", default="0.0.0.0", help="address to bind (default: all interfaces)"
    )
    parser.add_argument("--tcp-port", type=int, default=DEFAULT_TCP_PORT, help="TCP handshake port")
    parser.add_argument("--udp-port", type=int, default=DEFAULT_UDP_PORT, help="UDP data port")
    parser.add_argument(
        "--users", default=str(Path("data") / "users.json"), help="account store (JSON)"
    )
    parser.add_argument("--no-register", action="store_true", help="reject unknown usernames")
    parser.add_argument(
        "--cc", choices=["cubic", "reno"], default="cubic", help="congestion control"
    )
    parser.add_argument("--max-clients", type=int, default=64)
    parser.add_argument(
        "--status-interval", type=float, default=30.0, help="seconds between status logs"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass

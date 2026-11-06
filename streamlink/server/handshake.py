"""TCP handshake: HELLO (X25519 key exchange) then AUTH, answered by AUTH_OK or AUTH_FAIL."""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from streamlink.crypto.session import (
    CryptoError,
    KeyPair,
    PacketCipher,
    decode_public,
    seal_packet,
    server_cipher,
)
from streamlink.protocol.constants import VERSION, PacketType
from streamlink.protocol.messages import decode_json, encode_json
from streamlink.protocol.packet import Header, PacketError, encode, read_frame, write_frame
from streamlink.server.sessions import Session
from streamlink.server.users import UserStore

log = logging.getLogger(__name__)


class HandshakeHandler:
    """Runs the login exchange for one TCP connection, then closes it.

    ``create_session`` must register the UDP connection before AUTH_OK is sent, so the client's
    first datagram always finds its session.
    """

    def __init__(
        self,
        users: UserStore,
        new_session_id: Callable[[], int],
        create_session: Callable[[int, str, PacketCipher, tuple], Session],
        user_list: Callable[[], list],
        can_accept: Callable[[], bool],
        udp_port: Callable[[], int],
        heartbeat: float,
        timeout: float = 10.0,
    ) -> None:
        self.users = users
        self.new_session_id = new_session_id
        self.create_session = create_session
        self.user_list = user_list
        self.can_accept = can_accept
        self.udp_port = udp_port
        self.heartbeat = heartbeat
        self.timeout = timeout

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername") or ("?", 0)
        try:
            await asyncio.wait_for(self._run(reader, writer, peer), self.timeout)
        except asyncio.TimeoutError:
            log.warning("handshake with %s:%s timed out", peer[0], peer[1])
        except (PacketError, CryptoError, ValueError) as exc:
            log.warning("bad handshake from %s:%s: %s", peer[0], peer[1], exc)
        except (asyncio.IncompleteReadError, ConnectionError, OSError) as exc:
            log.info("client %s:%s left during handshake: %s", peer[0], peer[1], exc)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def _run(self, reader, writer, peer: tuple) -> None:
        header, _, payload = await read_frame(reader)
        if header.type != PacketType.HELLO:
            raise PacketError(f"expected HELLO, got type 0x{header.type:02x}")
        hello = decode_json(payload)
        if hello.get("version") != VERSION:
            raise PacketError(f"unsupported client version {hello.get('version')!r}")
        client_pub = decode_public(hello.get("pub"))

        session_id = self.new_session_id()
        keys = KeyPair()
        cipher = server_cipher(keys, client_pub, session_id)
        reply = {"session_id": session_id, "pub": keys.public_b64, "udp_port": self.udp_port()}
        await write_frame(
            writer, encode(Header(PacketType.HELLO, session_id=session_id), encode_json(reply))
        )

        header, raw, payload = await read_frame(reader)
        if header.type != PacketType.AUTH or header.session_id != session_id:
            raise PacketError("expected AUTH for this session")
        auth = decode_json(cipher.open(raw, payload))
        username = auth.get("username")

        async def send(ptype: PacketType, body: dict) -> None:
            packet = seal_packet(cipher, Header(ptype, session_id=session_id), encode_json(body))
            await write_frame(writer, packet)

        if not self.can_accept():
            await send(PacketType.AUTH_FAIL, {"reason": "server is full"})
            return
        ok, reason = await self.users.authenticate(username, auth.get("password"))
        if not ok:
            log.info("login failed for %r from %s:%s: %s", username, peer[0], peer[1], reason)
            await send(PacketType.AUTH_FAIL, {"reason": reason})
            return

        self.create_session(session_id, username, cipher, peer)
        await send(
            PacketType.AUTH_OK,
            {
                "username": username,
                "session_id": session_id,
                "udp_port": self.udp_port(),
                "heartbeat": self.heartbeat,
                "users": self.user_list(),
                "note": reason,
            },
        )
        log.info(
            "user %r authenticated from %s:%s (session %08x)",
            username,
            peer[0],
            peer[1],
            session_id,
        )

"""Application-level routing on the server: user list, presence, broadcast and direct messages."""

from __future__ import annotations

import itertools
import logging
import time

from streamlink.protocol.constants import MAX_TEXT_LENGTH, PacketType
from streamlink.protocol.messages import decode_json
from streamlink.server.file_relay import FileRelay
from streamlink.server.sessions import Session, SessionRegistry
from streamlink.transport.rudp import Connection

log = logging.getLogger(__name__)


class Router:
    """Dispatches application messages from clients: user list, chat and file control."""

    def __init__(self, registry: SessionRegistry, relay: FileRelay) -> None:
        self.registry = registry
        self.relay = relay
        self._message_ids = itertools.count(1)
        self.messages_routed = 0
        self._handlers = {
            PacketType.LIST: self._on_list,
            PacketType.MSG_ALL: self._on_broadcast,
            PacketType.MSG_DM: self._on_direct,
            PacketType.FILE_OFFER: relay.on_offer,
            PacketType.FILE_ACCEPT: relay.on_accept,
            PacketType.FILE_REJECT: relay.on_reject,
            PacketType.FILE_DONE: relay.on_done,
            PacketType.FILE_PROGRESS: relay.on_progress,
            PacketType.FILE_CANCEL: relay.on_cancel,
        }

    def handle(self, conn: Connection, ptype: int, stream_id: int, payload: bytes) -> None:
        """Connection.on_message callback for every authenticated client."""
        session = self.registry.get(conn.session_id)
        if session is None:
            return
        if ptype == PacketType.FILE_CHUNK:
            self.relay.on_chunk(session, payload)
            return
        handler = self._handlers.get(ptype)
        if handler is None:
            self.error(session, "unsupported", f"unsupported message type 0x{ptype:02x}")
            return
        try:
            body = decode_json(payload)
        except ValueError as exc:
            self.error(session, "bad_request", str(exc))
            return
        handler(session, body)

    def error(self, session: Session, code: str, message: str, **extra) -> None:
        session.send(PacketType.ERROR, {"code": code, "message": message, **extra})

    # presence -------------------------------------------------------------------

    def send_user_list(self, session: Session) -> None:
        session.send(PacketType.LIST, {"users": self.registry.user_list()})

    def announce(self, session: Session, event: str, reason: str | None = None) -> None:
        body = {"event": event, "user": session.info()}
        if reason:
            body["reason"] = reason
        for other in self.registry.active():
            if other is not session:
                other.send(PacketType.PRESENCE, body)

    def _on_list(self, session: Session, body: dict) -> None:
        self.send_user_list(session)

    # messages -------------------------------------------------------------------

    def _text(self, session: Session, body: dict) -> str | None:
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            self.error(
                session, "bad_request", "text must be a non-empty string", cid=body.get("cid")
            )
            return None
        if len(text) > MAX_TEXT_LENGTH:
            self.error(
                session, "too_long", f"text longer than {MAX_TEXT_LENGTH}", cid=body.get("cid")
            )
            return None
        return text

    def _on_broadcast(self, session: Session, body: dict) -> None:
        text = self._text(session, body)
        if text is None:
            return
        out = {
            "id": next(self._message_ids),
            "from": session.username,
            "text": text,
            "ts": time.time(),
            "cid": body.get("cid"),
        }
        for target in self.registry.active():
            target.send(PacketType.MSG_ALL, out)
        self.messages_routed += 1

    def _on_direct(self, session: Session, body: dict) -> None:
        text = self._text(session, body)
        if text is None:
            return
        to = body.get("to")
        target = self.registry.by_name(to) if isinstance(to, str) else None
        if target is None or not target.active:
            self.error(session, "user_offline", f"{to} is not online", to=to, cid=body.get("cid"))
            return
        out = {
            "id": next(self._message_ids),
            "from": session.username,
            "to": target.username,
            "text": text,
            "ts": time.time(),
            "cid": body.get("cid"),
        }
        target.send(PacketType.MSG_DM, out)
        if target is not session:
            session.send(PacketType.MSG_DM, out)
        self.messages_routed += 1

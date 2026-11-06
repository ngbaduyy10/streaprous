"""Server-side bookkeeping of logged-in users and their network addresses."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

from streamlink.protocol.messages import encode_json
from streamlink.transport.rudp import Connection, ConnectionClosed


@dataclass(eq=False)
class Session:
    """A logged-in user; ``active`` becomes true once its UDP address is known."""

    session_id: int
    username: str
    tcp_addr: tuple
    conn: Connection
    created_at: float = field(default_factory=time.time)
    active: bool = False

    @property
    def udp_addr(self) -> tuple | None:
        return self.conn.peer_addr

    def send(self, ptype: int, body: dict, stream_id: int = 0) -> bool:
        """Send a JSON message reliably; returns False if the connection is already closed."""
        try:
            self.conn.send(ptype, encode_json(body), stream_id)
            return True
        except ConnectionClosed:
            return False

    def info(self) -> dict:
        addr = self.udp_addr or self.tcp_addr
        return {
            "username": self.username,
            "ip": addr[0] if addr else None,
            "port": addr[1] if addr else None,
            "since": self.created_at,
        }


class SessionRegistry:
    """Index of sessions by id and by username."""

    def __init__(self) -> None:
        self._by_id: dict[int, Session] = {}
        self._by_name: dict[str, Session] = {}

    def __len__(self) -> int:
        return len(self._by_id)

    def new_session_id(self) -> int:
        while True:
            sid = secrets.randbits(32)
            if sid and sid not in self._by_id:
                return sid

    def add(self, session: Session) -> None:
        self._by_id[session.session_id] = session
        self._by_name[session.username] = session

    def remove(self, session_id: int) -> Session | None:
        session = self._by_id.pop(session_id, None)
        if session is not None and self._by_name.get(session.username) is session:
            del self._by_name[session.username]
        return session

    def get(self, session_id: int) -> Session | None:
        return self._by_id.get(session_id)

    def by_name(self, username: str) -> Session | None:
        return self._by_name.get(username)

    def active(self) -> list[Session]:
        return [s for s in self._by_id.values() if s.active]

    def user_list(self) -> list[dict]:
        return sorted((s.info() for s in self.active()), key=lambda u: u["username"])

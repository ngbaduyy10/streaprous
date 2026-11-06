"""Relays file transfers between two clients through the server.

Control messages travel on stream 0. Chunks and the sender's FILE_DONE travel on a dedicated
stream on each leg, so FILE_DONE can never overtake the last chunk.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from streamlink.protocol.constants import MAX_FILE_SIZE, PacketType
from streamlink.protocol.messages import (
    safe_filename,
    unpack_chunk,
    valid_sha256,
    valid_transfer_id,
)
from streamlink.server.sessions import Session, SessionRegistry
from streamlink.transport.rudp import ConnectionClosed

log = logging.getLogger(__name__)


@dataclass
class RelayTransfer:
    """A transfer being relayed: who sends to whom and on which outgoing stream."""

    id: str
    sender: Session
    receiver: Session
    name: str
    size: int
    sha256: str
    accepted: bool = False
    out_stream: int = 0
    relayed: int = 0


class FileRelay:
    """Forwards file offers, chunks and completion between two sessions without storing data."""

    def __init__(self, registry: SessionRegistry) -> None:
        self.registry = registry
        self.transfers: dict[str, RelayTransfer] = {}
        self.bytes_relayed = 0
        self.completed = 0

    def _send(self, session: Session, ptype: PacketType, body: dict, stream_id: int = 0) -> None:
        session.send(ptype, body, stream_id)

    def _lookup(self, session: Session, body: dict) -> RelayTransfer | None:
        tid = body.get("id")
        transfer = self.transfers.get(tid) if isinstance(tid, str) else None
        if transfer is None or session not in (transfer.sender, transfer.receiver):
            self._send(session, PacketType.ERROR, {"code": "unknown_transfer", "id": tid})
            return None
        return transfer

    def on_offer(self, session: Session, body: dict) -> None:
        tid, to, size, sha = body.get("id"), body.get("to"), body.get("size"), body.get("sha256")

        def reject(reason: str) -> None:
            self._send(session, PacketType.FILE_REJECT, {"id": tid, "reason": reason})

        if not valid_transfer_id(tid) or tid in self.transfers:
            return reject("invalid transfer id")
        if not isinstance(size, int) or not 0 <= size <= MAX_FILE_SIZE:
            return reject("invalid size")
        if not valid_sha256(sha):
            return reject("invalid sha256")
        receiver = self.registry.by_name(to) if isinstance(to, str) else None
        if receiver is None or not receiver.active:
            return reject("user_offline")
        if receiver is session:
            return reject("cannot send a file to yourself")

        transfer = RelayTransfer(tid, session, receiver, safe_filename(body.get("name")), size, sha)
        self.transfers[tid] = transfer
        self._send(
            receiver,
            PacketType.FILE_OFFER,
            {
                "id": tid,
                "from": session.username,
                "name": transfer.name,
                "size": size,
                "sha256": sha,
            },
        )
        log.info(
            "file offer %s: %s -> %s (%s, %d bytes)",
            tid[:8],
            session.username,
            receiver.username,
            transfer.name,
            size,
        )

    def on_accept(self, session: Session, body: dict) -> None:
        transfer = self._lookup(session, body)
        if transfer is None or session is not transfer.receiver or transfer.accepted:
            return
        transfer.accepted = True
        transfer.out_stream = transfer.receiver.conn.open_stream()
        self._send(transfer.sender, PacketType.FILE_ACCEPT, {"id": transfer.id})

    def on_reject(self, session: Session, body: dict) -> None:
        transfer = self._lookup(session, body)
        if transfer is None or session is not transfer.receiver:
            return
        del self.transfers[transfer.id]
        reason = body.get("reason") if isinstance(body.get("reason"), str) else "rejected"
        self._send(transfer.sender, PacketType.FILE_REJECT, {"id": transfer.id, "reason": reason})

    def on_chunk(self, session: Session, payload: bytes) -> None:
        try:
            tid, offset, data = unpack_chunk(payload)
        except ValueError:
            return
        transfer = self.transfers.get(tid)
        if transfer is None or transfer.sender is not session or not transfer.accepted:
            return
        if offset + len(data) > transfer.size:
            self._abort(transfer, "chunk beyond declared size")
            return
        try:
            transfer.receiver.conn.send(PacketType.FILE_CHUNK, payload, transfer.out_stream)
        except ConnectionClosed:
            return
        transfer.relayed += len(data)
        self.bytes_relayed += len(data)

    def on_done(self, session: Session, body: dict) -> None:
        transfer = self._lookup(session, body)
        if transfer is None:
            return
        if session is transfer.sender:
            self._send(
                transfer.receiver, PacketType.FILE_DONE, {"id": transfer.id}, transfer.out_stream
            )
            return
        status = body.get("status") if body.get("status") in ("ok", "corrupt") else "corrupt"
        self._send(
            transfer.sender,
            PacketType.FILE_DONE,
            {"id": transfer.id, "status": status, "sha256": body.get("sha256")},
        )
        del self.transfers[transfer.id]
        self.completed += 1
        log.info(
            "file %s finished with status %s (%d bytes relayed)",
            transfer.id[:8],
            status,
            transfer.relayed,
        )

    def on_progress(self, session: Session, body: dict) -> None:
        transfer = self._lookup(session, body)
        if transfer is None or session is not transfer.receiver:
            return
        received = body.get("bytes")
        if isinstance(received, int):
            self._send(
                transfer.sender, PacketType.FILE_PROGRESS, {"id": transfer.id, "bytes": received}
            )

    def on_cancel(self, session: Session, body: dict) -> None:
        transfer = self._lookup(session, body)
        if transfer is None:
            return
        reason = body.get("reason") if isinstance(body.get("reason"), str) else "cancelled"
        other = transfer.receiver if session is transfer.sender else transfer.sender
        del self.transfers[transfer.id]
        self._send(other, PacketType.FILE_CANCEL, {"id": transfer.id, "reason": reason})

    def _abort(self, transfer: RelayTransfer, reason: str) -> None:
        self.transfers.pop(transfer.id, None)
        for party in (transfer.sender, transfer.receiver):
            self._send(party, PacketType.FILE_CANCEL, {"id": transfer.id, "reason": reason})

    def on_session_closed(self, session: Session) -> None:
        for transfer in list(self.transfers.values()):
            if session in (transfer.sender, transfer.receiver):
                other = transfer.receiver if session is transfer.sender else transfer.sender
                del self.transfers[transfer.id]
                self._send(
                    other,
                    PacketType.FILE_CANCEL,
                    {"id": transfer.id, "reason": "peer disconnected"},
                )

"""Client networking core shared by the CLI and the web UI.

``ClientCore`` performs the TCP login, owns the UDP endpoint, keeps the user list, chat history
and file transfers, reconnects automatically, and publishes everything as events on an
``EventBus`` so user interfaces never touch sockets directly.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from streamlink.crypto.session import (
    CryptoError,
    KeyPair,
    PacketCipher,
    client_cipher,
    decode_public,
    seal_packet,
)
from streamlink.protocol.constants import (
    DEFAULT_TCP_PORT,
    FILE_CHUNK_SIZE,
    MAX_FILE_SIZE,
    MAX_TEXT_LENGTH,
    VERSION,
    PacketType,
)
from streamlink.protocol.messages import (
    decode_json,
    encode_json,
    new_transfer_id,
    pack_chunk,
    safe_filename,
    unpack_chunk,
    valid_sha256,
    valid_transfer_id,
)
from streamlink.protocol.packet import Header, PacketError, encode, read_frame, write_frame
from streamlink.transport.config import TransportConfig
from streamlink.transport.endpoint import Endpoint
from streamlink.transport.rudp import Connection, ConnectionClosed

log = logging.getLogger(__name__)

E2E_WINDOW = 512 * 1024
PROGRESS_EVERY = 128
STREAM_BACKLOG = 256
EMIT_INTERVAL = 0.2


class AuthError(Exception):
    """The server rejected the credentials."""


class NotConnected(Exception):
    """An operation needs an active connection."""


class TransferError(Exception):
    """A file transfer was rejected, cancelled or failed."""


class EventBus:
    """Fan-out of event dicts to any number of asyncio queues (one per UI subscriber)."""

    def __init__(self) -> None:
        self._queues: set[asyncio.Queue] = set()

    def subscribe(self, maxsize: int = 1000) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize)
        self._queues.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._queues.discard(queue)

    def emit(self, event: dict) -> None:
        for queue in list(self._queues):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(event)


@dataclass(eq=False)
class Transfer:
    """One file transfer as seen by this client (direction "out" = we are the sender)."""

    id: str
    direction: str  # "out" or "in"
    peer: str
    name: str
    size: int
    sha256: str = ""
    path: str | None = None
    state: str = "offered"
    bytes_done: int = 0
    acked: int = 0
    error: str | None = None
    started: float = field(default_factory=time.monotonic)
    finished: float | None = None
    cleanup: bool = False
    file: Any = None
    hasher: Any = None
    accepted: asyncio.Future | None = None
    result: asyncio.Future | None = None
    progress: asyncio.Event | None = None
    task: asyncio.Task | None = None
    last_emit: float = 0.0
    unreported_chunks: int = 0

    @property
    def confirmed(self) -> int:
        """Bytes known to be at the destination (for uploads: as acknowledged by the receiver)."""
        if self.direction == "out" and self.state != "done":
            return min(self.acked, self.bytes_done)
        return self.bytes_done

    def info(self) -> dict:
        elapsed = max((self.finished or time.monotonic()) - self.started, 1e-6)
        done = self.confirmed
        return {
            "id": self.id,
            "direction": self.direction,
            "peer": self.peer,
            "name": self.name,
            "size": self.size,
            "bytes": done,
            "sent": self.bytes_done,
            "percent": round(100 * done / self.size, 1) if self.size else 100.0,
            "state": self.state,
            "error": self.error,
            "speed": round(done / elapsed),
            "elapsed": round(elapsed, 2),
            "sha256": self.sha256,
            "download": f"/files/{self.id}"
            if self.direction == "in" and self.state == "done"
            else None,
        }


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _unique_path(directory: Path, name: str) -> Path:
    candidate = directory / name
    stem, suffix = os.path.splitext(name)
    counter = 1
    while candidate.exists():
        candidate = directory / f"{stem} ({counter}){suffix}"
        counter += 1
    return candidate


def _fail_future(future: asyncio.Future | None, exc: Exception) -> None:
    if future is not None and not future.done():
        future.set_exception(exc)
        future.exception()  # mark as retrieved: the waiter may already be gone


class ClientCore:
    """UI-independent client: login, messaging, file transfers and reconnection.

    Every state change is published on ``events`` as a dict with a ``type`` key
    (status, users, presence, message, file, stats, error), consumed by the CLI or web UI.
    """

    def __init__(
        self,
        download_dir: str | os.PathLike = "downloads",
        config: TransportConfig | None = None,
        auto_accept: bool = True,
        udp_override: tuple[str, int] | None = None,
        auto_reconnect: bool = True,
    ) -> None:
        self.download_dir = Path(download_dir)
        self.config = config or TransportConfig()
        self.auto_accept = auto_accept
        self.udp_override = udp_override
        self.auto_reconnect = auto_reconnect
        self.events = EventBus()

        self.state = "disconnected"
        self.state_reason: str | None = None
        self.username: str | None = None
        self.server: tuple[str, int] | None = None
        self.users: dict[str, dict] = {}
        self.history: deque[dict] = deque(maxlen=500)
        self.transfers: dict[str, Transfer] = {}
        self.conn: Connection | None = None
        self.endpoint: Endpoint | None = None
        self.reconnects = 0

        self._password: str | None = None
        self._closing = False
        self._reconnect_task: asyncio.Task | None = None
        self._stats_task: asyncio.Task | None = None
        self._cids = itertools.count(1)

    # ------------------------------------------------------------- connection

    @property
    def connected(self) -> bool:
        return self.conn is not None and not self.conn.closed

    async def connect(
        self,
        host: str,
        username: str,
        password: str,
        port: int = DEFAULT_TCP_PORT,
        timeout: float = 10.0,
    ) -> None:
        """Log in over TCP, then open the UDP association. Raises AuthError on bad credentials."""
        if self.state in ("connecting", "connected", "reconnecting"):
            raise RuntimeError(f"already {self.state}")
        self._closing = False
        self.server = (host, port)
        self.username = username
        self._password = password
        self._set_state("connecting")
        try:
            await self._establish(timeout)
        except BaseException as exc:
            self._set_state("disconnected", str(exc) or type(exc).__name__)
            raise
        if self._stats_task is None or self._stats_task.done():
            self._stats_task = asyncio.ensure_future(self._stats_loop())

    async def _establish(self, timeout: float) -> None:
        assert self.server is not None
        host, port = self.server
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        try:
            peer_ip = writer.get_extra_info("peername")[0]
            session_id, cipher, udp_port, welcome = await asyncio.wait_for(
                self._handshake(reader, writer), timeout
            )
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

        udp_addr = self.udp_override or (peer_ip, udp_port)
        bind_host = "::" if ":" in udp_addr[0] else "0.0.0.0"
        endpoint = await Endpoint.bind(bind_host, 0, self.config)
        conn = endpoint.create_connection(
            session_id, cipher, peer_addr=udp_addr, label=self.username or ""
        )
        conn.on_message = self._on_message
        conn.on_closed = self._on_closed
        self.endpoint, self.conn = endpoint, conn
        self.users = {u["username"]: u for u in welcome.get("users", []) if isinstance(u, dict)}
        conn.send_ping()
        log.info("logged in as %r (session %08x, UDP %s:%s)", self.username, session_id, *udp_addr)
        self._set_state("connected")
        self._emit_users()

    async def _handshake(self, reader, writer) -> tuple[int, PacketCipher, int, dict]:
        keys = KeyPair()
        hello = encode_json({"version": VERSION, "pub": keys.public_b64})
        await write_frame(writer, encode(Header(PacketType.HELLO), hello))

        header, _, payload = await read_frame(reader)
        if header.type != PacketType.HELLO:
            raise PacketError("expected HELLO from server")
        reply = decode_json(payload)
        session_id = int(reply["session_id"])
        cipher = client_cipher(keys, decode_public(reply.get("pub")), session_id)

        credentials = encode_json({"username": self.username, "password": self._password})
        await write_frame(
            writer, seal_packet(cipher, Header(PacketType.AUTH, session_id=session_id), credentials)
        )

        header, raw, payload = await read_frame(reader)
        body = decode_json(cipher.open(raw, payload))
        if header.type == PacketType.AUTH_FAIL:
            raise AuthError(body.get("reason", "login failed"))
        if header.type != PacketType.AUTH_OK:
            raise PacketError("expected AUTH_OK or AUTH_FAIL")
        return session_id, cipher, int(reply.get("udp_port", 0)), body

    async def disconnect(self) -> None:
        """Leave gracefully: BYE to the server, stop reconnecting, release the socket."""
        self._closing = True
        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
            self._reconnect_task = None
        conn = self.conn
        if conn is not None:
            conn.close("user quit")
        self._release_endpoint()
        if self._stats_task is not None:
            self._stats_task.cancel()
            self._stats_task = None
        self._set_state("disconnected", "user quit")

    def _release_endpoint(self) -> None:
        endpoint, self.endpoint, self.conn = self.endpoint, None, None
        if endpoint is not None:
            asyncio.get_running_loop().call_soon(endpoint.close)

    def _on_closed(self, conn: Connection, reason: str) -> None:
        if conn is not self.conn:
            return
        log.log(logging.INFO if self._closing else logging.WARNING, "connection closed: %s", reason)
        self._fail_transfers(f"connection lost: {reason}")
        self._release_endpoint()
        self.users = {}
        self._emit_users()
        if self._closing:
            self._set_state("disconnected", reason)
        elif "replaced" in reason:
            self._set_state("disconnected", "logged in from another place")
        elif self.auto_reconnect:
            self._set_state("reconnecting", reason)
            self._reconnect_task = asyncio.ensure_future(self._reconnect_loop())
        else:
            self._set_state("disconnected", reason)

    async def _reconnect_loop(self) -> None:
        delay = 1.0
        attempt = 0
        while not self._closing:
            await asyncio.sleep(delay)
            attempt += 1
            try:
                await self._establish(timeout=5.0)
            except AuthError as exc:
                self._set_state("disconnected", f"reconnect rejected: {exc}")
                return
            except (
                OSError,
                asyncio.TimeoutError,
                PacketError,
                CryptoError,
                ValueError,
                KeyError,
            ) as exc:
                log.info("reconnect attempt %d failed: %s", attempt, exc or type(exc).__name__)
                self._set_state(
                    "reconnecting", f"attempt {attempt} failed: {exc or type(exc).__name__}"
                )
                delay = min(delay * 2, 10.0)
                continue
            except asyncio.IncompleteReadError:
                delay = min(delay * 2, 10.0)
                continue
            self.reconnects += 1
            log.info("reconnected after %d attempt(s)", attempt)
            self.events.emit(self.status())
            return

    def _require(self) -> Connection:
        if self.conn is None or self.conn.closed:
            raise NotConnected("not connected to the server")
        return self.conn

    # ------------------------------------------------------------------ events

    def _set_state(self, state: str, reason: str | None = None) -> None:
        self.state = state
        self.state_reason = reason
        self.events.emit(self.status())

    def status(self) -> dict:
        return {
            "type": "status",
            "state": self.state,
            "reason": self.state_reason,
            "username": self.username,
            "server": f"{self.server[0]}:{self.server[1]}" if self.server else None,
            "reconnects": self.reconnects,
        }

    def _emit_users(self) -> None:
        self.events.emit(
            {"type": "users", "users": sorted(self.users.values(), key=lambda u: u["username"])}
        )

    def _emit_transfer(self, transfer: Transfer, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - transfer.last_emit >= EMIT_INTERVAL:
            transfer.last_emit = now
            self.events.emit({"type": "file", **transfer.info()})

    async def _stats_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            if self.conn is not None:
                self.events.emit({"type": "stats", **self.conn.snapshot()})

    def snapshot(self) -> dict:
        """Full client state, sent to a UI when it (re)attaches."""
        return {
            "type": "snapshot",
            "status": self.status(),
            "users": sorted(self.users.values(), key=lambda u: u["username"]),
            "history": list(self.history),
            "transfers": [t.info() for t in self.transfers.values()],
            "stats": self.conn.snapshot() if self.conn else None,
        }

    # ---------------------------------------------------------------- messaging

    def _check_text(self, text: str) -> str:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("message is empty")
        if len(text) > MAX_TEXT_LENGTH:
            raise ValueError(f"message longer than {MAX_TEXT_LENGTH} characters")
        return text

    def send_broadcast(self, text: str) -> str:
        conn = self._require()
        cid = f"c{next(self._cids)}"
        conn.send(PacketType.MSG_ALL, encode_json({"text": self._check_text(text), "cid": cid}))
        return cid

    def send_direct(self, to: str, text: str) -> str:
        conn = self._require()
        cid = f"c{next(self._cids)}"
        conn.send(
            PacketType.MSG_DM, encode_json({"to": to, "text": self._check_text(text), "cid": cid})
        )
        return cid

    def request_users(self) -> None:
        self._require().send(PacketType.LIST, encode_json({}))

    def _on_message(self, conn: Connection, ptype: int, stream_id: int, payload: bytes) -> None:
        if ptype == PacketType.FILE_CHUNK:
            self._on_file_chunk(payload)
            return
        try:
            body = decode_json(payload)
        except ValueError:
            log.warning("ignoring malformed message of type 0x%02x", ptype)
            return
        if ptype == PacketType.LIST:
            users = body.get("users", [])
            self.users = {
                u["username"]: u for u in users if isinstance(u, dict) and "username" in u
            }
            self._emit_users()
        elif ptype == PacketType.PRESENCE:
            self._on_presence(body)
        elif ptype in (PacketType.MSG_ALL, PacketType.MSG_DM):
            event = {
                "type": "message",
                "kind": "all" if ptype == PacketType.MSG_ALL else "dm",
                "id": body.get("id"),
                "from": body.get("from"),
                "to": body.get("to"),
                "text": body.get("text"),
                "ts": body.get("ts"),
                "cid": body.get("cid"),
                "mine": body.get("from") == self.username,
            }
            self.history.append(event)
            self.events.emit(event)
        elif ptype == PacketType.FILE_OFFER:
            self._on_file_offer(body)
        elif ptype == PacketType.FILE_ACCEPT:
            transfer = self.transfers.get(body.get("id"))
            if (
                transfer is not None
                and transfer.accepted is not None
                and not transfer.accepted.done()
            ):
                transfer.accepted.set_result(True)
        elif ptype == PacketType.FILE_REJECT:
            transfer = self.transfers.get(body.get("id"))
            if transfer is not None:
                self._abort_transfer(
                    transfer, f"rejected: {body.get('reason', 'rejected')}", "rejected"
                )
        elif ptype == PacketType.FILE_PROGRESS:
            self._on_file_progress(body)
        elif ptype == PacketType.FILE_DONE:
            self._on_file_done(body)
        elif ptype == PacketType.FILE_CANCEL:
            transfer = self.transfers.get(body.get("id"))
            if transfer is not None:
                self._abort_transfer(transfer, f"cancelled: {body.get('reason', 'cancelled')}")
        elif ptype == PacketType.ERROR:
            self.events.emit({"type": "error", **body})
            transfer = (
                self.transfers.get(body.get("id")) if isinstance(body.get("id"), str) else None
            )
            if transfer is not None:
                self._abort_transfer(transfer, body.get("code", "error"))

    def _on_presence(self, body: dict) -> None:
        user = body.get("user")
        if not isinstance(user, dict) or "username" not in user:
            return
        if body.get("event") == "join":
            self.users[user["username"]] = user
        else:
            self.users.pop(user["username"], None)
        self.events.emit(
            {
                "type": "presence",
                "event": body.get("event"),
                "user": user,
                "reason": body.get("reason"),
            }
        )
        self._emit_users()

    # ------------------------------------------------------------ file sending

    def send_file(
        self, path: str | os.PathLike, to: str, name: str | None = None, cleanup: bool = False
    ) -> str:
        """Start sending a file to ``to``; returns the transfer id. Progress arrives as events."""
        conn = self._require()
        if to == self.username:
            raise ValueError("cannot send a file to yourself")
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(str(path))
        size = path.stat().st_size
        if size > MAX_FILE_SIZE:
            raise ValueError("file is larger than 2 GiB")
        transfer = Transfer(
            new_transfer_id(), "out", to, safe_filename(name or path.name), size, path=str(path)
        )
        transfer.state = "hashing"
        transfer.cleanup = cleanup
        self.transfers[transfer.id] = transfer
        transfer.task = asyncio.ensure_future(self._send_file(transfer, conn))
        self._emit_transfer(transfer, force=True)
        return transfer.id

    async def _send_file(self, transfer: Transfer, conn: Connection) -> None:
        loop = asyncio.get_running_loop()
        try:
            transfer.sha256 = await loop.run_in_executor(None, sha256_file, transfer.path)
            transfer.accepted = loop.create_future()
            transfer.result = loop.create_future()
            transfer.progress = asyncio.Event()
            transfer.state = "waiting"
            self._emit_transfer(transfer, force=True)
            offer = {
                "id": transfer.id,
                "to": transfer.peer,
                "name": transfer.name,
                "size": transfer.size,
                "sha256": transfer.sha256,
            }
            conn.send(PacketType.FILE_OFFER, encode_json(offer))
            await asyncio.wait_for(asyncio.shield(transfer.accepted), 120)

            transfer.state = "sending"
            transfer.started = time.monotonic()
            self._emit_transfer(transfer, force=True)
            stream = conn.open_stream()
            with open(transfer.path, "rb") as f:
                offset = 0
                while offset < transfer.size:
                    while offset - transfer.acked >= E2E_WINDOW:
                        transfer.progress.clear()
                        await asyncio.wait_for(transfer.progress.wait(), 30)
                        if transfer.finished:
                            raise TransferError(transfer.error or "aborted")
                    await conn.wait_writable(stream, STREAM_BACKLOG)
                    if transfer.finished:
                        raise TransferError(transfer.error or "aborted")
                    data = f.read(FILE_CHUNK_SIZE)
                    if not data:
                        raise TransferError("file shrank while sending")
                    conn.send(PacketType.FILE_CHUNK, pack_chunk(transfer.id, offset, data), stream)
                    offset += len(data)
                    transfer.bytes_done = offset
                    self._emit_transfer(transfer)
            conn.send(PacketType.FILE_DONE, encode_json({"id": transfer.id}), stream)
            transfer.state = "verifying"
            self._emit_transfer(transfer, force=True)
            status = await asyncio.wait_for(asyncio.shield(transfer.result), 120)
            if status == "ok":
                self._finish(transfer, "done")
            else:
                self._finish(transfer, "failed", "receiver reported a checksum mismatch")
        except TransferError as exc:
            self._finish(transfer, "failed", str(exc))
        except ConnectionClosed:
            self._finish(transfer, "failed", "connection lost")
        except asyncio.TimeoutError:
            self._abort_transfer(transfer, "timed out waiting for the receiver", notify=True)
        except OSError as exc:
            self._abort_transfer(transfer, f"cannot read file: {exc}", notify=True)
        except asyncio.CancelledError:
            self._abort_transfer(transfer, "cancelled", notify=True)
            raise
        finally:
            if transfer.cleanup and transfer.path:
                try:
                    os.remove(transfer.path)
                except OSError:
                    pass

    def _on_file_progress(self, body: dict) -> None:
        transfer = self.transfers.get(body.get("id"))
        received = body.get("bytes")
        if transfer is None or transfer.direction != "out" or not isinstance(received, int):
            return
        transfer.acked = max(transfer.acked, received)
        if transfer.progress is not None:
            transfer.progress.set()
        self._emit_transfer(transfer)

    # ---------------------------------------------------------- file receiving

    def _on_file_offer(self, body: dict) -> None:
        tid, size, sha = body.get("id"), body.get("size"), body.get("sha256")
        if not valid_transfer_id(tid) or tid in self.transfers:
            return
        if not isinstance(size, int) or not 0 <= size <= MAX_FILE_SIZE or not valid_sha256(sha):
            self._send_quiet(PacketType.FILE_REJECT, {"id": tid, "reason": "invalid offer"})
            return
        transfer = Transfer(
            tid, "in", str(body.get("from")), safe_filename(body.get("name")), size, sha
        )
        self.transfers[tid] = transfer
        if self.auto_accept:
            self.accept_file(tid)
        else:
            self._emit_transfer(transfer, force=True)

    def accept_file(self, transfer_id: str) -> None:
        transfer = self.transfers[transfer_id]
        if transfer.direction != "in" or transfer.state != "offered":
            raise ValueError("transfer is not waiting for acceptance")
        conn = self._require()
        try:
            self.download_dir.mkdir(parents=True, exist_ok=True)
            part = self.download_dir / f".{transfer.id}.part"
            transfer.file = open(part, "wb")
        except OSError as exc:
            self.reject_file(transfer_id, f"receiver cannot write file: {exc}")
            return
        transfer.path = str(part)
        transfer.hasher = hashlib.sha256()
        transfer.state = "receiving"
        transfer.started = time.monotonic()
        conn.send(PacketType.FILE_ACCEPT, encode_json({"id": transfer.id}))
        self._emit_transfer(transfer, force=True)

    def reject_file(self, transfer_id: str, reason: str = "rejected by user") -> None:
        transfer = self.transfers[transfer_id]
        self._send_quiet(PacketType.FILE_REJECT, {"id": transfer.id, "reason": reason})
        self._finish(transfer, "rejected", reason)

    def _on_file_chunk(self, payload: bytes) -> None:
        try:
            tid, offset, data = unpack_chunk(payload)
        except ValueError:
            return
        transfer = self.transfers.get(tid)
        if transfer is None or transfer.direction != "in" or transfer.state != "receiving":
            return
        if offset != transfer.bytes_done or offset + len(data) > transfer.size:
            self._abort_transfer(transfer, "unexpected chunk offset", notify=True)
            return
        try:
            transfer.file.seek(offset)
            transfer.file.write(data)
        except OSError as exc:
            self._abort_transfer(transfer, f"cannot write file: {exc}", notify=True)
            return
        transfer.hasher.update(data)
        transfer.bytes_done += len(data)
        transfer.unreported_chunks += 1
        if transfer.unreported_chunks >= PROGRESS_EVERY:
            transfer.unreported_chunks = 0
            self._send_quiet(
                PacketType.FILE_PROGRESS, {"id": transfer.id, "bytes": transfer.bytes_done}
            )
        self._emit_transfer(transfer)

    def _on_file_done(self, body: dict) -> None:
        transfer = self.transfers.get(body.get("id"))
        if transfer is None:
            return
        if transfer.direction == "out":
            if transfer.result is not None and not transfer.result.done():
                transfer.result.set_result(body.get("status"))
            return
        if transfer.state != "receiving":
            return
        transfer.file.close()
        transfer.file = None
        digest = transfer.hasher.hexdigest()
        ok = transfer.bytes_done == transfer.size and digest == transfer.sha256
        part = Path(transfer.path)
        if ok:
            try:
                final = _unique_path(self.download_dir, transfer.name)
                os.replace(part, final)
                transfer.path = str(final)
            except OSError as exc:
                ok = False
                log.error("cannot store received file: %s", exc)
        if not ok:
            try:
                part.unlink()
            except OSError:
                pass
        self._send_quiet(
            PacketType.FILE_DONE,
            {"id": transfer.id, "status": "ok" if ok else "corrupt", "sha256": digest},
        )
        if ok:
            self._finish(transfer, "done")
        else:
            self._finish(transfer, "failed", "checksum mismatch")

    # -------------------------------------------------------- transfer helpers

    def _send_quiet(self, ptype: PacketType, body: dict) -> None:
        conn = self.conn
        if conn is not None:
            try:
                conn.send(ptype, encode_json(body))
            except ConnectionClosed:
                pass

    def _finish(self, transfer: Transfer, state: str, error: str | None = None) -> None:
        if transfer.finished is not None:
            return
        transfer.state = state
        transfer.error = error
        transfer.finished = time.monotonic()
        if transfer.file is not None:
            transfer.file.close()
            transfer.file = None
        self._emit_transfer(transfer, force=True)
        if error:
            log.info("transfer %s %s: %s", transfer.id[:8], state, error)

    def _abort_transfer(
        self, transfer: Transfer, reason: str, state: str = "failed", notify: bool = False
    ) -> None:
        if transfer.finished is not None:
            return
        _fail_future(transfer.accepted, TransferError(reason))
        _fail_future(transfer.result, TransferError(reason))
        if transfer.progress is not None:
            transfer.progress.set()
        if notify:
            self._send_quiet(PacketType.FILE_CANCEL, {"id": transfer.id, "reason": reason})
        if transfer.direction == "in" and transfer.file is not None:
            transfer.file.close()
            transfer.file = None
            try:
                os.remove(transfer.path)
            except OSError:
                pass
        self._finish(transfer, state, reason)

    def _fail_transfers(self, reason: str) -> None:
        for transfer in list(self.transfers.values()):
            self._abort_transfer(transfer, reason)

    def cancel_transfer(self, transfer_id: str) -> None:
        transfer = self.transfers[transfer_id]
        self._abort_transfer(transfer, "cancelled by user", notify=True)
        if transfer.task is not None:
            transfer.task.cancel()

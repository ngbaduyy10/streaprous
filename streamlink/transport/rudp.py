"""Reliable, ordered, multi-stream message delivery over UDP for one peer.

Each side owns numbered send streams. Inside a stream, packets carry a monotonic ``seq`` and the
receiver delivers messages strictly in order; across streams there is no ordering, so a loss on a
file stream never delays chat on stream 0 (no head-of-line blocking).

Reliability: cumulative ACK + 32-bit SACK bitmap, selective retransmission (fast retransmit after
``dupthresh`` later packets are SACKed, otherwise on RTO), Jacobson/Karels RTO with Karn's rule.
Flow control: per-stream sliding window of ``recv_window`` packets. Congestion control: a
connection-wide ``cwnd`` (CUBIC or NewReno) caps the number of packets in flight.
"""

from __future__ import annotations

import asyncio
import logging
import math
import struct
import time
from collections import OrderedDict, deque
from dataclasses import asdict, dataclass
from typing import Callable

from streamlink.crypto.session import CryptoError, PacketCipher, seal_packet
from streamlink.protocol.constants import HEADER_SIZE, MAX_FRAGMENT, Flags, PacketType, is_reliable
from streamlink.protocol.messages import decode_json, encode_json
from streamlink.protocol.packet import Header
from streamlink.transport.config import TransportConfig
from streamlink.transport.congestion import make_controller

log = logging.getLogger(__name__)

_PING = struct.Struct("!d")
MAX_MESSAGE_SIZE = 1024 * 1024

MessageHandler = Callable[["Connection", int, int, bytes], None]


class ConnectionClosed(Exception):
    """Raised when sending on, or waiting for, a connection that has been closed."""


@dataclass
class ConnectionStats:
    """Counters exposed to the UI and used by the tests."""

    packets_sent: int = 0
    packets_received: int = 0
    bytes_sent: int = 0
    bytes_received: int = 0
    messages_sent: int = 0
    messages_received: int = 0
    retransmits: int = 0
    fast_retransmits: int = 0
    timeouts: int = 0
    duplicates: int = 0
    out_of_order: int = 0
    window_drops: int = 0
    decrypt_failures: int = 0
    acks_sent: int = 0


class _OutPacket:
    __slots__ = ("stream_id", "seq", "ptype", "flags", "payload", "sent_at", "last_sent", "retx")

    def __init__(self, stream_id: int, seq: int, ptype: int, flags: int, payload: bytes) -> None:
        self.stream_id = stream_id
        self.seq = seq
        self.ptype = ptype
        self.flags = flags
        self.payload = payload
        self.sent_at = 0.0
        self.last_sent = 0.0
        self.retx = 0


class _SendStream:
    __slots__ = ("next_seq", "base")

    def __init__(self) -> None:
        self.next_seq = 0
        self.base = 0  # every seq below this has been cumulatively acknowledged


class _RecvStream:
    __slots__ = ("expected", "buffer", "fragments", "fragment_bytes", "unacked")

    def __init__(self) -> None:
        self.expected = 0
        self.buffer: dict[int, tuple[int, int, bytes]] = {}
        self.fragments: list[bytes] = []
        self.fragment_bytes = 0
        self.unacked = 0


class Connection:
    """State of the RUDP association with one peer, identified by ``session_id``."""

    def __init__(
        self,
        session_id: int,
        cipher: PacketCipher,
        config: TransportConfig,
        send_datagram: Callable[[bytes, tuple], None],
        peer_addr: tuple | None = None,
        label: str = "",
    ) -> None:
        self.session_id = session_id
        self.cipher = cipher
        self.config = config
        self.peer_addr = peer_addr
        self.label = label or f"{session_id:08x}"
        self._send_datagram = send_datagram
        self._loop = asyncio.get_running_loop()

        self.on_message: MessageHandler | None = None
        self.on_bound: Callable[[Connection], None] | None = None
        self.on_closed: Callable[[Connection, str], None] | None = None
        self._detach: Callable[[Connection], None] | None = None

        now = time.monotonic()
        self.created_at = now
        self.last_recv = now
        self.last_ping = 0.0
        self.received_any = False
        self.closed = False
        self.close_reason: str | None = None

        self.srtt: float | None = None
        self.rttvar = 0.0
        self.rto = config.initial_rto
        self.ping_rtt: float | None = None
        self.cc = make_controller(config.congestion, config.initial_cwnd, config.max_cwnd)
        self._recovery_start = -math.inf
        self._rack_sent = 0.0  # last transmission time of the newest acknowledged packet

        self._send_streams: dict[int, _SendStream] = {}
        self._queues: dict[int, deque[_OutPacket]] = {}
        self._round_robin: deque[int] = deque()
        self._unacked: OrderedDict[tuple[int, int], _OutPacket] = OrderedDict()
        self._recv_streams: dict[int, _RecvStream] = {}
        self._ack_pending: set[int] = set()
        self._ack_timer: asyncio.TimerHandle | None = None
        self._waiters: list[asyncio.Future] = []
        self._next_stream = 1
        self.stats = ConnectionStats()

    # ------------------------------------------------------------------ sending

    def open_stream(self) -> int:
        """Allocate a new send stream id (stream 0 is the control/chat stream)."""
        sid = self._next_stream
        self._next_stream += 1
        return sid

    @property
    def inflight(self) -> int:
        return len(self._unacked)

    def send(self, ptype: int, payload: bytes = b"", stream_id: int = 0) -> None:
        """Queue one reliable message, fragmenting it if it exceeds one datagram."""
        if self.closed:
            raise ConnectionClosed(self.close_reason or "connection closed")
        if not is_reliable(ptype):
            raise ValueError(f"packet type 0x{ptype:02x} is not a reliable message type")
        if len(payload) > MAX_MESSAGE_SIZE:
            raise ValueError("message too large")
        stream = self._send_streams.get(stream_id)
        if stream is None:
            stream = self._send_streams[stream_id] = _SendStream()
        queue = self._queues.get(stream_id)
        if queue is None:
            queue = self._queues[stream_id] = deque()
        was_empty = not queue

        if len(payload) <= MAX_FRAGMENT:
            fragments = [payload]
        else:
            fragments = [
                payload[i : i + MAX_FRAGMENT] for i in range(0, len(payload), MAX_FRAGMENT)
            ]
        last = len(fragments) - 1
        for i, fragment in enumerate(fragments):
            flags = Flags.FIN if i == last else Flags.FRAG
            queue.append(_OutPacket(stream_id, stream.next_seq, ptype, flags, fragment))
            stream.next_seq += 1

        if was_empty and stream_id != 0:
            self._round_robin.append(stream_id)
        self.stats.messages_sent += 1
        self._pump()

    def outstanding(self, stream_id: int) -> int:
        """Packets of a stream that are queued or sent but not yet acknowledged."""
        stream = self._send_streams.get(stream_id)
        return stream.next_seq - stream.base if stream else 0

    async def wait_writable(self, stream_id: int, limit: int) -> None:
        """Wait until at most ``limit`` packets of the stream are outstanding (backpressure)."""
        while True:
            if self.closed:
                raise ConnectionClosed(self.close_reason or "connection closed")
            if self.outstanding(stream_id) <= limit:
                return
            future = self._loop.create_future()
            self._waiters.append(future)
            await future

    def _wake(self) -> None:
        waiters, self._waiters = self._waiters, []
        for future in waiters:
            if not future.done():
                future.set_result(None)

    def _window_open(self, packet: _OutPacket) -> bool:
        stream = self._send_streams[packet.stream_id]
        return packet.seq < stream.base + self.config.recv_window

    def _next_packet(self) -> _OutPacket | None:
        control = self._queues.get(0)
        if control and self._window_open(control[0]):
            return control.popleft()
        for _ in range(len(self._round_robin)):
            sid = self._round_robin[0]
            queue = self._queues[sid]
            if self._window_open(queue[0]):
                packet = queue.popleft()
                if queue:
                    self._round_robin.rotate(-1)
                else:
                    self._round_robin.popleft()
                    del self._queues[sid]
                return packet
            self._round_robin.rotate(-1)
        return None

    def _pump(self) -> None:
        """Send queued packets while the congestion window allows."""
        if self.closed or self.peer_addr is None:
            return
        window = self.cc.window
        while len(self._unacked) < window:
            packet = self._next_packet()
            if packet is None:
                break
            self._unacked[(packet.stream_id, packet.seq)] = packet
            self._transmit(packet)

    def _transmit(self, packet: _OutPacket) -> None:
        now = time.monotonic()
        flags = packet.flags | (Flags.RETX if packet.retx else 0)
        self._emit(
            Header(packet.ptype, flags, self.session_id, packet.stream_id, packet.seq),
            packet.payload,
        )
        if not packet.sent_at:
            packet.sent_at = now
        packet.last_sent = now

    def _retransmit(self, packet: _OutPacket, fast: bool) -> None:
        packet.retx += 1
        self.stats.retransmits += 1
        if fast:
            self.stats.fast_retransmits += 1
        self._unacked.move_to_end((packet.stream_id, packet.seq))
        self._transmit(packet)

    def _emit(self, header: Header, plaintext: bytes) -> None:
        if self.peer_addr is None:
            return
        datagram = seal_packet(self.cipher, header, plaintext)
        self._send_datagram(datagram, self.peer_addr)
        self.stats.packets_sent += 1
        self.stats.bytes_sent += len(datagram)

    def send_ping(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self.last_ping = now
        self._emit(Header(PacketType.PING, 0, self.session_id), _PING.pack(now))

    # ---------------------------------------------------------------- receiving

    def handle_datagram(
        self, header: Header, raw_header: bytes, payload: bytes, addr: tuple
    ) -> None:
        """Entry point for every datagram carrying this connection's session id."""
        if self.closed:
            return
        try:
            plaintext = self.cipher.open(raw_header, payload)
        except CryptoError:
            self.stats.decrypt_failures += 1
            return
        self.stats.packets_received += 1
        self.stats.bytes_received += HEADER_SIZE + len(payload)
        self.last_recv = time.monotonic()

        if addr != self.peer_addr:
            if self.peer_addr is not None:
                log.info("[%s] peer address changed %s -> %s", self.label, self.peer_addr, addr)
            self.peer_addr = addr
            self._pump()
        if not self.received_any:
            self.received_any = True
            if self.on_bound is not None:
                self._call(self.on_bound, self)
            self._pump()

        ptype = header.type
        if ptype == PacketType.ACK:
            self._on_ack(header.stream_id, header.ack, header.sack)
        elif ptype == PacketType.PING:
            self._emit(Header(PacketType.PONG, 0, self.session_id), plaintext)
        elif ptype == PacketType.PONG:
            self._on_pong(plaintext)
        elif ptype == PacketType.BYE:
            try:
                reason = str(decode_json(plaintext).get("reason", "bye"))
            except ValueError:
                reason = "bye"
            self.close(f"peer closed: {reason}", send_bye=False)
        elif is_reliable(ptype):
            self._on_data(header, plaintext)

    def _on_data(self, header: Header, data: bytes) -> None:
        sid, seq = header.stream_id, header.seq
        stream = self._recv_streams.get(sid)
        if stream is None:
            stream = self._recv_streams[sid] = _RecvStream()
        immediate = False
        if seq < stream.expected or seq in stream.buffer:
            self.stats.duplicates += 1
            immediate = True
        elif seq >= stream.expected + self.config.recv_window:
            self.stats.window_drops += 1
            immediate = True
        else:
            if seq != stream.expected:
                self.stats.out_of_order += 1
                immediate = True
            stream.buffer[seq] = (header.type, header.flags, data)
            self._deliver_ready(sid, stream)
            if self.closed:
                return
        self._schedule_ack(sid, stream, immediate)

    def _deliver_ready(self, sid: int, stream: _RecvStream) -> None:
        while stream.expected in stream.buffer:
            ptype, flags, data = stream.buffer.pop(stream.expected)
            stream.expected += 1
            if not flags & Flags.FIN:
                stream.fragments.append(data)
                stream.fragment_bytes += len(data)
                if stream.fragment_bytes > MAX_MESSAGE_SIZE:
                    self.close("protocol violation: message too large")
                    return
                continue
            if stream.fragments:
                stream.fragments.append(data)
                message = b"".join(stream.fragments)
                stream.fragments = []
                stream.fragment_bytes = 0
            else:
                message = data
            self.stats.messages_received += 1
            if self.on_message is not None:
                self._call(self.on_message, self, ptype, sid, message)
            if self.closed:
                return

    def _schedule_ack(self, sid: int, stream: _RecvStream, immediate: bool) -> None:
        self._ack_pending.add(sid)
        stream.unacked += 1
        if immediate or stream.unacked >= 2 or stream.buffer:
            self._flush_acks()
        elif self._ack_timer is None:
            self._ack_timer = self._loop.call_later(self.config.ack_delay, self._flush_acks)

    def _flush_acks(self) -> None:
        if self._ack_timer is not None:
            self._ack_timer.cancel()
            self._ack_timer = None
        if self.closed:
            return
        for sid in self._ack_pending:
            stream = self._recv_streams[sid]
            bitmap = 0
            if stream.buffer:
                base = stream.expected + 1
                for i in range(32):
                    if base + i in stream.buffer:
                        bitmap |= 1 << i
            header = Header(
                PacketType.ACK, Flags.ACK, self.session_id, sid, 0, stream.expected, bitmap
            )
            self._emit(header, b"")
            self.stats.acks_sent += 1
            stream.unacked = 0
        self._ack_pending.clear()

    def _on_ack(self, sid: int, ack: int, bitmap: int) -> None:
        stream = self._send_streams.get(sid)
        if stream is None or ack > stream.next_seq:
            return
        now = time.monotonic()
        acked = 0
        newest_sent = -1.0

        acked_packets = []
        if ack > stream.base:
            for seq in range(stream.base, ack):
                packet = self._unacked.pop((sid, seq), None)
                if packet is not None:
                    acked_packets.append(packet)
            stream.base = ack

        highest = ack - 1
        if bitmap:
            for i in range(32):
                if bitmap >> i & 1:
                    seq = ack + 1 + i
                    highest = seq
                    packet = self._unacked.pop((sid, seq), None)
                    if packet is not None:
                        acked_packets.append(packet)

        for packet in acked_packets:
            acked += 1
            if packet.last_sent > self._rack_sent:
                self._rack_sent = packet.last_sent
            if packet.retx == 0 and packet.sent_at > newest_sent:
                newest_sent = packet.sent_at

        if newest_sent >= 0:
            self._update_rtt(now - newest_sent)
        if acked:
            self.cc.on_ack(acked, self.srtt or self.rto, now)

        # Selective fast retransmit: a hole followed by >= dupthresh SACKed packets is lost.
        threshold = self.srtt if self.srtt is not None else self.rto / 2
        for seq in range(ack, highest - self.config.dupthresh + 1):
            packet = self._unacked.get((sid, seq))
            if packet is None or now - packet.last_sent < threshold:
                continue
            self._declare_lost(packet, now)
        self._detect_lost_by_time(now)

        if acked:
            self._pump()
            self._wake()

    def _declare_lost(self, packet: _OutPacket, now: float) -> None:
        if packet.last_sent > self._recovery_start:
            self.cc.on_loss(now)
            self._recovery_start = now
        self._retransmit(packet, fast=True)

    def _detect_lost_by_time(self, now: float) -> None:
        """RACK-style detection: a packet sent before an acknowledged one is lost once it is
        older than SRTT plus a reordering window. Works even when cwnd is too small for SACK."""
        if self._rack_sent <= 0:
            return
        srtt = self.srtt if self.srtt is not None else self.rto
        deadline = srtt + max(srtt / 4, 0.002)
        lost = []
        for packet in self._unacked.values():  # ordered by last transmission time
            if packet.last_sent >= self._rack_sent or now - packet.last_sent < deadline:
                break
            lost.append(packet)
        for packet in lost:
            self._declare_lost(packet, now)

    def _on_pong(self, payload: bytes) -> None:
        if len(payload) != _PING.size:
            return
        (sent,) = _PING.unpack(payload)
        sample = time.monotonic() - sent
        if 0 <= sample < 60:
            self.ping_rtt = sample
            self._update_rtt(sample)

    def _update_rtt(self, sample: float) -> None:
        if self.srtt is None:
            self.srtt = sample
            self.rttvar = sample / 2
        else:
            self.rttvar = 0.75 * self.rttvar + 0.25 * abs(self.srtt - sample)
            self.srtt = 0.875 * self.srtt + 0.125 * sample
        self.rto = min(max(self.srtt + 4 * self.rttvar, self.config.min_rto), self.config.max_rto)

    # ------------------------------------------------------------------- timers

    def on_tick(self, now: float) -> None:
        """Periodic work: liveness check, heartbeat and retransmission timeouts."""
        if self.closed:
            return
        if now - self.last_recv > self.config.dead_timeout:
            self.close("timeout: no packets from peer", send_bye=False)
            return
        if self.peer_addr is not None:
            interval = (
                self.config.heartbeat_interval
                if self.received_any
                else self.config.bind_probe_interval
            )
            if now - self.last_ping >= interval:
                self.send_ping(now)

        self._detect_lost_by_time(now)
        expired = []
        for packet in self._unacked.values():  # ordered by last transmission time
            if now - packet.last_sent < self.rto:
                break
            expired.append(packet)
        if not expired:
            return
        if any(p.retx >= self.config.max_retransmits for p in expired):
            self.close("too many retransmissions", send_bye=False)
            return
        self.stats.timeouts += 1
        self.cc.on_timeout(now)
        self._recovery_start = now
        self.rto = min(self.rto * 2, self.config.max_rto)
        for packet in expired:
            self._retransmit(packet, fast=False)

    # ------------------------------------------------------------------ closing

    def close(self, reason: str = "closed", send_bye: bool = True) -> None:
        """Close the connection once; optionally notify the peer with BYE (sent twice)."""
        if self.closed:
            return
        if send_bye and self.peer_addr is not None:
            body = encode_json({"reason": reason})
            for _ in range(2):
                self._emit(Header(PacketType.BYE, 0, self.session_id), body)
        self.closed = True
        self.close_reason = reason
        if self._ack_timer is not None:
            self._ack_timer.cancel()
            self._ack_timer = None
        self._unacked.clear()
        self._queues.clear()
        self._round_robin.clear()
        self._wake()
        if self._detach is not None:
            self._detach(self)
        if self.on_closed is not None:
            self._call(self.on_closed, self, reason)

    def _call(self, callback: Callable, *args) -> None:
        try:
            callback(*args)
        except Exception:
            log.exception("[%s] error in connection callback", self.label)

    # -------------------------------------------------------------------- stats

    def snapshot(self) -> dict:
        data = asdict(self.stats)
        sent = max(self.stats.packets_sent, 1)
        data.update(
            srtt_ms=round(self.srtt * 1000, 2) if self.srtt is not None else None,
            ping_ms=round(self.ping_rtt * 1000, 2) if self.ping_rtt is not None else None,
            rto_ms=round(self.rto * 1000, 1),
            cwnd=round(self.cc.cwnd, 1),
            ssthresh=None if math.isinf(self.cc.ssthresh) else round(self.cc.ssthresh, 1),
            congestion=self.cc.name,
            inflight=len(self._unacked),
            queued=sum(len(q) for q in self._queues.values()),
            retransmit_rate=round(self.stats.retransmits / sent, 4),
            peer=f"{self.peer_addr[0]}:{self.peer_addr[1]}" if self.peer_addr else None,
            uptime=round(time.monotonic() - self.created_at, 1),
        )
        return data

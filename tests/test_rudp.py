"""Reliability tests of the RUDP layer, partly through an impaired network (tools.lossy_proxy)."""

import asyncio
import os

import pytest

from streamlink.protocol.constants import PacketType
from streamlink.protocol.packet import Header, encode
from streamlink.transport.config import TransportConfig
from tests.helpers import cipher_pair, endpoint_pair, fast_config, wait_until
from tools.lossy_proxy import LossyProxy


async def _link(config=None, **impairment):
    """Two connected endpoints; A talks to B through a lossy proxy when impairment is given."""
    a, b = await endpoint_pair(config)
    proxy = None
    target = b.local_address[:2]
    if impairment:
        proxy = await LossyProxy(target, seed=7, **impairment).start()
        target = proxy.address
    ca_cipher, cb_cipher = cipher_pair()
    ca = a.create_connection(1, ca_cipher, peer_addr=target)
    cb = b.create_connection(1, cb_cipher)
    return a, b, ca, cb, proxy


def _collect(conn):
    received: dict[int, list[bytes]] = {}

    def on_message(_conn, ptype, stream_id, payload):
        received.setdefault(stream_id, []).append(payload)

    conn.on_message = on_message
    return received


def _numbered(i: int, size: int = 0) -> bytes:
    return i.to_bytes(4, "big") + os.urandom(size)


def _numbers(messages):
    return [int.from_bytes(m[:4], "big") for m in messages]


def test_ordered_delivery_and_fragmentation():
    async def main():
        a, b, ca, cb, _ = await _link()
        received = _collect(cb)
        big = os.urandom(50_000)
        for i in range(200):
            ca.send(PacketType.MSG_ALL, _numbered(i, 100))
        ca.send(PacketType.MSG_ALL, big)
        await wait_until(lambda: len(received.get(0, [])) == 201)
        assert _numbers(received[0][:200]) == list(range(200))
        assert received[0][200] == big
        a.close()
        b.close()

    asyncio.run(main())


@pytest.mark.parametrize("loss", [0.1, 0.2])
def test_reliable_under_loss_reordering_and_duplication(loss):
    async def main():
        a, b, ca, cb, proxy = await _link(loss=loss, delay=0.002, jitter=0.004, duplicate=0.05)
        received = _collect(cb)
        stream = ca.open_stream()
        n = 300
        for i in range(n):
            ca.send(PacketType.MSG_ALL, _numbered(i, 20))
            ca.send(PacketType.FILE_CHUNK, _numbered(i, 1000), stream)
        await wait_until(lambda: sum(map(len, received.values())) == 2 * n, timeout=60)
        assert _numbers(received[0]) == list(range(n))
        assert _numbers(received[stream]) == list(range(n))
        stats = ca.snapshot()
        assert proxy.stats.dropped > 0
        assert stats["retransmits"] > 0
        assert cb.stats.duplicates > 0 or cb.stats.out_of_order > 0
        a.close()
        b.close()
        proxy.close()

    asyncio.run(main())


def test_streams_are_independent():
    """A stalled file stream must not block chat on stream 0 (no head-of-line blocking)."""

    async def main():
        a, b, ca, cb, _ = await _link()
        order = []
        cb.on_message = lambda _c, _t, sid, payload: order.append(payload)
        stream = ca.open_stream()
        # Make the receiver believe packet 0 of the file stream was lost: drop it once.
        original = ca._send_datagram
        dropped = []

        def drop_first_file_packet(data, addr):
            from streamlink.protocol.packet import decode

            header, _, _ = decode(data)
            if header.stream_id == stream and header.seq == 0 and not dropped:
                dropped.append(True)
                return
            original(data, addr)

        ca._send_datagram = drop_first_file_packet
        ca.send(PacketType.FILE_CHUNK, b"chunk-0", stream)
        ca.send(PacketType.FILE_CHUNK, b"chunk-1", stream)
        ca.send(PacketType.MSG_ALL, b"chat")
        await wait_until(lambda: len(order) == 3, timeout=5)
        # chat was sent last but is delivered first: it never waited for the lost file packet
        assert order == [b"chat", b"chunk-0", b"chunk-1"]
        assert ca.stats.retransmits >= 1
        a.close()
        b.close()

    asyncio.run(main())


def test_sliding_window_limits_outstanding_packets():
    async def main():
        config = TransportConfig(recv_window=16)
        a, b, ca, cb, _ = await _link(config)
        received = _collect(cb)
        stream = ca.open_stream()
        for i in range(100):
            ca.send(PacketType.FILE_CHUNK, _numbered(i), stream)
        assert ca.inflight <= 16
        await wait_until(lambda: len(received.get(stream, [])) == 100)
        assert _numbers(received[stream]) == list(range(100))
        a.close()
        b.close()

    asyncio.run(main())


def test_backpressure_wait_writable():
    async def main():
        a, b, ca, cb, _ = await _link()
        _collect(cb)
        stream = ca.open_stream()
        for i in range(500):
            ca.send(PacketType.FILE_CHUNK, _numbered(i, 500), stream)
        assert ca.outstanding(stream) > 10
        await asyncio.wait_for(ca.wait_writable(stream, 10), 5)
        assert ca.outstanding(stream) <= 10
        a.close()
        b.close()

    asyncio.run(main())


def test_dead_peer_detected_and_garbage_ignored():
    async def main():
        config = fast_config()
        a, b, ca, cb, _ = await _link(config)
        closed = []
        ca.on_closed = lambda conn, reason: closed.append(reason)
        ca.send(PacketType.MSG_ALL, b"hello")
        await wait_until(lambda: cb.received_any)
        # garbage and forged packets must be dropped without killing anything
        b.transport.sendto(b"not a packet", ca_addr := a.local_address[:2])
        b.transport.sendto(
            encode(Header(PacketType.MSG_ALL, session_id=1), os.urandom(60)), ca_addr
        )
        b.transport.sendto(encode(Header(PacketType.MSG_ALL, session_id=999), b"x"), ca_addr)
        await asyncio.sleep(0.1)
        assert a.invalid_packets == 1
        assert a.unknown_session == 1
        assert ca.stats.decrypt_failures == 1
        assert not ca.closed
        b.close()  # peer vanishes; BYE is sent, so A closes immediately
        await wait_until(lambda: closed, timeout=3)
        assert "peer closed" in closed[0]
        a.close()

    asyncio.run(main())


def test_silent_peer_times_out():
    async def main():
        config = fast_config()
        a, b, ca, cb, _ = await _link(config)
        closed = []
        ca.on_closed = lambda conn, reason: closed.append(reason)
        ca.send(PacketType.MSG_ALL, b"hello")
        await wait_until(lambda: cb.received_any)
        b.transport.close()  # crash without BYE
        await wait_until(lambda: closed, timeout=5)
        assert closed[0].startswith("timeout") or "retransmissions" in closed[0]
        a.close()

    asyncio.run(main())

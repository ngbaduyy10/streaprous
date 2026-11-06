"""UDP proxy that emulates a bad network: packet loss, delay, jitter (reordering) and duplication.

Clients send UDP to the proxy instead of the server; every client address gets its own upstream
socket so replies find their way back.

    python -m tools.lossy_proxy --listen 9101 --target 127.0.0.1:9001 \
        --loss 0.1 --delay 20 --jitter 10
    python -m streamlink.client cli -u duy --udp-override 127.0.0.1:9101
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import random
from dataclasses import dataclass

log = logging.getLogger("lossy_proxy")


@dataclass
class ProxyStats:
    """Datagram counters across both directions."""

    forwarded: int = 0
    dropped: int = 0
    duplicated: int = 0


class _Upstream(asyncio.DatagramProtocol):
    def __init__(self, proxy: LossyProxy, client_addr: tuple) -> None:
        self.proxy = proxy
        self.client_addr = client_addr
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        downstream = self.proxy.downstream
        if downstream is not None:
            self.proxy.forward(lambda d: downstream.sendto(d, self.client_addr), data)

    def error_received(self, exc: Exception) -> None:
        log.debug("upstream error: %s", exc)


class _Downstream(asyncio.DatagramProtocol):
    def __init__(self, proxy: LossyProxy) -> None:
        self.proxy = proxy

    def connection_made(self, transport) -> None:
        self.proxy.downstream = transport

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        self.proxy.from_client(data, addr)

    def error_received(self, exc: Exception) -> None:
        log.debug("downstream error: %s", exc)


class LossyProxy:
    """Relays UDP between clients and ``target``; delays are in seconds, rates in 0..1."""

    def __init__(
        self,
        target: tuple[str, int],
        listen: tuple[str, int] = ("127.0.0.1", 0),
        loss: float = 0.0,
        delay: float = 0.0,
        jitter: float = 0.0,
        duplicate: float = 0.0,
        seed: int | None = None,
    ) -> None:
        self.target = target
        self.listen = listen
        self.loss = loss
        self.delay = delay
        self.jitter = jitter
        self.duplicate = duplicate
        self.random = random.Random(seed)
        self.stats = ProxyStats()
        self.downstream: asyncio.DatagramTransport | None = None
        self._upstreams: dict[tuple, _Upstream] = {}
        self._pending: dict[tuple, list[bytes]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def address(self) -> tuple[str, int]:
        assert self.downstream is not None
        return self.downstream.get_extra_info("sockname")[:2]

    async def start(self) -> LossyProxy:
        self._loop = asyncio.get_running_loop()
        await self._loop.create_datagram_endpoint(lambda: _Downstream(self), local_addr=self.listen)
        return self

    def close(self) -> None:
        if self.downstream is not None:
            self.downstream.close()
        for upstream in self._upstreams.values():
            if upstream.transport is not None:
                upstream.transport.close()
        self._upstreams.clear()

    def from_client(self, data: bytes, addr: tuple) -> None:
        upstream = self._upstreams.get(addr)
        if upstream is None:
            if addr in self._pending:
                self._pending[addr].append(data)
                return
            self._pending[addr] = [data]
            asyncio.ensure_future(self._open_upstream(addr))
            return
        if upstream.transport is not None:
            self.forward(upstream.transport.sendto, data)

    async def _open_upstream(self, addr: tuple) -> None:
        assert self._loop is not None
        upstream = _Upstream(self, addr)
        await self._loop.create_datagram_endpoint(lambda: upstream, remote_addr=self.target)
        self._upstreams[addr] = upstream
        for data in self._pending.pop(addr, []):
            self.forward(upstream.transport.sendto, data)

    def forward(self, send, data: bytes) -> None:
        """Apply the impairment model to one datagram and send the surviving copies."""
        if self.random.random() < self.loss:
            self.stats.dropped += 1
            return
        copies = 1
        if self.random.random() < self.duplicate:
            copies = 2
            self.stats.duplicated += 1
        for _ in range(copies):
            self.stats.forwarded += 1
            wait = self.delay + self.random.uniform(0, self.jitter)
            if wait <= 0:
                send(data)
            else:
                assert self._loop is not None
                self._loop.call_later(wait, _safe_send, send, data)


def _safe_send(send, data: bytes) -> None:
    try:
        send(data)
    except Exception as exc:  # transport may have been closed meanwhile
        log.debug("delayed send failed: %s", exc)


def _parse_addr(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    return host or "127.0.0.1", int(port)


async def _main(args: argparse.Namespace) -> None:
    proxy = LossyProxy(
        target=_parse_addr(args.target),
        listen=(args.host, args.listen),
        loss=args.loss,
        delay=args.delay / 1000,
        jitter=args.jitter / 1000,
        duplicate=args.duplicate,
    )
    await proxy.start()
    log.info(
        "proxy %s:%s -> %s:%s  loss=%.0f%% delay=%sms jitter=%sms dup=%.0f%%",
        *proxy.address,
        *proxy.target,
        args.loss * 100,
        args.delay,
        args.jitter,
        args.duplicate * 100,
    )
    try:
        while True:
            await asyncio.sleep(5)
            s = proxy.stats
            log.info("forwarded=%d dropped=%d duplicated=%d", s.forwarded, s.dropped, s.duplicated)
    finally:
        proxy.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="UDP network impairment proxy")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--listen", type=int, default=9101, help="UDP port clients send to")
    parser.add_argument("--target", default="127.0.0.1:9001", help="server UDP address")
    parser.add_argument("--loss", type=float, default=0.1, help="drop probability (0-1)")
    parser.add_argument("--delay", type=float, default=20, help="base one-way delay in ms")
    parser.add_argument("--jitter", type=float, default=10, help="random extra delay in ms")
    parser.add_argument("--duplicate", type=float, default=0.02, help="duplication probability")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    try:
        asyncio.run(_main(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

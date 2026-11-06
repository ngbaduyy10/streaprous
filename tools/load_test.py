"""Load test: N concurrent clients exchanging broadcasts, direct messages and files.

By default an embedded server is started in-process; use --server to target a running one.
All clients live in this process, so latency is measured with a shared monotonic clock.

    python -m tools.load_test --clients 20 --messages 20 --file-size 2
    python -m tools.load_test --clients 20 --messages 50 --interval 0      # one burst
    python -m tools.load_test --clients 20 --loss 0.05 --delay 10 --jitter 5
    python -m tools.load_test --server 192.168.1.10 --port 9000
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import statistics
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from streamlink.client.core import ClientCore
from streamlink.server.main import StreamLinkServer
from streamlink.server.users import UserStore
from streamlink.transport.config import TransportConfig
from tools.lossy_proxy import LossyProxy

TERMINAL_STATES = ("done", "failed", "cancelled", "rejected")


@dataclass
class Probe:
    """Collects what one client observed."""

    broadcasts: int = 0
    directs: int = 0
    latencies: list[float] = field(default_factory=list)


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


async def _watch(client: ClientCore, probe: Probe, stop: asyncio.Event) -> None:
    queue = client.events.subscribe(maxsize=100_000)
    try:
        while not stop.is_set():
            try:
                event = await asyncio.wait_for(queue.get(), 0.2)
            except asyncio.TimeoutError:
                continue
            if event.get("type") != "message":
                continue
            text = event.get("text") or ""
            if not text.startswith("load "):
                continue
            sent_at = float(text.split()[-1])
            probe.latencies.append(time.perf_counter() - sent_at)
            if event["kind"] == "all":
                probe.broadcasts += 1
            else:
                probe.directs += 1
    finally:
        client.events.unsubscribe(queue)


async def _wait_for(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(0.05)
    return True


async def run(args: argparse.Namespace) -> int:
    config = TransportConfig(congestion=args.cc)
    server = proxy = None
    host, port, udp_override = args.server, args.port, None
    if host is None:
        users = UserStore(None, iterations=1000)
        server = StreamLinkServer("127.0.0.1", 0, 0, users, config, max_clients=args.clients + 4)
        await server.start()
        host, port = "127.0.0.1", server.tcp_port
        if args.loss or args.delay or args.jitter:
            proxy = LossyProxy(
                ("127.0.0.1", server.udp_port),
                loss=args.loss,
                delay=args.delay / 1000,
                jitter=args.jitter / 1000,
                seed=1,
            )
            await proxy.start()
            udp_override = proxy.address

    workdir = Path(tempfile.mkdtemp(prefix="streamlink-load-"))
    names = [f"load{i:02d}" for i in range(args.clients)]
    clients = [
        ClientCore(workdir / name, config=config, udp_override=udp_override, auto_reconnect=False)
        for name in names
    ]
    probes = [Probe() for _ in clients]
    stop = asyncio.Event()
    failures: list[str] = []

    print(f"== {args.clients} clients -> {host}:{port}  (cc={args.cc}", end="")
    if proxy:
        print(f", loss={args.loss:.0%} delay={args.delay}ms jitter={args.jitter}ms", end="")
    print(")")

    try:
        started = time.perf_counter()
        await asyncio.gather(
            *(c.connect(host, n, args.password, port) for c, n in zip(clients, names))
        )
        print(f"login          : {args.clients} clients in {time.perf_counter() - started:.2f}s")
        watchers = [asyncio.ensure_future(_watch(c, p, stop)) for c, p in zip(clients, probes)]
        if not await _wait_for(
            lambda: all(len(c.users) >= args.clients for c in clients), args.timeout
        ):
            failures.append("presence: not every client saw every other client")
        print(f"presence       : every client sees {min(len(c.users) for c in clients)} users")

        started = time.perf_counter()
        for _ in range(args.messages):
            for client in clients:
                client.send_broadcast(f"load all {time.perf_counter():.6f}")
            for i, client in enumerate(clients):
                peer = names[(i + 1) % len(names)]
                client.send_direct(peer, f"load dm {time.perf_counter():.6f}")
            await asyncio.sleep(args.interval)
        expected_all = args.clients * args.messages
        expected_dm = 2 * args.messages  # one from the left neighbour plus our own echo
        delivered = await _wait_for(
            lambda: all(p.broadcasts >= expected_all and p.directs >= expected_dm for p in probes),
            args.timeout,
        )
        elapsed = time.perf_counter() - started
        got = sum(p.broadcasts for p in probes)
        want = expected_all * args.clients
        latencies = [x * 1000 for p in probes for x in p.latencies]
        print(
            f"broadcast      : {got}/{want} deliveries, "
            f"{sum(p.directs for p in probes)}/{expected_dm * args.clients} DMs in {elapsed:.2f}s"
        )
        print(
            f"latency (ms)   : median {statistics.median(latencies):.1f}  "
            f"p95 {_percentile(latencies, 0.95):.1f}  max {max(latencies):.1f}"
        )
        if not delivered:
            failures.append(f"messages: only {got}/{want} broadcasts delivered")

        if args.file_size > 0:
            size = int(args.file_size * 1024 * 1024)
            source = workdir / "payload.bin"
            source.write_bytes(os.urandom(size))
            pairs = min(args.file_pairs, args.clients // 2)
            started = time.perf_counter()
            outgoing = []
            for i in range(pairs):
                sender, receiver = clients[2 * i], names[2 * i + 1]
                outgoing.append((sender, sender.send_file(source, receiver, name=f"f{i}.bin")))
            await _wait_for(
                lambda: all(c.transfers[t].state in TERMINAL_STATES for c, t in outgoing),
                args.timeout * 6,
            )
            elapsed = time.perf_counter() - started
            ok = sum(c.transfers[t].state == "done" for c, t in outgoing)
            total = pairs * size
            print(
                f"files          : {ok}/{pairs} transfers of {args.file_size:g} MB verified "
                f"(SHA-256) in {elapsed:.2f}s -> {total / elapsed / 1e6:.1f} MB/s aggregate"
            )
            if ok != pairs:
                states = [c.transfers[t].state for c, t in outgoing]
                failures.append(f"files: states {states}")

        snaps = [c.conn.snapshot() for c in clients if c.conn is not None]
        sent = sum(s["packets_sent"] for s in snaps)
        retx = sum(s["retransmits"] for s in snaps)
        print(
            f"client RUDP    : {sent} packets sent, {retx} retransmits "
            f"({retx / max(sent, 1):.2%}), median srtt "
            f"{statistics.median(s['srtt_ms'] or 0 for s in snaps):.1f} ms"
        )
        if proxy:
            s = proxy.stats
            print(f"proxy          : forwarded {s.forwarded}, dropped {s.dropped}")
        stop.set()
        await asyncio.gather(*watchers)
    finally:
        for client in clients:
            await client.disconnect()
        await asyncio.sleep(0.2)
        if proxy:
            proxy.close()
        if server:
            await server.close()

    if failures:
        print("FAIL:", "; ".join(failures))
        return 1
    print("PASS")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="StreamLink load test")
    parser.add_argument("--server", help="server host (default: start an embedded server)")
    parser.add_argument("--port", type=int, default=9000, help="server TCP port")
    parser.add_argument("--password", default="loadtest")
    parser.add_argument("--clients", type=int, default=20)
    parser.add_argument("--messages", type=int, default=20, help="broadcasts per client")
    parser.add_argument(
        "--interval", type=float, default=0.05, help="seconds between rounds (0 = one burst)"
    )
    parser.add_argument("--file-size", type=float, default=2.0, help="MB per file (0 = skip)")
    parser.add_argument("--file-pairs", type=int, default=5, help="concurrent file transfers")
    parser.add_argument("--loss", type=float, default=0.0, help="embedded proxy drop rate")
    parser.add_argument("--delay", type=float, default=0.0, help="embedded proxy delay (ms)")
    parser.add_argument("--jitter", type=float, default=0.0, help="embedded proxy jitter (ms)")
    parser.add_argument("--cc", choices=("cubic", "reno"), default="cubic")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.ERROR)
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()

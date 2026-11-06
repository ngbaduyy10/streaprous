"""Shared fixtures for the async tests (pytest-asyncio is not required)."""

from __future__ import annotations

import asyncio
import os

from streamlink.client.core import ClientCore
from streamlink.crypto.session import PacketCipher
from streamlink.server.main import StreamLinkServer
from streamlink.server.users import UserStore
from streamlink.transport.config import TransportConfig
from streamlink.transport.endpoint import Endpoint


def fast_config(**overrides) -> TransportConfig:
    """Short timers so fault-tolerance tests finish in seconds."""
    values = dict(heartbeat_interval=0.3, dead_timeout=1.5, initial_rto=0.3, max_rto=1.0)
    values.update(overrides)
    return TransportConfig(**values)


def cipher_pair() -> tuple[PacketCipher, PacketCipher]:
    k1, k2 = os.urandom(32), os.urandom(32)
    return PacketCipher(k1, k2), PacketCipher(k2, k1)


async def endpoint_pair(config: TransportConfig | None = None):
    config = config or TransportConfig()
    a = await Endpoint.bind("127.0.0.1", 0, config)
    b = await Endpoint.bind("127.0.0.1", 0, config)
    return a, b


async def start_server(config: TransportConfig | None = None, **kwargs) -> StreamLinkServer:
    users = kwargs.pop("users", None) or UserStore(None, iterations=1000)
    server = StreamLinkServer(
        "127.0.0.1", kwargs.pop("tcp_port", 0), kwargs.pop("udp_port", 0), users, config, **kwargs
    )
    await server.start()
    return server


async def login(server: StreamLinkServer, name: str, tmp_path, config=None, **kwargs) -> ClientCore:
    client = ClientCore(tmp_path / name, config=config, **kwargs)
    await client.connect("127.0.0.1", name, "secret", server.tcp_port)
    return client


async def next_event(queue: asyncio.Queue, predicate, timeout: float = 10.0) -> dict:
    async def wait() -> dict:
        while True:
            event = await queue.get()
            if predicate(event):
                return event

    return await asyncio.wait_for(wait(), timeout)


async def wait_until(predicate, timeout: float = 10.0, interval: float = 0.02) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise asyncio.TimeoutError("condition not reached")
        await asyncio.sleep(interval)

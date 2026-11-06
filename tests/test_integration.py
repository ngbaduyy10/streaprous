"""End-to-end tests: a real server and several clients over loopback."""

import asyncio
import hashlib
import os
import socket

import pytest

from streamlink.client.core import AuthError, ClientCore
from streamlink.protocol.packet import Header, encode
from streamlink.server.users import UserStore
from tests.helpers import fast_config, login, next_event, start_server, wait_until
from tools.lossy_proxy import LossyProxy


def _done(event):
    return event["type"] == "file" and event["state"] in ("done", "failed", "rejected")


def test_login_presence_and_messages(tmp_path):
    async def main():
        server = await start_server()
        duy = await login(server, "duy", tmp_path)
        qa = duy.events.subscribe()
        nguyen = await login(server, "nguyen", tmp_path)
        carol = await login(server, "carol", tmp_path)
        qb, qc = nguyen.events.subscribe(), carol.events.subscribe()

        await wait_until(lambda: set(duy.users) == {"duy", "nguyen", "carol"})
        assert {u["username"] for u in server.registry.user_list()} == {"duy", "nguyen", "carol"}
        session = server.registry.by_name("nguyen")
        assert session.tcp_addr[0] == "127.0.0.1" and session.udp_addr[1] > 0

        duy.send_broadcast("hello everyone")
        for queue in (qa, qb, qc):
            event = await next_event(queue, lambda e: e["type"] == "message")
            assert event["kind"] == "all" and event["from"] == "duy"
            assert event["text"] == "hello everyone"

        nguyen.send_direct("carol", "secret for carol")
        event = await next_event(qc, lambda e: e["type"] == "message")
        assert event["kind"] == "dm" and event["to"] == "carol"
        echo = await next_event(qb, lambda e: e["type"] == "message")
        assert echo["mine"]
        await asyncio.sleep(0.1)
        assert not any(e["type"] == "message" for e in _drain(qa))  # duy never sees the DM

        nguyen.send_direct("nobody", "hello?")
        error = await next_event(qb, lambda e: e["type"] == "error")
        assert error["code"] == "user_offline"

        await carol.disconnect()
        event = await next_event(qa, lambda e: e["type"] == "presence" and e["event"] == "leave")
        assert event["event"] == "leave" and event["user"]["username"] == "carol"
        await duy.disconnect()
        await nguyen.disconnect()
        await server.close()

    asyncio.run(main())


def _drain(queue):
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    return items


def test_wrong_password_and_registration(tmp_path):
    async def main():
        store = UserStore(tmp_path / "users.json", iterations=1000)
        server = await start_server(users=store)
        duy = await login(server, "duy", tmp_path)
        await duy.disconnect()
        assert (tmp_path / "users.json").exists()

        intruder = ClientCore(tmp_path / "x")
        with pytest.raises(AuthError, match="invalid credentials"):
            await intruder.connect("127.0.0.1", "duy", "wrong", server.tcp_port)
        assert intruder.state == "disconnected"
        with pytest.raises(AuthError, match="invalid username"):
            await intruder.connect("127.0.0.1", "bad name!", "pw", server.tcp_port)

        closed = UserStore(None, allow_register=False, iterations=1000)
        server2 = await start_server(users=closed)
        with pytest.raises(AuthError, match="unknown user"):
            await ClientCore(tmp_path / "y").connect("127.0.0.1", "eve", "pw", server2.tcp_port)
        await server.close()
        await server2.close()

    asyncio.run(main())


def test_server_full(tmp_path):
    async def main():
        server = await start_server(max_clients=1)
        duy = await login(server, "duy", tmp_path)
        with pytest.raises(AuthError, match="full"):
            await login(server, "nguyen", tmp_path)
        await duy.disconnect()
        await server.close()

    asyncio.run(main())


@pytest.mark.parametrize("size", [0, 1, 1024, 1025, 700_000])
def test_file_transfer_integrity(tmp_path, size):
    async def main():
        server = await start_server()
        duy = await login(server, "duy", tmp_path)
        nguyen = await login(server, "nguyen", tmp_path)
        qa, qb = duy.events.subscribe(), nguyen.events.subscribe()
        await wait_until(lambda: "nguyen" in duy.users)
        source = tmp_path / "report.pdf"
        data = os.urandom(size)
        source.write_bytes(data)

        tid = duy.send_file(source, "nguyen")
        received = await next_event(qb, _done, 30)
        sent = await next_event(qa, _done, 30)
        assert received["state"] == "done" and sent["state"] == "done"
        path = nguyen.transfers[tid].path
        assert os.path.basename(path) == "report.pdf"
        with open(path, "rb") as f:
            assert hashlib.sha256(f.read()).hexdigest() == hashlib.sha256(data).hexdigest()
        assert not list((tmp_path / "nguyen").glob("*.part"))

        # a second copy with the same name must not overwrite the first
        duy.send_file(source, "nguyen")
        await next_event(qb, _done, 30)
        assert sorted(p.name for p in (tmp_path / "nguyen").iterdir()) == [
            "report (1).pdf",
            "report.pdf",
        ]
        await duy.disconnect()
        await nguyen.disconnect()
        await server.close()

    asyncio.run(main())


def test_file_transfer_over_lossy_network(tmp_path):
    async def main():
        server = await start_server()
        proxy = await LossyProxy(
            ("127.0.0.1", server.udp_port),
            loss=0.1,
            delay=0.001,
            jitter=0.003,
            duplicate=0.03,
            seed=3,
        ).start()
        duy = await login(server, "duy", tmp_path, udp_override=proxy.address)
        nguyen = await login(server, "nguyen", tmp_path, udp_override=proxy.address)
        qb = nguyen.events.subscribe()
        await wait_until(lambda: "nguyen" in duy.users)
        source = tmp_path / "photo.jpg"
        data = os.urandom(300_000)
        source.write_bytes(data)
        tid = duy.send_file(source, "nguyen")
        event = await next_event(qb, _done, 90)
        assert event["state"] == "done", event
        with open(nguyen.transfers[tid].path, "rb") as f:
            assert f.read() == data
        assert proxy.stats.dropped > 50
        assert duy.conn.stats.retransmits > 0
        await duy.disconnect()
        await nguyen.disconnect()
        proxy.close()
        await server.close()

    asyncio.run(main())


def test_transfer_cancelled_when_receiver_disappears(tmp_path):
    async def main():
        config = fast_config()
        server = await start_server(config)
        duy = await login(server, "duy", tmp_path, config=config)
        nguyen = await login(server, "nguyen", tmp_path, config=config, auto_reconnect=False)
        qa = duy.events.subscribe()
        await wait_until(lambda: "nguyen" in duy.users)
        source = tmp_path / "big.bin"
        source.write_bytes(os.urandom(20_000_000))
        duy.send_file(source, "nguyen")
        await next_event(qa, lambda e: e["type"] == "file" and e["state"] == "sending", 10)
        nguyen.endpoint.transport.close()  # nguyen crashes mid-transfer, no BYE
        event = await next_event(qa, _done, 15)
        assert event["state"] == "failed"
        assert "disconnected" in event["error"] or "cancel" in event["error"]
        assert not server.relay.transfers
        assert duy.connected
        await duy.disconnect()
        await server.close()

    asyncio.run(main())


def test_crashed_client_is_removed_and_server_survives_garbage(tmp_path):
    async def main():
        config = fast_config()
        server = await start_server(config)
        duy = await login(server, "duy", tmp_path, config=config)
        nguyen = await login(server, "nguyen", tmp_path, config=config, auto_reconnect=False)
        qa = duy.events.subscribe()
        await wait_until(lambda: "nguyen" in duy.users)

        # garbage on both ports
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for payload in (
            b"",
            b"junk",
            encode(Header(0x22, session_id=12345), b"x" * 50),
            os.urandom(1200),
        ):
            udp.sendto(payload, ("127.0.0.1", server.udp_port))
        udp.close()
        reader, writer = await asyncio.open_connection("127.0.0.1", server.tcp_port)
        writer.write(b"GET / HTTP/1.1\r\n\r\n" + os.urandom(100))
        await writer.drain()
        assert await reader.read() == b""  # server hangs up on a bad handshake
        writer.close()
        _, idle_writer = await asyncio.open_connection("127.0.0.1", server.tcp_port)  # never speaks

        nguyen.endpoint.transport.close()  # crash: no BYE
        event = await next_event(
            qa, lambda e: e["type"] == "presence" and e["event"] == "leave", 10
        )
        assert event["event"] == "leave" and "timeout" in event["reason"]
        assert server.registry.by_name("nguyen") is None

        duy.send_broadcast("still alive")
        event = await next_event(qa, lambda e: e["type"] == "message")
        assert event["text"] == "still alive"
        assert server.endpoint.invalid_packets >= 3
        idle_writer.close()
        await duy.disconnect()
        await server.close()

    asyncio.run(main())


def test_client_reconnects_after_server_restart(tmp_path):
    async def main():
        config = fast_config()
        server = await start_server(config)
        tcp_port, udp_port = server.tcp_port, server.udp_port
        duy = await login(server, "duy", tmp_path, config=config)
        qa = duy.events.subscribe()
        await wait_until(lambda: server.registry.by_name("duy").active)
        await server.close()
        event = await next_event(
            qa, lambda e: e["type"] == "status" and e["state"] == "reconnecting"
        )
        assert "server shutdown" in event["reason"]

        server = await start_server(config, tcp_port=tcp_port, udp_port=udp_port)
        await next_event(
            qa,
            lambda e: e["type"] == "status" and e["state"] == "connected" and e["reconnects"] == 1,
            20,
        )
        assert duy.reconnects == 1
        await wait_until(
            lambda: (
                server.registry.by_name("duy") is not None
                and server.registry.by_name("duy").active
            )
        )
        duy.send_broadcast("back again")
        event = await next_event(qa, lambda e: e["type"] == "message")
        assert event["text"] == "back again"
        await duy.disconnect()
        await server.close()

    asyncio.run(main())


def test_second_login_replaces_first(tmp_path):
    async def main():
        server = await start_server()
        first = await login(server, "duy", tmp_path)
        q1 = first.events.subscribe()
        second = await login(server, "duy", tmp_path)
        event = await next_event(
            q1, lambda e: e["type"] == "status" and e["state"] == "disconnected"
        )
        assert "another place" in event["reason"]
        assert server.registry.by_name("duy").session_id == second.conn.session_id
        assert len(server.registry) == 1
        await second.disconnect()
        await server.close()

    asyncio.run(main())


def test_payloads_are_encrypted_on_the_wire(tmp_path):
    async def main():
        server = await start_server()
        captured = []

        class Sniffer(LossyProxy):
            def forward(self, send, data):
                captured.append(data)
                super().forward(send, data)

        proxy = await Sniffer(("127.0.0.1", server.udp_port)).start()
        duy = await login(server, "duy", tmp_path, udp_override=proxy.address)
        nguyen = await login(server, "nguyen", tmp_path, udp_override=proxy.address)
        qb = nguyen.events.subscribe()
        await wait_until(lambda: "nguyen" in duy.users)
        duy.send_direct("nguyen", "TOP-SECRET-PASSWORD-123")
        source = tmp_path / "plain.txt"
        source.write_bytes(b"VISIBLE-FILE-CONTENT " * 100)
        duy.send_file(source, "nguyen")
        await next_event(qb, _done, 20)
        assert captured
        blob = b"".join(captured)
        assert b"TOP-SECRET-PASSWORD-123" not in blob
        assert b"VISIBLE-FILE-CONTENT" not in blob
        assert b"duy" not in blob and b"nguyen" not in blob
        await duy.disconnect()
        await nguyen.disconnect()
        proxy.close()
        await server.close()

    asyncio.run(main())

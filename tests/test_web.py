"""Tests of the hand-written HTTP gateway (requests are written byte by byte, no HTTP library)."""

from __future__ import annotations

import asyncio
import json
import os

from streamlink.client.core import ClientCore
from streamlink.client.web.http_server import WebGateway
from tests.helpers import login, next_event, start_server, wait_until


async def http(port: int, method: str, path: str, body: bytes = b"", headers: dict | None = None):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    lines = [f"{method} {path} HTTP/1.1", f"Host: 127.0.0.1:{port}", f"Content-Length: {len(body)}"]
    lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode() + body)
    await writer.drain()
    raw = await reader.read()
    writer.close()
    head, _, payload = raw.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    return status, head.decode(), payload


async def post_json(port, path, data, **kw):
    status, _, payload = await http(
        port, "POST", path, json.dumps(data).encode(), {"Content-Type": "application/json", **kw}
    )
    return status, json.loads(payload or b"{}")


async def read_sse_event(reader) -> dict:
    while True:
        line = await reader.readline()
        if line.startswith(b"data: "):
            return json.loads(line[6:])


def test_gateway_end_to_end(tmp_path):
    async def main():
        server = await start_server()
        core = ClientCore(tmp_path / "duy")
        gateway = WebGateway(
            core, port=0, default_server=("127.0.0.1", server.tcp_port), upload_dir=tmp_path / "up"
        )
        await gateway.start()
        port = gateway.port

        status, head, body = await http(port, "GET", "/")
        assert status == 200 and "text/html" in head and b"StreamLink" in body
        status, head, _ = await http(port, "GET", "/static/app.js")
        assert status == 200 and "javascript" in head
        assert (await http(port, "GET", "/static/../../core.py"))[0] == 404
        assert (await http(port, "GET", "/nope"))[0] == 404
        assert (await http(port, "DELETE", "/api/state"))[0] == 405

        status, data = await post_json(
            port, "/api/login", {"username": "duy", "password": "x", "port": server.tcp_port}
        )
        assert status == 200 and data["ok"]
        status, data = await post_json(port, "/api/login", {"username": "duy", "password": "x"})
        assert status == 409

        # live events over Server-Sent Events
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /api/events HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        assert b"text/event-stream" in await reader.readuntil(b"\r\n\r\n")
        snapshot = await read_sse_event(reader)
        assert snapshot["type"] == "snapshot" and snapshot["status"]["state"] == "connected"

        status, data = await post_json(port, "/api/msg", {"text": "hello from the browser"})
        assert status == 200
        while True:
            event = await asyncio.wait_for(read_sse_event(reader), 5)
            if event["type"] == "message":
                break
        assert event["text"] == "hello from the browser" and event["cid"] == data["cid"]

        assert (await post_json(port, "/api/msg", {"text": "  "}))[0] == 400
        assert (await post_json(port, "/api/msg", {"text": "x"}, Origin="http://evil.example"))[
            0
        ] == 403

        # upload a file to nguyen through the gateway, then download what nguyen sent back
        nguyen = await login(server, "nguyen", tmp_path)
        qb = nguyen.events.subscribe()
        await wait_until(lambda: "nguyen" in core.users)
        content = os.urandom(150_000)
        status, _, payload = await http(
            port, "POST", "/api/file?to=nguyen&name=b%C3%A1o%20c%C3%A1o.bin", content
        )
        assert status == 200, payload
        event = await next_event(
            qb, lambda e: e["type"] == "file" and e["state"] in ("done", "failed"), 20
        )
        assert event["state"] == "done" and event["name"] == "báo cáo.bin"
        await wait_until(lambda: not list((tmp_path / "up").iterdir()))  # temporary upload removed

        source = tmp_path / "reply.txt"
        source.write_bytes(b"reply" * 1000)
        tid = nguyen.send_file(source, "duy")
        await wait_until(lambda: tid in core.transfers and core.transfers[tid].state == "done", 20)
        status, head, body = await http(port, "GET", f"/files/{tid}")
        assert status == 200 and body == source.read_bytes()
        assert "filename*=UTF-8''reply.txt" in head

        # malformed requests never crash the gateway
        reader2, writer2 = await asyncio.open_connection("127.0.0.1", port)
        writer2.write(b"garbage\r\n\r\n")
        await writer2.drain()
        assert b"400" in await reader2.read()
        status, _ = await post_json(port, "/api/logout", {})
        assert status == 200 and core.state == "disconnected"

        writer.close()
        await nguyen.disconnect()
        await gateway.close()
        await server.close()

    asyncio.run(main())


def test_login_errors_are_reported(tmp_path):
    async def main():
        server = await start_server()
        await login(server, "carol", tmp_path)
        gateway = WebGateway(
            ClientCore(tmp_path / "x"), port=0, default_server=("127.0.0.1", server.tcp_port)
        )
        await gateway.start()
        status, data = await post_json(
            gateway.port, "/api/login", {"username": "carol", "password": "wrong"}
        )
        assert status == 401 and "invalid credentials" in data["error"]
        status, data = await post_json(
            gateway.port, "/api/login", {"username": "dave", "password": "pw", "port": 1}
        )
        assert status == 502
        await gateway.close()
        await server.close()

    asyncio.run(main())

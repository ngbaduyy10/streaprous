"""Minimal HTTP/1.1 server written directly on asyncio streams (raw TCP sockets).

It serves the static web UI and a small JSON API backed by ``ClientCore``. Live updates go to
the browser with Server-Sent Events (``text/event-stream``), also produced by hand. Every
response closes the connection, except the long-lived event stream.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlsplit

from streamlink.client.core import AuthError, ClientCore, NotConnected
from streamlink.protocol.constants import DEFAULT_TCP_PORT, MAX_FILE_SIZE
from streamlink.protocol.messages import safe_filename

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
MAX_HEADER_BYTES = 16 * 1024
MAX_JSON_BODY = 64 * 1024
KEEPALIVE_INTERVAL = 15.0

STATUS_TEXT = {
    200: "OK",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    411: "Length Required",
    413: "Payload Too Large",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
    501: "Not Implemented",
    502: "Bad Gateway",
}

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


class HttpError(Exception):
    """Aborts request handling with an HTTP status and a JSON error body."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Request:
    """Parsed request line and headers; the body is still unread on the stream."""

    method: str
    path: str
    query: dict[str, str]
    version: str
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def content_length(self) -> int:
        if "transfer-encoding" in self.headers:
            raise HttpError(501, "chunked request bodies are not supported")
        value = self.headers.get("content-length", "0")
        if not value.isdigit():
            raise HttpError(400, "invalid Content-Length")
        return int(value)


async def read_request(reader: asyncio.StreamReader) -> Request:
    """Parse the request line and headers; the body is left in the stream."""
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except asyncio.LimitOverrunError as exc:
        raise HttpError(431, "headers too large") from exc
    except asyncio.IncompleteReadError as exc:
        raise ConnectionError("client closed the connection") from exc
    if len(head) > MAX_HEADER_BYTES:
        raise HttpError(431, "headers too large")
    lines = head.decode("iso-8859-1").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        raise HttpError(400, "malformed request line")
    method, target, version = parts
    headers = {}
    for line in lines[1:]:
        if not line:
            continue
        name, sep, value = line.partition(":")
        if not sep or not name.strip():
            raise HttpError(400, "malformed header line")
        headers[name.strip().lower()] = value.strip()
    url = urlsplit(target)
    return Request(method.upper(), unquote(url.path), dict(parse_qsl(url.query)), version, headers)


async def read_json(reader: asyncio.StreamReader, request: Request) -> dict:
    length = request.content_length
    if length > MAX_JSON_BODY:
        raise HttpError(413, "body too large")
    raw = await reader.readexactly(length) if length else b"{}"
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HttpError(400, "body must be JSON") from exc
    if not isinstance(body, dict):
        raise HttpError(400, "body must be a JSON object")
    return body


def build_response(
    status: int,
    body: bytes = b"",
    content_type: str = "text/plain",
    headers: dict | None = None,
    length: int | None = None,
) -> bytes:
    """Serialize a response. ``length`` overrides Content-Length when the body is streamed later."""
    lines = [
        f"HTTP/1.1 {status} {STATUS_TEXT.get(status, 'Unknown')}",
        f"Content-Type: {content_type}",
        f"Content-Length: {len(body) if length is None else length}",
        "Connection: close",
        "X-Content-Type-Options: nosniff",
        "Cache-Control: no-store",
    ]
    for name, value in (headers or {}).items():
        lines.append(f"{name}: {value}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + body


def json_response(status: int, data: dict) -> bytes:
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    return build_response(status, body, "application/json; charset=utf-8")


def sse_event(data: dict) -> bytes:
    return b"data: " + json.dumps(data, ensure_ascii=False).encode("utf-8") + b"\n\n"


class WebGateway:
    """Bridges a browser on localhost to ``ClientCore``."""

    def __init__(
        self,
        core: ClientCore,
        host: str = "127.0.0.1",
        port: int = 8080,
        default_server: tuple[str, int] = ("127.0.0.1", DEFAULT_TCP_PORT),
        upload_dir: str | os.PathLike = "uploads",
    ) -> None:
        self.core = core
        self.host = host
        self.port = port
        self.default_server = default_server
        self.upload_dir = Path(upload_dir)
        self._server: asyncio.base_events.Server | None = None
        self._static = {p.name: p for p in STATIC_DIR.iterdir() if p.is_file()}

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, self.host, self.port, limit=MAX_HEADER_BYTES
        )
        self.port = self._server.sockets[0].getsockname()[1]
        log.info("web UI at http://%s:%d/", self.host, self.port)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                request = await asyncio.wait_for(read_request(reader), 30)
            except asyncio.TimeoutError:
                return
            await self._dispatch(request, reader, writer)
        except HttpError as exc:
            await self._write(writer, json_response(exc.status, {"error": exc.message}))
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception:
            log.exception("error while serving HTTP request")
            await self._write(writer, json_response(500, {"error": "internal error"}))
        finally:
            writer.close()

    @staticmethod
    async def _write(writer: asyncio.StreamWriter, data: bytes) -> None:
        try:
            writer.write(data)
            await writer.drain()
        except (ConnectionError, OSError):
            pass

    def _check_origin(self, request: Request) -> None:
        """Reject cross-site POSTs: another web page must not drive this local client."""
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.headers.get("host"):
            raise HttpError(403, "cross-origin request refused")

    async def _dispatch(self, request: Request, reader, writer) -> None:
        method, path = request.method, request.path
        if method == "GET":
            if path in ("/", "/index.html"):
                return await self._static_file(writer, "index.html")
            if path.startswith("/static/"):
                return await self._static_file(writer, path[len("/static/") :])
            if path == "/api/state":
                return await self._write(writer, json_response(200, self._snapshot()))
            if path == "/api/events":
                return await self._event_stream(writer)
            if path.startswith("/files/"):
                return await self._download(writer, path[len("/files/") :])
            raise HttpError(404, "not found")
        if method != "POST":
            raise HttpError(405, "method not allowed")
        self._check_origin(request)
        if path == "/api/file":
            result = await self._upload(request, reader)
        else:
            body = await read_json(reader, request)
            handlers = {
                "/api/login": self._login,
                "/api/logout": self._logout,
                "/api/msg": self._message,
                "/api/file/cancel": self._cancel,
                "/api/users/refresh": self._refresh,
            }
            handler = handlers.get(path)
            if handler is None:
                raise HttpError(404, "not found")
            result = await handler(body)
        await self._write(writer, json_response(200, result))

    def _snapshot(self) -> dict:
        data = self.core.snapshot()
        data["defaults"] = {"host": self.default_server[0], "port": self.default_server[1]}
        return data

    # API handlers -------------------------------------------------------------

    async def _login(self, body: dict) -> dict:
        if self.core.state != "disconnected":
            raise HttpError(409, f"already {self.core.state}")
        host = str(body.get("host") or self.default_server[0])
        try:
            port = int(body.get("port") or self.default_server[1])
        except (TypeError, ValueError) as exc:
            raise HttpError(400, "invalid port") from exc
        username, password = body.get("username"), body.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            raise HttpError(400, "username and password are required")
        try:
            await self.core.connect(host, username, password, port)
        except AuthError as exc:
            raise HttpError(401, str(exc)) from exc
        except (OSError, asyncio.TimeoutError) as exc:
            raise HttpError(502, f"cannot reach server {host}:{port}: {exc or 'timeout'}") from exc
        return {"ok": True, "username": username}

    async def _logout(self, body: dict) -> dict:
        await self.core.disconnect()
        return {"ok": True}

    async def _message(self, body: dict) -> dict:
        to, text = body.get("to"), body.get("text")
        try:
            cid = self.core.send_direct(to, text) if to else self.core.send_broadcast(text)
        except NotConnected as exc:
            raise HttpError(409, str(exc)) from exc
        except ValueError as exc:
            raise HttpError(400, str(exc)) from exc
        return {"ok": True, "cid": cid}

    async def _cancel(self, body: dict) -> dict:
        tid = body.get("id")
        if tid not in self.core.transfers:
            raise HttpError(404, "unknown transfer")
        self.core.cancel_transfer(tid)
        return {"ok": True}

    async def _refresh(self, body: dict) -> dict:
        try:
            self.core.request_users()
        except NotConnected as exc:
            raise HttpError(409, str(exc)) from exc
        return {"ok": True}

    async def _upload(self, request: Request, reader: asyncio.StreamReader) -> dict:
        """Receive the raw file body, store it in ``upload_dir``, then hand it to ClientCore."""
        to = request.query.get("to")
        name = safe_filename(request.query.get("name"))
        length = request.content_length
        if not to:
            raise HttpError(400, "missing recipient")
        if length > MAX_FILE_SIZE:
            raise HttpError(413, "file larger than 2 GiB")
        if not self.core.connected:
            raise HttpError(409, "not connected to the server")
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        path = self.upload_dir / f"{uuid.uuid4().hex}_{name}"
        try:
            with open(path, "wb") as f:
                remaining = length
                while remaining:
                    chunk = await reader.read(min(256 * 1024, remaining))
                    if not chunk:
                        raise ConnectionError("upload interrupted")
                    f.write(chunk)
                    remaining -= len(chunk)
            tid = self.core.send_file(path, to, name=name, cleanup=True)
        except BaseException as exc:
            try:
                path.unlink()
            except OSError:
                pass
            if isinstance(exc, (ValueError, NotConnected, FileNotFoundError)):
                raise HttpError(400, str(exc)) from exc
            raise
        return {"ok": True, "id": tid}

    # streaming responses --------------------------------------------------------

    async def _static_file(self, writer, name: str) -> None:
        path = self._static.get(name)
        if path is None:
            raise HttpError(404, "not found")
        content_type = CONTENT_TYPES.get(path.suffix, "application/octet-stream")
        await self._write(writer, build_response(200, path.read_bytes(), content_type))

    async def _download(self, writer, transfer_id: str) -> None:
        transfer = self.core.transfers.get(transfer_id)
        if (
            transfer is None
            or transfer.direction != "in"
            or transfer.state != "done"
            or not transfer.path
        ):
            raise HttpError(404, "file not available")
        path = Path(transfer.path)
        if not path.is_file():
            raise HttpError(404, "file was removed")
        filename = path.name
        fallback = filename.encode("ascii", "replace").decode("ascii").replace('"', "_")
        disposition = f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename)}"
        writer.write(
            build_response(
                200,
                content_type="application/octet-stream",
                headers={"Content-Disposition": disposition},
                length=path.stat().st_size,
            )
        )
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(256 * 1024), b""):
                writer.write(block)
                await writer.drain()

    async def _event_stream(self, writer: asyncio.StreamWriter) -> None:
        queue = self.core.events.subscribe()
        try:
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/event-stream; charset=utf-8\r\n"
                b"Cache-Control: no-store\r\n"
                b"Connection: keep-alive\r\n"
                b"X-Content-Type-Options: nosniff\r\n\r\n"
                b"retry: 2000\n\n"
            )
            writer.write(sse_event(self._snapshot()))
            await writer.drain()
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), KEEPALIVE_INTERVAL)
                except asyncio.TimeoutError:
                    writer.write(b": keepalive\n\n")
                else:
                    writer.write(sse_event(event))
                await writer.drain()
        finally:
            self.core.events.unsubscribe(queue)

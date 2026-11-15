"""Tests for request parsing and whole-request reading in the HTTP side."""

import hashlib
import io
import unittest
from contextlib import redirect_stdout
from unittest import mock

import tests  # noqa: F401  (installs the netifaces stand-in)
from daemon import httpadapter
from daemon.request import Request
from streaprous_socket import tcp_config

POST = b"POST /echo HTTP/1.1\r\nHost: x\r\nContent-Length: 10\r\n\r\n0123456789"


class FakeInfo(object):
    """Connection info stand-in."""

    client_ip = "1.2.3.4"
    client_port = 5555


class FakeStream(object):
    """Fake run_conn_recv that hands out a list of chunks, then b""."""

    def __init__(self, chunks):
        """Keep the chunks to hand out.

        Args:
            chunks: list of bytes returned one per call.
        """
        self.chunks = list(chunks)
        self.calls = []

    def __call__(self, sock, info, max_bytes=None, timeout=None):
        """Return the next chunk, cut to max_bytes.

        Args:
            sock: unused.
            info: unused.
            max_bytes: largest chunk to return, or None.
            timeout: recorded only.

        Returns:
            bytes, b"" when no chunk is left.
        """
        self.calls.append((max_bytes, timeout))
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        if max_bytes is not None and len(chunk) > max_bytes:
            self.chunks.insert(0, chunk[max_bytes:])
            chunk = chunk[:max_bytes]
        return chunk


def make_adapter():
    """Return an HttpAdapter for the tests."""
    return httpadapter.HttpAdapter("1.2.3.4", 5555, None, None, {})


class Env(unittest.TestCase):
    """Patches the three connection functions and SETTINGS."""

    def setUp(self):
        """Install the fakes."""
        self.sent = []
        self.closed = []
        self.stream = FakeStream([])
        patches = [
            mock.patch.object(httpadapter, "run_conn_recv",
                              lambda *a, **k: self.stream(*a, **k)),
            mock.patch.object(httpadapter, "run_conn_send",
                              lambda s, i, p: self.sent.append(p)),
            mock.patch.object(httpadapter, "close_connection",
                              lambda s, i: self.closed.append(1)),
            mock.patch.object(tcp_config.SETTINGS, "app_read_delay", 0.0),
            mock.patch.object(tcp_config.SETTINGS, "app_read_bytes", 0),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def read(self):
        """Run read_request_raw with output captured."""
        out = io.StringIO()
        with redirect_stdout(out):
            data = make_adapter().read_request_raw(None, FakeInfo())
        return data, out.getvalue()

    def handle(self, chunks):
        """Run handle_client_raw on the chunks.

        Args:
            chunks: list of bytes the fake stream delivers.
        """
        self.stream.chunks = list(chunks)
        with redirect_stdout(io.StringIO()):
            make_adapter().handle_client_raw(None, FakeInfo(), {})


class TestPrepare(unittest.TestCase):
    """Request.prepare."""

    def test_fields(self):
        """Method, path, headers and body are filled."""
        req = Request()
        with redirect_stdout(io.StringIO()):
            req.prepare(POST.decode(), {})
        self.assertEqual(req.method, "POST")
        self.assertEqual(req.path, "/echo")
        self.assertEqual(req.headers["host"], "x")
        self.assertEqual(req.headers["content-length"], "10")
        self.assertEqual(req.body, "0123456789")


class TestRead(Env):
    """read_request_raw."""

    def test_small_chunks(self):
        """One byte at a time gives the whole request."""
        self.stream.chunks = [POST[i:i + 1] for i in range(len(POST))]
        data, _ = self.read()
        self.assertEqual(data, POST)

    def test_header_end_split(self):
        """The blank line may be split across chunks."""
        cut = POST.index(b"\r\n\r\n") + 2
        self.stream.chunks = [POST[:cut], POST[cut:cut + 1], POST[cut + 1:]]
        data, _ = self.read()
        self.assertEqual(data, POST)

    def test_body_max_bytes_and_delay(self):
        """app_read_bytes caps each body read and app_read_delay sleeps before it."""
        cut = POST.index(b"\r\n\r\n") + 4
        self.stream.chunks = [POST[:cut], POST[cut:]]
        with mock.patch.object(tcp_config.SETTINGS, "app_read_bytes", 4), \
                mock.patch.object(tcp_config.SETTINGS, "app_read_delay", 0.5), \
                mock.patch.object(httpadapter.time, "sleep") as sleep:
            data, _ = self.read()
        self.assertEqual(data, POST)
        self.assertEqual([c[0] for c in self.stream.calls], [None, 4, 4, 4])
        self.assertEqual(sleep.call_count, 3)
        sleep.assert_called_with(0.5)

    def test_timeout_passed(self):
        """The request timeout is passed to every read."""
        self.stream.chunks = [POST]
        self.read()
        self.assertEqual(self.stream.calls[0][1], tcp_config.SETTINGS.request_timeout)

    def test_closed_before_any_byte(self):
        """No byte at all returns b""."""
        data, _ = self.read()
        self.assertEqual(data, b"")

    def test_short_body_stops_on_close(self):
        """A connection that closes mid-body returns what arrived."""
        self.stream.chunks = [POST[:-4]]
        data, _ = self.read()
        self.assertEqual(data, POST[:-4])

    def test_bad_content_length(self):
        """A bad Content-Length counts as 0."""
        raw = b"GET / HTTP/1.1\r\ncontent-length: abc\r\n\r\n"
        self.stream.chunks = [raw]
        data, _ = self.read()
        self.assertEqual(data, raw)

    def test_log_line(self):
        """The Read request line has the agreed format."""
        self.stream.chunks = [POST]
        _, out = self.read()
        head = len(POST) - 4 - 10
        digest = hashlib.sha256(b"0123456789").hexdigest()
        self.assertIn("[HttpAdapter] Read request: header {} bytes, body 10 bytes, "
                      "sha256 {}".format(head, digest), out)


class TestHandle(Env):
    """handle_client_raw."""

    def test_closed_without_data(self):
        """Closed connection: close once, send nothing."""
        self.handle([])
        self.assertEqual(self.sent, [])
        self.assertEqual(len(self.closed), 1)

    def test_binary_body(self):
        """A binary body does not crash and the response is sent."""
        body = bytes(range(256))
        raw = b"POST /x HTTP/1.1\r\nContent-Length: 256\r\n\r\n" + body
        self.handle([raw])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.closed), 1)

    def test_malformed_request_line(self):
        """A bad request line gets 400."""
        self.handle([b"BROKEN\r\n\r\n"])
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.sent[0].startswith(b"HTTP/1.1 400 Bad Request"))
        self.assertIn(b"Content-Length: 0", self.sent[0])
        self.assertIn(b"Connection: close", self.sent[0])
        self.assertEqual(len(self.closed), 1)

    def test_send_failure_still_closes(self):
        """close_connection runs once even when sending raises."""
        def boom(s, i, p):
            raise OSError("x")
        self.stream.chunks = [b"GET / HTTP/1.1\r\n\r\n"]
        with mock.patch.object(httpadapter, "run_conn_send", boom):
            with redirect_stdout(io.StringIO()):
                with self.assertRaises(OSError):
                    make_adapter().handle_client_raw(None, FakeInfo(), {})
        self.assertEqual(len(self.closed), 1)

    def test_good_request_closes_once(self):
        """A normal request sends once and closes once."""
        self.handle([b"GET / HTTP/1.1\r\nHost: x\r\n\r\n"])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.closed), 1)


if __name__ == "__main__":
    unittest.main()

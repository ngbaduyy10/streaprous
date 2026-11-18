"""
test_demo_client
~~~~~~~~~~~~~~~~

Tests of the demo client's pure parts (URL, request, response head, RESULT line)
and of the deterministic demo files. No sockets are opened.
"""

import hashlib
import os
import sys
import unittest

STREAP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(STREAP_DIR, 'demo'))

import client  # noqa: E402
import make_files  # noqa: E402


class ParseUrlTest(unittest.TestCase):
    """parse_url."""

    def test_full_url(self):
        self.assertEqual(client.parse_url('http://172.28.0.10:9000/demo_1m.bin'),
                         ('172.28.0.10', 9000, '/demo_1m.bin'))

    def test_defaults(self):
        self.assertEqual(client.parse_url('http://example.test'), ('example.test', 80, '/'))

    def test_query_is_kept(self):
        self.assertEqual(client.parse_url('http://h:1/a?x=1'), ('h', 1, '/a?x=1'))

    def test_rejects_other_schemes(self):
        with self.assertRaises(ValueError):
            client.parse_url('ftp://h/x')


class BuildRequestTest(unittest.TestCase):
    """build_request."""

    def test_plain_get(self):
        data = client.build_request('GET', '172.28.0.10', 9000, '/')
        self.assertTrue(data.startswith(b'GET / HTTP/1.1\r\nHost: 172.28.0.10:9000\r\n'))
        self.assertIn(b'Connection: close\r\n', data)
        self.assertTrue(data.endswith(b'\r\n\r\n'))
        self.assertNotIn(b'X-Pad-', data)

    def test_header_bytes_pads_the_head(self):
        for wanted in (500, 3000, 3200):
            data = client.build_request('GET', 'h', 9000, '/', header_bytes=wanted)
            head_length = data.index(b'\r\n\r\n') + 4
            self.assertGreaterEqual(head_length, wanted)
            self.assertEqual(len(data), head_length)
        data = client.build_request('GET', 'h', 9000, '/', header_bytes=3000)
        self.assertIn(b'X-Pad-0: ' + b'x' * client.PAD_LENGTH + b'\r\n', data)
        self.assertIn(b'X-Pad-2: ', data)
        self.assertNotIn(b'X-Pad-5: ', data)

    def test_post_has_length_and_body(self):
        body = client.post_body(300)
        data = client.build_request('POST', 'h', 9000, '/', body)
        head, _sep, rest = data.partition(b'\r\n\r\n')
        self.assertTrue(head.startswith(b'POST / HTTP/1.1'))
        self.assertIn(b'Content-Length: 300', head)
        self.assertEqual(rest, body)


class PostBodyTest(unittest.TestCase):
    """post_body."""

    def test_size_and_determinism(self):
        for size in (0, 1, 65, 150000):
            self.assertEqual(len(client.post_body(size)), size)
        self.assertEqual(client.post_body(1000), client.post_body(1000))

    def test_printable(self):
        text = client.post_body(5000)
        self.assertTrue(all(32 <= b < 127 or b == 10 for b in text))

    def test_known_prefix(self):
        digest = hashlib.sha256(client.post_body(100)).hexdigest()
        self.assertTrue(digest.startswith('1ee6f34d801eb50e'), digest)


class ResponseHeadTest(unittest.TestCase):
    """parse_response_head."""

    def test_complete_head(self):
        raw = (b'HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 5\r\n\r\nhello')
        status, headers, length = client.parse_response_head(raw)
        self.assertEqual(status, 200)
        self.assertEqual(headers['content-length'], '5')
        self.assertEqual(headers['content-type'], 'text/html')
        self.assertEqual(raw[length:], b'hello')

    def test_incomplete_head(self):
        self.assertIsNone(client.parse_response_head(b'HTTP/1.1 200 OK\r\nContent-Le'))

    def test_bad_status_line(self):
        with self.assertRaises(ValueError):
            client.parse_response_head(b'garbage\r\n\r\n')

    def test_response_ok(self):
        good = {'status': 200, 'body_bytes': 5, 'expected': 5}
        self.assertTrue(client.response_ok(good))
        self.assertTrue(client.response_ok({'status': 200, 'body_bytes': 5, 'expected': None}))
        self.assertFalse(client.response_ok({'status': 200, 'body_bytes': 4, 'expected': 5}))
        self.assertFalse(client.response_ok({'status': 404, 'body_bytes': 5, 'expected': 5}))


class ResultLineTest(unittest.TestCase):
    """format_result."""

    def test_format(self):
        line = client.format_result([('method', 'GET'), ('status', 200), ('expect', 'ok')])
        self.assertEqual(line, 'RESULT method=GET status=200 expect=ok')

    def test_round_trip_through_parse_result(self):
        sys.path.insert(0, os.path.join(STREAP_DIR, 'docker'))
        import checks
        line = client.format_result([('method', 'POST'), ('sent_bytes', 10), ('handshake', 'ok')])
        self.assertEqual(checks.parse_result('x\n' + line + '\n'),
                         {'method': 'POST', 'sent_bytes': '10', 'handshake': 'ok'})


class ConnectCloseParserTest(unittest.TestCase):
    """The connect-close command line."""

    def test_host_and_port_only(self):
        args = client.build_parser().parse_args(['connect-close', '172.28.0.10', '9000'])
        self.assertEqual((args.host, args.port), ('172.28.0.10', 9000))

    def test_fin_check_flag_is_gone(self):
        with self.assertRaises(SystemExit):
            client.build_parser().parse_args(
                ['connect-close', '172.28.0.10', '9000', '--no-fin-check'])


class DemoContentTest(unittest.TestCase):
    """make_files.demo_content."""

    def test_sizes(self):
        self.assertEqual(len(make_files.demo_content('demo_256k.bin')), 262144)
        self.assertEqual(len(make_files.demo_content('demo_1m.bin')), 1048576)

    def test_same_bytes_twice(self):
        self.assertEqual(make_files.demo_content('demo_256k.bin'),
                         make_files.demo_content('demo_256k.bin'))

    def test_first_block_follows_the_recipe(self):
        data = make_files.demo_content('demo_1m.bin')
        first = hashlib.sha256(b'streaprous-demo:demo_1m.bin:0').digest()
        second = hashlib.sha256(b'streaprous-demo:demo_1m.bin:1').digest()
        self.assertEqual(data[:64], first + second)

    def test_known_sha256_prefixes(self):
        small = hashlib.sha256(make_files.demo_content('demo_256k.bin')).hexdigest()
        big = hashlib.sha256(make_files.demo_content('demo_1m.bin')).hexdigest()
        self.assertTrue(small.startswith('ad8642f375339abc'), small)
        self.assertTrue(big.startswith('3dbbefab3a76b401'), big)

    def test_unknown_name(self):
        with self.assertRaises(ValueError):
            make_files.demo_content('demo_2m.bin')


if __name__ == '__main__':
    unittest.main()

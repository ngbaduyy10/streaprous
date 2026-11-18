"""Tests for streaprous_socket.tcp_segment."""

import json
import os
import struct
import unittest

from streaprous_socket import tcp_segment
from streaprous_socket.tcp_segment import (
    ACK, FIN, PSH, RST, SYN, parse_mss_option, parse_tcp_packet, unwrap, wrap)
from tests.slide_rules import assert_slide_rules

SEGMENT_RULES = [('unwrap', '3-78', 'counting bytes of data into bytestream')]

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       'fixtures', 'docker_client_packets.json')


def load_packets():
    """Returns the fixture packets as {name: (bytes, expected fields)}."""
    with open(FIXTURE) as f:
        data = json.load(f)
    return {p['name']: (bytes.fromhex(p['hex']), p['expect']) for p in data['packets']}


def make_packet(proto=6, ihl=5, offset=5, payload=b'', total_len=None):
    """Builds a small IPv4 packet, with a TCP header when proto is 6."""
    tcp = struct.pack('!HHLLBBHHH', 1000, 2000, 1, 2, offset << 4, ACK, 100, 0, 0)
    tcp += b'\x00' * max(0, (offset - 5) * 4) + payload
    ip = bytearray(ihl * 4)
    ip[0] = 0x40 | ihl
    ip[9] = proto
    length = ihl * 4 + len(tcp) if total_len is None else total_len
    ip[2:4] = struct.pack('!H', length)
    ip[12:16] = bytes([10, 0, 0, 1])
    ip[16:20] = bytes([10, 0, 0, 2])
    return bytes(ip) + tcp


class FixtureTest(unittest.TestCase):
    """Real client packets captured in Docker."""

    def test_every_packet(self):
        packets = load_packets()
        self.assertGreaterEqual(len(packets), 4)
        for name, (raw, expect) in packets.items():
            seg = parse_tcp_packet(raw)
            self.assertIsNotNone(seg, name)
            self.assertEqual(seg.src_ip, expect['src_ip'], name)
            self.assertEqual(seg.dst_ip, expect['dst_ip'], name)
            self.assertEqual(seg.src_port, expect['src_port'], name)
            self.assertEqual(seg.dst_port, expect['dst_port'], name)
            self.assertEqual(seg.seq, expect['seq'], name)
            self.assertEqual(seg.ack, expect['ack'], name)
            self.assertEqual(seg.flags, expect['flags'], name)
            self.assertEqual(seg.window, expect['window'], name)
            self.assertEqual(len(seg.payload), expect['payload_len'], name)

    def test_mss_option(self):
        packets = load_packets()
        for name, (raw, expect) in packets.items():
            ihl = (raw[0] & 0x0f) * 4
            self.assertEqual(parse_mss_option(raw[ihl:]), expect['mss_option'], name)
        self.assertEqual(parse_mss_option(packets['syn'][0][20:]), 1460)

    def test_syn_flags(self):
        seg = parse_tcp_packet(load_packets()['syn'][0])
        self.assertEqual(seg.flags, SYN)
        self.assertEqual(seg.payload, b'')

    def test_request_payload(self):
        seg = parse_tcp_packet(load_packets()['get_request'][0])
        self.assertTrue(seg.payload.startswith(b'GET / HTTP/1.1\r\n'))
        self.assertTrue(seg.payload.endswith(b'\r\n\r\n'))

    def test_merged_segment_is_longer_than_mss(self):
        seg = parse_tcp_packet(load_packets()['merged_data'][0])
        self.assertEqual(len(seg.payload), 1608)


class ParseEdgeTest(unittest.TestCase):
    """Packets that are not complete IPv4 TCP packets give None."""

    def test_valid_helper_packet(self):
        seg = parse_tcp_packet(make_packet(payload=b'abc'))
        self.assertEqual(seg.payload, b'abc')
        self.assertEqual((seg.src_port, seg.dst_port), (1000, 2000))

    def test_short(self):
        self.assertIsNone(parse_tcp_packet(b''))
        self.assertIsNone(parse_tcp_packet(b'\x45' * 19))
        self.assertIsNone(parse_tcp_packet(make_packet()[:30]))

    def test_not_tcp(self):
        self.assertIsNone(parse_tcp_packet(make_packet(proto=17)))

    def test_not_ipv4(self):
        raw = bytearray(make_packet())
        raw[0] = 0x65
        self.assertIsNone(parse_tcp_packet(bytes(raw)))

    def test_bad_ihl(self):
        raw = bytearray(make_packet())
        raw[0] = 0x44
        self.assertIsNone(parse_tcp_packet(bytes(raw)))

    def test_bad_data_offset(self):
        small = bytearray(make_packet())
        small[20 + 12] = 4 << 4
        self.assertIsNone(parse_tcp_packet(bytes(small)))
        big = bytearray(make_packet())
        big[20 + 12] = 15 << 4
        self.assertIsNone(parse_tcp_packet(bytes(big)))

    def test_trimmed_to_ip_length(self):
        raw = make_packet(payload=b'abc') + b'\x00\x00\x00\x00'
        self.assertEqual(parse_tcp_packet(raw).payload, b'abc')

    def test_ip_length_cuts_header(self):
        self.assertIsNone(parse_tcp_packet(make_packet(total_len=30)))

    def test_ip_options(self):
        seg = parse_tcp_packet(make_packet(ihl=6, payload=b'xy'))
        self.assertEqual(seg.payload, b'xy')


class MssOptionTest(unittest.TestCase):
    """parse_mss_option on hand-made headers."""

    def header(self, options):
        offset = 5 + len(options) // 4
        return struct.pack('!HHLLBBHHH', 1, 2, 3, 4, offset << 4, SYN, 5, 0, 0) + options

    def test_found_after_nops(self):
        self.assertEqual(parse_mss_option(self.header(b'\x01\x01\x02\x04\x02\x18\x00\x00')), 536)

    def test_other_options_skipped(self):
        opts = b'\x03\x03\x07\x04\x02\x00\x00\x00'
        self.assertIsNone(parse_mss_option(self.header(opts)))

    def test_end_of_list(self):
        self.assertIsNone(parse_mss_option(self.header(b'\x00\x02\x04\x05\xb4\x00\x00\x00')))

    def test_bad_length(self):
        self.assertIsNone(parse_mss_option(self.header(b'\x08\x00\x00\x00')))
        self.assertIsNone(parse_mss_option(self.header(b'\x01\x01\x01\x08')))

    def test_short_or_bad_offset(self):
        self.assertIsNone(parse_mss_option(b'\x00' * 10))
        self.assertIsNone(parse_mss_option(self.header(b'')[:12] + b'\xf0' + b'\x00' * 7))


class SequenceTest(unittest.TestCase):
    """wrap and unwrap."""

    def test_wrap(self):
        self.assertEqual(wrap(5), 5)
        self.assertEqual(wrap(2 ** 32), 0)
        self.assertEqual(wrap(2 ** 32 + 7), 7)
        self.assertEqual(wrap(-1), 2 ** 32 - 1)

    def test_unwrap_plain(self):
        self.assertEqual(unwrap(2001, 2000), 2001)
        self.assertEqual(unwrap(1990, 2000), 1990)

    def test_unwrap_forward_across_boundary(self):
        near = 2 ** 32 - 10
        self.assertEqual(unwrap(5, near), 2 ** 32 + 5)
        self.assertEqual(unwrap(2 ** 32 - 5, near), 2 ** 32 - 5)

    def test_unwrap_backward_across_boundary(self):
        near = 2 ** 32 + 10
        self.assertEqual(unwrap(2 ** 32 - 5, near), 2 ** 32 - 5)
        self.assertEqual(unwrap(20, near), 2 ** 32 + 20)

    def test_unwrap_near_zero_gives_negative(self):
        self.assertEqual(unwrap(2 ** 32 - 3, 2), -3)

    def test_roundtrip(self):
        for near in (0, 2 ** 32 - 1, 2 ** 32, 5 * 2 ** 32 + 17):
            for delta in (-1000, -1, 0, 1, 1000, 2 ** 31 - 1):
                self.assertEqual(unwrap(wrap(near + delta), near), near + delta)


class ConstantsTest(unittest.TestCase):
    """Flag bits and the sequence space."""

    def test_constants(self):
        self.assertEqual((FIN, SYN, RST, PSH, ACK), (1, 2, 4, 8, 16))
        self.assertEqual(tcp_segment.SEQ_MOD, 2 ** 32)

    def test_flag_names_is_gone(self):
        self.assertFalse(hasattr(tcp_segment, 'flag_names'))


class SlideRulesTest(unittest.TestCase):
    """The docstring of unwrap carries its slide rule."""

    def test_slide_rules(self):
        assert_slide_rules(self, 'tcp_segment', SEGMENT_RULES)


if __name__ == '__main__':
    unittest.main()

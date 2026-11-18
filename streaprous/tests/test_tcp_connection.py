"""
Tests for the TCP connection glue: client packets in, our packets out.

A fake socket records what we send, a fake clock drives the timers and the
client's packets are built with struct. Calls that block (send, close) run
in helper threads that are joined with a timeout.
"""

import contextlib
import io
import json
import os
import queue
import socket
import struct
import threading
import time
import unittest
from unittest import mock

from streaprous_socket import streaprous_raw_connection as raw
from streaprous_socket import tcp_connection
from streaprous_socket.streaprous_packet_dispatcher import remove_packet_queue
from streaprous_socket.tcp_config import load_settings
from streaprous_socket.tcp_connection import SynInfo, TcpConnection, choose_mss
from streaprous_socket.tcp_segment import ACK, FIN, PSH, RST, SYN, parse_tcp_packet, wrap
from tests.slide_rules import assert_slide_rules

CLIENT_IP = '172.28.0.20'
CLIENT_PORT = 51892
SERVER_IP = '172.28.0.10'
SERVER_PORT = 9000
SERVER_SEQ = 2001
CLIENT_SEQ = 357825583
KEY = (CLIENT_IP, CLIENT_PORT)
FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures',
                       'docker_client_packets.json')

CONNECTION_RULES = [
    ('choose_mss', 'extra', "client's MSS option"),
    ('TcpConnection.put_on_wire', '3-94',
     'advertises free buffer space in rwnd field in TCP header'),
    ('TcpConnection.process_timers', 'extra', '10 s without any segment'),
    ('TcpConnection.close', '3-103', 'respond to received FIN with ACK'),
    ('TcpConnection.close', 'extra', 'up to 2 s'),
]


class FakeSocket:
    """Raw socket stand-in that records every sendto call."""

    def __init__(self):
        """Starts with no packets."""
        self.packets = []
        self.lock = threading.Lock()

    def sendto(self, packet, addr):
        """Records one packet and returns its length."""
        with self.lock:
            self.packets.append((bytes(packet), addr))
        return len(packet)

    def segments(self):
        """Parses the recorded packets, in sending order."""
        with self.lock:
            packets = list(self.packets)
        return [parse_tcp_packet(packet) for packet, _ in packets]

    def count(self):
        """Counts the recorded packets."""
        with self.lock:
            return len(self.packets)


class FakeClock:
    """Clock that only moves when a test sets ``now``."""

    def __init__(self, now=100.0):
        """Starts at a fixed time in seconds."""
        self.now = now

    def __call__(self):
        """Gives the current fake time."""
        return self.now


class Runner(threading.Thread):
    """Runs one blocking call in a daemon thread and keeps its result."""

    def __init__(self, func, *args):
        """Prepares the call func(*args)."""
        threading.Thread.__init__(self, daemon=True)
        self.func = func
        self.args = args
        self.result = None
        self.error = None

    def run(self):
        """Makes the call and stores its result or exception."""
        try:
            self.result = self.func(*self.args)
        except Exception as err:  # reported by finish()
            self.error = err

    def finish(self, test, timeout=2.0):
        """Joins the thread, fails the test if it is still blocked, returns the result."""
        self.join(timeout)
        test.assertFalse(self.is_alive(), 'call still blocked')
        if self.error is not None:
            raise self.error
        return self.result


def client_packet(seq, ack=SERVER_SEQ, flags=ACK, window=64240, payload=b"", options=b"",
                  src_port=CLIENT_PORT):
    """Builds one IP datagram as the client would send it; seq and ack are wrapped here.

    Args:
         seq       sequence number
         ack       ACK number
         flags     TCP flag byte
         window    window field
         payload   data bytes
         options   TCP option bytes, a multiple of 4 long
         src_port  client port
    """
    offset = (20 + len(options)) // 4
    tcp = struct.pack('!HHLLBBHHH', src_port, SERVER_PORT, wrap(seq), wrap(ack),
                      offset << 4, flags, window, 0, 0) + options + payload
    ip = struct.pack('!BBHHHBBH4s4s', 0x45, 0, 20 + len(tcp), 1, 0x4000, 64,
                     socket.IPPROTO_TCP, 0, socket.inet_aton(CLIENT_IP),
                     socket.inet_aton(SERVER_IP))
    return ip + tcp


def make_settings(**changes):
    """Builds settings with the trace file off; changes override fields."""
    settings = load_settings({'STREAPROUS_TRACE': '0'})
    for name, value in changes.items():
        setattr(settings, name, value)
    return settings


def make_info(client_seq=CLIENT_SEQ, server_seq=SERVER_SEQ, client_port=CLIENT_PORT):
    """Builds the teacher's conn_struct of an established connection."""
    return raw.conn_struct(client_ip=CLIENT_IP, client_port=client_port, server_ip=SERVER_IP,
                           server_port=SERVER_PORT, state='ESTABLISHED',
                           server_seq=server_seq, client_seq=client_seq,
                           expected_final_ack=server_seq, last_activity=0.0)


def fixture_tcp(name):
    """Gives the TCP part (header, options, payload) of a captured packet, such as 'syn'."""
    with open(FIXTURE) as handle:
        packets = json.load(handle)['packets']
    data = bytes.fromhex(next(p['hex'] for p in packets if p['name'] == name))
    return data[(data[0] & 0x0f) * 4:]


def wait_for(predicate, timeout=2.0):
    """Polls until predicate() is true or the timeout passes; returns its last value."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class ConnectionTestCase(unittest.TestCase):
    """Shared set-up: fake socket and clock, teacher table entry, quiet stdout."""

    def setUp(self):
        """Creates the fakes and registers the connection in the teacher's table."""
        redirect = contextlib.redirect_stdout(io.StringIO())
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        self.sock = FakeSocket()
        self.clock = FakeClock()
        self.info = make_info()
        self.lines = []
        self.recorded = []
        with raw.connections_lock:
            raw.connections[KEY] = self.info
        self.addCleanup(self.forget)

    def forget(self):
        """Removes the connection from the teacher's table."""
        with raw.connections_lock:
            raw.connections.pop(KEY, None)

    def make(self, syn=SynInfo(1460, 64240, CLIENT_SEQ - 1), info=None, **changes):
        """Builds a connection (worker not started) whose events are recorded.

        Args:
             syn      SynInfo of the client's SYN, or None
             info     conn_struct to use instead of self.info
             changes  settings to override, such as rcv_buffer=4000
        """
        conn = TcpConnection(self.sock, info or self.info, queue.Queue(), syn,
                             make_settings(**changes), clock=self.clock)
        conn.trace.write = self.lines.append
        print_event = conn.trace.event

        def record(event, state, **details):
            """Keeps every event, quiet ones too, then logs it as usual."""
            self.recorded.append((event, details))
            print_event(event, state, **details)

        conn.trace.event = record
        return conn

    def events(self, name):
        """Lists the details dicts of the recorded events called name."""
        return [details for event, details in self.recorded if event == name]

    def feed(self, conn, packet):
        """Hands one client packet to the connection, as the worker does."""
        with conn.cond:
            conn.process_packet(packet, self.clock.now)
            conn.cond.notify_all()

    def tick(self, conn, now):
        """Moves the clock to now and runs the timers, as the worker does."""
        self.clock.now = now
        with conn.cond:
            conn.process_timers(now)
            conn.cond.notify_all()

    def entry(self, key=KEY):
        """Reads the teacher's table entry of the connection."""
        with raw.connections_lock:
            return raw.connections[key]


class SendTests(ConnectionTestCase):
    """Data we send: segment size, ACK and rwnd fields, blocking send, loss switches."""

    def test_segments_carry_mss_ack_and_rwnd(self):
        """Slides 3-77, 3-94: each data segment is at most one MSS and carries our ACK and rwnd."""
        conn = self.make(syn=SynInfo(536, 64240, CLIENT_SEQ - 1))
        self.feed(conn, client_packet(CLIENT_SEQ, flags=PSH | ACK, payload=b"r" * 80))
        self.assertEqual(self.sock.count(), 0)  # delayed ACK, nothing sent yet
        data = bytes(range(256)) * 8  # 2048 bytes
        runner = Runner(conn.send, data)
        runner.start()
        self.assertTrue(wait_for(lambda: self.sock.count() == 1))  # cwnd = 1 MSS
        self.assertFalse(conn.receiver.ack_pending)  # the data segment carried the ACK
        self.feed(conn, client_packet(CLIENT_SEQ + 80, ack=SERVER_SEQ + 536))
        self.assertEqual(self.sock.count(), 3)
        self.feed(conn, client_packet(CLIENT_SEQ + 80, ack=SERVER_SEQ + 1608))
        self.assertEqual(self.sock.count(), 4)
        self.feed(conn, client_packet(CLIENT_SEQ + 80, ack=SERVER_SEQ + 2048))
        self.assertEqual(runner.finish(self), 2048)
        segments = self.sock.segments()
        self.assertEqual([len(s.payload) for s in segments], [536, 536, 536, 440])
        self.assertEqual(b"".join(s.payload for s in segments), data)
        self.assertEqual([s.seq for s in segments],
                         [SERVER_SEQ, SERVER_SEQ + 536, SERVER_SEQ + 1072, SERVER_SEQ + 1608])
        for seg in segments:
            self.assertEqual(seg.ack, CLIENT_SEQ + 80)
            self.assertEqual(seg.window, 4096 - 80)
            self.assertEqual(seg.flags, PSH | ACK)
            self.assertEqual((seg.src_ip, seg.dst_ip), (SERVER_IP, CLIENT_IP))
            self.assertEqual((seg.src_port, seg.dst_port), (SERVER_PORT, CLIENT_PORT))
        self.assertEqual(self.entry().server_seq, SERVER_SEQ + 2048)
        self.assertEqual(self.entry().client_seq, CLIENT_SEQ + 80)

    def test_empty_send_returns_zero(self):
        """send(b"") returns 0 and sends nothing."""
        conn = self.make()
        self.assertEqual(conn.send(b""), 0)
        self.assertEqual(self.sock.count(), 0)

    def test_send_returns_minus_one_after_rst(self):
        """Flaw scenario 30: an RST while send waits makes it return -1; later calls fail."""
        conn = self.make()
        runner = Runner(conn.send, b"x" * 1000)
        runner.start()
        self.assertTrue(wait_for(lambda: self.sock.count() == 1))
        self.feed(conn, client_packet(CLIENT_SEQ, flags=RST))
        self.assertEqual(runner.finish(self), -1)
        self.assertEqual(conn.broken, 'reset')
        self.assertEqual(len(self.events('RESET')), 1)
        self.assertEqual(conn.send(b"y"), -1)
        self.assertEqual(conn.recv(), b"")
        self.tick(conn, self.clock.now + 5.0)
        self.assertEqual(self.sock.count(), 1)  # a broken connection resends nothing

    def test_silent_client_gives_up(self):
        """Flaw scenario 16, extra rule: 10 s without any segment while data waits."""
        conn = self.make()
        runner = Runner(conn.send, b"x" * 100)
        runner.start()
        self.assertTrue(wait_for(lambda: self.sock.count() == 1))
        start = self.clock.now
        self.tick(conn, start + 9.9)
        self.assertIsNone(conn.broken)
        self.assertEqual(len(self.events('TIMEOUT')), 1)
        self.tick(conn, start + 10.1)
        self.assertEqual(runner.finish(self), -1)
        self.assertEqual(self.events('GIVE_UP'), [{'reason': 'silence'}])
        self.assertTrue(any('GIVE_UP reason=silence' in line for line in self.lines))
        self.tick(conn, start + 30.0)
        self.assertEqual(len(self.events('GIVE_UP')), 1)

    def test_drop_send_setting(self):
        """STREAPROUS_DROP_SEND drops the n-th first transmission once; the timer resends it."""
        conn = self.make(drop_send=frozenset({1}))
        runner = Runner(conn.send, b"d" * 500)
        runner.start()
        self.assertTrue(wait_for(lambda: self.events('DROP')))
        self.assertEqual(self.sock.count(), 0)
        self.assertEqual(self.events('DROP'), [{'seq': SERVER_SEQ, 'len': 500, 'why': 'nth=1'}])
        self.tick(conn, self.clock.now + 1.01)
        self.assertEqual(self.sock.count(), 1)
        self.assertEqual(self.sock.segments()[0].payload, b"d" * 500)
        self.assertEqual(len(self.events('TIMEOUT')), 1)
        self.feed(conn, client_packet(CLIENT_SEQ, ack=SERVER_SEQ + 500))
        self.assertEqual(runner.finish(self), 500)

    def test_random_loss_setting(self):
        """STREAPROUS_LOSS drops data segments, resends too, but never pure ACKs."""
        conn = self.make(loss_rate=1.0, loss_seed=7)
        self.feed(conn, client_packet(CLIENT_SEQ, payload=b"q" * 10))
        self.tick(conn, self.clock.now + 0.2)
        self.assertEqual([s.ack for s in self.sock.segments()], [CLIENT_SEQ + 10])
        runner = Runner(conn.send, b"z" * 300)
        runner.start()
        self.assertTrue(wait_for(lambda: len(self.events('DROP')) == 1))
        self.tick(conn, self.clock.now + 1.01)
        self.assertEqual([d['why'] for d in self.events('DROP')], ['random', 'random'])
        self.assertEqual(self.sock.count(), 1)
        conn.loss.rate = 0.0
        self.tick(conn, self.clock.now + 1.01)
        self.assertEqual(self.sock.segments()[-1].payload, b"z" * 300)
        self.feed(conn, client_packet(CLIENT_SEQ + 10, ack=SERVER_SEQ + 300))
        self.assertEqual(runner.finish(self), 300)


class ReceiveTests(ConnectionTestCase):
    """Data we receive: the ACK rules of slide 3-85, recv and window updates."""

    def test_delayed_ack_then_ack_on_tick(self):
        """Slide 3-85 row 1: an in-order segment is ACKed when the delayed-ACK time passes."""
        conn = self.make()
        self.feed(conn, client_packet(CLIENT_SEQ, payload=b"a" * 100))
        self.tick(conn, 100.05)
        self.assertEqual(self.sock.count(), 0)
        self.tick(conn, 100.11)
        segments = self.sock.segments()
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].flags, ACK)
        self.assertEqual(segments[0].seq, SERVER_SEQ)
        self.assertEqual(segments[0].ack, CLIENT_SEQ + 100)
        self.assertEqual(segments[0].window, 4096 - 100)
        self.assertEqual(segments[0].payload, b"")
        self.assertEqual(self.events('ACK_SENT'),
                         [{'ack': CLIENT_SEQ + 100, 'rwnd': 3996, 'reason': 'delayed'}])

    def test_second_segment_acked_at_once(self):
        """Slide 3-85 row 2: a second in-order segment gets one cumulative ACK at once."""
        conn = self.make()
        self.feed(conn, client_packet(CLIENT_SEQ, payload=b"a" * 100))
        self.feed(conn, client_packet(CLIENT_SEQ + 100, payload=b"b" * 100))
        self.assertEqual([s.ack for s in self.sock.segments()], [CLIENT_SEQ + 200])
        self.assertEqual(self.events('ACK_SENT')[0]['reason'], 'cumulative')

    def test_gap_gets_duplicate_ack(self):
        """Slide 3-85 rows 3 and 4: a gap gets a duplicate ACK, its fill an immediate ACK."""
        conn = self.make()
        self.feed(conn, client_packet(CLIENT_SEQ + 100, payload=b"b" * 100))
        self.assertEqual([s.ack for s in self.sock.segments()], [CLIENT_SEQ])
        self.assertEqual(self.events('DUP_ACK_SENT'), [{'ack': CLIENT_SEQ}])
        self.assertEqual(len(self.events('OUT_OF_ORDER')), 1)
        self.feed(conn, client_packet(CLIENT_SEQ, payload=b"a" * 100))
        self.assertEqual(self.sock.segments()[-1].ack, CLIENT_SEQ + 200)
        self.assertEqual(self.events('ACK_SENT')[-1]['reason'], 'gap_filled')
        self.assertEqual(conn.recv(), b"a" * 100 + b"b" * 100)

    def test_data_segment_carries_pending_ack(self):
        """Flaw scenario 26: our data segment carries the pending delayed ACK, so none follows."""
        conn = self.make()
        self.feed(conn, client_packet(CLIENT_SEQ, payload=b"a" * 100))
        self.assertTrue(conn.receiver.ack_pending)
        runner = Runner(conn.send, b"s" * 50)
        runner.start()
        self.assertTrue(wait_for(lambda: self.sock.count() == 1))
        self.assertEqual(self.sock.segments()[0].ack, CLIENT_SEQ + 100)
        self.assertFalse(conn.receiver.ack_pending)
        self.tick(conn, 100.2)
        self.assertEqual(self.sock.count(), 1)
        self.feed(conn, client_packet(CLIENT_SEQ + 100, ack=SERVER_SEQ + 50))
        self.assertEqual(runner.finish(self), 50)

    def test_recv_returns_stream_then_empty_after_fin(self):
        """recv joins segments into one byte stream, then gives b"" after the client's FIN."""
        conn = self.make()
        self.feed(conn, client_packet(CLIENT_SEQ, payload=b"GET / HT"))
        self.feed(conn, client_packet(CLIENT_SEQ + 8, payload=b"TP/1.1\r\n\r\n"))
        self.assertEqual(conn.recv(max_bytes=3), b"GET")
        self.assertEqual(conn.recv(), b" / HTTP/1.1\r\n\r\n")
        self.assertEqual(self.entry().client_seq, CLIENT_SEQ + 18)
        before = self.sock.count()
        self.feed(conn, client_packet(CLIENT_SEQ + 18, flags=FIN | ACK))
        self.assertEqual(self.sock.count(), before + 1)
        fin_ack = self.sock.segments()[-1]
        self.assertEqual((fin_ack.ack, fin_ack.flags), (CLIENT_SEQ + 19, ACK))
        self.assertEqual(len(self.events('FIN_RECEIVED')), 1)
        self.assertEqual(conn.recv(), b"")
        self.assertEqual(conn.recv(), b"")

    def test_recv_timeout(self):
        """recv gives b"" when nothing arrives before the timeout."""
        conn = self.make()
        self.assertEqual(conn.recv(timeout=0), b"")
        runner = Runner(conn.recv, None, 1.0)
        runner.start()
        self.assertTrue(runner.is_alive())
        self.clock.now += 1.5
        self.assertEqual(runner.finish(self), b"")

    def test_window_update_after_read(self):
        """Slide 3-94 and the quarter-buffer extra: reading reopens a zero window with an ACK."""
        conn = self.make(rcv_buffer=4000)
        for i in range(4):
            self.feed(conn, client_packet(CLIENT_SEQ + 1000 * i, payload=b"w" * 1000))
        self.assertEqual([s.window for s in self.sock.segments()], [2000, 0])
        self.assertEqual(conn.state()['adv_rwnd'], 0)
        self.assertEqual(conn.recv(max_bytes=500), b"w" * 500)
        self.assertEqual(self.sock.count(), 2)  # 500 free is below a quarter
        self.assertEqual(conn.recv(max_bytes=600), b"w" * 600)
        update = self.sock.segments()[-1]
        self.assertEqual(self.sock.count(), 3)
        self.assertEqual((update.window, update.ack), (1100, CLIENT_SEQ + 4000))
        self.assertEqual(self.events('WINDOW_UPDATE'), [{'rwnd': 1100}])

    def test_sequence_numbers_wrap_past_2_32(self):
        """Flaw scenario 12, slide 3-78: data crosses 2^32 both ways; the wire carries 32 bits."""
        client_first = 2 ** 32 - 100
        server_first = 2 ** 32 - 500
        info = make_info(client_seq=client_first, server_seq=server_first)
        with raw.connections_lock:
            raw.connections[KEY] = info
        conn = self.make(syn=SynInfo(536, 64240, client_first - 1), info=info)
        self.feed(conn, client_packet(client_first, ack=server_first, payload=b"a" * 60))
        self.feed(conn, client_packet(client_first + 60, ack=server_first, payload=b"b" * 80))
        self.assertEqual(self.sock.segments()[-1].ack, 40)
        self.feed(conn, client_packet(40, ack=server_first, payload=b"c" * 100))
        self.tick(conn, 100.2)
        self.assertEqual(self.sock.segments()[-1].ack, 140)
        self.assertEqual(conn.recv(), b"a" * 60 + b"b" * 80 + b"c" * 100)
        self.assertEqual(self.entry().client_seq, 140)
        before = self.sock.count()
        runner = Runner(conn.send, b"s" * 1200)
        runner.start()
        self.assertTrue(wait_for(lambda: self.sock.count() == before + 1))
        self.feed(conn, client_packet(140, ack=server_first + 536))
        self.feed(conn, client_packet(140, ack=server_first + 1200))
        self.assertEqual(runner.finish(self), 1200)
        sent = self.sock.segments()[before:]
        self.assertEqual([s.seq for s in sent], [server_first, 36, 572])
        self.assertEqual([len(s.payload) for s in sent], [536, 536, 128])
        self.assertEqual(conn.sender.send_base, server_first + 1200)
        self.assertEqual(self.entry().server_seq, 700)

    def test_other_ports_and_syn_ignored(self):
        """Flaw scenario 31: a repeated SYN and another port's packet change nothing."""
        conn = self.make()
        self.feed(conn, client_packet(CLIENT_SEQ - 1, flags=SYN, ack=0))
        self.feed(conn, client_packet(CLIENT_SEQ, payload=b"x", src_port=CLIENT_PORT + 1))
        self.assertEqual(conn.receiver.expected_seq_num, CLIENT_SEQ)
        self.assertEqual(self.sock.count(), 0)
        self.assertEqual(self.recorded, [])

    def test_client_segment_with_ack_and_data(self):
        """Flaw scenario 33: one client segment ACKs our data and brings new data."""
        conn = self.make()
        runner = Runner(conn.send, b"x" * 1000)
        runner.start()
        self.assertTrue(wait_for(lambda: self.sock.count() == 1))
        self.feed(conn, client_packet(CLIENT_SEQ, ack=SERVER_SEQ + 1000, flags=PSH | ACK,
                                      payload=b"req"))
        self.assertEqual(runner.finish(self), 1000)
        self.assertEqual(conn.sender.send_base, SERVER_SEQ + 1000)
        self.assertEqual(self.events('DUPACK'), [])
        self.tick(conn, 100.2)
        self.assertEqual(self.sock.segments()[-1].ack, CLIENT_SEQ + 3)
        self.assertEqual(conn.recv(), b"req")

    def test_two_connections_keep_separate_state(self):
        """Flaw scenario 20: two clients at once; each connection takes only its own packets."""
        other_port = CLIENT_PORT + 1
        other_seq = 900000
        other_info = make_info(client_seq=other_seq, client_port=other_port)
        with raw.connections_lock:
            raw.connections[(CLIENT_IP, other_port)] = other_info
        self.addCleanup(lambda: raw.connections.pop((CLIENT_IP, other_port), None))
        first = self.make()
        second = self.make(syn=SynInfo(536, 30000, other_seq - 1), info=other_info)
        one = client_packet(CLIENT_SEQ, payload=b"one")
        two = client_packet(other_seq, window=30000, payload=b"second", src_port=other_port)
        for conn in (first, second):
            self.feed(conn, one)
            self.feed(conn, two)
        self.assertEqual(first.recv(), b"one")
        self.assertEqual(second.recv(), b"second")
        self.assertEqual((first.mss, second.mss), (1460, 536))
        self.assertEqual((first.sender.rwnd, second.sender.rwnd), (64240, 30000))
        self.assertEqual(self.entry().client_seq, CLIENT_SEQ + 3)
        self.assertEqual(self.entry((CLIENT_IP, other_port)).client_seq, other_seq + 6)


class CloseTests(ConnectionTestCase):
    """Our FIN (slide 3-103), its retransmission and the client's FIN."""

    def test_close_retransmits_fin_and_acks_client_fin(self):
        """Slides 3-103, 3-84: the FIN is resent on timeout; the client's FIN gets an ACK."""
        conn = self.make()
        runner = Runner(conn.close)
        runner.start()
        self.assertTrue(wait_for(lambda: self.sock.count() == 1))
        first = self.sock.segments()[0]
        self.assertEqual((first.flags, first.seq, first.ack), (FIN | ACK, SERVER_SEQ, CLIENT_SEQ))
        self.tick(conn, self.clock.now + 1.01)
        second = self.sock.segments()[1]
        self.assertEqual((second.flags, second.seq), (FIN | ACK, SERVER_SEQ))
        self.assertEqual(len(self.events('TIMEOUT')), 1)
        self.feed(conn, client_packet(CLIENT_SEQ, ack=SERVER_SEQ + 1))
        self.assertTrue(conn.sender.fin_acked())
        self.assertTrue(runner.is_alive())  # lingers for the client's FIN
        self.feed(conn, client_packet(CLIENT_SEQ, ack=SERVER_SEQ + 1, flags=FIN | ACK))
        runner.finish(self)
        last = self.sock.segments()[-1]
        self.assertEqual((last.flags, last.seq, last.ack), (ACK, SERVER_SEQ + 1, CLIENT_SEQ + 1))
        self.assertTrue(conn.finished)
        self.assertEqual(len(self.events('CLOSE')), 1)
        self.assertEqual(self.entry().server_seq, SERVER_SEQ + 1)
        self.assertEqual(self.entry().client_seq, CLIENT_SEQ + 1)

    def test_close_after_client_fin_does_not_linger(self):
        """When the client closed first, close returns once our FIN is ACKed."""
        conn = self.make()
        self.feed(conn, client_packet(CLIENT_SEQ, flags=FIN | ACK))
        runner = Runner(conn.close)
        runner.start()
        self.assertTrue(wait_for(lambda: self.sock.count() == 2))
        fin = self.sock.segments()[1]
        self.assertEqual((fin.flags, fin.ack), (FIN | ACK, CLIENT_SEQ + 1))
        self.feed(conn, client_packet(CLIENT_SEQ + 1, ack=SERVER_SEQ + 1))
        runner.finish(self)
        self.assertEqual(conn.send(b"late"), -1)

    def test_linger_ends_after_its_time(self):
        """Extra rule: without a client FIN, close returns after up to 2 s."""
        conn = self.make()
        runner = Runner(conn.close)
        runner.start()
        self.assertTrue(wait_for(lambda: self.sock.count() == 1))
        self.feed(conn, client_packet(CLIENT_SEQ, ack=SERVER_SEQ + 1))
        time.sleep(0.2)  # close wakes and starts its linger at the old clock time
        start = self.clock.now
        self.clock.now = start + 1.9
        time.sleep(0.25)
        self.assertTrue(runner.is_alive())
        self.clock.now = start + 2.1
        runner.finish(self)
        self.assertTrue(conn.finished)
        self.assertEqual(self.sock.count(), 1)
        self.assertAlmostEqual(self.events('CLOSE')[0]['seconds'], 2.1)

    def test_close_after_reset_sends_nothing(self):
        """After an RST, close only finishes the connection."""
        conn = self.make()
        self.feed(conn, client_packet(CLIENT_SEQ, flags=RST))
        conn.close()
        self.assertEqual(self.sock.count(), 0)
        self.assertTrue(conn.finished)


class WorkerTests(ConnectionTestCase):
    """The worker thread reads the queue by itself."""

    def test_worker_reads_queue(self):
        """A packet put in the queue reaches recv through the worker."""
        conn = self.make()
        conn.start()
        self.assertEqual(len(self.events('OPEN')), 1)
        self.assertEqual(conn.thread.name, 'tcp-%s:%d' % KEY)
        conn.queue.put((client_packet(CLIENT_SEQ, payload=b"hello"), (CLIENT_IP, 0)))
        runner = Runner(conn.recv)
        runner.start()
        self.assertEqual(runner.finish(self), b"hello")
        self.feed(conn, client_packet(CLIENT_SEQ + 5, flags=RST))
        conn.close()
        conn.thread.join(1.0)
        self.assertFalse(conn.thread.is_alive())

    def test_worker_error_breaks_connection(self):
        """An error in the worker logs GIVE_UP reason=error and stops the worker."""
        conn = self.make()

        def fail(packet, now):
            """Stands in for process_packet and fails."""
            raise ValueError('bad packet')

        conn.process_packet = fail
        conn.start()
        conn.queue.put((client_packet(CLIENT_SEQ, payload=b"x"), (CLIENT_IP, 0)))
        conn.thread.join(2.0)
        self.assertFalse(conn.thread.is_alive())
        self.assertEqual(conn.broken, 'error')
        self.assertEqual(self.events('GIVE_UP'), [{'reason': 'error'}])
        self.assertEqual(conn.send(b"y"), -1)


class RegistryTests(ConnectionTestCase):
    """note_client_syn, get_connection, finish_connection and choose_mss."""

    def setUp(self):
        """Uses quiet settings for connections made by the registry."""
        ConnectionTestCase.setUp(self)
        patcher = mock.patch.object(tcp_connection, 'SETTINGS', make_settings())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(remove_packet_queue, KEY)
        self.addCleanup(tcp_connection._syn_info.pop, KEY, None)

    def open_and_close(self):
        """Creates the connection through the registry, then resets and finishes it."""
        conn = tcp_connection.get_connection(self.sock, self.info)
        self.assertIs(tcp_connection.get_connection(self.sock, self.info), conn)
        with conn.cond:
            conn.broken = 'reset'
        tcp_connection.finish_connection(self.sock, self.info)
        self.assertNotIn(KEY, tcp_connection._connections)
        conn.thread.join(1.0)
        self.assertFalse(conn.thread.is_alive())
        return conn

    def syn_with_mss(self, mss):
        """Gives the TCP bytes of a client SYN with an MSS option."""
        packet = client_packet(CLIENT_SEQ - 1, ack=0, flags=SYN, window=29200,
                               options=struct.pack('!BBH', 2, 4, mss))
        return packet[20:]

    def test_mss_from_client_syn(self):
        """Extra rule: the fixture's SYN carries MSS 1460 and window 64240."""
        tcp_connection.note_client_syn(KEY, fixture_tcp('syn'))
        conn = self.open_and_close()
        self.assertEqual(conn.mss, 1460)
        self.assertEqual(conn.sender.rwnd, 64240)
        self.assertNotIn(KEY, tcp_connection._syn_info)

    def test_mss_without_syn_option(self):
        """Extra rule: without an MSS option the connection uses 536 bytes."""
        syn = fixture_tcp('syn')
        no_options = syn[:12] + bytes([0x50]) + syn[13:20]  # data offset 5, no options
        tcp_connection.note_client_syn(KEY, no_options)
        self.assertIsNone(tcp_connection._syn_info[KEY].mss)
        conn = self.open_and_close()
        self.assertEqual(conn.mss, 536)
        self.assertEqual(conn.sender.rwnd, 64240)

    def test_mss_capped_at_1460(self):
        """Extra rule: a client MSS above 1460 is cut to 1460; a smaller one is kept."""
        tcp_connection.note_client_syn(KEY, self.syn_with_mss(8960))
        self.assertEqual(tcp_connection._syn_info[KEY], SynInfo(8960, 29200, CLIENT_SEQ - 1))
        conn = self.open_and_close()
        self.assertEqual(conn.mss, 1460)
        self.assertEqual(choose_mss(SynInfo(1200, 0, 0)), 1200)
        self.assertEqual(choose_mss(None), 536)

    def test_first_byte_follows_syn(self):
        """Flaw scenario 24: the receiver expects SYN seq + 1 even when accept saw data first.

        With reordering a data segment can reach accept before the bare ACK,
        and accept then keeps that segment's seq as client_seq.
        """
        syn = fixture_tcp('syn')
        syn_seq = struct.unpack('!L', syn[4:8])[0]
        tcp_connection.note_client_syn(KEY, syn)
        self.info = make_info(client_seq=wrap(syn_seq + 1 + 1072))
        conn = self.open_and_close()
        self.assertEqual(conn.receiver.expected_seq_num, wrap(syn_seq + 1))

    def test_first_byte_without_syn(self):
        """With no SYN noted, the receiver starts at accept's client_seq."""
        conn = self.open_and_close()
        self.assertEqual(conn.receiver.expected_seq_num, CLIENT_SEQ)

    def test_no_syn_noted(self):
        """With no SYN noted, MSS is 536 and the first rwnd is one MSS."""
        conn = self.open_and_close()
        self.assertEqual(conn.mss, 536)
        self.assertEqual(conn.sender.rwnd, 536)
        self.assertEqual(conn.sender.send_base, SERVER_SEQ)


class SlideRuleTests(unittest.TestCase):
    """Slide rules in the connection's docstrings."""

    def test_slide_rules(self):
        """Every connection function names its slide or extra and the rule's words."""
        assert_slide_rules(self, 'tcp_connection', CONNECTION_RULES)


if __name__ == '__main__':
    unittest.main()

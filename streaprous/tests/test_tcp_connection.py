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
from streaprous_socket.tcp_connection import TcpConnection
from streaprous_socket.tcp_segment import ACK, FIN, PSH, RST, SYN, parse_tcp_packet, wrap

CLIENT_IP = '172.28.0.20'
CLIENT_PORT = 51892
SERVER_IP = '172.28.0.10'
SERVER_PORT = 9000
SERVER_SEQ = 2001
CLIENT_SEQ = 357825583
KEY = (CLIENT_IP, CLIENT_PORT)
FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures',
                       'docker_client_packets.json')


class FakeSocket:
    """Raw socket stand-in that records every sendto call."""

    def __init__(self):
        """Starts with no packets."""
        self.packets = []
        self.lock = threading.Lock()

    def sendto(self, packet, addr):
        """
        Record one packet.

        Args:
            packet: IP datagram bytes.
            addr: Destination address tuple.

        Returns:
            int: Number of bytes "sent".
        """
        with self.lock:
            self.packets.append((bytes(packet), addr))
        return len(packet)

    def segments(self):
        """
        Parse the recorded packets.

        Returns:
            list: Segment tuples in sending order.
        """
        with self.lock:
            packets = list(self.packets)
        return [parse_tcp_packet(packet) for packet, _ in packets]

    def count(self):
        """
        Count the recorded packets.

        Returns:
            int: Number of packets sent so far.
        """
        with self.lock:
            return len(self.packets)


class FakeClock:
    """Clock that only moves when a test sets ``now``."""

    def __init__(self, now=100.0):
        """
        Start at a fixed time.

        Args:
            now: Starting time in seconds.
        """
        self.now = now

    def __call__(self):
        """
        Give the current fake time.

        Returns:
            float: Seconds.
        """
        return self.now


class Runner(threading.Thread):
    """Runs one blocking call in a daemon thread and keeps its result."""

    def __init__(self, func, *args):
        """
        Prepare the call.

        Args:
            func: Function to call.
            args: Its positional arguments.
        """
        threading.Thread.__init__(self, daemon=True)
        self.func = func
        self.args = args
        self.result = None
        self.error = None

    def run(self):
        """Make the call and store its result or exception."""
        try:
            self.result = self.func(*self.args)
        except Exception as err:  # reported by finish()
            self.error = err

    def finish(self, test, timeout=2.0):
        """
        Join the thread and fail the test if it is still blocked.

        Args:
            test: The running TestCase.
            timeout: Seconds to wait.

        Returns:
            The call's result.
        """
        self.join(timeout)
        test.assertFalse(self.is_alive(), 'call still blocked')
        if self.error is not None:
            raise self.error
        return self.result


def client_packet(seq, ack=SERVER_SEQ, flags=ACK, window=64240, payload=b"", options=b"",
                  src_port=CLIENT_PORT):
    """
    Build one IP datagram as the client would send it.

    Args:
        seq: Sequence number (wrapped to 32 bits here).
        ack: ACK number (wrapped to 32 bits here).
        flags: TCP flag byte.
        window: Window field.
        payload: Data bytes.
        options: TCP option bytes, a multiple of 4 long.
        src_port: Client port.

    Returns:
        bytes: IPv4 header + TCP header + payload; checksums left at 0.
    """
    offset = (20 + len(options)) // 4
    tcp = struct.pack('!HHLLBBHHH', src_port, SERVER_PORT, wrap(seq), wrap(ack),
                      offset << 4, flags, window, 0, 0) + options + payload
    ip = struct.pack('!BBHHHBBH4s4s', 0x45, 0, 20 + len(tcp), 1, 0x4000, 64,
                     socket.IPPROTO_TCP, 0, socket.inet_aton(CLIENT_IP),
                     socket.inet_aton(SERVER_IP))
    return ip + tcp


def make_settings(**changes):
    """
    Build settings with the trace file off and every event logged.

    Args:
        changes: Fields to override, such as recv_buffer=4000.

    Returns:
        types.SimpleNamespace: The settings.
    """
    settings = load_settings({'STREAPROUS_TRACE': '0', 'STREAPROUS_TCP_LOG': '2'})
    for name, value in changes.items():
        setattr(settings, name, value)
    return settings


def make_info(client_seq=CLIENT_SEQ):
    """
    Build the teacher's conn_struct of an established connection.

    Args:
        client_seq: First data byte of the client.

    Returns:
        conn_struct: The connection information.
    """
    return raw.conn_struct(client_ip=CLIENT_IP, client_port=CLIENT_PORT, server_ip=SERVER_IP,
                           server_port=SERVER_PORT, state='ESTABLISHED',
                           server_seq=SERVER_SEQ, client_seq=client_seq,
                           expected_final_ack=SERVER_SEQ, last_activity=0.0)


def fixture_tcp(name):
    """
    Give the TCP part of a captured packet from the fixture file.

    Args:
        name: Packet name in the fixture, such as 'syn'.

    Returns:
        bytes: TCP header with options and payload.
    """
    with open(FIXTURE) as handle:
        packets = json.load(handle)['packets']
    data = bytes.fromhex(next(p['hex'] for p in packets if p['name'] == name))
    return data[(data[0] & 0x0f) * 4:]


def wait_for(predicate, timeout=2.0):
    """
    Poll until predicate() is true.

    Args:
        predicate: Function returning a bool.
        timeout: Seconds to wait.

    Returns:
        bool: The last value of predicate().
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class ConnectionTestCase(unittest.TestCase):
    """Shared set-up: fake socket and clock, teacher table entry, quiet stdout."""

    def setUp(self):
        """Create the fakes and register the connection in the teacher's table."""
        redirect = contextlib.redirect_stdout(io.StringIO())
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)
        self.sock = FakeSocket()
        self.clock = FakeClock()
        self.info = make_info()
        self.lines = []
        with raw.connections_lock:
            raw.connections[KEY] = self.info
        self.addCleanup(self._forget)

    def _forget(self):
        """Remove the connection from the teacher's table."""
        with raw.connections_lock:
            raw.connections.pop(KEY, None)

    def make(self, peer_mss=1460, peer_window=64240, info=None, **changes):
        """
        Build a connection whose log lines are collected in self.lines.

        Args:
            peer_mss: MSS of the client's SYN.
            peer_window: Window of the client's SYN.
            info: conn_struct to use instead of self.info.
            changes: Settings to override.

        Returns:
            TcpConnection: The connection (worker not started).
        """
        conn = TcpConnection(self.sock, info or self.info, queue.Queue(), peer_mss,
                             peer_window, make_settings(**changes), clock=self.clock)
        conn.trace.write = self.lines.append
        return conn

    def events(self, name):
        """
        List the logged events of one name with their details.

        Args:
            name: Event name, such as 'ACK_SENT'.

        Returns:
            list: One dict of details (text values) per event.
        """
        found = []
        for line in list(self.lines):
            words = line.split()
            if words[2] != name:
                continue
            details = {}
            for word in words[3:]:
                if word == '|':
                    break
                key, _, value = word.partition('=')
                details[key] = value
            found.append(details)
        return found

    def feed(self, conn, packet):
        """
        Hand one client packet to the connection, as the worker does.

        Args:
            conn: The connection.
            packet: IP datagram bytes.
        """
        with conn.cond:
            conn.process_packet(packet, self.clock.now)
            conn.cond.notify_all()

    def tick(self, conn, now):
        """
        Move the clock and run the timers, as the worker does.

        Args:
            conn: The connection.
            now: New time in seconds.
        """
        self.clock.now = now
        with conn.cond:
            conn.process_timers(now)
            conn.cond.notify_all()

    def entry(self):
        """
        Read the teacher's table entry of the connection.

        Returns:
            conn_struct: The entry.
        """
        with raw.connections_lock:
            return raw.connections[KEY]


class SendTests(ConnectionTestCase):
    """Data we send: segment size, ACK and window fields, blocking send."""

    def test_segments_carry_mss_ack_and_window(self):
        """Every data segment is at most one MSS and carries rcv_nxt and our window."""
        conn = self.make(peer_mss=536)
        self.feed(conn, client_packet(CLIENT_SEQ, flags=PSH | ACK, payload=b"r" * 80))
        self.assertEqual(self.sock.count(), 0)  # delayed ACK, nothing sent yet
        data = bytes(range(256)) * 8  # 2048 bytes
        runner = Runner(conn.send, data)
        runner.start()
        self.assertTrue(wait_for(lambda: self.sock.count() == 1))
        self.assertFalse(conn.receiver.ack_pending)  # the data segment carried the ACK
        self.feed(conn, client_packet(CLIENT_SEQ + 80, ack=SERVER_SEQ + 536))
        self.assertTrue(wait_for(lambda: self.sock.count() == 3))
        self.feed(conn, client_packet(CLIENT_SEQ + 80, ack=SERVER_SEQ + 1608))
        self.assertTrue(wait_for(lambda: self.sock.count() == 4))
        self.feed(conn, client_packet(CLIENT_SEQ + 80, ack=SERVER_SEQ + 2048))
        self.assertEqual(runner.finish(self), 2048)

        segments = self.sock.segments()
        self.assertEqual([len(s.payload) for s in segments], [536, 536, 536, 440])
        self.assertEqual(b"".join(s.payload for s in segments), data)
        self.assertEqual([s.seq for s in segments],
                         [SERVER_SEQ, SERVER_SEQ + 536, SERVER_SEQ + 1072, SERVER_SEQ + 1608])
        for seg in segments:
            self.assertEqual(seg.ack, CLIENT_SEQ + 80)
            self.assertEqual(seg.window, conn.receiver.window())
            self.assertEqual(seg.window, 32768 - 80)
            self.assertEqual(seg.flags, PSH | ACK)
            self.assertEqual((seg.src_ip, seg.dst_ip), (SERVER_IP, CLIENT_IP))
            self.assertEqual((seg.src_port, seg.dst_port), (SERVER_PORT, CLIENT_PORT))
        self.assertEqual(self.entry().server_seq, SERVER_SEQ + 2048)
        self.assertEqual(self.entry().client_seq, CLIENT_SEQ + 80)

    def test_empty_send_returns_zero(self):
        """Sending nothing sends nothing."""
        conn = self.make()
        self.assertEqual(conn.send(b""), 0)
        self.assertEqual(self.sock.count(), 0)

    def test_send_returns_minus_one_after_rst(self):
        """An RST while send waits makes it return -1; later calls fail at once."""
        conn = self.make()
        runner = Runner(conn.send, b"x" * 1000)
        runner.start()
        self.assertTrue(wait_for(lambda: self.sock.count() == 1))
        self.feed(conn, client_packet(CLIENT_SEQ, flags=RST))
        self.assertEqual(runner.finish(self), -1)
        self.assertEqual(len(self.events('RESET')), 1)
        self.assertEqual(conn.send(b"y"), -1)
        self.assertEqual(conn.recv(), b"")

    def test_send_gives_up_on_a_silent_peer(self):
        """With data outstanding and no packet for peer_silence seconds, send fails."""
        conn = self.make()
        runner = Runner(conn.send, b"x" * 100)
        runner.start()
        self.assertTrue(wait_for(lambda: self.sock.count() == 1))
        self.tick(conn, self.clock.now + 61.0)
        self.assertEqual(runner.finish(self), -1)
        self.assertEqual(self.events('GIVE_UP')[-1]['reason'], 'silence')

    def test_drop_send_setting(self):
        """The n-th first transmission is dropped once; the timeout resends it."""
        conn = self.make(drop_send=frozenset({1}))
        runner = Runner(conn.send, b"d" * 500)
        runner.start()
        self.assertTrue(wait_for(lambda: self.events('DROP')))
        self.assertEqual(self.sock.count(), 0)
        drop = self.events('DROP')[0]
        self.assertEqual(drop, {'dir': 'send', 'why': 'nth=1', 'seq': str(SERVER_SEQ),
                                'len': '500'})
        self.tick(conn, self.clock.now + 1.01)
        self.assertEqual(self.sock.count(), 1)
        self.assertEqual(self.sock.segments()[0].payload, b"d" * 500)
        self.assertEqual(len(self.events('TIMEOUT')), 1)
        self.feed(conn, client_packet(CLIENT_SEQ, ack=SERVER_SEQ + 500))
        self.assertEqual(runner.finish(self), 500)


class ReceiveTests(ConnectionTestCase):
    """Data we receive: ACK rules, recv, window updates."""

    def test_delayed_ack_then_ack_on_tick(self):
        """An in-order segment is ACKed only when the delayed-ACK timer fires."""
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
        self.assertEqual(segments[0].window, 32768 - 100)
        self.assertEqual(segments[0].payload, b"")
        self.assertEqual(self.events('ACK_SENT')[0]['reason'], 'delayed')

    def test_second_segment_acked_at_once(self):
        """A second in-order segment while an ACK is pending is ACKed at once."""
        conn = self.make()
        self.feed(conn, client_packet(CLIENT_SEQ, payload=b"a" * 100))
        self.feed(conn, client_packet(CLIENT_SEQ + 100, payload=b"b" * 100))
        self.assertEqual([s.ack for s in self.sock.segments()], [CLIENT_SEQ + 200])
        self.assertEqual(self.events('ACK_SENT')[0]['reason'], 'cumulative')

    def test_gap_gets_duplicate_ack(self):
        """A segment above rcv_nxt gets an immediate duplicate ACK; the fill is ACKed too."""
        conn = self.make()
        self.feed(conn, client_packet(CLIENT_SEQ + 100, payload=b"b" * 100))
        self.assertEqual([s.ack for s in self.sock.segments()], [CLIENT_SEQ])
        self.assertEqual(self.events('DUP_ACK_SENT'), [{'ack': str(CLIENT_SEQ)}])
        self.assertEqual(len(self.events('OUT_OF_ORDER')), 1)
        self.feed(conn, client_packet(CLIENT_SEQ, payload=b"a" * 100))
        self.assertEqual(self.sock.segments()[-1].ack, CLIENT_SEQ + 200)
        self.assertEqual(self.events('ACK_SENT')[-1]['reason'], 'gap_filled')
        self.assertEqual(conn.recv(), b"a" * 100 + b"b" * 100)

    def test_recv_returns_stream_then_empty_after_fin(self):
        """recv joins segments into one stream, then gives b"" after the client's FIN."""
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
        self.assertEqual(fin_ack.ack, CLIENT_SEQ + 19)
        self.assertEqual(fin_ack.flags, ACK)
        self.assertEqual(len(self.events('FIN_RECEIVED')), 1)
        self.assertEqual(conn.recv(), b"")
        self.assertEqual(conn.recv(), b"")

    def test_recv_timeout(self):
        """recv gives b"" when nothing arrives before the timeout."""
        conn = self.make()
        self.assertEqual(conn.recv(timeout=0), b"")

    def test_window_update_after_read(self):
        """Reading past a quarter of a full buffer sends a window update at once."""
        conn = self.make(recv_buffer=4000)
        for i in range(4):
            self.feed(conn, client_packet(CLIENT_SEQ + 1000 * i, payload=b"w" * 1000))
        self.assertEqual([s.window for s in self.sock.segments()], [2000, 0])
        self.assertEqual(len(self.events('WINDOW_ZERO')), 1)
        self.assertEqual(conn.recv(max_bytes=500), b"w" * 500)
        self.assertEqual(self.sock.count(), 2)  # 500 free is below a quarter
        self.assertEqual(conn.recv(max_bytes=600), b"w" * 600)
        update = self.sock.segments()[-1]
        self.assertEqual(self.sock.count(), 3)
        self.assertEqual(update.window, 1100)
        self.assertEqual(update.ack, CLIENT_SEQ + 4000)
        self.assertEqual(self.events('WINDOW_UPDATE'), [{'window': '1100'}])

    def test_drop_recv_setting(self):
        """The n-th received data segment is dropped once and not ACKed."""
        conn = self.make(drop_recv=frozenset({1}))
        packet = client_packet(CLIENT_SEQ, payload=b"q" * 50)
        self.feed(conn, packet)
        self.assertEqual(self.events('DROP'), [{'dir': 'recv', 'why': 'nth=1',
                                                'seq': str(CLIENT_SEQ), 'len': '50'}])
        self.assertEqual(conn.receiver.rcv_nxt, CLIENT_SEQ)
        self.feed(conn, packet)
        self.assertEqual(conn.receiver.rcv_nxt, CLIENT_SEQ + 50)
        self.assertEqual(len(self.events('DROP')), 1)

    def test_sequence_numbers_wrap(self):
        """Client data crossing 2**32 is ACKed with the wrapped number."""
        info = make_info(client_seq=2 ** 32 - 10)
        with raw.connections_lock:
            raw.connections[KEY] = info
        conn = self.make(info=info)
        self.feed(conn, client_packet(2 ** 32 - 10, payload=b"z" * 30))
        self.feed(conn, client_packet(20, payload=b"z" * 30))
        self.assertEqual(self.sock.segments()[-1].ack, 50)
        self.assertEqual(conn.recv(), b"z" * 60)
        self.assertEqual(self.entry().client_seq, 50)

    def test_syn_and_other_ports_are_ignored(self):
        """A repeated SYN is logged as IGNORED; another port's packet is skipped."""
        conn = self.make()
        self.feed(conn, client_packet(CLIENT_SEQ - 1, flags=SYN, ack=0))
        self.assertEqual(self.events('IGNORED'), [{'why': 'syn'}])
        self.feed(conn, client_packet(CLIENT_SEQ, payload=b"x", src_port=CLIENT_PORT + 1))
        self.assertEqual(conn.receiver.rcv_nxt, CLIENT_SEQ)
        self.assertEqual(self.sock.count(), 0)


class CloseTests(ConnectionTestCase):
    """Our FIN, its retransmission and the client's FIN."""

    def test_close_retransmits_fin_and_acks_client_fin(self):
        """close sends a FIN, resends it on timeout and ACKs the client's FIN."""
        conn = self.make()
        runner = Runner(conn.close)
        runner.start()
        self.assertTrue(wait_for(lambda: self.sock.count() == 1))
        first = self.sock.segments()[0]
        self.assertEqual(first.flags, FIN | ACK)
        self.assertEqual(first.seq, SERVER_SEQ)
        self.assertEqual(first.ack, CLIENT_SEQ)
        self.tick(conn, self.clock.now + 1.01)
        self.assertEqual(self.sock.count(), 2)
        self.assertEqual(self.sock.segments()[1].flags, FIN | ACK)
        self.assertEqual(self.sock.segments()[1].seq, SERVER_SEQ)
        self.assertEqual(len(self.events('TIMEOUT')), 1)
        self.feed(conn, client_packet(CLIENT_SEQ, ack=SERVER_SEQ + 1))
        self.assertTrue(conn.sender.fin_acked())
        self.assertTrue(runner.is_alive())  # lingers for the client's FIN
        self.feed(conn, client_packet(CLIENT_SEQ, ack=SERVER_SEQ + 1, flags=FIN | ACK))
        runner.finish(self)
        last = self.sock.segments()[-1]
        self.assertEqual(last.flags, ACK)
        self.assertEqual(last.seq, SERVER_SEQ + 1)
        self.assertEqual(last.ack, CLIENT_SEQ + 1)
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
        self.assertEqual(self.sock.segments()[1].flags, FIN | ACK)
        self.assertEqual(self.sock.segments()[1].ack, CLIENT_SEQ + 1)
        self.feed(conn, client_packet(CLIENT_SEQ + 1, ack=SERVER_SEQ + 1))
        runner.finish(self)
        self.assertEqual(conn.send(b"late"), -1)

    def test_linger_ends_after_its_time(self):
        """Without a client FIN, close returns after settings.linger seconds."""
        conn = self.make()
        runner = Runner(conn.close)
        runner.start()
        self.assertTrue(wait_for(lambda: self.sock.count() == 1))
        self.feed(conn, client_packet(CLIENT_SEQ, ack=SERVER_SEQ + 1))

        def later():
            """Move the fake clock on and tell whether close returned."""
            self.clock.now += 0.5
            return not runner.is_alive()

        self.assertTrue(wait_for(later))
        runner.finish(self)
        self.assertTrue(conn.finished)
        self.assertEqual(self.sock.count(), 1)

    def test_close_after_reset_sends_nothing(self):
        """After an RST close only finishes the connection."""
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
        conn.queue.put((client_packet(CLIENT_SEQ, payload=b"hello"), (CLIENT_IP, 0)))
        runner = Runner(conn.recv)
        runner.start()
        self.assertEqual(runner.finish(self), b"hello")
        self.feed(conn, client_packet(CLIENT_SEQ + 5, flags=RST))
        conn.close()
        conn._thread.join(1.0)
        self.assertFalse(conn._thread.is_alive())


class RegistryTests(ConnectionTestCase):
    """note_client_syn, get_connection and finish_connection."""

    def setUp(self):
        """Use quiet settings for connections made by the registry."""
        ConnectionTestCase.setUp(self)
        patcher = mock.patch.object(tcp_connection, 'SETTINGS', make_settings())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(remove_packet_queue, KEY)

    def open_and_close(self):
        """
        Create the connection through the registry, then reset and finish it.

        Returns:
            TcpConnection: The connection that was created.
        """
        conn = tcp_connection.get_connection(self.sock, self.info)
        self.assertIs(tcp_connection.get_connection(self.sock, self.info), conn)
        with conn.cond:
            conn.reset = True
        tcp_connection.finish_connection(self.sock, self.info)
        self.assertNotIn(KEY, tcp_connection._connections)
        conn._thread.join(1.0)
        self.assertFalse(conn._thread.is_alive())
        return conn

    def test_mss_from_client_syn(self):
        """The fixture's SYN carries MSS 1460 and window 64240."""
        tcp_connection.note_client_syn(KEY, fixture_tcp('syn'))
        conn = self.open_and_close()
        self.assertEqual(conn.peer_mss, 1460)
        self.assertEqual(conn.mss, 1460)
        self.assertEqual(conn.sender.rwnd, 64240)
        self.assertNotIn(KEY, tcp_connection._syn_info)

    def test_mss_without_syn_option(self):
        """Without an MSS option the connection uses 536 bytes."""
        syn = fixture_tcp('syn')
        no_options = syn[:12] + bytes([0x50]) + syn[13:20]  # data offset 5, no options
        tcp_connection.note_client_syn(KEY, no_options)
        conn = self.open_and_close()
        self.assertEqual(conn.mss, 536)
        self.assertEqual(conn.sender.rwnd, 64240)

    def test_no_syn_noted(self):
        """With no SYN noted, MSS is 536 and the first rwnd is one MSS."""
        tcp_connection._syn_info.pop(KEY, None)
        conn = self.open_and_close()
        self.assertEqual(conn.mss, 536)
        self.assertEqual(conn.sender.rwnd, 536)

    def test_mss_limit_setting(self):
        """STREAPROUS_MSS lowers the MSS below the client's option."""
        tcp_connection.SETTINGS.mss_limit = 1000
        tcp_connection.note_client_syn(KEY, fixture_tcp('syn'))
        conn = self.open_and_close()
        self.assertEqual(conn.mss, 1000)


if __name__ == '__main__':
    unittest.main()

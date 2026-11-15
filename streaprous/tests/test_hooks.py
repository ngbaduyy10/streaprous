"""
Tests for our hooks in the teacher's raw connection library.

The public functions keep the teacher's names and positional parameters,
new parameters have defaults, the header builders put the window argument
into the TCP header, and the hooks hand their work to tcp_connection.
"""

import contextlib
import inspect
import io
import socket
import struct
import threading
import time
import unittest
from unittest import mock

from streaprous_socket import streaprous_packet_dispatcher as dispatcher
from streaprous_socket import streaprous_raw_connection as raw
from streaprous_socket import tcp_connection
from streaprous_socket.tcp_config import RECV_BUFFER
from streaprous_socket.tcp_segment import ACK, SYN, parse_tcp_packet
from tests.test_tcp_connection import (CLIENT_IP, CLIENT_PORT, KEY, SERVER_IP, SERVER_PORT,
                                       FakeSocket, fixture_tcp, make_info)

# The teacher's positional parameters, in order.
TEACHER_PARAMS = {
    'accept': ['s', 'listen_ip', 'listen_port', 'interface_name'],
    'run_conn_recv': ['s', 'conn_info'],
    'run_conn_send': ['s', 'conn_info', 'payload'],
    'close_connection': ['s', 'conn_info'],
    'build_tcp_packet': ['src_ip', 'dest_ip', 'src_port', 'dst_port', 'seq', 'ack_seq',
                         'syn', 'ack', 'fin'],
    'send_tcp_data_segment': ['s', 'src_ip', 'dest_ip', 'src_port', 'dst_port', 'seq',
                              'ack_seq', 'payload'],
}


def tcp_checksum_ok(src_ip, dst_ip, tcp):
    """
    Check a TCP checksum with the teacher's checksum function.

    Args:
        src_ip: Source IP address.
        dst_ip: Destination IP address.
        tcp: TCP header and payload, checksum field filled in.

    Returns:
        bool: True when the sum over pseudo header and segment checks out.
    """
    pseudo = struct.pack('!4s4sBBH', socket.inet_aton(src_ip), socket.inet_aton(dst_ip),
                         0, socket.IPPROTO_TCP, len(tcp))
    return raw.checksum(pseudo + tcp) == 0


def quiet(test):
    """
    Send stdout to a buffer until the test ends.

    Args:
        test: The running TestCase.
    """
    redirect = contextlib.redirect_stdout(io.StringIO())
    redirect.__enter__()
    test.addCleanup(redirect.__exit__, None, None, None)


class SignatureTests(unittest.TestCase):
    """The teacher's interface stays usable by his callers."""

    def test_positional_parameters_kept(self):
        """Each function starts with the teacher's parameters, in order."""
        for name, params in TEACHER_PARAMS.items():
            with self.subTest(function=name):
                found = list(inspect.signature(getattr(raw, name)).parameters.values())
                self.assertEqual([p.name for p in found[:len(params)]], params)
                for param in found:
                    self.assertEqual(param.kind, inspect.Parameter.POSITIONAL_OR_KEYWORD)

    def test_new_parameters_have_defaults(self):
        """Every parameter we added has a default, so old calls still work."""
        for name, params in TEACHER_PARAMS.items():
            with self.subTest(function=name):
                found = list(inspect.signature(getattr(raw, name)).parameters.values())
                for param in found[len(params):]:
                    self.assertIsNot(param.default, inspect.Parameter.empty, param.name)

    def test_added_parameters(self):
        """The added keywords are the ones the callers use."""
        recv = inspect.signature(raw.run_conn_recv).parameters
        self.assertIsNone(recv['max_bytes'].default)
        self.assertIsNone(recv['timeout'].default)
        for func in (raw.build_tcp_packet, raw.send_tcp_data_segment):
            self.assertEqual(inspect.signature(func).parameters['window'].default, RECV_BUFFER)

    def test_fixed_window_is_gone(self):
        """The old fixed window value no longer appears in the library."""
        self.assertNotIn('5840', inspect.getsource(raw))


class HeaderTests(unittest.TestCase):
    """The window argument reaches the TCP header."""

    def setUp(self):
        """Keep the teacher's prints out of the test output."""
        quiet(self)

    def test_build_tcp_packet_window(self):
        """build_tcp_packet writes the window field and a valid checksum."""
        header = raw.build_tcp_packet(SERVER_IP, CLIENT_IP, SERVER_PORT, CLIENT_PORT,
                                      seq=2001, ack_seq=7, ack=1, window=1234)
        self.assertEqual(len(header), 20)
        self.assertEqual(struct.unpack('!H', header[14:16])[0], 1234)
        self.assertEqual(header[13], ACK)
        self.assertTrue(tcp_checksum_ok(SERVER_IP, CLIENT_IP, header))

    def test_build_tcp_packet_default_window(self):
        """Without a window argument the whole receive buffer is advertised."""
        header = raw.build_tcp_packet(SERVER_IP, CLIENT_IP, SERVER_PORT, CLIENT_PORT,
                                      seq=2000, ack_seq=1, syn=1, ack=1)
        self.assertEqual(struct.unpack('!H', header[14:16])[0], RECV_BUFFER)

    def test_send_tcp_data_segment_window(self):
        """send_tcp_data_segment writes the window field and a valid checksum."""
        sock = FakeSocket()
        sent = raw.send_tcp_data_segment(sock, SERVER_IP, CLIENT_IP, SERVER_PORT, CLIENT_PORT,
                                         seq=2001, ack_seq=9, payload=b"hello", window=777)
        self.assertEqual(sent, 5)
        packet, addr = sock.packets[0]
        self.assertEqual(addr, (CLIENT_IP, 0))
        seg = parse_tcp_packet(packet)
        self.assertEqual((seg.seq, seg.ack, seg.window, seg.payload), (2001, 9, 777, b"hello"))
        self.assertTrue(tcp_checksum_ok(SERVER_IP, CLIENT_IP, packet[20:]))

    def test_send_tcp_data_segment_default_window(self):
        """Without a window argument the data segment advertises the whole buffer."""
        sock = FakeSocket()
        raw.send_tcp_data_segment(sock, SERVER_IP, CLIENT_IP, SERVER_PORT, CLIENT_PORT,
                                  2001, 9, b"x")
        self.assertEqual(parse_tcp_packet(sock.packets[0][0]).window, RECV_BUFFER)


class DelegationTests(unittest.TestCase):
    """run_conn_recv, run_conn_send and close_connection hand over to the connection."""

    def setUp(self):
        """Register the connection in the teacher's table."""
        quiet(self)
        self.sock = FakeSocket()
        self.info = make_info()
        with raw.connections_lock:
            raw.connections[KEY] = self.info
        self.addCleanup(self._forget)

    def _forget(self):
        """Remove the table entry and the queue."""
        with raw.connections_lock:
            raw.connections.pop(KEY, None)
        dispatcher.remove_packet_queue(KEY)

    def test_recv_unknown_connection(self):
        """run_conn_recv gives b"" for a connection not in the table."""
        self._forget()
        with mock.patch.object(tcp_connection, 'get_connection') as get:
            self.assertEqual(raw.run_conn_recv(self.sock, self.info), b"")
        get.assert_not_called()

    def test_recv_delegates(self):
        """run_conn_recv returns what TcpConnection.recv returns."""
        with mock.patch.object(tcp_connection, 'get_connection') as get:
            get.return_value.recv.return_value = b"abc"
            self.assertEqual(raw.run_conn_recv(self.sock, self.info, 5, timeout=1.5), b"abc")
        get.assert_called_once_with(self.sock, self.info)
        get.return_value.recv.assert_called_once_with(5, 1.5)

    def test_send_unknown_connection(self):
        """run_conn_send keeps the teacher's message and -1 for an unknown connection."""
        self._forget()
        out = io.StringIO()
        with mock.patch.object(tcp_connection, 'get_connection') as get:
            with contextlib.redirect_stdout(out):
                self.assertEqual(raw.run_conn_send(self.sock, self.info, b"x"), -1)
        get.assert_not_called()
        self.assertIn('Connection not found', out.getvalue())

    def test_send_delegates(self):
        """run_conn_send returns what TcpConnection.send returns."""
        with mock.patch.object(tcp_connection, 'get_connection') as get:
            get.return_value.send.return_value = 3
            self.assertEqual(raw.run_conn_send(self.sock, self.info, b"abc"), 3)
        get.return_value.send.assert_called_once_with(b"abc")

    def test_close_connection(self):
        """close_connection finishes the TCP connection, then drops the entry and queue."""
        dispatcher.get_packet_queue(KEY)
        with mock.patch.object(tcp_connection, 'finish_connection') as finish:
            raw.close_connection(self.sock, self.info)
        finish.assert_called_once_with(self.sock, self.info)
        self.assertNotIn(KEY, raw.connections)
        self.assertNotIn(KEY, dispatcher._packet_queues)


class AcceptSocket(FakeSocket):
    """Fake socket that the dispatcher thread can poll."""

    def settimeout(self, value):
        """
        Accept the dispatcher's timeout.

        Args:
            value: Seconds (ignored).
        """

    def recvfrom(self, size):
        """
        Pretend nothing arrived; the test fills the queues itself.

        Args:
            size: Buffer size (ignored).
        """
        time.sleep(0.02)
        raise socket.timeout()


class AcceptHookTests(unittest.TestCase):
    """accept notes the client's SYN for the TCP connection."""

    def setUp(self):
        """Quiet output and plan the clean-up of the global tables."""
        quiet(self)
        self.addCleanup(self._clean)

    def _clean(self):
        """Stop the dispatcher and forget everything accept registered."""
        dispatcher.stop_packet_dispatcher()
        dispatcher.remove_packet_queue(KEY)
        dispatcher.remove_packet_queue(dispatcher._SYN_KEY)
        with raw.connections_lock:
            raw.connections.pop(KEY, None)
        with raw.handshake_lock:
            raw.handshake_in_progress.pop(KEY, None)
        tcp_connection._syn_info.pop(KEY, None)

    def test_accept_notes_syn(self):
        """The fixture's handshake completes and its MSS and window are noted."""
        ip_header = struct.pack('!BBHHHBBH4s4s', 0x45, 0, 0, 1, 0x4000, 64,
                                socket.IPPROTO_TCP, 0, socket.inet_aton(CLIENT_IP),
                                socket.inet_aton(SERVER_IP))
        syn = fixture_tcp('syn')
        final_ack = fixture_tcp('handshake_ack')
        dispatcher.get_packet_queue(dispatcher._SYN_KEY).put((ip_header + syn, (CLIENT_IP, 0)))
        dispatcher.get_packet_queue(KEY).put((ip_header + final_ack, (CLIENT_IP, 0)))
        sock = AcceptSocket()
        result = []
        thread = threading.Thread(
            target=lambda: result.append(raw.accept(sock, SERVER_IP, SERVER_PORT)),
            daemon=True)
        thread.start()
        thread.join(5.0)
        self.assertFalse(thread.is_alive(), 'accept did not return')
        session = result[0]
        self.assertEqual(session.state, 'ESTABLISHED')
        self.assertEqual(session.server_seq, 2001)
        self.assertEqual(tcp_connection._syn_info[KEY], (1460, 64240))
        syn_ack = parse_tcp_packet(sock.packets[0][0])
        self.assertEqual(syn_ack.flags, SYN | ACK)
        self.assertEqual(syn_ack.window, RECV_BUFFER)


if __name__ == '__main__':
    unittest.main()

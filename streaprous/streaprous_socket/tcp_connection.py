"""
StreapRous TCP connection
Joins the TCP sender and receiver to the raw socket of one connection. A worker thread
reads the connection's queue, feeds ACKs to the sender and data to the receiver, and runs
the timers. recv, send and close wait on the connection's condition. Every segment we send
goes out through put_on_wire.
"""

import os
import queue
import socket
import struct
import threading
import time
from collections import namedtuple

from .streaprous_raw_connection import (build_tcp_packet, build_ip_header,
                                        send_tcp_data_segment, connections, connections_lock)
from .streaprous_packet_dispatcher import get_packet_queue
from .tcp_config import SETTINGS
from .tcp_loss import LossInjector
from .tcp_receiver import TcpReceiver
from .tcp_reno import RenoCongestion
from .tcp_rtt import RttEstimator
from .tcp_segment import ACK, FIN, RST, SYN, parse_mss_option, parse_tcp_packet, unwrap, wrap
from .tcp_sender import TcpSender
from .tcp_trace import TcpTrace

MAX_MSS = 1460          # extra: largest MSS we use, the client's option at most
DEFAULT_MSS = 536       # extra: MSS when the client's SYN had no MSS option
PEER_SILENCE = 10.0     # extra: give up after 10 s without any segment while data waits
LINGER = 2.0            # extra: wait up to 2 s for the client's FIN after ours is ACKed
TICK = 0.01             # the worker runs the timers at least every 10 ms

# From the client's SYN; mss is None when the SYN had no MSS option.
SynInfo = namedtuple('SynInfo', ['mss', 'window', 'seq'])


def choose_mss(syn):
    """Picks the MSS of the connection.

    extra: the client's MSS option, at most 1460 bytes; 536 bytes without it. The
    slides give MSS no value, and a larger segment than the client asked for may never arrive.

    Args:
         syn  SynInfo of the client's SYN, or None when no SYN was noted

    Returns:
         the MSS in bytes
    """
    if syn is not None and syn.mss is not None:
        return min(MAX_MSS, syn.mss)
    return DEFAULT_MSS


class TcpConnection:
    """One TCP connection on the raw socket; cond guards all of its state.

    Attributes:
         broken      None, or 'reset', 'silence' or 'error' once the connection broke
         closing     True once close() was called; finished once it is done
         last_heard  when the last segment from the client came
    """

    def __init__(self, sock, conn_info, packet_queue, syn=None, settings=SETTINGS,
                 clock=time.monotonic):
        """Builds the sender, receiver and helpers; the worker starts in start().

        Args:
             sock          raw socket used for sendto
             conn_info     conn_struct of the established connection
             packet_queue  the dispatcher's queue.Queue for this connection
             syn           SynInfo of the client's SYN, or None when none was noted
             settings      settings object from tcp_config.load_settings
             clock         function returning seconds
        """
        self.sock = sock
        self.conn_info = conn_info
        self.queue = packet_queue
        self.settings = settings
        self.clock = clock
        self.key = (conn_info.client_ip, conn_info.client_port)
        self.name = '%s:%d' % self.key
        self.mss = choose_mss(syn)
        # The client's first data byte is SYN seq + 1. The handshake's third packet
        # can be a reordered data segment, so accept's client_seq is used only without a SYN.
        first_client_byte = conn_info.client_seq if syn is None else wrap(syn.seq + 1)
        rwnd = self.mss if syn is None else syn.window
        self.cond = threading.Condition()
        csv_path = os.path.join(settings.trace_dir, 'trace_%s_%d.csv' % self.key)
        if not settings.trace:
            csv_path = None
        self.trace = TcpTrace(self.name, csv_path, clock=clock)
        self.rtt = RttEstimator()
        self.reno = RenoCongestion(self.mss, log=self.log)
        self.sender = TcpSender(conn_info.server_seq, self.mss, rwnd, self.reno, self.rtt,
                                self.log)
        self.receiver = TcpReceiver(first_client_byte, settings.rcv_buffer, self.log)
        self.loss = LossInjector(settings.drop_send, settings.loss_rate, settings.loss_seed)
        self.broken = None
        self.closing = False
        self.finished = False
        self.started_at = clock()
        self.last_heard = self.started_at
        self.thread = None

    def start(self):
        """Logs OPEN and starts the worker thread "tcp-<ip>:<port>"."""
        drop_send = ','.join(str(n) for n in sorted(self.settings.drop_send)) or '-'
        self.log('OPEN', mss=self.mss, rwnd=self.sender.rwnd,
                 rcv_buffer=self.settings.rcv_buffer, send_base=self.sender.send_base,
                 expected=self.receiver.expected_seq_num, drop_send=drop_send,
                 loss=self.settings.loss_rate, trace=self.settings.trace)
        self.thread = threading.Thread(target=self.worker, daemon=True, name='tcp-' + self.name)
        self.thread.start()

    def worker(self):
        """Takes one packet at a time from the queue and runs the timers every TICK.

        Any error breaks the connection and stops the worker.
        """
        while not self.finished:
            try:
                packet = self.queue.get(timeout=TICK)[0]
            except queue.Empty:
                packet = None
            with self.cond:
                if self.finished:
                    return
                try:
                    now = self.clock()
                    if packet is not None:
                        self.process_packet(packet, now)
                    self.process_timers(now)
                except Exception as err:
                    print('[Streap_tcp] %s worker error: %r' % (self.name, err))
                    self.give_up('error')
                    return
                self.cond.notify_all()

    def process_packet(self, packet, now):
        """Handles one packet from the queue; the caller holds self.cond.

        The ACK field goes to the sender (slide 3-84), then the data and FIN to the
        receiver, which says whether an ACK must go out now (slide 3-85).

        Args:
             packet  IP datagram bytes from the raw socket
             now     current time in seconds
        """
        seg = parse_tcp_packet(packet)
        if seg is None:
            return
        if seg.src_port != self.conn_info.client_port or seg.dst_port != self.conn_info.server_port:
            return
        self.last_heard = now
        if seg.flags & RST:
            self.broken = 'reset'
            self.log('RESET')
            return
        if seg.flags & SYN:
            return
        fin = bool(seg.flags & FIN)
        if seg.flags & ACK:
            ack = unwrap(seg.ack, self.sender.send_base)
            carries_data = bool(seg.payload) or fin
            self.send_segments(self.sender.on_ack(ack, seg.window, carries_data, now))
        seq = unwrap(seg.seq, self.receiver.expected_seq_num)
        reason = self.receiver.on_segment(seq, seg.payload, fin, now)
        if reason:
            self.send_ack(reason)

    def process_timers(self, now):
        """Runs the timers; the caller holds self.cond.

        The sender's timer and probe timer, then the delayed ACK of slide 3-85.
        extra: we give up after 10 s without any segment from the client while data
        waits; else a vanished client keeps the timer resending and send waits forever.

        Args:
             now  current time in seconds
        """
        if self.broken is not None:
            return
        self.send_segments(self.sender.on_tick(now))
        if self.receiver.on_tick(now):
            self.send_ack('delayed')
        waiting = (self.sender.in_flight() > 0
                   or self.sender.next_seq_num < self.sender.data_end())
        if waiting and now - self.last_heard > PEER_SILENCE:
            self.give_up('silence')

    def send_segments(self, segments):
        """Puts the sender's segments on the wire, unless the loss switch drops them.

        Args:
             segments  list of SendSegment from the sender
        """
        for segment in segments:
            if segment.payload:
                why = self.loss.check_send(segment.kind)
                if why is not None:
                    self.log('DROP', seq=segment.seq, len=len(segment.payload), why=why)
                    continue
            self.put_on_wire(segment.seq, segment.payload, segment.fin)

    def send_ack(self, reason):
        """Sends a pure ACK with seq # NextSeqNum for the next byte we expect.

        Slide 3-85 says when to ACK; for the seq # see slide 3-80: Seq=43, ACK=80.

        Args:
             reason  why the ACK goes out, such as 'delayed', 'duplicate' or 'window_update'
        """
        if not self.put_on_wire(self.sender.next_seq_num, b"", False):
            return
        ack = self.receiver.expected_seq_num
        rwnd = self.receiver.last_rwnd
        self.log('ACK_SENT', ack=ack, rwnd=rwnd, reason=reason)
        if reason == 'duplicate':
            self.log('DUP_ACK_SENT', ack=ack)
        elif reason == 'window_update':
            self.log('WINDOW_UPDATE', rwnd=rwnd)

    def put_on_wire(self, seq, payload, fin):
        """Sends one segment with ACK = expected seq # and our free buffer space.

        Slide 3-94: TCP receiver "advertises" free buffer space in rwnd field
        in TCP header. Any segment we send also clears a pending delayed ACK.

        Args:
             seq      unbounded seq # of the segment
             payload  data bytes, b"" for a FIN or pure ACK
             fin      True to set the FIN bit

        Returns:
             True when sent; False on a send error, and the segment counts as lost
        """
        info = self.conn_info
        rwnd = self.receiver.rwnd()
        ack_seq = wrap(self.receiver.expected_seq_num)
        try:
            if payload:
                send_tcp_data_segment(self.sock, info.server_ip, info.client_ip,
                                      info.server_port, info.client_port, seq=wrap(seq),
                                      ack_seq=ack_seq, payload=payload, window=rwnd)
            else:
                tcp = build_tcp_packet(info.server_ip, info.client_ip, info.server_port,
                                       info.client_port, seq=wrap(seq), ack_seq=ack_seq,
                                       ack=1, fin=int(fin), window=rwnd)
                ip_header = build_ip_header(info.server_ip, info.client_ip,
                                            socket.IPPROTO_TCP, len(tcp))
                self.sock.sendto(ip_header + tcp, (info.client_ip, 0))
        except OSError as err:
            print('[Streap_tcp] %s send error: %s' % (self.name, err))
            return False
        self.receiver.ack_sent(rwnd)
        return True

    def give_up(self, reason):
        """Marks the connection broken and wakes the waiting calls.

        Args:
             reason  'silence' or 'error'
        """
        self.broken = reason
        self.log('GIVE_UP', reason=reason)
        self.cond.notify_all()

    def wait_until(self, done, deadline=None):
        """Waits on self.cond until done(), a broken or finished connection, or the deadline.

        Args:
             done      function returning True when the wait is over
             deadline  clock time to stop at, or None to wait without a limit
        """
        while not done():
            if self.broken is not None or self.finished:
                return
            if deadline is not None and self.clock() >= deadline:
                return
            self.cond.wait(0.1)

    def log(self, event, **details):
        """Passes one event to the trace with the current numbers.

        Args:
             event    event name
             details  key=value pairs for the log line
        """
        self.trace.event(event, self.state(), **details)

    def state(self):
        """Gives the numbers every trace row records."""
        return {'cwnd': self.reno.cwnd, 'ssthresh': self.reno.ssthresh, 'rwnd': self.sender.rwnd,
                'in_flight': self.sender.in_flight(), 'send_base': self.sender.send_base,
                'next_seq_num': self.sender.next_seq_num, 'adv_rwnd': self.receiver.last_rwnd}

    def update_table(self):
        """Writes our sequence numbers into the teacher's connections entry."""
        with connections_lock:
            entry = connections.get(self.key)
            if entry is not None:
                connections[self.key] = entry._replace(
                    server_seq=wrap(self.sender.next_seq_num),
                    client_seq=wrap(self.receiver.expected_seq_num),
                    last_activity=time.time())

    def recv(self, max_bytes=None, timeout=None):
        """Returns the next in-order bytes; a window update follows if one is due.

        Args:
             max_bytes  most bytes to return; None returns all that is ready
             timeout    seconds to wait for bytes; None waits until they come

        Returns:
             bytes; b"" when the client closed, the connection broke or the timeout passed
        """
        with self.cond:
            deadline = None if timeout is None else self.clock() + timeout
            self.wait_until(self.receiver.readable, deadline)
            data = self.receiver.read(max_bytes)
            if self.receiver.window_update_due():
                self.send_ack('window_update')
            self.update_table()
            return data

    def send(self, payload):
        """Sends payload reliably and returns once every byte is ACKed.

        Args:
             payload  bytes to send

        Returns:
             len(payload), 0 for b"", or -1 when the connection broke or is closing
        """
        if not payload:
            return 0
        with self.cond:
            if self.broken is not None or self.closing:
                return -1
            self.send_segments(self.sender.send_data(payload, self.clock()))
            self.wait_until(self.sender.data_acked)
            self.update_table()
            if not self.sender.data_acked():
                return -1
            return len(payload)

    def close(self):
        """Sends our FIN until it is ACKed, then waits for the client's FIN.

        Slide 3-103: respond to received FIN with ACK; the worker sends that ACK.
        extra: after our FIN is ACKed we wait up to 2 s for the client's FIN.
        """
        with self.cond:
            if self.finished:
                return
            self.closing = True
            if self.broken is None:
                self.send_segments(self.sender.close(self.clock()))
                self.wait_until(self.sender.fin_acked)
            if self.sender.fin_acked():
                self.wait_until(lambda: self.receiver.fin_received, self.clock() + LINGER)
            self.finished = True
            self.update_table()
            self.log('CLOSE', seconds=self.clock() - self.started_at)
            self.trace.close()
            self.cond.notify_all()


# Registry of live connections and of the SYNs noted by accept.
_connections = {}
_syn_info = {}
_registry_lock = threading.Lock()


def note_client_syn(client_key, tcp_bytes):
    """Remembers the MSS option, window and seq # of a client's SYN.

    Args:
         client_key  (client_ip, client_port)
         tcp_bytes   TCP header with options, as accept slices it from the packet
    """
    if len(tcp_bytes) < 20:
        return
    seq = struct.unpack('!L', tcp_bytes[4:8])[0]
    window = struct.unpack('!H', tcp_bytes[14:16])[0]
    with _registry_lock:
        _syn_info[client_key] = SynInfo(parse_mss_option(tcp_bytes), window, seq)


def get_connection(sock, conn_info):
    """Finds the connection of conn_info, creating and starting it on first use.

    Args:
         sock       raw socket
         conn_info  conn_struct of the established connection

    Returns:
         the TcpConnection
    """
    key = (conn_info.client_ip, conn_info.client_port)
    with _registry_lock:
        conn = _connections.get(key)
        if conn is None:
            conn = TcpConnection(sock, conn_info, get_packet_queue(key),
                                 _syn_info.pop(key, None), SETTINGS)
            _connections[key] = conn
            conn.start()
        return conn


def finish_connection(sock, conn_info):
    """Closes the connection of conn_info and forgets it.

    Args:
         sock       raw socket
         conn_info  conn_struct of the connection
    """
    key = (conn_info.client_ip, conn_info.client_port)
    conn = get_connection(sock, conn_info)
    try:
        conn.close()
    finally:
        with _registry_lock:
            if _connections.get(key) is conn:
                del _connections[key]
            _syn_info.pop(key, None)

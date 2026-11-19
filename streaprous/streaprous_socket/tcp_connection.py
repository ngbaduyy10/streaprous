"""
StreapRous TCP connection
Joins the TCP sender and receiver to the raw socket of one connection.
A worker thread per connection is the only reader of the dispatcher's
per-connection queue: it feeds ACKs to the sender, data to the receiver and
runs the timers. The handler thread calls recv, send and close, which wait
on the connection's condition until the worker has done its part.
"""

import os
import queue
import socket
import struct
import threading
import time

from .streaprous_raw_connection import (
    build_tcp_packet,
    build_ip_header,
    send_tcp_data_segment,
    connections,
    connections_lock,
)
from .streaprous_packet_dispatcher import get_packet_queue
from .tcp_config import SETTINGS, describe
from .tcp_loss import LossInjector
from .tcp_receiver import TcpReceiver
from .tcp_reno import RenoCongestion
from .tcp_rtt import RttEstimator
from .tcp_segment import ACK, FIN, RST, SYN, parse_mss_option, parse_tcp_packet, unwrap, wrap
from .tcp_sender import TcpSender
from .tcp_trace import TcpTrace

# Most packets the worker takes from the queue before it runs the timers.
_BATCH = 64


class TcpConnection:
    """One TCP connection on the raw socket.

    Args:
         sock          raw socket used for sendto
         conn_info     conn_struct of the established connection
         packet_queue  the dispatcher's queue.Queue for this connection
         peer_mss      MSS the client asked for in its SYN
         peer_window   window of the client's SYN, or None when unknown
         settings      settings object from tcp_config.load_settings
         clock         function returning seconds
         syn_seq       sequence number of the client's SYN, or None when unknown
    """

    def __init__(self, sock, conn_info, packet_queue, peer_mss, peer_window, settings,
                 clock=time.monotonic, syn_seq=None):
        """Builds the sender, receiver and helpers; the worker starts in start().

        Args:
             sock          raw socket used for sendto
             conn_info     conn_struct of the established connection
             packet_queue  the dispatcher's queue.Queue for this connection
             peer_mss      MSS the client asked for in its SYN
             peer_window   window of the client's SYN, or None when unknown
             settings      settings object from tcp_config.load_settings
             clock=time.monotonic  function returning seconds
             syn_seq=None  sequence number of the client's SYN, or None when unknown
        """
        self.sock = sock
        self.conn_info = conn_info
        self.queue = packet_queue
        self.settings = settings
        self.clock = clock
        self.client_ip = conn_info.client_ip
        self.client_port = conn_info.client_port
        self.server_ip = conn_info.server_ip
        self.server_port = conn_info.server_port
        self.key = (self.client_ip, self.client_port)
        self.name = '%s:%d' % self.key

        self.peer_mss = peer_mss
        self.mss = min(settings.mss_limit, peer_mss)
        self.iss = conn_info.server_seq
        # The first data byte is SYN seq + 1. The handshake's third packet can be a
        # reordered data segment, so its seq is used only when the SYN was not seen.
        if syn_seq is not None:
            self.irs = wrap(syn_seq + 1)
        else:
            self.irs = conn_info.client_seq
        rwnd = peer_window if peer_window is not None else self.mss

        self.cond = threading.Condition()
        csv_path = None
        if settings.trace:
            csv_path = os.path.join(settings.trace_dir,
                                    'trace_%s_%d.csv' % (self.client_ip, self.client_port))
        self.trace = TcpTrace(self.name, settings.log_level, csv_path, clock=clock)
        self.rtt = RttEstimator(settings.initial_rto, settings.min_rto, settings.max_rto)
        # One RenoCongestion per TCP connection. The book (3.7.1, 3.7.3)
        # describes congestion control per connection: each connection
        # probes the path with its own cwnd, and AIMD makes connections that
        # share a bottleneck converge to about equal shares. So a loss on
        # one connection does not slow down another.
        # RenoCongestion(...) calls the constructor __init__. The arguments
        # go in the order it expects them: MSS, initial ssthresh, logger.
        self.cc = RenoCongestion(self.mss, settings.initial_ssthresh, self._on_event)
        self.sender = TcpSender(self.iss, self.mss, rwnd, self.rtt, self.cc,
                                settings.max_retries, settings.persist_min,
                                settings.persist_max, self._on_event)
        self.receiver = TcpReceiver(self.irs, settings.recv_buffer, settings.delayed_ack,
                                    self._on_event)
        self.loss = LossInjector(settings.drop_send, settings.drop_recv,
                                 settings.loss_rate, settings.loss_seed)

        # The SYN-ACK advertised the whole buffer.
        self.adv_wnd = settings.recv_buffer
        self.reset = False
        self.closing = False
        self.finished = False
        self.received = 0
        self.started_at = clock()
        self.last_peer_time = self.started_at
        self._thread = None

    # ------------------------------------------------------------------
    # Worker side
    # ------------------------------------------------------------------

    def start(self):
        """Logs OPEN and starts the worker thread "tcp-<ip>:<port>"."""
        self._on_event('OPEN', iss=self.iss, irs=self.irs, mss=self.mss,
                       peer_mss=self.peer_mss, rcvbuf=self.settings.recv_buffer,
                       rwnd=self.sender.rwnd, settings=describe(self.settings))
        self._thread = threading.Thread(target=self._worker, daemon=True,
                                        name='tcp-' + self.name)
        self._thread.start()

    def _worker(self):
        """Reads the queue, handles packets and timers until the connection is finished."""
        while not self.finished:
            packets = []
            try:
                packets.append(self.queue.get(timeout=self.settings.tick))
                while len(packets) < _BATCH:
                    packets.append(self.queue.get_nowait())
            except queue.Empty:
                pass
            with self.cond:
                if self.finished:
                    break
                try:
                    now = self.clock()
                    for packet, _addr in packets:
                        self.process_packet(packet, now)
                    self.process_timers(now)
                except Exception as err:
                    print('[Streap_tcp] %s worker error: %r' % (self.name, err))
                    self.sender.gave_up = True
                    self._on_event('GIVE_UP', reason='error')
                    self.cond.notify_all()
                    break
                self.cond.notify_all()

    def process_packet(self, packet, now):
        """Handles one packet from the queue; the caller holds self.cond.

        The ACK field goes to the sender, the data and FIN to the receiver,
        which says whether an ACK must go out now (slide 3-85).

        Args:
             packet  IP datagram bytes from the raw socket
             now     current time in seconds
        """
        seg = parse_tcp_packet(packet)
        if seg is None:
            return
        if seg.src_port != self.client_port or seg.dst_port != self.server_port:
            return
        self.last_peer_time = now
        if seg.flags & RST:
            self.reset = True
            self._on_event('RESET')
            return
        if seg.flags & SYN:
            self._on_event('IGNORED', why='syn')
            return
        payload = seg.payload
        if payload:
            why = self.loss.check_recv()
            if why:
                self._on_event('DROP', dir='recv', why=why,
                               seq=unwrap(seg.seq, self.receiver.rcv_nxt), len=len(payload))
                return
            self.received += 1
        fin = bool(seg.flags & FIN)
        if seg.flags & ACK:
            ack = unwrap(seg.ack, self.sender.snd_una)
            # a FIN is not a duplicate ACK either (RFC 5681, background)
            self._transmit(self.sender.on_ack(ack, seg.window, bool(payload) or fin, now), now)
        reason = self.receiver.on_segment(unwrap(seg.seq, self.receiver.rcv_nxt),
                                          payload, fin, now)
        if reason:
            self._send_ack(reason, now)

    def process_timers(self, now):
        """Runs the sender's timers and the delayed ACK; the caller holds self.cond.

        Args:
             now  current time in seconds
        """
        self._transmit(self.sender.on_tick(now), now)
        if self.receiver.on_tick(now):
            self._send_ack('delayed', now)
        waiting = (self.sender.bytes_in_flight() > 0
                   or self.sender.snd_nxt < self.sender.data_end())
        if (waiting and not self.sender.gave_up
                and now - self.last_peer_time > self.settings.peer_silence):
            self.sender.gave_up = True
            self._on_event('GIVE_UP', reason='silence')

    def _transmit(self, segments, now):
        """Puts the sender's segments on the wire, each carrying ack=rcv_nxt.

        Args:
             segments  list of SendSegment from the sender
             now       current time in seconds
        """
        for seg in segments:
            window = self.receiver.window()
            ack_seq = wrap(self.receiver.rcv_nxt)
            try:
                if seg.payload:
                    why = self.loss.check_send(seg.kind)
                    if why:
                        self._on_event('DROP', dir='send', why=why, seq=seg.seq,
                                       len=len(seg.payload))
                        continue
                    send_tcp_data_segment(self.sock, self.server_ip, self.client_ip,
                                          self.server_port, self.client_port,
                                          seq=wrap(seg.seq), ack_seq=ack_seq,
                                          payload=seg.payload, window=window)
                else:
                    tcp = build_tcp_packet(self.server_ip, self.client_ip, self.server_port,
                                           self.client_port, seq=wrap(seg.seq),
                                           ack_seq=ack_seq, ack=1, fin=1, window=window)
                    self._sendto(tcp)
            except OSError as err:
                # counts as a lost segment; the timer resends it
                print('[Streap_tcp] %s send error: %s' % (self.name, err))
                continue
            self._note_advertised(window)
            self.receiver.ack_sent(window)

    def _sendto(self, tcp):
        """Adds the IP header and sends one TCP segment.

        Args:
             tcp  TCP header (and payload) bytes
        """
        ip_header = build_ip_header(self.server_ip, self.client_ip, socket.IPPROTO_TCP, len(tcp))
        self.sock.sendto(ip_header + tcp, (self.client_ip, 0))

    def _send_ack(self, reason, now):
        """Sends a pure ACK for rcv_nxt with the current window (slides 3-85, 3-95).

        The ACK rules of slide 3-85 pick when; the window field tells the
        client how much free buffer space we have (slide 3-95).

        Args:
             reason  why the ACK goes out, such as 'delayed', 'duplicate' or 'window_update'
             now     current time in seconds
        """
        window = self.receiver.window()
        ack = self.receiver.rcv_nxt
        # pure ACKs carry snd_max, so a go-back after a timeout never puts
        # them below what the client already has (BSD tcp_output, background)
        tcp = build_tcp_packet(self.server_ip, self.client_ip, self.server_port,
                               self.client_port, seq=wrap(self.sender.snd_max),
                               ack_seq=wrap(ack), ack=1, window=window)
        try:
            self._sendto(tcp)
        except OSError as err:
            print('[Streap_tcp] %s send error: %s' % (self.name, err))
            return
        self._note_advertised(window)
        self.receiver.ack_sent(window)
        self._on_event('ACK_SENT', ack=ack, window=window, reason=reason)
        if reason == 'duplicate':
            self._on_event('DUP_ACK_SENT', ack=ack)
        elif reason == 'window_update':
            self._on_event('WINDOW_UPDATE', window=window)

    def _note_advertised(self, window):
        """Remembers the window we sent; logs WINDOW_ZERO when it closes.

        Args:
             window  window field of the segment just sent
        """
        old = self.adv_wnd
        self.adv_wnd = window
        if window == 0 and old > 0:
            self._on_event('WINDOW_ZERO', ack=self.receiver.rcv_nxt)

    def _state(self):
        """Gives the numbers every trace row records.

        Returns:
             dict with the numeric CSV columns
        """
        return {
            'cwnd': self.cc.cwnd,
            'ssthresh': self.cc.ssthresh,
            'rwnd': self.sender.rwnd,
            'in_flight': self.sender.bytes_in_flight(),
            'snd_una': self.sender.snd_una,
            'snd_nxt': self.sender.snd_nxt,
            'adv_wnd': self.adv_wnd,
        }

    def _on_event(self, name, **details):
        """Passes one event to the trace with the current state.

        Args:
             name     event name
             details  key=value pairs for the log line
        """
        self.trace.event(name, self._state(), **details)

    def _update_table(self):
        """Writes our sequence numbers into the teacher's connections entry."""
        with connections_lock:
            entry = connections.get(self.key)
            if entry is not None:
                connections[self.key] = entry._replace(
                    server_seq=wrap(self.sender.snd_nxt),
                    client_seq=wrap(self.receiver.rcv_nxt),
                    last_activity=time.time())

    def _broken(self):
        """Tells whether the connection was reset or gave up.

        Returns:
             True after an RST or a give-up
        """
        return self.reset or self.sender.gave_up

    # ------------------------------------------------------------------
    # Application side
    # ------------------------------------------------------------------

    def recv(self, max_bytes=None, timeout=None):
        """Returns the next in-order bytes of the stream.

        The bytes need not match the client's send calls: TCP delivers a
        byte stream. Reading frees buffer space, so a window update goes
        out when the window grows past a quarter of the buffer.

        Args:
             max_bytes=None  most bytes to return; None returns all that is ready
             timeout=None    seconds to wait for bytes; None waits until they come

        Returns:
             bytes; b"" when the client closed, the connection was reset or
             gave up, or the timeout passed
        """
        with self.cond:
            deadline = None if timeout is None else self.clock() + timeout
            while not self.receiver.readable():
                if self._broken() or self.finished:
                    break
                if deadline is not None and self.clock() >= deadline:
                    break
                self.cond.wait(0.1)
            data = self.receiver.read(max_bytes)
            if self.receiver.window_update_due():
                self._send_ack('window_update', self.clock())
            self._update_table()
            return data

    def send(self, payload):
        """Sends payload reliably and returns once every byte is ACKed.

        Args:
             payload  bytes to send

        Returns:
             len(payload), or -1 when the connection was reset, gave up or is closing
        """
        if not payload:
            return 0
        with self.cond:
            if self._broken() or self.closing or self.finished:
                return -1
            now = self.clock()
            self._transmit(self.sender.write(payload, now), now)
            while not self.sender.data_acked():
                if self._broken() or self.finished:
                    self._update_table()
                    return -1
                self.cond.wait(0.1)
            self._update_table()
            return len(payload)

    def close(self):
        """Sends our FIN until it is ACKed, then waits for the client's FIN (slide 3-96).

        The FIN takes one sequence number and the retransmission timer
        resends it like data. After its ACK we linger up to settings.linger
        seconds so the client's FIN gets an ACK, then the worker stops.
        """
        with self.cond:
            if self.finished:
                return
            self.closing = True
            if not self._broken():
                now = self.clock()
                self._transmit(self.sender.close(now), now)
                while not self.sender.fin_acked() and not self._broken():
                    self.cond.wait(0.1)
                if self.sender.fin_acked():
                    deadline = self.clock() + self.settings.linger
                    while (not self.receiver.fin_received and not self.reset
                           and self.clock() < deadline):
                        self.cond.wait(0.1)
            self.finished = True
            self._update_table()
            self._on_event('CLOSE', sent=self.sender.segments_sent,
                           retransmits=self.sender.retransmits,
                           timeouts=self.sender.timeouts,
                           fast_retransmits=self.sender.fast_retransmits,
                           probes=self.sender.probes, received=self.received,
                           seconds=self.clock() - self.started_at)
            self.trace.close()
            self.cond.notify_all()


# Registry of live connections and of the SYN options seen by accept.
_connections = {}
_syn_info = {}
_registry_lock = threading.Lock()


def note_client_syn(client_key, tcp_bytes):
    """Remembers the MSS option, window and sequence number of a client's SYN.

    Args:
         client_key  (client_ip, client_port)
         tcp_bytes   TCP header with options, as accept slices it from the packet
    """
    if len(tcp_bytes) < 20:
        return
    seq = struct.unpack('!L', tcp_bytes[4:8])[0]
    window = struct.unpack('!H', tcp_bytes[14:16])[0]
    with _registry_lock:
        _syn_info[client_key] = (parse_mss_option(tcp_bytes), window, seq)


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
            mss_option, window, syn_seq = _syn_info.pop(key, (None, None, None))
            peer_mss = mss_option if mss_option is not None else SETTINGS.default_peer_mss
            conn = TcpConnection(sock, conn_info, get_packet_queue(key), peer_mss, window,
                                 SETTINGS, syn_seq=syn_seq)
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

"""
StreapRous TCP receiver
The ACK rules of slide 3-85, in-order delivery and the window of slide 3-94.
No sockets, no clock: the caller passes segments and ``now`` and sends the ACKs.
"""

from .tcp_trace import no_log

DELAYED_ACK_WAIT = 0.1  # slide 3-85: "wait up to 500ms"; we wait 100 ms


class TcpReceiver:
    """Receiving side of one TCP connection; sequence numbers are plain ints."""

    def __init__(self, expected_seq_num, rcv_buffer, log=no_log):
        """Sets up an empty receive buffer.

        Args:
             expected_seq_num  seq # of the first byte we expect from the client
             rcv_buffer        RcvBuffer in bytes (slide 3-94)
             log               log(event, **details), called after each change
        """
        self.expected_seq_num = expected_seq_num
        self.rcv_buffer = rcv_buffer
        self.buffered_data = bytearray()
        self.out_of_order = {}
        self.ack_pending = False
        self.ack_deadline = None
        self.fin_received = False
        self.last_rwnd = rcv_buffer
        self.log = log

    def on_segment(self, seq, data, fin, now):
        """Handles one arriving segment and picks its ACK (slide 3-85, ACK generation).

        An empty segment elsewhere than ``expected_seq_num`` is a probe and gets
        an ACK. A FIN counts only on an in-order segment not cut at the window edge.

        ACK generation follows the table of slide 3-85 (book 3.5.4): in-order
        segment, nothing pending -> wait (delayed ACK); in-order segment, ACK pending
        -> send one cumulative ACK now; segment above the expected number (a gap) ->
        send a duplicate ACK at once; segment that fills a gap -> ACK at once. The
        duplicate ACKs are exactly what the sender's fast retransmit (3-88) and Reno
        (3-125) depend on.

        Args:
             seq   seq # of the first data byte, already unwrapped
             data  the payload bytes (may be empty)
             fin   True when the FIN bit is set
             now   the current time in seconds

        Returns:
             the reason to send an ACK now, or None
        """
        data = bytes(data)
        if not data and not fin:
            return None if seq == self.expected_seq_num else 'probe'
        if self.is_old_segment(seq, data, fin):
            self.log('DUP_SEGMENT', seq=seq, len=len(data))
            return 'old_segment'
        seq, data, fin = self.trim_to_window(seq, data, fin)
        if not data and not fin:
            return 'out_of_window'
        if seq > self.expected_seq_num:
            if data:
                self.store_out_of_order(seq, data)
            return 'duplicate'
        gap_was_open = bool(self.out_of_order)
        self.deliver_in_order(data)
        if fin:
            # Nothing can follow a FIN, so it is taken before the stored segments
            self.fin_received = True
            self.expected_seq_num += 1
            self.log('FIN_RECEIVED', seq=self.expected_seq_num - 1)
            return 'fin'
        self.deliver_from_out_of_order()
        return self.choose_ack(gap_was_open, now)

    def is_old_segment(self, seq, data, fin):
        """Tells whether every byte of the segment was received before.

        Slide 3-86, lost ACK scenario: the ACK was lost, the sender resends,
        and B ACKs the copy again. A FIN at ``expected_seq_num`` is still new.

        Args:
             seq   seq # of the first data byte
             data  the payload bytes
             fin   True when the FIN bit is set

        Returns:
             True when the segment brings nothing new
        """
        end = seq + len(data)
        return end < self.expected_seq_num or (end == self.expected_seq_num and not fin)

    def trim_to_window(self, seq, data, fin):
        """Cuts the bytes we already have and the bytes past the free space.

        Slide 3-94: the sender's limit to rwnd "guarantees receive buffer will not
        overflow"; we also cut anything a sender puts past rwnd, so the guarantee
        holds even if it does not. When the end is cut, the FIN goes too; the client
        resends it.

        Args:
             seq   seq # of the first data byte
             data  the payload bytes
             fin   True when the FIN bit is set

        Returns:
             (seq, data, fin) of the part that fits
        """
        if seq < self.expected_seq_num:
            data = data[self.expected_seq_num - seq:]
            seq = self.expected_seq_num
        right_edge = self.expected_seq_num + self.rwnd()
        if seq + len(data) > right_edge:
            self.log('OUT_OF_WINDOW', seq=seq, len=len(data), rwnd=self.rwnd())
            data = data[:max(0, right_edge - seq)]
            fin = False
        return seq, data, fin

    def store_out_of_order(self, seq, data):
        """Keeps a segment above a gap (slide 3-85, row 3).

        Slide 3-85: "immediately send duplicate ACK, indicating seq. # of next expected
        byte". Keeping it is "up to implementor" (slide 3-79): we keep the longer copy.
        WHY keep it: real implementations buffer such segments (book 3.5.4), so only
        the missing segment needs to be resent, not everything after it as in Go-Back-N.

        Args:
             seq   seq # of the first byte, above ``expected_seq_num``
             data  the payload bytes, already cut to the window
        """
        old = self.out_of_order.get(seq)
        if old is None or len(data) > len(old):
            self.out_of_order[seq] = data
        self.log('OUT_OF_ORDER', seq=seq, len=len(data), expected=self.expected_seq_num)

    def deliver_in_order(self, data):
        """Adds in-order bytes to the buffer and moves the ACK number on.

        Slide 3-79: the ACK is the "seq # of next byte expected from other side".

        Args:
             data  bytes that start exactly at ``expected_seq_num``
        """
        if not data:
            return
        self.buffered_data.extend(data)
        self.expected_seq_num += len(data)
        self.log('RECV', seq=self.expected_seq_num - len(data), len=len(data))

    def deliver_from_out_of_order(self):
        """Delivers the stored segments that now touch ``expected_seq_num``.

        Only new tails go in, so each byte is delivered once; ones fully below are dropped.
        """
        for seq in sorted(self.out_of_order):
            if seq > self.expected_seq_num:
                break
            data = self.out_of_order.pop(seq)
            if seq + len(data) > self.expected_seq_num:
                self.deliver_in_order(data[self.expected_seq_num - seq:])

    def choose_ack(self, gap_was_open, now):
        """Picks the ACK for an in-order segment (slide 3-85, rows 1, 2 and 4).

        Row 4: the segment fills a gap: "immediate send ACK, provided that
        segment starts at lower end of gap". Row 2: one other segment has an
        ACK pending: "immediately send single cumulative ACK". Row 1: delayed
        ACK, "Wait up to 500ms for next segment"; we wait DELAYED_ACK_WAIT.

        Args:
             gap_was_open  True when segments were stored above a gap
             now           the current time in seconds

        Returns:
             'gap_filled', 'cumulative', or None while the ACK waits
        """
        if gap_was_open:
            return 'gap_filled'
        if self.ack_pending:
            return 'cumulative'
        self.ack_pending = True
        self.ack_deadline = now + DELAYED_ACK_WAIT
        return None

    def on_tick(self, now):
        """Checks the delayed ACK (slide 3-85, row 1: "If no next segment, send ACK").

        Args:
             now  the current time in seconds

        Returns:
             'delayed' when the wait is over, else None
        """
        if self.ack_pending and now >= self.ack_deadline:
            return 'delayed'
        return None

    def read(self, max_bytes=None):
        """Gives in-order bytes to the application, which frees buffer space.

        Slide 3-90: "Application removing data from TCP socket buffers".

        Args:
             max_bytes  the most bytes to return; None returns all

        Returns:
             the bytes read (b'' when the buffer is empty)
        """
        if max_bytes is None or max_bytes > len(self.buffered_data):
            max_bytes = len(self.buffered_data)
        if max_bytes <= 0:
            return b''
        data = bytes(self.buffered_data[:max_bytes])
        del self.buffered_data[:max_bytes]
        self.log('APP_READ', bytes=len(data))
        return data

    def readable(self):
        """Tells whether ``read`` has bytes, or the client's FIN has come."""
        return len(self.buffered_data) > 0 or self.fin_received

    def rwnd(self):
        """Computes the window we advertise: RcvBuffer minus buffered data.

        Slide 3-94: the receiver "advertises" free buffer space in rwnd field.
        Out-of-order bytes lie inside the window already advertised, so they do not count.
        Book 3.5.5 writes it rwnd = RcvBuffer - [LastByteRcvd - LastByteRead]: the bytes
        the application has not read yet (buffered_data) take up space.
        """
        return self.rcv_buffer - len(self.buffered_data)

    def window_update_due(self):
        """Tells whether to send an ACK only to announce that space opened.

        extra: one ACK when the free space grows from below a quarter of RcvBuffer
        to at least a quarter. The window only travels in segments we send (slide
        3-94), so after a zero or small window nothing else would tell the sender.
        """
        quarter = self.rcv_buffer // 4
        return self.last_rwnd < quarter and quarter <= self.rwnd()

    def ack_sent(self, rwnd):
        """Notes that a segment with ACK = ``expected_seq_num`` went out.

        Args:
             rwnd  the window that segment advertised
        """
        self.ack_pending = False
        self.ack_deadline = None
        self.last_rwnd = rwnd

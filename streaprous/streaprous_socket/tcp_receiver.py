"""
TCP receiver for StreapRous.

Keeps the receiving side of one connection: in-order delivery with a
buffer for out-of-order segments, the ACK rules of slide 3-85 and the
receive window of slide 3-94. The class never touches sockets, threads
or the clock: the caller passes segments and the current time ``now``
and sends the ACKs the return values ask for.
"""


class TcpReceiver:
    """
    Receiving side of one TCP connection.

    Sequence numbers are plain ints (already unwrapped). ``buffered_data``
    holds in-order bytes the application has not read yet; ``out_of_order``
    holds segments that arrived above ``rcv_nxt``.

    Args:
        rcv_nxt: Sequence number of the first byte we expect.
        rcv_buffer: RcvBuffer in bytes (slide 3-94).
        delayed_ack: Delayed-ACK time in seconds (slide 3-85, row 1).
        on_event: Callback ``on_event(name, **details)`` for trace events.
    """

    def __init__(self, rcv_nxt, rcv_buffer, delayed_ack, on_event=None):
        """Sets up the receiver.

        Args:
            rcv_nxt: First sequence number expected from the peer.
            rcv_buffer: RcvBuffer in bytes (slide 3-94).
            delayed_ack: Delayed-ACK time in seconds (slide 3-85, row 1).
            on_event: Callback ``on_event(name, **details)`` for trace events.
        """
        self.rcv_nxt = rcv_nxt
        self.rcv_buffer = rcv_buffer
        # Book 3.5.4: an in-order segment may wait up to 500 ms for another
        # one before an ACK is sent. I use a shorter wait (config:
        # delayed_ack = 0.1 s) so transfers and demos are quicker. It is
        # well below the 500 ms limit of RFC 5681.
        self.delayed_ack = delayed_ack
        self.on_event = on_event or (lambda name, **details: None)
        self.buffered_data = bytearray()
        self.out_of_order = {}
        self.fin_seq = None
        self.fin_received = False
        self.ack_pending = False
        self.ack_deadline = None
        self.last_rwnd = rcv_buffer

    def on_segment(self, seq, payload, fin, now):
        """
        Handle one arriving segment and pick the ACK rule (slide 3-85).

        Row 1: in-order segment, nothing pending -> delay the ACK.
        Row 2: in-order segment while an ACK is pending -> ACK at once.
        Row 3: segment above the expected number (a gap) -> duplicate ACK.
        Row 4: segment that partly or fully fills a gap -> ACK at once.
        ``trim_to_window`` cuts data past the right edge of the window (slide 3-94).

        Args:
            seq: Sequence number of the first payload byte.
            payload: Segment data (bytes, may be empty).
            fin: True when the FIN flag is set.
            now: Current time in seconds.

        Returns:
            str or None: Why an ACK must go out now ('cumulative',
            'duplicate', 'gap_filled', 'fin', 'old_segment',
            'out_of_window', 'probe'), or None when no ACK is needed yet.
        """
        # ACK generation follows the table in book 3.5.4 (RFC 5681):
        # in-order segment, nothing pending -> wait (delayed ACK);
        # in-order segment, ACK pending -> send one cumulative ACK now;
        # segment above the expected number (a gap) -> send a duplicate ACK
        # at once; segment that fills a gap -> ACK at once.
        # The duplicate ACKs are exactly what the sender's fast retransmit
        # (book 3.5.4) and Reno (3.7.1) depend on.
        payload = bytes(payload)
        if not payload and not fin:
            # Pure ACK, or a zero-window probe / keepalive below rcv_nxt
            return None if seq == self.rcv_nxt else 'probe'

        end = seq + len(payload)
        new_fin = fin and not self.fin_received
        if end < self.rcv_nxt or (end == self.rcv_nxt and not new_fin):
            self.on_event('DUP_SEGMENT', seq=seq, len=len(payload))
            return 'old_segment'

        seq, payload, fin = self.trim_to_window(seq, payload, fin)
        if fin and self.fin_seq is None:
            self.fin_seq = seq + len(payload)
        if not payload and not fin:
            return 'out_of_window'

        # The book (3.5.4) notes that the RFCs do not say what a receiver
        # must do with out-of-order segments. Real implementations buffer
        # them, as I do. WHY: then only the missing segment needs to be
        # resent (not everything after it, as in Go-Back-N). The duplicate
        # ACK below goes out either way (slide 3-85, row 3).
        if seq > self.rcv_nxt:
            if payload:
                self._store_out_of_order(seq, payload)
            return 'duplicate'

        had_gap = bool(self.out_of_order)
        self._deliver_in_order(payload)
        self._pull_from_out_of_order()

        if self.fin_seq is not None and self.rcv_nxt == self.fin_seq:
            self.rcv_nxt += 1
            self.fin_received = True
            self.on_event('FIN_RECEIVED', seq=self.fin_seq)
            return 'fin'
        if had_gap:
            return 'gap_filled'
        if self.ack_pending:
            return 'cumulative'
        self.ack_pending = True
        self.ack_deadline = now + self.delayed_ack
        return None

    def trim_to_window(self, seq, payload, fin):
        """
        Cut the bytes we already have and the bytes past the free space.

        Slide 3-94: the sender's limit to rwnd "guarantees receive buffer will
        not overflow"; we also cut anything a sender puts past rwnd, so the
        guarantee holds even if it does not. When the end is cut, the FIN goes
        too; the client resends it.

        Args:
            seq: Sequence number of the first payload byte.
            payload: Segment data.
            fin: True when the FIN flag is set.

        Returns:
            tuple: (seq, payload, fin) of the part that fits.
        """
        if seq < self.rcv_nxt:
            payload = payload[self.rcv_nxt - seq:]
            seq = self.rcv_nxt
        right_edge = self.rcv_nxt + self.rwnd()
        if seq + len(payload) > right_edge:
            self.on_event('OUT_OF_WINDOW', seq=seq, len=len(payload), rwnd=self.rwnd())
            payload = payload[:max(0, right_edge - seq)]
            fin = False
        return seq, payload, fin

    def _deliver_in_order(self, payload):
        """
        Append in-order bytes to the buffered data (slide 3-80).

        Args:
            payload: Bytes that start exactly at ``rcv_nxt``.
        """
        if not payload:
            return
        self.on_event('RECV', seq=self.rcv_nxt, len=len(payload))
        self.buffered_data.extend(payload)
        self.rcv_nxt += len(payload)

    def _store_out_of_order(self, seq, payload):
        """
        Keep a segment that arrived above ``rcv_nxt`` (slide 3-85, row 3).

        When a segment with the same sequence number is already stored,
        the longer one is kept.

        Args:
            seq: Sequence number of the first byte, above ``rcv_nxt``.
            payload: Segment data, already cut to the window.
        """
        old = self.out_of_order.get(seq)
        if old is None or len(payload) > len(old):
            self.out_of_order[seq] = payload
        self.on_event('OUT_OF_ORDER', seq=seq, len=len(payload),
                      expected=self.rcv_nxt)

    def _pull_from_out_of_order(self):
        """
        Move stored segments that now touch ``rcv_nxt`` into the ready buffer.

        Segments fully below ``rcv_nxt`` are dropped; a segment that
        overlaps it gives only its new tail, so every byte is delivered once.
        """
        for seq in sorted(self.out_of_order):
            if seq > self.rcv_nxt:
                break
            data = self.out_of_order.pop(seq)
            if seq + len(data) > self.rcv_nxt:
                self._deliver_in_order(data[self.rcv_nxt - seq:])

    def on_tick(self, now):
        """
        Check the delayed-ACK timer (slide 3-85, row 1).

        Args:
            now: Current time in seconds.

        Returns:
            str or None: 'delayed' when a pending ACK's timer has expired,
            else None. The ACK stays pending until ``ack_sent`` is called.
        """
        if self.ack_pending and self.ack_deadline is not None and now >= self.ack_deadline:
            return 'delayed'
        return None

    def read(self, max_bytes=None):
        """
        Give in-order bytes to the application, which frees buffer space.

        Slide 3-90: "Application removing data from TCP socket buffers".

        Args:
            max_bytes: Most bytes to return; None returns all.

        Returns:
            bytes: The bytes read (b"" when the buffer is empty).
        """
        if max_bytes is None or max_bytes > len(self.buffered_data):
            max_bytes = len(self.buffered_data)
        if max_bytes <= 0:
            return b""
        data = bytes(self.buffered_data[:max_bytes])
        del self.buffered_data[:max_bytes]
        self.on_event('APP_READ', bytes=len(data))
        return data

    def readable(self):
        """
        Tell whether ``read`` has something to report.

        Returns:
            bool: True when in-order bytes are buffered or the FIN has arrived.
        """
        return len(self.buffered_data) > 0 or self.fin_received

    def rwnd(self):
        """
        Compute the window we advertise: RcvBuffer minus buffered data.

        Slide 3-94: the receiver "advertises" free buffer space in rwnd field.
        Out-of-order bytes lie inside the window already advertised, so they
        do not count. Book 3.5.5 writes it rwnd = RcvBuffer - [LastByteRcvd -
        LastByteRead]: the bytes the application has not read yet
        (buffered_data) take up space.

        Returns:
            int: Free buffer space in bytes.
        """
        return self.rcv_buffer - len(self.buffered_data)

    def window_update_due(self):
        """
        Tell whether to send an ACK only to announce that space opened.

        extra: one ACK when the free space grows from below a quarter of
        RcvBuffer to at least a quarter. The window only travels in segments
        we send (slide 3-94), so after a zero or small window nothing else
        would tell the sender.

        Returns:
            bool: True when an ACK with the new window should go out.
        """
        quarter = self.rcv_buffer // 4
        return self.last_rwnd < quarter and quarter <= self.rwnd()

    def ack_sent(self, rwnd):
        """
        Record that a segment carrying ack=rcv_nxt was sent.

        Args:
            rwnd: The window that segment advertised.
        """
        self.ack_pending = False
        self.ack_deadline = None
        self.last_rwnd = rwnd

    def next_deadline(self):
        """
        Give the time the delayed-ACK timer fires.

        Returns:
            float or None: The deadline, or None when no ACK is pending.
        """
        return self.ack_deadline if self.ack_pending else None

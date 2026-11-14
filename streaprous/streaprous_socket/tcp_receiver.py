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

    Sequence numbers are plain ints (already unwrapped). ``ready`` holds
    in-order bytes the application has not read yet; ``out_of_order``
    holds segments that arrived above ``rcv_nxt``.

    Args:
        rcv_nxt: Sequence number of the first byte we expect.
        buffer_size: RcvBuffer in bytes (slide 3-94).
        delayed_ack: Delayed-ACK time in seconds (slide 3-85, row 1).
        on_event: Callback ``on_event(name, **details)`` for trace events.
    """

    def __init__(self, rcv_nxt, buffer_size, delayed_ack, on_event=None):
        self.rcv_nxt = rcv_nxt
        self.buffer_size = buffer_size
        self.delayed_ack = delayed_ack
        self.on_event = on_event or (lambda name, **details: None)
        self.ready = bytearray()
        self.out_of_order = {}
        self.fin_seq = None
        self.fin_received = False
        self.last_byte_rcvd = 0
        self.last_byte_read = 0
        self.ack_pending = False
        self.ack_deadline = None
        self.last_advertised = buffer_size

    def on_segment(self, seq, payload, fin, now):
        """
        Handle one arriving segment and pick the ACK rule (slide 3-85).

        Row 1: in-order segment, nothing pending -> delay the ACK.
        Row 2: in-order segment while an ACK is pending -> ACK at once.
        Row 3: segment above the expected number (a gap) -> duplicate ACK.
        Row 4: segment that partly or fully fills a gap -> ACK at once.
        Data past the right edge of the window (slide 3-94) is cut off.

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
        payload = bytes(payload)
        if not payload and not fin:
            # Pure ACK, or a zero-window probe / keepalive below rcv_nxt
            return None if seq == self.rcv_nxt else 'probe'

        end = seq + len(payload)
        new_fin = fin and not self.fin_received
        if end < self.rcv_nxt or (end == self.rcv_nxt and not new_fin):
            self.on_event('DUP_SEGMENT', seq=seq, len=len(payload))
            return 'old_segment'

        if seq < self.rcv_nxt:
            payload = payload[self.rcv_nxt - seq:]
            seq = self.rcv_nxt

        right_edge = self.rcv_nxt + self.window()
        if seq + len(payload) > right_edge:
            self.on_event('OUT_OF_WINDOW', seq=seq, len=len(payload),
                          window=self.window())
            payload = payload[:max(0, right_edge - seq)]
            fin = False
        if fin and self.fin_seq is None:
            self.fin_seq = seq + len(payload)
        if not payload and not fin:
            return 'out_of_window'

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

    def _deliver_in_order(self, payload):
        """
        Append in-order bytes to the ready buffer (slide 3-80).

        Args:
            payload: Bytes that start exactly at ``rcv_nxt``.
        """
        if not payload:
            return
        self.on_event('RECV', seq=self.rcv_nxt, len=len(payload))
        self.ready.extend(payload)
        self.rcv_nxt += len(payload)
        self.last_byte_rcvd += len(payload)

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
        Hand in-order bytes to the application, which opens the window (slide 3-94).

        Args:
            max_bytes: Most bytes to return; None returns everything ready.

        Returns:
            bytes: The data read (b"" when nothing is ready).
        """
        if max_bytes is None or max_bytes > len(self.ready):
            max_bytes = len(self.ready)
        if max_bytes <= 0:
            return b""
        data = bytes(self.ready[:max_bytes])
        del self.ready[:max_bytes]
        self.last_byte_read += len(data)
        self.on_event('APP_READ', bytes=len(data))
        return data

    def readable(self):
        """
        Tell whether ``read`` has something to report.

        Returns:
            bool: True when in-order bytes are ready or the FIN has arrived.
        """
        return len(self.ready) > 0 or self.fin_received

    def window(self):
        """
        Compute the receive window to advertise (slide 3-94).

        rwnd = RcvBuffer - (LastByteRcvd - LastByteRead). Out-of-order
        bytes are not counted: they sit inside the advertised space.

        Returns:
            int: Free space in bytes, between 0 and ``buffer_size``.
        """
        free = self.buffer_size - (self.last_byte_rcvd - self.last_byte_read)
        return max(0, min(self.buffer_size, free))

    def window_update_due(self):
        """
        Tell whether a window update must be sent now (slide 3-94).

        True when the last advertised window was below a quarter of the
        buffer and the free space has grown to at least that quarter, so
        a sender stalled on a small window learns that it may go on.

        Returns:
            bool: True when an ACK with the new window should go out.
        """
        quarter = self.buffer_size // 4
        return self.last_advertised < quarter <= self.window()

    def ack_sent(self, window):
        """
        Record that a segment carrying ack=rcv_nxt was sent.

        Args:
            window: The window that segment advertised.
        """
        self.ack_pending = False
        self.ack_deadline = None
        self.last_advertised = window

    def next_deadline(self):
        """
        Give the time the delayed-ACK timer fires.

        Returns:
            float or None: The deadline, or None when no ACK is pending.
        """
        return self.ack_deadline if self.ack_pending else None

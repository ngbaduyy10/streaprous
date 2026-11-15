"""
TCP sender for StreapRous.

Keeps the sending side of one connection: the data is cut into segments of
at most one MSS, one retransmission timer covers the oldest unACKed byte
(slide 3-84), the third duplicate ACK triggers a fast retransmit (slide 3-88),
and at most min(cwnd, rwnd) bytes are in flight (slides 3-122, 3-94). cwnd
comes from the Reno machine of slide 3-125, rwnd from the receiver's ACKs.
When the receiver's window is 0, a one-byte probe asks again until it opens.

The class never touches sockets, threads or the clock: the caller passes ACK
numbers and the current time ``now`` and sends the segments the methods
return. Sequence numbers are plain ints (already unwrapped).
"""

from collections import namedtuple

# One segment to put on the wire. kind is 'new' (first transmission),
# 'retransmit' (resent after going back), 'timeout', 'fast', 'probe' or 'fin'.
SendSegment = namedtuple('SendSegment', ['seq', 'payload', 'fin', 'kind'])


class TcpSender:
    """
    Sending side of one TCP connection.

    ``snd_una`` is the oldest unACKed byte, ``snd_nxt`` the next byte to
    send and ``snd_max`` one past the highest byte ever sent. After a
    timeout ``snd_nxt`` goes back to ``snd_una`` and the bytes up to
    ``snd_max`` are sent again as the window allows.

    Args:
        iss: Sequence number of our first data byte.
        mss: Largest payload of one segment in bytes.
        rwnd: First receive window of the peer in bytes.
        rtt: RttEstimator that gives the timeout (slides 3-82 to 3-84).
        cc: RenoCongestion that gives cwnd (slide 3-125).
        max_retries: Timeouts in a row before we give up.
        persist_min: First zero-window probe interval in seconds.
        persist_max: Largest zero-window probe interval in seconds.
        on_event: Callback ``on_event(name, **details)`` for trace events.
    """

    def __init__(self, iss, mss, rwnd, rtt, cc, max_retries, persist_min, persist_max,
                 on_event=None):
        """
        Start with nothing to send, no timer and no probe.

        Args:
            iss: Sequence number of our first data byte.
            mss: Largest payload of one segment in bytes.
            rwnd: First receive window of the peer in bytes.
            rtt: RttEstimator that gives the timeout.
            cc: RenoCongestion that gives cwnd.
            max_retries: Timeouts in a row before we give up.
            persist_min: First zero-window probe interval in seconds.
            persist_max: Largest zero-window probe interval in seconds.
            on_event: Callback ``on_event(name, **details)``; None means no events.
        """
        self.mss = mss
        self.rtt = rtt
        self.cc = cc
        self.max_retries = max_retries
        self.persist_min = persist_min
        self.persist_max = persist_max
        self.on_event = on_event or (lambda name, **details: None)

        self.snd_una = iss
        self.snd_nxt = iss
        self.snd_max = iss
        self.rwnd = rwnd
        self.buffer = b""
        self.buffer_seq = iss
        self.fin_requested = False
        self.fin_seq = None

        self.timer_deadline = None
        self.timeouts_in_row = 0
        self.timed_end = None
        self.timed_at = None
        self.persist_deadline = None
        self.persist_interval = persist_min
        self.probe_seq = None
        self.gave_up = False

        self.segments_sent = 0
        self.retransmits = 0
        self.timeouts = 0
        self.fast_retransmits = 0
        self.probes = 0

    # ------------------------------------------------------------------
    # Calls from the connection
    # ------------------------------------------------------------------

    def write(self, data, now):
        """
        Take the next block of application data and send what the window allows.

        The previous block must be fully ACKed first, so the buffer only
        ever holds bytes from ``snd_una`` on.

        Args:
            data: Bytes to send.
            now: Current time in seconds.

        Returns:
            list: SendSegment values to transmit now.
        """
        if not self.data_acked():
            raise ValueError("write() before the previous data was ACKed")
        self.buffer = bytes(data)
        self.buffer_seq = self.snd_una
        return self._send_allowed(now)

    def close(self, now):
        """
        Ask for a FIN after the last data byte.

        The FIN takes one sequence number and goes out once every data byte
        has been sent; the same timer retransmits it until it is ACKed.

        Args:
            now: Current time in seconds.

        Returns:
            list: SendSegment values to transmit now.
        """
        if self.fin_requested:
            return []
        self.fin_requested = True
        self.fin_seq = self.data_end()
        return self._send_allowed(now)

    def on_ack(self, ack, window, has_data, now):
        """
        Handle the ACK number and window of one received segment.

        An ACK above ``snd_una`` is a new ACK. An ACK equal to ``snd_una``
        is a duplicate only when data is outstanding, the segment carries
        no data and the window did not change (RFC 5681, background); other
        such ACKs just update the window. ACKs below ``snd_una`` or above
        anything we sent are ignored.

        Args:
            ack: ACK number, already unwrapped near ``snd_una``.
            window: Window field of the segment in bytes.
            has_data: True when the segment carries payload.
            now: Current time in seconds.

        Returns:
            list: SendSegment values to transmit now.
        """
        if self.gave_up:
            return []
        limit = self.snd_max
        if self.probe_seq is not None:
            limit = max(limit, self.probe_seq + 1)
        if ack < self.snd_una or ack > limit:
            return []
        if ack > self.snd_una:
            self._on_new_ack(ack, window, now)
            return self._send_allowed(now)
        duplicate = (self.snd_nxt > self.snd_una and not has_data
                     and window == self.rwnd)
        self._update_rwnd(window)
        if duplicate:
            return self._on_duplicate_ack(now)
        return self._send_allowed(now)

    def on_tick(self, now):
        """
        Run the retransmission timer and the persist timer.

        Args:
            now: Current time in seconds.

        Returns:
            list: SendSegment values to transmit now.
        """
        if self.gave_up:
            return []
        out = []
        if self.timer_deadline is not None and now >= self.timer_deadline:
            out.extend(self._on_timeout(now))
        if self.persist_deadline is not None and now >= self.persist_deadline:
            out.extend(self._send_window_probe(now))
        return out

    def bytes_in_flight(self):
        """
        Count bytes sent but not ACKed yet.

        Returns:
            int: ``snd_nxt - snd_una`` (the FIN counts as one byte).
        """
        return self.snd_nxt - self.snd_una

    def data_end(self):
        """
        Give the sequence number just after the last data byte.

        Returns:
            int: ``buffer_seq + len(buffer)``.
        """
        return self.buffer_seq + len(self.buffer)

    def data_acked(self):
        """
        Tell whether every data byte written so far is ACKed.

        Returns:
            bool: True when ``snd_una`` reached the end of the data.
        """
        return self.snd_una >= self.data_end()

    def fin_acked(self):
        """
        Tell whether our FIN is ACKed.

        Returns:
            bool: True when a FIN was asked for and ``snd_una`` is past it.
        """
        return self.fin_requested and self.snd_una > self.fin_seq

    def next_deadline(self):
        """
        Give the time the caller must call ``on_tick`` next.

        Returns:
            float or None: The earlier of the retransmission and persist
            deadlines, or None when neither timer runs.
        """
        deadlines = [d for d in (self.timer_deadline, self.persist_deadline)
                     if d is not None]
        return min(deadlines) if deadlines else None

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    def _usable_window(self):
        """
        Give how many more bytes may go out now (slides 3-122, 3-94).

        In flight may be at most cwnd (congestion control, slide 3-122) and
        at most rwnd (flow control, slide 3-94).

        Returns:
            int: ``min(cwnd, rwnd) - bytes_in_flight()``; may be negative
            when a window shrank.
        """
        return min(self.cc.cwnd, self.rwnd) - self.bytes_in_flight()

    def _send_allowed(self, now):
        """
        Transmit new segment(s), as allowed (slide 3-122).

        Sends from ``snd_nxt`` while the usable window lets a segment out.
        A segment shorter than one MSS waits while data is in flight,
        unless it is the last piece of data: ACKs will open room for a
        full one. With nothing in flight a small segment goes out, so a
        window below one MSS cannot stall us. Bytes below ``snd_max`` are
        resent after a timeout ('retransmit'). After the data, the FIN goes
        out without needing window space.

        Args:
            now: Current time in seconds.

        Returns:
            list: SendSegment values to transmit now.
        """
        out = []
        while True:
            if self.snd_nxt < self.data_end():
                remaining = self.data_end() - self.snd_nxt
                length = min(self.mss, remaining, self._usable_window())
                if length <= 0:
                    break
                if length < self.mss and length < remaining and self.bytes_in_flight() > 0:
                    break
                seq = self.snd_nxt
                resend = seq < self.snd_max
                if resend:
                    self._cancel_rtt_timing()
                elif self.timed_end is None:
                    self.timed_end = seq + length
                    self.timed_at = now
                out.append(self._emit(seq, self._payload(seq, length), False,
                                      'retransmit' if resend else 'new', resend))
                self.snd_nxt += length
                self.snd_max = max(self.snd_max, self.snd_nxt)
                if self.timer_deadline is None:
                    self._start_timer(now)
                self.on_event('RETRANSMIT' if resend else 'SEND', seq=seq, len=length)
            else:
                if self.fin_requested and self.snd_nxt == self.fin_seq:
                    seq = self.snd_nxt
                    out.append(self._emit(seq, b"", True, 'fin', seq < self.snd_max))
                    self.snd_nxt += 1
                    self.snd_max = max(self.snd_max, self.snd_nxt)
                    if self.timer_deadline is None:
                        self._start_timer(now)
                    self.on_event('FIN_SENT', seq=seq)
                break
        self._check_persist(now)
        return out

    def _payload(self, seq, length):
        """
        Cut ``length`` data bytes starting at ``seq`` from the buffer.

        Args:
            seq: Sequence number of the first byte.
            length: Number of bytes.

        Returns:
            bytes: The payload.
        """
        start = seq - self.buffer_seq
        return self.buffer[start:start + length]

    def _segment_at_una(self):
        """
        Build the payload and FIN flag of the oldest unACKed segment.

        Returns:
            tuple: (payload, fin) for the segment that starts at ``snd_una``:
            up to one MSS of data, or the FIN when only the FIN is left.
        """
        if self.snd_una < self.data_end():
            length = min(self.mss, self.data_end() - self.snd_una)
            return self._payload(self.snd_una, length), False
        return b"", True

    def _emit(self, seq, payload, fin, kind, resend):
        """
        Make one SendSegment and count it.

        Args:
            seq: Sequence number of the segment.
            payload: Data bytes (b"" for a FIN).
            fin: True for the FIN segment.
            kind: 'new', 'retransmit', 'timeout', 'fast', 'probe' or 'fin'.
            resend: True when these sequence numbers were sent before.

        Returns:
            SendSegment: The segment.
        """
        self.segments_sent += 1
        if resend:
            self.retransmits += 1
        return SendSegment(seq, payload, fin, kind)

    # ------------------------------------------------------------------
    # Timer and RTT
    # ------------------------------------------------------------------

    def _start_timer(self, now):
        """
        Start the one retransmission timer (slide 3-84).

        The timer belongs to the oldest unACKed segment and expires one
        TimeoutInterval from now.

        Args:
            now: Current time in seconds.
        """
        self.timer_deadline = now + self.rtt.rto

    def _stop_timer(self):
        """
        Stop the retransmission timer (slide 3-84): nothing is outstanding.
        """
        self.timer_deadline = None

    def _cancel_rtt_timing(self):
        """
        Forget the segment being timed (slide 3-81: ignore retransmissions).
        """
        self.timed_end = None
        self.timed_at = None

    def _take_rtt_sample(self, ack, now):
        """
        Measure SampleRTT when the timed segment is ACKed (slide 3-81).

        Only one segment is timed at a time, and only a first transmission:
        an ACK after a retransmission cannot tell which copy it answers, so
        every retransmission cancels the timing.

        Args:
            ack: The new ACK number.
            now: Current time in seconds.
        """
        if self.timed_end is None or ack < self.timed_end:
            return
        sample = now - self.timed_at
        self.rtt.add_sample(sample)
        self._cancel_rtt_timing()
        self.on_event('RTT', sample=sample, estimated=self.rtt.estimated_rtt,
                      dev=self.rtt.dev_rtt, rto=self.rtt.rto)

    def _on_timeout(self, now):
        """
        Handle an expired retransmission timer (slides 3-84, 3-86).

        The oldest unACKed segment is resent at once, whatever the window,
        the timeout doubles (slide 3-84) and Reno drops cwnd to one MSS
        (slide 3-124). ``snd_nxt`` goes back to ``snd_una`` (BSD Reno,
        background), so the rest of the window is resent as ACKs open it and
        several losses in one window cost one timeout. After more than
        ``max_retries`` timeouts in a row we give up. The resent segment is
        a full one when data allows, so it may reach past ``snd_max``; then
        ``snd_max`` moves with it, so an ACK for all of it is accepted.

        Args:
            now: Current time in seconds.

        Returns:
            list: The retransmitted segment, or [] when nothing was outstanding
            or we gave up.
        """
        if self.snd_una == self.snd_nxt:
            self._stop_timer()
            return []
        self.timeouts_in_row += 1
        if self.timeouts_in_row > self.max_retries:
            self.gave_up = True
            self._stop_timer()
            self.persist_deadline = None
            self.on_event('GIVE_UP', reason='timeouts')
            return []
        self.timeouts += 1
        self.rtt.back_off()
        self.cc.on_timeout()
        self._cancel_rtt_timing()
        self.snd_nxt = self.snd_una
        payload, fin = self._segment_at_una()
        seq = self.snd_una
        segment = self._emit(seq, payload, fin, 'timeout', True)
        self.snd_nxt = seq + (1 if fin else len(payload))
        self.snd_max = max(self.snd_max, self.snd_nxt)  # may be longer than the first copy
        self._start_timer(now)
        self.on_event('TIMEOUT', seq=seq, len=len(payload), rto=self.rtt.rto)
        return [segment]

    # ------------------------------------------------------------------
    # ACKs
    # ------------------------------------------------------------------

    def _on_new_ack(self, ack, window, now):
        """
        Handle an ACK that acknowledges new bytes (slides 3-84, 3-87).

        ACKs are cumulative (slide 3-87): every byte below ``ack`` arrived,
        even bytes we were about to resend after a timeout. The timer is
        restarted while bytes are still outstanding and stopped otherwise
        (slide 3-84).

        Args:
            ack: The ACK number, above ``snd_una``.
            window: Window field of the segment in bytes.
            now: Current time in seconds.
        """
        acked = ack - self.snd_una
        self.snd_una = ack
        self.snd_nxt = max(self.snd_nxt, ack)
        self.snd_max = max(self.snd_max, self.snd_nxt)  # an ACKed probe byte
        self.probe_seq = None
        self._update_rwnd(window)
        self._take_rtt_sample(ack, now)
        self.timeouts_in_row = 0
        self.cc.on_new_ack()
        if self.snd_una < self.snd_nxt:
            self._start_timer(now)
        else:
            self._stop_timer()
        self.on_event('ACK', ack=ack, acked=acked)

    def _on_duplicate_ack(self, now):
        """
        Handle a duplicate ACK (slide 3-88).

        Reno counts it; on the third one it asks for a fast retransmit of
        the segment at ``snd_una``. In fast recovery cwnd grows with each
        duplicate ACK, so new data may go out afterwards.

        Args:
            now: Current time in seconds.

        Returns:
            list: SendSegment values to transmit now.
        """
        retransmit_now = self.cc.on_duplicate_ack()
        self.on_event('DUPACK', ack=self.snd_una, count=self.cc.dup_ack_count)
        out = []
        if retransmit_now:
            out.extend(self._fast_retransmit(now))
        out.extend(self._send_allowed(now))
        return out

    def _fast_retransmit(self, now):
        """
        Resend the segment at ``snd_una`` without waiting for the timer (slide 3-88).

        Three duplicate ACKs mean later segments arrived, so only this one
        is probably lost. ``snd_nxt`` and the timer stay as they are;
        ``snd_max`` moves when the resent segment is longer than the first
        copy, so an ACK for all of it is accepted.

        Args:
            now: Current time in seconds.

        Returns:
            list: The retransmitted segment.
        """
        payload, fin = self._segment_at_una()
        seq = self.snd_una
        self.fast_retransmits += 1
        self._cancel_rtt_timing()
        segment = self._emit(seq, payload, fin, 'fast', True)
        self.snd_max = max(self.snd_max, seq + (1 if fin else len(payload)))
        self.on_event('FAST_RETRANSMIT', seq=seq, len=len(payload))
        return [segment]

    # ------------------------------------------------------------------
    # Flow control
    # ------------------------------------------------------------------

    def _update_rwnd(self, window):
        """
        Take the receiver's advertised window (slide 3-94).

        When the window opens after being 0, probing stops and the probe
        interval starts again from ``persist_min``.

        Args:
            window: Window field of the received segment in bytes.
        """
        old = self.rwnd
        self.rwnd = window
        if old > 0 and window == 0:
            self.on_event('RWND_ZERO', ack=self.snd_una)
        elif old == 0 and window > 0:
            self.persist_interval = self.persist_min
            self.persist_deadline = None
            self.on_event('RWND_OPEN', rwnd=window)

    def _probe_needed(self):
        """
        Tell whether the zero-window probe timer should run.

        Returns:
            bool: True when rwnd is 0, nothing is in flight and data waits.
        """
        return (self.rwnd == 0 and self.bytes_in_flight() == 0
                and self.snd_nxt < self.data_end())

    def _check_persist(self, now):
        """
        Arm or disarm the persist timer for zero-window probes.

        With rwnd 0 and nothing in flight no ACK will come to tell us the
        window opened, so a timer must ask. An armed timer keeps its deadline.

        Args:
            now: Current time in seconds.
        """
        if not self._probe_needed():
            self.persist_deadline = None
        elif self.persist_deadline is None:
            self.persist_deadline = now + self.persist_interval

    def _send_window_probe(self, now):
        """
        Send one byte at ``snd_nxt`` to ask for the window (slide 3-94).

        The receiver answers with an ACK that carries its current window, so
        a lost window update cannot stall us. The probe byte is not counted
        in flight. The interval doubles up to ``persist_max`` (the persist
        timer is background, not on the slides).

        Args:
            now: Current time in seconds.

        Returns:
            list: The probe segment, or [] when no probe is needed any more.
        """
        if not self._probe_needed():
            self.persist_deadline = None
            return []
        seq = self.snd_nxt
        interval = self.persist_interval
        self.probes += 1
        segment = self._emit(seq, self._payload(seq, 1), False, 'probe', False)
        self.probe_seq = seq
        self.on_event('PROBE', seq=seq, interval=interval)
        self.persist_interval = min(2 * self.persist_interval, self.persist_max)
        self.persist_deadline = now + self.persist_interval
        return [segment]

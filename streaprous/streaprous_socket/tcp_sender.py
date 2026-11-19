"""
StreapRous TCP sender
The sending side of one connection: segments with seq #, one timer, ACKs, fast retransmit,
at most min(cwnd, rwnd) bytes in flight, and zero-window probes.
The class never touches sockets or the clock: the caller passes ACK numbers (already
unwrapped) and the time now, and puts the returned segments on the wire.
"""

from collections import namedtuple

from .tcp_trace import no_log

# kind: 'new' (first transmission of data), 'fin' (first FIN), 'timeout', 'fast', 'probe'
SendSegment = namedtuple('SendSegment', ['seq', 'payload', 'fin', 'kind'])

PROBE_INTERVAL = 0.5                 # extra: one data byte every 0.5 s while rwnd is 0


class TcpSender:
    """The TCP sender of slides 3-84 and 3-146, limited by cwnd and rwnd.

    Attributes:
         send_base       oldest unACKed byte (SendBase); SendBase-1 is the last ACKed byte
         next_seq_num    seq # of the next new byte to send (NextSeqNum)
         rwnd            the window field of the latest acceptable ACK
         data            every byte the application gave us; byte i has seq #
                         initial_seq_num + i
         segment_end     seq # of each unACKed segment's first byte -> seq # just
                         after its last byte, so a resend repeats that segment
         fin_seq         seq # of our FIN, None until close
         timer_deadline  when the one timer expires, None when it is stopped
         rtt_timed_seq   the ACK number that ends the timed segment, None if none is timed
         rtt_sent_at     when the timed segment was sent
         probe_seq       seq # of the probe byte sent last, None if none waits
         probe_deadline  when the next probe goes out, None when no probe is needed
    """

    def __init__(self, initial_seq_num, mss, rwnd, reno, rtt, log=no_log):
        """Starts with nothing sent: NextSeqNum = SendBase = InitialSeqNum (slide 3-146).

        Args:
             initial_seq_num  seq # of our first data byte
             mss              largest payload of one segment in bytes
             rwnd             first window of the receiver in bytes
             reno             RenoCongestion that gives cwnd
             rtt              RttEstimator that gives TimeoutInterval
             log              called as log(event, **details) for the trace
        """
        self.mss = mss
        self.reno = reno
        self.rtt = rtt
        self.log = log
        self.initial_seq_num = initial_seq_num
        self.send_base = initial_seq_num
        self.next_seq_num = initial_seq_num
        self.rwnd = rwnd
        self.data = b""
        self.segment_end = {}
        self.fin_seq = None
        self.timer_deadline = None
        self.rtt_timed_seq = None
        self.rtt_sent_at = None
        self.probe_seq = None
        self.probe_deadline = None

    def send_data(self, data, now):
        """Event: data received from application (slide 3-84); the bytes join the stream.

        Args:
             data  bytes from the application
             now   current time in seconds

        Returns:
             list of SendSegment to put on the wire
        """
        self.data = self.data + bytes(data)
        return self.send_allowed(now)

    def close(self, now):
        """Asks for our FIN after the last data byte (slides 3-103, 3-104).

        slide 3-103: "send TCP segment with FIN bit = 1". The FIN takes one seq #
        (slide 3-104: "FINbit=1, seq=x" is answered with "ACKnum=x+1"), goes out
        after the data, and the timer resends it until it is ACKed.

        Args:
             now  current time in seconds

        Returns:
             list of SendSegment to put on the wire
        """
        if self.fin_seq is None:
            self.fin_seq = self.data_end()
        return self.send_allowed(now)

    def on_ack(self, ack, window, carries_data, now):
        """Event: ACK received, with ACK field value ack (slides 3-84, 3-146).

        An ACK above SendBase is a new ACK. An ACK below SendBase is old: slide 3-146
        acts only when y > SendBase. An ACK may also cover our probe byte
        (probe_seq + 1), which NextSeqNum does not count.

        extra: an ACK for bytes we never sent is ignored; it cannot be a real ACK.
        extra: an ACK equal to SendBase is a duplicate ACK only while data is in
        flight and when it carries no data and the same window; anything else
        only updates rwnd.

        Args:
             ack           ACK number, already unwrapped
             window        window field of the segment in bytes
             carries_data  True when the segment has payload or FIN
             now           current time in seconds

        Returns:
             list of SendSegment to put on the wire
        """
        highest = self.next_seq_num
        if self.probe_seq is not None:
            highest = max(self.next_seq_num, self.probe_seq + 1)
        if ack < self.send_base or ack > highest:
            return []
        if ack > self.send_base:
            self.on_new_ack(ack, now)
            self.update_rwnd(window)
            return self.send_allowed(now)
        # An ACK that only changes rwnd is flow control, not a loss (book 3.5.5).
        duplicate = self.in_flight() > 0 and not carries_data and window == self.rwnd
        self.update_rwnd(window)
        if duplicate:
            return self.on_duplicate_ack(now)
        return self.send_allowed(now)

    def on_tick(self, now):
        """Runs the timer and the probe timer.

        Args:
             now  current time in seconds

        Returns:
             list of SendSegment to put on the wire
        """
        segments = []
        if self.timer_deadline is not None and now >= self.timer_deadline:
            segments = segments + self.on_timeout(now)
        if self.probe_deadline is not None and now >= self.probe_deadline:
            segments = segments + self.send_probe(now)
        return segments

    def in_flight(self):
        """Counts bytes sent but not ACKed: LastByteSent - LastByteAcked (slide 3-122).

        LastByteSent = NextSeqNum - 1 and LastByteAcked = SendBase - 1, so this is
        NextSeqNum - SendBase. The FIN counts as one byte; a probe byte never counts.
        """
        return self.next_seq_num - self.send_base

    def data_end(self):
        """Gives the seq # just after the last data byte."""
        return self.initial_seq_num + len(self.data)

    def data_acked(self):
        """Tells whether every data byte is ACKed."""
        return self.send_base >= self.data_end()

    def fin_acked(self):
        """Tells whether our FIN is ACKed."""
        return self.fin_seq is not None and self.send_base > self.fin_seq

    def payload(self, seq, length):
        """Cuts length data bytes starting at seq # seq.

        Args:
             seq     seq # of the first byte
             length  number of bytes
        """
        start = seq - self.initial_seq_num
        return self.data[start:start + length]

    def send_allowed(self, now):
        """Transmit new segment(s), as allowed (slide 3-125).

        Congestion control (slide 3-122): LastByteSent - LastByteAcked <= cwnd.
        Flow control (slide 3-94): sender limits amount of unACKed ("in-flight")
        data to received rwnd. So each new segment is at most one MSS and at most
        min(cwnd, rwnd) - in_flight() bytes. The FIN needs no window space.

        Args:
             now  current time in seconds

        Returns:
             list of SendSegment to put on the wire
        """
        segments = []
        while self.next_seq_num < self.data_end():
            # cwnd is the network, rwnd the receiver. Zero or less means wait (book 3.7.1).
            room = min(self.reno.cwnd, self.rwnd) - self.in_flight()
            length = min(self.mss, self.data_end() - self.next_seq_num, room)
            if length <= 0:
                break
            segments.append(self.new_segment(length, now))
        if self.fin_seq is not None and self.next_seq_num == self.fin_seq:
            segments.append(self.new_fin(now))
        self.check_probe_timer(now)
        return segments

    def new_segment(self, length, now):
        """Creates segment with seq # NextSeqNum (slides 3-84, 3-146).

        slide 3-84: seq # is byte-stream number of first data byte in segment, and
        start timer if not already running.
        extra: one segment is timed at a time; it gives the next SampleRTT.

        Args:
             length  payload length in bytes
             now     current time in seconds

        Returns:
             the new SendSegment
        """
        seq = self.next_seq_num
        if self.rtt_timed_seq is None:
            self.rtt_timed_seq = seq + length
            self.rtt_sent_at = now
        self.next_seq_num = self.next_seq_num + length
        self.segment_end[seq] = seq + length
        if self.timer_deadline is None:
            self.start_timer(now)
        self.log('SEND', seq=seq, len=length)
        return SendSegment(seq, self.payload(seq, length), False, 'new')

    def new_fin(self, now):
        """Creates our FIN segment; it takes one seq # (slide 3-104).

        Args:
             now  current time in seconds

        Returns:
             the FIN SendSegment
        """
        segment = SendSegment(self.fin_seq, b"", True, 'fin')
        self.next_seq_num = self.fin_seq + 1
        if self.timer_deadline is None:
            self.start_timer(now)
        self.log('FIN_SENT', seq=self.fin_seq)
        return segment

    def on_new_ack(self, ack, now):
        """ACK acknowledges previously unACKed segments (slides 3-84, 3-87, 3-146).

        Update what is known to be ACKed: SendBase = ack. The ACK is cumulative
        (slide 3-87), so it covers an earlier lost ACK. Segments the ACK covers leave
        segment_end; when it ends inside a segment, the rest stays under the key ack.
        Then start timer if there are still unACKed segments, else stop it.

        Args:
             ack  ACK number, above SendBase
             now  current time in seconds
        """
        for seq in list(self.segment_end):
            if seq < ack:
                end = self.segment_end.pop(seq)
                if end > ack:
                    self.segment_end[ack] = end
        self.send_base = ack
        self.next_seq_num = max(self.next_seq_num, ack)  # may cover our probe byte (probe_seq + 1)
        self.probe_seq = None
        self.take_rtt_sample(ack, now)
        self.reno.on_new_ack()
        if self.in_flight() > 0:
            self.start_timer(now)
        else:
            self.stop_timer()
        self.log('ACK', ack=ack)

    def on_duplicate_ack(self, now):
        """Counts a duplicate ACK (slides 3-88, 3-125).

        If sender receives 3 additional ACKs for same data, it fast retransmits.
        In fast recovery each duplicate ACK grows cwnd, so new data may follow.

        Args:
             now  current time in seconds

        Returns:
             list of SendSegment to put on the wire
        """
        # True only on the third duplicate ACK: that is the fast retransmit (book 3.5.4).
        third = self.reno.on_duplicate_ack()
        self.log('DUPACK', ack=self.send_base, count=self.reno.dup_ack_count)
        segments = []
        if third:
            segments = self.fast_retransmit()
        return segments + self.send_allowed(now)

    def fast_retransmit(self):
        """Resend unACKed segment with smallest seq # (slide 3-88).

        Three duplicate ACKs mean later segments arrived, so this one is likely
        lost: do not wait for the timeout. The timer stays as it is.

        Returns:
             list with the resent SendSegment
        """
        segment = self.resend_oldest('fast')
        self.log('FAST_RETRANSMIT', seq=segment.seq, len=len(segment.payload))
        return [segment]

    def on_timeout(self, now):
        """Event: timeout (slides 3-84, 3-125).

        Retransmit segment that caused timeout, the oldest unACKed one, and restart
        timer with the same TimeoutInterval. Reno goes back to slow start. Only this
        one segment is resent: NextSeqNum does not move back.

        Args:
             now  current time in seconds

        Returns:
             list with the resent SendSegment, or [] when nothing is in flight
        """
        if self.in_flight() == 0:
            self.stop_timer()
            return []
        # on_timeout drops cwnd to 1 MSS (slide 3-125). No doubling: 3-84 only restarts the timer.
        self.reno.on_timeout()
        segment = self.resend_oldest('timeout')
        self.start_timer(now)
        self.log('TIMEOUT', seq=segment.seq, len=len(segment.payload),
                 interval=self.rtt.timeout_interval)
        return [segment]

    def resend_oldest(self, kind):
        """Builds the unACKed segment with the smallest seq # again (slides 3-84, 3-86, 3-88).

        slide 3-84: retransmit segment that caused timeout, as it was first sent
        (slide 3-86: "Seq=92, 8 bytes of data"); it ends at segment_end, else at
        NextSeqNum. It cancels the RTT timing (slide 3-81: ignore retransmissions).
        With only the FIN unACKed, it is the FIN.

        Args:
             kind  'timeout' or 'fast'

        Returns:
             the SendSegment starting at SendBase
        """
        # Karn's rule (book 3.5.3): an ACK can't tell the original from the copy.
        self.rtt_timed_seq = None
        if self.fin_seq is not None and self.send_base == self.fin_seq:
            return SendSegment(self.send_base, b"", True, kind)
        end = self.segment_end.get(self.send_base, min(self.next_seq_num, self.data_end()))
        length = min(self.mss, end - self.send_base)
        return SendSegment(self.send_base, self.payload(self.send_base, length), False, kind)

    def start_timer(self, now):
        """Starts the one timer: think of timer as for oldest unACKed segment (slide 3-84).

        It expires one TimeoutInterval from now.

        Args:
             now  current time in seconds
        """
        self.timer_deadline = now + self.rtt.timeout_interval

    def stop_timer(self):
        """Stops the timer: nothing is unACKed (slide 3-146)."""
        self.timer_deadline = None

    def take_rtt_sample(self, ack, now):
        """Measures SampleRTT when the timed segment is ACKed (slide 3-81).

        SampleRTT: measured time from segment transmission until ACK receipt;
        ignore retransmissions (resend_oldest cancels the timing).

        Args:
             ack  the new ACK number
             now  current time in seconds
        """
        if self.rtt_timed_seq is None or ack < self.rtt_timed_seq:
            return
        sample_rtt = now - self.rtt_sent_at
        self.rtt.add_sample(sample_rtt)
        self.rtt_timed_seq = None
        self.log('RTT', sample=sample_rtt, estimated=self.rtt.estimated_rtt,
                 dev=self.rtt.dev_rtt, timeout=self.rtt.timeout_interval)

    def update_rwnd(self, window):
        """Sets rwnd to received rwnd (slide 3-94).

        Args:
             window  window field of the received segment in bytes
        """
        old = self.rwnd
        self.rwnd = window
        if old > 0 and window == 0:
            self.log('RWND_ZERO', ack=self.send_base)
        elif old == 0 and window > 0:
            self.log('RWND_OPEN', rwnd=window)

    def check_probe_timer(self, now):
        """Arms or stops the probe timer.

        extra: a probe is needed while rwnd is 0, nothing is in flight and data
        waits. Then no ACK will come to tell us the window opened.

        Args:
             now  current time in seconds
        """
        needed = (self.rwnd == 0 and self.in_flight() == 0
                  and self.next_seq_num < self.data_end())
        if not needed:
            self.probe_deadline = None
        elif self.probe_deadline is None:
            self.probe_deadline = now + PROBE_INTERVAL

    def send_probe(self, now):
        """Sends one byte at NextSeqNum to ask for the window.

        extra: with rwnd 0, one data byte every 0.5 s. If the receiver's window
        update is lost, its answer to a probe still brings the new rwnd. The probe
        byte is neither timed nor counted in flight.

        Args:
             now  current time in seconds

        Returns:
             list with the probe SendSegment, or [] when no probe is needed
        """
        self.probe_deadline = None
        self.check_probe_timer(now)    # re-arms the next probe; None means none is needed
        if self.probe_deadline is None:
            return []
        self.probe_seq = self.next_seq_num
        self.log('PROBE', seq=self.probe_seq)
        return [SendSegment(self.next_seq_num, self.payload(self.next_seq_num, 1), False,
                            'probe')]

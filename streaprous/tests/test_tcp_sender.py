"""
Tests for the TCP sender: segments, timer, fast retransmit, windows and probes.
"""

import random
import unittest
from collections import deque

from streaprous_socket.tcp_reno import FAST_RECOVERY, RenoCongestion
from streaprous_socket.tcp_rtt import RttEstimator
from streaprous_socket.tcp_sender import SendSegment, TcpSender


ISS = 1000
MSS = 100
BIG = 1000000


def make(rwnd=BIG, cwnd=None, ssthresh=BIG, max_retries=8):
    """
    Build a sender with its own RTT estimator and Reno machine.

    Every event is recorded together with the sender's numbers at that moment.

    Args:
        rwnd: First receive window of the peer.
        cwnd: Starting cwnd; None keeps Reno's one MSS.
        ssthresh: First ssthresh.
        max_retries: Timeouts in a row before giving up.

    Returns:
        tuple: (sender, list of (name, details, in_flight, cwnd, rwnd) events).
    """
    events = []
    holder = []

    def on_event(name, **details):
        """Record one event with the sender's numbers."""
        tx = holder[0]
        events.append((name, details, tx.bytes_in_flight(), tx.cc.cwnd, tx.rwnd))

    rtt = RttEstimator(1.0, 0.2, 4.0)
    cc = RenoCongestion(MSS, ssthresh, on_event)
    if cwnd is not None:
        cc.cwnd = cwnd
    tx = TcpSender(ISS, MSS, rwnd, rtt, cc, max_retries, on_event)
    holder.append(tx)
    return tx, events


def names(events):
    """
    List the event names.

    Args:
        events: Events recorded by ``make``.

    Returns:
        list: The names in order.
    """
    return [event[0] for event in events]


def data_of(size):
    """
    Make deterministic test data.

    Args:
        size: Number of bytes.

    Returns:
        bytes: ``size`` bytes.
    """
    return bytes(i % 251 for i in range(size))


class HandReceiver:
    """
    A tiny receiver played by hand: keeps out-of-order segments and gives
    the cumulative ACK number.

    Args:
        start: First expected sequence number.
    """

    def __init__(self, start=ISS):
        """
        Expect the byte ``start`` first.

        Args:
            start: First expected sequence number.
        """
        self.rcv_nxt = start
        self.data = bytearray()
        self.held = {}
        self.fin = False

    def take(self, segment):
        """
        Accept one segment and return the ACK number.

        Args:
            segment: A SendSegment from the sender.

        Returns:
            int: The next byte expected.
        """
        self.held[segment.seq] = segment
        moved = True
        while moved:
            moved = False
            for seq in sorted(self.held):
                seg = self.held[seq]
                end = seq + len(seg.payload)
                if seq <= self.rcv_nxt < end:
                    self.data.extend(seg.payload[self.rcv_nxt - seq:])
                    self.rcv_nxt = end
                    moved = True
                if seg.fin and end == self.rcv_nxt and not self.fin:
                    self.rcv_nxt += 1
                    self.fin = True
                    moved = True
                if end < self.rcv_nxt or (end == self.rcv_nxt and not seg.fin):
                    del self.held[seq]
        return self.rcv_nxt


def run_clean(tx, data, close=False, window=BIG):
    """
    Send ``data`` over a loss-free path, ACKing every segment at once.

    Args:
        tx: The sender.
        data: Bytes to send.
        close: Also send the FIN after the data.
        window: Window the receiver advertises.

    Returns:
        tuple: (receiver, list of every segment sent).
    """
    rx = HandReceiver()
    now = 0.0
    queue = deque(tx.write(data, now))
    if close:
        queue.extend(tx.close(now))
    sent = list(queue)
    while queue:
        now += 0.01
        ack = rx.take(queue.popleft())
        out = tx.on_ack(ack, window, False, now)
        sent.extend(out)
        queue.extend(out)
    return rx, sent


class SegmentationTest(unittest.TestCase):
    """Segments never exceed one MSS and cover the data exactly."""

    def test_segments_cover_data(self):
        """Every byte goes out once, in segments of at most one MSS."""
        tx, events = make()
        data = data_of(1234)
        rx, sent = run_clean(tx, data)
        self.assertTrue(all(len(s.payload) <= MSS for s in sent))
        self.assertTrue(all(s.kind == 'new' for s in sent))
        expected = ISS
        for seg in sent:
            self.assertEqual(seg.seq, expected)
            self.assertEqual(seg.payload, data[seg.seq - ISS:seg.seq - ISS + len(seg.payload)])
            expected += len(seg.payload)
        self.assertEqual(expected, ISS + len(data))
        self.assertEqual(bytes(rx.data), data)
        self.assertTrue(tx.data_acked())
        self.assertEqual(tx.segments_sent, 13)
        self.assertEqual(tx.retransmits, 0)

    def test_write_before_ack_is_refused(self):
        """A new block may only be written after the last one is ACKed."""
        tx, _ = make()
        tx.write(data_of(50), 0.0)
        with self.assertRaises(ValueError):
            tx.write(data_of(50), 0.0)

    def test_old_and_impossible_acks_ignored(self):
        """ACKs below snd_una or above anything sent change nothing."""
        tx, events = make(cwnd=2 * MSS)
        tx.write(data_of(400), 0.0)
        tx.on_ack(ISS + 100, BIG, False, 0.1)
        before = (tx.snd_una, tx.snd_nxt, len(events))
        self.assertEqual(tx.on_ack(ISS + 50, BIG, False, 0.2), [])
        self.assertEqual(tx.on_ack(tx.snd_max + 1, BIG, False, 0.2), [])
        self.assertEqual((tx.snd_una, tx.snd_nxt, len(events)), before)


class TimerTest(unittest.TestCase):
    """One retransmission timer for the oldest unACKed byte (slide 3-84)."""

    def test_timer_start_restart_stop(self):
        """Started by the first segment, restarted by a new ACK, stopped when all is ACKed."""
        tx, _ = make()
        self.assertIsNone(tx.timer_deadline)
        tx.write(data_of(600), 0.0)
        self.assertEqual(tx.timer_deadline, 1.0)
        # ACK the first segment: two more go out, one timer from now
        out = tx.on_ack(ISS + 100, BIG, False, 0.1)
        self.assertEqual(len(out), 2)
        first = tx.timer_deadline
        self.assertAlmostEqual(first, 0.1 + tx.rtt.rto)
        # partial ACK restarts it; the segments sent with it do not move it
        out = tx.on_ack(ISS + 200, BIG, False, 0.15)
        self.assertEqual(len(out), 2)
        self.assertAlmostEqual(tx.timer_deadline, 0.15 + tx.rtt.rto)
        self.assertNotEqual(tx.timer_deadline, first)
        tx.on_ack(tx.snd_nxt, BIG, False, 0.2)
        self.assertIsNotNone(tx.timer_deadline)
        while not tx.data_acked():
            tx.on_ack(tx.snd_nxt, BIG, False, 0.3)
        self.assertIsNone(tx.timer_deadline)
        self.assertIsNone(tx.next_deadline())

    def test_tick_before_deadline_does_nothing(self):
        """The timer fires only when its deadline has passed."""
        tx, _ = make()
        tx.write(data_of(100), 0.0)
        self.assertEqual(tx.on_tick(0.99), [])
        self.assertEqual(tx.timeouts, 0)


class TimeoutTest(unittest.TestCase):
    """Timeout: resend the oldest segment, back off, cwnd = 1 MSS, go back."""

    def test_timeout_retransmits_oldest(self):
        """The segment at snd_una goes out again and the sender goes back."""
        tx, events = make(cwnd=4 * MSS)
        data = data_of(400)
        sent = tx.write(data, 0.0)
        self.assertEqual(len(sent), 4)
        out = tx.on_tick(1.0)
        self.assertEqual(out, [SendSegment(ISS, data[:100], False, 'timeout')])
        self.assertEqual(tx.rtt.rto, 2.0)
        self.assertEqual(tx.cc.cwnd, MSS)
        self.assertEqual(tx.cc.ssthresh, 200)
        self.assertEqual(tx.snd_nxt, tx.snd_una + 100)
        self.assertEqual(tx.snd_max, ISS + 400)
        self.assertEqual(tx.timer_deadline, 3.0)
        self.assertEqual((tx.timeouts, tx.retransmits), (1, 1))
        timeout = [e for e in events if e[0] == 'TIMEOUT'][0]
        self.assertEqual(timeout[1], {'seq': ISS, 'len': 100, 'rto': 2.0})

    def test_go_back_resends_rest_as_window_allows(self):
        """After the go-back, ACKs let the old bytes out again as 'retransmit'."""
        tx, events = make(cwnd=4 * MSS)
        data = data_of(400)
        tx.write(data, 0.0)
        tx.on_tick(1.0)
        out = tx.on_ack(ISS + 100, BIG, False, 1.1)
        self.assertEqual([(s.seq, s.kind) for s in out],
                         [(ISS + 100, 'retransmit'), (ISS + 200, 'retransmit')])
        self.assertEqual(names(events).count('RETRANSMIT'), 2)
        self.assertEqual(tx.retransmits, 3)

    def test_cumulative_ack_beyond_lost_segment(self):
        """Segments that did arrive are not resent once the gap is filled."""
        tx, _ = make(cwnd=4 * MSS)
        rx = HandReceiver()
        data = data_of(400)
        sent = tx.write(data, 0.0)
        for seg in sent[1:]:
            rx.take(seg)    # their duplicate ACKs are lost
        out = tx.on_tick(1.0)
        ack = rx.take(out[0])
        self.assertEqual(ack, ISS + 400)
        out = tx.on_ack(ack, BIG, False, 1.1)
        self.assertEqual(out, [])
        self.assertTrue(tx.data_acked())
        self.assertEqual(tx.snd_nxt, ISS + 400)
        self.assertEqual(tx.retransmits, 1)
        self.assertIsNone(tx.timer_deadline)
        self.assertEqual(bytes(rx.data), data)

    def test_give_up_after_max_retries(self):
        """max_retries timeouts in a row resend; the next one gives up."""
        tx, events = make(max_retries=8)
        tx.write(data_of(100), 0.0)
        rtos = []
        for _ in range(8):
            out = tx.on_tick(tx.next_deadline())
            self.assertEqual(len(out), 1)
            rtos.append(tx.rtt.rto)
        self.assertEqual(rtos, [2.0, 4.0, 4.0, 4.0, 4.0, 4.0, 4.0, 4.0])
        self.assertFalse(tx.gave_up)
        self.assertEqual(tx.on_tick(tx.next_deadline()), [])
        self.assertTrue(tx.gave_up)
        self.assertEqual(names(events)[-1], 'GIVE_UP')
        self.assertIsNone(tx.next_deadline())
        self.assertEqual(tx.timeouts, 8)
        self.assertEqual(tx.on_ack(ISS + 100, BIG, False, 50.0), [])

    def test_new_ack_resets_timeouts_in_row(self):
        """Only timeouts in a row count towards giving up."""
        tx, _ = make(max_retries=2)
        tx.write(data_of(300), 0.0)
        tx.on_tick(tx.next_deadline())
        tx.on_tick(tx.next_deadline())
        self.assertEqual(tx.timeouts_in_row, 2)
        tx.on_ack(ISS + 100, BIG, False, 10.0)
        self.assertEqual(tx.timeouts_in_row, 0)
        tx.on_tick(tx.next_deadline())
        self.assertFalse(tx.gave_up)

    def test_timeout_resend_longer_than_first_copy(self):
        """A full resend past snd_max moves snd_max, so its ACK is accepted."""
        tx, _ = make(rwnd=50)
        data = data_of(400)
        self.assertEqual([len(s.payload) for s in tx.write(data, 0.0)], [50])
        out = tx.on_tick(1.0)
        self.assertEqual(out, [SendSegment(ISS, data[:100], False, 'timeout')])
        self.assertEqual((tx.snd_nxt, tx.snd_max), (ISS + 100, ISS + 100))
        tx.on_ack(ISS + 100, 50, False, 1.1)
        self.assertEqual(tx.snd_una, ISS + 100)


class RttSampleTest(unittest.TestCase):
    """SampleRTT from first transmissions only (slide 3-81)."""

    def test_sample_from_first_transmission(self):
        """The ACK of a timed first transmission gives one sample."""
        tx, events = make()
        tx.write(data_of(100), 0.0)
        tx.on_ack(ISS + 100, BIG, False, 0.1)
        rtt = [e[1] for e in events if e[0] == 'RTT']
        self.assertEqual(len(rtt), 1)
        self.assertAlmostEqual(rtt[0]['sample'], 0.1)
        self.assertAlmostEqual(tx.rtt.estimated_rtt, 0.1)
        self.assertAlmostEqual(tx.rtt.rto, 0.3)

    def test_one_segment_timed_at_a_time(self):
        """Segments sent while one is timed are not timed."""
        tx, events = make(cwnd=3 * MSS)
        tx.write(data_of(300), 0.0)
        self.assertEqual(tx.timed_end, ISS + 100)
        tx.on_ack(ISS + 300, BIG, False, 0.2)
        self.assertEqual(names(events).count('RTT'), 1)

    def test_no_sample_after_timeout(self):
        """An ACK after a timeout retransmission gives no sample."""
        tx, events = make()
        tx.write(data_of(100), 0.0)
        tx.on_tick(1.0)
        tx.on_ack(ISS + 100, BIG, False, 1.05)
        self.assertNotIn('RTT', names(events))
        self.assertIsNone(tx.rtt.estimated_rtt)
        self.assertEqual(tx.rtt.rto, 2.0)

    def test_no_sample_after_fast_retransmit(self):
        """A fast retransmission cancels the timing too."""
        tx, events = make(cwnd=4 * MSS)
        tx.write(data_of(400), 0.0)
        for _ in range(3):
            tx.on_ack(ISS, BIG, False, 0.1)
        tx.on_ack(ISS + 400, BIG, False, 0.2)
        self.assertNotIn('RTT', names(events))

    def test_no_sample_from_go_back_resend(self):
        """Bytes resent after the go-back are never timed."""
        tx, events = make(cwnd=4 * MSS)
        tx.write(data_of(400), 0.0)
        tx.on_tick(1.0)
        tx.on_ack(ISS + 100, BIG, False, 1.1)
        self.assertIsNone(tx.timed_end)
        tx.on_ack(ISS + 300, BIG, False, 1.2)
        self.assertNotIn('RTT', names(events))


class FastRetransmitTest(unittest.TestCase):
    """Fast retransmit on the third duplicate ACK (slide 3-88)."""

    def test_third_duplicate_ack(self):
        """Exactly the third duplicate ACK resends the segment at snd_una."""
        tx, events = make(cwnd=5 * MSS)
        data = data_of(500)
        tx.write(data, 0.0)
        deadline = tx.timer_deadline
        self.assertEqual(tx.on_ack(ISS, BIG, False, 0.1), [])
        self.assertEqual(tx.on_ack(ISS, BIG, False, 0.1), [])
        out = tx.on_ack(ISS, BIG, False, 0.1)
        fast = [s for s in out if s.kind == 'fast']
        self.assertEqual(fast, [SendSegment(ISS, data[:100], False, 'fast')])
        self.assertEqual(tx.fast_retransmits, 1)
        self.assertEqual(tx.snd_nxt, ISS + 500)
        self.assertEqual(tx.timer_deadline, deadline)
        self.assertEqual(tx.cc.state, FAST_RECOVERY)
        out = tx.on_ack(ISS, BIG, False, 0.1)
        self.assertEqual([s for s in out if s.kind == 'fast'], [])
        self.assertEqual(tx.fast_retransmits, 1)
        dupacks = [e[1]['count'] for e in events if e[0] == 'DUPACK']
        self.assertEqual(dupacks, [1, 2, 3, 3])
        self.assertEqual(names(events).count('FAST_RETRANSMIT'), 1)

    def test_acks_with_data_are_not_duplicates(self):
        """ACKs that carry data do not count."""
        tx, events = make(cwnd=5 * MSS)
        tx.write(data_of(500), 0.0)
        for _ in range(4):
            self.assertEqual(tx.on_ack(ISS, BIG, True, 0.1), [])
        self.assertNotIn('DUPACK', names(events))
        self.assertEqual(tx.fast_retransmits, 0)

    def test_window_change_is_not_duplicate(self):
        """ACKs that change the window do not count."""
        tx, events = make(rwnd=5000, cwnd=5 * MSS)
        tx.write(data_of(500), 0.0)
        for window in (4900, 4800, 4700, 4600):
            tx.on_ack(ISS, window, False, 0.1)
        self.assertNotIn('DUPACK', names(events))
        self.assertEqual(tx.rwnd, 4600)
        self.assertEqual(tx.fast_retransmits, 0)

    def test_nothing_outstanding_is_not_duplicate(self):
        """Repeated ACKs with nothing in flight do not count."""
        tx, events = make()
        run_clean(tx, data_of(200))
        for _ in range(4):
            self.assertEqual(tx.on_ack(ISS + 200, BIG, False, 1.0), [])
        self.assertNotIn('DUPACK', names(events))

    def test_inflation_sends_new_data(self):
        """In fast recovery the inflated cwnd lets new segments out."""
        tx, _ = make(cwnd=4 * MSS)
        tx.write(data_of(1000), 0.0)
        tx.on_ack(ISS, BIG, False, 0.1)
        tx.on_ack(ISS, BIG, False, 0.1)
        out = tx.on_ack(ISS, BIG, False, 0.1)
        # ssthresh = 200, cwnd = 200 + 3 * 100 = 500, 400 in flight
        self.assertEqual([(s.seq, s.kind) for s in out], [(ISS, 'fast'), (ISS + 400, 'new')])
        out = tx.on_ack(ISS, BIG, False, 0.1)
        self.assertEqual([(s.seq, s.kind) for s in out], [(ISS + 500, 'new')])
        self.assertEqual(tx.cc.cwnd, 600)
        self.assertEqual(tx.bytes_in_flight(), 600)

    def test_fast_resend_longer_than_first_copy(self):
        """A fast retransmit past snd_max moves snd_max, so its ACK is accepted."""
        tx, _ = make(rwnd=50, cwnd=4 * MSS)
        data = data_of(400)
        tx.write(data, 0.0)
        tx.on_ack(ISS, 50, False, 0.1)
        tx.on_ack(ISS, 50, False, 0.1)
        out = tx.on_ack(ISS, 50, False, 0.1)
        self.assertEqual(out, [SendSegment(ISS, data[:100], False, 'fast')])
        self.assertEqual((tx.snd_nxt, tx.snd_max), (ISS + 50, ISS + 100))
        out = tx.on_ack(ISS + 100, 50, False, 0.2)
        self.assertEqual(tx.snd_una, ISS + 100)
        self.assertEqual([(s.seq, len(s.payload), s.kind) for s in out], [(ISS + 100, 50, 'new')])


class WindowLimitTest(unittest.TestCase):
    """In flight never exceeds min(cwnd, rwnd) (slides 3-122, 3-94)."""

    def check_invariant(self, events):
        """
        Assert the limit after every SEND and RETRANSMIT event.

        Args:
            events: Events recorded by ``make``.

        Returns:
            int: How many events were checked.
        """
        checked = 0
        for name, details, in_flight, cwnd, rwnd in events:
            if name in ('SEND', 'RETRANSMIT'):
                self.assertLessEqual(in_flight, min(cwnd, rwnd), details)
                checked += 1
        return checked

    def test_long_lossy_run(self):
        """A long transfer with losses and a changing window keeps the limit."""
        tx, events = make(rwnd=800, ssthresh=1600)
        rx = HandReceiver()
        rng = random.Random(5)
        data = data_of(30000)
        now = 0.0
        window = 800
        queue = deque(tx.write(data, now))
        for _ in range(20000):
            if tx.data_acked():
                break
            if queue:
                seg = queue.popleft()
                now += 0.005
                if rng.random() < 0.1:
                    continue
                ack = rx.take(seg)
                if rng.random() < 0.05:
                    window = rng.choice([150, 300, 500, 800, 1200])
                queue.extend(tx.on_ack(ack, window, False, now))
            else:
                now = max(now, tx.next_deadline())
                queue.extend(tx.on_tick(now))
        self.assertTrue(tx.data_acked())
        self.assertEqual(bytes(rx.data), data)
        self.assertGreater(self.check_invariant(events), 300)
        self.assertGreater(tx.timeouts, 0)
        self.assertGreater(tx.fast_retransmits, 0)
        self.assertGreater(names(events).count('RETRANSMIT'), 0)
        self.assertFalse(tx.gave_up)

    def test_rwnd_limits_clean_run(self):
        """With a small rwnd and a big cwnd, rwnd sets the limit."""
        tx, events = make(rwnd=300, cwnd=10 * MSS)
        rx, _ = run_clean(tx, data_of(2000), window=300)
        self.assertEqual(len(rx.data), 2000)
        self.assertGreater(self.check_invariant(events), 0)
        self.assertEqual(max(e[2] for e in events if e[0] == 'SEND'), 300)


class SmallSegmentTest(unittest.TestCase):
    """Each segment is at most one MSS and at most the room left (slide 3-122)."""

    def test_room_below_mss_used_while_in_flight(self):
        """Room below one MSS is used at once, even while data is in flight."""
        tx, _ = make(rwnd=250, cwnd=10 * MSS)
        out = tx.write(data_of(1000), 0.0)
        self.assertEqual([len(s.payload) for s in out], [100, 100, 50])
        self.assertEqual(tx.bytes_in_flight(), 250)
        out = tx.on_ack(ISS + 100, 250, False, 0.1)
        self.assertEqual([(s.seq, len(s.payload)) for s in out], [(ISS + 250, 100)])
        self.assertEqual(tx.bytes_in_flight(), 250)

    def test_last_piece_may_be_small(self):
        """The last piece of data goes out even while data is in flight."""
        tx, _ = make(cwnd=10 * MSS)
        out = tx.write(data_of(250), 0.0)
        self.assertEqual([len(s.payload) for s in out], [100, 100, 50])

    def test_small_segment_when_nothing_in_flight(self):
        """With nothing in flight a window below one MSS is used."""
        tx, _ = make(rwnd=50, cwnd=10 * MSS)
        out = tx.write(data_of(1000), 0.0)
        self.assertEqual([(s.seq, len(s.payload)) for s in out], [(ISS, 50)])
        out = tx.on_ack(ISS + 50, 50, False, 0.1)
        self.assertEqual([(s.seq, len(s.payload)) for s in out], [(ISS + 50, 50)])


class ZeroWindowTest(unittest.TestCase):
    """Zero-window probes keep asking for the window (slide 3-94)."""

    def zero_after_first(self):
        """
        Send one segment, then get an ACK that closes the window.

        Returns:
            tuple: (sender, events, data).
        """
        tx, events = make()
        data = data_of(500)
        tx.write(data, 0.0)
        self.assertIsNone(tx.probe_deadline)
        tx.on_ack(ISS + 100, 0, False, 0.1)
        return tx, events, data

    def test_zero_window_arms_probe(self):
        """rwnd 0 with nothing in flight arms the probe 0.5 s later."""
        tx, events, data = self.zero_after_first()
        self.assertIn('RWND_ZERO', names(events))
        self.assertIsNone(tx.timer_deadline)
        self.assertAlmostEqual(tx.probe_deadline, 0.6)
        self.assertEqual(tx.on_tick(0.59), [])
        out = tx.on_tick(tx.probe_deadline)
        self.assertEqual(out, [SendSegment(ISS + 100, data[100:101], False, 'probe')])
        self.assertEqual(tx.snd_nxt, ISS + 100)
        self.assertEqual(tx.bytes_in_flight(), 0)
        self.assertEqual(tx.probe_seq, ISS + 100)
        self.assertEqual(tx.probes, 1)

    def test_probe_interval_is_fixed(self):
        """One probe every 0.5 s while rwnd stays 0."""
        tx, events = make(rwnd=0)
        self.assertEqual(tx.write(data_of(500), 0.0), [])
        times = []
        now = 0.0
        for _ in range(6):
            now = tx.next_deadline()
            out = tx.on_tick(now)
            self.assertEqual([(s.seq, len(s.payload), s.kind) for s in out],
                             [(ISS, 1, 'probe')])
            times.append(now)
            self.assertEqual(tx.on_ack(ISS, 0, False, now + 0.01), [])
        gaps = [b - a for a, b in zip([0.0] + times, times)]
        for gap in gaps:
            self.assertAlmostEqual(gap, 0.5)
        self.assertEqual(names(events).count('PROBE'), 6)
        self.assertEqual(tx.timeouts, 0)

    def test_ack_covering_probe_byte(self):
        """An ACK of the probe byte is accepted; probing goes on at the next byte."""
        tx, events, data = self.zero_after_first()
        tx.on_tick(tx.probe_deadline)
        deadline = tx.probe_deadline
        out = tx.on_ack(ISS + 101, 0, False, 0.61)
        self.assertEqual(out, [])
        self.assertEqual((tx.snd_una, tx.snd_nxt), (ISS + 101, ISS + 101))
        self.assertIsNone(tx.probe_seq)
        self.assertEqual(tx.probe_deadline, deadline)
        out = tx.on_tick(deadline)
        self.assertEqual(out, [SendSegment(ISS + 101, data[101:102], False, 'probe')])

    def test_window_update_after_acked_probe(self):
        """A window update that repeats the probe byte's ACK is not ignored."""
        tx, events, data = self.zero_after_first()
        tx.on_tick(tx.probe_deadline)
        tx.on_ack(ISS + 101, 0, False, 0.61)
        out = tx.on_ack(ISS + 101, 1000, False, 0.7)
        self.assertIn('RWND_OPEN', names(events))
        self.assertEqual(out[0], SendSegment(ISS + 101, data[101:201], False, 'new'))

    def test_window_update_resumes_sending(self):
        """A window update stops probing and sends data again."""
        tx, events, data = self.zero_after_first()
        tx.on_tick(tx.probe_deadline)
        out = tx.on_ack(ISS + 100, 1000, False, 0.65)
        self.assertIn('RWND_OPEN', names(events))
        self.assertIsNone(tx.probe_deadline)
        self.assertEqual([(s.seq, s.kind) for s in out], [(ISS + 100, 'new'), (ISS + 200, 'new')])
        self.assertIsNotNone(tx.timer_deadline)

    def test_lost_window_update_recovered_by_probe(self):
        """The receiver's answer to the next probe opens the window."""
        tx, events, data = self.zero_after_first()
        rx = HandReceiver(ISS + 100)
        probe = tx.on_tick(tx.probe_deadline)[0]
        self.assertEqual(tx.on_ack(ISS + 100, 0, False, 0.61), [])   # byte refused
        # the receiver reads; its window update to 1000 is lost
        probe = tx.on_tick(tx.next_deadline())[0]
        self.assertEqual(probe.seq, ISS + 100)
        ack = rx.take(probe)
        out = tx.on_ack(ack, 1000, False, 1.2)
        self.assertEqual(ack, ISS + 101)
        self.assertIn('RWND_OPEN', names(events))
        self.assertIsNone(tx.probe_deadline)
        # cwnd is 3 MSS after the two new ACKs
        self.assertEqual([s.seq for s in out], [ISS + 101, ISS + 201, ISS + 301])
        self.assertTrue(all(s.kind == 'new' for s in out))

    def test_no_probe_while_data_in_flight(self):
        """A zero window with data in flight is left to the timer."""
        tx, _ = make(cwnd=2 * MSS)
        tx.write(data_of(500), 0.0)
        tx.on_ack(ISS + 100, 0, False, 0.1)
        self.assertGreater(tx.bytes_in_flight(), 0)
        self.assertIsNone(tx.probe_deadline)


class FinTest(unittest.TestCase):
    """Our FIN takes one sequence number and is retransmitted until ACKed."""

    def test_fin_after_data(self):
        """The FIN follows the last data byte, then gets ACKed."""
        tx, events = make(cwnd=10 * MSS)
        tx.write(data_of(150), 0.0)
        out = tx.close(0.0)
        self.assertEqual(out, [SendSegment(ISS + 150, b"", True, 'fin')])
        self.assertEqual(tx.snd_nxt, ISS + 151)
        self.assertIn('FIN_SENT', names(events))
        tx.on_ack(ISS + 150, BIG, False, 0.1)
        self.assertTrue(tx.data_acked())
        self.assertFalse(tx.fin_acked())
        self.assertIsNotNone(tx.timer_deadline)
        tx.on_ack(ISS + 151, BIG, False, 0.2)
        self.assertTrue(tx.fin_acked())
        self.assertIsNone(tx.timer_deadline)

    def test_fin_waits_for_data(self):
        """The FIN does not overtake data still waiting for window."""
        tx, events = make()
        queue = deque(tx.write(data_of(300), 0.0))
        self.assertEqual(tx.close(0.0), [])
        rx = HandReceiver()
        now = 0.0
        kinds = []
        while queue:
            now += 0.01
            out = tx.on_ack(rx.take(queue.popleft()), BIG, False, now)
            kinds.extend(s.kind for s in out)
            queue.extend(out)
        self.assertEqual(kinds[-1], 'fin')
        self.assertEqual(kinds.count('fin'), 1)
        self.assertTrue(rx.fin)
        self.assertTrue(tx.fin_acked())

    def test_fin_retransmitted_on_timeout(self):
        """A lost FIN is resent by the timer."""
        tx, _ = make()
        rx, _ = run_clean(tx, data_of(100))
        tx.close(1.0)
        self.assertEqual(tx.timer_deadline, 1.0 + tx.rtt.rto)
        out = tx.on_tick(tx.timer_deadline)
        self.assertEqual(out, [SendSegment(ISS + 100, b"", True, 'timeout')])
        self.assertEqual(tx.snd_nxt, ISS + 101)
        self.assertFalse(tx.fin_acked())
        tx.on_ack(ISS + 101, BIG, False, 5.0)
        self.assertTrue(tx.fin_acked())

    def test_fin_fast_retransmit(self):
        """Three duplicate ACKs for the FIN resend the FIN."""
        tx, _ = make()
        run_clean(tx, data_of(100))
        tx.close(1.0)
        out = []
        for _ in range(3):
            out.extend(tx.on_ack(ISS + 100, BIG, False, 1.1))
        self.assertEqual(out, [SendSegment(ISS + 100, b"", True, 'fast')])


if __name__ == '__main__':
    unittest.main()

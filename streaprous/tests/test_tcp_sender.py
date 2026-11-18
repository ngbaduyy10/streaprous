"""
Tests for the TCP sender: segments, the one timer, ACKs, fast retransmit, windows and probes.
"""

import random
import unittest
from collections import deque

from streaprous_socket.tcp_reno import FAST_RECOVERY, SLOW_START, RenoCongestion
from streaprous_socket.tcp_rtt import RttEstimator
from streaprous_socket.tcp_sender import PROBE_INTERVAL, SendSegment, TcpSender
from tests.slide_rules import assert_slide_rules

ISN = 1000
MSS = 100
BIG = 1000000

SENDER_RULES = [
    ('TcpSender.send_data', '3-84', 'data received from application'),
    ('TcpSender.new_segment', '3-84', 'seq # is byte-stream number of first data byte in segment'),
    ('TcpSender.new_segment', '3-84', 'start timer if not already running'),
    ('TcpSender.new_segment', 'extra', 'one segment is timed at a time'),
    ('TcpSender.in_flight', '3-122', 'LastByteSent - LastByteAcked'),
    ('TcpSender.send_allowed', '3-125', 'transmit new segment(s), as allowed'),
    ('TcpSender.send_allowed', '3-122', 'LastByteSent - LastByteAcked <= cwnd'),
    ('TcpSender.send_allowed', '3-94',
     'sender limits amount of unACKed ("in-flight") data to received rwnd'),
    ('TcpSender.on_ack', '3-84', 'ACK received'),
    ('TcpSender.on_ack', 'extra', 'ACK for bytes we never sent'),
    ('TcpSender.on_ack', 'extra', 'carries no data and the same window'),
    ('TcpSender.on_new_ack', '3-84', 'update what is known to be ACKed'),
    ('TcpSender.on_new_ack', '3-84', 'start timer if there are still unACKed segments'),
    ('TcpSender.on_duplicate_ack', '3-88', '3 additional ACKs for same data'),
    ('TcpSender.fast_retransmit', '3-88', 'resend unACKed segment with smallest seq #'),
    ('TcpSender.on_timeout', '3-84', 'retransmit segment that caused timeout'),
    ('TcpSender.on_timeout', '3-84', 'restart timer'),
    ('TcpSender.start_timer', '3-84', 'think of timer as for oldest unACKed segment'),
    ('TcpSender.take_rtt_sample', '3-81',
     'measured time from segment transmission until ACK receipt'),
    ('TcpSender.take_rtt_sample', '3-81', 'ignore retransmissions'),
    ('TcpSender.update_rwnd', '3-94', 'to received rwnd'),
    ('TcpSender.send_probe', 'extra', 'one data byte every 0.5 s'),
    ('TcpSender.close', '3-103', 'send TCP segment with FIN bit = 1'),
]


def make(rwnd=BIG, cwnd=None, ssthresh=BIG):
    """Builds a sender with a real RttEstimator and RenoCongestion.

    Every logged event is appended with the sender's numbers at that moment.

    Args:
         rwnd      first window of the receiver
         cwnd      starting cwnd; None keeps Reno's 1 MSS
         ssthresh  first ssthresh

    Returns:
         (sender, list of (event, details, in_flight, cwnd, rwnd))
    """
    events = []
    holder = []

    def log(event, **details):
        """Appends one event with the sender's numbers."""
        sender = holder[0]
        events.append((event, details, sender.in_flight(), sender.reno.cwnd, sender.rwnd))

    reno = RenoCongestion(MSS, ssthresh, log)
    if cwnd is not None:
        reno.cwnd = cwnd
    sender = TcpSender(ISN, MSS, rwnd, reno, RttEstimator(), log)
    holder.append(sender)
    return sender, events


def names(events):
    """Lists the event names in order.

    Args:
         events  events recorded by make
    """
    return [event[0] for event in events]


def details(events, name):
    """Lists the details of every event with this name.

    Args:
         events  events recorded by make
         name    event name
    """
    return [event[1] for event in events if event[0] == name]


def data_of(size):
    """Makes size bytes of test data.

    Args:
         size  number of bytes
    """
    return bytes(i % 251 for i in range(size))


def next_deadline(sender):
    """Gives the earlier of the timer and probe deadlines, or None.

    Args:
         sender  the TcpSender
    """
    deadlines = [d for d in (sender.timer_deadline, sender.probe_deadline) if d is not None]
    return min(deadlines) if deadlines else None


class HandReceiver:
    """A tiny receiver played by hand: keeps out-of-order segments, gives the cumulative ACK."""

    def __init__(self, start=ISN):
        """Expects seq # start first.

        Args:
             start  first expected seq #
        """
        self.expected_seq_num = start
        self.data = bytearray()
        self.held = {}
        self.fin = False

    def take(self, segment):
        """Accepts one segment and gives the ACK number.

        Args:
             segment  a SendSegment from the sender

        Returns:
             the seq # of the next byte expected
        """
        self.held[segment.seq] = segment
        moved = True
        while moved:
            moved = False
            for seq in sorted(self.held):
                seg = self.held.pop(seq)
                end = seq + len(seg.payload)
                if seq <= self.expected_seq_num < end:
                    self.data.extend(seg.payload[self.expected_seq_num - seq:])
                    self.expected_seq_num = end
                    moved = True
                if seg.fin and end == self.expected_seq_num and not self.fin:
                    self.expected_seq_num = end + 1
                    self.fin = True
                    moved = True
                if end > self.expected_seq_num or (seg.fin and not self.fin):
                    self.held[seq] = seg
        return self.expected_seq_num


def run_clean(sender, data, close=False, window=BIG):
    """Sends data over a loss-free path; each segment is ACKed at once.

    Args:
         sender  the TcpSender
         data    bytes to send
         close   also send the FIN
         window  window the receiver advertises

    Returns:
         (receiver, list of every segment sent)
    """
    receiver = HandReceiver()
    now = 0.0
    queue = deque(sender.send_data(data, now))
    if close:
        queue.extend(sender.close(now))
    sent = list(queue)
    while queue:
        now += 0.01
        out = sender.on_ack(receiver.take(queue.popleft()), window, False, now)
        sent.extend(out)
        queue.extend(out)
    return receiver, sent


def zero_after_first():
    """Sends one segment of 500 bytes of data, then an ACK closes the window.

    Returns:
         (sender, events, data)
    """
    sender, events = make()
    data = data_of(500)
    sender.send_data(data, 0.0)
    sender.on_ack(ISN + 100, 0, False, 0.1)
    return sender, events, data


class SegmentTest(unittest.TestCase):
    """Segments with seq # (slides 3-84, 3-146)."""

    def test_segments_cover_data_with_slide_seq_numbers(self):
        """Slide 3-84: seq # is byte-stream number of first data byte in segment."""
        sender, events = make()
        data = data_of(1234)
        receiver, sent = run_clean(sender, data)
        self.assertEqual(len(sent), 13)
        expected = ISN
        for segment in sent:
            self.assertEqual((segment.seq, segment.fin, segment.kind), (expected, False, 'new'))
            self.assertLessEqual(len(segment.payload), MSS)
            self.assertEqual(segment.payload, data[expected - ISN:expected - ISN + MSS])
            expected += len(segment.payload)
        self.assertEqual(expected, ISN + 1234)
        self.assertEqual(details(events, 'SEND')[0], {'seq': ISN, 'len': 100})
        self.assertEqual(bytes(receiver.data), data)
        self.assertTrue(sender.data_acked())
        self.assertEqual((sender.send_base, sender.next_seq_num), (ISN + 1234, ISN + 1234))

    def test_send_data_joins_stream(self):
        """Slide 3-84 data received from application: later bytes join the stream."""
        sender, _ = make(cwnd=4 * MSS)
        data = data_of(100)
        sender.send_data(data[:50], 0.0)
        out = sender.send_data(data[50:], 0.1)
        self.assertEqual(out, [SendSegment(ISN + 50, data[50:], False, 'new')])
        self.assertEqual(sender.data_end(), ISN + 100)


class TimerTest(unittest.TestCase):
    """One timer for the oldest unACKed segment (slide 3-84)."""

    def test_timer_started_once_for_oldest_segment(self):
        """Slide 3-84: start timer if not already running; a later segment does not move it."""
        sender, _ = make(cwnd=4 * MSS)
        self.assertIsNone(sender.timer_deadline)
        sender.send_data(data_of(100), 0.0)
        self.assertEqual(sender.timer_deadline, 1.0)
        out = sender.send_data(data_of(100), 0.5)
        self.assertEqual([s.seq for s in out], [ISN + 100])
        self.assertEqual(sender.timer_deadline, 1.0)

    def test_new_ack_restarts_or_stops_timer(self):
        """Slide 3-84: start timer if there are still unACKed segments, else stop it."""
        sender, _ = make(cwnd=3 * MSS)
        sender.send_data(data_of(300), 0.0)
        sender.on_ack(ISN + 100, BIG, False, 0.1)
        self.assertAlmostEqual(sender.rtt.timeout_interval, 0.3)
        self.assertAlmostEqual(sender.timer_deadline, 0.4)
        sender.on_ack(ISN + 300, BIG, False, 0.2)
        self.assertIsNone(sender.timer_deadline)

    def test_tick_before_deadline_does_nothing(self):
        """Slide 3-84: the timer fires only once TimeoutInterval has passed."""
        sender, events = make()
        sender.send_data(data_of(100), 0.0)
        self.assertEqual(sender.on_tick(0.99), [])
        self.assertNotIn('TIMEOUT', names(events))


class TimeoutTest(unittest.TestCase):
    """Event timeout: retransmit segment that caused timeout, restart timer (slide 3-84)."""

    def test_timeout_resends_oldest_unacked_segment(self):
        """Flaw scenario: a data segment is lost; the timer resends the oldest unACKed one."""
        sender, _ = make(cwnd=4 * MSS)
        data = data_of(400)
        self.assertEqual(len(sender.send_data(data, 0.0)), 4)
        self.assertEqual(sender.on_tick(1.0), [SendSegment(ISN, data[:100], False, 'timeout')])
        sender.on_ack(ISN + 100, BIG, False, 1.1)
        self.assertAlmostEqual(sender.timer_deadline, 2.1)
        out = sender.on_tick(2.1)
        self.assertEqual(out, [SendSegment(ISN + 100, data[100:200], False, 'timeout')])

    def test_timeout_resends_only_one_segment(self):
        """Slide 3-84 and flaw scenario 5: one segment per timeout; NextSeqNum never moves back."""
        sender, events = make(cwnd=4 * MSS)
        sender.send_data(data_of(400), 0.0)
        self.assertEqual(len(sender.on_tick(1.0)), 1)
        self.assertEqual(sender.next_seq_num, ISN + 400)
        self.assertEqual(sender.on_ack(ISN + 100, BIG, False, 1.1), [])
        self.assertEqual(len(sender.on_tick(sender.timer_deadline)), 1)
        self.assertEqual(sender.next_seq_num, ISN + 400)
        self.assertEqual(names(events).count('SEND'), 4)

    def test_timeout_keeps_timeout_interval(self):
        """Slide 3-84 restart timer: nothing doubles TimeoutInterval."""
        sender, events = make()
        sender.send_data(data_of(100), 0.0)
        times = []
        for _ in range(5):
            now = sender.timer_deadline
            self.assertEqual(len(sender.on_tick(now)), 1)
            times.append(now)
            self.assertEqual(sender.rtt.timeout_interval, 1.0)
        self.assertEqual(times, [1.0, 2.0, 3.0, 4.0, 5.0])
        self.assertEqual([d['interval'] for d in details(events, 'TIMEOUT')], [1.0] * 5)

    def test_timeout_calls_reno(self):
        """Slide 3-125 timeout: ssthresh = cwnd/2, cwnd = 1 MSS, go to slow start."""
        sender, events = make(cwnd=4 * MSS)
        sender.send_data(data_of(400), 0.0)
        sender.on_tick(1.0)
        reno = sender.reno
        self.assertEqual((reno.cwnd, reno.ssthresh, reno.state), (MSS, 200, SLOW_START))
        self.assertEqual(details(events, 'TIMEOUT'), [{'seq': ISN, 'len': 100, 'interval': 1.0}])

    def test_timeout_with_nothing_in_flight_stops_timer(self):
        """Slide 3-146: with no not-yet-acked segment the timer stops and nothing is resent."""
        sender, events = make()
        run_clean(sender, data_of(200))
        cwnd = sender.reno.cwnd
        sender.timer_deadline = 5.0
        self.assertEqual(sender.on_tick(5.0), [])
        self.assertIsNone(sender.timer_deadline)
        self.assertEqual(sender.reno.cwnd, cwnd)
        self.assertNotIn('TIMEOUT', names(events))

    def test_resend_never_past_next_seq_num(self):
        """Flaw scenario: a resend longer than what was sent; it stops at NextSeqNum."""
        sender, _ = make(rwnd=50, cwnd=4 * MSS)
        data = data_of(400)
        self.assertEqual([len(s.payload) for s in sender.send_data(data, 0.0)], [50])
        self.assertEqual(sender.on_tick(1.0), [SendSegment(ISN, data[:50], False, 'timeout')])
        self.assertEqual(sender.next_seq_num, ISN + 50)
        sender, _ = make(rwnd=50, cwnd=4 * MSS)
        sender.send_data(data, 0.0)
        out = []
        for _ in range(3):
            out.extend(sender.on_ack(ISN, 50, False, 0.1))
        self.assertEqual(out, [SendSegment(ISN, data[:50], False, 'fast')])
        self.assertEqual(sender.on_ack(ISN + 100, 50, False, 0.2), [])
        self.assertEqual(sender.send_base, ISN)

    def test_cumulative_ack_covers_lost_ack(self):
        """Slide 3-87: cumulative ACK covers for earlier lost ACK; nothing is resent."""
        sender, events = make(cwnd=2 * MSS)
        sender.send_data(data_of(200), 0.0)
        # the ACK for the first segment is lost
        self.assertEqual(sender.on_ack(ISN + 200, BIG, False, 0.2), [])
        self.assertEqual(sender.send_base, ISN + 200)
        self.assertTrue(sender.data_acked())
        self.assertIsNone(sender.timer_deadline)
        self.assertEqual(sender.on_tick(5.0), [])
        self.assertNotIn('TIMEOUT', names(events))


class DuplicateAckTest(unittest.TestCase):
    """Fast retransmit on 3 additional ACKs for same data (slides 3-88, 3-125)."""

    def test_third_duplicate_ack_fast_retransmit(self):
        """Slide 3-88: the third duplicate ACK resends the unACKed segment with smallest seq #."""
        sender, events = make(cwnd=5 * MSS)
        data = data_of(500)
        sender.send_data(data, 0.0)
        self.assertEqual(sender.on_ack(ISN, BIG, False, 0.1), [])
        self.assertEqual(sender.on_ack(ISN, BIG, False, 0.1), [])
        out = sender.on_ack(ISN, BIG, False, 0.1)
        self.assertEqual(out, [SendSegment(ISN, data[:100], False, 'fast')])
        self.assertEqual(sender.timer_deadline, 1.0)
        self.assertEqual(sender.next_seq_num, ISN + 500)
        self.assertEqual(sender.reno.state, FAST_RECOVERY)
        self.assertEqual(sender.on_ack(ISN, BIG, False, 0.1), [])
        self.assertEqual([d['count'] for d in details(events, 'DUPACK')], [1, 2, 3, 3])
        self.assertEqual(details(events, 'FAST_RETRANSMIT'), [{'seq': ISN, 'len': 100}])

    def test_ack_with_data_is_not_duplicate(self):
        """Extra and flaw scenario: an ACK that carries data is not a duplicate ACK."""
        sender, events = make(cwnd=5 * MSS)
        sender.send_data(data_of(500), 0.0)
        for _ in range(4):
            self.assertEqual(sender.on_ack(ISN, BIG, True, 0.1), [])
        self.assertNotIn('DUPACK', names(events))
        self.assertEqual(sender.reno.dup_ack_count, 0)

    def test_window_change_is_not_duplicate(self):
        """Extra and flaw scenario: an ACK with a new window is a window update, not a duplicate."""
        sender, events = make(rwnd=5000, cwnd=5 * MSS)
        sender.send_data(data_of(500), 0.0)
        for window in (4900, 4800, 4700, 4600):
            sender.on_ack(ISN, window, False, 0.1)
        self.assertNotIn('DUPACK', names(events))
        self.assertEqual(sender.rwnd, 4600)

    def test_nothing_in_flight_is_not_duplicate(self):
        """Extra: a repeated ACK with nothing in flight is not a duplicate ACK."""
        sender, events = make()
        run_clean(sender, data_of(200))
        for _ in range(4):
            self.assertEqual(sender.on_ack(ISN + 200, BIG, False, 1.0), [])
        self.assertNotIn('DUPACK', names(events))

    def test_fast_recovery_sends_new_data(self):
        """Slide 3-125 fast recovery: cwnd = cwnd + MSS, transmit new segment(s), as allowed."""
        sender, _ = make(cwnd=4 * MSS)
        sender.send_data(data_of(1000), 0.0)
        sender.on_ack(ISN, BIG, False, 0.1)
        sender.on_ack(ISN, BIG, False, 0.1)
        out = sender.on_ack(ISN, BIG, False, 0.1)
        # ssthresh = 200, cwnd = 200 + 3 MSS = 500, 400 in flight
        self.assertEqual([(s.seq, s.kind) for s in out], [(ISN, 'fast'), (ISN + 400, 'new')])
        out = sender.on_ack(ISN, BIG, False, 0.1)
        self.assertEqual([(s.seq, s.kind) for s in out], [(ISN + 500, 'new')])
        self.assertEqual((sender.reno.cwnd, sender.in_flight()), (600, 600))

    def test_timeout_during_fast_recovery(self):
        """Flaw scenario: a timeout during fast recovery goes to slow start with cwnd = 1 MSS."""
        sender, events = make(cwnd=4 * MSS)
        data = data_of(1000)
        sender.send_data(data, 0.0)
        for _ in range(3):
            sender.on_ack(ISN, BIG, False, 0.1)
        out = sender.on_tick(1.0)
        self.assertEqual(out, [SendSegment(ISN, data[:100], False, 'timeout')])
        reno = sender.reno
        self.assertEqual((reno.ssthresh, reno.cwnd, reno.state), (250, 100, SLOW_START))
        self.assertIn({'old': FAST_RECOVERY, 'new': SLOW_START}, details(events, 'STATE'))
        out = sender.on_ack(ISN + 500, BIG, False, 1.1)
        self.assertEqual([(s.seq, s.kind) for s in out], [(ISN + 500, 'new'), (ISN + 600, 'new')])


class RttSampleTest(unittest.TestCase):
    """SampleRTT from first transmissions only (slide 3-81)."""

    def test_sample_from_first_transmission(self):
        """Slide 3-81: measured time from segment transmission until ACK receipt."""
        sender, events = make()
        sender.send_data(data_of(100), 0.0)
        sender.on_ack(ISN + 100, BIG, False, 0.1)
        samples = details(events, 'RTT')
        self.assertEqual(len(samples), 1)
        self.assertAlmostEqual(samples[0]['sample'], 0.1)
        self.assertAlmostEqual(samples[0]['estimated'], 0.1)
        self.assertAlmostEqual(samples[0]['dev'], 0.05)
        self.assertAlmostEqual(samples[0]['timeout'], 0.3)
        self.assertAlmostEqual(sender.rtt.timeout_interval, 0.3)

    def test_one_segment_timed_at_a_time(self):
        """Extra: one segment is timed at a time; the next new one is timed after its ACK."""
        sender, events = make(cwnd=3 * MSS)
        sender.send_data(data_of(300), 0.0)
        self.assertEqual((sender.rtt_timed_seq, sender.rtt_sent_at), (ISN + 100, 0.0))
        sender.on_ack(ISN + 300, BIG, False, 0.2)
        self.assertEqual(names(events).count('RTT'), 1)
        self.assertAlmostEqual(details(events, 'RTT')[0]['sample'], 0.2)
        sender.send_data(data_of(100), 0.3)
        self.assertEqual((sender.rtt_timed_seq, sender.rtt_sent_at), (ISN + 400, 0.3))

    def test_no_sample_after_timeout(self):
        """Slide 3-81 ignore retransmissions (flaw scenario): no sample after a timeout."""
        sender, events = make()
        sender.send_data(data_of(100), 0.0)
        sender.on_tick(1.0)
        sender.on_ack(ISN + 100, BIG, False, 1.05)
        self.assertNotIn('RTT', names(events))
        self.assertIsNone(sender.rtt.estimated_rtt)
        self.assertEqual(sender.rtt.timeout_interval, 1.0)

    def test_no_sample_after_fast_retransmit(self):
        """Slide 3-81 ignore retransmissions (flaw scenario): no sample after a fast resend."""
        sender, events = make(cwnd=4 * MSS)
        sender.send_data(data_of(400), 0.0)
        for _ in range(3):
            sender.on_ack(ISN, BIG, False, 0.1)
        sender.on_ack(ISN + 400, BIG, False, 0.2)
        self.assertNotIn('RTT', names(events))


class AckFilterTest(unittest.TestCase):
    """ACKs that the sender ignores (slide 3-146 and an extra)."""

    def test_old_ack_ignored(self):
        """Flaw scenario: an old ACK below SendBase changes nothing (slide 3-146: y > SendBase)."""
        sender, events = make(cwnd=2 * MSS)
        sender.send_data(data_of(400), 0.0)
        sender.on_ack(ISN + 100, BIG, False, 0.1)
        before = (sender.send_base, sender.next_seq_num, sender.rwnd, len(events))
        self.assertEqual(sender.on_ack(ISN + 50, 7, False, 0.2), [])
        self.assertEqual((sender.send_base, sender.next_seq_num, sender.rwnd, len(events)),
                         before)

    def test_ack_above_next_seq_num_ignored(self):
        """Extra and flaw scenario: an ACK for bytes we never sent is ignored."""
        sender, events = make(cwnd=2 * MSS)
        sender.send_data(data_of(400), 0.0)
        count = len(events)
        self.assertEqual(sender.on_ack(ISN + 201, BIG, False, 0.1), [])
        self.assertEqual((sender.send_base, sender.next_seq_num), (ISN, ISN + 200))
        self.assertEqual(len(events), count)


class WindowLimitTest(unittest.TestCase):
    """In flight never above min(cwnd, rwnd) (slides 3-122, 3-94)."""

    def test_in_flight_within_cwnd_and_rwnd(self):
        """Slides 3-122 and 3-94: after every SEND, in_flight() <= min(cwnd, rwnd).

        A long run with seeded random segment and ACK loss and a changing window.
        """
        sender, events = make(rwnd=800, ssthresh=1600)
        receiver = HandReceiver()
        rng = random.Random(5)
        data = data_of(30000)
        now = 0.0
        window = 800
        queue = deque(sender.send_data(data, now))
        for _ in range(100000):
            if sender.data_acked():
                break
            if queue:
                now += 0.005
                segment = queue.popleft()
                if rng.random() < 0.05:
                    continue
                ack = receiver.take(segment)
                if rng.random() < 0.05:
                    window = rng.choice([150, 300, 500, 800, 1200])
                if rng.random() < 0.1:
                    continue
                queue.extend(sender.on_ack(ack, window, False, now))
            else:
                now = max(now, next_deadline(sender))
                queue.extend(sender.on_tick(now))
        self.assertTrue(sender.data_acked())
        self.assertEqual(bytes(receiver.data), data)
        sends = [e for e in events if e[0] == 'SEND']
        self.assertGreater(len(sends), 300)
        for event, info, in_flight, cwnd, rwnd in sends:
            self.assertLessEqual(in_flight, min(cwnd, rwnd), info)
        self.assertIn('TIMEOUT', names(events))
        self.assertIn('FAST_RETRANSMIT', names(events))

    def test_rwnd_limits_clean_run(self):
        """Slide 3-94: with a small rwnd and a big cwnd, rwnd sets the limit."""
        sender, events = make(rwnd=300, cwnd=10 * MSS)
        receiver, _ = run_clean(sender, data_of(2000), window=300)
        self.assertEqual(len(receiver.data), 2000)
        self.assertEqual(max(e[2] for e in events if e[0] == 'SEND'), 300)

    def test_rwnd_below_mss_sends_small_segment(self):
        """Flaw scenario: rwnd smaller than one MSS; slide 3-122 counts bytes, so send less."""
        sender, _ = make(rwnd=50, cwnd=10 * MSS)
        out = sender.send_data(data_of(1000), 0.0)
        self.assertEqual([(s.seq, len(s.payload)) for s in out], [(ISN, 50)])
        out = sender.on_ack(ISN + 50, 50, False, 0.1)
        self.assertEqual([(s.seq, len(s.payload)) for s in out], [(ISN + 50, 50)])
        sender, _ = make(rwnd=250, cwnd=10 * MSS)
        out = sender.send_data(data_of(1000), 0.0)
        self.assertEqual([len(s.payload) for s in out], [100, 100, 50])

    def test_shrinking_window_sends_nothing_new(self):
        """Flaw scenario: the window shrinks below what is in flight; nothing new goes out."""
        sender, events = make(rwnd=400, cwnd=10 * MSS)
        sender.send_data(data_of(1000), 0.0)
        self.assertEqual(sender.on_ack(ISN + 100, 100, False, 0.1), [])
        self.assertEqual((sender.next_seq_num, sender.rwnd), (ISN + 400, 100))
        self.assertEqual(names(events).count('SEND'), 4)
        out = sender.on_ack(ISN + 400, 100, False, 0.2)
        self.assertEqual([(s.seq, len(s.payload)) for s in out], [(ISN + 400, 100)])


class ZeroWindowTest(unittest.TestCase):
    """Extra: one probe byte every 0.5 s while rwnd is 0 (slide 3-94 flow control)."""

    def test_zero_window_arms_probe(self):
        """Extra: rwnd 0 with nothing in flight arms the probe 0.5 s later."""
        sender, events, data = zero_after_first()
        self.assertEqual(details(events, 'RWND_ZERO'), [{'ack': ISN}])
        self.assertIsNone(sender.timer_deadline)
        self.assertAlmostEqual(sender.probe_deadline, 0.1 + PROBE_INTERVAL)
        self.assertEqual(sender.on_tick(0.59), [])
        out = sender.on_tick(0.6)
        self.assertEqual(out, [SendSegment(ISN + 100, data[100:101], False, 'probe')])
        self.assertEqual(details(events, 'PROBE'), [{'seq': ISN + 100}])

    def test_probe_interval_is_fixed(self):
        """Extra: one data byte every 0.5 s; the interval never doubles."""
        sender, events = make(rwnd=0)
        self.assertEqual(sender.send_data(data_of(500), 0.0), [])
        times = []
        for _ in range(6):
            now = sender.probe_deadline
            out = sender.on_tick(now)
            self.assertEqual([(s.seq, len(s.payload), s.kind) for s in out], [(ISN, 1, 'probe')])
            times.append(now)
            self.assertEqual(sender.on_ack(ISN, 0, False, now + 0.01), [])
        gaps = [b - a for a, b in zip([0.0] + times, times)]
        for gap in gaps:
            self.assertAlmostEqual(gap, 0.5)
        self.assertNotIn('TIMEOUT', names(events))

    def test_probe_byte_not_counted_in_flight(self):
        """Extra: the probe byte is neither timed nor counted in flight."""
        sender, _, _ = zero_after_first()
        sender.on_tick(0.6)
        self.assertEqual((sender.in_flight(), sender.next_seq_num), (0, ISN + 100))
        self.assertEqual(sender.probe_seq, ISN + 100)
        self.assertIsNone(sender.timer_deadline)
        self.assertIsNone(sender.rtt_timed_seq)

    def test_ack_covering_probe_byte(self):
        """Flaw scenario: the probe byte is accepted; probing goes on, then the window opens."""
        sender, events, data = zero_after_first()
        sender.on_tick(0.6)
        deadline = sender.probe_deadline
        self.assertEqual(sender.on_ack(ISN + 101, 0, False, 0.61), [])
        self.assertEqual((sender.send_base, sender.next_seq_num), (ISN + 101, ISN + 101))
        self.assertIsNone(sender.probe_seq)
        self.assertEqual(sender.probe_deadline, deadline)
        out = sender.on_tick(deadline)
        self.assertEqual(out, [SendSegment(ISN + 101, data[101:102], False, 'probe')])
        out = sender.on_ack(ISN + 101, 1000, False, 1.2)
        self.assertIn('RWND_OPEN', names(events))
        self.assertEqual(out[0], SendSegment(ISN + 101, data[101:201], False, 'new'))

    def test_lost_window_update_recovered_by_probe(self):
        """Flaw scenario: rwnd 0 and the window update is lost; a probe's answer opens it."""
        sender, events, _ = zero_after_first()
        receiver = HandReceiver(ISN + 100)
        sender.on_tick(0.6)
        self.assertEqual(sender.on_ack(ISN + 100, 0, False, 0.61), [])   # byte refused
        # the receiver reads; its window update to 1000 is lost
        probe = sender.on_tick(next_deadline(sender))[0]
        self.assertEqual(probe.seq, ISN + 100)
        ack = receiver.take(probe)
        self.assertEqual(ack, ISN + 101)
        out = sender.on_ack(ack, 1000, False, 1.15)
        self.assertEqual(details(events, 'RWND_OPEN'), [{'rwnd': 1000}])
        self.assertIsNone(sender.probe_deadline)
        # cwnd is 3 MSS after two new ACKs
        self.assertEqual([(s.seq, s.kind) for s in out],
                         [(ISN + 101, 'new'), (ISN + 201, 'new'), (ISN + 301, 'new')])

    def test_window_update_resumes_sending(self):
        """Slide 3-94: a window update stops probing and data flows again."""
        sender, events, _ = zero_after_first()
        sender.on_tick(0.6)
        out = sender.on_ack(ISN + 100, 1000, False, 0.65)
        self.assertIn('RWND_OPEN', names(events))
        self.assertIsNone(sender.probe_deadline)
        self.assertEqual([(s.seq, s.kind) for s in out], [(ISN + 100, 'new'), (ISN + 200, 'new')])
        self.assertIsNotNone(sender.timer_deadline)

    def test_no_probe_while_data_in_flight(self):
        """Extra: a zero window with data in flight is left to the timer."""
        sender, _ = make(cwnd=2 * MSS)
        sender.send_data(data_of(500), 0.0)
        sender.on_ack(ISN + 100, 0, False, 0.1)
        self.assertGreater(sender.in_flight(), 0)
        self.assertIsNone(sender.probe_deadline)


class FinTest(unittest.TestCase):
    """Our FIN takes one seq # and the timer resends it (slides 3-103, 3-84)."""

    def test_fin_after_data(self):
        """Slide 3-103: send TCP segment with FIN bit = 1 after the last data byte."""
        sender, events = make(cwnd=10 * MSS)
        sender.send_data(data_of(150), 0.0)
        self.assertEqual(sender.close(0.0), [SendSegment(ISN + 150, b"", True, 'fin')])
        self.assertEqual((sender.next_seq_num, sender.in_flight()), (ISN + 151, 151))
        self.assertEqual(details(events, 'FIN_SENT'), [{'seq': ISN + 150}])
        self.assertEqual(sender.close(0.0), [])
        sender.on_ack(ISN + 150, BIG, False, 0.1)
        self.assertTrue(sender.data_acked())
        self.assertFalse(sender.fin_acked())
        self.assertIsNotNone(sender.timer_deadline)
        sender.on_ack(ISN + 151, BIG, False, 0.2)
        self.assertTrue(sender.fin_acked())
        self.assertIsNone(sender.timer_deadline)

    def test_fin_waits_for_data(self):
        """Slide 3-103: the FIN does not overtake data still waiting for window."""
        sender, _ = make()
        queue = deque(sender.send_data(data_of(300), 0.0))
        self.assertEqual(sender.close(0.0), [])
        receiver = HandReceiver()
        now = 0.0
        kinds = []
        while queue:
            now += 0.01
            out = sender.on_ack(receiver.take(queue.popleft()), BIG, False, now)
            kinds.extend(s.kind for s in out)
            queue.extend(out)
        self.assertEqual(kinds[-1], 'fin')
        self.assertEqual(kinds.count('fin'), 1)
        self.assertTrue(receiver.fin)
        self.assertTrue(sender.fin_acked())

    def test_fin_resent_on_timeout(self):
        """Slide 3-84 timeout: a lost FIN is resent by the timer."""
        sender, events = make()
        run_clean(sender, data_of(100))
        sender.close(1.0)
        self.assertAlmostEqual(sender.timer_deadline, 1.0 + sender.rtt.timeout_interval)
        out = sender.on_tick(sender.timer_deadline)
        self.assertEqual(out, [SendSegment(ISN + 100, b"", True, 'timeout')])
        self.assertEqual(details(events, 'TIMEOUT')[0]['len'], 0)
        self.assertEqual(sender.next_seq_num, ISN + 101)
        sender.on_ack(ISN + 101, BIG, False, 5.0)
        self.assertTrue(sender.fin_acked())

    def test_fin_fast_retransmit(self):
        """Slide 3-88: three duplicate ACKs for the FIN resend the FIN."""
        sender, _ = make()
        run_clean(sender, data_of(100))
        sender.close(1.0)
        out = []
        for _ in range(3):
            out.extend(sender.on_ack(ISN + 100, BIG, False, 1.1))
        self.assertEqual(out, [SendSegment(ISN + 100, b"", True, 'fast')])


class SlideRuleTest(unittest.TestCase):
    """The sender's docstrings carry the slide rules."""

    def test_slide_rules(self):
        """Each sender function's docstring carries its slide rule."""
        assert_slide_rules(self, 'tcp_sender', SENDER_RULES)


if __name__ == '__main__':
    unittest.main()

"""
Whole transfers between the TCP sender and receiver in virtual time.

Each test runs one transfer over the simulated links of ``tests/sim.py``
and checks that the application read exactly the bytes that were sent,
that every SEND and RETRANSMIT kept in flight at most min(cwnd, rwnd)
(slides 3-122, 3-94), and that the events of the mechanism under test
show up.
"""

import hashlib
import unittest

from tests.sim import Link, Reader, SimulationFailed, run_transfer


ISS = 1000
MSS = 1460


def data_of(size):
    """
    Make deterministic data in which no short block repeats.

    Args:
        size: Number of bytes.

    Returns:
        bytes: ``size`` bytes built from SHA-256 blocks.
    """
    out = bytearray()
    block = 0
    while len(out) < size:
        out.extend(hashlib.sha256(b"sim:" + str(block).encode()).digest())
        block += 1
    return bytes(out[:size])


def names(events):
    """
    List the event names in order.

    Args:
        events: Event values from a Result.

    Returns:
        list: The names.
    """
    return [event.name for event in events]


def of_name(events, name):
    """
    Keep the events with one name.

    Args:
        events: Event values from a Result.
        name: Event name.

    Returns:
        list: The matching events in order.
    """
    return [event for event in events if event.name == name]


def state_changes(result):
    """
    List the Reno state changes of a run.

    Args:
        result: Result of run_transfer.

    Returns:
        list: (old, new) pairs in order.
    """
    return [(e.details['old'], e.details['new']) for e in of_name(result.sender_events, 'STATE')]


def first_rule(test):
    """
    Make a drop rule that drops the first packet passing ``test``.

    Args:
        test: Callable ``test(packet, now)`` that picks the packet.

    Returns:
        tuple: (rule for Link.drop_if, list that receives the dropped
        (time, packet)).
    """
    dropped = []

    def rule(packet, now):
        """Drop the first matching packet only."""
        if not dropped and test(packet, now):
            dropped.append((now, packet))
            return True
        return False

    return rule, dropped


class TransferTestCase(unittest.TestCase):
    """Checks every simulated transfer must pass."""

    def run_checked(self, data, **options):
        """
        Run a transfer and check delivery and the window limit.

        Args:
            data: Bytes to send.
            options: Keyword arguments for run_transfer.

        Returns:
            Result: The finished run.
        """
        result = run_transfer(data, **options)
        self.assertEqual(len(result.delivered), len(data))
        self.assertTrue(result.delivered == data, 'delivered bytes differ from sent bytes')
        self.assertTrue(result.sender.data_acked())
        self.assertFalse(result.sender.gave_up)
        for event in result.sender_events:
            if event.name in ('SEND', 'RETRANSMIT'):
                state = event.state
                self.assertLessEqual(state['in_flight'], min(state['cwnd'], state['rwnd']),
                                     'window exceeded at %.3f: %r' % (event.time, event))
                self.assertLessEqual(event.details['len'], options.get('mss', MSS))
        return result


class CleanTransferTest(TransferTestCase):
    """A loss-free link: slow start, then congestion avoidance (slide 3-125)."""

    def test_clean_200k(self):
        """200 KB arrive intact with no retransmission and both growth phases."""
        data = data_of(200 * 1024)
        result = self.run_checked(data, mss=MSS, rcvbuf=65535, ssthresh=32768)
        sender_names = names(result.sender_events)
        for name in ('TIMEOUT', 'RETRANSMIT', 'FAST_RETRANSMIT', 'DUPACK'):
            self.assertNotIn(name, sender_names)
        self.assertEqual(state_changes(result), [('SLOW_START', 'CONGESTION_AVOIDANCE')])
        sends = of_name(result.sender_events, 'SEND')
        self.assertEqual(sends[0].state['state'], 'SLOW_START')
        self.assertEqual(sends[-1].state['state'], 'CONGESTION_AVOIDANCE')
        self.assertEqual(sum(e.details['len'] for e in sends), len(data))
        self.assertEqual(result.sender.retransmits, 0)
        self.assertNotIn('OUT_OF_ORDER', names(result.receiver_events))
        self.assertGreater(result.sender.cc.cwnd, 32768)

    def test_random_data_loss(self):
        """Seeded random loss on the data link still delivers every byte."""
        data = data_of(150000)
        link = Link(0.01, loss=0.05, seed=11)
        result = self.run_checked(data, data_link=link)
        self.assertTrue(link.dropped)
        self.assertGreater(result.sender.retransmits, 0)

    def test_same_seed_same_run(self):
        """Two runs with the same seeds give the same events and time."""
        data = data_of(60000)
        runs = []
        for _ in range(2):
            link = Link(0.01, loss=0.05, duplicate=0.05, reorder=0.2, seed=4)
            runs.append(run_transfer(data, data_link=link))
        self.assertEqual(names(runs[0].sender_events), names(runs[1].sender_events))
        self.assertEqual(runs[0].time, runs[1].time)


class TimeoutTransferTest(TransferTestCase):
    """Losses that only the timer can repair (slides 3-84, 3-86)."""

    def test_early_drop_times_out(self):
        """The very first segment is lost: nothing else can trigger a resend."""
        data = data_of(100000)
        result = self.run_checked(data, data_link=Link(0.01, drop_new={1}))
        timeouts = of_name(result.sender_events, 'TIMEOUT')
        self.assertEqual(len(timeouts), 1)
        self.assertEqual(timeouts[0].details['seq'], ISS)
        self.assertEqual(timeouts[0].time, 1.0)
        self.assertEqual(timeouts[0].state['cwnd'], MSS)
        self.assertEqual(timeouts[0].state['ssthresh'], 2 * MSS)
        self.assertNotIn('FAST_RETRANSMIT', names(result.sender_events))

    def test_burst_drop_of_whole_window(self):
        """Every segment of one window is lost: one timeout, then go-back."""
        data = data_of(200000)
        lost = []
        after_timeout = []

        def burst(packet, now):
            """Drop every first transmission from the 30th until the timeout."""
            if packet.kind == 'timeout':
                after_timeout.append(now)
            if packet.kind == 'new' and link.new_count >= 30 and not after_timeout:
                lost.append(packet.seq)
                return True
            return False

        link = Link(0.01, drop_if=burst)
        result = self.run_checked(data, data_link=link, rcvbuf=65535)
        self.assertGreaterEqual(len(lost), 4)
        timeouts = of_name(result.sender_events, 'TIMEOUT')
        self.assertEqual(len(timeouts), 1)
        self.assertEqual(timeouts[0].details['seq'], lost[0])
        self.assertNotIn('FAST_RETRANSMIT', names(result.sender_events))
        resent = [e.details['seq'] for e in of_name(result.sender_events, 'RETRANSMIT')]
        self.assertEqual(resent, lost[1:])
        self.assertTrue(all(e.time > timeouts[0].time
                            for e in of_name(result.sender_events, 'RETRANSMIT')))

    def test_timeout_resend_longer_than_first_copy(self):
        """A window below one MSS, then a lost ACK: the full resend is ACKed."""
        data = data_of(20000)
        rule, dropped = first_rule(lambda packet, now: True)
        result = self.run_checked(data, mss=MSS, rcvbuf=1000,
                                  ack_link=Link(0.01, drop_if=rule))
        self.assertEqual(len(dropped), 1)
        first = of_name(result.sender_events, 'SEND')[0]
        timeout = of_name(result.sender_events, 'TIMEOUT')[0]
        self.assertEqual(first.details['len'], 1000)
        self.assertEqual(timeout.details['len'], MSS)
        self.assertEqual(len(of_name(result.sender_events, 'TIMEOUT')), 1)


class FastRetransmitTransferTest(TransferTestCase):
    """One loss in a large window: three duplicate ACKs (slides 3-88, 3-125)."""

    def test_drop_at_large_window(self):
        """Fast retransmit and fast recovery repair the loss without a timeout."""
        data = data_of(200000)
        result = self.run_checked(data, data_link=Link(0.01, drop_new={40}))
        lost_seq = ISS + 39 * MSS
        fast = of_name(result.sender_events, 'FAST_RETRANSMIT')
        self.assertEqual(len(fast), 1)
        self.assertEqual(fast[0].details['seq'], lost_seq)
        self.assertEqual(fast[0].state['state'], 'FAST_RECOVERY')
        self.assertEqual(fast[0].state['cwnd'], fast[0].state['ssthresh'] + 3 * MSS)
        self.assertNotIn('TIMEOUT', names(result.sender_events))
        dupacks = of_name(result.sender_events, 'DUPACK')
        self.assertEqual([e.details['count'] for e in dupacks[:3]], [1, 2, 3])
        self.assertTrue(all(e.details['ack'] == lost_seq for e in dupacks))
        changes = state_changes(result)
        self.assertIn(('SLOW_START', 'FAST_RECOVERY'), changes)
        self.assertEqual(changes[-1], ('FAST_RECOVERY', 'CONGESTION_AVOIDANCE'))
        out_of_order = of_name(result.receiver_events, 'OUT_OF_ORDER')
        self.assertTrue(out_of_order)
        self.assertTrue(all(e.details['expected'] == lost_seq for e in out_of_order))


class ReceiverTransferTest(TransferTestCase):
    """Reordered and repeated segments on the data link (slide 3-85)."""

    def test_reorder_and_duplicate(self):
        """Out-of-order segments are held and copies discarded; data is intact."""
        data = data_of(150000)
        link = Link(0.01, reorder=0.25, reorder_delay=0.03, duplicate=0.05, seed=3)
        result = self.run_checked(data, data_link=link)
        self.assertGreater(link.held_back, 0)
        self.assertGreater(link.copies, 0)
        receiver_names = names(result.receiver_events)
        self.assertIn('OUT_OF_ORDER', receiver_names)
        self.assertIn('DUP_SEGMENT', receiver_names)
        reasons = [e.details['reason'] for e in of_name(result.receiver_events, 'ACK_SENT')]
        self.assertIn('duplicate', reasons)
        self.assertIn('gap_filled', reasons)
        self.assertIn('old_segment', reasons)

    def test_ack_loss(self):
        """10 % of the ACKs are lost; cumulative ACKs cover for them (slide 3-87)."""
        data = data_of(150000)
        link = Link(0.01, loss=0.1, seed=5)
        self.run_checked(data, ack_link=link)
        self.assertGreater(len(link.dropped), 0.05 * link.sent)


class FlowControlTransferTest(TransferTestCase):
    """A slow reader closes the window; probes reopen it (slide 3-94)."""

    def slow_reader(self):
        """
        Make the reader of these tests.

        Returns:
            Reader: 1024 bytes every 0.05 s, with a 2 s pause after 16 KB.
        """
        return Reader(chunk=1024, interval=0.05, pause_after=16384, pause=2.0)

    def test_slow_reader(self):
        """The window reaches 0, the sender probes every 0.5 s, and sending resumes."""
        data = data_of(65536)
        result = self.run_checked(data, mss=536, rcvbuf=4096, reader=self.slow_reader())
        sender_names = names(result.sender_events)
        for name in ('RWND_ZERO', 'PROBE', 'RWND_OPEN'):
            self.assertIn(name, sender_names)
        zeros = [e.time for e in of_name(result.sender_events, 'RWND_ZERO')]
        opens = [e.time for e in of_name(result.sender_events, 'RWND_OPEN')]
        probes = of_name(result.sender_events, 'PROBE')
        self.assertTrue(all(p.state['rwnd'] == 0 and p.state['in_flight'] == 0 for p in probes))
        closed_at = max(t for t in zeros if t <= probes[0].time)
        self.assertGreaterEqual(probes[0].time - closed_at, 0.5 - 1e-9)
        for earlier, later in zip(probes, probes[1:]):
            if not any(earlier.time < t < later.time for t in opens):
                self.assertAlmostEqual(later.time - earlier.time, 0.5)
        last_probe = max(i for i, name in enumerate(sender_names) if name == 'PROBE')
        self.assertIn('SEND', sender_names[last_probe:])
        windows = [e.details['window'] for e in of_name(result.receiver_events, 'ACK_SENT')]
        self.assertIn(0, windows)
        self.assertIn('OUT_OF_WINDOW', names(result.receiver_events))

    def test_lost_window_update(self):
        """The ACK that reopens the window is lost; a later probe reopens it."""
        data = data_of(65536)
        seen = []

        def reopening(packet, now):
            """Pick the first ACK with a window after an ACK with window 0."""
            reopens = bool(seen) and seen[-1] == 0 and packet.window > 0
            seen.append(packet.window)
            return reopens

        rule, dropped = first_rule(reopening)
        result = self.run_checked(data, mss=536, rcvbuf=4096, reader=self.slow_reader(),
                                  ack_link=Link(0.01, drop_if=rule))
        self.assertEqual(len(dropped), 1)
        drop_time, ack = dropped[0]
        self.assertEqual(ack.reason, 'window_update')
        opens = of_name(result.sender_events, 'RWND_OPEN')
        self.assertGreater(opens[0].time, drop_time)
        later_probes = [p for p in of_name(result.sender_events, 'PROBE')
                        if drop_time < p.time < opens[0].time]
        self.assertTrue(later_probes)


class FinTransferTest(TransferTestCase):
    """The FIN after the data, retransmitted until ACKed."""

    def test_fin_after_data(self):
        """The FIN follows the last data byte and is received and ACKed."""
        data = data_of(50000)
        result = self.run_checked(data, close=True)
        sender_names = names(result.sender_events)
        self.assertEqual(sender_names.count('FIN_SENT'), 1)
        fin = of_name(result.sender_events, 'FIN_SENT')[0]
        self.assertEqual(fin.details['seq'], ISS + len(data))
        self.assertGreater(sender_names.index('FIN_SENT'),
                           len(sender_names) - 1 - sender_names[::-1].index('SEND'))
        self.assertTrue(result.sender.fin_acked())
        self.assertTrue(result.receiver.fin_received)
        self.assertEqual(result.receiver.rcv_nxt, ISS + len(data) + 1)
        received = of_name(result.receiver_events, 'FIN_RECEIVED')
        self.assertEqual([e.details['seq'] for e in received], [ISS + len(data)])

    def test_lost_fin_is_resent(self):
        """A lost FIN goes out again on the timer."""
        data = data_of(50000)
        rule, dropped = first_rule(lambda packet, now: packet.fin)
        result = self.run_checked(data, close=True, data_link=Link(0.01, drop_if=rule))
        self.assertEqual(len(dropped), 1)
        timeouts = of_name(result.sender_events, 'TIMEOUT')
        self.assertEqual(len(timeouts), 1)
        self.assertEqual(timeouts[0].details, {'seq': ISS + len(data), 'len': 0,
                                               'rto': timeouts[0].details['rto']})
        self.assertTrue(result.sender.fin_acked())
        self.assertTrue(result.receiver.fin_received)


class FailureTest(unittest.TestCase):
    """A transfer that cannot finish stops with a failure."""

    def test_dead_link_gives_up(self):
        """With every segment lost the sender gives up."""
        with self.assertRaises(SimulationFailed) as caught:
            run_transfer(data_of(5000), data_link=Link(0.01, loss=1.0))
        self.assertIn('gave up', str(caught.exception))

    def test_time_limit(self):
        """The run stops at the virtual time limit."""
        with self.assertRaises(SimulationFailed) as caught:
            run_transfer(data_of(5000), data_link=Link(0.01, loss=1.0), time_limit=5.0)
        self.assertIn('time limit', str(caught.exception))


if __name__ == '__main__':
    unittest.main()

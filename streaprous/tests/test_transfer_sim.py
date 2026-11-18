"""
Whole transfers between the TCP sender and receiver in virtual time.

Each test runs one transfer over the simulated links of ``tests/sim.py``
and checks that the application read exactly the bytes that were sent,
that every SEND kept in flight at most min(cwnd, rwnd) (slides 3-122,
3-94) and was at most one MSS long, and that the events of the mechanism
under test show up.
"""

import hashlib
import unittest

from tests.sim import Link, Reader, Simulation, SimulationFailed, run_transfer


ISS = 1000
MSS = 1460


def data_of(size, tag=b"sim"):
    """
    Make deterministic data in which no short block repeats.

    Args:
        size: Number of bytes.
        tag: Prefix that makes different blocks of data differ.

    Returns:
        bytes: ``size`` bytes built from SHA-256 blocks.
    """
    out = bytearray()
    block = 0
    while len(out) < size:
        out.extend(hashlib.sha256(tag + b":" + str(block).encode()).digest())
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


def resends(result):
    """
    List the resent segments of a run.

    Args:
        result: Result of run_transfer.

    Returns:
        list: TIMEOUT and FAST_RETRANSMIT events in order.
    """
    return [e for e in result.sender_events if e.name in ('TIMEOUT', 'FAST_RETRANSMIT')]


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

    def check_run(self, result, mss):
        """
        Check delivery and the window limit of a finished run.

        Args:
            result: Result of run_transfer.
            mss: The MSS of the run.
        """
        self.assertEqual(len(result.delivered), len(result.sent))
        self.assertTrue(result.delivered == result.sent,
                        'delivered bytes differ from sent bytes')
        self.assertTrue(result.sender.data_acked())
        for event in of_name(result.sender_events, 'SEND'):
            state = event.state
            self.assertLessEqual(state['in_flight'], min(state['cwnd'], state['rwnd']),
                                 'window exceeded at %.3f: %r' % (event.time, event))
            self.assertLessEqual(event.details['len'], mss)

    def run_checked(self, data, **options):
        """
        Run a transfer and check delivery and the window limit.

        Args:
            data: Bytes written at t = 0.
            options: Keyword arguments for run_transfer.

        Returns:
            Result: The finished run.
        """
        result = run_transfer(data, **options)
        self.check_run(result, options.get('mss', MSS))
        return result


class CleanTransferTest(TransferTestCase):
    """A loss-free link and random loss: slow start, then congestion avoidance (3-125)."""

    def test_clean_transfer_slow_start_then_avoidance(self):
        """Slide 3-125: 200 KB arrive with no resend; slow start, then congestion avoidance."""
        data = data_of(200 * 1024)
        result = self.run_checked(data)
        sender_names = names(result.sender_events)
        for name in ('TIMEOUT', 'FAST_RETRANSMIT', 'DUPACK'):
            self.assertNotIn(name, sender_names)
        self.assertEqual(state_changes(result), [('SLOW_START', 'CONGESTION_AVOIDANCE')])
        sends = of_name(result.sender_events, 'SEND')
        self.assertEqual(sends[0].state['state'], 'SLOW_START')
        self.assertEqual(sends[0].state['cwnd'], MSS)
        self.assertEqual(sends[-1].state['state'], 'CONGESTION_AVOIDANCE')
        self.assertEqual(sum(e.details['len'] for e in sends), len(data))
        self.assertNotIn('OUT_OF_ORDER', names(result.receiver_events))
        self.assertGreater(result.sender.reno.cwnd, 64 * 1024)

    def test_random_data_loss(self):
        """Slides 3-84, 3-88: seeded random loss on the data link still delivers every byte."""
        data = data_of(150000)
        link = Link(0.01, loss=0.05, seed=11)
        result = self.run_checked(data, data_link=link)
        self.assertTrue(link.dropped)
        self.assertTrue(resends(result))

    def test_same_seed_same_run(self):
        """Two runs with the same seeds give the same events and time."""
        data = data_of(60000)
        runs = []
        for _ in range(2):
            link = Link(0.01, loss=0.05, duplicate=0.05, reorder=0.2, seed=4)
            runs.append(self.run_checked(data, data_link=link))
        self.assertEqual(names(runs[0].sender_events), names(runs[1].sender_events))
        self.assertEqual(runs[0].time, runs[1].time)


class TimeoutScenarioTest(TransferTestCase):
    """The timeout scenarios of slides 3-84, 3-86 and 3-87, with 3-86's numbers."""

    def test_early_drop_times_out(self):
        """Flaw 1, slide 3-84: the first segment is lost, so only the timer can resend it."""
        data = data_of(100000)
        result = self.run_checked(data, data_link=Link(0.01, drop_new={1}))
        timeouts = of_name(result.sender_events, 'TIMEOUT')
        self.assertEqual(len(timeouts), 1)
        self.assertEqual(timeouts[0].details['seq'], ISS)
        self.assertEqual(timeouts[0].time, 1.0)
        self.assertEqual(timeouts[0].details['interval'], 1.0)
        self.assertEqual(timeouts[0].state['cwnd'], MSS)
        self.assertEqual(timeouts[0].state['ssthresh'], MSS // 2)
        self.assertNotIn('FAST_RETRANSMIT', names(result.sender_events))

    def test_lost_ack_scenario(self):
        """Flaw 2, slide 3-86 left: ACK=100 is lost, Seq=92 is resent, B ACKs the copy."""
        rule, dropped = first_rule(lambda packet, now: True)
        result = self.run_checked(data_of(8), initial_seq_num=92,
                                  ack_link=Link(0.01, drop_if=rule))
        self.assertEqual(dropped[0][1].ack, 100)
        timeouts = of_name(result.sender_events, 'TIMEOUT')
        self.assertEqual([(e.details['seq'], e.details['len']) for e in timeouts], [(92, 8)])
        copies = of_name(result.receiver_events, 'DUP_SEGMENT')
        self.assertEqual([(e.details['seq'], e.details['len']) for e in copies], [(92, 8)])
        reasons = [e.details['reason'] for e in of_name(result.receiver_events, 'ACK_SENT')]
        self.assertEqual(reasons, ['delayed', 'old_segment'])
        self.assertEqual(of_name(result.receiver_events, 'RECV')[0].details['len'], 8)
        self.assertEqual(len(result.delivered), 8)
        acks = of_name(result.sender_events, 'ACK')
        self.assertEqual([e.details['ack'] for e in acks], [100])
        self.assertGreater(acks[0].time, timeouts[0].time)
        self.assertEqual(result.sender.send_base, 100)

    def test_premature_timeout_scenario(self):
        """Flaw 3, slide 3-86 right: both ACKs are late, Seq=92 is resent, ACK=120 covers both.

        The one resend is the segment that caused the timeout as it was first
        sent: Seq=92 with 8 bytes, as slide 3-86 draws it. B already has it.
        """
        first, second = data_of(8, b"a"), data_of(20, b"b")

        def late(packet, now):
            """Hold back every ACK sent before t = 0.5 past the 1 s TimeoutInterval."""
            return 1.0 if now < 0.5 else 0

        ack_link = Link(0.01, delay_if=late)
        result = self.run_checked(first, writes=[(0.2, second)], initial_seq_num=92,
                                  ack_link=ack_link)
        self.assertEqual([packet.ack for _, packet, _ in ack_link.delayed], [100, 120])
        first_ack_arrives = ack_link.delayed[0][0] + 0.01 + 1.0
        timeouts = of_name(result.sender_events, 'TIMEOUT')
        self.assertEqual([(e.details['seq'], e.details['len']) for e in timeouts], [(92, 8)])
        self.assertEqual(timeouts[0].state['next_seq_num'], 120)
        self.assertEqual(timeouts[0].time, 1.0)
        self.assertLess(timeouts[0].time, first_ack_arrives)
        self.assertEqual(len(resends(result)), 1)
        copies = of_name(result.receiver_events, 'DUP_SEGMENT')
        self.assertEqual([(e.details['seq'], e.details['len']) for e in copies], [(92, 8)])
        acks = of_name(result.sender_events, 'ACK')
        self.assertEqual([e.details['ack'] for e in acks], [120])
        self.assertGreater(acks[0].time, timeouts[0].time)
        self.assertNotIn('RTT', names(result.sender_events))
        self.assertEqual(result.sender.send_base, 120)

    def test_cumulative_ack_scenario(self):
        """Flaw 4, slide 3-87: ACK=100 is lost, ACK=120 covers it, and nothing is resent."""
        first, second = data_of(8, b"a"), data_of(20, b"b")
        rule, dropped = first_rule(lambda packet, now: True)
        result = self.run_checked(first, writes=[(0.2, second)], initial_seq_num=92,
                                  ack_link=Link(0.01, drop_if=rule))
        self.assertEqual(dropped[0][1].ack, 100)
        self.assertEqual(resends(result), [])
        acks = of_name(result.sender_events, 'ACK')
        self.assertEqual([e.details['ack'] for e in acks], [120])
        self.assertLess(acks[0].time, 1.0)
        self.assertNotIn('DUP_SEGMENT', names(result.receiver_events))


class LossInWindowTest(TransferTestCase):
    """Losses inside a large window: fast retransmit and the one-segment timeout (3-84, 3-88)."""

    def test_several_losses_in_one_window(self):
        """Flaw 5, slides 3-84, 3-88: each lost segment is resent once, the timer one at a time.

        Known limit: with no go-back, every lost segment after the first
        costs its own timeout.
        """
        data = data_of(200000)
        link = Link(0.01, drop_new={30, 31, 32, 33, 34})
        result = self.run_checked(data, data_link=link)
        lost = [packet.seq for _, packet, _ in link.dropped]
        self.assertEqual(len(lost), 5)
        resent = sorted(e.details['seq'] for e in resends(result))
        self.assertEqual(resent, lost)
        timeouts = of_name(result.sender_events, 'TIMEOUT')
        for event in timeouts:
            self.assertEqual(event.details['seq'], event.state['send_base'])
            self.assertEqual(event.state['cwnd'], MSS)
        self.assertGreaterEqual(len(timeouts), len(lost) - 1)

    def test_resend_after_small_window(self):
        """Flaw 25, slide 3-84: rwnd 1000 below one MSS; the timeout resends the same 1000 bytes."""
        data = data_of(20000)
        rule, dropped = first_rule(lambda packet, now: True)
        result = self.run_checked(data, mss=MSS, rcv_buffer=1000,
                                  ack_link=Link(0.01, drop_if=rule))
        self.assertEqual(len(dropped), 1)
        first = of_name(result.sender_events, 'SEND')[0]
        timeouts = of_name(result.sender_events, 'TIMEOUT')
        self.assertEqual(first.details['len'], 1000)
        self.assertEqual(len(timeouts), 1)
        self.assertEqual((timeouts[0].details['seq'], timeouts[0].details['len']), (ISS, 1000))
        self.assertEqual(timeouts[0].state['next_seq_num'], ISS + 1000)

    def test_drop_at_large_window_fast_retransmit(self):
        """Flaw 1, slides 3-88, 3-125: one loss in a large window, three duplicate ACKs."""
        data = data_of(200000)
        link = Link(0.01, drop_new={40})
        result = self.run_checked(data, data_link=link)
        lost_seq = link.dropped[0][1].seq
        fast = of_name(result.sender_events, 'FAST_RETRANSMIT')
        self.assertEqual(len(fast), 1)
        self.assertEqual(fast[0].details['seq'], lost_seq)
        self.assertEqual(fast[0].state['state'], 'FAST_RECOVERY')
        self.assertEqual(fast[0].state['cwnd'], fast[0].state['ssthresh'] + 3 * MSS)
        self.assertNotIn('TIMEOUT', names(result.sender_events))
        dupacks = of_name(result.sender_events, 'DUPACK')
        self.assertEqual([e.details['count'] for e in dupacks[:3]], [1, 2, 3])
        self.assertTrue(all(e.details['ack'] == lost_seq for e in dupacks))
        self.assertIn(('SLOW_START', 'FAST_RECOVERY'), state_changes(result))
        self.assertEqual(state_changes(result)[-1], ('FAST_RECOVERY', 'CONGESTION_AVOIDANCE'))
        out_of_order = of_name(result.receiver_events, 'OUT_OF_ORDER')
        self.assertTrue(out_of_order)
        self.assertTrue(all(e.details['expected'] == lost_seq for e in out_of_order))

    def test_lone_segment_no_premature_timeout(self):
        """Flaw 28, slide 3-85: a lone segment waits 100 ms for its delayed ACK; no TIMEOUT.

        First one segment alone, then the same after 300 segments over a
        1 ms link have pulled TimeoutInterval down to its 0.2 s floor.
        """
        alone = self.run_checked(data_of(1000), delay=0.001)
        self.assertNotIn('TIMEOUT', names(alone.sender_events))
        reasons = [e.details['reason'] for e in of_name(alone.receiver_events, 'ACK_SENT')]
        self.assertEqual(reasons, ['delayed'])
        result = self.run_checked(data_of(300 * MSS), writes=[(2.0, data_of(1000, b"x"))],
                                  delay=0.001)
        self.assertNotIn('TIMEOUT', names(result.sender_events))
        last_send = of_name(result.sender_events, 'SEND')[-1]
        self.assertEqual((last_send.time, last_send.details['len']), (2.0, 1000))
        samples = of_name(result.sender_events, 'RTT')
        self.assertLess(samples[-2].time, 2.0)
        self.assertAlmostEqual(samples[-2].details['timeout'], 0.2)
        self.assertGreater(samples[-1].time, 2.0)
        self.assertAlmostEqual(samples[-1].details['sample'], 0.102)


class ReceiverTransferTest(TransferTestCase):
    """Reordered, repeated and unACKed segments (slides 3-85, 3-87)."""

    def test_reorder_and_duplicate(self):
        """Flaws 6 and 7, slide 3-85: out-of-order segments wait for the gap; copies go."""
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
        """Flaw 4, slide 3-87: 10 % of the ACKs are lost; cumulative ACKs cover for them."""
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

    def test_slow_reader_zero_window(self):
        """Flaw 8, slide 3-94: rwnd reaches 0, the sender probes every 0.5 s, then resumes."""
        data = data_of(65536)
        result = self.run_checked(data, mss=536, rcv_buffer=4096, reader=self.slow_reader())
        sender_names = names(result.sender_events)
        for name in ('RWND_ZERO', 'PROBE', 'RWND_OPEN'):
            self.assertIn(name, sender_names)
        zero = of_name(result.sender_events, 'RWND_ZERO')[0]
        probes = of_name(result.sender_events, 'PROBE')
        self.assertTrue(all(p.state['rwnd'] == 0 and p.state['in_flight'] == 0 for p in probes))
        self.assertGreaterEqual(probes[0].time - zero.time, 0.5 - 1e-9)
        first_open = of_name(result.sender_events, 'RWND_OPEN')[0]
        spell = [p.time for p in probes if p.time < first_open.time]
        for earlier, later in zip(spell, spell[1:]):
            self.assertAlmostEqual(later - earlier, 0.5)
        last_probe = max(i for i, name in enumerate(sender_names) if name == 'PROBE')
        self.assertIn('SEND', sender_names[last_probe:])
        windows = [e.details['rwnd'] for e in of_name(result.receiver_events, 'ACK_SENT')]
        self.assertIn(0, windows)
        self.assertIn('OUT_OF_WINDOW', names(result.receiver_events))

    def test_lost_window_update(self):
        """Flaw 8, slide 3-94: the ACK that reopens the window is lost; a probe reopens it."""
        data = data_of(65536)
        seen = []

        def reopening(packet, now):
            """Pick the first ACK with a window after an ACK with window 0."""
            reopens = bool(seen) and seen[-1] == 0 and packet.window > 0
            seen.append(packet.window)
            return reopens

        rule, dropped = first_rule(reopening)
        result = self.run_checked(data, mss=536, rcv_buffer=4096, reader=self.slow_reader(),
                                  ack_link=Link(0.01, drop_if=rule))
        self.assertEqual(len(dropped), 1)
        drop_time, ack = dropped[0]
        self.assertEqual(ack.reason, 'window_update')
        opens = of_name(result.sender_events, 'RWND_OPEN')
        self.assertGreater(opens[0].time, drop_time)
        later_probes = [p for p in of_name(result.sender_events, 'PROBE')
                        if drop_time < p.time < opens[0].time]
        self.assertTrue(later_probes)

    def test_tiny_receive_buffer(self):
        """Flaw 9, slides 3-94, 3-122: RcvBuffer 500 below one MSS; segments of 500 bytes."""
        data = data_of(20000)
        result = self.run_checked(data, rcv_buffer=500)
        sends = of_name(result.sender_events, 'SEND')
        self.assertTrue(all(e.details['len'] <= 500 for e in sends))
        self.assertEqual(sends[0].details['len'], 500)
        self.assertEqual(sum(e.details['len'] for e in sends), len(data))
        self.assertEqual(resends(result), [])


class FinTransferTest(TransferTestCase):
    """The FIN after the data, resent by the timer until ACKed (slides 3-103, 3-84)."""

    def test_fin_after_data(self):
        """Slides 3-103, 3-104: the FIN follows the last data byte and takes one seq #."""
        data = data_of(50000)
        result = self.run_checked(data, close=True)
        sender_names = names(result.sender_events)
        self.assertEqual(sender_names.count('FIN_SENT'), 1)
        fin = of_name(result.sender_events, 'FIN_SENT')[0]
        self.assertEqual(fin.details['seq'], ISS + len(data))
        self.assertGreater(sender_names.index('FIN_SENT'),
                           len(sender_names) - 1 - sender_names[::-1].index('SEND'))
        self.assertTrue(result.sender.fin_acked())
        self.assertEqual(result.sender.send_base, ISS + len(data) + 1)
        self.assertTrue(result.receiver.fin_received)
        self.assertEqual(result.receiver.expected_seq_num, ISS + len(data) + 1)
        received = of_name(result.receiver_events, 'FIN_RECEIVED')
        self.assertEqual([e.details['seq'] for e in received], [ISS + len(data)])

    def test_lost_fin_is_resent(self):
        """Flaw 17, slide 3-84: a lost FIN goes out again when the timer expires."""
        data = data_of(50000)
        rule, dropped = first_rule(lambda packet, now: packet.fin)
        result = self.run_checked(data, close=True, data_link=Link(0.01, drop_if=rule))
        self.assertEqual(len(dropped), 1)
        timeouts = of_name(result.sender_events, 'TIMEOUT')
        self.assertEqual(len(timeouts), 1)
        self.assertEqual(timeouts[0].details, {'seq': ISS + len(data), 'len': 0,
                                               'interval': timeouts[0].details['interval']})
        self.assertAlmostEqual(timeouts[0].time - dropped[0][0],
                               timeouts[0].details['interval'])
        self.assertTrue(result.sender.fin_acked())
        self.assertTrue(result.receiver.fin_received)


class FailureTest(unittest.TestCase):
    """A transfer that cannot finish stops with a failure."""

    def test_dead_link_resends_at_fixed_interval(self):
        """Slide 3-84: with every segment lost the timer resends every 1 s; nothing doubles it."""
        sim = Simulation(data_of(5000), data_link=Link(0.01, loss=1.0), time_limit=10.0)
        with self.assertRaises(SimulationFailed) as caught:
            sim.run()
        self.assertIn('time limit', str(caught.exception))
        timeouts = of_name(sim.sender_events, 'TIMEOUT')
        self.assertEqual([e.time for e in timeouts[:5]], [1.0, 2.0, 3.0, 4.0, 5.0])
        self.assertTrue(all(e.details['seq'] == ISS for e in timeouts))
        self.assertTrue(all(e.details['interval'] == 1.0 for e in timeouts))
        self.assertEqual(len(timeouts), 10)

    def test_time_limit(self):
        """The run stops at the virtual time limit."""
        with self.assertRaises(SimulationFailed) as caught:
            run_transfer(data_of(5000), data_link=Link(0.01, loss=1.0), time_limit=5.0)
        self.assertIn('time limit 5.0 s', str(caught.exception))


if __name__ == '__main__':
    unittest.main()

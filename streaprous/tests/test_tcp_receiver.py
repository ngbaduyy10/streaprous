"""Tests for the TCP receiver: the ACK rules of slide 3-85 and the window of slide 3-94."""

import random
import unittest

from streaprous_socket.tcp_receiver import DELAYED_ACK_WAIT, TcpReceiver
from tests.slide_rules import assert_slide_rules

ISN = 1000
BUF = 4096

RECEIVER_RULES = [
    ('TcpReceiver.on_segment', '3-85', 'ACK generation'),
    ('TcpReceiver.is_old_segment', '3-86', 'lost ACK scenario'),
    ('TcpReceiver.trim_to_window', '3-94', 'guarantees receive buffer will not overflow'),
    ('TcpReceiver.store_out_of_order', '3-85',
     'immediately send duplicate ACK, indicating seq. # of next expected byte'),
    ('TcpReceiver.store_out_of_order', '3-79', 'up to implementor'),
    ('TcpReceiver.deliver_in_order', '3-79', 'seq # of next byte expected from other side'),
    ('TcpReceiver.choose_ack', '3-85', 'Wait up to 500ms for next segment'),
    ('TcpReceiver.choose_ack', '3-85', 'immediately send single cumulative ACK'),
    ('TcpReceiver.choose_ack', '3-85', 'provided that segment starts at lower end of gap'),
    ('TcpReceiver.on_tick', '3-85', 'If no next segment, send ACK'),
    ('TcpReceiver.rwnd', '3-94', 'free buffer space in rwnd field'),
    ('TcpReceiver.read', '3-90', 'Application removing data from TCP socket buffers'),
    ('TcpReceiver.window_update_due', 'extra', 'a quarter of RcvBuffer'),
]


def make(rcv_buffer=BUF):
    """Builds a receiver that records its log events.

    Args:
         rcv_buffer  RcvBuffer in bytes

    Returns:
         (receiver, list of (event, details))
    """
    events = []
    receiver = TcpReceiver(ISN, rcv_buffer,
                           log=lambda event, **details: events.append((event, details)))
    return receiver, events


def names(events):
    """Lists the event names in order.

    Args:
         events  list of (event, details)
    """
    return [event for event, details in events]


class AckRuleTests(unittest.TestCase):
    """The four rows of slide 3-85."""

    def test_row1_in_order_segment_delays_ack(self):
        """Row 1: an in-order segment with nothing pending waits for the delayed ACK."""
        rx, events = make()
        self.assertIsNone(rx.on_segment(ISN, b'a' * 100, False, 1.0))
        self.assertTrue(rx.ack_pending)
        self.assertEqual(rx.ack_deadline, 1.0 + DELAYED_ACK_WAIT)
        self.assertEqual(rx.expected_seq_num, ISN + 100)
        self.assertEqual(events, [('RECV', {'seq': ISN, 'len': 100})])

    def test_row1_delayed_ack_fires_on_tick(self):
        """Row 1: if no next segment comes, the ACK goes out after DELAYED_ACK_WAIT."""
        self.assertEqual(DELAYED_ACK_WAIT, 0.1)
        rx, _ = make()
        rx.on_segment(ISN, b'a' * 100, False, 1.0)
        self.assertIsNone(rx.on_tick(1.0))
        self.assertIsNone(rx.on_tick(1.05))
        self.assertEqual(rx.on_tick(1.1), 'delayed')
        self.assertEqual(rx.on_tick(1.2), 'delayed')

    def test_row2_second_segment_gets_cumulative_ack(self):
        """Row 2: a second in-order segment while an ACK is pending is ACKed at once."""
        rx, _ = make()
        rx.on_segment(ISN, b'a' * 100, False, 1.0)
        self.assertEqual(rx.on_segment(ISN + 100, b'b' * 100, False, 1.01), 'cumulative')
        self.assertEqual(rx.expected_seq_num, ISN + 200)

    def test_row3_out_of_order_gets_duplicate_ack(self):
        """Row 3 and flaw scenario 6: a segment above a gap is kept and gets a duplicate ACK."""
        rx, events = make()
        self.assertEqual(rx.on_segment(ISN + 100, b'b' * 100, False, 1.0), 'duplicate')
        self.assertEqual(rx.expected_seq_num, ISN)
        self.assertEqual(rx.out_of_order, {ISN + 100: b'b' * 100})
        self.assertEqual(rx.rwnd(), BUF)
        self.assertEqual(rx.read(), b'')
        self.assertEqual(events[-1], ('OUT_OF_ORDER',
                                      {'seq': ISN + 100, 'len': 100, 'expected': ISN}))

    def test_row3_segment_inside_gap_not_at_lower_end(self):
        """Row 3, not row 4: a segment inside the gap that misses its lower end."""
        rx, _ = make()
        rx.on_segment(ISN + 200, b'c' * 100, False, 1.0)
        self.assertEqual(rx.on_segment(ISN + 100, b'b' * 100, False, 1.0), 'duplicate')
        self.assertEqual(rx.expected_seq_num, ISN)
        self.assertEqual(sorted(rx.out_of_order), [ISN + 100, ISN + 200])

    def test_row4_gap_fill_gets_immediate_ack(self):
        """Row 4 and flaw scenario 6: the segment at the lower end of the gap is ACKed at once."""
        rx, _ = make()
        rx.on_segment(ISN + 200, b'c' * 100, False, 1.0)
        rx.on_segment(ISN + 100, b'b' * 100, False, 1.0)
        self.assertEqual(rx.on_segment(ISN, b'a' * 100, False, 1.0), 'gap_filled')
        self.assertEqual(rx.expected_seq_num, ISN + 300)
        self.assertEqual(rx.out_of_order, {})
        self.assertEqual(rx.read(), b'a' * 100 + b'b' * 100 + b'c' * 100)
        self.assertFalse(rx.ack_pending)


class DeliveryTests(unittest.TestCase):
    """Each byte delivered once, in order (slides 3-79 and 3-86)."""

    def test_duplicate_segment_acked_and_discarded(self):
        """Flaw scenario 7 (3-86 lost ACK): a copy is ACKed again and not delivered twice."""
        rx, events = make()
        rx.on_segment(ISN, b'a' * 100, False, 1.0)
        self.assertEqual(rx.on_segment(ISN, b'a' * 100, False, 1.1), 'old_segment')
        self.assertEqual(rx.on_segment(ISN + 10, b'a' * 20, False, 1.1), 'old_segment')
        self.assertEqual(names(events).count('DUP_SEGMENT'), 2)
        self.assertEqual(events[-1], ('DUP_SEGMENT', {'seq': ISN + 10, 'len': 20}))
        self.assertEqual(rx.read(), b'a' * 100)

    def test_partly_old_segment_trimmed(self):
        """Flaw scenario 7: only the new tail of a partly old segment is delivered."""
        rx, _ = make()
        rx.on_segment(ISN, b'0123456789', False, 1.0)
        self.assertEqual(rx.on_segment(ISN + 5, b'56789ABCDE', False, 1.0), 'cumulative')
        self.assertEqual(rx.expected_seq_num, ISN + 15)
        self.assertEqual(rx.read(), b'0123456789ABCDE')

    def test_overlapping_out_of_order_segments(self):
        """Flaw scenario 6: overlapping stored segments give each byte once."""
        data = bytes(range(256)) * 2
        rx, _ = make()
        rx.on_segment(ISN + 300, data[300:400], False, 1.0)
        rx.on_segment(ISN + 250, data[250:350], False, 1.0)
        rx.on_segment(ISN + 100, data[100:200], False, 1.0)
        rx.on_segment(ISN + 100, data[100:150], False, 1.0)
        self.assertEqual(rx.out_of_order[ISN + 100], data[100:200])
        rx.on_segment(ISN + 150, data[150:260], False, 1.0)
        rx.on_segment(ISN + 50, data[50:120], False, 1.0)
        self.assertEqual(rx.on_segment(ISN, data[0:60], False, 1.0), 'gap_filled')
        self.assertEqual(rx.expected_seq_num, ISN + 400)
        self.assertEqual(rx.out_of_order, {})
        self.assertEqual(rx.read(), data[:400])


class WindowTests(unittest.TestCase):
    """The receive window of slide 3-94."""

    def test_segment_beyond_rwnd_trimmed(self):
        """Flaw scenario 19: data past the free space is cut and its FIN dropped."""
        rx, events = make(rcv_buffer=1000)
        rx.on_segment(ISN, b'a' * 600, False, 1.0)
        self.assertEqual(rx.on_segment(ISN + 600, b'b' * 600, True, 1.0), 'cumulative')
        self.assertIn(('OUT_OF_WINDOW', {'seq': ISN + 600, 'len': 600, 'rwnd': 400}), events)
        self.assertEqual(rx.expected_seq_num, ISN + 1000)
        self.assertFalse(rx.fin_received)
        self.assertEqual(rx.rwnd(), 0)
        self.assertEqual(rx.on_segment(ISN + 900, b'z' * 300, False, 1.0), 'out_of_window')
        self.assertEqual(rx.read(), b'a' * 600 + b'b' * 400)

    def test_zero_window_probe_answered(self):
        """3-94 with rwnd 0: a one-byte probe is refused but still gets an ACK."""
        rx, events = make(rcv_buffer=100)
        rx.on_segment(ISN, b'a' * 100, False, 1.0)
        self.assertEqual(rx.on_segment(ISN + 100, b'b', False, 1.2), 'out_of_window')
        self.assertEqual(rx.expected_seq_num, ISN + 100)
        self.assertEqual(names(events)[-1], 'OUT_OF_WINDOW')
        rx.read()
        rx.ack_sent(rx.rwnd())
        self.assertIsNone(rx.on_segment(ISN + 100, b'b', False, 1.3))
        self.assertEqual(rx.read(), b'b')

    def test_pure_segment_below_expected_answered(self):
        """3-85: an empty segment below the expected seq # is a keepalive and gets an ACK."""
        rx, _ = make()
        rx.on_segment(ISN, b'a' * 10, False, 1.0)
        self.assertEqual(rx.on_segment(ISN + 9, b'', False, 1.0), 'probe')
        self.assertIsNone(rx.on_segment(ISN + 10, b'', False, 1.0))
        self.assertEqual(rx.expected_seq_num, ISN + 10)

    def test_rwnd_is_free_buffer_space(self):
        """3-94: rwnd = RcvBuffer minus buffered data; out-of-order bytes do not count."""
        rx, _ = make()
        self.assertEqual(rx.rcv_buffer, 4096)
        self.assertEqual(rx.rwnd(), BUF)
        rx.on_segment(ISN, b'x' * 1000, False, 1.0)
        self.assertEqual(rx.rwnd(), BUF - 1000)
        rx.on_segment(ISN + 2000, b'y' * 500, False, 1.0)
        self.assertEqual(rx.rwnd(), BUF - 1000)
        self.assertEqual(rx.rwnd(), rx.rcv_buffer - len(rx.buffered_data))

    def test_read_opens_window(self):
        """3-90: the application removing data frees space in rwnd."""
        rx, events = make()
        rx.on_segment(ISN, b'abcdef', False, 1.0)
        self.assertEqual(rx.read(4), b'abcd')
        self.assertEqual(rx.rwnd(), BUF - 2)
        self.assertEqual(rx.read(), b'ef')
        self.assertEqual(rx.rwnd(), BUF)
        self.assertEqual(rx.read(), b'')
        self.assertFalse(rx.readable())
        self.assertEqual([d for e, d in events if e == 'APP_READ'], [{'bytes': 4}, {'bytes': 2}])

    def test_window_update_due_at_quarter(self):
        """Extra: one window update when rwnd grows from below a quarter to a quarter."""
        rx, _ = make(rcv_buffer=1000)
        self.assertFalse(rx.window_update_due())
        rx.on_segment(ISN, b'a' * 900, False, 1.0)
        rx.ack_sent(rx.rwnd())
        self.assertEqual(rx.last_rwnd, 100)
        rx.read(100)
        self.assertFalse(rx.window_update_due())
        rx.read(50)
        self.assertTrue(rx.window_update_due())
        rx.ack_sent(rx.rwnd())
        self.assertFalse(rx.window_update_due())

    def test_no_window_update_when_window_was_large(self):
        """Extra: after a large advertised window a read needs no window update."""
        rx, _ = make(rcv_buffer=1000)
        rx.on_segment(ISN, b'a' * 500, False, 1.0)
        rx.ack_sent(rx.rwnd())
        rx.read()
        self.assertFalse(rx.window_update_due())

    def test_ack_sent_clears_pending(self):
        """Flaw scenario 26: our data segment carries the pending ACK, so the delay ends."""
        rx, _ = make()
        rx.on_segment(ISN, b'a' * 100, False, 1.0)
        rx.ack_sent(rx.rwnd())
        self.assertFalse(rx.ack_pending)
        self.assertIsNone(rx.ack_deadline)
        self.assertIsNone(rx.on_tick(5.0))
        self.assertEqual(rx.last_rwnd, BUF - 100)
        self.assertIsNone(rx.on_segment(ISN + 100, b'b' * 10, False, 2.0))


class FinTests(unittest.TestCase):
    """The client's FIN (flaw scenario 17)."""

    def test_fin_in_order(self):
        """Flaw scenario 17: an in-order bare FIN takes one seq #, even with rwnd 0."""
        rx, events = make(rcv_buffer=5)
        rx.on_segment(ISN, b'hello', False, 1.0)
        self.assertEqual(rx.on_segment(ISN + 5, b'', True, 1.0), 'fin')
        self.assertTrue(rx.fin_received)
        self.assertEqual(rx.expected_seq_num, ISN + 6)
        self.assertEqual(events[-1], ('FIN_RECEIVED', {'seq': ISN + 5}))
        self.assertEqual(rx.read(), b'hello')
        self.assertTrue(rx.readable())

    def test_fin_with_data(self):
        """Flaw scenario 17: a FIN on a data segment comes after its bytes."""
        rx, events = make()
        self.assertEqual(rx.on_segment(ISN, b'hello', True, 1.0), 'fin')
        self.assertEqual(rx.expected_seq_num, ISN + 6)
        self.assertEqual(names(events), ['RECV', 'FIN_RECEIVED'])
        self.assertEqual(rx.read(), b'hello')

    def test_fin_out_of_order_waits_for_resend(self):
        """Flaw scenario 17: a FIN above a gap is not kept; the client's resend brings it."""
        rx, _ = make()
        self.assertEqual(rx.on_segment(ISN + 5, b'world', True, 1.0), 'duplicate')
        self.assertFalse(rx.fin_received)
        self.assertEqual(rx.on_segment(ISN, b'hello', False, 1.1), 'gap_filled')
        self.assertFalse(rx.fin_received)
        self.assertEqual(rx.expected_seq_num, ISN + 10)
        self.assertEqual(rx.on_segment(ISN + 5, b'world', True, 1.5), 'fin')
        self.assertEqual(rx.expected_seq_num, ISN + 11)
        self.assertEqual(rx.read(), b'helloworld')

    def test_duplicate_fin_acked_again(self):
        """Flaw scenario 17 with 3-86: a FIN seen again is ACKed and changes nothing."""
        rx, events = make()
        rx.on_segment(ISN, b'hello', True, 1.0)
        self.assertEqual(rx.on_segment(ISN, b'hello', True, 1.5), 'old_segment')
        self.assertEqual(rx.on_segment(ISN + 5, b'', True, 1.6), 'old_segment')
        self.assertEqual(rx.expected_seq_num, ISN + 6)
        self.assertEqual(names(events).count('FIN_RECEIVED'), 1)
        self.assertEqual(rx.read(), b'hello')


class RandomStreamTests(unittest.TestCase):
    """Slides 3-79 and 3-85 together: any arrival order gives the exact stream."""

    def round_of_segments(self, rng, data):
        """Builds one shuffled send round: some segments missing, some repeated or overlapping.

        Args:
             rng   the seeded random.Random
             data  the whole stream

        Returns:
             list of (seq, data, fin)
        """
        cuts = sorted(rng.sample(range(1, len(data)), 60)) + [len(data)]
        segments, start = [], 0
        for end in cuts:
            if rng.random() > 0.3:
                segments.append((ISN + start, data[start:end], end == len(data)))
            start = end
        for _ in range(20):
            start = rng.randrange(len(data))
            piece = data[start:start + rng.randint(1, 1500)]
            segments.append((ISN + start, piece, start + len(piece) == len(data)))
        segments.extend(rng.sample(segments, len(segments) // 4))
        rng.shuffle(segments)
        return segments

    def test_random_stream_delivered_once_in_order(self):
        """Flaw scenarios 6, 7 and 19: overlaps, copies and resends give each byte once, in order.

        A seeded shuffle; each round leaves some segments out, and a later round resends them.
        """
        rng = random.Random(3094)
        data = bytes(rng.getrandbits(8) for _ in range(20000))
        rx, events = make()
        out = bytearray()
        now = 0.0
        for _ in range(200):
            for seq, piece, fin in self.round_of_segments(rng, data):
                now += 0.001
                rx.on_segment(seq, piece, fin, now)
                self.assertLessEqual(len(rx.buffered_data), rx.rcv_buffer)
                if rng.random() < 0.3:
                    out.extend(rx.read(rng.randint(1, 3000)))
            out.extend(rx.read())
            if rx.fin_received:
                break
        self.assertTrue(rx.fin_received)
        self.assertEqual(bytes(out), data)
        self.assertEqual(sum(d['len'] for e, d in events if e == 'RECV'), len(data))
        self.assertEqual(rx.expected_seq_num, ISN + len(data) + 1)


class SlideRuleTests(unittest.TestCase):
    """Slide rules in the receiver's docstrings."""

    def test_slide_rules(self):
        """Every receiver function names its slide and the slide's words."""
        assert_slide_rules(self, 'tcp_receiver', RECEIVER_RULES)


if __name__ == '__main__':
    unittest.main()

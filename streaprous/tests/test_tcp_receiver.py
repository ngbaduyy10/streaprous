"""
Tests for the TCP receiver: ACK rules, out-of-order buffer and window.
"""

import random
import unittest

from streaprous_socket.tcp_receiver import TcpReceiver


ISN = 1000
BUF = 4096
DELAY = 0.1


def make(rcv_nxt=ISN, rcv_buffer=BUF, delayed_ack=DELAY):
    """
    Build a receiver that records its events.

    Args:
        rcv_nxt: First expected sequence number.
        rcv_buffer: RcvBuffer in bytes.
        delayed_ack: Delayed-ACK time in seconds.

    Returns:
        tuple: (receiver, list of (name, details) events).
    """
    events = []
    rx = TcpReceiver(rcv_nxt, rcv_buffer, delayed_ack,
                     on_event=lambda name, **d: events.append((name, d)))
    return rx, events


def names(events):
    """
    List the event names in order.

    Args:
        events: List of (name, details) pairs.

    Returns:
        list: The names.
    """
    return [name for name, _ in events]


class AckRuleTests(unittest.TestCase):
    """The four rows of slide 3-85."""

    def test_row1_in_order_is_delayed(self):
        """An in-order segment with nothing pending waits for the timer."""
        rx, events = make()
        self.assertIsNone(rx.on_segment(ISN, b"a" * 100, False, 1.0))
        self.assertTrue(rx.ack_pending)
        self.assertEqual(rx.next_deadline(), 1.0 + DELAY)
        self.assertIsNone(rx.on_tick(1.0))
        self.assertIsNone(rx.on_tick(1.05))
        self.assertEqual(rx.on_tick(1.2), 'delayed')
        self.assertEqual(rx.rcv_nxt, ISN + 100)
        self.assertIn('RECV', names(events))

    def test_row2_second_in_order_is_cumulative(self):
        """A second in-order segment while an ACK is pending is ACKed at once."""
        rx, _ = make()
        rx.on_segment(ISN, b"a" * 100, False, 1.0)
        self.assertEqual(rx.on_segment(ISN + 100, b"b" * 100, False, 1.01), 'cumulative')
        self.assertEqual(rx.rcv_nxt, ISN + 200)

    def test_row3_gap_gives_duplicate(self):
        """A segment above rcv_nxt is stored and answered with a duplicate ACK."""
        rx, events = make()
        before = rx.rwnd()
        self.assertEqual(rx.on_segment(ISN + 100, b"b" * 100, False, 1.0), 'duplicate')
        self.assertEqual(rx.rcv_nxt, ISN)
        self.assertEqual(rx.out_of_order, {ISN + 100: b"b" * 100})
        self.assertEqual(rx.rwnd(), before)
        self.assertEqual(rx.read(), b"")
        self.assertEqual(events[-1], ('OUT_OF_ORDER',
                                      {'seq': ISN + 100, 'len': 100, 'expected': ISN}))

    def test_row4_gap_filled_delivers_all(self):
        """Filling the gap delivers every stored byte in order."""
        rx, _ = make()
        rx.on_segment(ISN + 200, b"c" * 100, False, 1.0)
        rx.on_segment(ISN + 100, b"b" * 100, False, 1.0)
        self.assertEqual(rx.on_segment(ISN, b"a" * 100, False, 1.0), 'gap_filled')
        self.assertEqual(rx.rcv_nxt, ISN + 300)
        self.assertEqual(rx.out_of_order, {})
        self.assertEqual(rx.read(), b"a" * 100 + b"b" * 100 + b"c" * 100)

    def test_partial_gap_fill_is_gap_filled(self):
        """A segment that fills only part of the gap is also ACKed at once."""
        rx, _ = make()
        rx.on_segment(ISN + 200, b"c" * 100, False, 1.0)
        self.assertEqual(rx.on_segment(ISN, b"a" * 100, False, 1.0), 'gap_filled')
        self.assertEqual(rx.rcv_nxt, ISN + 100)
        self.assertIn(ISN + 200, rx.out_of_order)

    def test_ack_sent_clears_pending(self):
        """Sending any segment with the ACK clears the delayed ACK."""
        rx, _ = make()
        rx.on_segment(ISN, b"a" * 100, False, 1.0)
        rx.ack_sent(rx.rwnd())
        self.assertFalse(rx.ack_pending)
        self.assertIsNone(rx.next_deadline())
        self.assertIsNone(rx.on_tick(5.0))
        self.assertEqual(rx.last_rwnd, BUF - 100)
        # The next in-order segment starts a new delay instead of 'cumulative'
        self.assertIsNone(rx.on_segment(ISN + 100, b"b" * 10, False, 2.0))


class DeliveryTests(unittest.TestCase):
    """Exactly-once delivery with duplicates and overlaps."""

    def test_old_segment(self):
        """A segment fully below rcv_nxt is discarded but answered."""
        rx, events = make()
        rx.on_segment(ISN, b"a" * 100, False, 1.0)
        self.assertEqual(rx.on_segment(ISN, b"a" * 100, False, 1.1), 'old_segment')
        self.assertEqual(rx.on_segment(ISN + 10, b"a" * 20, False, 1.1), 'old_segment')
        self.assertEqual(names(events).count('DUP_SEGMENT'), 2)
        self.assertEqual(rx.read(), b"a" * 100)

    def test_partly_old_segment_trimmed(self):
        """Only the new tail of a partly old segment is delivered."""
        rx, _ = make()
        rx.on_segment(ISN, b"0123456789", False, 1.0)
        rx.on_segment(ISN + 5, b"56789ABCDE", False, 1.0)
        self.assertEqual(rx.rcv_nxt, ISN + 15)
        self.assertEqual(rx.read(), b"0123456789ABCDE")

    def test_overlapping_out_of_order(self):
        """Overlapping stored segments give each byte once."""
        data = bytes(range(256)) * 2
        rx, _ = make()
        rx.on_segment(ISN + 300, data[300:400], False, 1.0)
        rx.on_segment(ISN + 250, data[250:350], False, 1.0)
        rx.on_segment(ISN + 100, data[100:200], False, 1.0)
        rx.on_segment(ISN + 100, data[100:150], False, 1.0)  # shorter copy is ignored
        rx.on_segment(ISN + 150, data[150:260], False, 1.0)
        self.assertEqual(rx.out_of_order[ISN + 100], data[100:200])
        rx.on_segment(ISN + 50, data[50:120], False, 1.0)
        self.assertEqual(rx.on_segment(ISN, data[0:60], False, 1.0), 'gap_filled')
        self.assertEqual(rx.rcv_nxt, ISN + 400)
        self.assertEqual(rx.out_of_order, {})
        self.assertEqual(rx.read(), data[:400])

    def test_read_max_bytes(self):
        """read() returns at most max_bytes and keeps the rest."""
        rx, events = make()
        rx.on_segment(ISN, b"abcdef", False, 1.0)
        self.assertEqual(rx.read(4), b"abcd")
        self.assertEqual(rx.read(), b"ef")
        self.assertEqual(rx.read(), b"")
        self.assertFalse(rx.readable())
        self.assertEqual([d for n, d in events if n == 'APP_READ'],
                         [{'bytes': 4}, {'bytes': 2}])


class WindowTests(unittest.TestCase):
    """Receive window of slide 3-94."""

    def test_window_formula(self):
        """rwnd = RcvBuffer - buffered data (slide 3-94)."""
        rx, _ = make()
        self.assertEqual(rx.rwnd(), BUF)
        rx.on_segment(ISN, b"x" * 1000, False, 1.0)
        self.assertEqual(rx.rwnd(), BUF - 1000)
        rx.on_segment(ISN + 1000, b"x" * 500, False, 1.0)
        self.assertEqual(rx.rwnd(), BUF - 1500)
        self.assertEqual(rx.rwnd(), rx.rcv_buffer - len(rx.buffered_data))
        rx.read(700)
        self.assertEqual(rx.rwnd(), BUF - 800)
        rx.read()
        self.assertEqual(rx.rwnd(), BUF)

    def test_beyond_right_edge_trimmed(self):
        """Data past rcv_nxt + window is cut off and reported."""
        rx, events = make(rcv_buffer=1000)
        rx.on_segment(ISN, b"a" * 600, False, 1.0)
        reason = rx.on_segment(ISN + 600, b"b" * 600, True, 1.0)
        self.assertEqual(reason, 'cumulative')
        self.assertEqual(rx.rcv_nxt, ISN + 1000)
        self.assertIsNone(rx.fin_seq)
        self.assertFalse(rx.fin_received)
        self.assertEqual(rx.rwnd(), 0)
        self.assertIn(('OUT_OF_WINDOW', {'seq': ISN + 600, 'len': 600, 'rwnd': 400}),
                      events)
        self.assertEqual(rx.read(), b"a" * 600 + b"b" * 400)

    def test_out_of_order_beyond_edge_trimmed(self):
        """An out-of-order segment is also cut at the right edge."""
        rx, _ = make(rcv_buffer=1000)
        self.assertEqual(rx.on_segment(ISN + 900, b"z" * 300, False, 1.0), 'duplicate')
        self.assertEqual(rx.out_of_order, {ISN + 900: b"z" * 100})
        self.assertEqual(rx.on_segment(ISN + 1000, b"z" * 300, False, 1.0), 'out_of_window')
        self.assertNotIn(ISN + 1000, rx.out_of_order)

    def test_zero_window_takes_no_data_but_answers(self):
        """With window 0 new data is refused yet still gets an ACK."""
        rx, events = make(rcv_buffer=100)
        rx.on_segment(ISN, b"a" * 100, False, 1.0)
        self.assertEqual(rx.rwnd(), 0)
        self.assertEqual(rx.on_segment(ISN + 100, b"b", False, 1.2), 'out_of_window')
        self.assertEqual(rx.rcv_nxt, ISN + 100)
        self.assertEqual(rx.rwnd(), 0)
        self.assertEqual(names(events)[-1], 'OUT_OF_WINDOW')
        rx.read()
        self.assertEqual(rx.on_segment(ISN + 100, b"b", False, 1.3), 'cumulative')
        self.assertEqual(rx.read(), b"b")

    def test_probe_and_pure_ack(self):
        """An empty segment below rcv_nxt is a probe; at rcv_nxt it is a pure ACK."""
        rx, _ = make()
        rx.on_segment(ISN, b"a" * 10, False, 1.0)
        self.assertEqual(rx.on_segment(ISN + 9, b"", False, 1.0), 'probe')
        self.assertIsNone(rx.on_segment(ISN + 10, b"", False, 1.0))
        self.assertEqual(rx.rcv_nxt, ISN + 10)

    def test_window_never_out_of_range(self):
        """rwnd() stays between 0 and rcv_buffer: trim_to_window never overfills the buffer."""
        rx, _ = make(rcv_buffer=10)
        rx.on_segment(ISN, b"x" * 50, False, 1.0)
        self.assertEqual(len(rx.buffered_data), 10)
        self.assertEqual(rx.rwnd(), 0)
        rx.read()
        self.assertEqual(rx.rwnd(), 10)

    def test_window_update_due_on_crossing(self):
        """window_update_due is true only when crossing rcv_buffer // 4 from below."""
        rx, _ = make(rcv_buffer=1000)
        self.assertFalse(rx.window_update_due())
        rx.on_segment(ISN, b"a" * 900, False, 1.0)
        rx.ack_sent(rx.rwnd())  # advertised 100, below 250
        self.assertFalse(rx.window_update_due())
        rx.read(100)  # window 200, still below the quarter
        self.assertFalse(rx.window_update_due())
        rx.read(50)  # window 250, crosses
        self.assertTrue(rx.window_update_due())
        rx.ack_sent(rx.rwnd())
        self.assertFalse(rx.window_update_due())
        rx.read()
        self.assertFalse(rx.window_update_due())

    def test_no_update_when_last_window_was_large(self):
        """A read after a large advertised window needs no update."""
        rx, _ = make(rcv_buffer=1000)
        rx.on_segment(ISN, b"a" * 500, False, 1.0)
        rx.ack_sent(rx.rwnd())  # 500
        rx.read()
        self.assertFalse(rx.window_update_due())


class FinTests(unittest.TestCase):
    """FIN handling."""

    def test_fin_in_order(self):
        """A FIN right after the data takes one sequence number."""
        rx, events = make()
        self.assertEqual(rx.on_segment(ISN, b"hello", True, 1.0), 'fin')
        self.assertTrue(rx.fin_received)
        self.assertEqual(rx.rcv_nxt, ISN + 6)
        self.assertIn(('FIN_RECEIVED', {'seq': ISN + 5}), events)
        self.assertTrue(rx.readable())
        self.assertEqual(rx.read(), b"hello")
        self.assertTrue(rx.readable())

    def test_empty_fin_with_zero_window(self):
        """A bare FIN needs no buffer space."""
        rx, _ = make(rcv_buffer=5)
        rx.on_segment(ISN, b"hello", False, 1.0)
        self.assertEqual(rx.on_segment(ISN + 5, b"", True, 1.0), 'fin')
        self.assertEqual(rx.rcv_nxt, ISN + 6)

    def test_fin_after_gap(self):
        """A FIN above a gap waits until the gap fills."""
        rx, _ = make()
        self.assertEqual(rx.on_segment(ISN + 5, b"world", True, 1.0), 'duplicate')
        self.assertFalse(rx.fin_received)
        self.assertEqual(rx.fin_seq, ISN + 10)
        self.assertEqual(rx.on_segment(ISN, b"hello", False, 1.1), 'fin')
        self.assertTrue(rx.fin_received)
        self.assertEqual(rx.rcv_nxt, ISN + 11)
        self.assertEqual(rx.read(), b"helloworld")

    def test_duplicate_fin(self):
        """A FIN seen again is old and changes nothing."""
        rx, events = make()
        rx.on_segment(ISN, b"hello", True, 1.0)
        self.assertEqual(rx.on_segment(ISN, b"hello", True, 1.5), 'old_segment')
        self.assertEqual(rx.on_segment(ISN + 5, b"", True, 1.6), 'old_segment')
        self.assertEqual(rx.rcv_nxt, ISN + 6)
        self.assertEqual(names(events).count('FIN_RECEIVED'), 1)
        self.assertEqual(rx.read(), b"hello")

    def test_fin_resent_with_old_data(self):
        """A resent data+FIN whose data is already in gives only the FIN."""
        rx, _ = make()
        rx.on_segment(ISN, b"hello", False, 1.0)
        self.assertEqual(rx.on_segment(ISN, b"hello", True, 1.0), 'fin')
        self.assertEqual(rx.read(), b"hello")


class RandomStreamTest(unittest.TestCase):
    """Shuffled, duplicated and split segments still give the exact stream."""

    def test_random_stream(self):
        """Feed a 50 KB stream in random order until all of it is delivered."""
        rng = random.Random(3094)
        data = bytes(rng.getrandbits(8) for _ in range(50 * 1024))
        segments = []
        pos = 0
        while pos < len(data):
            size = rng.randint(1, 1460)
            segments.append((ISN + pos, data[pos:pos + size],
                             pos + size >= len(data)))
            pos += size
        # Duplicates and overlapping pieces split from the stream
        for _ in range(len(segments) // 2):
            start = rng.randrange(len(data))
            size = rng.randint(1, 2000)
            piece = data[start:start + size]
            segments.append((ISN + start, piece, start + len(piece) >= len(data)))
        segments.extend(rng.sample(segments, len(segments) // 4))

        rx, _ = make(rcv_buffer=8192)
        out = bytearray()
        now = 0.0
        for _ in range(200):
            rng.shuffle(segments)
            for seq, payload, fin in segments:
                now += 0.001
                rx.on_segment(seq, payload, fin, now)
                self.assertTrue(0 <= rx.rwnd() <= rx.rcv_buffer)
                self.assertLessEqual(len(rx.buffered_data), rx.rcv_buffer)
                if rng.random() < 0.3:
                    out.extend(rx.read(rng.randint(1, 4000)))
            out.extend(rx.read())
            if rx.fin_received:
                break
        self.assertTrue(rx.fin_received)
        self.assertEqual(bytes(out), data)
        self.assertEqual(rx.rcv_nxt, ISN + len(data) + 1)


if __name__ == '__main__':
    unittest.main()

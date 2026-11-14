"""Unit tests for the Reno state machine (slides 3-123 to 3-125)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from streaprous_socket.tcp_reno import (  # noqa: E402
    CONGESTION_AVOIDANCE, FAST_RECOVERY, SLOW_START, RenoCongestion)

MSS = 1000


def make(ssthresh=8000, events=None):
    """Builds a Reno machine; state changes are appended to events if given."""
    on_event = None
    if events is not None:
        def on_event(name, **details):
            events.append((name, details))
    return RenoCongestion(MSS, ssthresh, on_event)


def to_fast_recovery(reno):
    """Sends three duplicate ACKs."""
    for _ in range(3):
        reno.on_duplicate_ack()


class RenoReferenceTest(unittest.TestCase):

    def test_reference_sequence(self):
        reno = make()
        self.assertEqual((reno.cwnd, reno.ssthresh, reno.state), (1000, 8000, SLOW_START))
        for expected in range(2000, 8001, 1000):
            reno.on_new_ack()
            self.assertEqual(reno.cwnd, expected)
            self.assertEqual(reno.state, SLOW_START)
        reno.on_new_ack()
        self.assertEqual(reno.cwnd, 9000)
        self.assertEqual(reno.state, CONGESTION_AVOIDANCE)
        reno.on_new_ack()
        self.assertEqual(reno.cwnd, 9111)
        self.assertEqual([reno.on_duplicate_ack() for _ in range(3)], [False, False, True])
        self.assertEqual(reno.ssthresh, 4555)
        self.assertEqual(reno.cwnd, 7555)
        self.assertEqual(reno.state, FAST_RECOVERY)
        self.assertFalse(reno.on_duplicate_ack())
        self.assertEqual(reno.cwnd, 8555)
        reno.on_new_ack()
        self.assertEqual(reno.cwnd, 4555)
        self.assertEqual(reno.state, CONGESTION_AVOIDANCE)
        reno.on_timeout()
        self.assertEqual(reno.ssthresh, 2277)
        self.assertEqual(reno.cwnd, 1000)
        self.assertEqual(reno.state, SLOW_START)
        reno.on_timeout()
        self.assertEqual(reno.ssthresh, 2000)
        self.assertEqual(reno.cwnd, 1000)


class RenoTableTest(unittest.TestCase):

    def test_slow_start_new_ack(self):
        reno = make()
        reno.dup_ack_count = 2
        self.assertIsNone(reno.on_new_ack())
        self.assertEqual(reno.cwnd, 2000)
        self.assertEqual(reno.dup_ack_count, 0)
        self.assertEqual(reno.state, SLOW_START)

    def test_slow_start_stays_when_cwnd_equals_ssthresh(self):
        reno = make(ssthresh=2000)
        reno.on_new_ack()
        self.assertEqual(reno.cwnd, 2000)
        self.assertEqual(reno.state, SLOW_START)
        reno.on_new_ack()
        self.assertEqual(reno.cwnd, 3000)
        self.assertEqual(reno.state, CONGESTION_AVOIDANCE)

    def test_congestion_avoidance_new_ack(self):
        reno = make(ssthresh=1000)
        reno.on_new_ack()
        self.assertEqual(reno.state, CONGESTION_AVOIDANCE)
        reno.dup_ack_count = 2
        self.assertIsNone(reno.on_new_ack())
        self.assertEqual(reno.cwnd, 2000 + 1000000 // 2000)
        self.assertEqual(reno.dup_ack_count, 0)
        self.assertEqual(reno.state, CONGESTION_AVOIDANCE)

    def test_congestion_avoidance_growth_is_at_least_one_byte(self):
        reno = make(ssthresh=1000)
        reno.on_new_ack()
        reno.cwnd = 5000000
        reno.on_new_ack()
        self.assertEqual(reno.cwnd, 5000001)

    def test_fast_recovery_new_ack(self):
        reno = make()
        to_fast_recovery(reno)
        self.assertIsNone(reno.on_new_ack())
        self.assertEqual(reno.cwnd, reno.ssthresh)
        self.assertEqual(reno.ssthresh, 2000)
        self.assertEqual(reno.dup_ack_count, 0)
        self.assertEqual(reno.state, CONGESTION_AVOIDANCE)

    def test_only_third_duplicate_ack_returns_true(self):
        reno = make()
        reno.on_new_ack()
        self.assertIs(reno.on_duplicate_ack(), False)
        self.assertIs(reno.on_duplicate_ack(), False)
        self.assertEqual(reno.dup_ack_count, 2)
        self.assertIs(reno.on_duplicate_ack(), True)
        self.assertEqual(reno.state, FAST_RECOVERY)

    def test_duplicate_acks_in_slow_start_do_not_change_cwnd(self):
        reno = make()
        reno.on_new_ack()
        reno.on_duplicate_ack()
        reno.on_duplicate_ack()
        self.assertEqual(reno.cwnd, 2000)
        self.assertEqual(reno.ssthresh, 8000)
        self.assertEqual(reno.state, SLOW_START)

    def test_duplicate_acks_in_congestion_avoidance(self):
        reno = make(ssthresh=1000)
        reno.on_new_ack()
        before = reno.cwnd
        reno.on_duplicate_ack()
        reno.on_duplicate_ack()
        self.assertEqual(reno.cwnd, before)
        self.assertTrue(reno.on_duplicate_ack())
        self.assertEqual(reno.ssthresh, max(before // 2, 2000))
        self.assertEqual(reno.cwnd, reno.ssthresh + 3000)

    def test_third_duplicate_ack_floor_of_two_mss(self):
        reno = make()
        to_fast_recovery(reno)
        self.assertEqual(reno.ssthresh, 2000)
        self.assertEqual(reno.cwnd, 5000)

    def test_new_ack_resets_duplicate_count(self):
        reno = make()
        reno.on_duplicate_ack()
        reno.on_duplicate_ack()
        reno.on_new_ack()
        self.assertEqual([reno.on_duplicate_ack() for _ in range(3)], [False, False, True])

    def test_fast_recovery_duplicate_ack(self):
        reno = make()
        to_fast_recovery(reno)
        before = reno.cwnd
        self.assertIs(reno.on_duplicate_ack(), False)
        self.assertEqual(reno.cwnd, before + 1000)
        self.assertEqual(reno.state, FAST_RECOVERY)

    def test_timeout_from_each_state(self):
        reno = make()
        reno.cwnd = 6000
        reno.dup_ack_count = 2
        self.assertIsNone(reno.on_timeout())
        self.assertEqual((reno.ssthresh, reno.cwnd), (3000, 1000))
        self.assertEqual(reno.dup_ack_count, 0)
        self.assertEqual(reno.state, SLOW_START)

        reno = make(ssthresh=1000)
        reno.on_new_ack()
        reno.on_timeout()
        self.assertEqual((reno.ssthresh, reno.cwnd), (2000, 1000))
        self.assertEqual(reno.state, SLOW_START)

        reno = make()
        to_fast_recovery(reno)
        reno.on_timeout()
        self.assertEqual((reno.ssthresh, reno.cwnd), (2500, 1000))
        self.assertEqual(reno.dup_ack_count, 0)
        self.assertEqual(reno.state, SLOW_START)


class RenoEventTest(unittest.TestCase):

    def test_no_callback_is_fine(self):
        reno = make()
        to_fast_recovery(reno)
        reno.on_timeout()
        self.assertEqual(reno.state, SLOW_START)

    def test_state_events(self):
        events = []
        reno = make(ssthresh=2000, events=events)
        reno.on_new_ack()
        self.assertEqual(events, [])
        reno.on_new_ack()
        to_fast_recovery(reno)
        reno.on_new_ack()
        to_fast_recovery(reno)
        reno.on_timeout()
        pairs = [(SLOW_START, CONGESTION_AVOIDANCE),
                 (CONGESTION_AVOIDANCE, FAST_RECOVERY),
                 (FAST_RECOVERY, CONGESTION_AVOIDANCE),
                 (CONGESTION_AVOIDANCE, FAST_RECOVERY),
                 (FAST_RECOVERY, SLOW_START)]
        self.assertEqual(events, [('STATE', {'old': old, 'new': new}) for old, new in pairs])

    def test_no_event_without_state_change(self):
        events = []
        reno = make(events=events)
        reno.on_timeout()
        reno.on_duplicate_ack()
        reno.on_new_ack()
        self.assertEqual(events, [])


if __name__ == '__main__':
    unittest.main()

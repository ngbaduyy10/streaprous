"""
Tests for the Reno state machine: the 13 arrows of slide 3-125.
"""

import unittest

from streaprous_socket.tcp_reno import (
    CONGESTION_AVOIDANCE, FAST_RECOVERY, INITIAL_SSTHRESH, SLOW_START, RenoCongestion)
from tests.slide_rules import assert_slide_rules

MSS = 1000

RENO_RULES = [
    ('RenoCongestion.__init__', '3-125', 'cwnd = 1 MSS, ssthresh = 64 KB, dupACKcount = 0'),
    ('RenoCongestion.on_new_ack', '3-123', 'incrementing cwnd for every ACK received'),
    ('RenoCongestion.on_new_ack', '3-125', 'cwnd = cwnd + MSS*(MSS/cwnd)'),
    ('RenoCongestion.on_new_ack', '3-125', 'cwnd = ssthresh'),
    ('RenoCongestion.on_new_ack', 'extra', 'round down'),
    ('RenoCongestion.on_duplicate_ack', '3-125', 'dupACKcount == 3'),
    ('RenoCongestion.on_duplicate_ack', '3-125', 'cwnd = ssthresh + 3'),
    ('RenoCongestion.on_timeout', '3-124', 'ssthresh is set to 1/2 of cwnd just before loss event'),
    ('RenoCongestion.on_timeout', '3-125', 'cwnd = 1 MSS'),
    ('RenoCongestion.check_slow_start_end', '3-125', 'cwnd > ssthresh'),
]


def make(ssthresh=8000, events=None):
    """Builds a Reno machine; with events, its log appends (event, details) there.

    Args:
         ssthresh  first ssthresh in bytes
         events    list for the logged events, or None

    Returns:
         the RenoCongestion
    """
    if events is None:
        return RenoCongestion(MSS, ssthresh)

    def log(event, **details):
        """Appends one event."""
        events.append((event, details))
    return RenoCongestion(MSS, ssthresh, log)


def to_congestion_avoidance(reno):
    """Gives new ACKs until Reno leaves slow start.

    Args:
         reno  the RenoCongestion
    """
    while reno.state == SLOW_START:
        reno.on_new_ack()


def to_fast_recovery(reno):
    """Gives three duplicate ACKs.

    Args:
         reno  the RenoCongestion
    """
    for _ in range(3):
        reno.on_duplicate_ack()


class RenoArrowTest(unittest.TestCase):
    """One test per arrow of the finite state machine on slide 3-125."""

    def test_arrow_01_start(self):
        """Slide 3-125 start: cwnd = 1 MSS, ssthresh = 64 KB, dupACKcount = 0, slow start."""
        reno = RenoCongestion(MSS)
        self.assertEqual(INITIAL_SSTHRESH, 64 * 1024)
        self.assertEqual((reno.cwnd, reno.ssthresh, reno.dup_ack_count, reno.state),
                         (1000, 65536, 0, SLOW_START))

    def test_arrow_02_slow_start_new_ack(self):
        """Slide 3-125 slow start, new ACK: cwnd = cwnd+MSS, dupACKcount = 0."""
        reno = make()
        reno.dup_ack_count = 2
        self.assertIsNone(reno.on_new_ack())
        self.assertEqual((reno.cwnd, reno.dup_ack_count, reno.state), (2000, 0, SLOW_START))

    def test_arrow_03_slow_start_duplicate_ack(self):
        """Slide 3-125 slow start, duplicate ACK: dupACKcount++ and nothing else."""
        reno = make()
        reno.on_new_ack()
        self.assertIs(reno.on_duplicate_ack(), False)
        self.assertIs(reno.on_duplicate_ack(), False)
        self.assertEqual((reno.cwnd, reno.ssthresh, reno.dup_ack_count, reno.state),
                         (2000, 8000, 2, SLOW_START))

    def test_arrow_04_slow_start_to_congestion_avoidance(self):
        """Slide 3-125 arrow cwnd > ssthresh: cwnd equal to ssthresh stays in slow start."""
        reno = make(ssthresh=2000)
        reno.on_new_ack()
        self.assertEqual((reno.cwnd, reno.state), (2000, SLOW_START))
        reno.on_new_ack()
        self.assertEqual((reno.cwnd, reno.state), (3000, CONGESTION_AVOIDANCE))

    def test_arrow_05_slow_start_timeout(self):
        """Slide 3-125 slow start, timeout: ssthresh = cwnd/2, cwnd = 1 MSS, dupACKcount = 0."""
        reno = make()
        reno.cwnd = 6000
        reno.dup_ack_count = 2
        self.assertIsNone(reno.on_timeout())
        self.assertEqual((reno.ssthresh, reno.cwnd, reno.dup_ack_count, reno.state),
                         (3000, 1000, 0, SLOW_START))

    def test_arrow_06_slow_start_third_duplicate_ack(self):
        """Slide 3-125 dupACKcount == 3 in slow start: ssthresh = cwnd/2 with no floor.

        cwnd 1000 gives ssthresh 500 and cwnd = ssthresh + 3 MSS = 3500.
        """
        reno = make()
        self.assertEqual([reno.on_duplicate_ack() for _ in range(3)], [False, False, True])
        self.assertEqual((reno.ssthresh, reno.cwnd, reno.state), (500, 3500, FAST_RECOVERY))

    def test_arrow_07_congestion_avoidance_new_ack(self):
        """Slide 3-125 congestion avoidance, new ACK: cwnd = cwnd + MSS*(MSS/cwnd)."""
        reno = make(ssthresh=1000)
        reno.on_new_ack()
        self.assertEqual((reno.cwnd, reno.state), (2000, CONGESTION_AVOIDANCE))
        reno.dup_ack_count = 2
        reno.on_new_ack()
        self.assertEqual((reno.cwnd, reno.dup_ack_count, reno.state),
                         (2500, 0, CONGESTION_AVOIDANCE))

    def test_arrow_08_congestion_avoidance_duplicate_ack(self):
        """Slide 3-125 congestion avoidance, duplicate ACK: dupACKcount++ only."""
        reno = make(ssthresh=1000)
        to_congestion_avoidance(reno)
        self.assertIs(reno.on_duplicate_ack(), False)
        self.assertEqual((reno.cwnd, reno.dup_ack_count, reno.state),
                         (2000, 1, CONGESTION_AVOIDANCE))

    def test_arrow_09_congestion_avoidance_timeout(self):
        """Slide 3-125 congestion avoidance, timeout: back to slow start with cwnd = 1 MSS."""
        reno = make(ssthresh=1000)
        to_congestion_avoidance(reno)
        reno.on_timeout()
        self.assertEqual((reno.ssthresh, reno.cwnd, reno.dup_ack_count, reno.state),
                         (1000, 1000, 0, SLOW_START))

    def test_arrow_10_congestion_avoidance_third_duplicate_ack(self):
        """Slide 3-125 dupACKcount == 3 in congestion avoidance: go to fast recovery."""
        reno = make(ssthresh=1000)
        to_congestion_avoidance(reno)
        reno.cwnd = 6000
        self.assertEqual([reno.on_duplicate_ack() for _ in range(3)], [False, False, True])
        self.assertEqual((reno.ssthresh, reno.cwnd, reno.state), (3000, 6000, FAST_RECOVERY))

    def test_arrow_11_fast_recovery_duplicate_ack(self):
        """Slide 3-125 fast recovery, duplicate ACK: cwnd = cwnd + MSS, no retransmit."""
        reno = make()
        to_fast_recovery(reno)
        self.assertIs(reno.on_duplicate_ack(), False)
        self.assertEqual((reno.cwnd, reno.dup_ack_count, reno.state), (4500, 3, FAST_RECOVERY))

    def test_arrow_12_fast_recovery_new_ack(self):
        """Slide 3-125 fast recovery, new ACK: cwnd = ssthresh, dupACKcount = 0."""
        reno = make()
        to_fast_recovery(reno)
        reno.on_new_ack()
        self.assertEqual((reno.cwnd, reno.dup_ack_count, reno.state),
                         (500, 0, CONGESTION_AVOIDANCE))

    def test_arrow_13_fast_recovery_timeout(self):
        """Slide 3-125 fast recovery, timeout (flaw scenario: timeout during fast recovery)."""
        reno = make()
        reno.cwnd = 6000
        to_fast_recovery(reno)
        self.assertEqual(reno.cwnd, 6000)
        reno.on_timeout()
        self.assertEqual((reno.ssthresh, reno.cwnd, reno.dup_ack_count, reno.state),
                         (3000, 1000, 0, SLOW_START))


class RenoNumbersTest(unittest.TestCase):
    """Reference numbers and edge cases of the Reno machine."""

    def test_reference_sequence(self):
        """The reference numbers with mss 1000 and ssthresh 8000 (slides 3-123 to 3-125)."""
        reno = make()
        for expected in range(2000, 8001, 1000):
            reno.on_new_ack()
            self.assertEqual((reno.cwnd, reno.state), (expected, SLOW_START))
        reno.on_new_ack()
        self.assertEqual((reno.cwnd, reno.state), (9000, CONGESTION_AVOIDANCE))
        reno.on_new_ack()
        self.assertEqual(reno.cwnd, 9111)
        to_fast_recovery(reno)
        self.assertEqual((reno.ssthresh, reno.cwnd, reno.state), (4555, 7555, FAST_RECOVERY))
        reno.on_duplicate_ack()
        self.assertEqual(reno.cwnd, 8555)
        reno.on_new_ack()
        self.assertEqual((reno.cwnd, reno.state), (4555, CONGESTION_AVOIDANCE))
        reno.on_timeout()
        self.assertEqual((reno.ssthresh, reno.cwnd, reno.state), (2277, 1000, SLOW_START))
        reno.on_timeout()
        self.assertEqual((reno.ssthresh, reno.cwnd, reno.state),
                         (500, 1000, CONGESTION_AVOIDANCE))

    def test_repeated_timeouts_ssthresh_below_mss(self):
        """Flaw scenario: ssthresh below one MSS after repeated timeouts never stalls cwnd."""
        reno = make()
        for _ in range(5):
            reno.on_timeout()
            self.assertEqual(reno.cwnd, 1000)
            self.assertEqual(reno.state, CONGESTION_AVOIDANCE)
        self.assertEqual(reno.ssthresh, 500)
        reno.on_new_ack()
        self.assertEqual(reno.cwnd, 2000)

    def test_congestion_avoidance_growth_in_whole_bytes(self):
        """Extra: MSS*(MSS/cwnd) rounds down to whole bytes, with no 1-byte minimum."""
        for cwnd, grown in [(3000, 3333), (7000, 7142), (1000000, 1000001), (5000000, 5000000)]:
            reno = make(ssthresh=500)
            reno.on_new_ack()
            reno.cwnd = cwnd
            reno.on_new_ack()
            self.assertEqual(reno.cwnd, grown)
            self.assertIsInstance(reno.cwnd, int)

    def test_new_ack_resets_duplicate_count(self):
        """Slide 3-125: a new ACK sets dupACKcount = 0, so three more are needed."""
        reno = make()
        reno.on_duplicate_ack()
        reno.on_duplicate_ack()
        reno.on_new_ack()
        self.assertEqual([reno.on_duplicate_ack() for _ in range(3)], [False, False, True])

    def test_state_log_events(self):
        """Slide 3-125: each state change logs STATE old, new; no change logs nothing."""
        events = []
        reno = make(ssthresh=2000, events=events)
        reno.on_new_ack()
        reno.on_duplicate_ack()
        self.assertEqual(events, [])
        reno.on_new_ack()
        to_fast_recovery(reno)
        reno.on_new_ack()
        to_fast_recovery(reno)
        reno.on_timeout()
        reno.on_timeout()
        pairs = [(SLOW_START, CONGESTION_AVOIDANCE),
                 (CONGESTION_AVOIDANCE, FAST_RECOVERY),
                 (FAST_RECOVERY, CONGESTION_AVOIDANCE),
                 (CONGESTION_AVOIDANCE, FAST_RECOVERY),
                 (FAST_RECOVERY, SLOW_START),
                 (SLOW_START, CONGESTION_AVOIDANCE)]
        self.assertEqual(events, [('STATE', {'old': old, 'new': new}) for old, new in pairs])

    def test_slide_rules(self):
        """Each Reno function's docstring carries its slide rule."""
        assert_slide_rules(self, 'tcp_reno', RENO_RULES)


if __name__ == '__main__':
    unittest.main()

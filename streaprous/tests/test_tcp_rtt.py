"""
Tests for the round trip time estimator (slides 3-81 to 3-83).
"""

import unittest

from streaprous_socket.tcp_rtt import (
    ALPHA, BETA, FIRST_TIMEOUT_INTERVAL, MIN_TIMEOUT_INTERVAL, RttEstimator)
from tests.slide_rules import assert_slide_rules

RTT_RULES = [
    ('RttEstimator.__init__', 'extra', '1 s before the first SampleRTT'),
    ('RttEstimator.add_sample', 'extra', 'DevRTT = SampleRTT/2'),
    ('RttEstimator.update_estimated_rtt', '3-82',
     'EstimatedRTT = (1 - alpha)*EstimatedRTT + alpha*SampleRTT'),
    ('RttEstimator.update_dev_rtt', '3-83',
     'DevRTT = (1 - beta)*DevRTT + beta*|SampleRTT - EstimatedRTT|'),
    ('RttEstimator.update_timeout_interval', '3-83', 'TimeoutInterval = EstimatedRTT + 4*DevRTT'),
    ('RttEstimator.update_timeout_interval', 'extra', 'never below 0.2 s'),
]


class RttEstimatorTest(unittest.TestCase):
    """EstimatedRTT, DevRTT and TimeoutInterval with the reference numbers."""

    def test_constants_are_the_slides(self):
        """Slides 3-82 and 3-83: alpha = 0.125 and beta = 0.25; the extras 1 s and 0.2 s."""
        self.assertEqual((ALPHA, BETA), (0.125, 0.25))
        self.assertEqual((FIRST_TIMEOUT_INTERVAL, MIN_TIMEOUT_INTERVAL), (1.0, 0.2))

    def test_first_timeout_interval_is_one_second(self):
        """Extra: TimeoutInterval is 1 s before the first SampleRTT."""
        est = RttEstimator()
        self.assertIsNone(est.estimated_rtt)
        self.assertIsNone(est.dev_rtt)
        self.assertEqual(est.timeout_interval, 1.0)

    def test_first_sample_starts_both_averages(self):
        """Extra: the first SampleRTT 0.1 gives EstimatedRTT 0.1, DevRTT 0.05, interval 0.3."""
        est = RttEstimator()
        est.add_sample(0.1)
        self.assertAlmostEqual(est.estimated_rtt, 0.1)
        self.assertAlmostEqual(est.dev_rtt, 0.05)
        self.assertAlmostEqual(est.timeout_interval, 0.3)

    def test_second_sample_in_slide_order(self):
        """Slides 3-82 then 3-83: DevRTT uses the new EstimatedRTT.

        A second sample 0.2 gives EstimatedRTT 0.1125, DevRTT 0.059375 and
        TimeoutInterval 0.35. The other order would give DevRTT 0.0625.
        """
        est = RttEstimator()
        est.add_sample(0.1)
        est.add_sample(0.2)
        self.assertAlmostEqual(est.estimated_rtt, 0.1125)
        self.assertAlmostEqual(est.dev_rtt, 0.059375)
        self.assertAlmostEqual(est.timeout_interval, 0.35)

    def test_each_formula_alone(self):
        """Slides 3-82 and 3-83: each update function does one formula."""
        est = RttEstimator()
        est.estimated_rtt = 0.4
        est.dev_rtt = 0.1
        est.update_estimated_rtt(0.8)
        self.assertAlmostEqual(est.estimated_rtt, 0.875 * 0.4 + 0.125 * 0.8)
        self.assertAlmostEqual(est.dev_rtt, 0.1)
        est.update_dev_rtt(0.8)
        self.assertAlmostEqual(est.dev_rtt, 0.75 * 0.1 + 0.25 * abs(0.8 - 0.45))
        est.update_timeout_interval()
        self.assertAlmostEqual(est.timeout_interval, 0.45 + 4 * est.dev_rtt)

    def test_timeout_interval_floor(self):
        """Extra: never below 0.2 s; a first sample of 0.001 gives 0.2."""
        est = RttEstimator()
        est.add_sample(0.001)
        self.assertAlmostEqual(est.timeout_interval, 0.2)
        self.assertAlmostEqual(est.estimated_rtt, 0.001)

    def test_no_ceiling(self):
        """Slide 3-83 has no upper limit: a first sample of 5.0 gives 15.0, and nothing doubles."""
        est = RttEstimator()
        est.add_sample(5.0)
        self.assertAlmostEqual(est.timeout_interval, 15.0)
        self.assertFalse(hasattr(est, 'back_off'))

    def test_custom_first_and_minimum(self):
        """Extra: the first interval and the floor can be chosen."""
        est = RttEstimator(first_timeout_interval=3.0, min_timeout_interval=0.5)
        self.assertEqual(est.timeout_interval, 3.0)
        est.add_sample(0.01)
        self.assertAlmostEqual(est.timeout_interval, 0.5)

    def test_slide_rules(self):
        """Each RTT function's docstring carries its slide rule."""
        assert_slide_rules(self, 'tcp_rtt', RTT_RULES)


if __name__ == '__main__':
    unittest.main()

"""Unit tests for the timeout estimator (slides 3-81 to 3-84)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from streaprous_socket.tcp_rtt import RttEstimator  # noqa: E402


def make():
    """Builds the estimator used in the reference numbers."""
    return RttEstimator(initial_rto=1.0, min_rto=0.2, max_rto=4.0)


class RttEstimatorTest(unittest.TestCase):

    def test_starts_with_initial_rto_and_no_estimates(self):
        est = make()
        self.assertIsNone(est.estimated_rtt)
        self.assertIsNone(est.dev_rtt)
        self.assertEqual(est.rto, 1.0)

    def test_first_sample(self):
        est = make()
        rto = est.add_sample(0.1)
        self.assertAlmostEqual(est.estimated_rtt, 0.1)
        self.assertAlmostEqual(est.dev_rtt, 0.05)
        self.assertAlmostEqual(rto, 0.3)
        self.assertAlmostEqual(est.rto, 0.3)

    def test_second_sample(self):
        est = make()
        est.add_sample(0.1)
        rto = est.add_sample(0.2)
        self.assertAlmostEqual(est.dev_rtt, 0.0625)
        self.assertAlmostEqual(est.estimated_rtt, 0.1125)
        self.assertAlmostEqual(rto, 0.3625)

    def test_floor(self):
        est = make()
        self.assertAlmostEqual(est.add_sample(0.001), 0.2)

    def test_ceiling(self):
        est = make()
        self.assertAlmostEqual(est.add_sample(5.0), 4.0)

    def test_back_off_doubles_and_caps(self):
        est = make()
        est.add_sample(0.1)
        results = [est.back_off() for _ in range(4)]
        for got, want in zip(results, [0.6, 1.2, 2.4, 4.0]):
            self.assertAlmostEqual(got, want)
        self.assertAlmostEqual(est.back_off(), 4.0)

    def test_back_off_from_reference_rto(self):
        est = make()
        est.add_sample(0.1)
        est.add_sample(0.2)
        results = [est.back_off() for _ in range(4)]
        for got, want in zip(results, [0.725, 1.45, 2.9, 4.0]):
            self.assertAlmostEqual(got, want)

    def test_back_off_before_any_sample(self):
        est = make()
        self.assertAlmostEqual(est.back_off(), 2.0)
        self.assertIsNone(est.estimated_rtt)

    def test_back_off_keeps_estimates(self):
        est = make()
        est.add_sample(0.1)
        est.back_off()
        self.assertAlmostEqual(est.estimated_rtt, 0.1)
        self.assertAlmostEqual(est.dev_rtt, 0.05)

    def test_sample_after_back_off_recomputes_from_estimates(self):
        est = make()
        est.add_sample(0.1)
        est.add_sample(0.2)
        est.back_off()
        est.back_off()
        self.assertAlmostEqual(est.rto, 1.45)
        # dev = 0.75 * 0.0625 + 0.25 * |0.1125 - 0.1125| = 0.046875
        # est = 0.875 * 0.1125 + 0.125 * 0.1125 = 0.1125
        rto = est.add_sample(0.1125)
        self.assertAlmostEqual(est.dev_rtt, 0.046875)
        self.assertAlmostEqual(est.estimated_rtt, 0.1125)
        self.assertAlmostEqual(rto, 0.1125 + 4 * 0.046875)


if __name__ == '__main__':
    unittest.main()

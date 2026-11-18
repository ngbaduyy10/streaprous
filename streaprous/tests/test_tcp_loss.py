"""Tests for streaprous_socket.tcp_loss."""

import unittest

from streaprous_socket.tcp_loss import LossInjector


class NthSendTest(unittest.TestCase):
    """Deterministic drops of sent segments."""

    def test_counts_only_new(self):
        loss = LossInjector(drop_send={2})
        self.assertIsNone(loss.check_send('new'))
        for kind in ('timeout', 'fast', 'retransmit', 'probe'):
            self.assertIsNone(loss.check_send(kind))
        self.assertEqual(loss.sent_new, 1)
        self.assertEqual(loss.check_send('new'), 'nth=2')
        self.assertEqual(loss.sent_new, 2)

    def test_each_index_once(self):
        loss = LossInjector(drop_send={1, 3})
        results = [loss.check_send('new') for _ in range(5)]
        self.assertEqual(results, ['nth=1', None, 'nth=3', None, None])

    def test_resend_of_dropped_segment_passes(self):
        loss = LossInjector(drop_send={1})
        self.assertEqual(loss.check_send('new'), 'nth=1')
        self.assertIsNone(loss.check_send('timeout'))

    def test_defaults_drop_nothing(self):
        loss = LossInjector()
        self.assertEqual([loss.check_send('new') for _ in range(50)], [None] * 50)


class RandomLossTest(unittest.TestCase):
    """Random drops with a seed."""

    def run_once(self, seed, rate=0.3):
        loss = LossInjector(rate=rate, seed=seed)
        return [loss.check_send('new') for _ in range(200)]

    def test_same_seed_repeats(self):
        self.assertEqual(self.run_once(7), self.run_once(7))
        self.assertEqual(self.run_once('abc'), self.run_once('abc'))

    def test_different_seed_differs(self):
        self.assertNotEqual(self.run_once(7), self.run_once(8))

    def test_some_drop_and_some_pass(self):
        results = self.run_once(1)
        self.assertIn('random', results)
        self.assertIn(None, results)

    def test_rate_one_drops_every_kind(self):
        loss = LossInjector(rate=1.0, seed=1)
        for kind in ('new', 'timeout', 'fast', 'probe'):
            self.assertEqual(loss.check_send(kind), 'random')

    def test_rate_zero_drops_nothing(self):
        self.assertEqual(set(self.run_once(1, rate=0.0)), {None})

    def test_nth_wins_over_random(self):
        loss = LossInjector(drop_send={1}, rate=1.0, seed=1)
        self.assertEqual(loss.check_send('new'), 'nth=1')
        self.assertEqual(loss.check_send('new'), 'random')


if __name__ == '__main__':
    unittest.main()

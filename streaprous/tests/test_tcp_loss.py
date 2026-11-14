"""Tests for streaprous_socket.tcp_loss."""

import unittest

from streaprous_socket.tcp_loss import LossInjector


class NthSendTest(unittest.TestCase):
    """Deterministic drops of sent segments."""

    def test_counts_only_new(self):
        loss = LossInjector(drop_send={2})
        self.assertIsNone(loss.check_send('new'))
        self.assertIsNone(loss.check_send('timeout'))
        self.assertIsNone(loss.check_send('fast'))
        self.assertIsNone(loss.check_send('retransmit'))
        self.assertIsNone(loss.check_send('probe'))
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

    def test_no_drops_by_default(self):
        loss = LossInjector()
        self.assertEqual([loss.check_send('new') for _ in range(50)], [None] * 50)


class RecvTest(unittest.TestCase):
    """Deterministic drops of received segments."""

    def test_counting(self):
        loss = LossInjector(drop_recv={2, 4})
        results = [loss.check_recv() for _ in range(5)]
        self.assertEqual(results, [None, 'nth=2', None, 'nth=4', None])
        self.assertEqual(loss.received, 5)

    def test_send_and_recv_counters_are_separate(self):
        loss = LossInjector(drop_send={1}, drop_recv={1})
        self.assertEqual(loss.check_recv(), 'nth=1')
        self.assertEqual(loss.check_send('new'), 'nth=1')


class RandomTest(unittest.TestCase):
    """Random drops."""

    def run_sequence(self, seed):
        loss = LossInjector(rate=0.3, seed=seed)
        return [loss.check_send('new') for _ in range(200)]

    def test_seed_is_repeatable(self):
        self.assertEqual(self.run_sequence(7), self.run_sequence(7))
        self.assertEqual(self.run_sequence('abc'), self.run_sequence('abc'))

    def test_different_seeds_differ(self):
        self.assertNotEqual(self.run_sequence(7), self.run_sequence(8))

    def test_random_drops_happen_and_say_random(self):
        results = self.run_sequence(7)
        self.assertIn('random', results)
        self.assertEqual(set(results), {'random', None})

    def test_random_applies_to_any_kind(self):
        loss = LossInjector(rate=1.0, seed=1)
        for kind in ('new', 'timeout', 'fast', 'retransmit', 'probe'):
            self.assertEqual(loss.check_send(kind), 'random')

    def test_nth_wins_over_random(self):
        loss = LossInjector(drop_send={1}, rate=1.0, seed=1)
        self.assertEqual(loss.check_send('new'), 'nth=1')
        self.assertEqual(loss.check_send('new'), 'random')

    def test_rate_zero_never_drops(self):
        loss = LossInjector(rate=0.0, seed=5)
        self.assertEqual([loss.check_send('new') for _ in range(500)], [None] * 500)

    def test_recv_ignores_rate(self):
        loss = LossInjector(rate=1.0, seed=1)
        self.assertEqual([loss.check_recv() for _ in range(10)], [None] * 10)


if __name__ == '__main__':
    unittest.main()

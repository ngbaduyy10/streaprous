"""Tests for streaprous_socket.tcp_config."""

import unittest

from streaprous_socket import tcp_config
from streaprous_socket.tcp_config import load_settings, parse_drop_list


class ParseDropListTest(unittest.TestCase):
    """parse_drop_list turns text into a set of numbers."""

    def test_empty(self):
        self.assertEqual(parse_drop_list(''), frozenset())
        self.assertEqual(parse_drop_list('  ,  '), frozenset())

    def test_numbers_and_ranges(self):
        self.assertEqual(parse_drop_list('3,60'), frozenset({3, 60}))
        self.assertEqual(parse_drop_list('150,400-402'), frozenset({150, 400, 401, 402}))

    def test_spaces(self):
        self.assertEqual(parse_drop_list(' 3 , 5 - 7 '), frozenset({3, 5, 6, 7}))

    def test_errors(self):
        for text in ('x', '3,,a', '5-3', '0', '-4', '1-2-3', '2-'):
            with self.assertRaises(ValueError, msg=text):
                parse_drop_list(text)


class DefaultsTest(unittest.TestCase):
    """Every field of load_settings({}) has its default."""

    def test_defaults(self):
        s = load_settings({})
        self.assertEqual(s.rcv_buffer, 4096)
        self.assertEqual(s.drop_send, frozenset())
        self.assertEqual(s.loss_rate, 0.0)
        self.assertIsNone(s.loss_seed)
        self.assertTrue(s.trace)
        self.assertEqual(s.trace_dir, 'out')
        self.assertEqual(s.app_read_delay, 0.0)
        self.assertEqual(s.app_read_bytes, 0)
        self.assertEqual(s.request_timeout, 30.0)

    def test_removed_fields_are_gone(self):
        s = load_settings({})
        for name in ('mss_limit', 'recv_buffer', 'initial_ssthresh', 'log_level', 'drop_recv'):
            self.assertFalse(hasattr(s, name), name)

    def test_empty_text_gives_default(self):
        s = load_settings({'STREAPROUS_RCVBUF': ' ', 'STREAPROUS_LOSS': ''})
        self.assertEqual((s.rcv_buffer, s.loss_rate), (4096, 0.0))

    def test_hooks(self):
        self.assertEqual(tcp_config.RECV_BUFFER, tcp_config.SETTINGS.rcv_buffer)


class ValidValuesTest(unittest.TestCase):
    """A valid value for each variable."""

    def test_each_variable(self):
        s = load_settings({
            'STREAPROUS_RCVBUF': '16384',
            'STREAPROUS_DROP_SEND': '3,60',
            'STREAPROUS_LOSS': '0.05',
            'STREAPROUS_LOSS_SEED': '42',
            'STREAPROUS_TRACE': '0',
            'STREAPROUS_TRACE_DIR': 'logs',
            'STREAPROUS_APP_READ_DELAY': '0.5',
            'STREAPROUS_APP_READ_BYTES': '4',
        })
        self.assertEqual(s.rcv_buffer, 16384)
        self.assertEqual(s.drop_send, frozenset({3, 60}))
        self.assertEqual(s.loss_rate, 0.05)
        self.assertEqual(s.loss_seed, 42)
        self.assertFalse(s.trace)
        self.assertEqual(s.trace_dir, 'logs')
        self.assertEqual(s.app_read_delay, 0.5)
        self.assertEqual(s.app_read_bytes, 4)

    def test_edges(self):
        self.assertEqual(load_settings({'STREAPROUS_RCVBUF': '1'}).rcv_buffer, 1)
        self.assertEqual(load_settings({'STREAPROUS_RCVBUF': '65535'}).rcv_buffer, 65535)
        self.assertEqual(load_settings({'STREAPROUS_LOSS': '1'}).loss_rate, 1.0)
        self.assertEqual(load_settings({'STREAPROUS_APP_READ_DELAY': '10'}).app_read_delay, 10.0)

    def test_seed_text_stays_text(self):
        self.assertEqual(load_settings({'STREAPROUS_LOSS_SEED': 'abc'}).loss_seed, 'abc')

    def test_trace_on_unless_zero(self):
        self.assertTrue(load_settings({'STREAPROUS_TRACE': '1'}).trace)
        self.assertTrue(load_settings({'STREAPROUS_TRACE': 'yes'}).trace)


class BadValuesTest(unittest.TestCase):
    """A bad value raises ValueError naming the variable."""

    def check(self, name, text):
        with self.assertRaises(ValueError) as caught:
            load_settings({name: text})
        self.assertIn(name, str(caught.exception))

    def test_rcv_buffer(self):
        for text in ('0', '65536', 'x', '1.5'):
            self.check('STREAPROUS_RCVBUF', text)

    def test_drop_send(self):
        for text in ('x', '5-3', '0'):
            self.check('STREAPROUS_DROP_SEND', text)

    def test_loss(self):
        for text in ('-0.1', '1.5', 'x'):
            self.check('STREAPROUS_LOSS', text)

    def test_app_read_delay(self):
        for text in ('-1', '10.5', 'x'):
            self.check('STREAPROUS_APP_READ_DELAY', text)

    def test_app_read_bytes(self):
        for text in ('-1', 'x', '2.5'):
            self.check('STREAPROUS_APP_READ_BYTES', text)


if __name__ == '__main__':
    unittest.main()

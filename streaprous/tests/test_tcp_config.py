"""Tests for streaprous_socket.tcp_config."""

import unittest

from streaprous_socket import tcp_config
from streaprous_socket.tcp_config import describe, load_settings, parse_drop_list


class ParseDropListTest(unittest.TestCase):
    """parse_drop_list turns text into a set of numbers."""

    def test_empty(self):
        self.assertEqual(parse_drop_list(''), frozenset())
        self.assertEqual(parse_drop_list('  ,  '), frozenset())

    def test_numbers_and_ranges(self):
        self.assertEqual(parse_drop_list('3,60'), frozenset({3, 60}))
        self.assertEqual(parse_drop_list('150,400-403'), frozenset({150, 400, 401, 402, 403}))

    def test_spaces(self):
        self.assertEqual(parse_drop_list(' 3 , 5 - 7 '), frozenset({3, 5, 6, 7}))

    def test_errors(self):
        for text in ('x', '3,,a', '5-3', '0', '-4', '1-2-3', '2-'):
            with self.assertRaises(ValueError, msg=text):
                parse_drop_list(text)


class LoadSettingsTest(unittest.TestCase):
    """load_settings reads the environment variables."""

    def test_defaults(self):
        s = load_settings({})
        self.assertEqual(s.mss_limit, 1460)
        self.assertEqual(s.default_peer_mss, 536)
        self.assertEqual(s.recv_buffer, 32768)
        self.assertEqual(s.initial_ssthresh, 32768)
        self.assertEqual((s.initial_rto, s.min_rto, s.max_rto), (1.0, 0.2, 4.0))
        self.assertEqual(s.max_retries, 8)
        self.assertEqual(s.delayed_ack, 0.1)
        self.assertEqual((s.persist_min, s.persist_max), (0.2, 2.0))
        self.assertEqual(s.linger, 2.0)
        self.assertEqual(s.tick, 0.01)
        self.assertEqual(s.peer_silence, 60.0)
        self.assertEqual(s.request_timeout, 30.0)
        self.assertEqual(s.log_level, 1)
        self.assertTrue(s.trace)
        self.assertEqual(s.trace_dir, 'out')
        self.assertEqual(s.drop_send, frozenset())
        self.assertEqual(s.drop_recv, frozenset())
        self.assertEqual(s.loss_rate, 0.0)
        self.assertIsNone(s.loss_seed)
        self.assertEqual(s.app_read_delay, 0.0)
        self.assertEqual(s.app_read_bytes, 0)

    def test_each_variable(self):
        s = load_settings({
            'STREAPROUS_MSS': '1000',
            'STREAPROUS_RCVBUF': '4096',
            'STREAPROUS_SSTHRESH': '9000',
            'STREAPROUS_TCP_LOG': '2',
            'STREAPROUS_TRACE': '0',
            'STREAPROUS_TRACE_DIR': 'traces',
            'STREAPROUS_DROP_SEND': '3,60',
            'STREAPROUS_DROP_RECV': '2-4',
            'STREAPROUS_LOSS': '0.25',
            'STREAPROUS_LOSS_SEED': '42',
            'STREAPROUS_APP_READ_DELAY': '0.05',
            'STREAPROUS_APP_READ_BYTES': '4096',
        })
        self.assertEqual(s.mss_limit, 1000)
        self.assertEqual(s.recv_buffer, 4096)
        self.assertEqual(s.initial_ssthresh, 9000)
        self.assertEqual(s.log_level, 2)
        self.assertFalse(s.trace)
        self.assertEqual(s.trace_dir, 'traces')
        self.assertEqual(s.drop_send, frozenset({3, 60}))
        self.assertEqual(s.drop_recv, frozenset({2, 3, 4}))
        self.assertEqual(s.loss_rate, 0.25)
        self.assertEqual(s.loss_seed, 42)
        self.assertEqual(s.app_read_delay, 0.05)
        self.assertEqual(s.app_read_bytes, 4096)

    def test_trace_other_values_stay_on(self):
        self.assertTrue(load_settings({'STREAPROUS_TRACE': '1'}).trace)
        self.assertTrue(load_settings({'STREAPROUS_TRACE': 'yes'}).trace)

    def test_seed_text(self):
        self.assertEqual(load_settings({'STREAPROUS_LOSS_SEED': 'demo'}).loss_seed, 'demo')

    def test_range_edges(self):
        self.assertEqual(load_settings({'STREAPROUS_MSS': '536'}).mss_limit, 536)
        self.assertEqual(load_settings({'STREAPROUS_RCVBUF': '1'}).recv_buffer, 1)
        self.assertEqual(load_settings({'STREAPROUS_RCVBUF': '65535'}).recv_buffer, 65535)
        self.assertEqual(load_settings({'STREAPROUS_LOSS': '1'}).loss_rate, 1.0)
        self.assertEqual(load_settings({'STREAPROUS_TCP_LOG': '0'}).log_level, 0)
        self.assertEqual(load_settings({'STREAPROUS_APP_READ_DELAY': '10'}).app_read_delay, 10.0)

    def test_errors_name_the_variable(self):
        bad = {
            'STREAPROUS_MSS': ['535', '1461', 'big'],
            'STREAPROUS_RCVBUF': ['0', '65536', 'x'],
            'STREAPROUS_SSTHRESH': ['0', '1000000001', 'x'],
            'STREAPROUS_TCP_LOG': ['3', '-1', 'x'],
            'STREAPROUS_DROP_SEND': ['a', '5-3'],
            'STREAPROUS_DROP_RECV': ['0'],
            'STREAPROUS_LOSS': ['1.1', '-0.1', 'x'],
            'STREAPROUS_APP_READ_DELAY': ['10.5', '-1', 'x'],
            'STREAPROUS_APP_READ_BYTES': ['-1', 'x'],
        }
        for name, values in bad.items():
            for value in values:
                with self.assertRaises(ValueError, msg=(name, value)) as ctx:
                    load_settings({name: value})
                self.assertIn(name, str(ctx.exception))


class DescribeTest(unittest.TestCase):
    """describe summarises the settings."""

    def test_defaults(self):
        text = describe(load_settings({}))
        self.assertTrue(text.startswith('mss_limit=1460 rcvbuf=32768 ssthresh=32768 '))
        self.assertIn('drop_send= ', text)
        self.assertIn('loss=0.0', text)

    def test_drop_list_sorted(self):
        text = describe(load_settings({'STREAPROUS_DROP_SEND': '60,3'}))
        self.assertIn('drop_send=3,60 ', text)


class ModuleValuesTest(unittest.TestCase):
    """The module level constants follow the settings."""

    def test_recv_buffer(self):
        self.assertEqual(tcp_config.RECV_BUFFER, tcp_config.SETTINGS.recv_buffer)


if __name__ == '__main__':
    unittest.main()

"""Tests for streaprous_socket.tcp_trace."""

import csv
import os
import shutil
import tempfile
import unittest

from streaprous_socket.tcp_trace import CSV_COLUMNS, EVENT_LEVELS, TcpTrace, format_line

STATE = {'cwnd': 1460, 'ssthresh': 32768, 'rwnd': 64240, 'in_flight': 2920,
         'snd_una': 2001, 'snd_nxt': 4921, 'adv_wnd': 32768}

EXPECTED_EVENTS = [
    'OPEN', 'SEND', 'RETRANSMIT', 'ACK', 'DUPACK', 'FAST_RETRANSMIT', 'TIMEOUT', 'RTT',
    'RWND_ZERO', 'RWND_OPEN', 'PROBE', 'FIN_SENT', 'GIVE_UP', 'STATE', 'RECV',
    'OUT_OF_ORDER', 'DUP_SEGMENT', 'OUT_OF_WINDOW', 'FIN_RECEIVED', 'APP_READ', 'ACK_SENT',
    'DUP_ACK_SENT', 'WINDOW_ZERO', 'WINDOW_UPDATE', 'DROP', 'RESET', 'IGNORED', 'CLOSE']


class FakeClock:
    """A clock the test moves by hand."""

    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


class TablesTest(unittest.TestCase):
    """CSV_COLUMNS and EVENT_LEVELS."""

    def test_columns(self):
        self.assertEqual(CSV_COLUMNS, ('time', 'event', 'cwnd', 'ssthresh', 'rwnd',
                                       'in_flight', 'snd_una', 'snd_nxt', 'adv_wnd'))

    def test_every_event_is_listed(self):
        self.assertEqual(sorted(EVENT_LEVELS), sorted(EXPECTED_EVENTS))

    def test_levels(self):
        self.assertEqual(EVENT_LEVELS['DROP'], 0)
        for name in ('SEND', 'ACK', 'RTT', 'RECV', 'APP_READ', 'ACK_SENT', 'IGNORED'):
            self.assertEqual(EVENT_LEVELS[name], 2, name)
        for name in ('OPEN', 'TIMEOUT', 'STATE', 'CLOSE', 'RETRANSMIT', 'WINDOW_UPDATE'):
            self.assertEqual(EVENT_LEVELS[name], 1, name)


class FormatLineTest(unittest.TestCase):
    """format_line layout."""

    def test_details_order_and_floats(self):
        line = format_line('1.2.3.4:5555', 'TIMEOUT',
                           [('seq', 2001), ('len', 1460), ('rto', 0.3625)], STATE)
        self.assertEqual(
            line, '[Streap_tcp] 1.2.3.4:5555 TIMEOUT seq=2001 len=1460 rto=0.362 | '
                  'cwnd=1460 ssthresh=32768 rwnd=64240 flight=2920 adv=32768')

    def test_float_rounds_to_three_decimals(self):
        line = format_line('c', 'RTT', [('sample', 0.1), ('rto', 1.0)], STATE)
        self.assertIn('sample=0.100 rto=1.000 |', line)

    def test_no_details(self):
        line = format_line('c', 'RESET', [], STATE)
        self.assertEqual(line, '[Streap_tcp] c RESET | cwnd=1460 ssthresh=32768 rwnd=64240 '
                               'flight=2920 adv=32768')

    def test_detail_order_is_kept(self):
        line = format_line('c', 'X', [('b', 1), ('a', 2), ('c', 'text')], STATE)
        self.assertIn('X b=1 a=2 c=text |', line)


class LoggingTest(unittest.TestCase):
    """TcpTrace printing."""

    def make(self, level):
        self.lines = []
        return TcpTrace('c:1', level, write=self.lines.append, clock=FakeClock())

    def test_event_uses_keyword_order(self):
        trace = self.make(2)
        trace.event('TIMEOUT', STATE, seq=5, len=6, rto=2.0)
        self.assertEqual(len(self.lines), 1)
        self.assertIn('TIMEOUT seq=5 len=6 rto=2.000 |', self.lines[0])
        self.assertTrue(self.lines[0].startswith('[Streap_tcp] c:1 '))

    def test_level_zero_prints_only_drop(self):
        trace = self.make(0)
        for name in EXPECTED_EVENTS:
            trace.event(name, STATE)
        self.assertEqual(len(self.lines), 1)
        self.assertIn(' DROP ', self.lines[0])

    def test_level_one(self):
        trace = self.make(1)
        for name in EXPECTED_EVENTS:
            trace.event(name, STATE)
        expected = [n for n in EXPECTED_EVENTS if EVENT_LEVELS[n] <= 1]
        self.assertEqual(len(self.lines), len(expected))
        self.assertFalse(any(' SEND ' in line for line in self.lines))

    def test_level_two_prints_everything(self):
        trace = self.make(2)
        for name in EXPECTED_EVENTS:
            trace.event(name, STATE)
        self.assertEqual(len(self.lines), len(EXPECTED_EVENTS))


class CsvTest(unittest.TestCase):
    """TcpTrace CSV file."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_header_and_one_row_per_event_at_every_level(self):
        for level in (0, 1, 2):
            path = os.path.join(self.tmp, 'sub', 'trace_%d.csv' % level)
            clock = FakeClock(10.0)
            trace = TcpTrace('c', level, csv_path=path, clock=clock, write=lambda line: None)
            clock.now = 10.5
            trace.event('SEND', STATE, seq=1, len=2)
            clock.now = 11.23456
            trace.event('DROP', STATE, dir='send')
            trace.event('RESET', dict(STATE, adv_wnd=0))
            trace.close()
            with open(path, newline='') as f:
                rows = list(csv.reader(f))
            self.assertEqual(tuple(rows[0]), CSV_COLUMNS)
            self.assertEqual(len(rows), 4, level)
            self.assertEqual(rows[1], ['0.5000', 'SEND', '1460', '32768', '64240', '2920',
                                       '2001', '4921', '32768'])
            self.assertEqual(rows[2][:2], ['1.2346', 'DROP'])
            self.assertEqual(rows[3][-1], '0')

    def test_flushed_after_every_row(self):
        path = os.path.join(self.tmp, 'trace.csv')
        trace = TcpTrace('c', 1, csv_path=path, clock=FakeClock(), write=lambda line: None)
        trace.event('OPEN', STATE)
        with open(path) as f:
            self.assertEqual(len(f.read().splitlines()), 2)
        trace.close()

    def test_file_created_on_first_event(self):
        path = os.path.join(self.tmp, 'later', 'trace.csv')
        trace = TcpTrace('c', 1, csv_path=path, clock=FakeClock(), write=lambda line: None)
        self.assertFalse(os.path.exists(path))
        trace.event('OPEN', STATE)
        self.assertTrue(os.path.exists(path))
        trace.close()

    def test_no_event_no_file(self):
        path = os.path.join(self.tmp, 'none.csv')
        trace = TcpTrace('c', 1, csv_path=path, clock=FakeClock(), write=lambda line: None)
        trace.close()
        self.assertFalse(os.path.exists(path))

    def test_csv_path_none_writes_no_file(self):
        trace = TcpTrace('c', 2, csv_path=None, clock=FakeClock(), write=lambda line: None)
        trace.event('OPEN', STATE)
        trace.close()
        self.assertEqual(os.listdir(self.tmp), [])

    def test_close_twice(self):
        path = os.path.join(self.tmp, 'trace.csv')
        trace = TcpTrace('c', 1, csv_path=path, clock=FakeClock(), write=lambda line: None)
        trace.event('OPEN', STATE)
        trace.close()
        trace.close()


if __name__ == '__main__':
    unittest.main()

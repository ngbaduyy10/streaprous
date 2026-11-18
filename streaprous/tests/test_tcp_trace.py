"""Tests for streaprous_socket.tcp_trace."""

import csv
import os
import shutil
import tempfile
import unittest

from streaprous_socket.tcp_trace import CSV_COLUMNS, QUIET_EVENTS, TcpTrace, format_line, no_log

STATE = {'cwnd': 1460, 'ssthresh': 32768, 'rwnd': 64240, 'in_flight': 2920,
         'send_base': 2001, 'next_seq_num': 4921, 'adv_rwnd': 32768}


class FakeClock:
    """A clock the test moves by hand."""

    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


class FormatLineTest(unittest.TestCase):
    """format_line layout."""

    def test_details_order_and_floats(self):
        line = format_line('1.2.3.4:5555', 'TIMEOUT',
                           [('seq', 2001), ('len', 1460), ('interval', 0.3625)], STATE)
        self.assertEqual(
            line, '[Streap_tcp] 1.2.3.4:5555 TIMEOUT seq=2001 len=1460 interval=0.362 | '
                  'cwnd=1460 ssthresh=32768 rwnd=64240 flight=2920 adv=32768')

    def test_no_details(self):
        line = format_line('c', 'RESET', [], STATE)
        self.assertEqual(line, '[Streap_tcp] c RESET | cwnd=1460 ssthresh=32768 rwnd=64240 '
                               'flight=2920 adv=32768')

    def test_detail_order_is_kept(self):
        line = format_line('c', 'X', [('b', 1), ('a', 2), ('c', 'text')], STATE)
        self.assertIn('X b=1 a=2 c=text |', line)


class LoggingTest(unittest.TestCase):
    """TcpTrace printing."""

    def make(self):
        self.lines = []
        return TcpTrace('c:1', write=self.lines.append, clock=FakeClock())

    def test_event_uses_keyword_order(self):
        trace = self.make()
        trace.event('TIMEOUT', STATE, seq=5, len=6, interval=2.0)
        self.assertEqual(len(self.lines), 1)
        self.assertIn('TIMEOUT seq=5 len=6 interval=2.000 |', self.lines[0])
        self.assertTrue(self.lines[0].startswith('[Streap_tcp] c:1 '))

    def test_quiet_events_set(self):
        self.assertEqual(QUIET_EVENTS, frozenset(
            ['SEND', 'ACK', 'RTT', 'RECV', 'APP_READ', 'ACK_SENT']))

    def test_quiet_events_print_nothing(self):
        trace = self.make()
        for name in QUIET_EVENTS:
            trace.event(name, STATE)
        self.assertEqual(self.lines, [])

    def test_other_events_print(self):
        trace = self.make()
        for name in ('OPEN', 'DROP', 'DUPACK', 'CLOSE'):
            trace.event(name, STATE)
        self.assertEqual(len(self.lines), 4)

    def test_no_log_does_nothing(self):
        self.assertIsNone(no_log('TIMEOUT', seq=1))


class CsvTest(unittest.TestCase):
    """TcpTrace CSV file."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def make(self, path, clock=None):
        return TcpTrace('c', csv_path=path, clock=clock or FakeClock(),
                        write=lambda line: None)

    def test_csv_columns(self):
        self.assertEqual(CSV_COLUMNS, ('time', 'event', 'cwnd', 'ssthresh', 'rwnd',
                                       'in_flight', 'send_base', 'next_seq_num', 'adv_rwnd'))
        path = os.path.join(self.tmp, 'sub', 'trace.csv')
        clock = FakeClock(10.0)
        trace = self.make(path, clock)
        clock.now = 10.5
        trace.event('SEND', STATE, seq=1, len=2)
        clock.now = 11.23456
        trace.event('DROP', STATE)
        trace.event('RESET', dict(STATE, adv_rwnd=0))
        trace.close()
        with open(path, newline='') as f:
            rows = list(csv.reader(f))
        self.assertEqual(tuple(rows[0]), CSV_COLUMNS)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[1], ['0.5000', 'SEND', '1460', '32768', '64240', '2920',
                                   '2001', '4921', '32768'])
        self.assertEqual(rows[2][:2], ['1.2346', 'DROP'])
        self.assertEqual(rows[3][-1], '0')

    def test_quiet_events_go_only_to_csv(self):
        """Quiet events add a CSV row but print no log line."""
        lines = []
        path = os.path.join(self.tmp, 'trace.csv')
        trace = TcpTrace('c', csv_path=path, write=lines.append, clock=FakeClock())
        trace.event('SEND', STATE, seq=1, len=2)
        trace.event('TIMEOUT', STATE, seq=1, len=2, interval=1.0)
        trace.close()
        self.assertEqual(len(lines), 1)
        self.assertIn(' TIMEOUT ', lines[0])
        with open(path, newline='') as f:
            events = [row[1] for row in csv.reader(f)][1:]
        self.assertEqual(events, ['SEND', 'TIMEOUT'])

    def test_file_created_on_first_event_and_closed(self):
        path = os.path.join(self.tmp, 'later', 'trace.csv')
        trace = self.make(path)
        self.assertFalse(os.path.exists(path))
        trace.event('OPEN', STATE)
        self.assertTrue(os.path.exists(path))
        handle = trace.file
        self.assertFalse(handle.closed)
        trace.close()
        self.assertTrue(handle.closed)

    def test_no_event_no_file(self):
        path = os.path.join(self.tmp, 'none.csv')
        self.make(path).close()
        self.assertFalse(os.path.exists(path))

    def test_csv_path_none_writes_no_file(self):
        trace = TcpTrace('c', csv_path=None, clock=FakeClock(), write=lambda line: None)
        trace.event('OPEN', STATE)
        trace.close()
        self.assertEqual(os.listdir(self.tmp), [])

    def test_close_twice(self):
        trace = self.make(os.path.join(self.tmp, 'trace.csv'))
        trace.event('OPEN', STATE)
        trace.close()
        trace.close()


if __name__ == '__main__':
    unittest.main()

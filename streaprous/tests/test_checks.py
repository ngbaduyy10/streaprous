"""
test_checks
~~~~~~~~~~~

Tests of the pure helpers in docker/checks.py, on small sample text and a
temporary CSV trace.
"""

import os
import sys
import tempfile
import unittest

STREAP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(STREAP_DIR, 'docker'))

import checks  # noqa: E402

LOG = '\n'.join([
    '[Streap_raw] accept: Waiting for incoming connections on 172.28.0.10:9000',
    '[Streap_tcp] 172.28.0.20:51892 OPEN iss=2000 irs=357825662 mss=1460 | cwnd=1460 '
    'ssthresh=32768 rwnd=64240 flight=0 adv=32768',
    '[Streap_tcp] 172.28.0.20:51892 DROP dir=send why=nth=3 seq=5841 len=1460 | cwnd=4380 '
    'ssthresh=32768 rwnd=64240 flight=4380 adv=32768',
    '[Streap_tcp] 172.28.0.20:51892 DROP dir=send why=random seq=9000 len=1460 | cwnd=4380 '
    'ssthresh=32768 rwnd=64240 flight=4380 adv=32768',
    '[Streap_tcp] 172.28.0.20:51892 RWND_OPEN rwnd=2896 | cwnd=1460 ssthresh=32768 rwnd=2896 '
    'flight=0 adv=32768',
    '[Streap_tcp] 172.28.0.20:51892 STATE old=SLOW_START new=CONGESTION_AVOIDANCE | cwnd=32768 '
    'ssthresh=32768 rwnd=64240 flight=0 adv=32768',
    '[HttpAdapter] Read request: header 3126 bytes, body 0 bytes, sha256 ' + 'a' * 64,
    '[HttpAdapter] Read request: header 180 bytes, body 150000 bytes, sha256 ' + 'b' * 64,
    '[Streap_tcp] 172.28.0.20:51892 CLOSE sent=1048576 retransmits=2 | cwnd=1460 '
    'ssthresh=2920 rwnd=64240 flight=0 adv=32768',
])

PCAP = '\n'.join([
    '12:00:00.000000 IP 172.28.0.20.51892 > 172.28.0.10.9000: Flags [S], seq 357825662, '
    'win 64240, options [mss 1460,sackOK,TS val 1 ecr 0,nop,wscale 7], length 0',
    '12:00:00.000100 IP 172.28.0.10.9000 > 172.28.0.20.51892: Flags [S.], seq 2000, '
    'ack 357825663, win 32768, length 0',
    '12:00:00.001000 IP 172.28.0.10.9000 > 172.28.0.20.51892: Flags [P.], seq 2001:3461, '
    'ack 357825663, win 32768, length 1460',
    '12:00:00.002000 IP 172.28.0.10.9000 > 172.28.0.20.51892: Flags [.], seq 3461:4921, '
    'ack 357825663, win 32768, length 1460',
    '12:00:00.002500 IP 172.28.0.10.9000 > 172.28.0.20.51892: Flags [P.], seq 4921:5000, '
    'ack 357825663, win 32768, length 79',
    '12:00:00.003000 IP 172.28.0.20.51892 > 172.28.0.10.9000: Flags [.], ack 3461, '
    'win 64240, length 0',
    '12:00:00.004000 IP 172.28.0.20.51892 > 172.28.0.10.9000: Flags [P.], seq 357825663:'
    '357826663, ack 3461, win 64240, length 1000',
])

PCAP_ZERO = PCAP + '\n' + '\n'.join([
    '12:00:02.000000 IP 172.28.0.20.51892 > 172.28.0.10.9000: Flags [.], ack 5000, '
    'win 0, length 0',
    '12:00:02.100000 IP 172.28.0.10.9000 > 172.28.0.20.51892: Flags [R], seq 5000, '
    'win 0, length 0',
])

CSV_HEAD = 'time,event,cwnd,ssthresh,rwnd,in_flight,snd_una,snd_nxt,adv_wnd\n'


def write_trace(directory, name, lines):
    """
    Write a trace CSV into a folder.

    Args:
        directory (str): target folder.
        name (str): file name.
        lines (list): data rows as text (the header is added).

    Returns:
        the file's path.
    """
    path = os.path.join(directory, name)
    with open(path, 'w', newline='') as handle:
        handle.write(CSV_HEAD + ''.join(line + '\n' for line in lines))
    return path


class LogHelpersTest(unittest.TestCase):
    """Event counting and log scraping."""

    def test_count_events_matches_whole_event_names(self):
        self.assertEqual(checks.count_events(LOG, 'DROP'), 2)
        self.assertEqual(checks.count_events(LOG, 'OPEN'), 1)
        self.assertEqual(checks.count_events(LOG, 'RWND_OPEN'), 1)
        self.assertEqual(checks.count_events(LOG, 'TIMEOUT'), 0)

    def test_event_lines_order_and_limit(self):
        lines = checks.event_lines(LOG, 'DROP')
        self.assertEqual(len(lines), 2)
        self.assertIn('why=nth=3', lines[0])
        self.assertIn('why=random', lines[1])
        self.assertEqual(checks.event_lines(LOG, 'DROP', 1), lines[:1])

    def test_state_change_seen(self):
        self.assertTrue(checks.state_change_seen(LOG, 'SLOW_START', 'CONGESTION_AVOIDANCE'))
        self.assertFalse(checks.state_change_seen(LOG, 'CONGESTION_AVOIDANCE', 'SLOW_START'))
        self.assertFalse(checks.state_change_seen('', 'SLOW_START', 'FAST_RECOVERY'))

    def test_request_reads(self):
        self.assertEqual(checks.request_reads(LOG), [(3126, 0, 'a' * 64),
                                                     (180, 150000, 'b' * 64)])
        self.assertEqual(checks.request_reads('nothing here'), [])

    def test_parse_result_takes_the_last_line(self):
        text = 'noise\nRESULT method=GET status=500\nmore\nRESULT method=GET status=200 ' \
               'body_bytes=235 expect=ok\n'
        self.assertEqual(checks.parse_result(text), {
            'method': 'GET', 'status': '200', 'body_bytes': '235', 'expect': 'ok'})
        self.assertEqual(checks.parse_result('no result line'), {})


class TcpdumpHelpersTest(unittest.TestCase):
    """Reading tcpdump -nn -S text."""

    def test_max_payload_by_source_port(self):
        self.assertEqual(checks.max_payload(PCAP, 9000), 1460)
        self.assertEqual(checks.max_payload(PCAP, 51892), 1000)
        self.assertEqual(checks.max_payload(PCAP, 1234), 0)
        self.assertEqual(checks.max_payload('', 9000), 0)

    def test_zero_window_sent(self):
        self.assertFalse(checks.zero_window_sent(PCAP, 9000))
        # the client's own "win 0" and a server reset do not count for port 9000
        self.assertFalse(checks.zero_window_sent(PCAP_ZERO, 9000))
        self.assertTrue(checks.zero_window_sent(PCAP_ZERO, 51892))
        extra = PCAP + '\n12:00:03.000000 IP 172.28.0.10.9000 > 172.28.0.20.51892: ' \
            'Flags [.], seq 5000, ack 357826663, win 0, length 0'
        self.assertTrue(checks.zero_window_sent(extra, 9000))


class TraceHelpersTest(unittest.TestCase):
    """CSV traces: reading, choosing the largest, invariants."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_read_trace_converts_numbers(self):
        path = write_trace(self.tmp.name, 'trace_a_1.csv', [
            '0.0000,OPEN,1460,32768,64240,0,2001,2001,32768',
            '0.0123,SEND,1460,32768,64240,1460,2001,3461,',
        ])
        rows = checks.read_trace(path)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['event'], 'OPEN')
        self.assertEqual(rows[1]['time'], 0.0123)
        self.assertEqual(rows[1]['cwnd'], 1460)
        self.assertIsNone(rows[1]['adv_wnd'])

    def test_largest_trace(self):
        self.assertIsNone(checks.largest_trace(self.tmp.name))
        small = write_trace(self.tmp.name, 'trace_a_1.csv', ['0.0,OPEN,1,1,1,0,0,0,0'])
        big = write_trace(self.tmp.name, 'trace_a_2.csv',
                          ['0.0,OPEN,1,1,1,0,0,0,0'] * 5)
        with open(os.path.join(self.tmp.name, 'other.csv'), 'w') as handle:
            handle.write('x' * 5000)
        self.assertEqual(checks.largest_trace(self.tmp.name), big)
        self.assertNotEqual(checks.largest_trace(self.tmp.name), small)

    def test_window_invariant_violations(self):
        path = write_trace(self.tmp.name, 'trace_a_1.csv', [
            '0.1,SEND,2920,32768,64240,2920,1,2921,32768',         # equals cwnd: allowed
            '0.2,RETRANSMIT,2920,32768,1460,1460,1,1461,32768',    # equals rwnd: allowed
            '0.3,SEND,2920,32768,64240,4380,1,4381,32768',         # over cwnd
            '0.4,ACK,2920,32768,1460,9999,1,4381,32768',           # not a send row
            '0.5,RETRANSMIT,8760,32768,2920,4380,1,4381,32768',    # over rwnd
        ])
        bad = checks.window_invariant_violations(checks.read_trace(path))
        self.assertEqual([row['time'] for row in bad], [0.3, 0.5])
        self.assertEqual(checks.window_invariant_violations([]), [])

    def test_adv_zero_then_open(self):
        path = write_trace(self.tmp.name, 'trace_a_1.csv', [
            '0.1,SEND,1,1,1,0,1,1,16384',
            '0.2,WINDOW_ZERO,1,1,1,0,1,1,0',
            '0.3,WINDOW_UPDATE,1,1,1,0,1,1,2048',
            '0.4,WINDOW_UPDATE,1,1,1,0,1,1,6144',
        ])
        rows = checks.read_trace(path)
        self.assertTrue(checks.adv_zero_then_open(rows, 4096))
        self.assertFalse(checks.adv_zero_then_open(rows, 8192))
        self.assertFalse(checks.adv_zero_then_open(rows[:1], 1))
        # a window that opens before it closes does not count
        self.assertFalse(checks.adv_zero_then_open([rows[0], rows[3], rows[1]], 4096))


if __name__ == '__main__':
    unittest.main()

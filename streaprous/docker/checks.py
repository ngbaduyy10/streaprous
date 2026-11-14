"""
checks
~~~~~~

Pure helpers that read what a Docker lab run leaves behind: the server log, the
per-connection CSV trace and the tcpdump text. They never touch Docker or the
network, so they run (and are tested) on any host with Python 3.6 or later.
"""

import csv
import glob
import os
import re

# Columns of the CSV trace that hold numbers (the other column is "event").
NUMERIC_COLUMNS = ('time', 'cwnd', 'ssthresh', 'rwnd', 'in_flight',
                   'snd_una', 'snd_nxt', 'adv_wnd')

# A tcpdump -nn line: "<time> IP a.b.c.d.sport > e.f.g.h.dport: Flags [..], ..."
TCPDUMP_LINE = re.compile(
    r'^\S+ IP (\d+\.\d+\.\d+\.\d+)\.(\d+) > (\d+\.\d+\.\d+\.\d+)\.(\d+): '
    r'Flags \[([^\]]*)\](.*)$')

READ_LINE = re.compile(
    r'\[HttpAdapter\] Read request: header (\d+) bytes, body (\d+) bytes, sha256 ([0-9a-f]+)')


def _event_pattern(event):
    """
    Build the regular expression of one log line of an event.

    A line reads "[Streap_tcp] <ip>:<port> <EVENT> k=v ... | cwnd=..."; the
    peer token is optional so that a line without it still counts.

    Args:
        event (str): upper-case event name, such as "TIMEOUT".

    Returns:
        compiled regular expression.
    """
    return re.compile(r'\[Streap_tcp\] (?:\S+ )?' + re.escape(event) + r'(?=\s|$)')


def event_lines(log, event, limit=None):
    """
    Return the log lines of one event, in order.

    Args:
        log (str): the server log.
        event (str): upper-case event name.
        limit (int): keep only the first this many lines (None keeps all).

    Returns:
        list of str.
    """
    pattern = _event_pattern(event)
    lines = [line for line in log.splitlines() if pattern.search(line)]
    if limit is not None:
        lines = lines[:limit]
    return lines


def count_events(log, event):
    """
    Count the log lines of one event.

    Args:
        log (str): the server log.
        event (str): upper-case event name.

    Returns:
        int.
    """
    return len(event_lines(log, event))


def _number(text):
    """
    Turn a CSV cell into an int, a float, or None when it is empty.

    Args:
        text (str): the cell.

    Returns:
        int, float or None.
    """
    if text is None or text.strip() == '':
        return None
    try:
        return int(text)
    except ValueError:
        return float(text)


def read_trace(path):
    """
    Read a per-connection CSV trace.

    Args:
        path (str): the CSV file.

    Returns:
        list of dict, one per row; numeric columns are int/float (None when
        the cell is empty or the column is missing), "event" stays a string.
    """
    rows = []
    with open(path, newline='') as handle:
        for raw in csv.DictReader(handle):
            row = {'event': raw.get('event', '')}
            for name in NUMERIC_COLUMNS:
                row[name] = _number(raw.get(name))
            rows.append(row)
    return rows


def largest_trace(directory):
    """
    Find the biggest trace file of a run (the long transfer, not a short probe).

    Args:
        directory (str): folder that holds trace_<ip>_<port>.csv files.

    Returns:
        path of the largest file, or None when there is none.
    """
    paths = glob.glob(os.path.join(directory, 'trace_*.csv'))
    if not paths:
        return None
    return max(sorted(paths), key=os.path.getsize)


def window_invariant_violations(rows):
    """
    Find sends that put more bytes in flight than the sending window allows.

    Args:
        rows (list): rows from read_trace.

    Returns:
        list of the SEND / RETRANSMIT rows whose in_flight exceeds
        min(cwnd, rwnd); empty when the sender obeyed both windows.
    """
    bad = []
    for row in rows:
        if row['event'] not in ('SEND', 'RETRANSMIT'):
            continue
        values = (row['in_flight'], row['cwnd'], row['rwnd'])
        if None in values:
            continue
        if row['in_flight'] > min(row['cwnd'], row['rwnd']):
            bad.append(row)
    return bad


def _segments_from(tcpdump_text, src_port):
    """
    Yield the parsed tcpdump lines sent from one source port.

    Args:
        tcpdump_text (str): output of tcpdump -nn -S -r.
        src_port (int): source port to keep.

    Returns:
        generator of (flags, rest-of-line) tuples.
    """
    for line in tcpdump_text.splitlines():
        match = TCPDUMP_LINE.match(line.strip())
        if match and int(match.group(2)) == int(src_port):
            yield match.group(5), match.group(6)


def max_payload(tcpdump_text, src_port):
    """
    Find the largest TCP payload sent from a port.

    Args:
        tcpdump_text (str): output of tcpdump -nn -S -r.
        src_port (int): source port, such as the server's 9000.

    Returns:
        int, the largest "length N"; 0 when the port sent nothing.
    """
    largest = 0
    for _flags, rest in _segments_from(tcpdump_text, src_port):
        found = re.search(r'length (\d+)', rest)
        if found:
            largest = max(largest, int(found.group(1)))
    return largest


def zero_window_sent(tcpdump_text, src_port):
    """
    Tell whether a port advertised a zero window (slide 3-94).

    A reset also reads "win 0", so segments with the R flag do not count.

    Args:
        tcpdump_text (str): output of tcpdump -nn -S -r.
        src_port (int): source port, such as the server's 9000.

    Returns:
        True when a non-reset segment from the port carried "win 0".
    """
    for flags, rest in _segments_from(tcpdump_text, src_port):
        if 'R' in flags:
            continue
        if re.search(r'\bwin 0\b', rest):
            return True
    return False


def adv_zero_then_open(rows, reopen_at_least):
    """
    Tell whether the advertised window fell to zero and later opened again.

    Args:
        rows (list): rows from read_trace.
        reopen_at_least (int): window in bytes that counts as open.

    Returns:
        True when a row with adv_wnd 0 is followed by a row with
        adv_wnd >= reopen_at_least.
    """
    seen_zero = False
    for row in rows:
        window = row['adv_wnd']
        if window is None:
            continue
        if window == 0:
            seen_zero = True
        elif seen_zero and window >= reopen_at_least:
            return True
    return False


def parse_result(client_text):
    """
    Read the last RESULT line a demo client printed.

    Args:
        client_text (str): the client's output.

    Returns:
        dict of the k=v words (values stay strings); empty when there is no
        RESULT line.
    """
    result = {}
    for line in client_text.splitlines():
        if line.startswith('RESULT '):
            result = {}
            for word in line[len('RESULT '):].split():
                key, _sep, value = word.partition('=')
                result[key] = value
    return result


def request_reads(log):
    """
    List the requests the server's HTTP adapter read.

    Args:
        log (str): the server log.

    Returns:
        list of (header_bytes, body_bytes, sha256) tuples in log order.
    """
    return [(int(h), int(b), s) for h, b, s in READ_LINE.findall(log)]


def state_change_seen(log, old, new):
    """
    Tell whether a congestion-control STATE event changed old to new.

    Args:
        log (str): the server log.
        old (str): state name, such as "SLOW_START".
        new (str): state name, such as "CONGESTION_AVOIDANCE".

    Returns:
        True when some STATE line reads "old=<old> new=<new>".
    """
    wanted = re.compile(r'\bold=' + re.escape(old) + r'\b.*\bnew=' + re.escape(new) + r'\b')
    return any(wanted.search(line) for line in event_lines(log, 'STATE'))

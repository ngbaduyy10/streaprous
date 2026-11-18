"""
StreapRous TCP trace
Prints the [Streap_tcp] log lines and writes the per-connection CSV trace.
"""

import csv
import os
import time

CSV_COLUMNS = ('time', 'event', 'cwnd', 'ssthresh', 'rwnd', 'in_flight',
               'send_base', 'next_seq_num', 'adv_rwnd')

# These events go to the CSV only: one per segment would flood the log.
QUIET_EVENTS = frozenset(['SEND', 'ACK', 'RTT', 'RECV', 'APP_READ', 'ACK_SENT'])


def no_log(event, **details):
    """Ignores one log event; the default log of the TCP classes."""
    return None


def format_line(name, event, details, state):
    """Builds one log line.

    Args:
         name     connection name, "<client_ip>:<client_port>"
         event    event name
         details  list of (key, value) pairs, printed in this order
         state    dict with the numeric CSV keys

    Returns:
         the line, starting with [Streap_tcp]
    """
    parts = ['[Streap_tcp]', name, event]
    for key, value in details:
        if isinstance(value, float):
            value = '%.3f' % value
        parts.append('%s=%s' % (key, value))
    parts.append('| cwnd=%s ssthresh=%s rwnd=%s flight=%s adv=%s' % (
        state.get('cwnd', ''), state.get('ssthresh', ''), state.get('rwnd', ''),
        state.get('in_flight', ''), state.get('adv_rwnd', '')))
    return ' '.join(parts)


class TcpTrace:
    """Logs events and records them in a CSV file.

    Args:
         name      connection name, "<client_ip>:<client_port>"
         csv_path  file to write, or None for no file
         write     function that prints one line
         clock     function returning seconds
    """

    def __init__(self, name, csv_path=None, write=print, clock=time.monotonic):
        """Sets up the trace.

        Args:
             name      connection name for log lines, such as the client address
             csv_path  file to write, or None for no file
             write     function that prints one line
             clock     function returning seconds
        """
        self.name = name
        self.csv_path = csv_path
        self.write = write
        self.clock = clock
        self.start = clock()
        self.file = None
        self.writer = None

    def event(self, event, state, **details):
        """Records one event: prints it unless it is quiet, and adds a CSV row.

        Args:
             event    event name
             state    dict with the numeric CSV keys
             details  extra key=value pairs for the log line
        """
        if event not in QUIET_EVENTS:
            self.write(format_line(self.name, event, list(details.items()), state))
        if self.csv_path is None:
            return
        if self.file is None:
            folder = os.path.dirname(self.csv_path)
            if folder:
                os.makedirs(folder, exist_ok=True)
            self.file = open(self.csv_path, 'w', newline='')
            self.writer = csv.writer(self.file)
            self.writer.writerow(CSV_COLUMNS)
        row = ['%.4f' % (self.clock() - self.start), event]
        row.extend(state.get(key, '') for key in CSV_COLUMNS[2:])
        self.writer.writerow(row)

    def close(self):
        """Closes the CSV file if it was opened."""
        if self.file is not None:
            self.file.close()
            self.file = None

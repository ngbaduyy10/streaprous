"""
StreapRous TCP trace
Prints the [Streap_tcp] log lines and writes the per-connection CSV trace.
"""

import csv
import os
import time

CSV_COLUMNS = ('time', 'event', 'cwnd', 'ssthresh', 'rwnd', 'in_flight',
               'snd_una', 'snd_nxt', 'adv_wnd')

# Level 0 prints only DROP, level 1 the key events, level 2 every segment.
EVENT_LEVELS = {
    'OPEN': 1, 'SEND': 2, 'RETRANSMIT': 1, 'ACK': 2, 'DUPACK': 1,
    'FAST_RETRANSMIT': 1, 'TIMEOUT': 1, 'RTT': 2, 'RWND_ZERO': 1,
    'RWND_OPEN': 1, 'PROBE': 1, 'FIN_SENT': 1, 'GIVE_UP': 1, 'STATE': 1,
    'RECV': 2, 'OUT_OF_ORDER': 1, 'DUP_SEGMENT': 1, 'OUT_OF_WINDOW': 1,
    'FIN_RECEIVED': 1, 'APP_READ': 2, 'ACK_SENT': 2, 'DUP_ACK_SENT': 1,
    'WINDOW_ZERO': 1, 'WINDOW_UPDATE': 1, 'DROP': 0, 'RESET': 1,
    'IGNORED': 2, 'CLOSE': 1,
}


def _value_text(value):
    """Writes one detail value; floats get 3 decimals.

    Args:
         value  number or text
    """
    if isinstance(value, float):
        return '%.3f' % value
    return str(value)


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
    parts.extend('%s=%s' % (key, _value_text(value)) for key, value in details)
    parts.append('| cwnd=%s ssthresh=%s rwnd=%s flight=%s adv=%s' % (
        state.get('cwnd', ''), state.get('ssthresh', ''), state.get('rwnd', ''),
        state.get('in_flight', ''), state.get('adv_wnd', '')))
    return ' '.join(parts)


class TcpTrace:
    """Logs events and records them in a CSV file.

    Args:
         name       connection name, "<client_ip>:<client_port>"
         log_level  0, 1 or 2
         csv_path   file to write, or None for no file
         clock      function returning seconds
         write      function that prints one line
    """

    def __init__(self, name, log_level, csv_path=None, clock=time.monotonic, write=print):
        """Sets up the trace.

        Args:
             name       connection name for log lines, such as the client address
             log_level  0 only DROP, 1 key events, 2 every segment
             csv_path   file to write, or None for no file
             clock      function returning seconds
             write      function that prints one line
        """
        self.name = name
        self.log_level = log_level
        self.csv_path = csv_path
        self.clock = clock
        self.write = write
        self.start = clock()
        self._file = None
        self._writer = None

    def event(self, event, state, **details):
        """Records one event.

        Args:
             event    event name from EVENT_LEVELS
             state    dict with the 7 numeric CSV keys
             details  extra key=value pairs for the log line
        """
        if EVENT_LEVELS.get(event, 1) <= self.log_level:
            self.write(format_line(self.name, event, list(details.items()), state))
        if self.csv_path is not None:
            self._write_row(event, state)

    def _write_row(self, event, state):
        """Appends one CSV row, creating the file on first use.

        Args:
             event  event name
             state  dict with the numeric CSV keys
        """
        if self._file is None:
            folder = os.path.dirname(self.csv_path)
            if folder:
                os.makedirs(folder, exist_ok=True)
            self._file = open(self.csv_path, 'w', newline='')
            self._writer = csv.writer(self._file)
            self._writer.writerow(CSV_COLUMNS)
        row = ['%.4f' % (self.clock() - self.start), event]
        row.extend(state.get(key, '') for key in CSV_COLUMNS[2:])
        self._writer.writerow(row)
        self._file.flush()

    def close(self):
        """Closes the CSV file if it was opened."""
        if self._file is not None:
            self._file.close()
            self._file = None
            self._writer = None

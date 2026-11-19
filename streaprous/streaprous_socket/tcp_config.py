"""
StreapRous TCP settings
Reads the test switches from environment variables, once.
"""

import os
import types


def parse_drop_list(text):
    """Turns a drop list such as "3,60,400-402" into a set of numbers.

    Args:
         text  comma separated numbers and inclusive ranges, spaces allowed

    Returns:
         frozenset of ints; empty text gives an empty set
    """
    numbers = set()
    for part in text.split(','):
        part = part.strip()
        if not part:
            continue
        low, dash, high = part.partition('-')
        try:
            first = int(low.strip())
            last = int(high.strip()) if dash else first
            if first < 1 or last < first:
                raise ValueError(part)
        except ValueError:
            raise ValueError("bad drop list item %r" % part)
        numbers.update(range(first, last + 1))
    return frozenset(numbers)


def read_number(environ, name, default, low, high):
    """Reads a number variable and checks its range.

    Args:
         environ  mapping of variable names to text
         name     variable name
         default  value if missing or empty; its type (int or float) is the type read
         low      smallest allowed value, high the largest
    """
    text = environ.get(name, '').strip()
    if not text:
        return default
    try:
        value = type(default)(text)
        if not low <= value <= high:
            raise ValueError(text)
    except ValueError:
        raise ValueError("%s must be a number from %s to %s, got %r" % (name, low, high, text))
    return value


def load_settings(environ):
    """Builds the settings object from environment variables.

    Args:
         environ  mapping of variable names to text, such as os.environ

    Returns:
         types.SimpleNamespace with one field per setting
    """
    seed = environ.get('STREAPROUS_LOSS_SEED', '').strip()
    try:
        drop_send = parse_drop_list(environ.get('STREAPROUS_DROP_SEND', ''))
    except ValueError as err:
        raise ValueError("STREAPROUS_DROP_SEND: %s" % err)
    return types.SimpleNamespace(
        # RcvBuffer for flow control (slide 3-94, book 3.5.5). Upper limit 65535
        # because the receive window field in the TCP header is 16 bits (book 3.5.2).
        rcv_buffer=read_number(environ, 'STREAPROUS_RCVBUF', 4096, 1, 65535),
        drop_send=drop_send,
        loss_rate=read_number(environ, 'STREAPROUS_LOSS', 0.0, 0.0, 1.0),
        loss_seed=int(seed) if seed.isdigit() else seed or None,
        trace=environ.get('STREAPROUS_TRACE', '1').strip() != '0',
        trace_dir=environ.get('STREAPROUS_TRACE_DIR', '').strip() or 'out',
        app_read_delay=read_number(environ, 'STREAPROUS_APP_READ_DELAY', 0.0, 0.0, 10.0),
        app_read_bytes=read_number(environ, 'STREAPROUS_APP_READ_BYTES', 0, 0, 10 ** 9),
        request_timeout=30.0,
    )


SETTINGS = load_settings(os.environ)
RECV_BUFFER = SETTINGS.rcv_buffer  # default window argument of the header builders

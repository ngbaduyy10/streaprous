"""
StreapRous TCP settings
Reads the demo and tuning switches from environment variables, once.
Used by the TCP modules; no module here imports the raw-connection library.
"""

import os
import types


def parse_drop_list(text):
    """Turns a drop list such as "3,60,400-430" into a set of numbers.

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
        except ValueError:
            raise ValueError("bad drop list item %r" % part)
        if first < 1 or last < first:
            raise ValueError("bad drop list item %r" % part)
        numbers.update(range(first, last + 1))
    return frozenset(numbers)


def _read_int(environ, name, default, low, high):
    """Reads an integer variable and checks its range.

    Args:
         environ  mapping of variable names to text
         name     variable name
         default  value when the variable is missing or empty
         low      smallest allowed value
         high     largest allowed value
    """
    text = environ.get(name, '').strip()
    if not text:
        return default
    try:
        value = int(text)
    except ValueError:
        raise ValueError("%s must be an integer, got %r" % (name, text))
    if not low <= value <= high:
        raise ValueError("%s must be between %d and %d, got %d" % (name, low, high, value))
    return value


def _read_float(environ, name, default, low, high):
    """Reads a number variable and checks its range.

    Args:
         environ  mapping of variable names to text
         name     variable name
         default  value when the variable is missing or empty
         low      smallest allowed value
         high     largest allowed value
    """
    text = environ.get(name, '').strip()
    if not text:
        return default
    try:
        value = float(text)
    except ValueError:
        raise ValueError("%s must be a number, got %r" % (name, text))
    if not low <= value <= high:
        raise ValueError("%s must be between %s and %s, got %s" % (name, low, high, text))
    return value


def _read_drops(environ, name):
    """Reads a drop list variable, naming the variable on error.

    Args:
         environ  mapping of variable names to text
         name     variable name
    """
    try:
        return parse_drop_list(environ.get(name, ''))
    except ValueError as err:
        raise ValueError("%s: %s" % (name, err))


def load_settings(environ):
    """Builds the settings object from environment variables.

    Args:
         environ  mapping of variable names to text, such as os.environ

    Returns:
         types.SimpleNamespace with one field per setting
    """
    seed_text = environ.get('STREAPROUS_LOSS_SEED', '').strip()
    if not seed_text:
        loss_seed = None
    elif seed_text.isdigit():
        loss_seed = int(seed_text)
    else:
        loss_seed = seed_text
    return types.SimpleNamespace(
        mss_limit=_read_int(environ, 'STREAPROUS_MSS', 1460, 536, 1460),
        default_peer_mss=536,
        recv_buffer=_read_int(environ, 'STREAPROUS_RCVBUF', 32768, 1, 65535),
        initial_ssthresh=_read_int(environ, 'STREAPROUS_SSTHRESH', 32768, 1, 10 ** 9),
        initial_rto=1.0,
        min_rto=0.2,
        max_rto=4.0,
        max_retries=8,
        delayed_ack=0.1,
        persist_min=0.2,
        persist_max=2.0,
        linger=2.0,
        tick=0.01,
        peer_silence=60.0,
        request_timeout=30.0,
        log_level=_read_int(environ, 'STREAPROUS_TCP_LOG', 1, 0, 2),
        trace=environ.get('STREAPROUS_TRACE', '1').strip() != '0',
        trace_dir=environ.get('STREAPROUS_TRACE_DIR', '').strip() or 'out',
        drop_send=_read_drops(environ, 'STREAPROUS_DROP_SEND'),
        drop_recv=_read_drops(environ, 'STREAPROUS_DROP_RECV'),
        loss_rate=_read_float(environ, 'STREAPROUS_LOSS', 0.0, 0.0, 1.0),
        loss_seed=loss_seed,
        app_read_delay=_read_float(environ, 'STREAPROUS_APP_READ_DELAY', 0.0, 0.0, 10.0),
        app_read_bytes=_read_int(environ, 'STREAPROUS_APP_READ_BYTES', 0, 0, 10 ** 9),
    )


def _drops_text(numbers):
    """Writes a drop set back as a comma separated list.

    Args:
         numbers  set of ints
    """
    return ','.join(str(n) for n in sorted(numbers))


def describe(settings):
    """Summarises the settings on one line for the OPEN log line.

    Args:
         settings  object returned by load_settings

    Returns:
         text such as "mss_limit=1460 rcvbuf=32768 ssthresh=32768 drop_send=3,60 loss=0.0 ..."
    """
    return ' '.join([
        'mss_limit=%d' % settings.mss_limit,
        'rcvbuf=%d' % settings.recv_buffer,
        'ssthresh=%d' % settings.initial_ssthresh,
        'drop_send=%s' % _drops_text(settings.drop_send),
        'drop_recv=%s' % _drops_text(settings.drop_recv),
        'loss=%s' % settings.loss_rate,
        'loss_seed=%s' % settings.loss_seed,
        'log=%d' % settings.log_level,
        'trace=%s' % ('on' if settings.trace else 'off'),
        'app_read_delay=%s' % settings.app_read_delay,
        'app_read_bytes=%d' % settings.app_read_bytes,
    ])


SETTINGS = load_settings(os.environ)
RECV_BUFFER = SETTINGS.recv_buffer  # default window argument of the header builders

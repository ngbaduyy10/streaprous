"""
client
~~~~~~

Demo client for the StreapRous backend. It uses the operating system's own TCP
(a real Linux or Windows stack), so the server is tested against a standard
peer. Python 3.6+, standard library only.

Commands:
    get URL [--expect NAME | --expect-file PATH] [--rcvbuf N] [--read-bytes N]
            [--read-delay S] [--pause-after N] [--pause S] [--header-bytes N]
            [--timeout S]
    post URL --size N [--timeout S]
    connect-close HOST PORT

The last output line is always "RESULT k=v ..."; the exit code is 0 when the
request (or the handshake) worked and the expectation holds, else 1.
"""

import argparse
import hashlib
import os
import socket
import sys
import time
from urllib.parse import urlsplit

PAD_LENGTH = 1000        # characters in the value of one X-Pad-<i> header
RECV_SIZE = 65536        # bytes asked of recv() when --read-bytes is not given
DEFAULT_TIMEOUT = 60.0


def parse_url(url):
    """
    Split an http URL into the parts the request needs.

    Args:
        url (str): such as "http://172.28.0.10:9000/demo_1m.bin".

    Returns:
        (host, port, path); port defaults to 80 and path to "/".
    """
    parts = urlsplit(url)
    if parts.scheme != 'http' or not parts.hostname:
        raise ValueError('not an http URL: {}'.format(url))
    path = parts.path or '/'
    if parts.query:
        path += '?' + parts.query
    return parts.hostname, parts.port or 80, path


def post_body(size):
    """
    Build the deterministic printable body of a POST.

    Args:
        size (int): body length in bytes.

    Returns:
        bytes of printable ASCII (hex digits and newlines), the same every time.
    """
    pieces = []
    have = 0
    index = 0
    while have < size:
        piece = hashlib.sha256(b'streaprous-upload:' + str(index).encode('ascii'))
        text = piece.hexdigest().encode('ascii') + b'\n'
        pieces.append(text)
        have += len(text)
        index += 1
    return b''.join(pieces)[:size]


def build_request(method, host, port, path, body=b'', header_bytes=0):
    """
    Build one HTTP/1.1 request with Connection: close.

    Args:
        method (str): "GET" or "POST".
        host (str): server address for the Host header.
        port (int): server port for the Host header.
        path (str): request target.
        body (bytes): request body (POST).
        header_bytes (int): when the request head is shorter than this, add
            X-Pad-<i> headers of PAD_LENGTH characters until it is not.

    Returns:
        bytes: head, blank line and body.
    """
    lines = [
        '{} {} HTTP/1.1'.format(method, path),
        'Host: {}:{}'.format(host, port),
        'User-Agent: streaprous-demo',
        'Accept: */*',
        'Connection: close',
    ]
    if method == 'POST':
        lines.append('Content-Type: text/plain')
        lines.append('Content-Length: {}'.format(len(body)))
    index = 0
    while sum(len(line) + 2 for line in lines) + 2 < header_bytes:
        lines.append('X-Pad-{}: {}'.format(index, 'x' * PAD_LENGTH))
        index += 1
    head = '\r\n'.join(lines) + '\r\n\r\n'
    return head.encode('ascii') + body


def parse_response_head(data):
    """
    Read the status line and headers of an HTTP response.

    Args:
        data (bytes): what has been received so far.

    Returns:
        (status, headers, head_length), where headers maps lower-case names to
        values and head_length counts the bytes up to the blank line; None when
        the blank line has not arrived yet.
    """
    end = data.find(b'\r\n\r\n')
    if end < 0:
        return None
    lines = data[:end].decode('iso-8859-1').split('\r\n')
    fields = lines[0].split()
    if len(fields) < 2 or not fields[1].isdigit():
        raise ValueError('bad status line: {!r}'.format(lines[0]))
    headers = {}
    for line in lines[1:]:
        name, _sep, value = line.partition(':')
        headers[name.strip().lower()] = value.strip()
    return int(fields[1]), headers, end + 4


def format_result(pairs):
    """
    Format the final RESULT line.

    Args:
        pairs (list): (key, value) tuples in the order to print.

    Returns:
        str such as "RESULT method=GET status=200".
    """
    return 'RESULT ' + ' '.join('{}={}'.format(key, value) for key, value in pairs)


def open_connection(host, port, timeout, rcvbuf=None):
    """
    Open a TCP connection; the receive buffer must be set before connect.

    Args:
        host (str): server address.
        port (int): server port.
        timeout (float): seconds for each socket operation.
        rcvbuf (int): SO_RCVBUF to request, or None for the system default.

    Returns:
        the connected socket.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if rcvbuf:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
    except OSError:
        sock.close()
        raise
    return sock


def read_response(sock, read_bytes=0, read_delay=0.0, pause_after=0, pause=0.0):
    """
    Read a whole HTTP response, optionally slowly.

    Args:
        sock (socket): connected socket with the request already sent.
        read_bytes (int): bytes asked of each recv (0 means RECV_SIZE).
        read_delay (float): seconds to sleep after each recv.
        pause_after (int): once this many bytes have arrived, sleep `pause`
            seconds one time (0 turns the pause off).
        pause (float): length of that pause.

    Returns:
        dict with status (0 when no head arrived), headers, body_bytes, sha256.
    """
    data = b''
    head = None
    body = hashlib.sha256()
    body_bytes = 0
    total = 0
    paused = False
    expected = None
    while True:
        if head is not None and expected is not None and body_bytes >= expected:
            break
        if pause_after and not paused and total >= pause_after:
            paused = True
            time.sleep(pause)
        chunk = sock.recv(read_bytes or RECV_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if head is None:
            data += chunk
            head = parse_response_head(data)
            if head is None:
                continue
            length = head[1].get('content-length', '')
            expected = int(length) if length.isdigit() else None
            chunk = data[head[2]:]
        body.update(chunk)
        body_bytes += len(chunk)
        if read_delay > 0:
            time.sleep(read_delay)
    status = head[0] if head else 0
    headers = head[1] if head else {}
    return {'status': status, 'headers': headers, 'body_bytes': body_bytes,
            'sha256': body.hexdigest(), 'expected': expected}


def response_ok(response):
    """
    Decide whether a response counts as success.

    Args:
        response (dict): from read_response.

    Returns:
        True for status 200 with all the Content-Length bytes present.
    """
    if response['status'] != 200:
        return False
    expected = response['expected']
    return expected is None or response['body_bytes'] == expected


def check_expectation(args, response):
    """
    Compare the body with the expected content.

    Args:
        args (Namespace): parsed command line (expect / expect_file).
        response (dict): from read_response.

    Returns:
        "ok", "mismatch" or "none".
    """
    if getattr(args, 'expect', None):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from make_files import demo_content
        want = hashlib.sha256(demo_content(args.expect)).hexdigest()
    elif getattr(args, 'expect_file', None):
        with open(args.expect_file, 'rb') as handle:
            want = hashlib.sha256(handle.read()).hexdigest()
    else:
        return 'none'
    return 'ok' if response['sha256'] == want else 'mismatch'


def run_request(method, url, body, args):
    """
    Send one request and print the RESULT line.

    Args:
        method (str): "GET" or "POST".
        url (str): target URL.
        body (bytes): request body.
        args (Namespace): parsed command line.

    Returns:
        exit code, 0 on success.
    """
    started = time.time()
    host, port, path = parse_url(url)
    pairs = [('method', method), ('path', path)]
    sent = [] if method == 'GET' else [('sent_bytes', len(body)),
                                       ('sent_sha256', hashlib.sha256(body).hexdigest())]
    header_bytes = getattr(args, 'header_bytes', 0)
    request = build_request(method, host, port, path, body, header_bytes)
    try:
        sock = open_connection(host, port, args.timeout, getattr(args, 'rcvbuf', None))
    except OSError as exc:
        pairs += [('status', 0)] + sent + [('handshake', 'fail'),
                                           ('error', type(exc).__name__),
                                           ('seconds', '{:.3f}'.format(time.time() - started))]
        print(format_result(pairs))
        return 1
    error = None
    response = {'status': 0, 'body_bytes': 0, 'sha256': hashlib.sha256().hexdigest(),
                'expected': None}
    try:
        sock.sendall(request)
        response = read_response(sock, getattr(args, 'read_bytes', 0),
                                 getattr(args, 'read_delay', 0.0),
                                 getattr(args, 'pause_after', 0), getattr(args, 'pause', 0.0))
    except (OSError, ValueError) as exc:
        error = type(exc).__name__
    finally:
        sock.close()
    expect = check_expectation(args, response) if error is None else 'none'
    pairs += [('status', response['status']), ('body_bytes', response['body_bytes']),
              ('sha256', response['sha256'])] + sent
    pairs += [('expect', expect), ('handshake', 'ok')]
    if error:
        pairs.append(('error', error))
    pairs.append(('seconds', '{:.3f}'.format(time.time() - started)))
    print(format_result(pairs))
    return 0 if error is None and response_ok(response) and expect != 'mismatch' else 1


def run_connect_close(args):
    """
    Connect, wait, close our side and see whether the server closes too.

    Args:
        args (Namespace): parsed command line (host, port).

    Returns:
        exit code, 0 when the handshake worked and the server's FIN arrived.
    """
    started = time.time()
    pairs = [('method', 'connect-close')]
    try:
        sock = open_connection(args.host, args.port, 5.0)
    except OSError as exc:
        pairs += [('handshake', 'fail'), ('error', type(exc).__name__),
                  ('seconds', '{:.3f}'.format(time.time() - started))]
        print(format_result(pairs))
        return 1
    server_fin = 'no'
    try:
        time.sleep(0.5)
        sock.shutdown(socket.SHUT_WR)
        deadline = time.time() + 5.0
        while time.time() < deadline:
            sock.settimeout(max(0.1, deadline - time.time()))
            if not sock.recv(RECV_SIZE):
                server_fin = 'yes'
                break
    except socket.timeout:
        pass
    except OSError:
        server_fin = 'reset'
    finally:
        sock.close()
    pairs += [('handshake', 'ok'), ('server_fin', server_fin),
              ('seconds', '{:.3f}'.format(time.time() - started))]
    print(format_result(pairs))
    return 0 if server_fin == 'yes' else 1


def build_parser():
    """
    Build the command-line parser.

    Returns:
        argparse.ArgumentParser with the get, post and connect-close commands.
    """
    parser = argparse.ArgumentParser(prog='client.py', description='StreapRous demo client')
    commands = parser.add_subparsers(dest='command')

    get = commands.add_parser('get', help='one HTTP GET')
    get.add_argument('url')
    expect = get.add_mutually_exclusive_group()
    expect.add_argument('--expect', metavar='NAME', help='compare with demo_content(NAME)')
    expect.add_argument('--expect-file', metavar='PATH', help='compare with this file')
    get.add_argument('--rcvbuf', type=int, help='SO_RCVBUF before connect')
    get.add_argument('--read-bytes', type=int, default=0, help='bytes per recv')
    get.add_argument('--read-delay', type=float, default=0.0, help='seconds after each recv')
    get.add_argument('--pause-after', type=int, default=0, help='bytes before one long pause')
    get.add_argument('--pause', type=float, default=0.0, help='length of that pause')
    get.add_argument('--header-bytes', type=int, default=0, help='pad the request head to this')
    get.add_argument('--timeout', type=float, default=DEFAULT_TIMEOUT)

    post = commands.add_parser('post', help='one HTTP POST')
    post.add_argument('url')
    post.add_argument('--size', type=int, required=True, help='body bytes')
    post.add_argument('--timeout', type=float, default=DEFAULT_TIMEOUT)

    close = commands.add_parser('connect-close', help='connect, then close our side')
    close.add_argument('host')
    close.add_argument('port', type=int)
    return parser


def main(argv=None):
    """
    Run the command named on the command line.

    Args:
        argv (list): arguments after the program name (None reads sys.argv).

    Returns:
        exit code.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == 'get':
        return run_request('GET', args.url, b'', args)
    if args.command == 'post':
        return run_request('POST', args.url, post_body(args.size), args)
    if args.command == 'connect-close':
        return run_connect_close(args)
    parser.print_usage(sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main())

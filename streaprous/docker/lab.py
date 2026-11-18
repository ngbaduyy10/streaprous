"""
lab
~~~

Docker lab driver. It runs on the host (Python 3.6+, standard library only) and
starts the StreapRous backend and a real Linux TCP client in two containers on
one network, runs the scenarios below and checks the results.

Commands (run from anywhere):
    python streaprous/docker/lab.py build
    python streaprous/docker/lab.py test [--only a,b]
    python streaprous/docker/lab.py unit36
    python streaprous/docker/lab.py clean
"""

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STREAP_DIR = os.path.dirname(HERE)           # .../streaprous
ROOT = os.path.dirname(STREAP_DIR)           # repo root, mounted at /w in every container
sys.path.insert(0, HERE)

import checks  # noqa: E402

NETWORK = 'streaprous-net'
SUBNET = '172.28.0.0/24'
SERVER_IP = '172.28.0.10'
CLIENT_IP = '172.28.0.20'
PORT = 9000
SERVER = 'streaprous-server'
CLIENT = 'streaprous-client'
READY = 'Waiting for incoming connections'
SRV = 'http://{}:{}'.format(SERVER_IP, PORT)

# image key -> (image name, Dockerfile name)
IMAGES = {
    'lab': ('streaprous-lab', 'Dockerfile'),
    'lab36': ('streaprous-lab36', 'py36.Dockerfile'),
}

GET_1M = 'get {srv}/demo_1m.bin --expect demo_1m.bin'
GET_INDEX = 'get {srv}/ --expect-file www/index.html'

# One dict per scenario. Tune the drop lists, netem strings and thresholds here.
#   image        key of IMAGES for the server container
#   env          extra environment variables of the server
#   server_netem / client_netem   tc netem arguments, or None
#   steps        client commands, run in order inside the client container ({srv} = server URL)
#   checks       dicts: 'check' names a function of CHECKS, 'label' is printed, rest are arguments
SCENARIOS = [
    {
        'name': 'lab3',
        'image': 'lab', 'env': {}, 'server_netem': None, 'client_netem': None,
        'steps': ['connect-close ' + SERVER_IP + ' ' + str(PORT), GET_INDEX],
        'checks': [
            {'check': 'exits_ok', 'label': 'both client steps exit 0'},
        ],
    },
    {
        'name': 'download',
        'image': 'lab', 'env': {}, 'server_netem': None, 'client_netem': None,
        'steps': [GET_1M],
        'checks': [
            {'check': 'exits_ok', 'label': 'client exits 0'},
            {'check': 'max_payload', 'label': 'largest server payload is the MSS (1460)',
             'equals': 1460},
            {'check': 'no_text', 'label': 'no "Message too long" in the server log',
             'text': 'Message too long'},
            {'check': 'event_count', 'label': 'TIMEOUT count is 0', 'event': 'TIMEOUT',
             'equals': 0},
            {'check': 'no_violations', 'label': 'sends stay within min(cwnd, rwnd)'},
        ],
    },
    {
        'name': 'http',
        'image': 'lab', 'env': {}, 'server_netem': None, 'client_netem': None,
        'steps': ['get {srv}/ --header-bytes 3000 --expect-file www/index.html',
                  'post {srv}/ --size 150000'],
        'checks': [
            {'check': 'exits_ok', 'label': 'both client steps exit 0'},
            {'check': 'header_read', 'label': 'a request was read with header >= 3000 bytes',
             'min': 3000},
            {'check': 'body_read', 'label': 'a read has body 150000 and the sent sha256',
             'body': 150000},
        ],
    },
    {
        'name': 'loss',
        'image': 'lab', 'env': {'STREAPROUS_DROP_SEND': '3,60'},
        'server_netem': None, 'client_netem': None,
        'steps': [GET_1M],
        'checks': [
            {'check': 'exits_ok', 'label': 'client exits 0'},
            {'check': 'event_count', 'label': 'DROP count is 2', 'event': 'DROP', 'equals': 2},
            {'check': 'event_count', 'label': 'TIMEOUT >= 1', 'event': 'TIMEOUT', 'min': 1},
            {'check': 'event_count', 'label': 'FAST_RETRANSMIT >= 1',
             'event': 'FAST_RETRANSMIT', 'min': 1},
            {'check': 'event_count', 'label': 'DUPACK >= 3', 'event': 'DUPACK', 'min': 3},
        ],
    },
    {
        'name': 'random-loss',
        'image': 'lab', 'env': {'STREAPROUS_LOSS': '0.02', 'STREAPROUS_LOSS_SEED': '7'},
        'server_netem': None, 'client_netem': None,
        'steps': [GET_1M],
        'checks': [
            {'check': 'exits_ok', 'label': 'client exits 0'},
            {'check': 'event_count', 'label': 'DROP lines with why=random >= 5',
             'event': 'DROP', 'contains': 'why=random', 'min': 5},
            {'check': 'no_violations', 'label': 'sends stay within min(cwnd, rwnd)'},
        ],
    },
    {
        'name': 'slow-reader',
        'image': 'lab', 'env': {}, 'server_netem': None, 'client_netem': None,
        'steps': ['get {srv}/demo_256k.bin --expect demo_256k.bin --rcvbuf 4096 '
                  '--read-bytes 4096 --read-delay 0.05 --pause-after 32768 --pause 2.0'],
        'checks': [
            {'check': 'exits_ok', 'label': 'client exits 0'},
            {'check': 'event_count', 'label': 'RWND_ZERO >= 1', 'event': 'RWND_ZERO', 'min': 1},
            {'check': 'event_count', 'label': 'PROBE >= 1', 'event': 'PROBE', 'min': 1},
            {'check': 'event_count', 'label': 'RWND_OPEN >= 1', 'event': 'RWND_OPEN', 'min': 1},
            {'check': 'send_after_last', 'label': 'a SEND row follows the last PROBE row',
             'last': 'PROBE', 'then': 'SEND'},
            {'check': 'no_violations', 'label': 'sends stay within min(cwnd, rwnd)'},
        ],
    },
    {
        'name': 'trace',
        'image': 'lab', 'env': {'STREAPROUS_DROP_SEND': '150,400-460'},
        'server_netem': 'delay 20ms', 'client_netem': 'delay 20ms',
        'steps': [GET_1M],
        'checks': [
            {'check': 'exits_ok', 'label': 'client exits 0'},
            {'check': 'state_change', 'label': 'STATE SLOW_START -> CONGESTION_AVOIDANCE seen',
             'old': 'SLOW_START', 'new': 'CONGESTION_AVOIDANCE'},
            {'check': 'event_count', 'label': 'FAST_RETRANSMIT >= 1',
             'event': 'FAST_RETRANSMIT', 'min': 1},
            {'check': 'event_count', 'label': 'TIMEOUT >= 1', 'event': 'TIMEOUT', 'min': 1},
            {'check': 'trace_slow_start', 'label': 'slow start grows cwnd by one MSS per ACK',
             'mss': 1460},
            {'check': 'trace_loss_reaction', 'label': 'TIMEOUT and FAST_RETRANSMIT set cwnd',
             'mss': 1460},
        ],
    },
    {
        'name': 'upload',
        'image': 'lab', 'env': {}, 'server_netem': None,
        'client_netem': 'delay 10ms reorder 25% 50% duplicate 3% loss 2%',
        'steps': ['post {srv}/ --size 300000'],
        'checks': [
            {'check': 'exits_ok', 'label': 'client exits 0'},
            {'check': 'body_read', 'label': 'a read has body 300000 and the sent sha256',
             'body': 300000},
            {'check': 'event_count', 'label': 'OUT_OF_ORDER >= 1', 'event': 'OUT_OF_ORDER',
             'min': 1},
            {'check': 'event_count', 'label': 'DUP_SEGMENT >= 1', 'event': 'DUP_SEGMENT',
             'min': 1},
            {'check': 'event_count', 'label': 'DUP_ACK_SENT >= 1', 'event': 'DUP_ACK_SENT',
             'min': 1},
        ],
    },
    {
        'name': 'slow-app',
        'image': 'lab',
        'env': {'STREAPROUS_RCVBUF': '16080', 'STREAPROUS_APP_READ_DELAY': '0.05',
                'STREAPROUS_APP_READ_BYTES': '2144'},
        'server_netem': None, 'client_netem': None,
        'steps': ['post {srv}/ --size 300000'],
        'checks': [
            {'check': 'exits_ok', 'label': 'client exits 0'},
            {'check': 'body_read', 'label': 'the body read has the sent sha256 (300000 bytes)',
             'body': 300000},
            {'check': 'adv_zero_then_open', 'label': 'advertised window hit 0 and reopened to 4096',
             'reopen': 4096},
            {'check': 'event_count', 'label': 'WINDOW_UPDATE >= 1', 'event': 'WINDOW_UPDATE',
             'min': 1},
            {'check': 'zero_window_sent', 'label': 'a "win 0" segment is in the pcap'},
        ],
    },
    {
        'name': 'py36',
        'image': 'lab36', 'env': {}, 'server_netem': None, 'client_netem': None,
        'steps': [GET_1M, GET_INDEX],
        'checks': [
            {'check': 'exits_ok', 'label': 'both client steps exit 0'},
            {'check': 'server_version', 'label': 'server runs Python 3.6.15',
             'version': '3.6.15'},
            {'check': 'unit36', 'label': 'unit36 passes'},
        ],
    },
]


class LabError(Exception):
    """Raised when the lab cannot continue (Docker missing, subnet clash, ...)."""


def say(text=''):
    """
    Print a line, replacing characters the console cannot show.

    Args:
        text (str): the line.
    """
    encoding = sys.stdout.encoding or 'utf-8'
    sys.stdout.write(text.encode(encoding, 'replace').decode(encoding) + '\n')
    sys.stdout.flush()


def docker(args, timeout=None):
    """
    Run a docker command and capture its output.

    Args:
        args (list): arguments after "docker".
        timeout (float): seconds before the command is killed.

    Returns:
        (return code, stdout and stderr as one str); 124 on timeout.
    """
    try:
        done = subprocess.run(['docker'] + list(args), stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=timeout)
    except FileNotFoundError:
        raise LabError('docker is not installed or not on PATH')
    except subprocess.TimeoutExpired as exc:
        return 124, (exc.stdout or b'').decode('utf-8', 'replace') + '\n[timed out]'
    return done.returncode, done.stdout.decode('utf-8', 'replace')


def ensure_docker():
    """
    Stop early when the Docker daemon does not answer.

    Raises:
        LabError: when "docker info" fails.
    """
    code, _out = docker(['info'], timeout=60)
    if code != 0:
        raise LabError('Docker is not running (docker info failed); start Docker Desktop')


def image_exists(key):
    """
    Tell whether a lab image has been built.

    Args:
        key (str): key of IMAGES.

    Returns:
        True when the image exists.
    """
    return docker(['image', 'inspect', IMAGES[key][0]])[0] == 0


def build_image(key):
    """
    Build one lab image, showing Docker's output.

    Args:
        key (str): key of IMAGES.

    Returns:
        docker build's exit code.
    """
    name, dockerfile = IMAGES[key]
    say('Building {} from {}'.format(name, dockerfile))
    command = ['docker', 'build', '-t', name, '-f', os.path.join(HERE, dockerfile), HERE]
    if docker(['buildx', 'version'])[0] == 0:
        # a docker-container builder keeps the image in its cache unless told to load it
        command.insert(2, '--load')
    return subprocess.call(command)


def ensure_images(keys=('lab', 'lab36')):
    """
    Build the images that are missing.

    Args:
        keys (iterable): keys of IMAGES that are needed.

    Raises:
        LabError: when a build fails.
    """
    for key in keys:
        if not image_exists(key) and build_image(key) != 0:
            raise LabError('building {} failed'.format(IMAGES[key][0]))


def ensure_network():
    """
    Create the lab network when it is missing.

    Raises:
        LabError: when the subnet is taken or the network has another subnet.
    """
    code, out = docker(['network', 'inspect', NETWORK, '--format',
                        '{{range .IPAM.Config}}{{.Subnet}} {{end}}'])
    if code == 0:
        if SUBNET not in out.split():
            raise LabError('network {} exists with subnet {}, not {}; remove it with '
                           '"docker network rm {}"'.format(NETWORK, out.strip(), SUBNET, NETWORK))
        return
    code, out = docker(['network', 'create', '--subnet', SUBNET, NETWORK])
    if code != 0:
        raise LabError('cannot create network {} on {} ({}); find the network that uses the '
                       'subnet with "docker network ls" and remove it with '
                       '"docker network rm <name>"'.format(NETWORK, SUBNET, out.strip()))


def is_running(name):
    """
    Tell whether a container is running.

    Args:
        name (str): container name.

    Returns:
        True when it runs.
    """
    code, out = docker(['inspect', '-f', '{{.State.Running}}', name])
    return code == 0 and out.strip() == 'true'


def remove_container(name):
    """
    Remove a container if it exists.

    Args:
        name (str): container name.
    """
    docker(['rm', '-f', name])


def ensure_client():
    """
    Make sure the client container runs (it is kept between scenarios).

    Raises:
        LabError: when it cannot start.
    """
    if is_running(CLIENT):
        return
    remove_container(CLIENT)
    code, out = docker(['run', '-d', '--name', CLIENT, '--network', NETWORK, '--ip', CLIENT_IP,
                        '--cap-add', 'NET_ADMIN', '-v', ROOT + ':/w', IMAGES['lab'][0],
                        'sleep', 'infinity'])
    if code != 0:
        raise LabError('cannot start {}: {}'.format(CLIENT, out.strip()))


def set_client_netem(spec):
    """
    Replace the client's tc netem rule.

    Args:
        spec (str): netem arguments such as "delay 20ms", or None for no rule.

    Raises:
        LabError: when tc refuses the rule.
    """
    docker(['exec', CLIENT, 'tc', 'qdisc', 'del', 'dev', 'eth0', 'root'])
    if spec:
        code, out = docker(['exec', CLIENT, 'tc', 'qdisc', 'add', 'dev', 'eth0', 'root',
                            'netem'] + shlex.split(spec))
        if code != 0:
            raise LabError('tc netem {} failed on the client: {}'.format(spec, out.strip()))


def server_command(scenario):
    """
    Build the shell command the server container runs.

    Args:
        scenario (dict): an entry of SCENARIOS (name, server_netem).

    Returns:
        str for "sh -c": make the output folder, set netem, start tcpdump in
        the background, then replace the shell with the backend.
    """
    out_dir = 'out/' + scenario['name']
    parts = ['mkdir -p ' + out_dir]
    if scenario['server_netem']:
        parts.append('tc qdisc add dev eth0 root netem ' + scenario['server_netem'])
    parts.append('(tcpdump -U -Z root -i eth0 -w {}/server.pcap tcp port {} >/dev/null 2>&1 &)'
                 .format(out_dir, PORT))
    parts.append('sleep 0.5')
    parts.append('exec python3 start_backend.py {} {}'.format(SERVER_IP, PORT))
    return '; '.join(parts)


def start_server(scenario):
    """
    Start the server container for a scenario and wait until it listens.

    Args:
        scenario (dict): an entry of SCENARIOS.

    Raises:
        LabError: when the container fails or does not print READY in 15 s.
    """
    remove_container(SERVER)
    env = {'PYTHONUNBUFFERED': '1', 'STREAPROUS_TRACE_DIR': 'out/' + scenario['name']}
    env.update(scenario['env'])
    args = ['run', '-d', '--name', SERVER, '--network', NETWORK, '--ip', SERVER_IP,
            '--cap-add', 'NET_ADMIN', '-v', ROOT + ':/w', '-w', '/w/streaprous']
    for key in sorted(env):
        args += ['-e', '{}={}'.format(key, env[key])]
    args += [IMAGES[scenario['image']][0], 'sh', '-c', server_command(scenario)]
    code, out = docker(args)
    if code != 0:
        raise LabError('cannot start {}: {}'.format(SERVER, out.strip()))
    deadline = time.time() + 15
    log = ''
    while time.time() < deadline:
        log = docker(['logs', SERVER])[1]
        if READY in log:
            return
        if not is_running(SERVER):
            break
        time.sleep(0.3)
    raise LabError('the server did not print "{}" within 15 s; its log:\n{}'.format(READY, log))


def client_exec(step, out_file):
    """
    Run one client command in the client container and log it.

    Args:
        step (str): the command line after "client.py", with {srv} filled in.
        out_file (str): file the output is appended to.

    Returns:
        dict with args, rc, output and result (the parsed RESULT line).
    """
    args = shlex.split(step.format(srv=SRV))
    command = ['exec', '-w', '/w/streaprous', CLIENT, 'python3', 'demo/client.py'] + args
    started = time.time()
    code, out = docker(command, timeout=180)
    with open(out_file, 'a', encoding='utf-8') as handle:
        handle.write('$ client.py {}\n{}\n[exit {}, {:.1f} s]\n\n'.format(
            ' '.join(args), out.rstrip(), code, time.time() - started))
    return {'args': args, 'rc': code, 'output': out, 'result': checks.parse_result(out)}


def wait_for_closes(timeout=15):
    """
    Wait until the server log has as many CLOSE as OPEN lines.

    Args:
        timeout (float): seconds to wait at most.

    Returns:
        the server log.
    """
    deadline = time.time() + timeout
    log = docker(['logs', SERVER])[1]
    while time.time() < deadline:
        if checks.count_events(log, 'CLOSE') >= checks.count_events(log, 'OPEN'):
            break
        time.sleep(0.5)
        log = docker(['logs', SERVER])[1]
    return log


def reset_dir(path):
    """
    Remove a folder and create it again, empty.

    Args:
        path (str): the folder.
    """
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path)


def make_demo_files():
    """
    Generate the demo files on the host.

    Raises:
        LabError: when make_files.py fails.
    """
    code = subprocess.call([sys.executable, os.path.join('demo', 'make_files.py')],
                           cwd=STREAP_DIR, stdout=subprocess.DEVNULL)
    if code != 0:
        raise LabError('demo/make_files.py failed')


def run_scenario(scenario):
    """
    Run one scenario and collect everything the checks need.

    Args:
        scenario (dict): an entry of SCENARIOS.

    Returns:
        ctx dict: name, out_dir, steps, log, pcap, server_version.

    Raises:
        LabError: when the lab itself fails (not when a check will fail).
    """
    name = scenario['name']
    out_dir = os.path.join(STREAP_DIR, 'out', name)
    reset_dir(out_dir)
    client_file = os.path.join(out_dir, 'client.txt')
    open(client_file, 'w').close()
    ensure_images(['lab', scenario['image']])
    ensure_network()
    ensure_client()
    set_client_netem(scenario['client_netem'])
    ctx = {'name': name, 'out_dir': out_dir, 'steps': []}
    try:
        start_server(scenario)
        for step in scenario['steps']:
            ctx['steps'].append(client_exec(step, client_file))
        ctx['log'] = wait_for_closes()
        with open(os.path.join(out_dir, 'server.log'), 'w', encoding='utf-8') as handle:
            handle.write(ctx['log'])
        ctx['server_version'] = docker(['exec', SERVER, 'python3', '--version'])[1].strip()
        pcap = '/w/streaprous/out/{}/server.pcap'.format(name)
        ctx['pcap'] = docker(['exec', CLIENT, 'tcpdump', '-nn', '-S', '-r', pcap], timeout=120)[1]
    finally:
        remove_container(SERVER)
    return ctx


# ---- checks: each takes (spec, ctx) and returns (ok, detail) ----

def _trace_rows(ctx):
    """
    Read the largest trace of a run.

    Args:
        ctx (dict): run context.

    Returns:
        list of rows, or None when the run left no trace file.
    """
    path = checks.largest_trace(ctx['out_dir'])
    return None if path is None else checks.read_trace(path)


def _compare(value, spec):
    """
    Compare a count with the limits in a check spec.

    Args:
        value (int): the measured number.
        spec (dict): may hold "equals", "min", "max".

    Returns:
        (ok, "n=<value>").
    """
    ok = True
    if 'equals' in spec:
        ok = ok and value == spec['equals']
    if 'min' in spec:
        ok = ok and value >= spec['min']
    if 'max' in spec:
        ok = ok and value <= spec['max']
    return ok, 'got {}'.format(value)


def check_exits_ok(spec, ctx):
    """Every client step exited 0."""
    bad = ['{} -> exit {}'.format(' '.join(step['args'][:2]), step['rc'])
           for step in ctx['steps'] if step['rc'] != 0]
    return not bad, '; '.join(bad) or 'all exit codes 0'


def check_event_count(spec, ctx):
    """Count of an event's log lines (optionally only lines that contain a word)."""
    lines = checks.event_lines(ctx['log'], spec['event'])
    if 'contains' in spec:
        lines = [line for line in lines if spec['contains'] in line]
    return _compare(len(lines), spec)


def check_no_text(spec, ctx):
    """A phrase does not appear in the server log."""
    found = spec['text'] in ctx['log']
    return not found, 'found in the log' if found else 'absent'


def check_max_payload(spec, ctx):
    """Largest payload the server sent, from the pcap."""
    return _compare(checks.max_payload(ctx['pcap'], PORT), spec)


def check_no_violations(spec, ctx):
    """No send put more in flight than min(cwnd, rwnd)."""
    rows = _trace_rows(ctx)
    if rows is None:
        return False, 'no trace file'
    bad = checks.window_invariant_violations(rows)
    return not bad, '{} violating rows of {}'.format(len(bad), len(rows))


def check_header_read(spec, ctx):
    """The server read a request head of at least spec['min'] bytes."""
    heads = [head for head, _body, _sha in checks.request_reads(ctx['log'])]
    return any(head >= spec['min'] for head in heads), 'header sizes read: {}'.format(heads)


def check_body_read(spec, ctx):
    """The server read a body of spec['body'] bytes whose sha256 the client sent."""
    sent = [step['result'].get('sent_sha256') for step in ctx['steps']
            if step['result'].get('sent_sha256')]
    reads = checks.request_reads(ctx['log'])
    ok = any(body == spec['body'] and sha in sent for _head, body, sha in reads)
    return ok, 'client sent {}; server read {}'.format(
        [s[:12] for s in sent], [(b, s[:12]) for _h, b, s in reads])


def check_state_change(spec, ctx):
    """A STATE event went from spec['old'] to spec['new']."""
    return checks.state_change_seen(ctx['log'], spec['old'], spec['new']), 'STATE lines: {}'.format(
        len(checks.event_lines(ctx['log'], 'STATE')))


def check_trace_slow_start(spec, ctx):
    """The CSV trace shows slow start growing cwnd as the slides say (3-123, 3-125)."""
    rows = _trace_rows(ctx)
    if rows is None:
        return False, 'no trace file'
    problems = checks.slow_start_problems(rows, spec['mss'])
    return not problems, '; '.join(problems[:3]) or 'slow start steps are right'


def check_trace_loss_reaction(spec, ctx):
    """The CSV trace shows cwnd after TIMEOUT and FAST_RETRANSMIT (3-124, 3-125)."""
    rows = _trace_rows(ctx)
    if rows is None:
        return False, 'no trace file'
    problems = checks.loss_reaction_problems(rows, spec['mss'])
    return not problems, '; '.join(problems[:3]) or 'loss reactions are right'


def check_adv_zero_then_open(spec, ctx):
    """The advertised window reached 0 and later reopened."""
    rows = _trace_rows(ctx)
    if rows is None:
        return False, 'no trace file'
    return checks.adv_zero_then_open(rows, spec['reopen']), '{} rows'.format(len(rows))


def check_send_after_last(spec, ctx):
    """A row of spec['then'] comes after the last row of spec['last']."""
    rows = _trace_rows(ctx)
    if rows is None:
        return False, 'no trace file'
    last = [i for i, row in enumerate(rows) if row['event'] == spec['last']]
    if not last:
        return False, 'no {} row'.format(spec['last'])
    later = [row for row in rows[last[-1] + 1:] if row['event'] == spec['then']]
    return bool(later), '{} {} rows after the last {}'.format(len(later), spec['then'],
                                                              spec['last'])


def check_zero_window_sent(spec, ctx):
    """The pcap shows a segment from the server with win 0."""
    return checks.zero_window_sent(ctx['pcap'], PORT), 'searched the server pcap'


def check_server_version(spec, ctx):
    """The server's Python is the wanted version."""
    return spec['version'] in ctx['server_version'], ctx['server_version']


def check_unit36(spec, ctx):
    """The unit tests pass under Python 3.6."""
    code, out = run_unit36(capture=True)
    return code == 0, 'ok' if code == 0 else '\n'.join(out.splitlines()[-15:])


CHECKS = {
    'exits_ok': check_exits_ok,
    'event_count': check_event_count,
    'no_text': check_no_text,
    'max_payload': check_max_payload,
    'no_violations': check_no_violations,
    'header_read': check_header_read,
    'body_read': check_body_read,
    'state_change': check_state_change,
    'trace_slow_start': check_trace_slow_start,
    'trace_loss_reaction': check_trace_loss_reaction,
    'adv_zero_then_open': check_adv_zero_then_open,
    'send_after_last': check_send_after_last,
    'zero_window_sent': check_zero_window_sent,
    'server_version': check_server_version,
    'unit36': check_unit36,
}


def evaluate(scenario, ctx):
    """
    Run the checks of a scenario and print PASS / FAIL lines.

    Args:
        scenario (dict): an entry of SCENARIOS.
        ctx (dict): from run_scenario.

    Returns:
        number of failed checks.
    """
    failed = 0
    for spec in scenario['checks']:
        try:
            ok, detail = CHECKS[spec['check']](spec, ctx)
        except Exception as exc:  # a broken check is a failed check
            ok, detail = False, '{}: {}'.format(type(exc).__name__, exc)
        if ok:
            say('PASS {}: {}'.format(scenario['name'], spec['label']))
        else:
            failed += 1
            say('FAIL {}: {}: {}'.format(scenario['name'], spec['label'], detail))
    return failed


def run_unit36(capture=False):
    """
    Run the compile check and the unit tests under Python 3.6 in Docker.

    Args:
        capture (bool): keep the output (and return it) instead of printing it.

    Returns:
        (exit code, output text); the text is empty unless capture is true.
    """
    ensure_images(['lab36'])
    script = ('python3 --version && python -m compileall -q streaprous && '
              'python -m unittest discover -s streaprous/tests -t streaprous && '
              '{ if [ -f streaprous/tools/check_imports.py ]; '
              'then python streaprous/tools/check_imports.py; fi; }')
    command = ['run', '--rm', '-v', ROOT + ':/w', '-w', '/w', IMAGES['lab36'][0],
               'sh', '-c', script]
    if capture:
        return docker(command, timeout=600)
    return subprocess.call(['docker'] + command), ''


# ---- commands ----

def cmd_build(args):
    """Build both images."""
    ensure_docker()
    for key in ('lab', 'lab36'):
        if build_image(key) != 0:
            say('build of {} failed'.format(IMAGES[key][0]))
            return 1
    say('Both images built')
    return 0


def cmd_test(args):
    """Run the scenarios in order and report every check."""
    ensure_docker()
    names = [s['name'] for s in SCENARIOS]
    chosen = args.only.split(',') if args.only else names
    unknown = [n for n in chosen if n not in names]
    if unknown:
        raise LabError('unknown scenario {}; choose from {}'.format(','.join(unknown),
                                                                    ','.join(names)))
    make_demo_files()
    failed = 0
    for scenario in SCENARIOS:
        if scenario['name'] not in chosen:
            continue
        say('--- {} ---'.format(scenario['name']))
        try:
            ctx = run_scenario(scenario)
        except LabError as exc:
            say('FAIL {}: run: {}'.format(scenario['name'], exc))
            failed += 1
            continue
        failed += evaluate(scenario, ctx)
    say('ALL PASSED' if failed == 0 else '{} FAILED'.format(failed))
    return 0 if failed == 0 else 1


def cmd_unit36(args):
    """Run the Python 3.6 checks in Docker."""
    ensure_docker()
    code, _out = run_unit36()
    return code


def cmd_clean(args):
    """Remove both containers and the network."""
    ensure_docker()
    for name in (SERVER, CLIENT):
        remove_container(name)
    code, out = docker(['network', 'rm', NETWORK])
    if code != 0 and 'No such network' not in out and 'not found' not in out:
        say('could not remove network {}: {}'.format(NETWORK, out.strip()))
        return 1
    say('Removed {}, {} and the network {}'.format(SERVER, CLIENT, NETWORK))
    return 0


def main(argv=None):
    """
    Parse the command line and run the chosen command.

    Args:
        argv (list): arguments after the program name (None reads sys.argv).

    Returns:
        exit code.
    """
    parser = argparse.ArgumentParser(prog='lab.py', description='StreapRous Docker lab')
    commands = parser.add_subparsers(dest='command')
    commands.add_parser('build', help='build both images').set_defaults(run=cmd_build)
    test = commands.add_parser('test', help='run the scenarios and check them')
    test.add_argument('--only', help='comma-separated scenario names')
    test.set_defaults(run=cmd_test)
    commands.add_parser('unit36', help='compile and unit-test under Python 3.6').set_defaults(
        run=cmd_unit36)
    commands.add_parser('clean', help='remove containers and network').set_defaults(
        run=cmd_clean)
    args = parser.parse_args(argv)
    if not getattr(args, 'run', None):
        parser.print_usage(sys.stderr)
        return 2
    try:
        return args.run(args)
    except LabError as exc:
        say('lab: {}'.format(exc))
        return 1


if __name__ == '__main__':
    sys.exit(main())

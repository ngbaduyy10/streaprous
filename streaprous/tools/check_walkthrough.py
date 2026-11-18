"""Check the walkthrough document against the course slides' rules and our code.

Usage:
    python check_walkthrough.py [--doc streaprous/docs/TCP_FEATURES.md] [--root REPO]

Fails, listing each problem, when
  * the document is longer than 600 lines;
  * its ## headings, or the ### subsections of a pair section, are not the
    expected ones in the expected order;
  * a pair's Slide rules table is missing, or does not hold each canonical
    slide rule exactly once, as the slide, or each extra key exactly once as an
    extra row with a reason;
  * a Mark is neither "as the slide" nor "extra: <reason>", or says "differs";
  * a function cell names something that is not in streaprous_socket/<file>;
  * Known limits, Questions to expect or Self-check are too thin;
  * a slide number outside the course slide map appears in the document or in
    streaprous_socket/tcp_*.py;
  * a teacher function holding a TEAM comment line is not named in the document;
  * a test command is missing, or the document talks about a demo;
  * a line outside Background mentions an outside source without saying
    background.
Finding the function around a TEAM line needs Python 3.8 or later.
"""

import argparse
import ast
import glob
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_DOC = os.path.join('streaprous', 'docs', 'TCP_FEATURES.md')
MARKER = '# TEAM'
MAX_LINES = 600
# slides 3-77 to 3-88, 3-90 to 3-96, 3-101, 3-103, 3-104, 3-120 to 3-125, 3-146, 3-147
ALLOWED_SLIDES = (set(range(77, 89)) | set(range(90, 97)) | {101, 103, 104}
                  | set(range(120, 126)) | {146, 147})
SLIDE_RE = re.compile(r'(?<![\w-])3-(\d{1,3})(?![\w-])')
OUR_FOLDERS = ('tests', 'tools', 'demo', 'docker', 'docs', '__pycache__')

HEADINGS = ['Before you start', 'Reliable data transfer', 'Flow control',
            'Congestion control', "Our hooks in the teacher's files", 'Background']
PAIRS = {'R': 'Reliable data transfer', 'F': 'Flow control', 'C': 'Congestion control'}
SUBSECTIONS = ['Slide rules', 'Traced example', 'Known limits', 'Questions to expect',
               'Self-check']
TABLE_HEADER = ['Slide', "The slide's words", 'Our function', 'Mark']
AS_THE_SLIDE = 'as the slide'
TEST_COMMANDS = ('python -m unittest discover -s streaprous/tests -t streaprous',
                 'python streaprous/docker/lab.py test')
OUTSIDE_RE = re.compile(r'\b(?:RFC|Linux|BSD|textbook)', re.IGNORECASE)

# (pair, slide numbers, quote); R = reliable, F = flow, C = congestion
RULES = [
    ('R', '3-79, 3-80',
     'Sequence numbers: byte stream "number" of first byte in segment\'s data. '
     'Acknowledgements: seq # of next byte expected from other side; '
     'cumulative ACK'),
    ('R', '3-81',
     'SampleRTT: measured time from segment transmission until ACK receipt; '
     'ignore retransmissions'),
    ('R', '3-82',
     'EstimatedRTT = (1 - alpha)*EstimatedRTT + alpha*SampleRTT; typical '
     'value: alpha = 0.125'),
    ('R', '3-83',
     r'DevRTT = (1 - beta)*DevRTT + beta*\|SampleRTT - EstimatedRTT\| '
     '(typically, beta = 0.25); TimeoutInterval = EstimatedRTT + 4*DevRTT'),
    ('R', '3-84',
     'event: data received from application: create segment with seq #; start '
     'timer if not already running (timer for oldest unACKed segment, '
     'expiration interval TimeoutInterval)'),
    ('R', '3-84',
     'event: timeout: retransmit segment that caused timeout; restart timer'),
    ('R', '3-84',
     'event: ACK received: if ACK acknowledges previously unACKed segments, '
     'update what is known to be ACKed; start timer if there are still '
     'unACKed segments'),
    ('R', '3-85',
     'in-order segment, all data ACKed: delayed ACK, wait up to 500ms for '
     'next segment, if no next segment send ACK; in-order segment, one other '
     'segment has ACK pending: immediately send single cumulative ACK; '
     'out-of-order segment, gap detected: immediately send duplicate ACK, '
     'indicating seq. # of next expected byte; segment that fills gap: '
     'immediate send ACK, provided that segment starts at lower end of gap'),
    ('R', '3-86, 3-87',
     'lost ACK scenario; premature timeout; cumulative ACK covers for earlier '
     'lost ACK; SendBase moves to each new ACK'),
    ('R', '3-88',
     'if sender receives 3 additional ACKs for same data ("triple duplicate '
     'ACKs"), resend unACKed segment with smallest seq #'),
    ('F', '3-94',
     'TCP receiver "advertises" free buffer space in rwnd field in TCP '
     'header; sender limits amount of unACKed ("in-flight") data to received '
     'rwnd'),
    ('C', '3-122',
     'LastByteSent - LastByteAcked <= cwnd'),
    ('C', '3-123',
     'initially cwnd = 1 MSS; double cwnd every RTT, done by incrementing '
     'cwnd for every ACK received'),
    ('C', '3-124',
     'on loss event, ssthresh is set to 1/2 of cwnd just before loss event'),
    ('C', '3-125',
     'start: cwnd = 1 MSS, ssthresh = 64 KB, dupACKcount = 0'),
    ('C', '3-125',
     'slow start, new ACK: cwnd = cwnd+MSS, dupACKcount = 0, transmit new '
     'segment(s), as allowed; cwnd > ssthresh: go to congestion avoidance'),
    ('C', '3-125',
     'congestion avoidance, new ACK: cwnd = cwnd + MSS*(MSS/cwnd), '
     'dupACKcount = 0, transmit new segment(s), as allowed'),
    ('C', '3-125',
     'slow start or congestion avoidance, duplicate ACK: dupACKcount++; '
     'dupACKcount == 3: ssthresh = cwnd/2, cwnd = ssthresh + 3, retransmit '
     'missing segment, go to fast recovery'),
    ('C', '3-125',
     'fast recovery, duplicate ACK: cwnd = cwnd + MSS, transmit new '
     'segment(s), as allowed; new ACK: cwnd = ssthresh, dupACKcount = 0, go '
     'to congestion avoidance'),
    ('C', '3-125',
     'timeout (any state): ssthresh = cwnd/2, cwnd = 1 MSS, dupACKcount = 0, '
     'retransmit missing segment, go to slow start'),
]

# (pair, key): the key sits in exactly one extra row of the pair's table
EXTRAS = [
    ('R', '1 s before the first SampleRTT'),
    ('R', 'DevRTT = SampleRTT/2'),
    ('R', 'never below 0.2 s'),
    ('R', 'one segment is timed at a time'),
    ('R', 'ACK for bytes we never sent'),
    ('R', 'carries no data and the same window'),
    ('R', '10 s without any segment'),
    ('R', "client's MSS option"),
    ('R', 'up to 2 s'),
    ('F', 'one data byte every 0.5 s'),
    ('F', 'a quarter of RcvBuffer'),
    ('C', 'round down'),
]


def read_text(path):
    """Return the text of a file."""
    with open(path, encoding='utf-8') as handle:
        return handle.read()


def normalize(text):
    """Lowercase a text and delete every character outside a-z 0-9 # + * / = < > ( ) -.

    Args:
        text: any text.

    Returns:
        the normalized text.
    """
    return re.sub(r'[^a-z0-9#+*/=<>()-]', '', text.lower())


def tcp_files(root):
    """List the tcp_*.py modules under root/streaprous/streaprous_socket.

    Args:
        root: repository root.

    Returns:
        sorted list of paths.
    """
    return sorted(glob.glob(os.path.join(root, 'streaprous', 'streaprous_socket', 'tcp_*.py')))


def teacher_files(root):
    """List the teacher's Python files (everything except our own folders and tcp_*.py).

    Args:
        root: repository root.

    Returns:
        sorted list of paths.
    """
    found = []
    top = os.path.join(root, 'streaprous')
    for folder, dirs, files in os.walk(top):
        dirs[:] = [d for d in dirs if d not in OUR_FOLDERS]
        for name in files:
            if name.endswith('.py') and not name.startswith('tcp_'):
                found.append(os.path.join(folder, name))
    return sorted(found)


def team_targets(root):
    """Find what the document must name for each TEAM comment in teacher files.

    Args:
        root: repository root.

    Returns:
        sorted list of names: the innermost function around each TEAM line,
        or the file name when the line is outside every function. Needs
        Python 3.8 or later when a TEAM line exists.
    """
    targets = set()
    for path in teacher_files(root):
        text = read_text(path)
        lines = [i + 1 for i, line in enumerate(text.split('\n')) if MARKER in line]
        if not lines:
            continue
        funcs = [n for n in ast.walk(ast.parse(text))
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for number in lines:
            inside = [f for f in funcs if f.lineno <= number <= f.end_lineno]
            if inside:
                targets.add(min(inside, key=lambda f: f.end_lineno - f.lineno).name)
            else:
                targets.add(os.path.basename(path))
    return sorted(targets)


def find_bad_slides(text):
    """List slide numbers in a text that are outside the slide map.

    Args:
        text: document or source text.

    Returns:
        sorted list of strings such as '3-89'.
    """
    bad = set()
    for match in SLIDE_RE.finditer(text):
        if int(match.group(1)) not in ALLOWED_SLIDES:
            bad.add('3-' + match.group(1))
    return sorted(bad)


def split_cells(line):
    """Split a table row on '|' that is not preceded by a backslash.

    Args:
        line: one line of a Markdown table.

    Returns:
        list of stripped cells, without the empty ones outside the outer bars.
    """
    cells = [c.strip() for c in re.split(r'(?<!\\)\|', line.strip())]
    if cells and cells[0] == '':
        cells = cells[1:]
    if cells and cells[-1] == '':
        cells = cells[:-1]
    return cells


def split_sections(lines, prefix):
    """Cut lines into sections at headings that start with prefix.

    Args:
        lines: list of lines; fenced code is not searched for headings.
        prefix: '## ' or '### '.

    Returns:
        list of (heading text, list of body lines).
    """
    sections = []
    fenced = False
    for line in lines:
        if line.startswith('```'):
            fenced = not fenced
        if not fenced and line.startswith(prefix):
            sections.append((line[len(prefix):].strip(), []))
        elif sections:
            sections[-1][1].append(line)
    return sections


def parse_table(body):
    """Read the Slide rules table from the lines of a subsection.

    Args:
        body: lines under '### Slide rules'.

    Returns:
        list of rows (each a list of four cells), or None when the header is missing.
    """
    for number, line in enumerate(body):
        if split_cells(line) == TABLE_HEADER:
            rows = []
            for row in body[number + 1:]:
                if not row.strip().startswith('|'):
                    break
                cells = split_cells(row)
                if cells and not re.match(r'^:?-+:?$', cells[0]):
                    rows.append(cells)
            return rows
    return None


def find_item(root, item):
    """Check that 'file.py:Name' or 'file.py:Class.method' exists in streaprous_socket.

    Args:
        root: repository root.
        item: the text of one item.

    Returns:
        True when the function, class or method exists.
    """
    match = re.match(r'^(\w+\.py):(\w+)(?:\.(\w+))?$', item)
    if not match:
        return False
    path = os.path.join(root, 'streaprous', 'streaprous_socket', match.group(1))
    if not os.path.isfile(path):
        return False
    first, second = match.group(2), match.group(3)
    for node in ast.parse(read_text(path)).body:
        if getattr(node, 'name', None) != first:
            continue
        if second is None:
            return True
        return any(getattr(sub, 'name', None) == second for sub in node.body)
    return False


def check_shape(lines):
    """Check the line count and the ## headings (checks 1 and 2).

    Args:
        lines: lines of the document.

    Returns:
        (problems, dict of ## sections by heading).
    """
    problems = []
    if len(lines) > MAX_LINES:
        problems.append('the document has %d lines, more than %d' % (len(lines), MAX_LINES))
    sections = split_sections(lines, '## ')
    found = [name for name, _ in sections]
    if found != HEADINGS:
        problems.append('the ## headings are %s, expected in this order: %s'
                        % (found, HEADINGS))
    return problems, dict(sections)


def check_subsections(name, body):
    """Check the ### subsections of a pair section (check 2).

    Args:
        name: the pair section's heading.
        body: its lines.

    Returns:
        (problems, dict of ### subsections by heading).
    """
    subs = split_sections(body, '### ')
    found = [s for s, _ in subs if s in SUBSECTIONS]
    problems = []
    if found != SUBSECTIONS:
        problems.append('section %s has the ### subsections %s, expected in this order: %s'
                        % (name, found, SUBSECTIONS))
    return problems, dict(subs)


def check_rows(root, name, rows):
    """Check the marks and the function cells of a table (checks 6 and 7).

    Args:
        root: repository root.
        name: the pair section's heading.
        rows: table rows.

    Returns:
        list of problems.
    """
    problems = []
    for cells in rows:
        if len(cells) != 4:
            problems.append('%s: table row with %d cells, expected 4: %s'
                            % (name, len(cells), cells[:1]))
            continue
        mark = cells[3]
        if 'differs' in mark.lower() or not (
                mark == AS_THE_SLIDE or mark.startswith('extra:')):
            problems.append('%s: the mark "%s" is neither "%s" nor "extra: <reason>"'
                            % (name, mark, AS_THE_SLIDE))
        for item in cells[2].replace('`', '').split(','):
            if not find_item(root, item.strip()):
                problems.append('%s: function cell item "%s" does not exist'
                                % (name, item.strip()))
    return problems


def check_canonical(pair, name, rows):
    """Check the canonical rules and extra keys of one pair (checks 4 and 5).

    Args:
        pair: 'R', 'F' or 'C'.
        name: the pair section's heading.
        rows: table rows.

    Returns:
        list of problems.
    """
    problems = []
    rows = [r for r in rows if len(r) == 4]
    for rule_pair, slides, quote in RULES:
        if rule_pair != pair:
            continue
        hits = [r for r in rows if normalize(r[1]) == normalize(quote)]
        if len(hits) != 1:
            problems.append('%s: canonical rule for %s matches %d rows, expected 1: %s...'
                            % (name, slides, len(hits), quote[:40]))
            continue
        for number in re.findall(r'3-\d+', slides):
            if number not in hits[0][0]:
                problems.append('%s: canonical rule row lacks slide %s in its Slide cell'
                                % (name, number))
        if hits[0][3] != AS_THE_SLIDE:
            problems.append('%s: canonical rule for %s is not marked "%s"'
                            % (name, slides, AS_THE_SLIDE))
    for extra_pair, key in EXTRAS:
        if extra_pair != pair:
            continue
        hits = [r for r in rows if r[3].startswith('extra:') and normalize(key) in normalize(r[1])]
        if len(hits) != 1:
            problems.append('%s: extra key "%s" is in %d extra rows, expected 1'
                            % (name, key, len(hits)))
        elif len(hits[0][3][len('extra:'):].split()) < 5:
            problems.append('%s: the reason of extra key "%s" has fewer than 5 words'
                            % (name, key))
    return problems


def check_pair(root, pair, name, body):
    """Check one pair section: its subsections, table and text blocks.

    Args:
        root: repository root.
        name: the section heading.
        pair: 'R', 'F' or 'C'.
        body: its lines.

    Returns:
        list of problems.
    """
    problems, subs = check_subsections(name, body)
    rows = parse_table(subs.get('Slide rules', []))
    if rows is None:
        problems.append('%s: Slide rules has no table with the header | %s |'
                        % (name, ' | '.join(TABLE_HEADER)))
    else:
        problems += check_rows(root, name, rows) + check_canonical(pair, name, rows)
    problems += check_limits(name, subs.get('Known limits', []))
    problems += check_questions(name, subs.get('Questions to expect', []))
    numbered = [x for x in subs.get('Self-check', []) if re.match(r'^\d+[.)]\s', x)]
    if len(numbered) < 3:
        problems.append('%s: Self-check has %d numbered items, expected at least 3'
                        % (name, len(numbered)))
    return problems


def check_limits(name, body):
    """Check that Known limits has bullets and each holds an Answer (check 8).

    Args:
        name: the section heading.
        body: lines under Known limits.

    Returns:
        list of problems.
    """
    bullets = []
    for line in body:
        if line.startswith('- '):
            bullets.append(line)
        elif bullets and line.strip():
            bullets[-1] += ' ' + line.strip()
    problems = []
    if not bullets:
        problems.append('%s: Known limits has no "- " bullet' % name)
    for bullet in bullets:
        if 'Answer:' not in bullet:
            problems.append('%s: a Known limits bullet has no Answer: %s'
                            % (name, bullet[:40]))
    return problems


def check_questions(name, body):
    """Check Questions to expect: at least 4 Q: lines, each followed by an A: line (check 9).

    Args:
        name: the section heading.
        body: lines under Questions to expect.

    Returns:
        list of problems.
    """
    lines = [x for x in body if x.strip()]
    problems = []
    asked = [i for i, x in enumerate(lines) if x.startswith('Q:')]
    if len(asked) < 4:
        problems.append('%s: Questions to expect has %d Q: lines, expected at least 4'
                        % (name, len(asked)))
    for i in asked:
        if i + 1 >= len(lines) or not lines[i + 1].startswith('A:'):
            problems.append('%s: Questions to expect: a Q: line has no A: line after it: %s'
                            % (name, lines[i][:40]))
    return problems


def check_text(root, lines):
    """Check the document-wide rules (checks 11 to 15).

    Args:
        root: repository root.
        lines: lines of the document.

    Returns:
        list of problems.
    """
    doc = '\n'.join(lines)
    problems = []
    for bad in find_bad_slides(doc):
        problems.append('slide %s in the document is outside the slide map' % bad)
    for path in tcp_files(root):
        for bad in find_bad_slides(read_text(path)):
            problems.append('slide %s in %s is outside the slide map'
                            % (bad, os.path.basename(path)))
    if sys.version_info < (3, 8):
        print('note: skipping the TEAM function check, it needs Python 3.8 or later')
    else:
        for name in team_targets(root):
            if name not in doc:
                problems.append('teacher change %s is not in the document' % name)
    for command in TEST_COMMANDS:
        if command not in doc:
            problems.append('test command is missing: %s' % command)
    hits = [n for n, line in enumerate(lines, 1) if re.search(r'demo|rehears', line, re.I)]
    if hits:
        problems.append('demo or rehearsal appears on %d lines, the first is line %d'
                        % (len(hits), hits[0]))
    return problems


def check_background(lines):
    """Check that outside sources are flagged outside Background (check 15).

    Args:
        lines: lines of the document.

    Returns:
        list of problems.
    """
    problems = []
    current = ''
    fenced = False
    for number, line in enumerate(lines, 1):
        if line.startswith('```'):
            fenced = not fenced
        if not fenced and line.startswith('## '):
            current = line[3:].strip()
        if current == 'Background':
            continue
        if OUTSIDE_RE.search(line) and 'background' not in line.lower():
            problems.append('line %d mentions an outside source without saying background: %s'
                            % (number, line[:40]))
    return problems


def check(root, doc_path):
    """Run all the checks.

    Args:
        root: repository root.
        doc_path: path of the walkthrough document.

    Returns:
        list of problem strings (empty when everything passes).
    """
    lines = read_text(doc_path).split('\n')
    if lines and lines[-1] == '':
        lines.pop()
    problems, sections = check_shape(lines)
    for pair, name in sorted(PAIRS.items()):
        if name in sections:
            problems += check_pair(root, pair, name, sections[name])
    return problems + check_text(root, lines) + check_background(lines)


def say(text):
    """Print a line, replacing characters the console cannot show."""
    encoding = sys.stdout.encoding or 'ascii'
    print(text.encode(encoding, 'replace').decode(encoding))


def main(argv=None):
    """Command line entry point.

    Args:
        argv: argument list without the program name (default sys.argv[1:]).

    Returns:
        0 when every check passes, 1 otherwise.
    """
    parser = argparse.ArgumentParser(description='Check the walkthrough document.')
    parser.add_argument('--root', default=ROOT, help='repository root (default: this repo)')
    parser.add_argument('--doc', default=None,
                        help='walkthrough document (default ROOT/%s)' % DEFAULT_DOC)
    args = parser.parse_args(argv)
    doc = args.doc or os.path.join(args.root, DEFAULT_DOC)
    try:
        problems = check(args.root, doc)
    except (OSError, SyntaxError) as error:
        say('FAIL: %s' % error)
        return 1
    if problems:
        say('FAIL: %d problems' % len(problems))
        for line in problems:
            say('  ' + line)
        return 1
    print('OK: walkthrough shape, %d slide rules, %d extras' % (len(RULES), len(EXTRAS)))
    return 0


if __name__ == '__main__':
    sys.exit(main())

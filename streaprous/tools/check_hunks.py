"""Check our changes to the teacher's files.

Usage:
    python check_hunks.py [--base ccc6e34] [--worktree]

Reads git diff <base>..HEAD -- streaprous/ (or git diff <base> with
--worktree). Every hunk of a file that existed at <base> must add a line with
a TEAM comment. In streaprous/daemon/ only Request.prepare,
HttpAdapter.handle_client_raw and HttpAdapter.read_request_raw may change.
Needs Python 3.8 or later for the daemon check (ast end line numbers).
"""

import argparse
import ast
import os
import re
import subprocess
import sys

DEFAULT_BASE = 'ccc6e34'
MARKER = '# TEAM'
# class name, method name
ALLOWED_METHODS = (
    ('Request', 'prepare'),
    ('HttpAdapter', 'handle_client_raw'),
    ('HttpAdapter', 'read_request_raw'),
)
HUNK_RE = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def parse_diff(text):
    """Split a unified diff into files and hunks.

    Args:
        text: output of git diff.

    Returns:
        list of dicts with 'path', 'is_new' and 'hunks'. Each hunk is a dict
        with 'header' and 'lines', a list of (kind, old_no, new_no, text) where
        kind is '+', '-' or ' ' and the number of the side that does not have
        the line is None.
    """
    files = []
    current = None
    hunk = None
    old_no = new_no = 0
    for line in text.split('\n'):
        if line.startswith('diff --git '):
            current = {'path': None, 'is_new': False, 'hunks': []}
            files.append(current)
            hunk = None
        elif current is None:
            continue
        elif hunk is None and line.startswith('new file mode'):
            current['is_new'] = True
        elif hunk is None and line.startswith('--- '):
            if line.strip() == '--- /dev/null':
                current['is_new'] = True
        elif hunk is None and line.startswith('+++ '):
            name = line[4:].strip()
            if name.startswith('b/'):
                current['path'] = name[2:]
            elif name == '/dev/null':
                current['path'] = None
        elif line.startswith('@@'):
            match = HUNK_RE.match(line)
            if not match:
                continue
            old_no = int(match.group(1))
            new_no = int(match.group(3))
            hunk = {'header': line, 'lines': []}
            current['hunks'].append(hunk)
        elif hunk is not None and line[:1] in ('+', '-', ' '):
            kind = line[0]
            if kind == '+':
                hunk['lines'].append(('+', None, new_no, line[1:]))
                new_no += 1
            elif kind == '-':
                hunk['lines'].append(('-', old_no, None, line[1:]))
                old_no += 1
            else:
                hunk['lines'].append((' ', old_no, new_no, line[1:]))
                old_no += 1
                new_no += 1
    return files


def has_team_line(hunk):
    """Tell whether a hunk adds a line containing the TEAM marker.

    Args:
        hunk: a hunk from parse_diff.

    Returns:
        True when some added line contains '# TEAM'.
    """
    return any(kind == '+' and MARKER in body for kind, _, _, body in hunk['lines'])


def allowed_spans(source):
    """Find the line spans of the methods the daemon may change.

    Args:
        source: Python source text.

    Returns:
        list of (first_line, last_line) for each allowed method present.
        Needs Python 3.8 or later.
    """
    spans = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if (isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and (node.name, item.name) in ALLOWED_METHODS):
                    spans.append((item.lineno, item.end_lineno))
    return spans


def _in_spans(number, spans):
    """Tell whether a line number lies in one of the spans."""
    return any(first <= number <= last for first, last in spans)


def scope_violations(hunk, new_spans, old_spans):
    """List changed lines of a daemon hunk that lie outside the allowed methods.

    Blank added or removed lines are ignored.

    Args:
        hunk: a hunk from parse_diff.
        new_spans: allowed_spans of the new file version.
        old_spans: allowed_spans of the old file version.

    Returns:
        list of offending diff lines (with their + or - sign).
    """
    bad = []
    for kind, old_no, new_no, body in hunk['lines']:
        if kind == ' ' or not body.strip():
            continue
        if kind == '+' and not _in_spans(new_no, new_spans):
            bad.append('+' + body)
        elif kind == '-' and not _in_spans(old_no, old_spans):
            bad.append('-' + body)
    return bad


def check_diff(text, read_old, read_new):
    """Check every hunk of a diff.

    Args:
        text: output of git diff.
        read_old: function path -> source of the file at the base.
        read_new: function path -> source of the file now.

    Returns:
        (hunk_count, file_count, problems); problems is a list of text blocks,
        one per offending hunk.
    """
    problems = []
    hunk_count = 0
    file_count = 0
    for entry in parse_diff(text):
        path = entry['path']
        if entry['is_new'] or path is None or not entry['hunks']:
            continue
        file_count += 1
        daemon = path.startswith('streaprous/daemon/') and path.endswith('.py')
        spans = None
        for hunk in entry['hunks']:
            hunk_count += 1
            block = []
            if not has_team_line(hunk):
                block.append('no added line with "%s"' % MARKER)
            if daemon:
                if spans is None:
                    spans = (allowed_spans(read_new(path)), allowed_spans(read_old(path)))
                for line in scope_violations(hunk, spans[0], spans[1]):
                    block.append('outside prepare/handle_client_raw/read_request_raw: ' + line)
            if block:
                problems.append('%s %s\n  ' % (path, hunk['header']) + '\n  '.join(block))
    return hunk_count, file_count, problems


def _git(*args):
    """Run git in the repository root and return its output as text."""
    proc = subprocess.run(('git', '-C', ROOT) + args, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError('git %s failed: %s' % (' '.join(args),
                                                  proc.stderr.decode('utf-8', 'replace')))
    return proc.stdout.decode('utf-8', 'replace')


def main(argv=None):
    """Command line entry point.

    Args:
        argv: argument list without the program name (default sys.argv[1:]).

    Returns:
        0 when every hunk passes, 1 otherwise.
    """
    parser = argparse.ArgumentParser(description='Check hunks in the teacher files.')
    parser.add_argument('--base', default=DEFAULT_BASE, help='base commit')
    parser.add_argument('--worktree', action='store_true',
                        help='compare the working tree instead of HEAD')
    args = parser.parse_args(argv)
    if sys.version_info < (3, 8):
        print('check_hunks.py needs Python 3.8 or later (ast end line numbers); '
              'run it on the host, not in the 3.6 environment.')
        return 1
    if args.worktree:
        text = _git('diff', '--no-renames', args.base, '--', 'streaprous/')
    else:
        text = _git('diff', '--no-renames', args.base + '..HEAD', '--', 'streaprous/')

    def read_old(path):
        """Return a file as it was at the base."""
        return _git('show', '%s:%s' % (args.base, path))

    def read_new(path):
        """Return a file as it is now."""
        if args.worktree:
            with open(os.path.join(ROOT, path), encoding='utf-8') as handle:
                return handle.read()
        return _git('show', 'HEAD:' + path)

    hunks, files, problems = check_diff(text, read_old, read_new)
    if problems:
        print('FAIL: %d of %d hunks need attention' % (len(problems), hunks))
        for block in problems:
            print(block)
        return 1
    print('OK: %d hunks in %d files carry # TEAM; daemon hunks stay in '
          'prepare/handle_client_raw/read_request_raw' % (hunks, files))
    return 0


if __name__ == '__main__':
    sys.exit(main())

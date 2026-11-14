"""Check the walkthrough document against the code.

Usage:
    python check_walkthrough.py [--doc streaprous/docs/TCP_FEATURES.md]

Fails when
  * a function defined in streaprous_socket/tcp_*.py, or read_request_raw, is
    not named in the document (special methods such as __init__ are not
    required);
  * a teacher function that holds a TEAM comment line is not named in the
    document (a TEAM line outside any function requires the file name);
  * a slide number outside the course slide map appears in the document or in
    streaprous_socket/tcp_*.py.
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
# slides 3-77 to 3-88, 3-90 to 3-96, 3-120 to 3-125
ALLOWED_SLIDES = set(range(77, 89)) | set(range(90, 97)) | set(range(120, 126))
SLIDE_RE = re.compile(r'(?<![\w-])3-(\d{1,3})(?![\w-])')
OUR_FOLDERS = ('tests', 'tools', 'demo', 'docker', 'docs', '__pycache__')


def _read(path):
    """Return the text of a file."""
    with open(path, encoding='utf-8') as handle:
        return handle.read()


def tcp_files(root):
    """List the tcp_*.py modules under root/streaprous/streaprous_socket.

    Args:
        root: repository root.

    Returns:
        sorted list of paths.
    """
    return sorted(glob.glob(os.path.join(root, 'streaprous', 'streaprous_socket', 'tcp_*.py')))


def required_functions(root):
    """Collect the function names the document must mention.

    Args:
        root: repository root.

    Returns:
        sorted list of names: every def in tcp_*.py (without special methods)
        and read_request_raw.
    """
    names = {'read_request_raw'}
    for path in tcp_files(root):
        for node in ast.walk(ast.parse(_read(path))):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not (node.name.startswith('__') and node.name.endswith('__')):
                    names.add(node.name)
    return sorted(names)


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
        text = _read(path)
        lines = [i + 1 for i, line in enumerate(text.split('\n')) if MARKER in line]
        if not lines:
            continue
        if sys.version_info < (3, 8):
            raise RuntimeError('finding the function around a TEAM line needs Python 3.8 '
                               'or later (ast end line numbers); run it on the host')
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


def check(root, doc_path):
    """Run all three checks.

    Args:
        root: repository root.
        doc_path: path of the walkthrough document.

    Returns:
        list of problem strings (empty when everything passes).
    """
    doc = _read(doc_path)
    problems = []
    for name in required_functions(root):
        if name not in doc:
            problems.append('function %s is not in the document' % name)
    for name in team_targets(root):
        if name not in doc:
            problems.append('teacher change %s is not in the document' % name)
    for bad in find_bad_slides(doc):
        problems.append('slide %s in the document is outside the slide map' % bad)
    for path in tcp_files(root):
        for bad in find_bad_slides(_read(path)):
            problems.append('slide %s in %s is outside the slide map'
                            % (bad, os.path.basename(path)))
    return problems


def main(argv=None):
    """Command line entry point.

    Args:
        argv: argument list without the program name (default sys.argv[1:]).

    Returns:
        0 when every check passes, 1 otherwise.
    """
    parser = argparse.ArgumentParser(description='Check the walkthrough document.')
    parser.add_argument('--doc', default=os.path.join(ROOT, DEFAULT_DOC),
                        help='walkthrough document (default %s)' % DEFAULT_DOC)
    args = parser.parse_args(argv)
    try:
        problems = check(ROOT, args.doc)
    except (OSError, RuntimeError) as error:
        print('FAIL: %s' % error)
        return 1
    if problems:
        print('FAIL: %d problems' % len(problems))
        for line in problems:
            print('  ' + line)
        return 1
    print('OK: every function, teacher change and slide number in %s checks out'
          % os.path.basename(args.doc))
    return 0


if __name__ == '__main__':
    sys.exit(main())

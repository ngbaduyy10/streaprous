"""Check that our TCP function bodies stay short.

Usage:
    python check_simple.py [FILE ...]

For every def (methods and nested functions too) prints
<path>:<line> <Qualified.name> <n>, where n counts the lines from the first
statement after the docstring to the end of the function. Then prints the
total number of lines in the files. Exits 1 if a body has more than 30 lines
or, for the default files (streaprous_socket/tcp_*.py), if the total is above 1700. Needs Python 3.8
or later (ast end line numbers); exits 2 on older versions.
"""

import ast
import glob
import os
import sys

MAX_BODY = 30
MAX_TOTAL = 1700
PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def has_docstring(node):
    """Tell whether the first statement of a def is its docstring.

    Args:
         node  a FunctionDef node
    """
    first = node.body[0]
    return (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str))


def body_lines(node):
    """Count the lines from the first statement after the docstring to the end.

    Args:
         node  a FunctionDef node
    """
    statements = node.body[1:] if has_docstring(node) else node.body
    if not statements:
        return 0
    return node.end_lineno - statements[0].lineno + 1


def walk(body, prefix, found):
    """Collect (line, qualified name, n) for every def under body.

    Args:
         body    list of statements
         prefix  qualified name of the parent, with a trailing dot
         found   list to append to
    """
    for item in body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = prefix + item.name
            found.append((item.lineno, name, body_lines(item)))
            walk(item.body, name + '.', found)
        elif isinstance(item, ast.ClassDef):
            walk(item.body, prefix + item.name + '.', found)


def count_file(path):
    """Return the (line, name, n) list of one file and its number of lines.

    Args:
         path  Python file to read
    """
    with open(path, encoding='utf-8') as handle:
        source = handle.read()
    tree = ast.parse(source, path)
    found = []
    walk(tree.body, '', found)
    return found, len(source.splitlines())


def check(paths, limit_total):
    """Print every function with its body length, then the total; return the exit code.

    Args:
         paths        files to check
         limit_total  True to also enforce MAX_TOTAL
    """
    failed = False
    total = 0
    for path in paths:
        found, lines = count_file(path)
        total += lines
        for line, name, n in found:
            print('%s:%d %s %d' % (os.path.relpath(path), line, name, n))
            if n > MAX_BODY:
                failed = True
    print('total %d lines' % total)
    if limit_total and total > MAX_TOTAL:
        failed = True
    if failed:
        return 1
    print('OK: every function body <= %d lines' % MAX_BODY)
    return 0


def main(argv):
    """Run the check on argv files, or on the default tcp_*.py files.

    Args:
         argv  command-line arguments after the script name
    """
    if sys.version_info < (3, 8):
        print('needs Python 3.8')
        return 2
    default = not argv
    if default:
        argv = sorted(glob.glob(os.path.join(PACKAGE_DIR, 'streaprous_socket', 'tcp_*.py')))
    return check(argv, default)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))

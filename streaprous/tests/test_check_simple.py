"""Tests for tools/check_simple.py."""

import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest

PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEEDS_38 = unittest.skipIf(sys.version_info < (3, 8), 'ast end line numbers need Python 3.8')


def load_tool(name):
    """Load streaprous/tools/<name>.py by path."""
    path = os.path.join(PACKAGE_DIR, 'tools', name + '.py')
    spec = importlib.util.spec_from_file_location('streaprous_tool_' + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_simple = load_tool('check_simple')


def write_temp(testcase, source):
    """Write source to a temporary .py file that is removed after the test."""
    handle = tempfile.NamedTemporaryFile('w', suffix='.py', delete=False, encoding='utf-8')
    handle.write(source)
    handle.close()
    testcase.addCleanup(os.remove, handle.name)
    return handle.name


def make_function(lines):
    """Source of one function with a docstring and the given number of body lines."""
    body = ''.join('    x = %d\n' % i for i in range(lines))
    return 'def f():\n    """Doc."""\n' + body


@NEEDS_38
class TestCheckSimple(unittest.TestCase):
    """The body length and total checks."""

    def test_counts_body_without_docstring(self):
        """The count starts at the first statement after the docstring."""
        path = write_temp(self, 'def f():\n    """Doc\n\n    more\n    """\n'
                          '    a = 1\n\n    # note\n    return a\n')
        found, total = check_simple.count_file(path)
        self.assertEqual(found, [(1, 'f', 4)])
        self.assertEqual(total, 9)

    def test_flags_long_function(self):
        """A body of 31 lines fails and 30 lines passes."""
        long_path = write_temp(self, make_function(31))
        short_path = write_temp(self, make_function(30))
        self.assertEqual(check_simple.check([long_path], False), 1)
        self.assertEqual(check_simple.check([short_path], False), 0)

    def test_prints_every_function(self):
        """A short function gets its line too."""
        path = write_temp(self, make_function(3))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            check_simple.check([path], False)
        self.assertIn(':1 f 3', out.getvalue())

    def test_total_limit(self):
        """The total limit applies only when asked."""
        paths = [write_temp(self, make_function(30)) for _ in range(56)]
        self.assertEqual(check_simple.check(paths, False), 0)
        self.assertEqual(check_simple.check(paths, True), 1)


if __name__ == '__main__':
    unittest.main()

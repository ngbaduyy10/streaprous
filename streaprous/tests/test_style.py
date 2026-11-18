"""Docstring and slide-citation checks for our own modules."""

import ast
import glob
import os
import unittest

PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PATTERNS = (
    os.path.join('streaprous_socket', 'tcp_*.py'),
    os.path.join('tools', '*.py'),
    os.path.join('demo', '*.py'),
    os.path.join('docker', '*.py'),
)


def our_files():
    """List the existing files the docstring rules apply to."""
    found = []
    for pattern in PATTERNS:
        found.extend(glob.glob(os.path.join(PACKAGE_DIR, pattern)))
    return sorted(found)


def parse(path):
    """Parse a Python file into a syntax tree."""
    with open(path, encoding='utf-8') as handle:
        return ast.parse(handle.read(), path)


class TestDocstrings(unittest.TestCase):
    """Every module, class and function has a docstring."""

    def test_docstrings(self):
        """Module, class and function docstrings are present."""
        for path in our_files():
            tree = parse(path)
            name = os.path.relpath(path, PACKAGE_DIR)
            with self.subTest(file=name):
                self.assertTrue(ast.get_docstring(tree), name + ': module docstring')
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                         ast.ClassDef)):
                        self.assertTrue(ast.get_docstring(node),
                                        '%s: docstring on %s (line %d)'
                                        % (name, node.name, node.lineno))


if __name__ == '__main__':
    unittest.main()

"""Docstring and slide-citation checks for our own modules."""

import ast
import glob
import os
import re
import unittest

PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PATTERNS = (
    os.path.join('streaprous_socket', 'tcp_*.py'),
    os.path.join('tools', '*.py'),
    os.path.join('demo', '*.py'),
    os.path.join('docker', '*.py'),
)

# module, function (Class.method for methods), slides the docstring must cite
SLIDE_TABLE = (
    ('tcp_segment', 'unwrap', ('3-79',)),
    ('tcp_rtt', 'RttEstimator.add_sample', ('3-82', '3-83')),
    ('tcp_rtt', 'RttEstimator.back_off', ('3-84',)),
    ('tcp_reno', 'RenoCongestion.on_new_ack', ('3-123', '3-125')),
    ('tcp_reno', 'RenoCongestion.on_duplicate_ack', ('3-125',)),
    ('tcp_reno', 'RenoCongestion.on_timeout', ('3-124', '3-125')),
    ('tcp_sender', 'TcpSender._usable_window', ('3-122', '3-94')),
    ('tcp_sender', 'TcpSender._send_allowed', ('3-122',)),
    ('tcp_sender', 'TcpSender._start_timer', ('3-84',)),
    ('tcp_sender', 'TcpSender._on_new_ack', ('3-84', '3-87')),
    ('tcp_sender', 'TcpSender._on_duplicate_ack', ('3-88',)),
    ('tcp_sender', 'TcpSender._fast_retransmit', ('3-88',)),
    ('tcp_sender', 'TcpSender._on_timeout', ('3-84', '3-86')),
    ('tcp_sender', 'TcpSender._take_rtt_sample', ('3-81',)),
    ('tcp_sender', 'TcpSender.update_rwnd', ('3-94',)),
    ('tcp_sender', 'TcpSender.send_probe', ('3-94',)),
    ('tcp_receiver', 'TcpReceiver.on_segment', ('3-85',)),
    ('tcp_receiver', 'TcpReceiver._deliver_in_order', ('3-80',)),
    ('tcp_receiver', 'TcpReceiver._store_out_of_order', ('3-85',)),
    ('tcp_receiver', 'TcpReceiver.on_tick', ('3-85',)),
    ('tcp_receiver', 'TcpReceiver.rwnd', ('3-94',)),
    ('tcp_receiver', 'TcpReceiver.window_update_due', ('3-94',)),
    ('tcp_connection', 'TcpConnection._send_ack', ('3-85', '3-95')),
    ('tcp_connection', 'TcpConnection.close', ('3-96',)),
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


def find_function(tree, dotted):
    """Find a function or method by name ('func' or 'Class.method')."""
    parts = dotted.split('.')
    body = tree.body
    node = None
    for part in parts:
        node = None
        for item in body:
            if (isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                    and item.name == part):
                node = item
                break
        if node is None:
            return None
        body = node.body
    return node


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


class TestSlideTable(unittest.TestCase):
    """Mechanism functions cite their slides."""

    def test_slides(self):
        """Each existing mechanism function names its slides in its docstring."""
        for module, dotted, slides in SLIDE_TABLE:
            path = os.path.join(PACKAGE_DIR, 'streaprous_socket', module + '.py')
            if not os.path.exists(path):
                continue
            with self.subTest(function=module + '.' + dotted):
                node = find_function(parse(path), dotted)
                self.assertIsNotNone(node, '%s has no %s' % (module, dotted))
                doc = ast.get_docstring(node) or ''
                for slide in slides:
                    pattern = r'(?<![\d-])' + re.escape(slide) + r'(?!\d)'
                    self.assertTrue(re.search(pattern, doc),
                                    '%s.%s docstring lacks slide %s' % (module, dotted, slide))


if __name__ == '__main__':
    unittest.main()

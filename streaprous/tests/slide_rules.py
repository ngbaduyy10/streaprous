"""Helper that checks slide rules in the docstrings of our TCP modules."""

import ast
import os
import re

PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def normalize(text):
    """Lowercase the text and delete every character outside a-z 0-9 # + * / = < > ( ) -."""
    return re.sub(r'[^a-z0-9#+*/=<>()-]', '', text.lower())


def find_function(path, dotted):
    """Find a function or method in a file by name ('func' or 'Class.method').

    Returns:
         the ast node, or None
    """
    with open(path, encoding='utf-8') as handle:
        tree = ast.parse(handle.read(), path)
    body = tree.body
    node = None
    for part in dotted.split('.'):
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


def assert_slide_rules(testcase, module, rules):
    """Check each (function, slide, phrase) row against the module's docstrings.

    Args:
         testcase  the running unittest.TestCase
         module    module name in streaprous_socket, such as 'tcp_rtt'
         rules     list of (dotted function name, slide or 'extra', phrase)
    """
    path = os.path.join(PACKAGE_DIR, 'streaprous_socket', module + '.py')
    for dotted, slide, phrase in rules:
        with testcase.subTest(function=dotted, slide=slide, phrase=phrase):
            node = find_function(path, dotted)
            testcase.assertIsNotNone(node, '%s has no %s' % (module, dotted))
            doc = ast.get_docstring(node) or ''
            if slide == 'extra':
                testcase.assertIn('extra:', doc, '%s lacks extra:' % dotted)
            else:
                pattern = r'(?<![\d-])' + re.escape(slide) + r'(?!\d)'
                testcase.assertTrue(re.search(pattern, doc),
                                    '%s docstring lacks slide %s' % (dotted, slide))
            testcase.assertIn(normalize(phrase), normalize(doc),
                              '%s docstring lacks: %s' % (dotted, phrase))

"""Tests for tools/check_hunks.py and tools/check_walkthrough.py."""

import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest

PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_tool(name):
    """Load streaprous/tools/<name>.py by path (the repo root has its own tools package)."""
    path = os.path.join(PACKAGE_DIR, 'tools', name + '.py')
    spec = importlib.util.spec_from_file_location('streaprous_tool_' + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_hunks = load_tool('check_hunks')
check_walkthrough = load_tool('check_walkthrough')

NEEDS_38 = unittest.skipIf(sys.version_info < (3, 8), 'ast end line numbers need Python 3.8')

TEAM_DIFF = '''diff --git a/streaprous/streaprous_socket/a.py b/streaprous/streaprous_socket/a.py
index 111..222 100644
--- a/streaprous/streaprous_socket/a.py
+++ b/streaprous/streaprous_socket/a.py
@@ -1,3 +1,4 @@
 import os
+import sys  # TEAM: needed
 x = 1
 y = 2
@@ -20,3 +21,4 @@ def f():
     a = 1
+    b = 2
     c = 3
     d = 4
diff --git a/streaprous/streaprous_socket/new.py b/streaprous/streaprous_socket/new.py
new file mode 100644
index 0000000..333
--- /dev/null
+++ b/streaprous/streaprous_socket/new.py
@@ -0,0 +1,2 @@
+a = 1
+b = 2
'''

OLD_SRC = '''class Request:
    def prepare(self):
        a = 1

    def other(self):
        b = 1
'''

NEW_SRC = '''class Request:
    def prepare(self):
        a = 1
        a2 = 2  # TEAM: new

    def other(self):
        b = 1
'''

DAEMON_OK = '''diff --git a/streaprous/daemon/request.py b/streaprous/daemon/request.py
--- a/streaprous/daemon/request.py
+++ b/streaprous/daemon/request.py
@@ -1,6 +1,7 @@
 class Request:
     def prepare(self):
         a = 1
+        a2 = 2  # TEAM: new
+
     def other(self):
         b = 1
'''

DAEMON_BAD = '''diff --git a/streaprous/daemon/request.py b/streaprous/daemon/request.py
--- a/streaprous/daemon/request.py
+++ b/streaprous/daemon/request.py
@@ -2,5 +2,6 @@
     def prepare(self):
         a = 1
+        a2 = 2  # TEAM: new
     def other(self):
-        b = 1
+        b = 2
'''

# the blank added line sits between two methods
OK_OLD = '''class Request:
    def prepare(self):
        a = 1
    def other(self):
        b = 1
'''
OK_NEW = '''class Request:
    def prepare(self):
        a = 1
        a2 = 2  # TEAM: new

    def other(self):
        b = 1
'''
BAD_NEW = '''class Request:
    def prepare(self):
        a = 1
        a2 = 2  # TEAM: new
    def other(self):
        b = 2
'''


class TestParseDiff(unittest.TestCase):
    """Hunk parsing and the TEAM rule."""

    def test_parse(self):
        """Two files, three hunks, line numbers follow the diff."""
        files = check_hunks.parse_diff(TEAM_DIFF)
        self.assertEqual([f['path'] for f in files],
                         ['streaprous/streaprous_socket/a.py',
                          'streaprous/streaprous_socket/new.py'])
        self.assertEqual([f['is_new'] for f in files], [False, True])
        added = [line for line in files[0]['hunks'][0]['lines'] if line[0] == '+']
        self.assertEqual(added[0][2], 2)

    def test_team_rule_and_new_file(self):
        """The hunk without a TEAM line is reported; the new file is skipped."""
        hunks, nfiles, problems = check_hunks.check_diff(TEAM_DIFF, None, None)
        self.assertEqual((hunks, nfiles), (2, 1))
        self.assertEqual(len(problems), 1)
        self.assertIn('@@ -20,3 +21,4 @@', problems[0])

    def test_empty_diff(self):
        """No changes means no hunks."""
        self.assertEqual(check_hunks.check_diff('', None, None), (0, 0, []))


@NEEDS_38
class TestDaemonScope(unittest.TestCase):
    """Only prepare, handle_client_raw and read_request_raw may change."""

    def check(self, diff, old, new):
        """Run check_diff with literal sources."""
        return check_hunks.check_diff(diff, lambda path: old, lambda path: new)

    def test_blank_line_between_methods(self):
        """A blank added line between methods is accepted."""
        hunks, nfiles, problems = self.check(DAEMON_OK, OK_OLD, OK_NEW)
        self.assertEqual((hunks, nfiles, problems), (1, 1, []))

    def test_other_method_rejected(self):
        """A changed line in another method is rejected."""
        _, _, problems = self.check(DAEMON_BAD, OK_OLD, BAD_NEW)
        self.assertEqual(len(problems), 1)
        self.assertIn('b = 2', problems[0])
        self.assertIn('b = 1', problems[0])

    def test_spans(self):
        """allowed_spans finds only the listed methods."""
        self.assertEqual(check_hunks.allowed_spans(NEW_SRC), [(2, 4)])
        self.assertEqual(check_hunks.allowed_spans(OLD_SRC), [(2, 3)])


class TestSlides(unittest.TestCase):
    """Slide-number rule."""

    def test_slide_numbers(self):
        """3-89 is outside the slide map, 3-88 is inside."""
        self.assertEqual(check_walkthrough.find_bad_slides('see slide 3-89'), ['3-89'])
        self.assertEqual(check_walkthrough.find_bad_slides('see slide 3-88 and 3-125'), [])
        self.assertEqual(check_walkthrough.find_bad_slides('date 2026-11-14, v3-88x'), [])


class TestWalkthrough(unittest.TestCase):
    """Walkthrough check on a minimal valid document in a temporary root."""

    FILES = {'R': 'tcp_sender.py:TcpSender.send_data',
             'F': 'tcp_receiver.py:TcpReceiver.rwnd',
             'C': 'tcp_reno.py:RenoCongestion.on_timeout, tcp_connection.py:choose_mss'}
    REASON = 'extra: we need it for this reason'

    def setUp(self):
        """Build a fake repository: tcp modules, a teacher module and a valid document."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        sock = os.path.join(self.root, 'streaprous', 'streaprous_socket')
        os.makedirs(sock)
        self.write(os.path.join(sock, 'tcp_sender.py'),
                   'class TcpSender:\n    def send_data(self):\n        pass\n')
        self.write(os.path.join(sock, 'tcp_receiver.py'),
                   'class TcpReceiver:\n    def rwnd(self):\n        pass\n')
        self.write(os.path.join(sock, 'tcp_reno.py'),
                   'class RenoCongestion:\n    def on_timeout(self):\n        pass\n')
        self.write(os.path.join(sock, 'tcp_connection.py'),
                   '"""Slide 3-88."""\n\n\ndef choose_mss():\n    pass\n')
        self.write(os.path.join(sock, 'teacher.py'),
                   'def accept():\n    x = 1\n    y = 2  # TEAM: hook\n    return x + y\n')
        self.doc = os.path.join(self.root, 'doc.md')
        self.text = self.valid_doc()
        self.write(self.doc, self.text)

    def write(self, path, text):
        """Write a text file."""
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(text)

    def row(self, slides, words, function, mark):
        """Return one table row."""
        return '| %s | %s | %s | %s |' % (slides, words, function, mark)

    def pair_section(self, pair, title):
        """Return the lines of one pair section built from RULES and EXTRAS."""
        out = ['## ' + title, '', '### Slide rules', '',
               "| Slide | The slide's words | Our function | Mark |",
               '| --- | --- | --- | --- |']
        for rule_pair, slides, quote in check_walkthrough.RULES:
            if rule_pair == pair:
                out.append(self.row(slides, quote, self.FILES[pair], 'as the slide'))
        for extra_pair, key in check_walkthrough.EXTRAS:
            if extra_pair == pair:
                out.append(self.row('extra', key, self.FILES[pair], self.REASON))
        out += ['', '### Traced example', '', 'cwnd goes 1, 2, 4.', '',
                '### Known limits', '', '- One limit. Answer: we keep it.', '',
                '### Questions to expect', '']
        for number in range(4):
            out += ['Q: question %d?' % number, 'A: answer %d.' % number]
        out += ['', '### Self-check', '', '1. First? Short.', '2. Second? Short.',
                '3. Third? Short.', '']
        return out

    def valid_doc(self):
        """Return a minimal document that passes every check."""
        out = ['## Before you start', '', 'Words: byte stream, segment.', '',
               '    python -m unittest discover -s streaprous/tests -t streaprous',
               '    python streaprous/docker/lab.py test', '']
        out += self.pair_section('R', 'Reliable data transfer')
        out += self.pair_section('F', 'Flow control')
        out += self.pair_section('C', 'Congestion control')
        out += ["## Our hooks in the teacher's files", '', '| Function | Hook |',
                '| --- | --- |', '| `accept` | one line |', '',
                '## Background', '', 'RFC 6298 sets the minimum timeout.', '']
        return '\n'.join(out)

    def problems_for(self, text):
        """Write text as the document and return the problems found."""
        self.write(self.doc, text)
        return check_walkthrough.check(self.root, self.doc)

    def assert_problem(self, text, expected):
        """Assert that the mutated document gets a problem containing expected."""
        problems = self.problems_for(text)
        self.assertTrue(any(expected in p for p in problems),
                        '%r not found in %r' % (expected, problems))

    def mutate(self, old, new, count=1):
        """Return the valid text with old replaced by new."""
        self.assertIn(old, self.text)
        return self.text.replace(old, new, count)

    def test_tables(self):
        """The tables hold 20 rules and 12 extras."""
        self.assertEqual(len(check_walkthrough.RULES), 20)
        self.assertEqual(len(check_walkthrough.EXTRAS), 12)

    def test_valid_document(self):
        """The valid document passes, also through main."""
        self.assertEqual(self.problems_for(self.text), [])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = check_walkthrough.main(['--root', self.root, '--doc', self.doc])
        self.assertEqual(code, 0)
        self.assertIn('OK: walkthrough shape, 20 slide rules, 12 extras', out.getvalue())

    def test_main_fails(self):
        """main prints FAIL and returns 1 on a bad document."""
        self.write(self.doc, self.text + 'demo\n')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = check_walkthrough.main(['--root', self.root, '--doc', self.doc])
        self.assertEqual(code, 1)
        self.assertIn('FAIL', out.getvalue())

    def test_normalize(self):
        """Normalization keeps only a-z 0-9 # + * / = < > ( ) - after lowercasing."""
        self.assertEqual(check_walkthrough.normalize('A b, "c" \\| 3-1 (x)*y.'), 'abc3-1(x)*y')

    def test_1_too_long(self):
        """Check 1: more than 600 lines."""
        self.assert_problem(self.text + '\nx' * 600, 'more than 600')

    def test_2_headings(self):
        """Check 2: a wrong ## heading, and a missing or misplaced ### subsection."""
        self.assert_problem(self.mutate('## Flow control', '## Flow'), 'the ## headings')
        self.assert_problem(self.mutate('### Self-check', '### Review'), '### subsections')
        self.assert_problem(self.mutate('### Traced example', '### Known limits'),
                            '### subsections')

    def test_3_table_header(self):
        """Check 3: the table header is missing."""
        self.assert_problem(self.mutate('| Our function |', '| Function |'), 'no table')

    def test_4_canonical_rules(self):
        """Check 4: words changed, slide missing, wrong mark, rule twice."""
        self.assert_problem(self.mutate('ignore retransmissions', 'ignore all'),
                            'canonical rule for 3-81')
        self.assert_problem(self.mutate('| 3-86, 3-87 |', '| 3-86 |'), 'lacks slide 3-87')
        row = self.row('3-122', 'LastByteSent - LastByteAcked <= cwnd', self.FILES['C'],
                       'as the slide')
        wrong = self.row('3-122', 'LastByteSent - LastByteAcked <= cwnd', self.FILES['C'],
                         self.REASON)
        self.assert_problem(self.mutate(row, wrong), 'not marked')
        self.assert_problem(self.mutate(row, row + '\n' + row), 'matches 2 rows')

    def test_5_extras(self):
        """Check 5: extra key missing, twice, or with a short reason."""
        row = self.row('extra', 'round down', self.FILES['C'], self.REASON)
        self.assert_problem(self.mutate(row, ''), 'extra key "round down"')
        self.assert_problem(self.mutate('| ' + self.REASON + ' |', '| extra: why |'),
                            'fewer than 5 words')
        self.assert_problem(self.mutate(row, row + '\n' + row), 'is in 2 extra rows')

    def test_6_marks(self):
        """Check 6: unknown mark and the word differs."""
        self.assert_problem(self.mutate('| as the slide |', '| roughly |'), 'neither')
        self.assert_problem(self.mutate('| as the slide |', '| differs from the slide |'),
                            'differs')

    def test_7_function_cells(self):
        """Check 7: unknown method, unknown file, bad item."""
        for bad in ('tcp_sender.py:TcpSender.nope', 'tcp_missing.py:Thing', 'just words'):
            self.assert_problem(self.mutate(self.FILES['R'], bad), 'does not exist')

    def test_8_known_limits(self):
        """Check 8: no bullet, or a bullet without Answer:."""
        self.assert_problem(self.mutate('Answer: we keep it.', 'We keep it.'), 'no Answer')
        self.assert_problem(self.mutate('- One limit. Answer: we keep it.', 'One limit.'),
                            'no "- " bullet')

    def test_9_questions(self):
        """Check 9: too few Q lines, or a Q without A."""
        self.assert_problem(self.mutate('Q: question 3?\nA: answer 3.\n', ''), 'at least 4')
        self.assert_problem(self.mutate('A: answer 0.', 'answer 0.'), 'no A: line')

    def test_10_self_check(self):
        """Check 10: fewer than 3 numbered items."""
        self.assert_problem(self.mutate('3. Third? Short.', 'Third? Short.'),
                            'Self-check has 2')

    def test_11_slide_map(self):
        """Check 11: a slide outside the map, in the document and in a module."""
        self.assert_problem(self.mutate('cwnd goes', 'slide 3-89, cwnd goes'),
                            'slide 3-89 in the document')
        inside = self.mutate('cwnd goes', 'slides 3-146 and 3-101, cwnd goes')
        self.assertEqual(self.problems_for(inside), [])
        path = os.path.join(self.root, 'streaprous', 'streaprous_socket', 'tcp_reno.py')
        self.write(path, '"""Slide 3-126."""\n')
        self.assert_problem(self.text, 'slide 3-126 in tcp_reno.py')

    @NEEDS_38
    def test_12_team_function(self):
        """Check 12: a teacher function with a TEAM line is not named."""
        self.assert_problem(self.mutate('`accept`', '`other`'), 'teacher change accept')

    def test_13_test_commands(self):
        """Check 13: each test command is needed."""
        self.assert_problem(self.mutate('python streaprous/docker/lab.py test', 'x'),
                            'test command is missing')
        self.assert_problem(self.mutate('python -m unittest discover -s streaprous/tests '
                                        '-t streaprous', 'x'), 'test command is missing')

    def test_14_demo(self):
        """Check 14: demo and rehearsal words."""
        self.assert_problem(self.text + '\nRun the Demo.\n', 'demo or rehearsal')
        self.assert_problem(self.text + '\nthe rehearsal\n', 'demo or rehearsal')

    def test_15_outside_sources(self):
        """Check 15: outside sources only under Background, or flagged as background."""
        self.assert_problem(self.mutate('Words: byte', 'RFC 793 says. Words: byte'),
                            'without saying background')
        ok = self.mutate('Words: byte', 'RFC 793, see Background. Words: byte')
        self.assertEqual(self.problems_for(ok), [])


if __name__ == '__main__':
    unittest.main()

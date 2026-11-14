"""Tests for tools/check_hunks.py and tools/check_walkthrough.py."""

import importlib.util
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
    """Walkthrough check on a tiny repository in a temp directory."""

    def setUp(self):
        """Build streaprous/ with one tcp module and one teacher module."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        sock = os.path.join(self.root, 'streaprous', 'streaprous_socket')
        os.makedirs(sock)
        self.write(os.path.join(sock, 'tcp_demo.py'),
                   '"""Demo."""\n\n\ndef do_thing():\n    """Slide 3-88."""\n\n\n'
                   'class Box:\n    def __init__(self):\n        pass\n\n'
                   '    def open_it(self):\n        pass\n')
        self.write(os.path.join(sock, 'teacher.py'),
                   'def accept():\n    x = 1\n    y = 2  # TEAM: hook\n    return x + y\n')
        self.write(os.path.join(self.root, 'streaprous', 'top.py'),
                   'import os  # TEAM: import\n')
        self.doc = os.path.join(self.root, 'doc.md')

    def write(self, path, text):
        """Write a text file."""
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(text)

    def test_required_functions(self):
        """Special methods are not required; read_request_raw always is."""
        self.assertEqual(check_walkthrough.required_functions(self.root),
                         ['do_thing', 'open_it', 'read_request_raw'])

    @NEEDS_38
    def test_complete_doc(self):
        """A document naming everything passes."""
        self.write(self.doc, 'do_thing open_it read_request_raw accept top.py slide 3-88')
        self.assertEqual(check_walkthrough.check(self.root, self.doc), [])

    @NEEDS_38
    def test_missing_names_and_bad_slide(self):
        """Missing names and a slide outside the map are reported."""
        self.write(self.doc, 'do_thing open_it slide 3-89')
        problems = check_walkthrough.check(self.root, self.doc)
        text = '\n'.join(problems)
        self.assertIn('read_request_raw', text)
        self.assertIn('accept', text)
        self.assertIn('top.py', text)
        self.assertIn('3-89', text)
        self.assertEqual(len(problems), 4)

    def test_bad_slide_in_module(self):
        """A slide outside the map in a tcp module is reported."""
        self.write(os.path.join(self.root, 'streaprous', 'streaprous_socket', 'tcp_demo.py'),
                   '"""Demo 3-89."""\n')
        self.write(os.path.join(self.root, 'streaprous', 'streaprous_socket', 'teacher.py'),
                   'x = 1\n')
        self.write(os.path.join(self.root, 'streaprous', 'top.py'), 'y = 1\n')
        self.write(self.doc, 'read_request_raw')
        problems = check_walkthrough.check(self.root, self.doc)
        self.assertEqual(len(problems), 1)
        self.assertIn('3-89', problems[0])


if __name__ == '__main__':
    unittest.main()

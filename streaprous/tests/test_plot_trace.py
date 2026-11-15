"""Tests for tools/plot_trace.py."""

import importlib.util
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET

PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_tool(name):
    """Load streaprous/tools/<name>.py by path (the repo root has its own tools package)."""
    path = os.path.join(PACKAGE_DIR, 'tools', name + '.py')
    spec = importlib.util.spec_from_file_location('streaprous_tool_' + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plot_trace = load_tool('plot_trace')

HEADER = 'time,event,cwnd,ssthresh,rwnd,in_flight,snd_una,snd_nxt,adv_wnd\n'
ROWS = (
    '0.0000,OPEN,536,32768,65535,0,1,1,32768\n'
    '0.5000,SEND,1072,32768,65535,536,1,537,32768\n'
    '1.0000,TIMEOUT,536,1072,65535,536,1,537,32768\n'
    '1.5000,FAST_RETRANSMIT,2144,1072,65535,536,537,1073,16384\n'
    '2.0000,PROBE,2144,1072,0,0,537,1073,0\n'
    '2.5000,WINDOW_ZERO,2144,1072,0,0,537,1073,0\n'
    '3.0000,WINDOW_UPDATE,2144,1072,65535,0,537,1073,32768\n'
)
NS = '{http://www.w3.org/2000/svg}'


class TestPlotTrace(unittest.TestCase):
    """SVG output of the chart tool."""

    def setUp(self):
        """Write a small trace CSV into a temp directory."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.csv = os.path.join(self.tmp.name, 'trace_1.2.3.4_5000.csv')
        with open(self.csv, 'w') as handle:
            handle.write(HEADER + ROWS)

    def render(self, window):
        """Render the temp CSV and parse the SVG."""
        rows = plot_trace.read_trace(self.csv)
        return ET.fromstring(plot_trace.render_svg(rows, window, 'trace.csv'))

    def test_default_chart(self):
        """Default mode has three polylines and the three event markers."""
        root = self.render(False)
        names = [p.get('data-name') for p in root.iter(NS + 'polyline')]
        self.assertEqual(names, ['cwnd', 'ssthresh', 'rwnd'])
        events = [c.get('data-event') for c in root.iter(NS + 'circle') if c.get('data-event')]
        self.assertEqual(sorted(events), ['FAST_RETRANSMIT', 'PROBE', 'TIMEOUT'])

    def test_window_mode(self):
        """Window mode plots adv_wnd and marks zero and update."""
        root = self.render(True)
        polys = list(root.iter(NS + 'polyline'))
        self.assertEqual([p.get('data-name') for p in polys], ['adv_wnd'])
        # the last row advertises 32768 bytes = 32 KB, the highest point
        top = min(float(pt.split(',')[1]) for pt in polys[0].get('points').split())
        last = float(polys[0].get('points').split()[-1].split(',')[1])
        self.assertEqual(top, last)
        events = [c.get('data-event') for c in root.iter(NS + 'circle') if c.get('data-event')]
        self.assertEqual(sorted(events), ['WINDOW_UPDATE', 'WINDOW_ZERO'])

    def test_title_and_size(self):
        """The title shows the file name and the size is 900 by 450."""
        root = self.render(False)
        self.assertEqual((root.get('width'), root.get('height')), ('900', '450'))
        texts = [t.text for t in root.iter(NS + 'text')]
        self.assertIn('trace.csv', texts)

    def test_main_writes_svg(self):
        """main writes the SVG next to the CSV and prints the counts."""
        import io
        import contextlib
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = plot_trace.main([self.csv])
        self.assertEqual(code, 0)
        out = os.path.splitext(self.csv)[0] + '.svg'
        self.assertTrue(os.path.exists(out))
        ET.parse(out)
        self.assertIn('TIMEOUT=1', buffer.getvalue())

    def test_main_non_ascii_path_on_narrow_console(self):
        """main still works when stdout cannot encode the folder name."""
        import io
        import contextlib
        folder = os.path.join(self.tmp.name, 'M' + chr(0x1ea1) + 'ng')
        os.makedirs(folder)
        out = os.path.join(folder, 'cwnd.svg')
        raw = io.BytesIO()
        narrow = io.TextIOWrapper(raw, encoding='cp1252')
        with contextlib.redirect_stdout(narrow):
            code = plot_trace.main([self.csv, '-o', out])
            narrow.flush()
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(out))
        self.assertIn(b'M\\u1ea1ng', raw.getvalue())

    def test_empty_trace(self):
        """A trace without rows still gives a valid SVG."""
        ET.fromstring(plot_trace.render_svg([], False))


if __name__ == '__main__':
    unittest.main()

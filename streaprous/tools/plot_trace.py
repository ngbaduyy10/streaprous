"""Draw a TCP trace CSV as an SVG chart.

Usage:
    python plot_trace.py TRACE.csv [-o OUT.svg] [--window]

The default chart shows cwnd, ssthresh and rwnd in KB against time (slides
3-122 to 3-125); --window shows the window the server advertises (slide 3-94).
Standard library only, so it runs wherever the trace file is.
"""

import argparse
import csv
import os
import sys
from xml.sax.saxutils import escape

WIDTH = 900
HEIGHT = 450
MARGIN_LEFT = 70
MARGIN_RIGHT = 20
MARGIN_TOP = 50
MARGIN_BOTTOM = 90

NUMERIC_COLUMNS = ('time', 'cwnd', 'ssthresh', 'rwnd', 'in_flight', 'snd_una', 'snd_nxt',
                   'adv_wnd')

# name, CSV column, colour, stroke width, dash pattern
SERIES = (
    ('cwnd', 'cwnd', '#1f6feb', 2.5, ''),
    ('ssthresh', 'ssthresh', '#555555', 1.5, '6,4'),
    ('rwnd', 'rwnd', '#2da44e', 1.0, ''),
)
WINDOW_SERIES = (('adv_wnd', 'adv_wnd', '#1f6feb', 2.0, ''),)

# event, colour
MARKERS = (('TIMEOUT', '#d1242f'), ('FAST_RETRANSMIT', '#e8801a'), ('PROBE', '#0969da'))
WINDOW_MARKERS = (('WINDOW_ZERO', '#d1242f'), ('WINDOW_UPDATE', '#2da44e'))


def read_trace(path):
    """Read a trace CSV into a list of row dictionaries.

    Args:
        path: path of the CSV written by TcpTrace.

    Returns:
        list of dicts; the numeric columns are floats (0.0 when missing),
        'event' is a string.
    """
    rows = []
    with open(path, newline='') as handle:
        for raw in csv.DictReader(handle):
            row = {'event': raw.get('event', '')}
            for name in NUMERIC_COLUMNS:
                try:
                    row[name] = float(raw.get(name) or 0)
                except ValueError:
                    row[name] = 0.0
            rows.append(row)
    return rows


def count_events(rows):
    """Count rows per event name, in order of first appearance.

    Args:
        rows: list returned by read_trace.

    Returns:
        list of (event, count) pairs.
    """
    counts = {}
    order = []
    for row in rows:
        if row['event'] not in counts:
            counts[row['event']] = 0
            order.append(row['event'])
        counts[row['event']] += 1
    return [(name, counts[name]) for name in order]


def nice_ceiling(value):
    """Round a positive value up to 1, 2, 5 or 10 times a power of ten.

    Args:
        value: the largest value to show.

    Returns:
        a rounded-up axis limit (1.0 when value is not positive).
    """
    if value <= 0:
        return 1.0
    scale = 1.0
    while value > 10 * scale:
        scale *= 10
    while value <= scale / 10:
        scale /= 10
    for step in (1, 2, 5, 10):
        if value <= step * scale:
            return step * scale
    return 10 * scale


def _fmt(number):
    """Format an axis number without a trailing zero."""
    text = '%.2f' % number
    return text.rstrip('0').rstrip('.')


def _dash(dash):
    """Return the stroke-dasharray attribute text for a dash pattern."""
    return ' stroke-dasharray="%s"' % dash if dash else ''


def render_svg(rows, window_mode=False, title='TCP trace'):
    """Build the SVG text of the chart.

    Args:
        rows: list returned by read_trace.
        window_mode: False for cwnd/ssthresh/rwnd, True for adv_wnd.
        title: text shown above the plot (the CSV file name).

    Returns:
        the SVG document as a string.
    """
    series = WINDOW_SERIES if window_mode else SERIES
    markers = WINDOW_MARKERS if window_mode else MARKERS
    plot_w = WIDTH - MARGIN_LEFT - MARGIN_RIGHT
    plot_h = HEIGHT - MARGIN_TOP - MARGIN_BOTTOM
    x_max = nice_ceiling(max([r['time'] for r in rows] or [0]))
    y_top = max([r[col] / 1024.0 for r in rows for _, col, _, _, _ in series] or [0])
    y_max = nice_ceiling(y_top)

    def px(time_value):
        """Map a time to an x pixel."""
        return MARGIN_LEFT + plot_w * time_value / x_max

    def py(kb_value):
        """Map a KB value to a y pixel."""
        return MARGIN_TOP + plot_h * (1 - kb_value / y_max)

    out = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
           'viewBox="0 0 %d %d" font-family="sans-serif" font-size="12">'
           % (WIDTH, HEIGHT, WIDTH, HEIGHT),
           '<rect width="%d" height="%d" fill="#ffffff"/>' % (WIDTH, HEIGHT),
           '<text x="%d" y="28" font-size="16" text-anchor="middle">%s</text>'
           % (WIDTH // 2, escape(title))]
    bottom = MARGIN_TOP + plot_h
    for i in range(6):
        y_value = y_max * i / 5.0
        out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#dddddd"/>'
                   % (MARGIN_LEFT, py(y_value), MARGIN_LEFT + plot_w, py(y_value)))
        out.append('<text x="%d" y="%.1f" text-anchor="end">%s</text>'
                   % (MARGIN_LEFT - 6, py(y_value) + 4, _fmt(y_value)))
        x_value = x_max * i / 5.0
        out.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" stroke="#999999"/>'
                   % (px(x_value), bottom, px(x_value), bottom + 5))
        out.append('<text x="%.1f" y="%d" text-anchor="middle">%s</text>'
                   % (px(x_value), bottom + 20, _fmt(x_value)))
    out.append('<rect x="%d" y="%d" width="%d" height="%d" fill="none" stroke="#333333"/>'
               % (MARGIN_LEFT, MARGIN_TOP, plot_w, plot_h))
    out.append('<text x="%d" y="%d" text-anchor="middle">time (s)</text>'
               % (MARGIN_LEFT + plot_w // 2, bottom + 40))
    out.append('<text x="16" y="%d" text-anchor="middle" transform="rotate(-90 16 %d)">'
               'KB</text>' % (MARGIN_TOP + plot_h // 2, MARGIN_TOP + plot_h // 2))

    for name, col, colour, width, dash in series:
        points = ' '.join('%.1f,%.1f' % (px(r['time']), py(r[col] / 1024.0)) for r in rows)
        out.append('<polyline class="series" data-name="%s" fill="none" stroke="%s" '
                   'stroke-width="%s"%s points="%s"/>'
                   % (name, colour, width, _dash(dash), points))

    marker_col = series[0][1]
    for event, colour in markers:
        for r in rows:
            if r['event'] == event:
                out.append('<circle class="marker" data-event="%s" cx="%.1f" cy="%.1f" r="5" '
                           'fill="%s" stroke="#ffffff"/>'
                           % (event, px(r['time']), py(r[marker_col] / 1024.0), colour))

    legend_y = bottom + 62
    legend_x = MARGIN_LEFT
    for name, _, colour, width, dash in series:
        out.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="%s" stroke-width="%s"%s/>'
                   % (legend_x, legend_y, legend_x + 24, legend_y, colour, width, _dash(dash)))
        out.append('<text x="%d" y="%d">%s</text>' % (legend_x + 30, legend_y + 4, name))
        legend_x += 40 + 8 * len(name) + 24
    for event, colour in markers:
        out.append('<circle cx="%d" cy="%d" r="5" fill="%s"/>' % (legend_x + 5, legend_y, colour))
        out.append('<text x="%d" y="%d">%s</text>' % (legend_x + 16, legend_y + 4, event))
        legend_x += 30 + 8 * len(event) + 16
    out.append('</svg>')
    return '\n'.join(out) + '\n'


def print_line(text):
    """Print one line, escaping characters the console cannot show.

    On Windows a piped stdout often uses a code page without letters such as
    the ones in our folder names, and a plain print() would stop the tool.

    Args:
        text: the line to print.
    """
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, 'encoding', None) or 'ascii'
        print(text.encode(encoding, 'backslashreplace').decode(encoding))


def main(argv=None):
    """Command line entry point.

    Args:
        argv: argument list without the program name (default sys.argv[1:]).

    Returns:
        0 on success.
    """
    parser = argparse.ArgumentParser(description='Plot a TCP trace CSV as SVG.')
    parser.add_argument('trace', help='trace CSV file')
    parser.add_argument('-o', '--output', help='SVG path (default: CSV path with .svg)')
    parser.add_argument('--window', action='store_true',
                        help='plot the advertised window instead of cwnd')
    args = parser.parse_args(argv)
    rows = read_trace(args.trace)
    out_path = args.output or os.path.splitext(args.trace)[0] + '.svg'
    svg = render_svg(rows, args.window, os.path.basename(args.trace))
    with open(out_path, 'w') as handle:
        handle.write(svg)
    print_line(out_path)
    print_line('events: ' + ' '.join('%s=%d' % pair for pair in count_events(rows)))
    return 0


if __name__ == '__main__':
    sys.exit(main())

"""
make_files
~~~~~~~~~~

Generates the two download files of the demos. The content is deterministic, so
the server and the client can each rebuild it and compare checksums.

Run from the streaprous folder:  python demo/make_files.py
"""

import hashlib
import os

# File name -> size in bytes.
DEMO_FILES = {
    'demo_256k.bin': 256 * 1024,
    'demo_1m.bin': 1024 * 1024,
}


def demo_content(name):
    """
    Build the bytes of a demo file.

    The file is the concatenation of sha256(b"streaprous-demo:" + name + b":" + str(i))
    for i = 0, 1, 2, ... cut to the file's size.

    Args:
        name (str): "demo_256k.bin" or "demo_1m.bin".

    Returns:
        bytes of the file.
    """
    if name not in DEMO_FILES:
        raise ValueError('unknown demo file: {}'.format(name))
    size = DEMO_FILES[name]
    prefix = b'streaprous-demo:' + name.encode('ascii') + b':'
    blocks = []
    have = 0
    index = 0
    while have < size:
        block = hashlib.sha256(prefix + str(index).encode('ascii')).digest()
        blocks.append(block)
        have += len(block)
        index += 1
    return b''.join(blocks)[:size]


def main():
    """
    Write the demo files into streaprous/apps and print one line for each.

    Returns:
        0.
    """
    apps_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'apps')
    for name in sorted(DEMO_FILES, key=DEMO_FILES.get):
        data = demo_content(name)
        with open(os.path.join(apps_dir, name), 'wb') as handle:
            handle.write(data)
        print('{} {} {}'.format(name, len(data), hashlib.sha256(data).hexdigest()))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

"""Check that the backend loads only the standard library and netifaces.

Usage:
    python check_imports.py

Imports start_backend, daemon and every streaprous_socket module, then
classifies each loaded module by where its file lives.
"""

import importlib
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
PACKAGE_DIR = os.path.dirname(HERE)


def _norm(path):
    """Return a comparable form of a path."""
    return os.path.normcase(os.path.realpath(path))


def _inside(path, folder):
    """Tell whether path is folder or lies below it."""
    folder = _norm(folder)
    return path == folder or path.startswith(folder + os.sep)


def classify(name, filename):
    """Classify one loaded module.

    Args:
        name: module name.
        filename: the module's __file__.

    Returns:
        'local', 'stdlib', 'allowed' or 'violation'.
    """
    path = _norm(filename)
    if _inside(path, PACKAGE_DIR):
        return 'local'
    if name.split('.')[0] == 'netifaces':
        return 'allowed'
    parts = path.split(os.sep)
    if 'site-packages' in parts or 'dist-packages' in parts:
        return 'violation'
    if _inside(path, sys.base_prefix):
        return 'stdlib'
    return 'violation'


def load_backend():
    """Import the backend modules, with a netifaces stand-in when needed."""
    if PACKAGE_DIR not in sys.path:
        sys.path.insert(0, PACKAGE_DIR)
    try:
        import netifaces  # noqa: F401
    except ImportError:
        sys.modules['netifaces'] = types.ModuleType('netifaces')
    importlib.import_module('start_backend')
    importlib.import_module('daemon')
    folder = os.path.join(PACKAGE_DIR, 'streaprous_socket')
    for entry in sorted(os.listdir(folder)):
        if entry.endswith('.py') and entry != '__init__.py':
            importlib.import_module('streaprous_socket.' + entry[:-3])


def find_violations():
    """Load the backend and list modules that are not local, stdlib or netifaces.

    Returns:
        sorted list of 'name (file)' strings.
    """
    # modules loaded before the backend (site .pth hooks, for example) are not its imports
    before = set(sys.modules)
    load_backend()
    bad = []
    for name, module in list(sys.modules.items()):
        if name in before:
            continue
        filename = getattr(module, '__file__', None)
        if not filename or name == '__main__':
            continue
        if classify(name, filename) == 'violation':
            bad.append('%s (%s)' % (name, filename))
    return sorted(bad)


def main():
    """Command line entry point.

    Returns:
        0 when every module is allowed, 1 otherwise.
    """
    bad = find_violations()
    if bad:
        print('FAIL: modules outside the standard library and netifaces:')
        for line in bad:
            print('  ' + line)
        return 1
    print('OK: backend loads only the standard library and netifaces')
    return 0


if __name__ == '__main__':
    sys.exit(main())

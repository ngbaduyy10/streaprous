"""Unit tests for the TCP modules.

Puts the streaprous folder first on sys.path and installs a stand-in
netifaces module when the real one is missing.
"""

import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    import netifaces  # noqa: F401
except ImportError:
    sys.modules['netifaces'] = types.ModuleType('netifaces')

"""Expose checkout fixtures to explicitly selected test subprocess modules.

Installed lanes import only declared helpers from an extracted sdist; this
checkout package is deliberately absent from that distribution contract.
"""

import sys
from pathlib import Path

_FIXTURES = str(Path(__file__).resolve().parent)
if _FIXTURES not in sys.path:
    sys.path.insert(0, _FIXTURES)

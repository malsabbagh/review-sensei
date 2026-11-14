"""One synthetic execution sequence across unittest import aliases.

Tests can import a case as ``test_quarantine_journey`` or
``tests.test_quarantine_journey``. Both must allocate distinct synthetic runs
from the same sequence; the production single-use registry remains untouched.
"""

import itertools

JOURNEY_RUNS = itertools.count(2900)

PUBLISHED_TAIL_RUNS = itertools.count(3900)

BODY_LIMIT_RUNS = itertools.count(7900)

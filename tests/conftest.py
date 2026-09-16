"""Keep the packaged lanes out of the developer checkout run.

``unittest discover -s tests`` already skips ``dist_safe`` and ``downstream``
because they are not packages, which is what makes the two lanes separate.
pytest collects directories regardless, so without this it would import those
modules against an editable install and trip the packaging guard, which is armed
by default. Run them the documented way instead, from an unpacked sdist against
an installed wheel.
"""

from __future__ import annotations

collect_ignore = ["dist_safe", "downstream"]

"""Keep session fixture creation and implicit ledger reads on the same clock."""

from datetime import datetime, timezone
from unittest.mock import patch

from review_sensei import session

SESSION_FIXTURE_NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


class SessionFixtureClockMixin:
    """Freeze only implicit session time; explicit expiry checks keep their time."""

    def setUp(self):
        super().setUp()
        original = session._aware_now
        fixture_now = SESSION_FIXTURE_NOW
        self.enterContext(
            patch.object(
                session,
                "_aware_now",
                side_effect=lambda now=None: original(
                    fixture_now if now is None else now
                ),
            )
        )

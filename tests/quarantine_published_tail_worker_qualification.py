"""Explicit production Worker composition with fourteen published-tail holds."""

import unittest
from unittest.mock import patch

import test_quarantine_journey as journey_contract
from review_sensei_github.quarantine_published_tail import BROKER_QUALIFICATIONS
from test_quarantine_published_tail_journey import PublishedTailJourneyFixture

from tests.quarantine_worker_qualification import ProductionQuarantineJourneyFixture


class PublishedTailProductionFixture(
    PublishedTailJourneyFixture, ProductionQuarantineJourneyFixture
):
    pass


class PublishedTailWorkerQualification(unittest.TestCase):
    exercise_counts = journey_contract.QuarantineJourneyTests.exercise_counts

    def test_production_worker_signs_the_complete_published_tail_journey(self):
        with patch.dict(BROKER_QUALIFICATIONS, {}, clear=True):
            self.exercise_counts(
                9, 5, delayed_old=True, fixture_type=PublishedTailProductionFixture
            )


if __name__ == "__main__":
    unittest.main()

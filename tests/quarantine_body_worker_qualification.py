"""Explicit c7 body-limit journey with the production Worker IPC composition."""

import unittest

import test_quarantine_body_limit as body_contract

from tests.quarantine_worker_qualification import ProductionQuarantineJourneyFixture


class ProductionBodyLimitFixture(
    body_contract.BodyLimitJourneyFixture, ProductionQuarantineJourneyFixture
):
    pass


class BodyLimitWorkerQualification(unittest.TestCase):
    fixture_type = ProductionBodyLimitFixture
    test_production_worker_preserves_partial_origin_and_exact_decisions = body_contract.BodyLimitQuarantineTests.test_partial_eighty_candidate_origin_survives_empty_full_successor_and_decisions


if __name__ == "__main__":
    unittest.main()

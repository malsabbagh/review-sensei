"""Opt-in V3 journey through the production Worker; external services synthetic."""

import test_quarantine_v3_journey as contract
from quarantine_worker_qualification import ProductionQuarantineJourneyFixture


class ProductionV3JourneyFixture(
    contract.V3JourneyFixture, ProductionQuarantineJourneyFixture
):
    pass


class SameHeadProductionV3JourneyFixture(ProductionV3JourneyFixture):
    same_head = True


class QuarantineV3WorkerQualificationTests(contract.QuarantineV3JourneyTests):
    fixture_types = (ProductionV3JourneyFixture, SameHeadProductionV3JourneyFixture)

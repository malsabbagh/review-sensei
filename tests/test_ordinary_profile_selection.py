"""Initial unsupported profiles retain conservative publication, never cheap proofs."""

import unittest


class OrdinaryProfileCompatibilityTests(unittest.TestCase):
    def test_real_late_draft_and_closed_gates_survive_profile_exclusion(self):
        from tests.test_g04_merge_characterization import G04HostedMergeCharacterization

        G04HostedMergeCharacterization().test_late_draft_or_closed_state_is_not_overridden_by_clean_mergeability()

    def test_real_explicit_rescan_retains_original_conservative_route(self):
        from tests.test_explicit_rescan import CurrentDispatchRescanEntryTests

        CurrentDispatchRescanEntryTests().test_actual_dispatch_full_rescan_writes_through_completion_on_same_allowance()

    def test_real_remaining_rescan_preserves_extended_reader_route(self):
        from tests.test_explicit_rescan import CurrentDispatchRescanEntryTests

        CurrentDispatchRescanEntryTests().test_actual_dispatch_remaining_reviews_only_unfinished_file_and_completes()


class OrdinaryProfileSelectionTests(unittest.TestCase):
    def fixture(self):
        from types import SimpleNamespace

        from tests.test_ordinary_publication_reads import (
            OrdinaryPublicationReadCustodyTests,
        )

        publisher, journal, http = OrdinaryPublicationReadCustodyTests().fixture()
        return publisher, journal, http, SimpleNamespace(provider="fixture")

    def test_initial_custom_source_and_extended_readers_get_no_cheap_witness(self):
        from unittest.mock import patch

        from review_sensei_github import ordinary_publication_reads as reads
        from review_sensei_github.publication import ReviewPublisher

        for kind in (
            "class",
            "instance",
            "historical",
            "prior",
            "union",
            "human",
            "callback",
        ):
            with self.subTest(kind=kind):
                publisher, journal, http, result = self.fixture()
                calls = []

                def custom(*args, **kwargs):
                    calls.append(True)

                owner, name = {
                    "class": (ReviewPublisher, "_preflight_pr"),
                    "instance": (publisher, "_preflight_pr"),
                    "historical": (
                        publisher.finalizer,
                        "historical_assessment_reader_factory",
                    ),
                    "prior": (publisher.finalizer, "prior_publication_reader_factory"),
                    "union": (publisher.finalizer, "native_union_reader_factory"),
                    "human": (publisher.finalizer, "human_inventory_reader_factory"),
                    "callback": (publisher.finalizer, "_operation_observer"),
                }[kind]
                with patch.object(owner, name, custom):
                    with reads.select_publication_observation(
                        publisher, journal, result=result
                    ) as observer:
                        self.assertIsNone(observer)
                        self.assertIsNone(reads.issued_witness(journal))
                        self.assertIs(publisher.http, http)
                self.assertEqual(calls, [])

    def test_initial_guarded_transport_keeps_its_original_guard(self):
        from review_sensei_github import ordinary_publication_reads as reads

        for name in ("_controlled_restart_guard", "_quarantine_capacity_guard"):
            with self.subTest(guard=name):
                publisher, journal, http, result = self.fixture()
                guard = object()
                setattr(http, name, guard)
                with reads.select_publication_observation(
                    publisher, journal, result=result
                ) as observer:
                    self.assertIsNone(observer)
                    self.assertIs(getattr(http, name), guard)
                    self.assertIs(publisher.http, http)

    def test_late_source_reader_and_transport_change_remain_hard_failures(self):
        from review_sensei_github import ordinary_publication_reads as reads

        from review_sensei.errors import ReviewInputError

        for kind in ("source", "reader", "guard"):
            with self.subTest(kind=kind):
                publisher, journal, http, result = self.fixture()
                with reads.select_publication_observation(
                    publisher, journal, result=result
                ) as observer:
                    self.assertIsNotNone(observer)
                    if kind == "source":
                        publisher._preflight_pr = lambda **kwargs: None
                    elif kind == "reader":
                        publisher.finalizer.prior_publication_reader_factory = (
                            lambda **kwargs: None
                        )
                    else:
                        http._controlled_restart_guard = object()
                    with self.assertRaises(ReviewInputError):
                        observer._require(journal, publisher)
                self.assertIs(publisher.http, http)

    def test_active_funding_failure_is_not_profile_fallback(self):
        from review_sensei_github import ordinary_publication_reads as reads

        from review_sensei.errors import EvidenceBudgetExhausted

        publisher, journal, http, result = self.fixture()
        with self.assertRaisesRegex(EvidenceBudgetExhausted, "original tail"):
            with reads.select_publication_observation(
                publisher, journal, result=result
            ) as observer:
                self.assertIsNotNone(observer)
                raise EvidenceBudgetExhausted("original tail is unfunded")
        self.assertIs(publisher.http, http)

    def test_active_observer_cannot_be_reselected_as_conservative_fallback(self):
        from review_sensei_github import ordinary_publication_reads as reads

        from review_sensei.errors import ReviewInputError

        publisher, journal, http, result = self.fixture()
        with reads.select_publication_observation(
            publisher, journal, result=result
        ) as observer:
            self.assertIsNotNone(observer)
            publisher.finalizer.prior_publication_reader_factory = object()
            with self.assertRaisesRegex(ReviewInputError, "already active"):
                reads.select_publication_observation(publisher, journal, result=result)
        self.assertIs(publisher.http, http)

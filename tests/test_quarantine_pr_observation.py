"""Source closure and single-use negative controls for shared fresh PR data."""

import copy
import unittest

from review_sensei_github import publication
from review_sensei_github import quarantine_pr_observation as observation
from review_sensei_github.operation_diagnostics import OperationObserver


class QuarantinePrObservationTests(unittest.TestCase):
    def owner(self):
        return publication.ReviewApprovalFinalizer(http=None)

    def test_real_source_static_methods_do_not_disqualify_capture(self):
        owner = self.owner()
        with publication._capture_quarantine_pr(owner) as token:
            self.assertIs(observation._ACTIVE.get(), token)
        self.assertIsNone(observation._ACTIVE.get())

    def test_builtin_pure_stage_observer_is_allowed(self):
        owner = self.owner()
        owner._operation_observer = OperationObserver()
        with publication._capture_quarantine_pr(owner) as token:
            self.assertIs(observation._ACTIVE.get(), token)

    def test_custom_stage_callback_is_never_invoked(self):
        owner = self.owner()

        class Observer:
            def at(self, stage):
                raise AssertionError("custom observer invoked")

        owner._operation_observer = Observer()
        with publication._capture_quarantine_pr(owner):
            self.assertIsNone(observation._ACTIVE.get())

    def test_instance_replaced_validator_cannot_capture(self):
        owner = self.owner()
        owner._preflight_payload = lambda *args, **kwargs: self.fail("custom gate")
        with publication._capture_quarantine_pr(owner):
            self.assertIsNone(observation._ACTIVE.get())

    def test_forged_copied_and_reused_tokens_do_not_produce_data(self):
        owner = self.owner()
        with publication._capture_quarantine_pr(owner) as token:
            pass
        for candidate in (observation._Observation(), copy.copy(token), token, token):
            self.assertIsNone(
                publication._consume_quarantine_pr(
                    candidate,
                    owner,
                    token="read",
                    repository="o/r",
                    pull_request=1,
                    head_sha="a" * 40,
                    app_slug="app",
                )
            )

    def test_no_arbitrary_payload_issuer_is_exported(self):
        from review_sensei_github import quarantine_action, quarantine_evidence

        self.assertFalse(hasattr(quarantine_evidence, "_observe_current_pr"))
        self.assertFalse(hasattr(quarantine_action, "_issue_lifecycle_transfer"))
        with self.assertRaisesRegex(Exception, "capture is source-owned"):
            quarantine_action._current_publication_digest(
                None, None, _transfer_capture=lambda value: self.fail("callback")
            )

    def test_ledger_method_provenance_is_distinct_from_transport_factory(self):
        from unittest.mock import patch

        from review_sensei_github import review_run

        from review_sensei.errors import ReviewInputError
        from review_sensei.source_registry import require_instance_source

        ledger = object.__new__(review_run.ActionRunLedger)
        names = ("load", "read_evidence", "replace", "evidence_producer")
        with patch.object(review_run, "GitHubHttp", lambda **kwargs: None):
            require_instance_source(ledger, methods=names)
        ledger.load = lambda *args: self.fail("substituted source invoked")
        with self.assertRaises(ReviewInputError):
            require_instance_source(ledger, methods=names)

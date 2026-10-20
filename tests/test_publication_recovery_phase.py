"""One read-only metadata phase shares a complete lossless inventory."""

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from review_sensei_github.errors import GitHubPublicationError
from review_sensei_github.http import GitHubHttp
from review_sensei_github.publication_recovery import (
    publication_recovery_read_phase,
    recover_retained_review_result,
    retain_review_result,
)
from review_sensei_github.session_ledger import (
    GitHubIssueCommentSessionLedger,
    render_session_comment,
)

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.errors import ReviewInputError
from review_sensei.session import LocalSessionLedger, read_session_baseline
from tests.test_publication_recovery import (
    APP,
    BASE,
    CONFIGURATION,
    EVIDENCE,
    HEAD,
    IDENTITY,
    POLICY,
    RetentionState,
    checkpoint,
)


class PublicationRecoveryReadPhaseTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.local = LocalSessionLedger(Path(directory.name))
        self.result, _ = checkpoint(self.local, count=1)
        self.state = RetentionState()
        retain_review_result(
            http=self.state.http,
            token="metadata",
            result=self.result,
            identity=IDENTITY,
            base_sha=BASE,
            head_sha=HEAD,
            app_slug=APP,
            ledger=self.local,
            configuration_context=CONFIGURATION,
            evidence_context=EVIDENCE,
            policy=POLICY,
        )
        self.state.session_comment = {
            "id": 600,
            "body": render_session_comment(
                repository_id=1, pull_request=1, record=self.local.load(IDENTITY).record
            ),
            "user": self.state.app_user,
            "issue_url": "https://api.github.test/repos/owner/repo/issues/1",
        }
        self.budget = EvidenceReadBudget()
        self.remote = self.ledger()
        self.state.calls.clear()

    def ledger(self, **changes):
        arguments = dict(
            http=self.state.http,
            token="metadata",
            app_slug=APP,
            evidence_budget=self.budget,
        )
        arguments.update(changes)
        return GitHubIssueCommentSessionLedger(**arguments)

    def phase(self, **changes):
        arguments = dict(
            http=self.state.http,
            token="metadata",
            ledger=self.remote,
            identity=IDENTITY,
            base_sha=BASE,
            head_sha=HEAD,
            app_slug=APP,
            budget=self.budget,
        )
        arguments.update(changes)
        return publication_recovery_read_phase(**arguments)

    def recover(self, phase, **changes):
        arguments = dict(
            http=self.state.http,
            token="metadata",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            base_sha=BASE,
            head_sha=HEAD,
            app_slug=APP,
            ledger=phase.ledger,
            configuration_context=CONFIGURATION,
            evidence_context=EVIDENCE,
            convergence_policy=POLICY,
            budget=self.budget,
            recovery_phase=phase,
        )
        arguments.update(changes)
        return recover_retained_review_result(**arguments)

    def test_context_selection_and_raw_recovery_fit_original_remaining_five_reads(self):
        for _ in range(55):
            self.budget.consume()
        with self.phase() as phase:
            record = phase.ledger.load(IDENTITY).record
            self.assertIsNotNone(read_session_baseline(phase.ledger, record))
            restored = self.recover(phase)
            self.assertEqual(restored.content_digest(), self.result.content_digest())
        self.assertEqual(self.budget.calls, 60)
        self.assertEqual(len(self.state.calls), 5)
        self.assertTrue(all(method == "GET" for method, _, _ in self.state.calls))

    def test_closed_phase_discards_raw_inventory_and_normal_ledger_load_stays_fresh(
        self,
    ):
        with self.phase() as phase:
            phase.ledger.load(IDENTITY)
            self.recover(phase)
        self.assertFalse(phase.active)
        self.assertIsNone(phase._snapshot)
        before = len(self.state.calls)
        self.assertEqual(phase.ledger.load(IDENTITY).status, "ok")
        self.assertGreater(len(self.state.calls), before)
        with self.assertRaises(GitHubPublicationError):
            self.recover(phase)

    def test_phase_preserves_duplicate_and_malformed_raw_records(self):
        for malformed in (False, True):
            with self.subTest(malformed=malformed):
                added = (
                    "malformed record" if malformed else deepcopy(self.state.parts[0])
                )
                self.state.parts.append(added)
                try:
                    with self.phase(ledger=self.ledger()) as phase:
                        # The ledger's own projection may omit/deduplicate this;
                        # raw integrity validation must still see the original.
                        phase.ledger.load(IDENTITY)
                        with self.assertRaises(GitHubPublicationError):
                            self.recover(phase)
                finally:
                    self.state.parts.pop()
        self.assertTrue(all(method == "GET" for method, _, _ in self.state.calls))

    def test_fresh_phase_rejects_used_primed_foreign_and_local_ledgers(self):
        used = self.ledger()
        used.load(IDENTITY)
        for changed in (
            {"ledger": used},
            {"ledger": self.local},
            {"ledger": self.ledger(evidence_budget=EvidenceReadBudget())},
            {"token": "different"},
            {"head_sha": "bad"},
            {"http": GitHubHttp(opener=self.state.http.opener)},
        ):
            with self.subTest(changed=tuple(changed)):
                before = len(self.state.calls)
                with self.assertRaises(ReviewInputError), self.phase(**changed):
                    self.fail("foreign phase admitted")
                self.assertEqual(len(self.state.calls), before)
        path = "/repos/owner/repo/issues/1/comments"
        self.state.http.primed_issue_comments[path] = []
        with self.assertRaises(ReviewInputError), self.phase():
            self.fail("primed phase admitted")
        self.assertIn(path, self.state.http.primed_issue_comments)

    def test_recovery_claim_rejects_foreign_budget_token_transport_scope_and_replay(
        self,
    ):
        with self.phase() as phase:
            phase.ledger.load(IDENTITY)
            for changed in (
                {"budget": EvidenceReadBudget()},
                {"token": "different"},
                {"http": GitHubHttp(opener=self.state.http.opener)},
                {"head_sha": "f" * 40},
                {"ledger": self.ledger()},
            ):
                with self.subTest(changed=tuple(changed)):
                    before = len(self.state.calls)
                    with self.assertRaises(GitHubPublicationError):
                        self.recover(phase, **changed)
                    self.assertEqual(len(self.state.calls), before)
            self.recover(phase)
            with self.assertRaises(GitHubPublicationError):
                self.recover(phase)

    def test_mutation_is_blocked_and_failure_clears_phase_without_touching_owner(self):
        original_http = self.remote.http
        with self.assertRaisesRegex(RuntimeError, "abort"):
            with self.phase() as phase:
                with self.assertRaises(ReviewInputError):
                    phase.ledger.http.request(
                        "POST", "/graphql", token="metadata", body={}
                    )
                raise RuntimeError("abort")
        self.assertIs(self.remote.http, original_http)
        self.assertFalse(phase.active)
        self.assertIsNone(phase._snapshot)
        self.assertEqual(phase._read_http.primed_issue_comments, {})
        self.assertTrue(all(method == "GET" for method, _, _ in self.state.calls))

    def test_later_retention_still_refuses_mutated_parts_after_phase(self):
        with self.phase() as phase:
            phase.ledger.load(IDENTITY)
            restored = self.recover(phase)
        self.state.parts.append(deepcopy(self.state.parts[0]))
        before = len(self.state.calls)
        with self.assertRaises(GitHubPublicationError):
            retain_review_result(
                http=self.state.http,
                token="publication-token",
                result=restored,
                identity=IDENTITY,
                base_sha=BASE,
                head_sha=HEAD,
                app_slug=APP,
                ledger=self.local,
                configuration_context=CONFIGURATION,
                evidence_context=EVIDENCE,
                policy=POLICY,
            )
        self.assertTrue(
            all(method == "GET" for method, _, _ in self.state.calls[before:])
        )

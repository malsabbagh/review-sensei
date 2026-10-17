"""A same-head explicit rescan is new bounded work, never an approval grant."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from review_sensei_github.approval import approval_eligibility_from_result
from review_sensei_github.publication import approval_eligibility_marker, review_marker
from review_sensei_github.rescan import resolve_rescan
from review_sensei_github.review_hooks import GitHubReviewHooks

from review_sensei.cli import main
from review_sensei.errors import ReviewInputError
from review_sensei.outcomes import RunOutcome
from review_sensei.rescan import AuthorizedRescan, rescan_source
from review_sensei.service import ReviewRun
from review_sensei.session import (
    InMemorySessionLedger,
    SessionRecord,
    checkpoint_review_analysis,
    complete_review_publication,
    prepare_review_transaction,
    read_session_baseline,
    record_session_failed_attempt,
)

try:
    import test_partial_review_transaction as partial
    import test_review_transaction as tx
except ModuleNotFoundError:
    from tests import test_partial_review_transaction as partial
    from tests import test_review_transaction as tx


def seed(ledger=None, *, complete=False, result=None):
    ledger = ledger or InMemorySessionLedger()
    prepared = partial.prepare(ledger)
    bound = checkpoint_review_analysis(
        ledger,
        tx.IDENTITY,
        prepared,
        result or (tx._result() if complete else partial.partial_result()),
    )
    complete_review_publication(ledger, tx.IDENTITY, bound.transaction, published=True)
    return ledger, bound


def request(ledger, request_id=10, kind="workflow_dispatch"):
    return AuthorizedRescan(
        kind, request_id, rescan_source(ledger.load(tx.IDENTITY).record), 5
    )


def prepare(ledger, rescan=None, *, head=tx.HEAD_SHA):
    return prepare_review_transaction(
        ledger,
        tx.IDENTITY,
        tx.POLICY,
        reservation_id=partial.session_reservation_id(
            repository=tx.IDENTITY.repository,
            pull_request=tx.IDENTITY.pull_request,
            head_sha=head,
            kind="publish",
        ),
        head_sha=head,
        base_sha=tx.BASE_SHA,
        configuration_digest=tx.CONFIGURATION_DIGEST,
        evidence_digest=tx.EVIDENCE_DIGEST,
        rescan_request=rescan,
    )


def cli_arguments(root, *, base_sha=tx.BASE_SHA, diff=partial.gates.DIFF):
    (root / "diff.patch").write_bytes(diff.encode("utf-8"))
    (root / "response.json").write_text('{"summary":"ok","comments":[]}')
    return [
        "--diff",
        str(root / "diff.patch"),
        "--provider",
        "fixture",
        "--fixture-response",
        str(root / "response.json"),
        "--model",
        "fixture-model",
        "--repository",
        "owner/repo",
        "--repository-id",
        "99",
        "--pull-request",
        "146",
        "--head-sha",
        tx.HEAD_SHA,
        "--base-sha",
        base_sha,
        "--format",
        "json",
        "--transaction",
        "--github-session-ledger",
        "--rescan",
        "--no-learning-proposals",
        "--output",
        str(root / "review.json"),
        "--outcome",
        str(root / "outcome.json"),
        "--configuration-context-output",
        str(root / "configuration.json"),
        "--admission-context-output",
        str(root / "admission.json"),
        "--exit-semantics",
        "operational",
    ]


def ledger_hooks(ledger):
    class Hooks(GitHubReviewHooks):
        def resolve_ledger(self, args, **kwargs):
            return ledger

        def resolve_rescan(self, args, resolved_ledger):
            return resolve_rescan(
                args, resolved_ledger, actions_token="synthetic-actions-token"
            )

    return Hooks()


class RescanAdmissionTests(unittest.TestCase):
    def test_published_partial_rescans_once_and_retains_truth_and_history(self):
        ledger, old = seed()
        before = ledger.load(tx.IDENTITY).record
        self.assertFalse(prepare(ledger).decision.admit)
        explicit = request(ledger)
        admitted = prepare(ledger, explicit)
        self.assertTrue(admitted.decision.admit)
        self.assertNotEqual(admitted.reservation_id, old.transaction.reservation_id)
        self.assertFalse(admitted.decision.may_emit_approve)
        self.assertEqual(admitted.record.failed_attempts, before.failed_attempts)
        new = checkpoint_review_analysis(
            ledger, tx.IDENTITY, admitted, partial.partial_result()
        )
        record = complete_review_publication(
            ledger, tx.IDENTITY, new.transaction, published=True
        )
        self.assertEqual(record.failed_attempts, 2)
        self.assertEqual(record.completed_initial_reviews, 0)
        self.assertIsNone(record.convergence_history)
        self.assertEqual(
            record.rescan_state["receipts"][0]["source"]["transaction_id"],
            old.transaction.transaction_id,
        )
        # A fresh process sees the same authenticated receipt and watermarks.
        ledger._records[(tx.IDENTITY.repository, tx.IDENTITY.pull_request)] = (
            SessionRecord.from_dict(record.to_dict())
        )
        self.assertFalse(prepare(ledger, explicit).decision.admit)
        self.assertFalse(prepare(ledger).decision.admit)
        self.assertEqual(ledger.load(tx.IDENTITY).record.generation, record.generation)

    def test_seven_reviewed_files_and_excluded_manifest_remain_partial(self):
        from review_sensei.coverage import CoverageManifest, FileCoverage

        paths = tuple(f"src/module{index}.py" for index in range(7))
        excluded = "vendor/brace-expansion-compat/package.json"
        result = replace(
            partial.partial_result(),
            comments=(),
            coverage=CoverageManifest(
                files=tuple(FileCoverage(path, "reviewed") for path in paths)
                + (FileCoverage(excluded, "excluded-by-policy", "generated"),),
                enumerated_paths=(*paths, excluded),
            ),
        )
        ledger, _ = seed(result=result)
        admitted = prepare(ledger, request(ledger))
        bound = checkpoint_review_analysis(ledger, tx.IDENTITY, admitted, result)
        self.assertEqual(
            sum(item.outcome == "reviewed" for item in bound.coverage.files), 7
        )
        self.assertFalse(bound.coverage.fully_reviewed)
        facts = approval_eligibility_from_result(
            bound, head_sha=tx.HEAD_SHA, enabled=True, app_authored=False
        ).facts
        self.assertEqual(facts.review_status, "partial")
        self.assertEqual(facts.coverage_blocker, "partial")
        self.assertEqual(ledger.load(tx.IDENTITY).record.failed_attempts, 2)

    def test_concurrent_and_stale_deliveries_cannot_replace_active_work(self):
        ledger, _ = seed()
        authority = request(ledger)
        first = prepare(ledger, authority)
        active = ledger.load(tx.IDENTITY).record
        for event in (replace(authority, request_id=11),):
            # The host does not grant a rescan of an analysis-phase source.
            with self.assertRaises(ReviewInputError):
                prepare(ledger, event)
        self.assertFalse(prepare(ledger).decision.admit)
        self.assertEqual(ledger.load(tx.IDENTITY).record, active)
        with self.assertRaisesRegex(ReviewInputError, "generation conflict"):
            ledger.reserve(
                tx.IDENTITY,
                slot="initial",
                reservation_id="e" * 64,
                expected_generation=first.record.generation - 1,
            )

    def test_failed_retry_consumes_event_and_keeps_budget_and_original_source(self):
        ledger, _ = seed()
        explicit = request(ledger)
        admitted = prepare(ledger, explicit)
        record_session_failed_attempt(
            ledger,
            tx.IDENTITY,
            reservation_id=admitted.reservation_id,
            head_sha=tx.HEAD_SHA,
            expected_generation=admitted.record.generation,
        )
        record = ledger.load(tx.IDENTITY).record
        self.assertEqual(record.failed_attempts, 2)
        self.assertIsNone(record.transaction)
        self.assertFalse(prepare(ledger, explicit).decision.admit)
        self.assertFalse(prepare(ledger).decision.admit)
        retry = prepare(ledger, request(ledger, 11))
        self.assertTrue(retry.decision.admit)
        self.assertEqual(retry.record.failed_attempts, 2)

    def test_fresh_local_adapters_preserve_active_and_stale_generation_fences(self):
        from review_sensei.session import LocalSessionLedger

        # Local storage's documented contract is one writer; hosted provider
        # jobs serialize by authoritative PR before using the hosted ledger.
        with tempfile.TemporaryDirectory() as root:
            ledger, _ = seed(LocalSessionLedger(Path(root)))
            explicit = request(ledger)
            before = ledger.load(tx.IDENTITY).record
            admitted = prepare(LocalSessionLedger(Path(root)), explicit)
            self.assertTrue(admitted.decision.admit)
            self.assertFalse(
                prepare(LocalSessionLedger(Path(root)), explicit).decision.admit
            )
            with self.assertRaisesRegex(ReviewInputError, "generation conflict"):
                LocalSessionLedger(Path(root)).reserve(
                    tx.IDENTITY,
                    slot="initial",
                    reservation_id="e" * 64,
                    expected_generation=before.generation,
                    rescan_request=replace(explicit, request_id=11),
                )
            record = LocalSessionLedger(Path(root)).load(tx.IDENTITY).record
            self.assertEqual(record.failed_attempts, 1)
            self.assertEqual(len(record.rescan_state["receipts"]), 1)

    def test_bounded_receipts_keep_watermarks_after_eviction(self):
        ledger, _ = seed()
        oldest = request(ledger, 10)
        for index in range(8):
            if index:
                head = f"{index:040x}"
                ordinary = prepare(ledger, head=head)
                bound = checkpoint_review_analysis(
                    ledger, tx.IDENTITY, ordinary, partial.partial_result()
                )
                complete_review_publication(
                    ledger, tx.IDENTITY, bound.transaction, published=True
                )
            else:
                head = tx.HEAD_SHA
            admitted = prepare(ledger, request(ledger, 10 + index), head=head)
            bound = checkpoint_review_analysis(
                ledger, tx.IDENTITY, admitted, partial.partial_result()
            )
            complete_review_publication(
                ledger, tx.IDENTITY, bound.transaction, published=True
            )
        record = SessionRecord.from_dict(ledger.load(tx.IDENTITY).record.to_dict())
        self.assertEqual(len(record.rescan_state["receipts"]), 6)
        self.assertEqual(record.rescan_state["workflow_dispatch"], 17)
        self.assertFalse(prepare(ledger, oldest, head=head).decision.admit)

    def test_malformed_receipts_are_rejected_before_admission_or_persistence(self):
        ledger, _ = seed()
        admitted = prepare(ledger, request(ledger))
        original = ledger.load(tx.IDENTITY).record
        state = json.loads(json.dumps(original.rescan_state))
        receipt = state["receipts"][0]
        malformed = (
            {**state, "receipts": []},
            {**state, "workflow_dispatch": "10"},
            {
                **state,
                "receipts": [
                    {
                        key: value
                        for key, value in receipt.items()
                        if key != "reservation"
                    }
                ],
            },
            {**state, "receipts": [{**receipt, "source": None}]},
            {
                **state,
                "receipts": [
                    {**receipt, "source": {**receipt["source"], "phase": "unknown"}}
                ],
            },
        )
        for invalid in malformed:
            with self.subTest(state=invalid):
                document = {**original.to_dict(), "rescan_state": invalid}
                with self.assertRaises(ReviewInputError):
                    SessionRecord.from_dict(document)
                with self.assertRaises(ReviewInputError):
                    ledger.replace(
                        tx.IDENTITY, lambda record: record.evolve(rescan_state=invalid)
                    )
                self.assertEqual(ledger.load(tx.IDENTITY).record, original)
        self.assertEqual(original.reservation_id, admitted.reservation_id)

    def test_receipt_for_a_different_head_is_not_this_head_source(self):
        from review_sensei.models import ReviewTransaction
        from review_sensei.rescan import (
            ordinary_rescan_duplicate,
            rescan_publication_recovery,
            rescan_reservation,
            rescan_source,
        )

        ledger, _bound = seed()
        explicit = request(ledger)
        admitted = prepare(ledger, explicit)
        record_session_failed_attempt(
            ledger,
            tx.IDENTITY,
            reservation_id=admitted.reservation_id,
            head_sha=tx.HEAD_SHA,
        )
        released = ledger.load(tx.IDENTITY).record
        self.assertIsNone(released.transaction)
        published = explicit.source
        other = (
            ReviewTransaction.create(
                repository=published.repository,
                pull_request=published.pull_request,
                base_sha=published.base_sha,
                head_sha="c" * 40,
                policy_digest=published.policy_digest,
                configuration_digest=published.configuration_digest,
                evidence_digest=published.evidence_digest,
                reservation_id=published.reservation_id,
                generation=published.generation,
            )
            .with_result(published.result_sha256)
            .with_phase("publication_succeeded")
        )
        receipt = released.rescan_state["receipts"][0]
        other_reservation = "cd" * 32
        mismatched = released.evolve(
            rescan_state={
                **released.rescan_state,
                "receipts": [
                    receipt,
                    {
                        **receipt,
                        "source": other.to_dict(),
                        "reservation": other_reservation,
                    },
                ],
            }
        )
        # receipts[-1] is a different head. Selection must keep this head's source.
        self.assertEqual(
            rescan_source(mismatched, tx.HEAD_SHA).transaction_id,
            explicit.source.transaction_id,
        )
        self.assertEqual(rescan_source(mismatched, "c" * 40).head_sha, "c" * 40)
        fresh = AuthorizedRescan(
            explicit.kind,
            int(mismatched.rescan_state[explicit.kind]) + 1,
            explicit.source,
            6,
        )
        self.assertEqual(len(rescan_reservation(mismatched, fresh, tx.HEAD_SHA)), 64)
        self.assertFalse(rescan_publication_recovery(mismatched, tx.HEAD_SHA))
        self.assertFalse(
            ordinary_rescan_duplicate(mismatched, other_reservation, tx.HEAD_SHA)
        )

    def test_substantive_terminal_phases_are_not_publication_recovery(self):
        from review_sensei.rescan import (
            SUBSTANTIVE_TERMINAL_PHASES,
            rescan_publication_recovery,
        )

        ledger, _bound = seed()
        admitted = prepare(ledger, request(ledger))
        record = admitted.record
        self.assertFalse(rescan_publication_recovery(record, tx.HEAD_SHA))
        pending = record.evolve(
            transaction=record.transaction.with_result("ab" * 32).with_phase(
                "publication_failed"
            ),
            reservation_id=None,
            reserved_slot=None,
            last_committed_reservation_id=record.transaction.reservation_id,
        )
        self.assertTrue(rescan_publication_recovery(pending, tx.HEAD_SHA))
        for phase in sorted(SUBSTANTIVE_TERMINAL_PHASES):
            if phase == "analysis_failed":
                terminal = record.evolve(
                    transaction=record.transaction.with_phase("analysis_failed"),
                    reservation_id=None,
                    reserved_slot=None,
                    last_committed_reservation_id=record.transaction.reservation_id,
                )
            else:
                terminal = pending.evolve(
                    transaction=pending.transaction.with_phase(phase)
                )
            self.assertFalse(rescan_publication_recovery(terminal, tx.HEAD_SHA), phase)

    def test_consumed_receipts_do_not_alias_the_source_record(self):
        ledger, _bound = seed()
        explicit = request(ledger)
        first = prepare(ledger, explicit)
        bound = checkpoint_review_analysis(
            ledger, tx.IDENTITY, first, partial.partial_result()
        )
        published = complete_review_publication(
            ledger, tx.IDENTITY, bound.transaction, published=True
        )
        first_receipts = published.rescan_state["receipts"]
        original_reservation = first_receipts[0]["reservation"]
        second = prepare(
            ledger,
            AuthorizedRescan(
                explicit.kind, explicit.request_id + 1, published.transaction, 6
            ),
        )
        first_receipts[0]["reservation"] = "f" * 64
        stored = ledger.load(tx.IDENTITY).record.rescan_state["receipts"]
        self.assertEqual(stored[0]["reservation"], original_reservation)
        self.assertEqual(len(second.record.rescan_state["receipts"]), 2)

    def test_negative_rescan_fixture_fails_for_the_watermark_not_an_empty_list(self):
        from review_sensei.schemas import validate_public_document

        document = json.loads(
            (
                Path(__file__).parents[1]
                / "tests/fixtures/schemas/negative/rescan-state.json"
            ).read_text(encoding="utf-8")
        )
        self.assertGreaterEqual(len(document["receipts"]), 1)
        with self.assertRaises(ReviewInputError):
            validate_public_document(document, "rescan-state")
        repaired = {**document, "issue_comment": 0}
        validate_public_document(repaired, "rescan-state")

    def test_empty_analysis_reservation_id_is_rejected(self):
        from review_sensei_github.commands import run_hosted_provider

        for value in ("", "   ", 12):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ReviewInputError, "non-empty"):
                    run_hosted_provider(
                        object(), object(), analysis_reservation_id=value
                    )

    def test_noncurrent_source_serialization_cannot_be_persisted(self):
        from dataclasses import asdict

        ledger, _ = seed()
        original = ledger.load(tx.IDENTITY).record
        source = original.transaction

        class LegacySerializedTransaction(type(source)):
            def to_dict(self):
                document = super().to_dict()
                document.pop("transaction_id")
                return document

        authority = replace(
            request(ledger), source=LegacySerializedTransaction(**asdict(source))
        )
        with self.assertRaises(ReviewInputError):
            prepare(ledger, authority)
        self.assertEqual(ledger.load(tx.IDENTITY).record, original)
        self.assertIsNone(original.reservation_id)
        self.assertIsNone(original.rescan_state)

    def test_budget_pause_and_changed_head_fences(self):
        ledger, _ = seed()
        record = ledger.load(tx.IDENTITY).record
        explicit = request(ledger)
        ledger._records[(tx.IDENTITY.repository, tx.IDENTITY.pull_request)] = (
            record.evolve(failed_attempts=6)
        )
        refused = prepare(ledger, explicit)
        self.assertFalse(refused.decision.admit)
        self.assertEqual(
            refused.decision.handoff_reason, "failed-attempt-budget-exhausted"
        )
        ledger._records[(tx.IDENTITY.repository, tx.IDENTITY.pull_request)] = (
            record.evolve(operator_paused=True)
        )
        self.assertFalse(prepare(ledger, explicit).decision.admit)
        ledger._records[(tx.IDENTITY.repository, tx.IDENTITY.pull_request)] = record
        with self.assertRaisesRegex(ReviewInputError, "source changed"):
            prepare(ledger, explicit, head="c" * 40)
        changed = prepare(ledger, head="c" * 40)
        self.assertTrue(changed.decision.admit)
        self.assertEqual(changed.record.failed_attempts, 0)


class AuthorityHTTP:
    def __init__(self, bound, *, complete=False):
        result = tx._result() if complete else partial.partial_result()
        eligibility = approval_eligibility_from_result(
            result, head_sha=tx.HEAD_SHA, enabled=True, app_authored=False
        )
        self.review = {
            "id": 5,
            "submitted_at": "2026-10-16T20:00:00Z",
            "state": "COMMENTED",
            "commit_id": tx.HEAD_SHA,
            "user": {"login": "reviewsensei[bot]", "type": "Bot"},
            "body": review_marker(
                repository_id=99, pull_request=146, head_sha=tx.HEAD_SHA, result=bound
            )
            + "\n"
            + approval_eligibility_marker(eligibility),
        }
        self.permission = "write"
        self.head = tx.HEAD_SHA
        self.comment = {
            "id": 20,
            "body": "@reviewsensei rescan",
            "updated_at": "2026-10-16T22:04:00Z",
            "created_at": "2026-10-16T22:04:00Z",
            "user": {"login": "alice", "type": "User"},
            "author_association": "COLLABORATOR",
            "issue_url": "https://api.github.com/repos/owner/repo/issues/146",
        }

    def repository_path(self, repository, suffix):
        return "/repos/" + repository + suffix

    def request(self, method, path, **kwargs):
        if "/actions/runs/" in path:
            return 200, {
                "id": 10,
                "event": "workflow_dispatch",
                "repository": {"id": 99},
                "actor": {"login": "alice"},
                "created_at": "2026-10-16T22:04:00Z",
            }
        if path.endswith("/permission"):
            return 200, {"permission": self.permission}
        if "/issues/comments/" in path:
            return 200, self.comment
        return 200, {
            "number": 146,
            "state": "open",
            "head": {"sha": self.head},
            "base": {"repo": {"id": 99}},
        }

    def paginate(self, **kwargs):
        return [self.review]


class RescanHostTests(unittest.TestCase):
    def setUp(self):
        self.ledger, bound = seed()
        self.http = AuthorityHTTP(bound)
        self.ledger.http = self.http
        self.ledger.token = "synthetic-token"
        self.args = SimpleNamespace(
            repository="owner/repo",
            repository_id=99,
            pull_request=146,
            head_sha=tx.HEAD_SHA,
            app_slug="reviewsensei[bot]",
        )
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.event_path = Path(self.temp.name) / "event.json"
        self.event = {
            "repository": {"full_name": "owner/repo", "id": 99},
            "inputs": {"pull_request_number": "146"},
            "sender": {"login": "alice", "type": "User"},
        }
        self.env = patch.dict(
            os.environ,
            {
                "GITHUB_EVENT_NAME": "workflow_dispatch",
                "GITHUB_EVENT_PATH": str(self.event_path),
                "GITHUB_ACTOR": "alice",
                "GITHUB_TRIGGERING_ACTOR": "alice",
                "GITHUB_RUN_ID": "10",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.write_event()

    def write_event(self):
        self.event_path.write_text(json.dumps(self.event))

    def test_dispatch_and_comment_bind_distinct_stable_request_identities(self):
        self.assertEqual(
            resolve_rescan(
                self.args, self.ledger, actions_token="synthetic-actions-token"
            ).request_id,
            10,
        )
        os.environ["GITHUB_RUN_ATTEMPT"] = "2"
        self.addCleanup(os.environ.pop, "GITHUB_RUN_ATTEMPT", None)
        self.assertEqual(
            resolve_rescan(
                self.args, self.ledger, actions_token="synthetic-actions-token"
            ).request_id,
            10,
        )
        os.environ["GITHUB_EVENT_NAME"] = "issue_comment"
        self.event.update(
            action="created", issue={"number": 146}, comment=dict(self.http.comment)
        )
        self.write_event()
        self.assertEqual(
            resolve_rescan(
                self.args, self.ledger, actions_token="synthetic-actions-token"
            ).request_id,
            20,
        )
        self.http.comment["body"] = "@reviewsensei rescan commit " + "c" * 40
        with self.assertRaisesRegex(ReviewInputError, "edited"):
            resolve_rescan(
                self.args, self.ledger, actions_token="synthetic-actions-token"
            )

    def test_unauthorized_and_stale_events_make_no_mutation(self):
        before = self.ledger.load(tx.IDENTITY).record
        self.http.permission = "read"
        with self.assertRaisesRegex(ReviewInputError, "permission"):
            resolve_rescan(
                self.args, self.ledger, actions_token="synthetic-actions-token"
            )
        self.http.permission = "write"
        self.http.head = "c" * 40
        with self.assertRaisesRegex(ReviewInputError, "stale"):
            resolve_rescan(
                self.args, self.ledger, actions_token="synthetic-actions-token"
            )
        self.http.head = tx.HEAD_SHA
        self.event["sender"]["type"] = "Bot"
        self.write_event()
        with self.assertRaisesRegex(ReviewInputError, "unauthorized"):
            resolve_rescan(
                self.args, self.ledger, actions_token="synthetic-actions-token"
            )
        self.assertEqual(self.ledger.load(tx.IDENTITY).record, before)

    def test_dispatch_rejects_wrong_run_identity_but_does_not_confuse_workflow_and_pr_heads(
        self,
    ):
        original_request = self.http.request
        for change, rejected in (
            ({"id": 11}, True),
            ({"actor": {"login": "another-maintainer"}}, True),
            ({"head_sha": "f" * 40}, False),
        ):
            with self.subTest(change=change):

                def request_with_changed_run(method, path, **kwargs):
                    status, response = original_request(method, path, **kwargs)
                    if "/actions/runs/" in path:
                        response = {**response, **change}
                    return status, response

                with patch.object(
                    self.http, "request", side_effect=request_with_changed_run
                ):
                    if rejected:
                        with self.assertRaisesRegex(ReviewInputError, "run identity"):
                            resolve_rescan(
                                self.args,
                                self.ledger,
                                actions_token="synthetic-actions-token",
                            )
                    else:
                        authority = resolve_rescan(
                            self.args,
                            self.ledger,
                            actions_token="synthetic-actions-token",
                        )
                        self.assertEqual(authority.source.head_sha, tx.HEAD_SHA)
                        self.assertEqual(authority.request_id, 10)

    def test_missing_triggering_actor_fails_closed(self):
        before = self.ledger.load(tx.IDENTITY).record
        os.environ.pop("GITHUB_TRIGGERING_ACTOR")
        with self.assertRaisesRegex(ReviewInputError, "unauthorized"):
            resolve_rescan(
                self.args, self.ledger, actions_token="synthetic-actions-token"
            )
        os.environ["GITHUB_TRIGGERING_ACTOR"] = ""
        with self.assertRaisesRegex(ReviewInputError, "unauthorized"):
            resolve_rescan(
                self.args, self.ledger, actions_token="synthetic-actions-token"
            )
        self.assertEqual(self.ledger.load(tx.IDENTITY).record, before)

    def test_transient_authority_reads_do_not_consume_the_request(self):
        from review_sensei_github.errors import GitHubHTTPTransientError

        before = self.ledger.load(tx.IDENTITY).record
        original = self.http.request
        for status_code in (429, 503):

            def request_with_status(method, path, status_code=status_code, **kwargs):
                if path.endswith("/permission"):
                    return status_code, {"message": "unavailable"}
                return original(method, path, **kwargs)

            with patch.object(self.http, "request", side_effect=request_with_status):
                with self.assertRaises(GitHubHTTPTransientError):
                    resolve_rescan(
                        self.args,
                        self.ledger,
                        actions_token="synthetic-actions-token",
                    )
            self.assertEqual(self.ledger.load(tx.IDENTITY).record, before)
        with patch.object(
            self.http, "request", return_value=(404, {"message": "missing"})
        ):
            with self.assertRaisesRegex(ReviewInputError, "could not be verified"):
                resolve_rescan(
                    self.args, self.ledger, actions_token="synthetic-actions-token"
                )
        self.assertEqual(self.ledger.load(tx.IDENTITY).record, before)

    def test_dispatch_without_rescan_intent_is_refused(self):
        from review_sensei_github.rescan import cli_event_requests_rescan

        self.assertTrue(cli_event_requests_rescan())
        self.event["inputs"]["rescan"] = "false"
        self.write_event()
        self.assertFalse(cli_event_requests_rescan())
        with self.assertRaisesRegex(ReviewInputError, "did not request a rescan"):
            resolve_rescan(
                self.args, self.ledger, actions_token="synthetic-actions-token"
            )
        self.event["inputs"]["rescan"] = "true"
        self.event["inputs"]["operation"] = "reply"
        self.write_event()
        self.assertFalse(cli_event_requests_rescan())
        with self.assertRaisesRegex(ReviewInputError, "did not request a rescan"):
            resolve_rescan(
                self.args, self.ledger, actions_token="synthetic-actions-token"
            )

    def test_offset_timestamps_compare_as_utc_instants(self):
        original = self.http.request

        def request_at(created_at):
            def request_with_time(method, path, **kwargs):
                status, response = original(method, path, **kwargs)
                if "/actions/runs/" in path:
                    response = {**response, "created_at": created_at}
                return status, response

            return request_with_time

        with patch.object(
            self.http,
            "request",
            side_effect=request_at("2026-10-16T21:00:00+02:00"),
        ):
            self.assertIsNone(
                resolve_rescan(
                    self.args, self.ledger, actions_token="synthetic-actions-token"
                )
            )
        with patch.object(
            self.http,
            "request",
            side_effect=request_at("2026-10-16T22:30:00+01:00"),
        ):
            authority = resolve_rescan(
                self.args, self.ledger, actions_token="synthetic-actions-token"
            )
            self.assertEqual(authority.request_id, 10)

    def test_comment_gate_requires_the_rescan_grammar(self):
        from review_sensei_github.rescan import cli_event_requests_rescan

        os.environ["GITHUB_EVENT_NAME"] = "issue_comment"
        self.event.update(
            action="created",
            issue={"number": 146},
            comment={"body": "@reviewsensei review"},
        )
        self.write_event()
        self.assertFalse(cli_event_requests_rescan())
        self.event["comment"]["body"] = "@reviewsensei rescan"
        self.write_event()
        self.assertTrue(cli_event_requests_rescan())

    def test_dispatch_cannot_broaden_the_humans_selected_pull_request(self):
        self.event["inputs"]["pull_request_number"] = "147"
        self.write_event()
        with self.assertRaisesRegex(ReviewInputError, "another pull request"):
            resolve_rescan(
                self.args, self.ledger, actions_token="synthetic-actions-token"
            )

    def test_old_queued_request_cannot_reopen_a_newer_review(self):
        self.http.review["submitted_at"] = "2026-10-16T22:05:00Z"
        self.assertIsNone(
            resolve_rescan(
                self.args, self.ledger, actions_token="synthetic-actions-token"
            )
        )
        self.assertFalse(prepare(self.ledger).decision.admit)

    def test_complete_review_and_changed_head_use_ordinary_admission(self):
        self.ledger, bound = seed(complete=True)
        self.ledger.http = AuthorityHTTP(bound, complete=True)
        self.ledger.token = "synthetic-token"
        self.assertIsNone(
            resolve_rescan(
                self.args, self.ledger, actions_token="synthetic-actions-token"
            )
        )
        self.assertFalse(prepare(self.ledger).decision.admit)
        self.args.head_sha = "c" * 40
        self.ledger.http.head = self.args.head_sha
        self.assertIsNone(
            resolve_rescan(
                self.args, self.ledger, actions_token="synthetic-actions-token"
            )
        )

    def test_changed_snapshot_rescan_reviews_full_diff_without_incremental_reuse(self):
        from review_sensei.baseline import admission_context_from_document
        from review_sensei.coverage import CoverageManifest, FileCoverage

        ledger = InMemorySessionLedger()
        initial = tx._checkpoint_with_baseline(ledger)
        complete_review_publication(
            ledger, tx.IDENTITY, initial.transaction, published=True
        )
        initial_baseline = read_session_baseline(
            ledger, ledger.load(tx.IDENTITY).record
        )
        current_base = "c" * 40
        previous_attempt = prepare_review_transaction(
            ledger,
            tx.IDENTITY,
            tx.POLICY,
            reservation_id="e" * 64,
            base_sha=current_base,
            head_sha=tx.HEAD_SHA,
            configuration_digest=tx.CONFIGURATION_DIGEST,
            evidence_digest=tx.EVIDENCE_DIGEST,
        )
        partial_result = checkpoint_review_analysis(
            ledger, tx.IDENTITY, previous_attempt, partial.partial_result()
        )
        complete_review_publication(
            ledger, tx.IDENTITY, partial_result.transaction, published=True
        )
        self.assertEqual(
            read_session_baseline(ledger, ledger.load(tx.IDENTITY).record),
            initial_baseline,
        )
        self.http = AuthorityHTTP(partial_result)
        ledger.http, ledger.token = self.http, "synthetic-token"
        full_diff = (
            partial.gates.DIFF
            + """diff --git a/src/util.py b/src/util.py
--- a/src/util.py
+++ b/src/util.py
@@ -1 +1,2 @@
 existing
+new_util
"""
        )
        root = Path(self.temp.name)
        argv = cli_arguments(root, base_sha=current_base, diff=full_diff)
        complete = replace(
            tx._result(),
            coverage=CoverageManifest(
                files=(
                    FileCoverage("src/app.py", "reviewed"),
                    FileCoverage("src/util.py", "reviewed"),
                ),
                enumerated_paths=("src/app.py", "src/util.py"),
            ),
        )
        with (
            patch(
                "review_sensei.cli.ReviewService.run",
                return_value=ReviewRun(
                    RunOutcome("reviewed", provider_calls=1), result=complete
                ),
            ) as provider,
            patch(
                "review_sensei.cli.plan_verification_scope",
                side_effect=AssertionError(
                    "explicit reanalysis must not plan inference reuse"
                ),
            ),
            redirect_stderr(io.StringIO()) as errors,
        ):
            self.assertEqual(
                main(argv, hosted_hooks=ledger_hooks(ledger)), 0, errors.getvalue()
            )
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(provider.call_args.args[0].diff, full_diff)
        self.assertIsNone(provider.call_args.kwargs["incremental"])
        self.assertEqual(provider.call_args.kwargs["trusted_base_sha"], current_base)
        self.assertEqual(
            provider.call_args.kwargs["current_key"].base_sha, current_base
        )
        prior, current = admission_context_from_document(
            json.loads((root / "admission.json").read_text())
        )
        self.assertEqual(prior, initial_baseline)
        self.assertEqual(current.base_sha, current_base)
        self.assertEqual(current.head_sha, tx.HEAD_SHA)
        refreshed = read_session_baseline(ledger, ledger.load(tx.IDENTITY).record)
        self.assertEqual(refreshed.cache_key, current)
        self.assertNotEqual(refreshed.cache_key, initial_baseline.cache_key)

    def test_ordinary_retry_after_failed_rescan_publication_requires_recovery_without_inference(
        self,
    ):
        root = Path(self.temp.name)
        ledger = self.ledger
        bound = checkpoint_review_analysis(
            ledger,
            tx.IDENTITY,
            prepare(ledger, request(ledger)),
            partial.partial_result(),
        )
        before = complete_review_publication(
            ledger, tx.IDENTITY, bound.transaction, published=False
        )
        self.assertNotEqual(
            bound.transaction.reservation_id,
            before.rescan_state["receipts"][-1]["ordinary_reservation"],
        )
        from review_sensei.rescan import rescan_publication_recovery

        self.assertTrue(rescan_publication_recovery(before, tx.HEAD_SHA))
        self.assertFalse(rescan_publication_recovery(before, "d" * 40))
        with self.assertRaises(ReviewInputError):
            before.evolve(last_committed_reservation_id="d" * 64)

        argv = cli_arguments(root)
        argv.remove("--rescan")
        # Even a different inference configuration cannot replace the pending
        # current-head publication with a newly inferred result.
        argv[argv.index("--model") + 1] = "changed-fixture-model"
        hooks = ledger_hooks(ledger)
        with (
            patch.object(
                hooks,
                "resolve_rescan",
                side_effect=AssertionError("ordinary retry requests no new authority"),
            ),
            patch(
                "review_sensei.cli.ReviewService.run",
                side_effect=AssertionError(
                    "publication recovery must not invoke inference"
                ),
            ) as provider,
            redirect_stderr(io.StringIO()) as errors,
        ):
            self.assertEqual(main(argv, hosted_hooks=hooks), 1, errors.getvalue())
        provider.assert_not_called()
        outcome = json.loads((root / "outcome.json").read_text())
        self.assertEqual(outcome["status"], "action_required")
        self.assertEqual(outcome["diagnostic"], "publication_recovery_required")
        self.assertEqual(outcome["provider_calls"], 0)
        self.assertEqual(outcome["head_sha"], tx.HEAD_SHA)
        for filename in ("review.json", "configuration.json", "admission.json"):
            self.assertFalse((root / filename).exists(), filename)
        self.assertEqual(ledger.load(tx.IDENTITY).record, before)

    def test_real_cli_published_partial_rescan_and_duplicate_delivery(self):
        root = Path(self.temp.name)
        ledger = self.ledger
        argv = cli_arguments(root)
        hooks = ledger_hooks(ledger)
        run = ReviewRun(
            RunOutcome("partial", diagnostic="partial_coverage", provider_calls=1),
            result=partial.partial_result(),
        )
        with (
            patch("review_sensei.cli.ReviewService.run", return_value=run) as provider,
            redirect_stderr(io.StringIO()) as err,
        ):
            self.assertEqual(main(argv, hosted_hooks=hooks), 0, err.getvalue())
            self.assertEqual(provider.call_count, 1)
            rendered = json.loads((root / "review.json").read_text())
            self.assertEqual(rendered["review_status"], "partial")
            record = ledger.load(tx.IDENTITY).record
            complete_review_publication(
                ledger, tx.IDENTITY, record.transaction, published=False
            )
            self.assertEqual(main(argv, hosted_hooks=hooks), 1, err.getvalue())
            self.assertEqual(provider.call_count, 1)
            self.assertEqual(
                json.loads((root / "outcome.json").read_text())["diagnostic"],
                "publication_recovery_required",
            )
            record = ledger.load(tx.IDENTITY).record
            complete_review_publication(
                ledger, tx.IDENTITY, record.transaction, published=True
            )
            self.assertEqual(main(argv, hosted_hooks=hooks), 0, err.getvalue())
            self.assertEqual(provider.call_count, 1)
            duplicate = json.loads((root / "outcome.json").read_text())
            self.assertEqual(duplicate["provider_calls"], 0)
            self.assertNotEqual(
                duplicate["diagnostic"], "publication_recovery_required"
            )


class RescanPublicationTests(unittest.TestCase):
    def test_application_publishes_new_partial_and_retries_without_duplication(self):
        from review_sensei_github import (
            GitHubApplication,
            GitHubWriteOptions,
            ReviewPublisher,
        )

        gates = partial.gates
        ledger, old = seed()
        admitted = prepare(ledger, request(ledger))
        result = checkpoint_review_analysis(
            ledger,
            tx.IDENTITY,
            admitted,
            replace(partial.partial_result(), comments=()),
        )
        old_review = AuthorityHTTP(old).review
        checks = gates.FakeCheckRuns()
        payload = gates.pr_payload(head_sha=tx.HEAD_SHA)
        payload["base"]["sha"] = tx.BASE_SHA
        payload["base"]["repo"]["id"] = tx.IDENTITY.repository_id
        http, calls = gates.make_http(
            [
                gates.json_response(payload),
                gates.json_response([old_review]),
                gates.json_response(payload),
                gates.json_response({"id": 6}),
            ],
            routes=((checks.matches, checks.respond),),
        )
        app = GitHubApplication(
            broker=tx._Broker(),
            http=http,
            reviewer=ReviewPublisher(http=http),
            learner=tx._Noop(),
            replier=tx._Noop(),
            session_ledger=ledger,
        )
        kwargs = dict(
            options=GitHubWriteOptions(auto_review=True, github_writes=True),
            oidc_token="oidc",
            repository=tx.IDENTITY.repository,
            repository_id=99,
            pull_request=146,
            head_sha=tx.HEAD_SHA,
            base_branch="main",
            base_sha=tx.BASE_SHA,
            result=result,
            diff=gates.DIFF,
            app_slug=gates.APP,
            convergence_policy=tx.POLICY,
            configuration_context=tx.CONFIGURATION,
            evidence_context=tx.EVIDENCE,
        )
        self.assertEqual(app.publish_review(**kwargs).status, "published")
        self.assertEqual(app.publish_review(**kwargs).status, "already_published")
        bodies = [
            json.loads(body)
            for method, url, body in calls
            if method == "POST" and url.endswith("/reviews")
        ]
        self.assertEqual([body["event"] for body in bodies], ["COMMENT"])
        self.assertIn("Review incomplete", bodies[0]["body"])
        self.assertEqual(checks.conclusions, [None, "action_required"])
        self.assertEqual(ledger.load(tx.IDENTITY).record.failed_attempts, 2)

    def test_application_missing_or_mismatched_receipt_cannot_enable_reanalysis(self):
        from review_sensei_github import (
            GitHubApplication,
            GitHubWriteOptions,
            ReviewPublisher,
        )

        gates = partial.gates
        for missing in (True, False):
            with self.subTest(missing_receipt=missing):
                ledger, old = seed()
                result = checkpoint_review_analysis(
                    ledger,
                    tx.IDENTITY,
                    prepare(ledger, request(ledger)),
                    replace(partial.partial_result(), comments=()),
                )
                record = ledger.load(tx.IDENTITY).record
                state = json.loads(json.dumps(record.rescan_state))
                state["receipts"][0]["reservation"] = "f" * 64
                ledger.replace(
                    tx.IDENTITY,
                    lambda current: current.evolve(
                        rescan_state=None if missing else state
                    ),
                )
                before = ledger.load(tx.IDENTITY).record
                checks = gates.FakeCheckRuns()
                payload = gates.pr_payload(head_sha=tx.HEAD_SHA)
                payload["base"]["sha"] = tx.BASE_SHA
                payload["base"]["repo"]["id"] = 99
                http, calls = gates.make_http(
                    [
                        gates.json_response(payload),
                        gates.json_response([AuthorityHTTP(old).review]),
                    ],
                    routes=((checks.matches, checks.respond),),
                )
                publisher = ReviewPublisher(http=http)
                app = GitHubApplication(
                    broker=tx._Broker(),
                    http=http,
                    reviewer=publisher,
                    learner=tx._Noop(),
                    replier=tx._Noop(),
                    session_ledger=ledger,
                )
                with patch.object(
                    publisher, "publish", wraps=publisher.publish
                ) as publish:
                    outcome = app.publish_review(
                        options=GitHubWriteOptions(
                            auto_review=True, github_writes=True
                        ),
                        oidc_token="oidc",
                        repository=tx.IDENTITY.repository,
                        repository_id=99,
                        pull_request=146,
                        head_sha=tx.HEAD_SHA,
                        base_branch="main",
                        base_sha=tx.BASE_SHA,
                        result=result,
                        diff=gates.DIFF,
                        app_slug=gates.APP,
                        convergence_policy=tx.POLICY,
                        configuration_context=tx.CONFIGURATION,
                        evidence_context=tx.EVIDENCE,
                    )
                self.assertEqual(outcome.status, "already_published")
                self.assertNotIn("explicit_reanalysis", publish.call_args.kwargs)
                self.assertFalse(
                    any(
                        method == "POST" and url.endswith("/reviews")
                        for method, url, _ in calls
                    )
                )
                after = ledger.load(tx.IDENTITY).record
                self.assertEqual(after.rescan_state, before.rescan_state)
                self.assertEqual(after.failed_attempts, before.failed_attempts)

    def test_reanalysis_does_not_reopen_a_newer_complete_publication(self):
        from review_sensei_github import ReviewPublisher

        gates = partial.gates
        ledger, old = seed()
        result = checkpoint_review_analysis(
            ledger,
            tx.IDENTITY,
            prepare(ledger, request(ledger)),
            replace(partial.partial_result(), comments=()),
        )
        complete_review = AuthorityHTTP(old, complete=True).review
        payload = gates.pr_payload(head_sha=tx.HEAD_SHA)
        payload["base"]["sha"] = tx.BASE_SHA
        payload["base"]["repo"]["id"] = 99
        http, calls = gates.make_http(
            [gates.json_response(payload), gates.json_response([complete_review])]
        )
        outcome = ReviewPublisher(http=http).publish(
            token="synthetic-token",
            repository="owner/repo",
            repository_id=99,
            pull_request=146,
            head_sha=tx.HEAD_SHA,
            base_branch="main",
            base_sha=tx.BASE_SHA,
            result=result,
            diff=gates.DIFF,
            app_slug=gates.APP,
            convergence_policy=tx.POLICY,
            explicit_reanalysis=True,
        )
        self.assertEqual(outcome.status, "already_published")
        self.assertFalse(
            any(
                method == "POST" and url.endswith("/reviews")
                for method, url, _ in calls
            )
        )

    def test_operation_entry_uses_the_admitted_rescan_reservation(self):
        from review_sensei_github.commands import run_hosted_provider
        from review_sensei_github.operation_entry import HostedProviderResult

        ledger, _ = seed()
        admitted = prepare(ledger, request(ledger))
        run = ReviewRun(
            RunOutcome("partial", diagnostic="partial_coverage", provider_calls=1),
            result=partial.partial_result(),
        )
        with (
            patch.dict(
                os.environ, {"GITHUB_RUN_ID": "10", "REVIEWSENSEI_APP_ID": "42"}
            ),
            patch(
                "review_sensei_github.operation_entry.run_actions_analysis",
                return_value=HostedProviderResult(run, None, None),
            ) as operation,
        ):
            result = run_hosted_provider(
                SimpleNamespace(),
                SimpleNamespace(),
                hosted_session_ledger=True,
                operation_entry="enabled",
                ledger=SimpleNamespace(http=object(), token="synthetic-token"),
                repository="owner/repo",
                repository_id=99,
                pull_request=146,
                trusted_head_sha=tx.HEAD_SHA,
                analysis_reservation_id=admitted.reservation_id,
            )
        self.assertIs(result, run)
        self.assertEqual(
            operation.call_args.kwargs["reservation_id"], admitted.reservation_id
        )

    def test_complete_reanalysis_can_pass_existing_approval_checks(self):
        from review_sensei_github import ReviewPublisher

        try:
            import test_github_publication as publishing
        except ModuleNotFoundError:
            from tests import test_github_publication as publishing
        gates = partial.gates
        ledger, old = seed()
        admitted = prepare(ledger, request(ledger))
        result = checkpoint_review_analysis(ledger, tx.IDENTITY, admitted, tx._result())
        old_review = AuthorityHTTP(old).review
        payload = gates.pr_payload(head_sha=tx.HEAD_SHA)
        payload["base"]["sha"] = tx.BASE_SHA
        payload["base"]["repo"]["id"] = 99
        http, calls = gates.make_http(
            [
                gates.json_response(payload),
                gates.json_response([old_review]),
                gates.json_response(payload),
                gates.json_response({"id": 6}),
                gates.json_response(payload),
                publishing.graphql_review_threads_response(),
                gates.json_response(payload),
                gates.json_response([]),
                gates.json_response({"id": 7}),
            ]
        )
        outcome = ReviewPublisher(http=http).publish(
            token="synthetic-token",
            repository="owner/repo",
            repository_id=99,
            pull_request=146,
            head_sha=tx.HEAD_SHA,
            base_branch="main",
            base_sha=tx.BASE_SHA,
            result=result,
            diff=gates.DIFF,
            app_slug=gates.APP,
            convergence_policy=tx.POLICY,
            explicit_reanalysis=True,
        )
        self.assertEqual(outcome.status, "published")
        bodies = [
            json.loads(body)
            for method, url, body in calls
            if method == "POST" and url.endswith("/reviews")
        ]
        self.assertIn("APPROVE", [body["event"] for body in bodies])

    def test_reusable_runner_wires_both_provider_lanes_without_caller_upgrade(self):
        if os.environ.get("REVIEWSENSEI_DIST_SAFE_LANE") == "1":
            self.skipTest("Reusable workflow source is verified in the checkout lane")
        import yaml

        workflow = yaml.safe_load(
            (
                Path(__file__).parents[1] / ".github/workflows/review-sensei-run.yml"
            ).read_text()
        )
        steps = [
            step
            for job in workflow["jobs"].values()
            for step in job.get("steps", [])
            if step.get("id") == "provider-review"
        ]
        provider_jobs = [
            job
            for job in workflow["jobs"].values()
            if any(step.get("id") == "provider-review" for step in job.get("steps", []))
        ]
        self.assertEqual(len(provider_jobs), len(steps))
        for job in provider_jobs:
            concurrency = job["concurrency"]
            self.assertIn(
                "needs.authoritative-preflight.outputs.pull_request_number",
                concurrency["group"],
            )
            self.assertIs(concurrency["cancel-in-progress"], False)
        self.assertEqual(len(steps), 2)
        for step in steps:
            self.assertIn("cli_event_requests_rescan", step["run"])
            self.assertIn('"$GITHUB_EVENT_NAME" == "workflow_dispatch"', step["run"])
            self.assertIn('"$GITHUB_EVENT_NAME" == "issue_comment"', step["run"])
            self.assertIn("review_args+=(--rescan)", step["run"])
            self.assertNotIn(
                'if [[ "$GITHUB_EVENT_NAME" == "workflow_dispatch" || "$GITHUB_EVENT_NAME" == "issue_comment" ]]; then\n            review_args+=(--rescan)\n          fi',
                step["run"],
            )
            self.assertIn(
                'review-sensei-github" review "${review_args[@]}"', step["run"]
            )

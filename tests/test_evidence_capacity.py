"""Representative capacity qualification, separate from adversarial entropy cases."""

import hashlib
import unittest
from dataclasses import replace

from review_sensei.baseline import (
    BaselinePersistenceError,
    baseline_finding_from_comment,
    baseline_from_history_document,
    baseline_history_document,
)
from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.errors import ReviewInputError
from review_sensei.hosting.github import ReviewPublisher
from review_sensei.hosting.github.errors import GitHubPublicationError
from review_sensei.hosting.github.publication import (
    MAX_PUBLISHED_REVIEW_BODY_BYTES,
    approval_eligibility_from_body,
)
from review_sensei.human_assessment import MAX_HUMAN_REVIEW_BYTES, PendingHumanReview
from review_sensei.models import ReviewComment, ReviewResult
from review_sensei.session import SessionRecord, checkpoint_baseline_capacity
from tests.test_human_assessment import APP, BASE, DIFF, HEAD, State
from tests.test_review_transaction import IDENTITY, NOW, _baseline

CAUSES = (
    "The retry acknowledges an event before its durable offset is committed.",
    "The transaction rolls back its row but leaves the external object allocated.",
    "A timeout releases the lease while the worker still owns a running task.",
    "The cache key omits the policy revision used to classify this request.",
    "The decoder accepts a trailing frame after validating the first document.",
    "The shutdown path closes the socket before draining pending writes.",
    "The cursor advances past an item when the filter excludes its successor.",
    "A concurrent update replaces a newer generation with the stale snapshot.",
    "The fallback interprets unavailable permissions as an empty grant set.",
    "The collector marks enumeration complete after hitting its page budget.",
    "A canceled caller consumes the response belonging to another operation.",
    "The path check compares normalized text to the original encoded spelling.",
    "The refresh resets the retry counter without retaining the previous failure.",
    "The reader selects an older marker when the latest version is unsupported.",
    "The resolver combines identities from different repository snapshots.",
    "The staging operation records success before verifying the remote digest.",
    "The batch aggregate clears obligations omitted from a provider response.",
    "The parser treats a nonfinite numeric value as an ordinary timestamp.",
    "The semaphore is released twice when cancellation follows acquisition.",
    "The publisher retries a non-idempotent request after losing the response.",
)


def varied_comments(count, *, detail=1, unique_paths=32):
    """Different causes, code identities, traces and paths; no repeated filler.

    Hex values represent ordinary digest/request identifiers, not random prose.
    Detail adds independently identified supporting observations rather than
    repeating a paragraph to manufacture a favorable compression ratio.
    """
    comments = []
    for index in range(count):
        observations = []
        for observation in range(detail):
            identity = hashlib.sha256(
                f"case:{index}:{observation}".encode()
            ).hexdigest()
            other = hashlib.sha256(
                f"expected:{index}:{observation}".encode()
            ).hexdigest()
            observations.append(
                f"{CAUSES[(index + observation * 7) % len(CAUSES)]} "
                f"In `process_{identity[:12]}`, input `{identity}` follows branch "
                f"`state_{other[:10]}` while the persisted receipt is `{other}`. "
                f"Reproduce with request {index * 17 + observation + 1}, then "
                f"interrupt after step {observation + 2}; the next invocation "
                f"observes generation {index + 3} and cannot recover the original "
                f"operation. Validate the transition before recording its outcome "
                f"and add a regression through `handler_{other[-12:]}`."
            )
        comments.append(
            ReviewComment(
                path=f"src/services/domain_{index % unique_paths}/worker.py",
                line=index + 1,
                body="\n\n".join(observations),
                symbol=f"process_{index}_{hashlib.sha256(str(index).encode()).hexdigest()[:12]}",
                defect_kind=f"transition-{index % len(CAUSES)}",
                evidence_id=hashlib.sha256(f"evidence:{index}".encode()).hexdigest(),
                blocking=False,
                severity="high",
                needs_human=True,
            )
        )
    return tuple(comments)


def varied_baseline(count):
    comments = varied_comments(count)
    return replace(
        _baseline(1),
        findings=tuple(
            sorted(
                (baseline_finding_from_comment(item) for item in comments),
                key=lambda item: item.fingerprint,
            )
        ),
        reviewed_paths=tuple(sorted({item.path for item in comments})),
    )


def human_result(comments):
    return ReviewResult(
        summary="Representative human obligations.",
        comments=comments,
        provider="fixture",
        review_status="complete",
    )


def publication_diff(comments):
    """Valid minimal patches; later locations deliberately exercise body placement."""
    return "".join(
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n{DIFF}\n"
        for path in sorted({item.path for item in comments})
    )


def publish(state, comments):
    return ReviewPublisher(http=state.http).publish(
        token="synthetic",
        repository="owner/repo",
        repository_id=1,
        pull_request=1,
        head_sha=HEAD,
        base_branch="main",
        base_sha=BASE,
        app_slug=APP,
        convergence_policy=ReviewConvergencePolicy(enforcement="publication"),
        result=human_result(comments),
        diff=publication_diff(comments),
    )


class EvidenceCapacityTests(unittest.TestCase):
    def test_diverse_full_publication_uses_framed_body_budget(self):
        for count, detail in ((21, 3), (50, 1)):
            with self.subTest(count=count, detail=detail):
                state = State()
                comments = varied_comments(count, detail=detail)
                outcome = publish(state, comments)
                self.assertEqual(outcome.status, "published")
                authority = approval_eligibility_from_body(state.reviews[-1]["body"])
                self.assertEqual(len(authority.human_review.findings), count)
                self.assertFalse(any(event == "APPROVE" for event in state.events()))
                self.assertLessEqual(
                    len(state.reviews[-1]["body"].encode()),
                    MAX_PUBLISHED_REVIEW_BODY_BYTES,
                )
                for comment in comments:
                    self.assertIn(comment.body, state.reviews[-1]["body"])

    def test_varied_publication_above_host_bound_preserves_prior_authority(self):
        state = State()
        prior_reviews = list(state.reviews)
        # Persistence itself fits; the complete formatted text plus authority
        # cannot fit one host body. Refusal must never trim the inventory.
        comments = varied_comments(50, detail=2)
        self.assertIsNotNone(
            PendingHumanReview.from_result(human_result(comments), BASE)
        )
        with self.assertRaisesRegex(GitHubPublicationError, "publication limit"):
            publish(state, comments)
        self.assertEqual(state.reviews, prior_reviews)
        self.assertEqual(state.events(), [])

    def test_diverse_baselines_at_ordinary_counts_are_lossless(self):
        for count in (12, 21, 50):
            with self.subTest(count=count):
                baseline = varied_baseline(count)
                persisted = baseline_history_document(baseline, require_complete=True)
                restored = baseline_from_history_document(persisted)
                self.assertEqual(set(restored.findings), set(baseline.findings))
                self.assertEqual(restored.reviewed_paths, baseline.reviewed_paths)

    def test_diverse_human_resolution_growth_is_lossless(self):
        for count, detail in ((21, 1), (21, 2), (21, 3), (50, 1), (50, 2)):
            with self.subTest(count=count, detail=detail):
                inventory = PendingHumanReview.from_result(
                    human_result(varied_comments(count, detail=detail)), BASE
                )
                for resolved in (
                    (),
                    tuple(item.fingerprint for item in inventory.findings[::2]),
                    tuple(item.fingerprint for item in inventory.findings),
                ):
                    state = replace(inventory, resolved=resolved)
                    self.assertEqual(
                        PendingHumanReview.from_dict(state.to_dict()), state
                    )
                    self.assertLessEqual(
                        len(canonical_bytes(state.to_dict())), MAX_HUMAN_REVIEW_BYTES
                    )

    def test_diverse_fifty_finding_baseline_with_retained_state_and_progress(self):
        dispositions = tuple(
            {
                "fingerprint": f"{index:064x}",
                "action": "defer",
                "reason": "r" * 512,
                "actor": "a" * 256,
                "head_sha": "a" * 40,
                "expires_at": "2026-11-08T04:28:11Z",
            }
            for index in range(4)
        )
        grants = tuple(
            {
                "command_id": f"command-{index}-" + "c" * 118,
                "actor": "a" * 256,
                "head_sha": "b" * 40,
                "policy_digest": "b" * 64,
                "issued_at": "2026-10-09T04:28:11Z",
                "expires_at": "2026-11-08T04:28:11Z",
                "consumed_reservation_id": "c" * 64,
                "consumed_generation": 2147483647,
            }
            for index in range(4)
        )
        record = SessionRecord.create(
            IDENTITY,
            now=NOW,
            dispositions=dispositions,
            continuation_grants=grants,
        )
        baseline = baseline_history_document(
            varied_baseline(50),
            max_bytes=checkpoint_baseline_capacity(record),
            require_complete=True,
        )
        grown = record.evolve(
            convergence_history={
                "state": "completed",
                "baseline": baseline,
                "progress": [
                    {
                        "event": "completed",
                        "generation": index + 1,
                        "blocker_set_sha256": "b" * 64,
                        "blocker_count": 50,
                        "transaction_id": "c" * 64,
                    }
                    for index in range(3)
                ],
                "provenance": {"ledger_digest": "d" * 64},
            },
            now=NOW,
        )
        self.assertEqual(SessionRecord.from_dict(grown.to_dict()), grown)
        self.assertEqual(grown.dispositions, dispositions)
        self.assertEqual(grown.continuation_grants, grants)

    def test_path_and_varied_prose_refusals_precede_mutation_and_keep_authority(self):
        for comments, reason in (
            (varied_comments(65, unique_paths=65), "required path"),
            (varied_comments(250, detail=3), "inventory"),
        ):
            with self.subTest(reason=reason):
                state = State()
                prior_reviews = list(state.reviews)
                authority = approval_eligibility_from_body(state.reviews[-1]["body"])
                with self.assertRaisesRegex(ReviewInputError, reason):
                    publish(state, comments)
                self.assertEqual(state.reviews, prior_reviews)
                self.assertEqual(
                    approval_eligibility_from_body(state.reviews[-1]["body"]), authority
                )
                self.assertFalse(any(method != "GET" for method, _, _ in state.calls))

    def test_larger_diverse_baseline_refusal_is_explicit(self):
        with self.assertRaises(BaselinePersistenceError) as caught:
            baseline_history_document(varied_baseline(250), require_complete=True)
        self.assertEqual(
            caught.exception.persistence_diagnostics["reasons"], ["encoded-bytes"]
        )

"""Coordinated path/history limits through planning and durable public seams."""

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import review_sensei.baseline as baseline_module
import review_sensei.session as session_module
from review_sensei.baseline import (
    MAX_HISTORY_BASELINE_BYTES,
    MAX_HISTORY_ENVELOPE_RESERVE_BYTES,
    admission_context_document,
    admission_context_from_document,
    baseline_from_review,
    baseline_history_document,
    plan_verification_scope,
)
from review_sensei.convergence import ReviewConvergencePolicy, admit_review_result
from review_sensei.errors import ReviewInputError
from review_sensei.hosting.github.publication import prepare_publication_review
from review_sensei.hosting.github.session_ledger import (
    parse_session_comment,
    render_session_comment,
)
from review_sensei.models import ReviewResult
from review_sensei.schemas import validate_public_document
from review_sensei.session import (
    MAX_CONVERGENCE_HISTORY_BYTES,
    MAX_SESSION_COMMENT_BYTES,
    MAX_SESSION_RECORD_BYTES,
    LocalSessionLedger,
    SessionIdentity,
    SessionRecord,
    checkpoint_baseline_capacity,
    checkpoint_review_analysis,
    complete_review_publication,
    prepare_review_transaction,
    record_admitted_blocker_progress,
)

try:
    import test_session_ledger as session_fixtures
    from test_verification_baseline import SHA_C, _comment, _key, _result
except ImportError:
    from tests import test_session_ledger as session_fixtures
    from tests.test_verification_baseline import SHA_C, _comment, _key, _result


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def history_of_size(size):
    history = copy.deepcopy(session_fixtures.SessionRecordTests._history())
    paths = history["baseline"]["reviewed_paths"]
    while len(encoded(history)) < size:
        gap = size - len(encoded(history))
        prefix = f"src/p{len(paths):03d}"
        minimum = len(prefix) + 3
        cost = min(258, gap)
        if 0 < gap - cost < minimum:
            cost -= minimum
        assert cost >= minimum
        paths.append(prefix + "a" * (cost - 3 - len(prefix)))
    assert len(encoded(history)) == size
    return history


def capacity_fixture(count, *, width=None, unicode=False):
    def path(index):
        if unicode and index >= 16:
            return f"src/{'é' * 100}{index}.py"
        if width is None:
            return f"p{index}.py"
        prefix = f"src/package_{index:03d}/"
        return prefix + "m" * (width - len(prefix) - 3) + ".py"

    policy = ReviewConvergencePolicy(mode="merge-focused")
    prior = tuple(path(index) for index in range(16))
    # Two prior obligations, matching the durable writer's supported shape.
    baseline = baseline_from_review(
        _result(_comment(), _comment(path="src/helper.py", symbol="helper")),
        cache_key=_key(),
        policy=policy,
        related_paths=prior,
    )
    current = prior[:7] + tuple(path(index) for index in range(16, count))
    assert len(set(prior) | set(current)) == count
    return policy, baseline, current


class ContextCapacityTests(unittest.TestCase):
    def test_maximum_unicode_surrounding_fields_cannot_force_an_oversized_cas(self):
        identity = SessionIdentity("owner/repo", 136)
        dispositions = tuple(
            {
                "fingerprint": f"{i:064x}",
                "action": "defer",
                "reason": "é" * 256,
                "actor": "é" * 128,
                "head_sha": None,
                "expires_at": None,
            }
            for i in range(4)
        )
        grants = tuple(
            session_fixtures.SessionRecordTests._grant(
                command_id=f"comment-{i}", actor="😀" * 256
            )
            for i in range(4)
        )
        with tempfile.TemporaryDirectory() as temp:
            ledger = LocalSessionLedger(Path(temp))
            prior = ledger.initialize(identity, now=session_fixtures.FIXED_NOW)
            ledger.replace(
                identity,
                lambda record: record.evolve(
                    convergence_history=session_fixtures.SessionRecordTests._history(),
                    now=session_fixtures.FIXED_NOW,
                ),
                now=session_fixtures.FIXED_NOW,
            )
            before = next(Path(temp).rglob("*.json")).read_bytes()
            with self.assertRaisesRegex(ReviewInputError, "record exceeds"):
                ledger.replace(
                    identity,
                    lambda record: record.evolve(
                        dispositions=dispositions,
                        continuation_grants=grants,
                        now=session_fixtures.FIXED_NOW,
                    ),
                    now=session_fixtures.FIXED_NOW,
                )
            self.assertEqual(next(Path(temp).rglob("*.json")).read_bytes(), before)
            self.assertEqual(
                ledger.load(identity, now=session_fixtures.FIXED_NOW).record.generation,
                prior.generation,
            )

    def test_incremental_admission_at_exact_canonical_baseline_byte_boundary(self):
        policy, baseline, current = capacity_fixture(41, width=52)
        related = tuple(dict.fromkeys((*baseline.related_paths, *current)))
        reviewed = tuple(dict.fromkeys((*baseline.reviewed_paths, *current, *related)))
        candidate = replace(
            baseline,
            cache_key=_key(head_sha=SHA_C),
            generation=baseline.generation + 1,
            related_paths=related,
            reviewed_paths=reviewed,
        )
        boundary = len(
            encoded(baseline_history_document(candidate, require_complete=True))
        )
        for allowance, expected in (
            (boundary, "incremental"),
            (boundary - 1, "fallback-full"),
        ):
            scope = plan_verification_scope(
                policy=policy,
                baseline=baseline,
                current_key=_key(head_sha=SHA_C),
                changed_paths=current,
                related_paths=current,
                baseline_bytes=allowance,
            )
            self.assertEqual(scope.coverage_mode, expected)

    def test_fallback_publication_still_rejects_malformed_explicit_context(self):
        policy, baseline, _current = capacity_fixture(41)
        result = replace(_result(_comment()), coverage_mode="fallback-full")
        for related, confirmed in (
            (tuple(f"src/p{i}.py" for i in range(65)), ()),
            (("src/helper.py",), ("malformed",)),
            (("../bad.py",), ()),
        ):
            with (
                self.subTest(related=related, confirmed=confirmed),
                self.assertRaises(ReviewInputError),
            ):
                admit_review_result(
                    result,
                    policy,
                    baseline=baseline,
                    current_key=_key(head_sha=SHA_C),
                    related_paths=related,
                    evidence_confirmed_concerns=confirmed,
                )

    def test_adaptive_fallback_cannot_regain_incremental_authority_at_publication(self):
        policy, baseline, current = capacity_fixture(64, width=52)
        current_key = _key(head_sha=SHA_C)
        scope = plan_verification_scope(
            policy=policy,
            baseline=baseline,
            current_key=current_key,
            related_paths=current,
            changed_paths=current,
            baseline_bytes=5525,
        )
        self.assertEqual(scope.coverage_mode, "fallback-full")
        self.assertEqual(
            plan_verification_scope(
                policy=policy,
                baseline=baseline,
                current_key=current_key,
                related_paths=current,
                changed_paths=current,
            ).coverage_mode,
            "incremental",
        )
        restored, key = admission_context_from_document(
            json.loads(json.dumps(admission_context_document(baseline, current_key)))
        )
        result = ReviewResult.from_dict(
            replace(
                _result(_comment(symbol="different", defect_kind="different")),
                coverage_mode="fallback-full",
            ).to_dict()
        )
        diff = "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1,2 @@\n keep\n+change\n"
        prepared = prepare_publication_review(
            result=result,
            diff=diff,
            head_sha=SHA_C,
            baseline=restored,
            current_key=key,
            related_paths=current,
            convergence_policy=policy,
        )
        self.assertTrue(prepared.result.comments[0].needs_human)
        self.assertFalse(prepared.result.comments[0].effective_blocking)

    def test_eight_vs_twelve_kib_and_41_path_recovery(self):
        outcomes = {}
        for count, width in ((32, None), (41, None), (41, 52), (55, 52), (64, 52)):
            policy, baseline, current = capacity_fixture(count, width=width)
            # Every starting baseline fits the previous durable allocation.
            self.assertTrue(
                baseline_history_document(baseline, max_bytes=3072)["complete"]
            )
            outcomes[count, width] = []
            for history_limit in (8192, 12288):
                scope = plan_verification_scope(
                    policy=policy,
                    baseline=baseline,
                    current_key=_key(head_sha=SHA_C),
                    changed_paths=current,
                    related_paths=current,
                    baseline_bytes=history_limit - MAX_HISTORY_ENVELOPE_RESERVE_BYTES,
                )
                outcomes[count, width].append(scope.coverage_mode)
        self.assertEqual(outcomes[41, None], ["incremental", "incremental"])
        self.assertEqual(outcomes[41, 52], ["incremental", "incremental"])
        self.assertEqual(outcomes[55, 52], ["fallback-full", "incremental"])
        self.assertEqual(outcomes[64, 52], ["fallback-full", "incremental"])

    def test_valid_41_unicode_paths_cannot_fit_and_do_not_truncate(self):
        policy, baseline, current = capacity_fixture(41, unicode=True)
        original = baseline_history_document(baseline)
        scope = plan_verification_scope(
            policy=policy,
            baseline=baseline,
            current_key=_key(head_sha=SHA_C),
            changed_paths=current,
            related_paths=current,
        )
        self.assertEqual(scope.coverage_mode, "fallback-full")
        self.assertEqual(scope.invalidation_reason, "related-context-overflow")
        self.assertIsNone(scope.incremental)
        self.assertEqual(baseline_history_document(baseline), original)
        with self.assertRaises(ReviewInputError):
            plan_verification_scope(
                policy=policy, baseline=baseline, related_paths=("../bad.py",)
            )

    def test_history_and_record_exact_byte_boundaries_round_trip(self):
        identity = SessionIdentity("owner/repo", 136, repository_id=99)
        history = history_of_size(MAX_CONVERGENCE_HISTORY_BYTES)
        record = SessionRecord.create(
            identity, now=session_fixtures.FIXED_NOW, convergence_history=history
        )
        validate_public_document(record.to_dict(), "session-record")
        self.assertEqual(SessionRecord.from_dict(record.to_dict()), record)
        with self.assertRaisesRegex(ReviewInputError, "history exceeds"):
            record.evolve(
                convergence_history=history_of_size(MAX_CONVERGENCE_HISTORY_BYTES + 1)
            )
        # Escaped, valid surrounding fields consume more than the old wrapper
        # allowance. Fill the resulting record to its precise outer boundary.
        dispositions = tuple(
            {
                "fingerprint": f"{i:064x}",
                "action": "defer",
                "reason": "é" * 256,
                "actor": "é" * 128,
                "head_sha": None,
                "expires_at": None,
            }
            for i in range(2)
        )
        grants = tuple(
            session_fixtures.SessionRecordTests._grant(
                command_id=f"comment-{i}", actor="é" * 256
            )
            for i in range(4)
        )
        shell = SessionRecord.create(
            identity,
            now=session_fixtures.FIXED_NOW,
            dispositions=dispositions,
            continuation_grants=grants,
        )
        probe = shell.evolve(
            convergence_history=session_fixtures.SessionRecordTests._history()
        )
        outside = len(encoded(probe.to_dict())) - len(
            encoded(probe.convergence_history)
        )
        capacity = MAX_SESSION_RECORD_BYTES - outside
        self.assertLessEqual(capacity, MAX_CONVERGENCE_HISTORY_BYTES)
        exact = shell.evolve(convergence_history=history_of_size(capacity))
        self.assertEqual(len(encoded(exact.to_dict())), MAX_SESSION_RECORD_BYTES)
        self.assertEqual(SessionRecord.from_dict(exact.to_dict()), exact)
        comment = render_session_comment(
            repository_id=99, pull_request=136, record=exact
        )
        self.assertLessEqual(len(comment.encode()), MAX_SESSION_COMMENT_BYTES)
        self.assertEqual(parse_session_comment(comment, identity=identity), exact)
        self.assertLess(len(encoded({"body": comment})), 512 * 1024)
        with self.assertRaisesRegex(ReviewInputError, "record exceeds"):
            shell.evolve(convergence_history=history_of_size(capacity + 1))

    def test_old_caps_and_schema_reject_expanded_records_without_reset(self):
        identity = SessionIdentity("owner/repo", 136)
        expanded = SessionRecord.create(
            identity,
            now=session_fixtures.FIXED_NOW,
            convergence_history=history_of_size(9000),
        )
        with tempfile.TemporaryDirectory() as temp:
            ledger = LocalSessionLedger(Path(temp))
            ledger.initialize(identity, now=session_fixtures.FIXED_NOW)
            ledger.replace(
                identity, lambda _record: expanded, now=session_fixtures.FIXED_NOW
            )
            before = next(Path(temp).rglob("*.json")).read_bytes()
            with (
                patch.object(session_module, "MAX_CONVERGENCE_HISTORY_BYTES", 4096),
                patch.object(session_module, "MAX_SESSION_RECORD_BYTES", 8192),
            ):
                self.assertEqual(
                    ledger.load(identity, now=session_fixtures.FIXED_NOW).status,
                    "integrity-failed",
                )
            self.assertEqual(next(Path(temp).rglob("*.json")).read_bytes(), before)
            self.assertEqual(
                ledger.load(identity, now=session_fixtures.FIXED_NOW).record, expanded
            )
        policy, baseline, current = capacity_fixture(41)
        scope = plan_verification_scope(
            policy=policy,
            baseline=baseline,
            current_key=_key(head_sha=SHA_C),
            related_paths=current,
        )
        validate_public_document(scope.to_dict(), "verification-scope")
        expanded_baseline = baseline_history_document(
            replace(baseline, related_paths=scope.related_paths)
        )
        with patch.object(baseline_module, "MAX_RELATED_PATHS", 32):
            with self.assertRaises(ReviewInputError):
                baseline_module.baseline_from_history_document(expanded_baseline)

    def test_checkpoint_preserves_grants_and_fits_future_publication_and_owned_round(
        self,
    ):
        identity = SessionIdentity("owner/repo", 136)
        policy, baseline, _ = capacity_fixture(41)
        grants = tuple(
            session_fixtures.SessionRecordTests._grant(
                command_id=f"comment-{i}", actor="é" * 256
            )
            for i in range(4)
        )
        dispositions = tuple(
            {
                "fingerprint": f"{i:064x}",
                "action": "defer",
                "reason": "é" * 256,
                "actor": "é" * 128,
                "head_sha": None,
                "expires_at": None,
            }
            for i in range(2)
        )
        with tempfile.TemporaryDirectory() as temp:
            ledger = LocalSessionLedger(Path(temp))
            ledger.initialize(identity, now=session_fixtures.FIXED_NOW)
            ledger.replace(
                identity,
                lambda record: record.evolve(
                    dispositions=dispositions,
                    continuation_grants=grants,
                    now=session_fixtures.FIXED_NOW,
                ),
                now=session_fixtures.FIXED_NOW,
            )
            for head, reservation in (("b", "a"), ("c", "b")):
                prepared = prepare_review_transaction(
                    ledger,
                    identity,
                    policy,
                    reservation_id=reservation * 64,
                    base_sha="a" * 40,
                    head_sha=head * 40,
                    configuration_digest="d" * 64,
                    evidence_digest="e" * 64,
                    now=session_fixtures.FIXED_NOW,
                )
                ledger.replace(
                    identity,
                    lambda record: record.evolve(
                        reservation_owner={"run_id": "9" * 19, "head_sha": head * 40},
                        now=session_fixtures.FIXED_NOW,
                    ),
                    now=session_fixtures.FIXED_NOW,
                )
                held = ledger.load(identity, now=session_fixtures.FIXED_NOW).record
                self.assertLess(
                    checkpoint_baseline_capacity(held), MAX_HISTORY_BASELINE_BYTES
                )
                checkpoint = replace(
                    baseline,
                    cache_key=_key(head_sha=head * 40),
                    generation=prepared.record.generation + 1,
                )
                result = checkpoint_review_analysis(
                    ledger,
                    identity,
                    prepared,
                    _result(),
                    baseline=checkpoint,
                    now=session_fixtures.FIXED_NOW,
                )
                record_admitted_blocker_progress(
                    ledger,
                    identity,
                    result.transaction,
                    blocker_set_sha256="f" * 64,
                    blocker_count=4096,
                    suppress_publication=False,
                    now=session_fixtures.FIXED_NOW,
                )
                complete_review_publication(
                    ledger,
                    identity,
                    result.transaction,
                    published=False,
                    now=session_fixtures.FIXED_NOW,
                )
                completed = complete_review_publication(
                    ledger,
                    identity,
                    result.transaction,
                    published=True,
                    now=session_fixtures.FIXED_NOW,
                )
                self.assertEqual(completed.continuation_grants, grants)
                self.assertEqual(completed.dispositions, dispositions)
                self.assertEqual(
                    completed.convergence_history["baseline"]["findings"],
                    baseline_history_document(checkpoint)["findings"],
                )
                validate_public_document(completed.to_dict(), "session-record")
                self.assertLessEqual(
                    len(encoded(completed.to_dict())), MAX_SESSION_RECORD_BYTES
                )

    def test_required_projection_cannot_drop_findings_or_path_evidence(self):
        _policy, baseline, _current = capacity_fixture(41)
        with self.assertRaisesRegex(ReviewInputError, "persisted bound"):
            baseline_history_document(
                baseline,
                max_bytes=len(
                    encoded(
                        {
                            **baseline_history_document(baseline),
                            "reviewed_paths": [],
                            "related_paths": [],
                        }
                    )
                )
                + 10,
                require_complete=True,
            )
        too_many = replace(
            baseline,
            findings=baseline.findings
            + (replace(baseline.findings[0], fingerprint="f" * 64),),
        )
        with self.assertRaisesRegex(ReviewInputError, "persisted bound"):
            baseline_history_document(too_many, require_complete=True)
        with self.assertRaisesRegex(ReviewInputError, "require_complete"):
            baseline_history_document(baseline, require_complete="yes")

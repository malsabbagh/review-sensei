"""Native current-root observations; synthetic HTTP, no recovery authority."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError, asdict
from pathlib import Path
from unittest.mock import patch

from fake_github_http import json_response, make_http
from review_sensei_github.accepted_action import (
    observe_selected_action,
    read_current_action,
)
from review_sensei_github.session_ledger import (
    GitHubIssueCommentSessionLedger,
    parse_session_comment,
)
from review_sensei_github.source_comment_lifecycle import (
    classify_source_comment_lifecycle,
)
from test_api_work_persistence import HEAD, IDENTITY
from test_source_comment_lifecycle_g27 import SOURCE, _prepare_fixture

from review_sensei.bounded_evidence import (
    MAX_PART_READS,
    EvidenceReadBudget,
    canonical_bytes,
)
from review_sensei.errors import ReviewInputError
from review_sensei.session import SessionIdentity, SessionRecord


class AcceptedActionTests(unittest.TestCase):
    def test_native_read_requires_existing_budget_and_configured_bot_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            f, _, _, _, _, _, _ = _prepare_fixture(directory)
            for slug, budget, error in (
                (None, EvidenceReadBudget(), "configured Bot ownership"),
                ("reviewsensei[bot]", None, "existing read budget"),
            ):
                http, calls = make_http([])
                reader = GitHubIssueCommentSessionLedger(
                    http,
                    token="synthetic-read-only",
                    app_slug=slug,
                    evidence_budget=budget,
                )
                with self.assertRaisesRegex(ReviewInputError, error):
                    read_current_action(reader, IDENTITY)
                self.assertEqual(calls, [])
            for author in (None, {"type": "Bot", "login": "foreign[bot]", "id": 777}):
                comment = copy.deepcopy(f.comment)
                comment["user"] = author
                http, calls = make_http([json_response([comment])])
                budget = EvidenceReadBudget()
                reader = GitHubIssueCommentSessionLedger(
                    http,
                    token="synthetic-read-only",
                    app_slug="reviewsensei[bot]",
                    evidence_budget=budget,
                )
                with self.assertRaisesRegex(
                    ReviewInputError, "current root is unavailable"
                ):
                    read_current_action(reader, IDENTITY)
                self.assertEqual(len(calls), 1)
                self.assertEqual(budget.calls, 1)

    def test_expired_or_exhausted_budget_refuses_without_http_or_replacement(self):
        for mode in ("expired", "exhausted"):
            with self.subTest(mode=mode):
                clock = [0.0]
                budget = EvidenceReadBudget(clock=lambda: clock[0])
                if mode == "expired":
                    clock[0] = budget.deadline
                else:
                    budget.calls = MAX_PART_READS
                snapshot = budget.snapshot()
                http, calls = make_http([])
                reader = GitHubIssueCommentSessionLedger(
                    http,
                    token="synthetic-read-only",
                    app_slug="reviewsensei[bot]",
                    evidence_budget=budget,
                )
                with (
                    patch.object(
                        EvidenceReadBudget,
                        "__init__",
                        side_effect=AssertionError("no replacement read allowance"),
                    ),
                    self.assertRaisesRegex(ReviewInputError, "exhausted"),
                ):
                    read_current_action(reader, IDENTITY)
                self.assertEqual(calls, [])
                self.assertIs(reader.evidence_budget, budget)
                self.assertEqual(budget.snapshot(), snapshot)

    def test_pending_observation_is_detached_and_does_not_prove_non_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            f, _, ledger, _, _, intent, record = _prepare_fixture(directory)
            before = copy.deepcopy(record.to_dict())
            observation = read_current_action(ledger, IDENTITY)
            self.assertIsNotNone(observation)
            assert observation is not None
            self.assertEqual(observation.operation_key, intent["operation_key"])
            self.assertEqual(observation.head_sha, HEAD)
            self.assertEqual(observation.source_document(), SOURCE)
            self.assertEqual(observation.phase, "pending")
            self.assertFalse(observation.execution_started)
            self.assertIsNone(observation.terminal_outcome)
            self.assertIsNone(observation.result_sha256)
            self.assertEqual(observation.root_sha256, record.record_sha256)
            self.assertEqual(record.to_dict(), before)
            detached_source = observation.source_document()
            detached_source["id"] = 999
            self.assertEqual(observation.source_document(), SOURCE)
            with self.assertRaises(FrozenInstanceError):
                observation.phase = "terminal"
            # A cold-readable observation intentionally supplies none of the
            # G13 progress/base/inventory or G32 process-issued proof fields.
            for absent in (
                "made_progress",
                "target_base_sha",
                "open_finding_ids",
                "witness",
            ):
                self.assertFalse(hasattr(observation, absent))
            self.assertEqual(f.accounted.accounting.action["phase"], "pending")

    def test_selected_snapshot_rejects_nested_mutation_under_original_root_hash(self):
        for field, value in (("operation_key", "e" * 64), ("head_sha", "f" * 40)):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                f, _, ledger, _, _, _, record = _prepare_fixture(directory)
                root = record.record_sha256
                original_comment = copy.deepcopy(f.comment)
                # SessionRecord is frozen, but its nested mapping is not.
                record.api_run_accounting["action"]["intent"][field] = value
                count = len(f.trace)
                with self.assertRaisesRegex(ReviewInputError, "integrity check failed"):
                    observe_selected_action(ledger, IDENTITY, record)
                self.assertEqual(record.record_sha256, root)
                self.assertEqual(f.comment, original_comment)
                self.assertEqual(len(f.trace), count)
                # Re-reading the actual stored root is unaffected and does not
                # inherit the caller's mutated in-memory value.
                current = read_current_action(ledger, IDENTITY)
                self.assertEqual(current.root_sha256, root)
                self.assertNotEqual(getattr(current, field), value)

    def test_selected_snapshot_rechecks_scope_after_detaching_subclass(self):
        with tempfile.TemporaryDirectory() as directory:
            f, _, ledger, _, _, _, record = _prepare_fixture(directory)
            other_identity = SessionIdentity("owner/repo", 8, repository_id=123)
            other = SessionRecord.create(other_identity).evolve(
                api_run_accounting=record.api_run_accounting
            )

            class RedirectedRecord(SessionRecord):
                def to_dict(self):
                    return other.to_dict()

            # Native parsing produces base SessionRecords. A caller-supplied
            # subclass must not redirect the detached snapshot to another PR.
            redirected = RedirectedRecord.from_dict(record.to_dict())
            self.assertEqual(redirected.identity(), IDENTITY)
            self.assertEqual(
                SessionRecord.from_dict(redirected.to_dict()).identity(), other_identity
            )
            before = copy.deepcopy(f.comment)
            count = len(f.trace)
            with self.assertRaisesRegex(ReviewInputError, "record scope conflicts"):
                observe_selected_action(ledger, IDENTITY, redirected)
            self.assertEqual(f.comment, before)
            self.assertEqual(len(f.trace), count)

    def test_terminal_without_retained_result_refuses_observation_and_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            f, _, ledger, _, _, intent, _ = _prepare_fixture(directory)
            boundary = f.accounted.begin_action_effect(ledger, IDENTITY, intent)
            proof = f.accounted.settle_action_effect(
                ledger,
                IDENTITY,
                boundary,
                outcome={"kind": "explicit_noop", "reason": "status"},
                result_document={"action": "status", "applied": False},
            )
            closed = f.accounted.close_action(ledger, IDENTITY, proof)
            before = copy.deepcopy(f.comment)
            count = len(f.trace)
            # Simulate a corrupted in-memory selected snapshot. The closed
            # accounting codec already requires every terminal retained result.
            closed.api_run_accounting["action"]["result"] = None
            with self.assertRaisesRegex(
                ReviewInputError, "retained result is malformed"
            ):
                observe_selected_action(ledger, IDENTITY, closed)
            with self.assertRaisesRegex(
                ReviewInputError, "retained result is malformed"
            ):
                classify_source_comment_lifecycle(
                    caller=f.accounted,
                    ledger=ledger,
                    identity=IDENTITY,
                    record=closed,
                )
            self.assertEqual(f.comment, before)
            self.assertEqual(len(f.trace), count)

    def test_ordinary_lifecycle_consumes_observation_without_extra_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            f, _, ledger, _, _, _, record = _prepare_fixture(directory)
            with patch(
                "review_sensei_github.source_comment_lifecycle.observe_selected_action",
                wraps=observe_selected_action,
            ) as observer:
                classification = classify_source_comment_lifecycle(
                    caller=f.accounted,
                    ledger=ledger,
                    identity=IDENTITY,
                    record=record,
                )
            self.assertEqual(classification.route, "execute_same_source")
            self.assertFalse(classification.g32_eligible)
            self.assertGreaterEqual(observer.call_count, 1)
            # Selected-root normalization itself is pure for pending intent.
            count = len(f.trace)
            observe_selected_action(ledger, IDENTITY, record)
            self.assertEqual(len(f.trace), count)

    def test_terminal_result_reconstructs_in_fresh_process_with_only_native_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            f, _, ledger, _, _, intent, _ = _prepare_fixture(directory)
            boundary = f.accounted.begin_action_effect(ledger, IDENTITY, intent)
            proof = f.accounted.settle_action_effect(
                ledger,
                IDENTITY,
                boundary,
                outcome={"kind": "explicit_noop", "reason": "status"},
                result_document={"action": "status", "applied": False},
            )
            closed = f.accounted.close_action(ledger, IDENTITY, proof)
            selected = observe_selected_action(ledger, IDENTITY, closed)
            expected = asdict(selected)
            # The child receives only synthetic durable GitHub comment bytes,
            # not caller state, witnesses, local run files or provider results.
            script = r"""
import json, sys
from dataclasses import asdict
from urllib.parse import urlsplit
from fake_github_http import json_response
from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.session import SessionIdentity
from review_sensei_github.accepted_action import read_current_action
from review_sensei_github.http import GitHubHttp
from review_sensei_github.session_ledger import GitHubIssueCommentSessionLedger
comment = json.load(sys.stdin)
calls = []
def opener(request, timeout):
    assert request.get_method() == "GET"
    assert urlsplit(request.full_url).path == "/repos/owner/repo/issues/7/comments"
    calls.append(request.get_method())
    return json_response([comment])
budget = EvidenceReadBudget()
ledger = GitHubIssueCommentSessionLedger(
    GitHubHttp(api_url="https://api.github.test", opener=opener),
    token="synthetic-read-only", app_slug="reviewsensei[bot]", evidence_budget=budget,
)
observation = read_current_action(ledger, SessionIdentity("owner/repo", 7, repository_id=123))
print(json.dumps({"observation": asdict(observation), "calls": calls}))
"""
            root = Path(__file__).resolve().parents[1]
            env = {
                **os.environ,
                "PYTHONPATH": os.pathsep.join(
                    str(root / path)
                    for path in (
                        "src",
                        "packages/python/review-sensei-github/src",
                        "tests",
                    )
                ),
            }
            result = subprocess.run(
                [sys.executable, "-c", script],
                input=json.dumps(f.comment),
                text=True,
                capture_output=True,
                env=env,
                cwd=directory,
                timeout=30,
                check=True,
            )
            observed = json.loads(result.stdout)
            self.assertEqual(observed["observation"], expected)
            self.assertEqual(observed["calls"], ["GET"])
            self.assertEqual(expected["terminal_outcome"], "explicit_noop")
            self.assertIsNotNone(expected["result_sha256"])

    def test_bad_terminal_binding_refuses_and_no_missing_root_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            f, _, ledger, _, _, intent, _ = _prepare_fixture(directory)
            boundary = f.accounted.begin_action_effect(ledger, IDENTITY, intent)
            proof = f.accounted.settle_action_effect(
                ledger,
                IDENTITY,
                boundary,
                outcome={"kind": "explicit_noop", "reason": "status"},
                result_document={"action": "status", "applied": False},
            )
            closed = f.accounted.close_action(ledger, IDENTITY, proof)
            observed = read_current_action(ledger, IDENTITY)
            self.assertEqual(
                observed.accounting_sha256,
                hashlib.sha256(canonical_bytes(closed.api_run_accounting)).hexdigest(),
            )
            # Valid root structure is insufficient when retained result binding
            # names another operation's authenticated producer.
            document = copy.deepcopy(closed.to_dict())
            document["api_run_accounting"]["action"]["result"]["binding"][
                "producer"
            ] = "github-bot:1234"
            forged = SessionRecord.create(IDENTITY).evolve(
                api_run_accounting=document["api_run_accounting"]
            )
            with self.assertRaises(ReviewInputError):
                observe_selected_action(ledger, IDENTITY, forged)
            before = copy.deepcopy(f.comment)
            f.comment = None
            with self.assertRaisesRegex(
                ReviewInputError, "current root is unavailable"
            ):
                read_current_action(ledger, IDENTITY)
            f.comment = before
            restored = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(restored.record_sha256, closed.record_sha256)


if __name__ == "__main__":
    unittest.main()

"""Public storage contract for default partitioned admitted human evidence."""

from __future__ import annotations

import hashlib
import unittest
from copy import deepcopy
from dataclasses import replace
from unittest.mock import patch

from review_sensei_github.default_human_inventory import (
    INTENT_MARKER,
    HumanInventoryReference,
    HumanInventoryStore,
    build_human_inventory_context,
)
from review_sensei_github.http import GitHubHttp
from review_sensei_github.operation_host import RESULT_COMMENT_MARKER
from review_sensei_github.publication import IndexedFinding, finding_index_marker
from review_sensei_github.publication_recovery import publication_http

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.errors import ReviewInputError
from review_sensei.human_assessment import (
    PendingHumanReview,
    finding_instance_fingerprint,
)
from review_sensei.human_inventory import inventory_result_document
from review_sensei.models import ReviewComment, ReviewResult, ReviewTransaction
from review_sensei.session import (
    InMemorySessionLedger,
    SessionIdentity,
    checkpoint_review_analysis,
    prepare_review_transaction,
)

BASE = "a" * 40
HEAD = "b" * 40
APP = "sensei[bot]"
IDENTITY = SessionIdentity("owner/repo", 7, repository_id=9)
PUBLICATION = "e" * 64


class InventoryHttp(GitHubHttp):
    def __init__(self):
        super().__init__()
        self.comments = []
        self.requests = []
        self.lose_marker = None
        self.lose_before_commit = False
        self.head = HEAD
        self.producer = 42
        self.lose_only_last_part = False

    def request(self, method, path, *, token, body=None, timeout_seconds=None):
        self.requests.append((method, path, deepcopy(body), timeout_seconds))
        if path == "/users/sensei%5Bbot%5D":
            return 200, {"id": self.producer, "login": APP, "type": "Bot"}
        if path == "/repos/owner/repo/pulls/7":
            return 200, {
                "state": "open",
                "base": {"sha": BASE, "repo": {"id": 9, "full_name": "owner/repo"}},
                "head": {"sha": self.head},
            }
        if method == "GET" and "?per_page=100&page=" in path:
            page = int(path.rsplit("=", 1)[1])
            return 200, deepcopy(self.comments[(page - 1) * 100 : page * 100])
        if method == "POST" and path == "/repos/owner/repo/issues/7/comments":
            lose = self.lose_marker and self.lose_marker in body["body"]
            if lose and self.lose_only_last_part:
                import json

                from review_sensei_github.operation_host import comment_payload

                part = json.loads(comment_payload(body["body"]))
                lose = part["index"] == part["count"] - 1
            if not (lose and self.lose_before_commit):
                self.comments.append(
                    {
                        "id": len(self.comments) + 1,
                        "body": body["body"],
                        "user": {"id": 42, "type": "Bot"},
                        "issue_url": self.api_url + "/repos/owner/repo/issues/7",
                    }
                )
            if lose:
                raise RuntimeError("synthetic lost response, never include token")
            return 201, deepcopy(self.comments[-1])
        raise AssertionError((method, path))


class DefaultHumanInventoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.http = InventoryHttp()
        self.ledger = InMemorySessionLedger()
        self.budget = EvidenceReadBudget()
        policy = ReviewConvergencePolicy(mode="merge-focused")
        self.result = ReviewResult(
            summary="complete",
            provider="fixture",
            model="fixture",
            review_status="complete",
            comments=tuple(
                ReviewComment(
                    path=f"src/file{index}.py",
                    line=1,
                    body=f"Full explanation {index} 深🙂.",
                    needs_human=True,
                    effective_blocking=False,
                )
                for index in range(77)
            ),
        )
        configuration = ReviewTransaction.compute_configuration_digest(
            {
                "provider": {
                    "name": "fixture",
                    "profile": None,
                    "base_url": None,
                    "timeout_seconds": 900.0,
                    "max_output_tokens": None,
                    "allow_custom_endpoint": False,
                    "openrouter_policy": None,
                },
                "model": "fixture-model",
                "stages": [],
                "category_policy": [],
                "orchestration": {"enabled": False},
                "publication_mode": "merge-focused",
            }
        )
        evidence = ReviewTransaction.compute_evidence_digest(
            {"evidence_policy": "legacy", "snapshot_sha256": None}
        )
        prepared = prepare_review_transaction(
            self.ledger,
            IDENTITY,
            policy,
            reservation_id=hashlib.sha256(b"original").hexdigest(),
            base_sha=BASE,
            head_sha=HEAD,
            configuration_digest=configuration,
            evidence_digest=evidence,
        )
        self.result = checkpoint_review_analysis(
            self.ledger, IDENTITY, prepared, self.result
        )
        self.context = build_human_inventory_context(
            http=self.http,
            token="synthetic",
            ledger=self.ledger,
            identity=IDENTITY,
            base_sha=BASE,
            head_sha=HEAD,
            app_slug=APP,
            publication_digest=PUBLICATION,
            policy_digest=policy.digest(),
            configuration_digest=configuration,
            evidence_digest=evidence,
            budget=self.budget,
        )
        inventory = PendingHumanReview.from_result(self.result, base_sha=BASE)
        self.document = {
            "human_inventory": inventory_result_document(
                inventory, result_digest=PUBLICATION
            ),
            "finding_index": finding_index_marker(
                repository_id=9,
                pull_request=7,
                base_sha=BASE,
                head_sha=HEAD,
                result_digest=PUBLICATION,
                complete_inventory=True,
                entries=tuple(
                    IndexedFinding(
                        finding_instance_fingerprint(comment),
                        "f" * 64,
                        False,
                        True,
                        True,
                    )
                    for comment in self.result.comments
                ),
            ),
            "visible_prose": [],
        }
        self.store = HumanInventoryStore(
            self.http, "synthetic", self.ledger, self.context
        )

    def test_77_complete_admitted_findings_survive_fresh_store_and_replay(self):
        reference = self.store.stage(self.document, allow_start=True)
        posts = sum(method == "POST" for method, *_ in self.http.requests)
        fresh = HumanInventoryStore(self.http, "synthetic", self.ledger, self.context)
        self.assertEqual(fresh.read(reference), self.document)
        self.assertEqual(fresh.stage(self.document), reference)
        self.assertEqual(
            sum(method == "POST" for method, *_ in self.http.requests), posts
        )
        self.assertEqual(self.budget.calls, len(self.http.requests))
        self.assertTrue(all(0 < timeout <= 60 for *_, timeout in self.http.requests))

    def posts(self):
        return sum(method == "POST" for method, *_ in self.http.requests)

    def test_discovery_requires_original_complete_intent_and_never_authorizes_absence(
        self,
    ):
        self.assertIsNone(self.store.read_available())
        self.assertEqual(self.posts(), 0)
        reference = self.store.stage(self.document, allow_start=True)
        posts = self.posts()
        fresh = HumanInventoryStore(self.http, "synthetic", self.ledger, self.context)
        self.assertEqual(fresh.read_available(), (reference, self.document))
        self.assertEqual(self.posts(), posts)
        self.http.comments = [
            item for item in self.http.comments if INTENT_MARKER not in item["body"]
        ]
        self.assertIsNone(fresh.read_available())
        self.assertEqual(self.posts(), posts)

    def test_explicit_empty_human_inventory_requires_a_complete_nonhuman_index(self):
        clean = {
            **self.document,
            "human_inventory": None,
            "finding_index": finding_index_marker(
                repository_id=9,
                pull_request=7,
                base_sha=BASE,
                head_sha=HEAD,
                result_digest=PUBLICATION,
                complete_inventory=True,
                entries=tuple(
                    IndexedFinding(
                        finding_instance_fingerprint(comment),
                        "f" * 64,
                        index == 0,
                        False,
                        True,
                    )
                    for index, comment in enumerate(self.result.comments)
                ),
            ),
        }
        with self.assertRaisesRegex(ReviewInputError, "admitted identities"):
            self.store.stage(
                {**self.document, "human_inventory": None}, allow_start=True
            )
        reference = self.store.stage(clean, allow_start=True)
        self.assertEqual(self.store.read(reference), clean)

    def test_fresh_recovery_cannot_authorize_initial_storage(self):
        with self.assertRaisesRegex(ReviewInputError, "fresh recovery cannot start"):
            self.store.stage(self.document)
        self.assertEqual(self.posts(), 0)

    def test_lost_intent_ack_holds_without_payload_posts_or_fresh_restart(self):
        self.http.lose_marker = INTENT_MARKER
        with self.assertRaisesRegex(ReviewInputError, "intent outcome unknown"):
            self.store.stage(self.document, allow_start=True)
        self.assertEqual(self.posts(), 1)
        for allow in (False, True):
            with self.assertRaisesRegex(ReviewInputError, "write outcome unknown"):
                self.store.stage(self.document, allow_start=allow)
        self.assertEqual(self.posts(), 1)

    def test_missing_unconfirmed_intent_cannot_start_fresh_recovery(self):
        self.http.lose_marker = INTENT_MARKER
        self.http.lose_before_commit = True
        with self.assertRaisesRegex(ReviewInputError, "intent outcome unknown"):
            self.store.stage(self.document, allow_start=True)
        self.assertEqual(self.http.comments, [])
        with self.assertRaisesRegex(ReviewInputError, "fresh recovery cannot start"):
            self.store.stage(self.document)
        self.assertEqual(self.posts(), 1)

    def test_lost_final_part_response_reconciles_complete_actual_remote_bytes(self):
        self.http.lose_marker = RESULT_COMMENT_MARKER
        self.http.lose_only_last_part = True
        reference = self.store.stage(self.document, allow_start=True)
        posts = self.posts()
        self.assertEqual(self.store.read(reference), self.document)
        self.assertEqual(self.store.stage(self.document), reference)
        self.assertEqual(self.posts(), posts)
        self.assertEqual(self.budget.calls, len(self.http.requests))

    def test_lost_part_without_remote_commit_is_held_after_fresh_owner(self):
        self.http.lose_marker = RESULT_COMMENT_MARKER
        self.http.lose_before_commit = True
        with self.assertRaisesRegex(ReviewInputError, "write outcome unknown"):
            self.store.stage(self.document, allow_start=True)
        posts = self.posts()
        fresh = HumanInventoryStore(self.http, "synthetic", self.ledger, self.context)
        with self.assertRaisesRegex(ReviewInputError, "write outcome unknown"):
            fresh.stage(self.document)
        self.assertEqual(self.posts(), posts)

    def test_crash_after_durable_intent_never_restarts_parts(self):
        original = self.http.request

        def crash(method, path, **kwargs):
            if method == "POST" and RESULT_COMMENT_MARKER in kwargs["body"]["body"]:
                raise KeyboardInterrupt()
            return original(method, path, **kwargs)

        with patch.object(self.http, "request", side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):
                self.store.stage(self.document, allow_start=True)
        posts = self.posts()
        fresh = HumanInventoryStore(self.http, "synthetic", self.ledger, self.context)
        with self.assertRaisesRegex(ReviewInputError, "write outcome unknown"):
            fresh.stage(self.document)
        self.assertEqual(self.posts(), posts)

    def test_changed_payload_under_same_original_transaction_refuses(self):
        self.store.stage(self.document, allow_start=True)
        changed = deepcopy(self.document)
        changed["finding_index"] = changed["finding_index"].replace(
            "result=" + PUBLICATION, "result=" + "d" * 64
        )
        posts = self.posts()
        with self.assertRaises(ReviewInputError):
            self.store.stage(changed, allow_start=True)
        self.assertEqual(self.posts(), posts)

    def test_reference_is_closed_and_cannot_supply_trusted_scope(self):
        reference = self.store.stage(self.document, allow_start=True)
        self.assertEqual(
            HumanInventoryReference.from_dict(reference.to_dict()), reference
        )
        for key, value in (
            ("purpose", "publication-result"),
            ("extra", 1),
            ("event_key", "a" * 129),
            ("payload_digest", "F" * 64),
        ):
            bad = {**reference.to_dict(), key: value}
            with self.subTest(key=key), self.assertRaises(ReviewInputError):
                HumanInventoryReference.from_dict(bad)
        foreign = replace(reference, event_key="0" * 64 + "\t" + "1" * 64)
        calls = len(self.http.requests)
        with self.assertRaisesRegex(ReviewInputError, "scope conflicts"):
            self.store.read(foreign)
        self.assertEqual(len(self.http.requests), calls)

    def test_stale_head_and_different_numeric_bot_refuse_without_posts(self):
        for changed in ("head", "producer"):
            if changed == "head":
                self.http.head = "c" * 40
            else:
                self.http.head = HEAD
                self.http.producer = 41
            with self.subTest(changed=changed), self.assertRaises(ReviewInputError):
                self.store.stage(self.document, allow_start=True)
        self.assertEqual(self.posts(), 0)

    def test_malformed_document_refuses_before_any_network_or_mutation(self):
        for field in ("human_inventory", "finding_index", "visible_prose", "extra"):
            malformed = deepcopy(self.document)
            malformed[field] = None
            calls = len(self.http.requests)
            with self.subTest(field=field), self.assertRaises(ReviewInputError):
                self.store.stage(malformed, allow_start=True)
            self.assertEqual(len(self.http.requests), calls)

    def test_expired_original_budget_does_not_dispatch(self):
        self.budget.deadline = self.budget.clock()
        calls = len(self.http.requests)
        with self.assertRaisesRegex(ReviewInputError, "deadline"):
            self.store.stage(self.document, allow_start=True)
        self.assertEqual(len(self.http.requests), calls)

    def test_insufficient_remaining_budget_preflights_before_intent_post(self):
        self.budget.calls = 53
        with self.assertRaisesRegex(ReviewInputError, "cannot stage complete"):
            self.store.stage(self.document, allow_start=True)
        self.assertEqual(self.posts(), 0)

    def test_existing_publication_wrapper_does_not_double_charge(self):
        wrapped = publication_http(self.http, self.budget)
        store = HumanInventoryStore(wrapped, "synthetic", self.ledger, self.context)
        store.stage(self.document, allow_start=True)
        self.assertEqual(self.budget.calls, len(self.http.requests))

    def test_duplicate_intent_and_foreign_part_association_refuse(self):
        reference = self.store.stage(self.document, allow_start=True)
        self.http.comments.append({**deepcopy(self.http.comments[0]), "id": 999})
        with self.assertRaisesRegex(ReviewInputError, "intent is duplicated"):
            self.store.read(reference)
        self.http.comments.pop()
        self.http.comments[-1]["issue_url"] = (
            self.http.api_url + "/repos/owner/repo/issues/8"
        )
        with self.assertRaises(ReviewInputError):
            self.store.read(reference)

    def test_canonical_stored_bytes_and_binding_are_checked_independently(self):
        import base64
        import json

        from review_sensei_github.operation_host import (
            comment_payload,
            render_marked_comment,
        )

        reference = self.store.stage(self.document, allow_start=True)
        # Rebuild a complete synthetic attacker-selected envelope with a matching
        # transport digest: digest agreement must not replace expected authority.
        parts = [
            item for item in self.http.comments if RESULT_COMMENT_MARKER in item["body"]
        ]
        records = [json.loads(comment_payload(item["body"])) for item in parts]
        raw = b"".join(base64.b64decode(record["data"]) for record in records)
        envelope = json.loads(raw)
        envelope["binding"]["transaction_id"] = "f" * 64
        changed = json.dumps(
            envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        digest = hashlib.sha256(changed).hexdigest()
        # Keep a single complete part when it fits; no publisher receives this.
        if len(parts) != 1:
            self.fail("contract fixture should fit one transport part")
        records[0]["data"] = base64.b64encode(changed).decode()
        records[0]["result_sha256"] = digest
        parts[0]["body"] = render_marked_comment(RESULT_COMMENT_MARKER, records[0])
        intent = json.loads(comment_payload(self.http.comments[0]["body"]))
        intent["payload_digest"] = digest
        self.http.comments[0]["body"] = render_marked_comment(INTENT_MARKER, intent)
        with self.assertRaisesRegex(ReviewInputError, "stored authority conflicts"):
            self.store.read(replace(reference, payload_digest=digest))

    def test_250_complete_findings_cross_multiple_transport_parts(self):
        # Independent logical admission for the larger result, not a changed
        # reference or a modification of the original 77-finding checkpoint.
        self.result = replace(
            self.result,
            transaction=None,
            comments=tuple(
                replace(
                    self.result.comments[0],
                    path=f"src/large{index}.py",
                    body=f"Distinct full Unicode explanation {index} 深🙂.",
                )
                for index in range(250)
            ),
        )
        self.ledger = InMemorySessionLedger()
        prepared = prepare_review_transaction(
            self.ledger,
            IDENTITY,
            ReviewConvergencePolicy(mode="merge-focused"),
            reservation_id=hashlib.sha256(b"original250").hexdigest(),
            base_sha=BASE,
            head_sha=HEAD,
            configuration_digest=self.context.configuration_digest,
            evidence_digest=self.context.evidence_digest,
        )
        self.result = checkpoint_review_analysis(
            self.ledger, IDENTITY, prepared, self.result
        )
        self.context = build_human_inventory_context(
            http=self.http,
            token="synthetic",
            ledger=self.ledger,
            identity=IDENTITY,
            base_sha=BASE,
            head_sha=HEAD,
            app_slug=APP,
            publication_digest=PUBLICATION,
            policy_digest=self.context.policy_digest,
            configuration_digest=self.context.configuration_digest,
            evidence_digest=self.context.evidence_digest,
            budget=self.budget,
        )
        self.document["human_inventory"] = inventory_result_document(
            PendingHumanReview.from_result(self.result, base_sha=BASE),
            result_digest=PUBLICATION,
        )
        self.document["finding_index"] = finding_index_marker(
            repository_id=9,
            pull_request=7,
            base_sha=BASE,
            head_sha=HEAD,
            result_digest=PUBLICATION,
            complete_inventory=True,
            entries=tuple(
                IndexedFinding(
                    finding_instance_fingerprint(comment), "f" * 64, False, True, True
                )
                for comment in self.result.comments
            ),
        )
        self.store = HumanInventoryStore(
            self.http, "synthetic", self.ledger, self.context
        )
        reference = self.store.stage(self.document, allow_start=True)
        self.assertGreater(
            sum(RESULT_COMMENT_MARKER in item["body"] for item in self.http.comments), 1
        )
        self.assertEqual(self.store.read(reference), self.document)
        self.assertEqual(self.budget.calls, len(self.http.requests))
        self.assertLess(self.budget.calls, 60)

    def test_duplicate_current_human_index_and_missing_admission_refuse(self):
        changed = deepcopy(self.document)
        changed["finding_index"] = finding_index_marker(
            repository_id=9,
            pull_request=7,
            base_sha=BASE,
            head_sha=HEAD,
            result_digest=PUBLICATION,
            complete_inventory=True,
            entries=(),
        )
        with self.assertRaisesRegex(ReviewInputError, "admitted identities conflict"):
            self.store.stage(changed, allow_start=True)
        self.assertEqual(self.posts(), 0)

    def test_interleaved_authorized_writers_leave_duplicate_intent_held(self):
        other_budget = EvidenceReadBudget()
        other = HumanInventoryStore(
            self.http,
            "synthetic",
            self.ledger,
            replace(self.context, budget=other_budget),
        )
        original = self.http.request
        competing = []
        entered = False

        def interleave(method, path, **arguments):
            nonlocal entered
            body = arguments.get("body")
            if (
                not entered
                and method == "POST"
                and isinstance(body, dict)
                and INTENT_MARKER in body.get("body", "")
            ):
                entered = True
                competing.append(other.stage(self.document, allow_start=True))
            return original(method, path, **arguments)

        with patch.object(self.http, "request", side_effect=interleave):
            with self.assertRaises(ReviewInputError):
                self.store.stage(self.document, allow_start=True)
        self.assertEqual(len(competing), 1)
        self.assertEqual(
            sum(INTENT_MARKER in item["body"] for item in self.http.comments), 2
        )
        posts = self.posts()
        # A reference returned before the competing intent is not durable
        # authorization: every fresh reader must refuse the ambiguous set.
        with self.assertRaises(ReviewInputError):
            other.read(competing[0])
        with self.assertRaises(ReviewInputError):
            self.store.read_available()
        self.assertEqual(self.posts(), posts)
        self.assertLessEqual(self.budget.calls, 60)
        self.assertLessEqual(other_budget.calls, 60)

    def test_changed_durable_transaction_refuses_reference_read(self):
        reference = self.store.stage(self.document, allow_start=True)
        self.ledger.replace(
            IDENTITY,
            lambda record: record.evolve(
                transaction=replace(record.transaction, result_sha256="0" * 64)
            ),
        )
        calls = len(self.http.requests)
        with self.assertRaisesRegex(ReviewInputError, "durable authority conflicts"):
            self.store.read(reference)
        self.assertEqual(len(self.http.requests), calls)

    def test_child_transport_errors_do_not_expose_credentials(self):
        with patch.object(
            self.http, "request", side_effect=RuntimeError("synthetic-secret")
        ):
            with self.assertRaises(ReviewInputError) as raised:
                self.store.stage(self.document, allow_start=True)
        self.assertNotIn("synthetic-secret", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)


if __name__ == "__main__":
    unittest.main()

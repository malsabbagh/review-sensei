"""Actual guarded source retains a precommitted, undispatched review on stops."""

import copy
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from review_sensei_github.finding_lifecycle import FindingLifecycleJournal
from review_sensei_github.guarded_publication_batch import intercept_request

from tests.fake_github_http import json_response
from tests.test_quarantine_activation import registered_resource
from tests.test_quarantine_published_tail_journey import (
    BROKER_QUALIFICATIONS,
    PublishedTailJourneyFixture,
)


class GuardedPublicationBatchTests(unittest.TestCase):
    def run_source(self, *, fault=None):
        documents, entries = [], []
        checkpoint_code = FindingLifecycleJournal.checkpoint.__code__

        def observe(frame, event, value):
            if event == "call" and frame.f_code is intercept_request.__code__:
                entries.append((frame.f_locals["method"], frame.f_locals["path"]))
            if event == "return" and frame.f_code is checkpoint_code:
                journal = frame.f_locals["self"]
                documents.append(copy.deepcopy(journal._document()))
            return observe if frame.f_code is checkpoint_code else None

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            fixture = PublishedTailJourneyFixture(directory)
            _, registration = fixture.original_origins(
                failed_count=1, published_count=1
            )
            old_reviews = copy.deepcopy(fixture.reviews)
            external = fixture.external
            attempted = []

            def service(request, timeout):
                body = json.loads(request.data) if request.data else {}
                path = urlsplit(request.full_url).path
                if (
                    request.get_method() == "PATCH"
                    and "/check-runs/" in path
                    and body.get("status") == "completed"
                ):
                    attempted.append(path)
                    if fault == "rejected":
                        fixture._meter(request)
                        return json_response({"message": "forbidden"}, 403)
                    if fault == "lost":
                        external(request, timeout)
                        raise TimeoutError("completed check response lost")
                    if fault == "mismatch":
                        response = external(request, timeout)
                        value = json.loads(response.read(524288))
                        value["id"] += 1
                        return json_response(value)
                return external(request, timeout)

            fixture.external = service
            previous_trace = sys.gettrace()
            try:
                sys.settrace(observe)
                with registered_resource([registration]):
                    result = fixture.invoke()
            finally:
                sys.settrace(previous_trace)
            return {
                "code": result,
                "error": fixture.error,
                "documents": documents,
                "entries": entries,
                "attempted": attempted,
                "old_reviews": old_reviews,
                "reviews": copy.deepcopy(fixture.reviews),
            }

    def test_actual_source_uses_three_checkpoints_and_preserves_start_receipt(self):
        result = self.run_source()
        self.assertEqual(result["code"], 0, result["error"])
        documents = result["documents"]
        nonempty = [document for document in documents if document["effects"]]
        self.assertEqual(len(nonempty), 3)
        self.assertEqual([len(document["effects"]) for document in nonempty], [1, 3, 3])
        self.assertEqual(
            [effect["status"] for effect in nonempty[1]["effects"]],
            ["confirmed", "pending", "pending"],
        )
        self.assertEqual(
            [effect["status"] for effect in nonempty[-1]["effects"]],
            ["confirmed"] * 3,
        )
        self.assertEqual(nonempty[1]["effects"][0], nonempty[-1]["effects"][0])
        self.assertEqual(len(result["reviews"]), len(result["old_reviews"]) + 1)

    def test_completed_check_failure_never_dispatches_precommitted_review(self):
        for fault in ("rejected", "lost", "mismatch"):
            with self.subTest(fault=fault):
                result = self.run_source(fault=fault)
                self.assertNotEqual(result["code"], 0)
                self.assertEqual(len(result["attempted"]), 1)
                self.assertEqual(result["reviews"], result["old_reviews"])
                effects = result["documents"][-1]["effects"]
                self.assertEqual(len(effects), 3)
                self.assertEqual(effects[0]["status"], "confirmed")
                self.assertEqual(effects[-1]["status"], "pending")
                self.assertIsNone(effects[-1]["response"])
                self.assertEqual(effects[-1]["request"]["comments"], [])


class GuardedPairSchemaTests(unittest.TestCase):
    def pair(self, *, existing=False):
        from types import SimpleNamespace

        from review_sensei_github.checks import ReviewCheckPublisher
        from review_sensei_github.guarded_publication_batch import (
            _effect,
            describe_pair,
        )

        from tests.test_finding_lifecycle_journal import HEAD, document

        publisher = ReviewCheckPublisher(http=SimpleNamespace())
        start = _effect(
            "PATCH" if existing else "POST",
            "/repos/o/r/check-runs/42" if existing else "/repos/o/r/check-runs",
            publisher._mutation_body(
                repository="o/r",
                head_sha=HEAD,
                status="in_progress",
                title="Review started",
                summary="Source summary",
                conclusion=None,
                create=not existing,
            ),
        )
        start.update(status="confirmed", response=[201, {"id": 42, "app_id": 9}])
        complete = _effect(
            "PATCH",
            "/repos/o/r/check-runs/42",
            publisher._mutation_body(
                repository="o/r",
                head_sha=HEAD,
                status="completed",
                title="Review completed",
                summary="Source summary",
                conclusion="success",
                create=False,
            ),
        )
        complete["check_update_precondition"] = {
            "check_run_id": 42,
            "head_sha": HEAD,
            "app_id": 9,
            "app_slug": "sensei",
        }
        if existing:
            start["check_update_precondition"] = dict(
                complete["check_update_precondition"]
            )
        review = _effect(
            "POST",
            "/repos/o/r/pulls/1/reviews",
            {
                "commit_id": HEAD,
                "event": "COMMENT",
                "body": "exact owned review",
                "comments": [],
            },
        )
        value = document([start, complete, review])
        value.update(
            schema_version="finding-lifecycle-journal-v3",
            accepted_semantic_history=None,
            guarded_publication_pair=describe_pair(value["effects"]),
        )
        return value

    def test_closed_v3_roundtrip_and_legacy_remains_single_pending(self):
        from review_sensei_github.finding_lifecycle import (
            _pack_journal,
            _unpack_journal,
            _validate,
        )

        from review_sensei.errors import ReviewInputError

        value = self.pair()
        self.assertEqual(_validate(value), value)
        self.assertEqual(_unpack_journal(_pack_journal(value)), value)
        for version in ("finding-lifecycle-journal-v1", "finding-lifecycle-journal-v2"):
            old = copy.deepcopy(value)
            old["schema_version"] = version
            old.pop("guarded_publication_pair")
            if version.endswith("v1"):
                old.pop("accepted_semantic_history")
            with self.assertRaises(ReviewInputError):
                _validate(old)

    def test_descriptor_mutations_are_not_valid_batch_evidence(self):
        from review_sensei_github.finding_lifecycle import _validate

        from review_sensei.errors import ReviewInputError

        for change in (
            "digest",
            "order",
            "receipt",
            "unknown",
            "extra",
            "app",
            "null",
            "review_confirmed",
        ):
            with self.subTest(change=change):
                value = self.pair()
                if change == "digest":
                    value["guarded_publication_pair"]["review_request_sha256"] = (
                        "0" * 64
                    )
                elif change == "order":
                    value["effects"][1:3] = reversed(value["effects"][1:3])
                elif change == "receipt":
                    value["effects"][0]["response"][1]["id"] += 1
                elif change == "unknown":
                    value["schema_version"] = "finding-lifecycle-journal-v4"
                elif change == "extra":
                    value["guarded_publication_pair"]["dispatch_authority"] = True
                elif change == "app":
                    value["effects"][1]["check_update_precondition"]["app_id"] += 1
                elif change == "null":
                    value["guarded_publication_pair"] = None
                else:
                    value["effects"][2].update(
                        status="confirmed", response=[200, {"id": 1}]
                    )
                with self.assertRaises(ReviewInputError):
                    _validate(value)

    def test_rejected_completion_never_adopts_review_on_cold_finish(self):
        from review_sensei_github.finding_lifecycle import _validate

        from review_sensei.errors import ReviewInputError
        from tests.test_finding_lifecycle_journal import journal

        value = self.pair()
        value["effects"][1].update(status="rejected", response=[403, {}])
        _validate(value)
        instance = journal(retained=value)
        instance._armed = True
        checkpoints = []
        instance.checkpoint = lambda **kwargs: checkpoints.append(True)
        instance._readback = lambda *a, **k: self.fail(
            "unconfirmed predecessor allowed review read"
        )
        with self.assertRaisesRegex(
            ReviewInputError, "completed check remains unconfirmed"
        ):
            instance.finish()
        self.assertEqual(checkpoints, [True])
        self.assertEqual(instance.effects[2]["status"], "pending")

    def test_confirmed_pair_allows_later_legacy_order_but_not_pending_suffix(self):
        from review_sensei_github.finding_lifecycle import _validate
        from review_sensei_github.guarded_publication_batch import _effect

        from review_sensei.errors import ReviewInputError

        value = self.pair()
        later = _effect("POST", "/repos/o/r/check-runs", {"name": "later"})
        value["effects"].append(later)
        with self.assertRaises(ReviewInputError):
            _validate(value)
        value["effects"][1].update(
            status="confirmed", response=[200, {"id": 42, "app_id": 9}]
        )
        value["effects"][2].update(
            status="confirmed", response=[200, {"id": 99, "producer_id": 7}]
        )
        _validate(value)
        value["effects"].append(
            _effect("POST", "/repos/o/r/check-runs", {"name": "extra"})
        )
        with self.assertRaises(ReviewInputError):
            _validate(value)

    def test_unknown_completion_stops_cold_finish_before_review(self):
        from review_sensei.errors import ReviewInputError
        from tests.test_finding_lifecycle_journal import journal

        value = self.pair()
        instance = journal(retained=value)
        instance._armed = True
        instance._readback_http = object()
        instance._check_token = "test-only"
        reads, checkpoints = [], []
        instance.checkpoint = lambda **kwargs: checkpoints.append(True)

        def readback(http, effect, **kwargs):
            reads.append(effect["path"])
            return None

        instance._readback = readback
        with self.assertRaisesRegex(
            ReviewInputError, "native intent remains uncertain"
        ):
            instance.finish()
        self.assertEqual(reads, ["/repos/o/r/check-runs/42"])
        self.assertEqual(checkpoints, [True])
        self.assertEqual(
            [e["status"] for e in instance.effects], ["confirmed", "pending", "pending"]
        )

    def test_existing_check_start_uses_real_patch_shape_without_name(self):
        from review_sensei_github.finding_lifecycle import _validate

        value = self.pair(existing=True)
        self.assertNotIn("name", value["effects"][0]["request"])
        self.assertNotIn("name", value["effects"][1]["request"])
        _validate(value)

    def test_hot_pair_review_requires_exact_ledger_numeric_owner(self):
        from types import SimpleNamespace

        from review_sensei.errors import ReviewInputError
        from tests.test_finding_lifecycle_journal import journal

        for owner in (9001, 9002, None, True):
            with self.subTest(owner=owner):
                value = self.pair()
                value["effects"][1].update(
                    status="confirmed", response=[200, {"id": 42, "app_id": 9}]
                )
                instance = journal(retained=value)
                effect = instance.effects[2]
                http = SimpleNamespace(
                    request=lambda *a, **k: (200, {"id": 99, "user": {"id": owner}})
                )
                if owner == 9001:
                    instance._dispatch_effect(
                        http, effect, "POST", effect["path"], token="test", body={}
                    )
                    self.assertEqual(effect["status"], "confirmed")
                else:
                    with self.assertRaises(ReviewInputError):
                        instance._dispatch_effect(
                            http, effect, "POST", effect["path"], token="test", body={}
                        )
                    self.assertEqual(effect["status"], "pending")
                    self.assertIsNone(effect["response"])

    def test_cold_pair_review_requires_owner_without_reviewer_context(self):
        from review_sensei.errors import ReviewInputError
        from tests.test_finding_lifecycle_journal import journal

        for producer in ("github-bot:9001", "github-bot:other", None):
            for observed in (9001, 9002):
                with self.subTest(producer=producer, observed=observed):
                    value = self.pair()
                    value["effects"][1].update(
                        status="confirmed", response=[200, {"id": 42, "app_id": 9}]
                    )
                    instance = journal(retained=value)
                    self.assertFalse(
                        hasattr(instance.reviewer, "_native_finding_context")
                    )
                    instance.ledger.evidence_producer = lambda: producer
                    instance._armed = True
                    instance._readback_http = object()
                    instance._review_token = "test"
                    instance._readback = lambda *a, **k: (
                        200,
                        {"id": 99, "user": {"id": observed}},
                    )
                    instance.checkpoint = lambda **kwargs: None
                    if producer == "github-bot:9001" and observed == 9001:
                        instance.finish()
                        self.assertEqual(instance.effects[2]["status"], "confirmed")
                    else:
                        with self.assertRaisesRegex(
                            ReviewInputError, "numeric producer"
                        ):
                            instance.finish()
                        self.assertEqual(instance.effects[2]["status"], "pending")
                        self.assertIsNone(instance.effects[2]["response"])

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
    def run_source(self, *, fault=None, limit=512):
        documents, entries, path_bounds, quotes = [], [], [], []
        from review_sensei_github.api_run_budget import CallerRunBudget

        budget_code = CallerRunBudget.preflight.__code__
        from review_sensei_github.finding_lifecycle import _closed_pair_path_bound

        path_code = _closed_pair_path_bound.__code__
        checkpoint_code = FindingLifecycleJournal.checkpoint.__code__

        def observe(frame, event, value):
            if (
                event == "call"
                and frame.f_code is budget_code
                and frame.f_locals["calls"] >= 60
            ):
                budget = frame.f_locals["self"]
                quotes.append(
                    (
                        frame.f_locals["calls"],
                        budget.ordinary_remaining(),
                        budget.caller.accounting.configuration.limit,
                    )
                )
            if frame.f_code is path_code:
                if event == "return" and value:
                    path_bounds.append(
                        (
                            copy.deepcopy(frame.f_locals["packed"]),
                            dict(value),
                            frame.f_locals["journal"],
                        )
                    )
                return observe
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
            if limit != 512:
                from tests.test_quarantine_evidence import git

                fixture.config_bytes = fixture.config_bytes.replace(
                    b"api_request_limit: 512", f"api_request_limit: {limit}".encode()
                )
                (fixture.root / ".reviewsensei.yml").write_bytes(fixture.config_bytes)
                git(fixture.root, "add", ".reviewsensei.yml")
                git(fixture.root, "commit", "-m", "bounded original request allocation")
            _, registration = fixture.original_origins(
                failed_count=1, published_count=1
            )
            old_reviews = copy.deepcopy(fixture.reviews)
            old_checks = copy.deepcopy(fixture.check_runs)
            external = fixture.external
            attempted = []
            review_attempts = []

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
                if request.get_method() == "POST" and path.endswith("/reviews"):
                    review_attempts.append(path)
                    if fault == "review_stored_loss":
                        external(request, timeout)
                        raise TimeoutError("main review response lost")
                    if fault == "review_absent_loss":
                        fixture._meter(request)
                        raise TimeoutError("main review was not stored")
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
                "quotes": quotes,
                "path_bounds": path_bounds,
                "code": result,
                "error": fixture.error,
                "documents": documents,
                "entries": entries,
                "attempted": attempted,
                "review_attempts": review_attempts,
                "old_reviews": old_reviews,
                "old_checks": old_checks,
                "checks": copy.deepcopy(fixture.check_runs),
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
        from review_sensei_github.finding_lifecycle import _maximum_native_allocation

        from review_sensei.bounded_evidence import canonical_bytes
        from review_sensei.errors import ReviewInputError

        self.assertTrue(result["path_bounds"])
        for packed, bounds, journal in result["path_bounds"]:
            quoted = _maximum_native_allocation(packed)
            for index, maximum in bounds.items():
                quoted["native_atoms"][index][1] = maximum
            self.assertLessEqual(
                len(canonical_bytes(packed)), len(canonical_bytes(quoted))
            )
            # Equal data or a copied journal cannot manufacture the original
            # source callsite needed for the narrower allocation.
            for source in (journal, copy.copy(journal)):
                with self.assertRaises(ReviewInputError):
                    _maximum_native_allocation(
                        copy.deepcopy(packed), _pair_source=source
                    )

    def test_original_total_exact_fit_and_one_short_before_business(self):
        for limit, remaining in ((506, 70), (504, 69)):
            with self.subTest(limit=limit):
                result = self.run_source(limit=limit)
                self.assertIn((70, remaining, limit), result["quotes"])
                if remaining == 70:
                    self.assertEqual(result["code"], 0, result["error"])
                else:
                    self.assertNotEqual(result["code"], 0)
                    self.assertIn("fund", result["error"])
                    self.assertEqual(result["entries"], [])
                    self.assertEqual(result["reviews"], result["old_reviews"])
                    self.assertEqual(result["checks"], result["old_checks"])

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

    def test_main_review_loss_preserves_pair_without_native_retry(self):
        for fault in ("review_stored_loss", "review_absent_loss"):
            with self.subTest(fault=fault):
                result = self.run_source(fault=fault)
                self.assertNotEqual(result["code"], 0, result["error"])
                self.assertEqual(len(result["review_attempts"]), 1)
                self.assertEqual(len(result["attempted"]), 1)
                self.assertEqual(
                    len(result["reviews"]),
                    len(result["old_reviews"]) + (fault == "review_stored_loss"),
                )
                self.assertTrue(result["documents"])
                effects = result["documents"][-1]["effects"]
                self.assertEqual(len(effects), 3)
                self.assertEqual(effects[0]["status"], "confirmed")
                # A stored lost response may prevent the later confirmation
                # checkpoint. The earlier pair retains both liabilities rather
                # than inventing durable confirmation from physical success.
                self.assertIn(effects[1]["status"], ("confirmed", "pending"))
                if effects[1]["status"] == "pending":
                    self.assertEqual(effects[2]["status"], "pending")
                self.assertIn(effects[2]["status"], ("confirmed", "pending"))
                self.assertIn("native outcome is unconfirmed", result["error"])
                self.assertIn("guarded_publication_pair", result["documents"][-1])


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

    def test_pair_unknown_scalars_do_not_change_compressed_skeleton(self):
        from review_sensei_github.finding_lifecycle import (
            _digest,
            _maximum_native_allocation,
            _pack_journal,
            _unpack_journal,
        )
        from review_sensei_github.guarded_publication_batch import describe_pair

        from review_sensei.bounded_evidence import canonical_bytes

        original = self.pair()
        packed = _pack_journal(original)
        maximum = _maximum_native_allocation(packed)
        for identifier, app, status in (
            (1, 1, 200),
            (42, 42, 201),
            (9223372036854775807, 9, 200),
        ):
            candidate = copy.deepcopy(original)
            start, complete, _ = candidate["effects"]
            start["response"][0] = status
            start["response"][1]["id"] = identifier
            start["response"][1]["app_id"] = app
            complete["path"] = f"/repos/o/r/check-runs/{identifier}"
            complete["check_update_precondition"]["check_run_id"] = identifier
            complete["check_update_precondition"]["app_id"] = app
            complete["request_sha256"] = _digest(
                {
                    "method": complete["method"],
                    "path": complete["path"],
                    "request": complete["request"],
                }
            )
            candidate["guarded_publication_pair"] = describe_pair(candidate["effects"])
            actual = _pack_journal(candidate)
            self.assertEqual(actual["journal"], packed["journal"])
            self.assertEqual(_unpack_journal(actual), candidate)
            self.assertLessEqual(
                len(canonical_bytes(actual)), len(canonical_bytes(maximum))
            )

    def test_initial_inner_bound_covers_target_entropy_without_decoded_authority(self):
        from review_sensei_github.finding_lifecycle import (
            _digest,
            _pack_journal,
            _unpack_journal,
        )
        from review_sensei_github.guarded_pending_publication import (
            _initial_carrier_bound,
        )

        from review_sensei.bounded_evidence import canonical_bytes
        from review_sensei.errors import ReviewInputError

        def initial(identifier, status):
            value = self.pair(existing=True)
            value.pop("guarded_publication_pair")
            value["schema_version"] = "finding-lifecycle-journal-v2"
            value["effects"] = value["effects"][:1]
            effect = value["effects"][0]
            effect["path"] = f"/repos/o/r/check-runs/{identifier}"
            effect["check_update_precondition"]["check_run_id"] = identifier
            effect["request_sha256"] = _digest(
                {
                    "method": effect["method"],
                    "path": effect["path"],
                    "request": effect["request"],
                }
            )
            if status is None:
                effect["status"], effect["response"] = "pending", None
            elif status >= 400:
                effect["status"], effect["response"] = "rejected", [status, None]
            else:
                effect["response"][0] = status
                effect["response"][1]["id"] = identifier
            return value

        for status in (None, 200, 201, 400, 401, 403, 404, 405, 410):
            upper = _initial_carrier_bound(
                _pack_journal(initial(9223372036854775807, status))
            )
            with self.assertRaises(ReviewInputError):
                _unpack_journal(upper)
            for identifier in (1, 42, 1000000007, 9223372036854775807):
                actual = _pack_journal(initial(identifier, status))
                self.assertLessEqual(
                    len(canonical_bytes(actual)), len(canonical_bytes(upper))
                )

    def test_pair_path_scope_and_alias_mutations_cannot_refine_capacity(self):
        from review_sensei_github.finding_lifecycle import (
            _digest,
            _pack_journal,
            _unpack_journal,
            _validate,
        )
        from review_sensei_github.guarded_publication_batch import _shape

        from review_sensei.bounded_evidence import decode_evidence, encode_evidence
        from review_sensei.errors import ReviewInputError

        for path in (
            "/repos/foreign/r/check-runs/42",
            "/repos/o/r/check-runs/42?x=1",
            "/repos/o/r/check-runs/42#x",
            "/repos/o/r/check-runs/not-a-number",
            "/repos/o/r/check-runs/9223372036854775808",
        ):
            value = self.pair()
            complete = value["effects"][1]
            complete["path"] = path
            complete["request_sha256"] = _digest(
                {
                    "method": complete["method"],
                    "path": path,
                    "request": complete["request"],
                }
            )
            with self.assertRaises(ReviewInputError):
                _validate(value)
        with self.assertRaisesRegex(ReviewInputError, "numeric domain"):
            _shape("PATCH", "/repos/o/r/check-runs/9223372036854775808", {})

        packed = _pack_journal(self.pair(existing=True))
        skeleton = decode_evidence(
            packed["journal"], max_encoded_bytes=1048576, max_decoded_bytes=1048576
        )
        first = skeleton["effects"][0]["path"]["$native"]
        second = skeleton["effects"][1]["path"]["$native"]
        self.assertNotEqual(first, second)
        skeleton["effects"][1]["path"]["$native"] = first

        def renumber(value):
            if isinstance(value, dict):
                if set(value) == {"$native"} and value["$native"] > second:
                    value["$native"] -= 1
                else:
                    for child in value.values():
                        renumber(child)
            elif isinstance(value, list):
                for child in value:
                    renumber(child)

        renumber(skeleton)
        del packed["native_atoms"][second]
        packed["journal"] = encode_evidence(skeleton, max_decoded_bytes=1048576)
        with self.assertRaises(ReviewInputError):
            _unpack_journal(packed)

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

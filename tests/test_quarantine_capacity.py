"""Native count/encoded growth is reserved before any successor staging."""

import copy
import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch

from fake_github_http import json_response, make_http
from review_sensei_github.publication_recovery import _publication_transport
from review_sensei_github.quarantine_capacity import NativeCapacityGuard
from review_sensei_github.quarantine_published_tail import BROKER_QUALIFICATIONS
from test_quarantine_activation import IDENTITY, registered_resource
from test_quarantine_published_tail_journey import PublishedTailJourneyFixture

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError


def no_reserve():
    return {
        name: {
            "new_objects": 0,
            "new_wire_bytes": 0,
            "positive_update_bytes": 0,
            "max_object_wire_bytes": 0,
        }
        for name in (
            "issue_comments",
            "inline_replies",
            "reviews",
            "checks",
            "threads",
            "reactions",
        )
    }


class QuarantineNativeCapacityTests(unittest.TestCase):
    def test_review_post_charges_separate_persisted_objects_even_without_ack(self):
        for response in (json_response({"id": 77}, 201), TimeoutError("unknown")):
            with self.subTest(ack=isinstance(response, TimeoutError)):
                guard = NativeCapacityGuard(
                    identity=IDENTITY,
                    reserve=no_reserve(),
                    cursor=True,
                    closure_wire=8192,
                )
                comments = [
                    {"path": f"f{i}.py", "line": 1, "side": "RIGHT", "body": "x" * 8000}
                    for i in range(80)
                ]
                frame = {
                    "body": "Complete.",
                    "event": "REQUEST_CHANGES",
                    "commit_id": "a" * 40,
                }

                def wire(value):
                    return len(json.dumps(value, separators=(",", ":")).encode())

                guard.admit_publication(
                    review_wire=wire(frame), inline_wires=[wire(c) for c in comments]
                )
                http, calls = make_http([response])
                http._quarantine_capacity_guard = guard
                payload = {**frame, "comments": comments}
                self.assertGreater(wire(payload), 512 * 1024)
                if isinstance(response, TimeoutError):
                    from review_sensei_github.errors import GitHubHTTPTransientError

                    with self.assertRaises(GitHubHTTPTransientError):
                        http.request(
                            "POST",
                            "/repos/owner/repo/pulls/7/reviews",
                            token="synthetic",
                            body=payload,
                        )
                else:
                    self.assertEqual(
                        http.request(
                            "POST",
                            "/repos/owner/repo/pulls/7/reviews",
                            token="synthetic",
                            body=payload,
                        )[0],
                        201,
                    )
                self.assertEqual(len(calls), 1)
                self.assertEqual(guard.stats["reviews"]["count"], 1)
                self.assertEqual(guard.stats["reviews"]["bytes"], wire(frame) + 4096)
                self.assertEqual(guard.stats["inline_replies"]["count"], 80)
                self.assertEqual(guard.stats["threads"]["count"], 80)
                self.assertEqual(guard.inline_bounds, [])
                charged = copy.deepcopy(guard.stats)
                with self.assertRaises(ReviewInputError):
                    http.request(
                        "POST",
                        "/repos/owner/repo/pulls/7/reviews",
                        token="synthetic",
                        body=payload,
                    )
                self.assertEqual(len(calls), 1)
                self.assertEqual(guard.stats, charged)

    def test_public_entry_and_successor_command_share_one_physical_guard(self):
        calls = []
        original = NativeCapacityGuard.before_request

        def before(guard, method, path, body):
            if method in {"POST", "PATCH", "PUT"} and path.startswith("/repos/"):
                calls.append((method, path))
            return original(guard, method, path, body)

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            f = PublishedTailJourneyFixture(directory)
            _, registration = f.original_origins()
            mutations = []
            with (
                registered_resource([registration]),
                patch.object(NativeCapacityGuard, "before_request", before),
            ):
                self.assertEqual(f.invoke(), 0, f.error)
                mutations.extend(
                    (method, path)
                    for _, method, path in f.trace
                    if method in {"POST", "PATCH", "PUT"} and path.startswith("/repos/")
                )
                physical = _publication_transport(f.selected.ledger.http)
                self.assertIsNotNone(physical._quarantine_capacity_guard)
                pending = f.selected.ledger._quarantine_authenticated.inventories
                scope = next(iter(pending))
                item = pending[scope]["assessments"][0]["instance"]
                f.quarantine_predecessor_comment_id = None
                f.select_command(
                    "@reviewsensei dismiss "
                    + item
                    + ' --reason "quarantine:'
                    + scope
                    + ' Original immutable source disproves this concern."',
                    new_run=True,
                )
                self.assertEqual(f.invoke_command(), 0, f.error or str(f.exception))
                mutations.extend(
                    (method, path)
                    for _, method, path in f.trace
                    if method in {"POST", "PATCH", "PUT"} and path.startswith("/repos/")
                )
            self.assertGreater(len(mutations), 20)
            self.assertEqual(calls, mutations)

    def test_public_entry_capacity_refusal_precedes_physical_staging(self):
        original = NativeCapacityGuard.before_request

        def before(guard, method, path, body):
            if method == "POST" and path.endswith("/issues/7/comments"):
                guard.reserve["issue_comments"]["new_objects"] = 1001
            return original(guard, method, path, body)

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            f = PublishedTailJourneyFixture(directory)
            _, registration = f.original_origins()
            old_body, provider_calls = f.comment["body"], f.provider_calls
            start = len(f.trace)
            with (
                registered_resource([registration]),
                patch.object(NativeCapacityGuard, "before_request", before),
            ):
                self.assertEqual(f.invoke(), 1, f.error)
            self.assertIn("capacity", f.error)
            self.assertEqual(f.comment["body"], old_body)
            self.assertEqual(f.provider_calls, provider_calls)
            self.assertFalse(
                any(
                    method in {"POST", "PATCH", "PUT"} and path.startswith("/repos/")
                    for _, method, path in f.trace[start:]
                )
            )

    def test_installed_oversized_native_reserve_refuses_before_staging(self):
        for surface, field, amount in (
            ("issue_comments", "new_objects", 1001),
            ("reviews", "new_wire_bytes", 2_000_000_000),
            ("inline_replies", "positive_update_bytes", 2_000_000_000),
        ):
            with (
                self.subTest(surface=surface, field=field),
                tempfile.TemporaryDirectory() as directory,
                patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
            ):
                f = PublishedTailJourneyFixture(directory)
                old, registration = f.original_origins()
                contract = registration["published_tail"]
                qualification = copy.deepcopy(
                    BROKER_QUALIFICATIONS[contract["broker_qualification_sha256"]]
                )
                qualification["exposure"]["surfaces"][surface][field] = amount
                digest = hashlib.sha256(canonical_bytes(qualification)).hexdigest()
                contract["broker_qualification_sha256"] = digest
                BROKER_QUALIFICATIONS[digest] = qualification
                before, provider_calls = f.comment["body"], f.provider_calls
                with registered_resource([registration]):
                    self.assertEqual(f.invoke(), 1)
                self.assertIn("capacity", f.error)
                self.assertEqual(f.root_posts, 0)
                self.assertFalse(
                    any(
                        method == "POST" and path.endswith("/issues/7/comments")
                        for _, method, path in f.trace
                    )
                )
                self.assertEqual(f.comment["body"], before)
                self.assertEqual(f.provider_calls, provider_calls)
                self.assertIsNone(old.api_run_accounting["terminal_transaction"])

    def test_transport_refuses_growth_before_dispatch_and_keeps_old_reserve(self):
        reserve = no_reserve()
        reserve["issue_comments"] = {
            "new_objects": 1000,
            "new_wire_bytes": 0,
            "positive_update_bytes": 0,
            "max_object_wire_bytes": 0,
        }
        # A cold unknown-count REST walk also needs its final empty response.
        # Ten full100-item pages leave no room to prove completeness.
        with self.assertRaises(ReviewInputError):
            NativeCapacityGuard(identity=IDENTITY, reserve=reserve)
        guard = NativeCapacityGuard(identity=IDENTITY, reserve=no_reserve())
        guard.seed("issue_comments", [{"id": 41, "body": "original"}])
        before = copy.deepcopy(guard.stats)
        http, calls = make_http([])
        http._quarantine_capacity_guard = guard
        with self.assertRaises(ReviewInputError):
            http.request(
                "PATCH",
                "/repos/owner/repo/issues/comments/41",
                token="synthetic",
                body={"body": "\x00" * 100000},
            )
        self.assertEqual(calls, [])
        self.assertEqual(guard.stats, before)

    def test_unknown_dispatch_stays_counted_and_repeated_updates_charge_positive_growth(
        self,
    ):
        guard = NativeCapacityGuard(identity=IDENTITY, reserve=no_reserve())
        guard.seed("issue_comments", [{"id": 41, "body": "original"}])
        guard.before_request(
            "POST", "/repos/owner/repo/issues/7/comments", {"body": "\u6f22" * 20}
        )
        self.assertEqual(guard.stats["issue_comments"]["count"], 2)
        self.assertGreater(guard.stats["issue_comments"]["bytes"], 2 * 4096 + 120)
        before = copy.deepcopy(guard.reserve)
        guard.before_request(
            "PATCH", "/repos/owner/repo/issues/comments/41", {"body": "a" * 100}
        )
        grown = copy.deepcopy(guard.stats)
        guard.before_request(
            "PATCH", "/repos/owner/repo/issues/comments/41", {"body": "small"}
        )
        self.assertEqual(
            guard.stats["issue_comments"]["bytes"], grown["issue_comments"]["bytes"]
        )
        self.assertEqual(guard.reserve, before)

    def test_largest_old_object_is_not_clamped_to_a_body_character_limit(self):
        from review_sensei_github.http import MAX_GITHUB_RESPONSE_BYTES

        reserve = no_reserve()
        reserve["reviews"].update(
            new_objects=1,
            new_wire_bytes=MAX_GITHUB_RESPONSE_BYTES,
            max_object_wire_bytes=MAX_GITHUB_RESPONSE_BYTES,
        )
        with self.assertRaises(ReviewInputError):
            NativeCapacityGuard(identity=IDENTITY, reserve=reserve)

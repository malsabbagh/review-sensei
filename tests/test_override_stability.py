"""Override identity, storage bounds, and receipt text."""

import json
import unittest
from dataclasses import replace
from unittest.mock import Mock

from review_sensei_github.commands import _COMMANDS
from review_sensei_github.publication import ReviewApprovalFinalizer

from review_sensei.convergence import ReviewConvergencePolicy, derive_blocker_candidate
from review_sensei.coverage import CoverageManifest, FileCoverage
from review_sensei.disposition import (
    FindingDisposition,
    FindingIdentity,
    OverrideReceipt,
    OverrideRejected,
    _evolve_dispositions,
    apply_session_command,
    parse_maintainer_command,
)
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewComment, ReviewResult
from review_sensei.presentation import (
    assign_finding_identifiers,
    render_command_receipt,
)
from review_sensei.session import (
    MAX_DISPOSITION_BYTES,
    MAX_SESSION_COMMENT_BYTES,
    InMemorySessionLedger,
    SessionIdentity,
    SessionRecord,
    disposition_array_bytes,
)
from tests.fake_github_http import json_response, make_http
from tests.test_github_publication import (
    DIFF,
    graphql_review_threads_response,
    pr_payload,
)

HEAD = "a" * 40
OTHER = "b" * 40
IDENTITY = SessionIdentity(repository="owner/repo", pull_request=1)


def _identity(
    instance: str, concern: str, *, overridable: bool = True
) -> FindingIdentity:
    return FindingIdentity(
        instance=instance,
        concern=concern,
        overridable=overridable,
        needs_human=True,
    )


def _command(body: str, *, head_sha: str = HEAD):
    parsed = parse_maintainer_command(body, actor="alice", head_sha=head_sha)
    assert parsed is not None
    return parsed


class OverrideStabilityTests(unittest.TestCase):
    def test_sibling_instances_of_one_concern_store_two_dispositions(self):
        instances = ("a" * 64, "b" * 64)
        concern = "c" * 64
        identifiers = assign_finding_identifiers(instances)
        inventory = tuple(_identity(item, concern) for item in instances)
        body = (
            "@reviewsensei override "
            f"{identifiers[instances[0]]} {identifiers[instances[1]]} "
            "acceptable risk"
        )
        ledger = InMemorySessionLedger()
        record, result = apply_session_command(
            ledger,
            IDENTITY,
            _command(body),
            finding_inventory=inventory,
        )
        self.assertEqual(
            {item["fingerprint"] for item in record.dispositions}, set(instances)
        )
        self.assertTrue(all(item.outcome == "recorded" for item in result.receipts))

    def test_one_unknown_id_writes_nothing(self):
        instance = "d" * 64
        identifiers = assign_finding_identifiers((instance, "e" * 64))
        inventory = (_identity(instance, "f" * 64),)
        body = (
            "@reviewsensei override "
            f"{identifiers[instance]} {identifiers['e' * 64]} acceptable risk"
        )
        ledger = InMemorySessionLedger()
        with self.assertRaises(OverrideRejected) as caught:
            apply_session_command(
                ledger,
                IDENTITY,
                _command(body),
                finding_inventory=inventory,
            )
        self.assertIn("Nothing was recorded", str(caught.exception))
        self.assertTrue(
            all(item.outcome != "recorded" for item in caught.exception.receipts)
        )
        self.assertEqual(ledger.load(IDENTITY).status, "missing")

    def test_empty_inventory_keeps_the_current_review_message(self):
        with self.assertRaises(OverrideRejected) as caught:
            apply_session_command(
                InMemorySessionLedger(),
                IDENTITY,
                _command("@reviewsensei override RS-ABCDEF acceptable risk"),
            )
        self.assertIn("finding id needs the current review", str(caught.exception))

    def test_optional_id_is_not_overridable(self):
        instance = "1" * 64
        identifier = assign_finding_identifiers((instance,))[instance]
        inventory = (_identity(instance, "2" * 64, overridable=False),)
        with self.assertRaises(OverrideRejected) as caught:
            apply_session_command(
                InMemorySessionLedger(),
                IDENTITY,
                _command(f"@reviewsensei override {identifier} acceptable risk"),
                finding_inventory=inventory,
            )
        self.assertEqual(caught.exception.receipts[0].outcome, "unknown")

    def test_five_dispositions_on_one_head_then_prune_on_the_next(self):
        instances = tuple(f"{index:02x}" + "ab" * 31 for index in range(5))
        identifiers = assign_finding_identifiers(instances)
        inventory = tuple(_identity(item, "9" * 64) for item in instances)
        listed = " ".join(identifiers[item] for item in instances)
        ledger = InMemorySessionLedger()
        first, _result = apply_session_command(
            ledger,
            IDENTITY,
            _command(f"@reviewsensei override {listed} acceptable risk"),
            finding_inventory=inventory,
        )
        self.assertEqual(len(first.dispositions), 5)
        self.assertEqual(first.generation, 1)
        again, replay = apply_session_command(
            ledger,
            IDENTITY,
            _command(f"@reviewsensei override {listed} acceptable risk"),
            finding_inventory=inventory,
        )
        self.assertEqual(again.generation, first.generation)
        self.assertTrue(
            all(item.outcome == "already-recorded" for item in replay.receipts)
        )
        moved, _moved_result = apply_session_command(
            ledger,
            IDENTITY,
            _command(
                f"@reviewsensei override {identifiers[instances[0]]} acceptable risk",
                head_sha=OTHER,
            ),
            finding_inventory=inventory,
        )
        self.assertEqual([item["head_sha"] for item in moved.dispositions], [OTHER])
        self.assertEqual(moved.generation, first.generation + 1)

    def test_long_reasons_stop_at_the_byte_budget(self):
        identity = SessionIdentity(repository="owner/repo", pull_request=2)
        ledger = InMemorySessionLedger()
        record = ledger.initialize(identity)
        stored = 0
        for index in range(32):
            disposition = FindingDisposition(
                fingerprint=f"{index:064x}",
                action="accept-risk",
                reason="x" * 512,
                actor="alice",
                head_sha=HEAD,
            )
            try:
                record = _evolve_dispositions(
                    record, (disposition,), head_sha=HEAD, now=None
                )
            except ReviewInputError as exc:
                self.assertIn("session disposition limit reached", str(exc))
                break
            stored += 1
            ledger.replace(identity, lambda current, record=record: record)
        self.assertGreater(stored, 4)
        self.assertLess(stored, 32)
        loaded = ledger.load(identity)
        self.assertEqual(len(loaded.record.dispositions), stored)
        self.assertLessEqual(
            disposition_array_bytes(loaded.record.dispositions),
            MAX_DISPOSITION_BYTES,
        )

    def test_thirty_two_short_dispositions_fit_under_the_byte_budget(self):
        import json

        dispositions = tuple(
            FindingDisposition(
                fingerprint=f"{index:02x}" + "cd" * 31,
                action="accept-risk",
                reason="acceptable risk",
                actor="alice",
                head_sha=HEAD,
            ).to_dict()
            for index in range(32)
        )
        self.assertLessEqual(
            disposition_array_bytes(dispositions), MAX_DISPOSITION_BYTES
        )
        record = SessionRecord.create(IDENTITY).evolve(dispositions=dispositions)
        self.assertEqual(len(record.dispositions), 32)
        self.assertEqual(
            len(SessionRecord.from_dict(record.to_dict()).dispositions), 32
        )
        self.assertLessEqual(
            len(json.dumps(record.to_dict()).encode("utf-8")),
            MAX_SESSION_COMMENT_BYTES,
        )
        with self.assertRaisesRegex(ReviewInputError, "configured bound"):
            SessionRecord.create(IDENTITY).evolve(
                dispositions=(
                    *dispositions,
                    FindingDisposition(
                        fingerprint="ff" + "cd" * 31,
                        action="accept-risk",
                        reason="acceptable risk",
                        actor="alice",
                        head_sha=HEAD,
                    ).to_dict(),
                )
            )

    def test_unreadable_eligibility_marker_fails_closed(self):
        legacy = _COMMANDS["_legacy_inventory"]
        http = Mock()
        with self.assertRaisesRegex(ReviewInputError, "incomplete"):
            legacy(
                http,
                token="token",
                repository="owner/repo",
                pull_request=1,
                review={
                    "id": 9,
                    "body": "<!-- reviewsensei:eligibility:v1 not-valid -->",
                },
            )
        http.paginate.assert_not_called()

    def test_failure_receipt_does_not_say_the_ledger_was_recorded(self):
        text = render_command_receipt(
            actor="alice",
            source_comment_id=7,
            command_digest="ab" * 32,
            receipts=(
                OverrideReceipt(
                    "RS-ABCDEF", None, "unknown", "needs the current review"
                ),
            ),
            ledger_written=False,
        )
        self.assertIn("The ledger was not written.", text)
        self.assertIn("needs the current review", text)
        self.assertNotIn("recorded", text)
        self.assertIn("source=7 digest=" + "ab" * 32, text)

    def test_same_head_override_refinalizes_a_complete_review(self):
        head = "b" * 40
        comment = ReviewComment(
            path="src/app.py",
            line=2,
            body="The snapshot cannot prove whether the retry drops a page.",
            blocking=False,
            severity="medium",
        )
        candidate = replace(
            derive_blocker_candidate(comment, on_changed_path=True),
            needs_human=True,
        )
        http, calls = make_http(
            [
                json_response(pr_payload(head_sha=head)),
                json_response([]),
                json_response(pr_payload(head_sha=head)),
                graphql_review_threads_response(),
                json_response({"id": 5}, 200),
                json_response(pr_payload(head_sha=head)),
                graphql_review_threads_response(),
                json_response(pr_payload(head_sha=head)),
                json_response([]),
                json_response({"id": 6}, 200),
            ]
        )
        from review_sensei_github import ReviewPublisher

        ReviewPublisher(http=http).publish(
            token="token",
            repository="owner/repo",
            repository_id=1,
            pull_request=2,
            head_sha=head,
            base_branch="main",
            base_sha="a" * 40,
            result=ReviewResult(
                summary="Summary.",
                comments=(comment,),
                provider="ollama",
                review_status="complete",
                coverage=CoverageManifest(
                    files=(
                        FileCoverage(path="src/app.py", outcome="reviewed"),
                        FileCoverage(path="src/util.py", outcome="reviewed"),
                    ),
                    enumerated_paths=("src/app.py", "src/util.py"),
                ),
            ),
            diff=DIFF,
            app_slug="reviewsensei[bot]",
            auto_approve=True,
            convergence_policy=ReviewConvergencePolicy(mode="merge-focused"),
            blocker_candidates=(candidate,),
        )
        review_body = next(
            json.loads(data.decode("utf-8"))["body"]
            for method, url, data in calls
            if method == "POST" and url.endswith("/pulls/2/reviews")
        )
        from review_sensei_github.publication import parse_finding_index

        index = parse_finding_index(
            review_body, repository_id=1, pull_request=2, head_sha=head
        )
        self.assertIsNotNone(index)
        stored = {
            "id": 5,
            "commit_id": head,
            "user": {"login": "reviewsensei[bot]"},
            "body": review_body,
        }

        def matches(request):
            return (
                "/pulls/2/reviews" in request.full_url
                and "/comments" not in request.full_url
            )

        def respond(request):
            if request.method == "PATCH":
                stored["body"] = json.loads(request.data.decode("utf-8"))["body"]
                return json_response(stored)
            if request.method == "POST":
                return json_response({"id": 9}, 200)
            return json_response([stored])

        finalizer_http, finalizer_calls = make_http(
            [
                json_response(pr_payload(head_sha=head)),
                graphql_review_threads_response(),
                json_response(pr_payload(head_sha=head)),
                graphql_review_threads_response(),
                json_response(pr_payload(head_sha=head)),
            ],
            routes=((matches, respond),),
        )
        disposition = FindingDisposition(
            fingerprint=index.entries[0].instance,
            action="accept-risk",
            reason="acceptable risk",
            actor="alice",
            head_sha=head,
        )
        outcome = ReviewApprovalFinalizer(
            http=finalizer_http
        ).refinalize_with_dispositions(
            token="token",
            check_token=None,
            repository="owner/repo",
            repository_id=1,
            pull_request=2,
            head_sha=head,
            app_slug="reviewsensei[bot]",
            actor="alice",
            finding_ids=(
                assign_finding_identifiers((index.entries[0].instance,))[
                    index.entries[0].instance
                ],
            ),
            dispositions=(disposition,),
        )
        self.assertEqual(outcome.status, "approved", outcome.diagnostic)
        approval = json.loads(finalizer_calls[-1][2].decode("utf-8"))
        self.assertEqual(approval["event"], "APPROVE")

    def test_eleven_ids_do_not_parse(self):
        identifiers = " ".join(f"RS-{index:06X}" for index in range(11))
        self.assertIsNone(
            parse_maintainer_command(
                f"@reviewsensei override {identifiers} acceptable risk",
                actor="alice",
            )
        )

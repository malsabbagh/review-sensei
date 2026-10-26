"""Current index selection through native caller accounting, without old fallbacks."""

from __future__ import annotations

import copy
import tempfile
import unittest
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from review_sensei_github.action_run import current_inventory
from review_sensei_github.api_run_requests import ApiRunSessionLedger
from review_sensei_github.errors import GitHubHTTPError
from review_sensei_github.http import GitHubHttp
from review_sensei_github.publication import (
    IndexedFinding,
    finding_index_marker,
    parse_finding_index,
)
from review_sensei_github.publication_recovery import _PaginationObservationHttp
from review_sensei_github.session_ledger import parse_session_comment
from tests.test_action_run import CommandEntryFixture

from review_sensei.errors import EvidenceBudgetExhausted, ReviewInputError
from review_sensei.session import SessionIdentity
from tests.fake_github_http import json_response
from tests.test_api_work_lifecycle import LifecycleFixture
from tests.test_api_work_persistence import HEAD, IDENTITY

FINGERPRINT = "abc123" + "0" * 58


def marker(*, head=HEAD, repository_id=123, pull_request=7, fingerprint=FINGERPRINT):
    return finding_index_marker(
        repository_id=repository_id,
        pull_request=pull_request,
        head_sha=head,
        base_sha="c" * 40,
        result_digest="d" * 64,
        entries=(
            IndexedFinding(
                instance=fingerprint,
                lifecycle="e" * 64,
                blocking=True,
                needs_human=False,
            ),
        ),
    )


def review(*, identifier=1, head=HEAD, producer=999, user_type="Bot", body=None):
    return {
        "id": identifier,
        "commit_id": head,
        "state": "COMMENTED",
        "user": {"id": producer, "type": user_type, "login": "reviewsensei[bot]"},
        "body": marker() if body is None else body,
    }


@dataclass
class InventoryReadScope:
    """Only the selector's read inputs; this grants no action/write authority."""

    http: GitHubHttp
    ledger: ApiRunSessionLedger
    identity: SessionIdentity
    head_sha: str


def inventory_scope(directory, rows):
    # Authenticate and activate the real native caller fixture. No phase budget,
    # fabricated grant, snapshot activation or business mutation is substituted.
    f = LifecycleFixture(directory, limit=1024)
    native, configuration, _evidence, owner = f.prelude()
    f.accounted.initialize_and_activate(native, IDENTITY, configuration, owner)
    ledger = ApiRunSessionLedger(native, f.accounted, IDENTITY)
    f.inventory_rows = rows
    f.inventory_status = 200
    f.inventory_reads = []
    f.inventory_full_pages = False
    f.inventory_lost = False
    f.inventory_expired = False
    original = f.accounted._opener

    def opened(request, timeout):
        parsed = urlsplit(request.full_url)
        if parsed.path == "/repos/owner/repo/pulls/7/reviews":
            f.trace.append((f.attempt, request.get_method(), parsed.path))
            f.inventory_reads.append(request.full_url)
            if f.inventory_lost:
                raise TimeoutError("synthetic lost inventory response")
            if f.inventory_expired:
                expired = f.accounted._monotonic() + 61
                f.accounted._monotonic = lambda: expired
            if f.inventory_status != 200 or not isinstance(f.inventory_rows, list):
                return json_response(f.inventory_rows, f.inventory_status)
            query = parse_qs(parsed.query)
            size, page = int(query["per_page"][0]), int(query["page"][0])
            rows = f.inventory_rows
            if f.inventory_full_pages:
                rows = [review(identifier=i + 1, body="") for i in range(size)]
                return json_response(rows)
            return json_response(copy.deepcopy(rows[(page - 1) * size : page * size]))
        return original(request, timeout)

    f.accounted._opener = opened
    http = _PaginationObservationHttp(
        GitHubHttp(api_url="https://api.github.test", opener=f.accounted.open),
        f.accounted.budget_view,
    )
    return f, InventoryReadScope(http, ledger, IDENTITY, HEAD)


class CurrentMetadataFixture(CommandEntryFixture):
    """The actual current CLI/application, with only external metadata varied."""

    def __init__(self, directory):
        super().__init__(directory, limit=1024)
        self.actor_identity = {
            "login": "alice",
            "type": "User",
            "association": "MEMBER",
        }
        self.permission = {"permission": "admin"}
        self.permission_status = 200
        self.review_status = 200
        self.metadata_reviews = None
        self.permission_reads = []
        self.inventory_reads = []
        self.inline_inventory_reads = []

    def _actor(self):
        return self.actor_identity

    def select_command(self, body, *, new_run=False):
        super().select_command(body, new_run=new_run)
        self.source["author_association"] = self.actor_identity["association"]
        self.source["user"].update(
            {k: self.actor_identity[k] for k in ("login", "type")}
        )

    def seed_partial(self):
        self.provider_comments = [
            {
                "path": "src/app.py",
                "line": 2,
                "side": "RIGHT",
                "body": "Confirm the external interface assumption.",
                "severity": "high",
                "blocking": False,
                "needs_human": True,
            }
        ]
        patch = self.root / "review.patch"
        patch.write_text(
            patch.read_text() + "\ndiff --git a/image.bin b/image.bin\n"
            "new file mode 100644\nindex 0000000..1111111\n"
            "Binary files /dev/null and b/image.bin differ\n"
        )
        if self.invoke() != 0:
            raise AssertionError(self.error)
        index = parse_finding_index(
            self.reviews[-1]["body"], repository_id=123, pull_request=7, head_sha=HEAD
        )
        self.fingerprint = index.entries[0].instance
        self.original_review = copy.deepcopy(self.reviews[-1])
        self.select_command(
            "@reviewsensei override RS-"
            + self.fingerprint[:6].upper()
            + " acceptable risk",
            new_run=True,
        )
        return self

    def record(self):
        return parse_session_comment(self.comment["body"], identity=IDENTITY)

    def external(self, request, timeout):
        path = urlsplit(request.full_url).path
        if self.command_active and request.get_method() == "GET":
            if "/collaborators/" in path and path.endswith("/permission"):
                self._meter(request)
                self.permission_reads.append(
                    (path, request.get_header("Authorization"))
                )
                return json_response(self.permission, self.permission_status)
            if path == "/repos/owner/repo/pulls/7/reviews":
                self._meter(request)
                self.inventory_reads.append(request.full_url)
                if self.review_status != 200:
                    return json_response(
                        {"message": "synthetic refusal"}, self.review_status
                    )
                rows = (
                    self.reviews
                    if self.metadata_reviews is None
                    else self.metadata_reviews
                )
                query = parse_qs(urlsplit(request.full_url).query)
                size, page = int(query["per_page"][0]), int(query["page"][0])
                return json_response(
                    copy.deepcopy(rows[(page - 1) * size : page * size])
                )
            if "/reviews/" in path and path.endswith("/comments"):
                self.inline_inventory_reads.append(path)
        return super().external(request, timeout)


class CurrentCommandInventoryTests(unittest.TestCase):
    def test_latest_numeric_app_identity_on_same_head_wins_without_login_fallback(self):
        rows = [
            review(identifier=3, body=marker(fingerprint="f" * 64)),
            review(identifier=9, producer=888, body="foreign"),
            review(identifier=8, head="a" * 40, body="stale"),
            review(identifier=2),
        ]
        with tempfile.TemporaryDirectory() as directory:
            f, scope = inventory_scope(directory, rows)
            found = current_inventory(scope)
            self.assertEqual([i.instance for i in found], ["f" * 64])
            self.assertEqual(len(f.inventory_reads), 1)
            self.assertEqual(
                scope.ledger.evidence_budget.calls,
                f.accounted.accounting.confirmed + f.accounted.accounting.uncertain,
            )

    def test_newer_empty_or_malformed_review_never_reveals_an_older_index(self):
        for body in (
            "",
            None,
            "Discussion.",
            "<!-- reviewsensei:eligibility:v1 invalid -->",
            "<!-- reviewsensei:finding-index:v1 invalid -->",
            marker(head="a" * 40),
            marker(repository_id=124),
            marker(pull_request=8),
        ):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as directory:
                newer = review(identifier=2)
                newer["body"] = body
                f, scope = inventory_scope(directory, [review(), newer])
                with self.assertRaises(ReviewInputError):
                    current_inventory(scope)
                self.assertEqual(len(f.inventory_reads), 1)
                self.assertFalse(
                    any("/reviews/2/comments" in path for _, _, path in f.trace)
                )

    def test_foreign_stale_and_non_bot_reviews_supply_no_inventory(self):
        for row in (
            review(producer=888),
            review(user_type="User"),
            review(head="a" * 40),
        ):
            with self.subTest(row=row), tempfile.TemporaryDirectory() as directory:
                f, scope = inventory_scope(directory, [row])
                with self.assertRaisesRegex(
                    ReviewInputError, "inventory is unavailable"
                ):
                    current_inventory(scope)
                self.assertEqual(len(f.inventory_reads), 1)

    def test_empty_discussions_refuse_without_old_inline_root_walk_for_any_count(self):
        for count in (10, 11):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as directory:
                rows = [review()] + [
                    review(identifier=i + 2, body="") for i in range(count)
                ]
                f, scope = inventory_scope(directory, rows)
                with self.assertRaises(ReviewInputError):
                    current_inventory(scope)
                self.assertEqual(len(f.inventory_reads), 1)
                self.assertFalse(any("/comments" in url for url in f.inventory_reads))

    def test_unreadable_list_fails_without_a_secondary_inventory_source(self):
        for status, rows in ((403, {}), (200, {}), (200, [None])):
            with (
                self.subTest(status=status, rows=rows),
                tempfile.TemporaryDirectory() as directory,
            ):
                f, scope = inventory_scope(directory, rows)
                f.inventory_status = status
                with self.assertRaises((ReviewInputError, GitHubHTTPError)):
                    current_inventory(scope)
                self.assertEqual(len(f.inventory_reads), 1)

    def test_pagination_keeps_its_existing_ten_request_ceiling(self):
        with tempfile.TemporaryDirectory() as directory:
            f, scope = inventory_scope(directory, [])
            f.inventory_full_pages = True
            with self.assertRaisesRegex(GitHubHTTPError, "pagination"):
                current_inventory(scope)
            self.assertEqual(len(f.inventory_reads), 10)

    def test_repeated_reads_cannot_reset_or_refund_the_current_caller_allowance(self):
        with tempfile.TemporaryDirectory() as directory:
            f, scope = inventory_scope(directory, [review()])
            original = f.accounted.accounting
            for _ in range(original.configuration.limit + 1):
                try:
                    current_inventory(scope)
                except EvidenceBudgetExhausted:
                    break
            else:
                self.fail(
                    "current inventory reads did not exhaust the original allowance"
                )
            self.assertGreater(len(f.inventory_reads), 60)
            charged = (
                f.accounted.accounting.confirmed + f.accounted.accounting.uncertain
            )
            attempts = len(f.inventory_reads)
            with self.assertRaises(ReviewInputError):
                current_inventory(scope)
            self.assertEqual(len(f.inventory_reads), attempts)
            self.assertEqual(
                f.accounted.accounting.confirmed + f.accounted.accounting.uncertain,
                charged,
            )
            self.assertEqual(f.accounted.accounting.identity, original.identity)
            self.assertEqual(
                f.accounted.accounting.configuration, original.configuration
            )
            self.assertEqual(len(f.accounted.observations), len(f.trace))

    def test_lost_and_expired_reads_keep_charges_without_fallback(self):
        for fault in ("lost", "expired"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                f, scope = inventory_scope(directory, [review()])
                before = f.accounted.accounting
                setattr(f, "inventory_" + fault, True)
                with self.assertRaises((ReviewInputError, GitHubHTTPError)):
                    current_inventory(scope)
                state = f.accounted.accounting
                self.assertEqual(
                    state.confirmed + state.uncertain,
                    before.confirmed + before.uncertain + 1,
                )
                if fault == "lost":
                    self.assertEqual(state.uncertain, before.uncertain + 1)
                else:
                    self.assertEqual(state.confirmed, before.confirmed + 1)
                    self.assertEqual(f.accounted.observations[-1]["outcome"], "expired")
                charged = scope.ledger.evidence_budget.calls
                with self.assertRaises(ReviewInputError):
                    current_inventory(scope)
                self.assertEqual(len(f.inventory_reads), 1)
                self.assertEqual(scope.ledger.evidence_budget.calls, charged)


if __name__ == "__main__":
    unittest.main()

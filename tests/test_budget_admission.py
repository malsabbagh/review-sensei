from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from review_sensei.convergence import (
    MAX_COMPLETED_VERIFICATION_ROUNDS,
    ReviewConvergencePolicy,
)
from review_sensei.errors import ReviewInputError
from review_sensei.hosting.github.budget_admission import (
    comments_from_pages,
    decide_automatic_review_budget,
    handoff_notice_already_posted,
    render_budget_handoff_notice,
)
from review_sensei.hosting.github.http import MAX_PAGINATION_ITEMS
from review_sensei.hosting.github.session_ledger import render_session_comment
from review_sensei.session import (
    ContinuationGrant,
    SessionIdentity,
    SessionRecord,
)

HEAD = "a" * 40
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
BOT = "reviewsensei[bot]"
IDENTITY = SessionIdentity(repository="acme/api", pull_request=7, repository_id=11)


def _session(
    *,
    initial: int,
    verification: int,
    grants: list[dict[str, object]] | None = None,
    identity: SessionIdentity = IDENTITY,
) -> SessionRecord:
    return SessionRecord.create(
        identity,
        now=NOW,
        completed_initial_reviews=initial,
        completed_verification_rounds=verification,
        continuation_grants=grants or [],
    )


def _comment(record: SessionRecord, *, repository_id: int = 11) -> dict[str, object]:
    return {
        "user": {"login": BOT, "type": "Bot"},
        "body": render_session_comment(
            repository_id=repository_id, pull_request=7, record=record
        ),
    }


def _decide(comments: object, **overrides: object) -> str:
    arguments: dict[str, object] = {
        "repository": "acme/api",
        "repository_id": 11,
        "pull_request": 7,
        "head_sha": HEAD,
        "now": NOW,
    }
    arguments.update(overrides)
    return decide_automatic_review_budget(comments, **arguments)  # type: ignore[arg-type]


class BudgetDecisionTests(unittest.TestCase):
    def test_exhausted_session_is_spent(self) -> None:
        self.assertEqual(
            _decide([_comment(_session(initial=1, verification=5))]),
            "spent",
        )

    def test_remaining_verification_round_is_review(self) -> None:
        self.assertEqual(
            _decide([_comment(_session(initial=1, verification=4))]),
            "review",
        )

    def test_unfinished_initial_review_is_review(self) -> None:
        self.assertEqual(
            _decide([_comment(_session(initial=0, verification=5))]),
            "review",
        )

    def test_paused_session_is_review(self) -> None:
        record = _session(initial=1, verification=5).evolve(
            now=NOW, generation=1, operator_paused=True
        )
        self.assertEqual(_decide([_comment(record)]), "review")

    def test_tampered_session_comment_is_review(self) -> None:
        body = _comment(_session(initial=1, verification=5))["body"]
        assert isinstance(body, str)
        tampered = body.replace(
            '"completed_verification_rounds":5',
            '"completed_verification_rounds":9',
            1,
        )
        self.assertNotEqual(tampered, body)
        self.assertEqual(
            _decide([{"user": {"login": BOT, "type": "Bot"}, "body": tampered}]),
            "review",
        )

    def test_human_authored_session_comment_is_review(self) -> None:
        comment = _comment(_session(initial=1, verification=5))
        comment["user"] = {"login": "mallory", "type": "User"}
        self.assertEqual(_decide([comment]), "review")

    def test_session_comment_naming_another_repository_id_is_review(self) -> None:
        foreign = SessionIdentity(
            repository="acme/api", pull_request=7, repository_id=99
        )
        self.assertEqual(
            _decide([_comment(_session(initial=1, verification=5, identity=foreign))]),
            "review",
        )

    def test_grant_under_the_current_policy_is_review(self) -> None:
        policy = ReviewConvergencePolicy()
        grant = ContinuationGrant.issue(
            command_id="continue-1",
            actor="maintainer",
            head_sha=HEAD,
            policy_digest=policy.digest(),
            now=NOW,
            expires_at=NOW + timedelta(hours=1),
        )
        record = _session(initial=1, verification=5, grants=[grant.to_dict()])
        self.assertEqual(
            _decide([_comment(record)], now=NOW + timedelta(minutes=1)),
            "review",
        )

    def test_grant_under_another_allowance_is_spent(self) -> None:
        # The hosted runtime matches a grant against the current policy digest
        # alone, so a grant recorded under a different allowance cannot admit a
        # round. The pre-check has to agree, or it starts a review CLI that
        # refuses and fails the check.
        other = ReviewConvergencePolicy(max_completed_verification_rounds=2)
        grant = ContinuationGrant.issue(
            command_id="continue-1",
            actor="maintainer",
            head_sha=HEAD,
            policy_digest=other.digest(),
            now=NOW,
            expires_at=NOW + timedelta(hours=1),
        )
        record = _session(initial=1, verification=5, grants=[grant.to_dict()])
        self.assertEqual(
            _decide([_comment(record)], now=NOW + timedelta(minutes=1)),
            "spent",
        )

    def test_decision_tracks_the_policy_allowance(self) -> None:
        lower = ReviewConvergencePolicy(max_completed_verification_rounds=2)
        record = _session(initial=1, verification=2)
        self.assertEqual(_decide([_comment(record)], policy=lower), "spent")
        self.assertEqual(
            _decide([_comment(record)], policy=ReviewConvergencePolicy()),
            "review",
        )
        upper = _session(initial=1, verification=MAX_COMPLETED_VERIFICATION_ROUNDS)
        self.assertEqual(
            _decide(
                [_comment(upper)],
                policy=ReviewConvergencePolicy(
                    max_completed_verification_rounds=MAX_COMPLETED_VERIFICATION_ROUNDS
                ),
            ),
            "spent",
        )

    def test_untrusted_input_is_refused(self) -> None:
        with self.assertRaises(ReviewInputError):
            _decide("not-a-list")
        with self.assertRaises(ReviewInputError):
            _decide([], head_sha="short")


class HandoffNoticeTests(unittest.TestCase):
    def test_notice_requires_a_trusted_terminal_marker(self) -> None:
        notice = render_budget_handoff_notice(head_sha=HEAD)
        self.assertFalse(
            handoff_notice_already_posted(
                [{"user": {"login": "mallory", "type": "User"}, "body": notice}],
                head_sha=HEAD,
            )
        )
        self.assertFalse(
            handoff_notice_already_posted(
                [
                    {
                        "user": {"login": BOT, "type": "Bot"},
                        "body": f"quoted\n{notice}\nthanks",
                    }
                ],
                head_sha=HEAD,
            )
        )
        self.assertTrue(
            handoff_notice_already_posted(
                [{"user": {"login": BOT, "type": "Bot"}, "body": notice}],
                head_sha=HEAD,
            )
        )

    def test_notice_names_the_maintainer_commands(self) -> None:
        notice = render_budget_handoff_notice(head_sha=HEAD)
        self.assertIn("@sensei review continue --rounds 1", notice)
        self.assertIn("@sensei re-scan", notice)
        self.assertIn(f"head={HEAD}", notice)

    def test_notice_head_must_be_a_commit_sha(self) -> None:
        with self.assertRaises(ReviewInputError):
            render_budget_handoff_notice(head_sha="HEAD")


class CommentPageTests(unittest.TestCase):
    def test_short_final_page_is_accepted(self) -> None:
        pages = [[{"id": index} for index in range(100)], [{"id": 100}]]
        self.assertEqual(len(comments_from_pages(pages)), 101)

    def test_non_final_short_page_is_refused(self) -> None:
        with self.assertRaises(ReviewInputError):
            comments_from_pages([[{"id": 0}], [{"id": 1}]])

    def test_full_final_page_is_refused(self) -> None:
        full = [[{"id": index} for index in range(100)] for _ in range(10)]
        with self.assertRaises(ReviewInputError):
            comments_from_pages(full)

    def test_bounded_page_set_is_accepted(self) -> None:
        pages = [[{"id": index} for index in range(100)] for _ in range(9)]
        pages.append([{"id": 900}])
        self.assertEqual(len(comments_from_pages(pages)), 901)
        self.assertLess(901, MAX_PAGINATION_ITEMS)

    def test_page_that_implies_another_page_is_refused(self) -> None:
        with self.assertRaises(ReviewInputError):
            comments_from_pages(
                [[{"id": index} for index in range(100)] for _ in range(11)]
            )

    def test_invalid_page_is_refused(self) -> None:
        with self.assertRaises(ReviewInputError):
            comments_from_pages([{"not": "a-page"}])


if __name__ == "__main__":
    unittest.main()

"""Workflow pre-check for a spent automatic review budget.

The reusable workflow owns the check result. This module only verifies the
session comment the way the ledger does, then reports whether the current
policy would refuse another round. A comment that fails that verification is
not treated as spent.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Mapping

from ...convergence import (
    MAX_COMPLETED_VERIFICATION_ROUNDS,
    ReviewConvergencePolicy,
)
from ...errors import ReviewInputError
from ...session import (
    SessionIdentity,
    SessionLoadError,
    active_continuation_grant,
    load_session_status,
)
from .http import MAX_PAGINATION_ITEMS
from .session_ledger import _marker_shaped, parse_session_comment

DEFAULT_BUDGET_APP_SLUG = "reviewsensei[bot]"


def handoff_notice_marker(*, head_sha: str) -> str:
    if not isinstance(head_sha, str) or len(head_sha) != 40:
        raise ReviewInputError("handoff notice head is invalid")
    return (
        "<!-- reviewsensei:handoff:v1 "
        f"head={head_sha} reason=round-budget-exhausted -->"
    )


def render_budget_handoff_notice(*, head_sha: str) -> str:
    marker = handoff_notice_marker(head_sha=head_sha)
    return (
        "ReviewSensei did not start another automated review. The automatic "
        "review budget for this pull request is used up, so the check "
        "completed without failing.\n\n"
        "A maintainer can allow another review round. The pull request author "
        "can do that when they are an owner, member, or collaborator. Comment "
        "on this pull request with exactly this line and nothing else:\n\n"
        "`@sensei review continue --rounds 1`\n\n"
        "Then start that pass with a second comment, exactly:\n\n"
        "`@sensei re-scan`\n\n"
        f"{marker}\n"
    )


def _trusted_author(item: Mapping[str, object], app_slug: str) -> bool:
    author = item.get("user")
    if not isinstance(author, Mapping) or author.get("type") != "Bot":
        return False
    login = author.get("login")
    return isinstance(login, str) and login.casefold() == app_slug.casefold()


def handoff_notice_already_posted(
    comments: object,
    *,
    head_sha: str,
    app_slug: str = DEFAULT_BUDGET_APP_SLUG,
) -> bool:
    """Return whether a trusted bot comment ends with this head's notice marker."""

    if not isinstance(comments, list):
        raise ReviewInputError("review budget comments are invalid")
    if len(comments) > MAX_PAGINATION_ITEMS:
        raise ReviewInputError("review budget comments exceed the configured bound")
    marker = handoff_notice_marker(head_sha=head_sha)
    for item in comments:
        if not isinstance(item, Mapping) or not _trusted_author(item, app_slug):
            continue
        body = item.get("body")
        if not isinstance(body, str):
            continue
        terminal = body.rstrip().rsplit("\n", 1)[-1].strip()
        if terminal == marker:
            return True
    return False


def decide_automatic_review_budget(
    comments: object,
    *,
    repository: str,
    repository_id: int,
    pull_request: int,
    head_sha: str,
    policy: ReviewConvergencePolicy | None = None,
    now: datetime | None = None,
    app_slug: str = DEFAULT_BUDGET_APP_SLUG,
) -> str:
    """Return ``spent`` or ``review``.

    ``spent`` means the verified session has used the automatic allowance and
    has no active continuation grant. Anything that cannot be verified returns
    ``review`` so a forged comment cannot skip the review command.
    """

    if not isinstance(comments, list):
        raise ReviewInputError("review budget comments are invalid")
    if len(comments) > MAX_PAGINATION_ITEMS:
        raise ReviewInputError("review budget comments exceed the configured bound")
    if not isinstance(head_sha, str) or len(head_sha) != 40:
        raise ReviewInputError("review budget head is invalid")
    resolved = policy or ReviewConvergencePolicy()
    if not isinstance(resolved, ReviewConvergencePolicy):
        raise ReviewInputError("review convergence policy is invalid")
    identity = SessionIdentity(
        repository=repository,
        pull_request=pull_request,
        repository_id=repository_id,
    )
    found = []
    for item in comments:
        if not isinstance(item, Mapping) or not _trusted_author(item, app_slug):
            continue
        body = item.get("body")
        if not isinstance(body, str) or not _marker_shaped(body):
            continue
        try:
            record = parse_session_comment(body, identity=identity)
        except SessionLoadError:
            return "review"
        if record is None:
            return "review"
        if record.repository_id not in {None, repository_id}:
            return "review"
        found.append(record)
    if len(found) != 1:
        return "review"
    record = found[0]
    if load_session_status(record, now=now).status != "ok":
        return "review"
    grant_digest = None
    for allowance in range(MAX_COMPLETED_VERIFICATION_ROUNDS + 1):
        candidate = (
            resolved
            if allowance == resolved.max_completed_verification_rounds
            else replace(resolved, max_completed_verification_rounds=allowance)
        )
        if (
            active_continuation_grant(
                record,
                head_sha=head_sha,
                policy_digest=candidate.digest(),
                now=now,
            )
            is not None
        ):
            grant_digest = candidate.digest()
            break
    if grant_digest is not None or record.operator_paused:
        return "review"
    if record.failed_attempts >= resolved.max_failed_attempts:
        return "review"
    if (
        record.completed_initial_reviews < resolved.max_completed_initial_reviews
        or record.completed_verification_rounds
        < resolved.max_completed_verification_rounds
    ):
        return "review"
    return "spent"


def comments_from_pages(pages: list[object]) -> list[object]:
    """Combine bounded comment pages, refusing a page that implies another."""

    if len(pages) > 10:
        raise ReviewInputError("review budget comments exceed the configured bound")
    comments: list[object] = []
    for index, page in enumerate(pages):
        if not isinstance(page, list) or len(page) > 100:
            raise ReviewInputError("review budget comment page is invalid")
        if index < len(pages) - 1 and len(page) != 100:
            raise ReviewInputError("review budget comment page is invalid")
        comments.extend(page)
    if len(comments) > MAX_PAGINATION_ITEMS:
        raise ReviewInputError("review budget comments exceed the configured bound")
    if (
        len(pages) == 10
        and pages
        and isinstance(pages[-1], list)
        and len(pages[-1]) == 100
    ):
        raise ReviewInputError("review budget comments exceed the configured bound")
    return comments


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    comments_path = os.environ.get("COMMENTS_FILE", "")
    if command == "assemble":
        page_dir = os.environ.get("PAGE_DIR", "")
        destination = os.environ.get("COMMENTS_FILE", "")
        if not page_dir or not destination:
            print("review budget comment pages are invalid", file=sys.stderr)
            return 1
        pages: list[object] = []
        root = Path(page_dir)
        for path in sorted(root.glob("*.json"), key=lambda item: int(item.stem)):
            try:
                pages.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
                print(f"review budget comment page is invalid: {exc}", file=sys.stderr)
                return 1
        try:
            comments = comments_from_pages(pages)
        except ReviewInputError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        Path(destination).write_text(json.dumps(comments), encoding="utf-8")
        return 0
    if command == "render-notice":
        destination = sys.argv[2] if len(sys.argv) > 2 else ""
        if not destination:
            print("review budget notice path is invalid", file=sys.stderr)
            return 1
        Path(destination).write_text(
            render_budget_handoff_notice(head_sha=os.environ["HEAD_SHA"]),
            encoding="utf-8",
        )
        return 0
    try:
        comments = json.loads(Path(comments_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"review budget comments could not be read: {exc}", file=sys.stderr)
        return 1
    head_sha = os.environ.get("HEAD_SHA", "")
    if command == "notice-status":
        try:
            posted = handoff_notice_already_posted(comments, head_sha=head_sha)
        except ReviewInputError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print("posted" if posted else "missing")
        return 0
    if command != "decide":
        print("review budget command is invalid", file=sys.stderr)
        return 1
    try:
        decision = decide_automatic_review_budget(
            comments,
            repository=os.environ["REPOSITORY"],
            repository_id=int(os.environ["REPOSITORY_ID"]),
            pull_request=int(os.environ["PULL_REQUEST"]),
            head_sha=head_sha,
        )
    except (ReviewInputError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(decision)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

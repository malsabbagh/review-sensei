"""Authenticate explicit Actions reanalysis without expanding broker authority."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from review_sensei.errors import ReviewInputError
from review_sensei.rescan import AuthorizedRescan, rescan_source
from review_sensei.session import SessionIdentity
from review_sensei_github.publication import (
    PUBLISHED_REVIEW_STATES,
    approval_eligibility_from_body,
    review_identity_marker,
)
from review_sensei_github.trigger import (
    choose_head_sha,
    extract_requested_commit,
    issue_comment_requests_rescan,
)


def _object(value):
    if not isinstance(value, dict):
        raise ReviewInputError("explicit rescan metadata is invalid")
    return value


def resolve_rescan(args, ledger, *, actions_token: str) -> AuthorizedRescan | None:
    """Resolve only a real human dispatch or an unedited maintainer comment.

    The protected reusable workflow and broker establish the Actions identity.
    Request identities come from the event, never caller inputs or run_attempt.
    Current GitHub permission, comment, head, and App publication are read here.
    """

    kind = os.environ.get("GITHUB_EVENT_NAME")
    if kind not in {"workflow_dispatch", "issue_comment"}:
        raise ReviewInputError(
            "explicit rescan requires a human dispatch or PR comment"
        )
    event_path = Path(os.environ.get("GITHUB_EVENT_PATH", ""))
    if not event_path.is_file() or event_path.stat().st_size > 1024 * 1024:
        raise ReviewInputError("explicit rescan event is unavailable or oversized")
    event = json.loads(event_path.read_text(encoding="utf-8"))
    if not isinstance(event, dict):
        raise ReviewInputError("explicit rescan event is invalid")
    repo = _object(event.get("repository"))
    sender = _object(event.get("sender"))
    actor = sender.get("login")
    if (
        repo.get("full_name") != args.repository
        or repo.get("id") != args.repository_id
        or sender.get("type") != "User"
        or not isinstance(actor, str)
        or re.fullmatch(r"[A-Za-z0-9-]{1,39}", actor) is None
        or os.environ.get("GITHUB_ACTOR") != actor
        or os.environ.get("GITHUB_TRIGGERING_ACTOR", actor) != actor
    ):
        raise ReviewInputError("explicit rescan event identity is unauthorized")
    http, token = ledger.http, ledger.token

    def get(suffix, *, read_token=token):
        status, payload = http.request(
            "GET", http.repository_path(args.repository, suffix), token=read_token
        )
        if status != 200 or not isinstance(payload, dict):
            raise ReviewInputError("explicit rescan authority could not be verified")
        return payload

    permission = get(f"/collaborators/{quote(actor, safe='')}/permission")
    if permission.get("permission") not in {"admin", "maintain", "write"}:
        raise ReviewInputError("explicit rescan requires current maintainer permission")
    pull = get(f"/pulls/{args.pull_request}")
    if (
        pull.get("number") != args.pull_request
        or pull.get("state") != "open"
        or _object(pull.get("head")).get("sha") != args.head_sha
        or _object(_object(pull.get("base")).get("repo")).get("id")
        != args.repository_id
    ):
        raise ReviewInputError("explicit rescan pull request is stale")
    if kind == "issue_comment":
        comment = _object(event.get("comment"))
        comment_id = comment.get("id")
        if (
            event.get("action") != "created"
            or _object(event.get("issue")).get("number") != args.pull_request
            or type(comment_id) is not int
            or comment_id <= 0
        ):
            raise ReviewInputError("explicit rescan comment identity is invalid")
        live = get(f"/issues/comments/{comment_id}")
        if (
            live.get("id") != comment_id
            or live.get("body") != comment.get("body")
            or live.get("updated_at") != comment.get("updated_at")
            or _object(live.get("user")).get("login") != actor
            or _object(live.get("user")).get("type") != "User"
            or live.get("author_association") not in {"OWNER", "MEMBER", "COLLABORATOR"}
            or not isinstance(live.get("issue_url"), str)
            or not live["issue_url"].endswith(
                f"/repos/{args.repository}/issues/{args.pull_request}"
            )
            or not isinstance(live.get("body"), str)
            or len(live["body"].encode("utf-8")) > 4096
            or not issue_comment_requests_rescan(live["body"])
        ):
            raise ReviewInputError("explicit rescan comment is unauthorized or edited")
        choose_head_sha(pull, extract_requested_commit(live["body"]))
        request_id = comment_id
        requested_at = live.get("created_at")
    else:
        inputs = _object(event.get("inputs"))
        if str(inputs.get("pull_request_number")) != str(args.pull_request):
            raise ReviewInputError(
                "explicit rescan dispatch targets another pull request"
            )
        run_id = os.environ.get("GITHUB_RUN_ID", "")
        if not run_id.isascii() or not run_id.isdecimal():
            raise ReviewInputError("explicit rescan run identity is invalid")
        request_id = int(run_id)
        run = get(f"/actions/runs/{request_id}", read_token=actions_token)
        if (
            run.get("id") != request_id
            or run.get("event") != "workflow_dispatch"
            or _object(run.get("repository")).get("id") != args.repository_id
            or _object(run.get("actor")).get("login") != actor
        ):
            raise ReviewInputError("explicit rescan run identity is unauthorized")
        requested_at = run.get("created_at")
    identity = SessionIdentity(args.repository, args.pull_request, args.repository_id)
    loaded = ledger.load(identity)
    if loaded.status not in {"ok", "migrated", "missing"}:
        raise ReviewInputError("explicit rescan session is unavailable")
    if loaded.record is None:
        return None  # First review still uses ordinary admission.
    source = rescan_source(loaded.record)
    if source is None or source.head_sha != args.head_sha:
        return None  # A changed head is ordinary new work.
    if source.phase != "publication_succeeded":
        return None  # Active work and publication recovery keep their fences.
    reviews = http.paginate(
        path=http.repository_path(
            args.repository, f"/pulls/{args.pull_request}/reviews"
        ),
        token=token,
    )
    marker = review_identity_marker(
        repository_id=args.repository_id,
        pull_request=args.pull_request,
        head_sha=args.head_sha,
    )
    candidates = [
        review
        for review in reviews
        if isinstance(review, dict)
        and review.get("commit_id") == args.head_sha
        and isinstance(review.get("user"), dict)
        and review["user"].get("login") == args.app_slug
        and review["user"].get("type") == "Bot"
        and review.get("state") in PUBLISHED_REVIEW_STATES
        and isinstance(review.get("body"), str)
        and marker in review["body"]
        and type(review.get("id")) is int
    ]
    if not candidates:
        raise ReviewInputError("explicit rescan published source is unavailable")
    latest = max(candidates, key=lambda review: review["id"])
    try:
        requested_time = datetime.fromisoformat(requested_at.replace("Z", "+00:00"))
        published_time = datetime.fromisoformat(
            latest["submitted_at"].replace("Z", "+00:00")
        )
        if requested_time.tzinfo is None or published_time.tzinfo is None:
            raise ValueError("unscoped time")
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ReviewInputError(
            "explicit rescan request/publication time is unverifiable"
        ) from exc
    if requested_time <= published_time:
        return (
            None  # Old queued events and original-run retries are ordinary duplicates.
        )
    eligibility = approval_eligibility_from_body(latest["body"])
    if eligibility is None or eligibility.head_sha != args.head_sha:
        raise ReviewInputError("explicit rescan published coverage is unverifiable")
    if (
        eligibility.facts.review_status == "complete"
        and eligibility.facts.coverage_blocker is None
    ):
        return None  # An explicit event does not reopen a completed review.
    return AuthorizedRescan(kind, request_id, source, latest["id"])

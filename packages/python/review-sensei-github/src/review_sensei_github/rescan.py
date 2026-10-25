"""Authenticate explicit Actions reanalysis without expanding broker authority."""

from __future__ import annotations

import json
import os
import re
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.errors import ReviewInputError
from review_sensei.rescan import AuthorizedRescan, rescan_source
from review_sensei.session import SessionIdentity
from review_sensei_github.default_human_reader import DefaultHumanInventoryReader
from review_sensei_github.errors import GitHubHTTPTransientError
from review_sensei_github.publication import (
    PUBLISHED_REVIEW_STATES,
    approval_eligibility_from_body,
    review_identity_marker,
    review_result_digest,
)
from review_sensei_github.publication_recovery import (
    PublicationBudgetHttp,
    publication_http,
    publication_session_ledger,
)
from review_sensei_github.retained_publication_source import (
    RetainedPublishedInventorySource,
)
from review_sensei_github.trigger import (
    choose_head_sha,
    extract_requested_commit,
    issue_comment_requests_remaining,
    issue_comment_requests_rescan,
)


def _object(value):
    if not isinstance(value, dict):
        raise ReviewInputError("explicit rescan metadata is invalid")
    return value


def _dispatch_requests_rescan(inputs: dict) -> bool:
    """Return whether a workflow_dispatch payload is an explicit rescan.

    A maintainer dispatch of this review workflow is the request when it names
    the review operation and a pull request. ``rescan: false`` or any other
    operation is not a rescan. Callers that omit ``rescan`` keep today's
    dispatch contract (ADR 0080).
    """

    if "pull_request_number" not in inputs:
        return False
    if _dispatch_requests_remaining(inputs):
        return False
    requested = inputs.get("rescan")
    if requested is not None and str(requested).strip().lower() not in {
        "1",
        "true",
        "yes",
    }:
        return False
    # Dispatch UIs can send "Review" or surrounding whitespace. Fold the same
    # way as the rescan flag so only a real review operation is explicit.
    return str(inputs.get("operation", "review")).strip().lower() == "review"


def _dispatch_requests_remaining(inputs: dict) -> bool:
    return (
        "pull_request_number" in inputs
        and str(inputs.get("operation", "review")).strip().lower() == "review"
        and str(inputs.get("review_remaining", "false")).strip().lower()
        in {"1", "true", "yes"}
    )


def event_requests_remaining(event: dict, kind: str) -> bool:
    if kind == "issue_comment":
        comment = event.get("comment")
        body = comment.get("body") if isinstance(comment, dict) else None
        return isinstance(body, str) and issue_comment_requests_remaining(body)
    if kind == "workflow_dispatch":
        inputs = event.get("inputs")
        return isinstance(inputs, dict) and _dispatch_requests_remaining(inputs)
    return False


def event_requests_rescan(event: dict, kind: str) -> bool:
    """Return whether this event itself asked for an explicit rescan."""

    if kind == "issue_comment":
        comment = event.get("comment")
        body = comment.get("body") if isinstance(comment, dict) else None
        return isinstance(body, str) and issue_comment_requests_rescan(body)
    if kind == "workflow_dispatch":
        inputs = event.get("inputs")
        return isinstance(inputs, dict) and _dispatch_requests_rescan(inputs)
    return False


def _cli_event_requests(predicate) -> bool:
    """Workflow gate: read the Actions event file and require rescan intent."""

    kind = os.environ.get("GITHUB_EVENT_NAME")
    if kind not in {"workflow_dispatch", "issue_comment"}:
        return False
    event_path = Path(os.environ.get("GITHUB_EVENT_PATH", ""))
    if not event_path.is_file() or event_path.stat().st_size > 1024 * 1024:
        return False
    try:
        event = json.loads(event_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(event, dict):
        return False
    return predicate(event, kind)


def cli_event_requests_rescan() -> bool:
    return _cli_event_requests(event_requests_rescan)


def cli_event_requests_remaining() -> bool:
    return _cli_event_requests(event_requests_remaining)


def cli_event_review_intent() -> str:
    """Read one valid original event; never downgrade unreadable intent."""
    kind = os.environ.get("GITHUB_EVENT_NAME")
    if kind not in {"workflow_dispatch", "issue_comment"}:
        return ""
    event_path = Path(os.environ.get("GITHUB_EVENT_PATH", ""))
    try:
        if not event_path.is_file() or event_path.stat().st_size > 1024 * 1024:
            raise ReviewInputError(
                "original review intent event is unavailable or oversized"
            )
        event = json.loads(event_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ReviewInputError(
            "original review intent event could not be read"
        ) from None
    if not isinstance(event, dict):
        raise ReviewInputError("original review intent event must be an object")
    if event_requests_remaining(event, kind):
        return "--review-remaining"
    if event_requests_rescan(event, kind):
        return "--rescan"
    return ""


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
    remaining = getattr(args, "review_remaining", False)
    if bool(remaining) != event_requests_remaining(event, kind):
        raise ReviewInputError(
            "remaining review intent does not match the authenticated event"
        )
    if remaining and getattr(args, "rescan", False):
        raise ReviewInputError(
            "remaining review and full rescan are mutually exclusive"
        )
    if (
        remaining
        and kind == "workflow_dispatch"
        and str(_object(event.get("inputs")).get("rescan", "false")).strip().lower()
        in {"1", "true", "yes"}
    ):
        raise ReviewInputError("remaining review and full rescan intent are ambiguous")
    repo = _object(event.get("repository"))
    sender = _object(event.get("sender"))
    actor = sender.get("login")
    triggering_actor = os.environ.get("GITHUB_TRIGGERING_ACTOR")
    if (
        repo.get("full_name") != args.repository
        or repo.get("id") != args.repository_id
        or sender.get("type") != "User"
        or not isinstance(actor, str)
        or re.fullmatch(r"[A-Za-z0-9-]{1,39}", actor) is None
        or os.environ.get("GITHUB_ACTOR") != actor
        or not isinstance(triggering_actor, str)
        or triggering_actor != actor
    ):
        raise ReviewInputError("explicit rescan event identity is unauthorized")
    # The command may precede ordinary review admission. Establish its allowance
    # once here only when the caller has none; never refill an existing command
    # or ledger budget at the referenced-inventory read boundary. A new command
    # allowance is not installed on the caller ledger: later inference and its
    # publication phase retain their own existing lifecycle.
    budget = getattr(ledger, "evidence_budget", None)
    if budget is None:
        budget = (
            ledger.http.publication_budget
            if type(ledger.http) is PublicationBudgetHttp
            else EvidenceReadBudget()
        )
    if not isinstance(budget, EvidenceReadBudget):
        raise ReviewInputError("explicit rescan read budget is invalid")
    http, token = publication_http(ledger.http, budget), ledger.token
    ledger = publication_session_ledger(ledger, budget)

    def get(suffix, *, read_token=token):
        status, payload = http.request(
            "GET", http.repository_path(args.repository, suffix), token=read_token
        )
        if status in {429} or (isinstance(status, int) and status >= 500):
            raise GitHubHTTPTransientError(
                "explicit rescan authority is temporarily unavailable"
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
            or live.get("created_at") != live.get("updated_at")
            or _object(live.get("user")).get("login") != actor
            or _object(live.get("user")).get("type") != "User"
            or live.get("author_association") not in {"OWNER", "MEMBER", "COLLABORATOR"}
            or not isinstance(live.get("issue_url"), str)
            or not live["issue_url"].endswith(
                f"/repos/{args.repository}/issues/{args.pull_request}"
            )
            or not isinstance(live.get("body"), str)
        ):
            raise ReviewInputError("explicit rescan comment is unauthorized or edited")
        if len(live["body"].encode("utf-8")) > 4096:
            raise ReviewInputError("explicit rescan comment exceeds 4096 UTF-8 bytes")
        requested = (
            issue_comment_requests_remaining
            if remaining
            else issue_comment_requests_rescan
        )
        if not requested(live["body"]):
            raise ReviewInputError(
                "explicit rescan requires a standalone @reviewsensei rescan command"
            )
        choose_head_sha(pull, extract_requested_commit(live["body"]))
        request_id = comment_id
        requested_at = live.get("created_at")
    else:
        inputs = _object(event.get("inputs"))
        if str(inputs.get("pull_request_number")) != str(args.pull_request):
            raise ReviewInputError(
                "explicit rescan dispatch targets another pull request"
            )
        requested_dispatch = (
            _dispatch_requests_remaining if remaining else _dispatch_requests_rescan
        )
        if not requested_dispatch(inputs):
            raise ReviewInputError("explicit rescan dispatch did not request a rescan")
        run_id = os.environ.get("GITHUB_RUN_ID", "")
        if not run_id.isascii() or not run_id.isdecimal():
            raise ReviewInputError("explicit rescan run identity is invalid")
        request_id = int(run_id)
        run = get(f"/actions/runs/{request_id}", read_token=actions_token)
        # The run's head is the workflow ref (usually the default branch), not
        # the pull request head. Bind the workflow file instead of that SHA.
        workflow_path = run.get("path")
        if (
            run.get("id") != request_id
            or run.get("event") != "workflow_dispatch"
            or _object(run.get("repository")).get("id") != args.repository_id
            or _object(run.get("actor")).get("login") != actor
            or not isinstance(workflow_path, str)
            or not workflow_path.endswith("/review-sensei-review.yml")
        ):
            raise ReviewInputError("explicit rescan run identity is unauthorized")
        requested_at = run.get("created_at")
    identity = SessionIdentity(args.repository, args.pull_request, args.repository_id)
    loaded = ledger.load(identity)
    if loaded.status not in {"ok", "migrated", "missing"}:
        raise ReviewInputError("explicit rescan session is unavailable")
    if loaded.record is None:
        if remaining:
            raise ReviewInputError(
                "remaining review requires a retained published source"
            )
        return None  # First review still uses ordinary admission.
    source = rescan_source(loaded.record, args.head_sha)
    if source is None or source.head_sha != args.head_sha:
        if remaining:
            raise ReviewInputError(
                "remaining review source is missing or stale; start a normal review"
            )
        return None  # A changed head is ordinary new work.
    if source.phase != "publication_succeeded":
        if remaining and not (
            source.phase in {"publication_pending", "publication_failed"}
            and loaded.record.rescan_state is not None
            and any(
                item.get("mode") == "remaining"
                and item["reservation"] == source.reservation_id
                for item in loaded.record.rescan_state["receipts"]
            )
        ):
            raise ReviewInputError(
                "remaining review source is not a completed publication"
            )
        return None  # Active work and publication recovery keep their fences.
    if remaining and _object(pull.get("base")).get("sha") != source.base_sha:
        raise ReviewInputError(
            "remaining review source base changed; start a normal review"
        )
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
        requested_time = requested_time.astimezone(timezone.utc)
        published_time = published_time.astimezone(timezone.utc)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ReviewInputError(
            "explicit rescan request/publication time is unverifiable"
        ) from exc
    if requested_time <= published_time:
        if remaining:
            raise ReviewInputError(
                "remaining review event is not newer than its source; no new window"
            )
        return (
            None  # Old queued events and original-run retries are ordinary duplicates.
        )
    retained_source = (
        RetainedPublishedInventorySource.from_checkpoint(loaded, source)
        if loaded.record.transaction is None
        else None
    )
    reader = DefaultHumanInventoryReader(
        http=http,
        ledger=ledger,
        token=token,
        repository=args.repository,
        pull_request=args.pull_request,
        head_sha=args.head_sha,
        app_slug=args.app_slug,
        budget=budget,
        _retained_source=retained_source,
    )
    eligibility = approval_eligibility_from_body(
        latest["body"], human_inventory_reader=reader
    )
    if eligibility is None or eligibility.head_sha != args.head_sha:
        raise ReviewInputError("explicit rescan published coverage is unverifiable")
    if eligibility.human_inventory_reference is not None:
        reader.validate_root(latest, reviews)
    if (
        eligibility.facts.review_status == "complete"
        and eligibility.facts.coverage_blocker is None
    ):
        if remaining:
            raise ReviewInputError(
                "remaining review source is complete; use @reviewsensei verify"
            )
        return None  # An explicit event does not reopen a completed review.
    if remaining:
        load = getattr(ledger, "load_retained_checkpoint_result", None)
        retained = (
            load(identity, transaction=source, head_sha=args.head_sha)
            if callable(load)
            else None
        )
        if retained is None or retained.transaction is None:
            raise ReviewInputError(
                "remaining review requires the authenticated retained source result"
            )
        if (
            not retained.transaction.logical_identity_matches(source)
            or retained.content_digest() != source.result_sha256
            or eligibility.result_digest
            != review_result_digest(replace(retained, transaction=source))
        ):
            raise ReviewInputError(
                "remaining review published source does not match retained evidence"
            )
        if (
            retained.review_status != "partial"
            or retained.coverage is None
            or not retained.coverage.enumeration_complete
            or retained.continuation_scope_digest is None
            or not any(
                entry.outcome in {"budget-exhausted", "partially-reviewed"}
                for entry in (*retained.coverage.files, *retained.coverage.hunks)
            )
        ):
            raise ReviewInputError(
                "remaining review has no reusable partial scope or reviewable work; repair evidence/configuration or use the supported media finalization"
            )
    return AuthorizedRescan(
        kind,
        request_id,
        source,
        latest["id"],
        mode="remaining" if remaining else "full",
    )

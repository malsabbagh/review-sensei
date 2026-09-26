"""Resolve ReviewSensei workflow trigger metadata from GitHub events."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from typing import Any, Mapping, TextIO

_GIT_SHA_FULL = re.compile(r"^[a-f0-9]{40}$")
_GIT_SHA_PREFIX = re.compile(r"^[a-f0-9]{7,39}$")
_GIT_REF = re.compile(r"^[A-Za-z0-9._/-]+$")
_RESCAN = re.compile(r"\bre[\s-]?scan\b", re.IGNORECASE)
_COMMIT_SHA = re.compile(r"\bcommit\s+([a-f0-9]{7,40})\b", re.IGNORECASE)
_SENSEI_MENTION = "@sensei"
_SENSEI_COMMAND = re.compile(r"(?m)(?<!\S)@sensei(?=\s+)")
# Grammar and dispatch move together: every token below must be handled by
# `parse_maintainer_command` and dispatched by `apply_maintainer_command`, and
# `reenroll` is the sharp one - the ledger retires state only for an expired or
# absent marker and refuses a live, unreadable, or ambiguous record. A token
# added here without a dispatcher would parse and then be ignored.
_MAINTAINER_COMMAND = re.compile(
    r"(?:"
    r"review\s+(?:status|pause|continue|reenroll)"
    r"|verify"
    r"|(?:dismiss|defer|accept-risk)\s+[a-f0-9]{16,64}"
    r"\s+--reason\s+(?P<reason>\S.*)"
    r")\Z",
    re.IGNORECASE | re.DOTALL,
)


def _is_valid_git_ref(value: str) -> bool:
    """Return whether a GitHub ref is safe to emit as workflow output."""

    if not _GIT_REF.fullmatch(value):
        return False
    if ".." in value or value.startswith("/") or value.endswith("/") or "//" in value:
        return False
    return True


@dataclass(frozen=True)
class TriggerResolution:
    """Normalized inputs for the reusable ReviewSensei runner.

    Resolution is event-shaped only. Whether a review or a reply is allowed is
    a package decision the reusable workflow reads from configuration, so a
    caller that is not the ReviewSensei repository cannot state it here.
    """

    operation: str
    head_sha: str
    head_ref: str
    base_ref: str
    base_sha: str
    pull_request_number: str


def comment_mentions_sensei(body: str) -> bool:
    """Return whether a comment body includes the case-sensitive @sensei gate."""

    return isinstance(body, str) and _SENSEI_MENTION in body


def issue_comment_requests_rescan(body: str) -> bool:
    """Return whether a PR issue comment asks ReviewSensei to re-scan."""

    if not comment_mentions_sensei(body):
        return False
    return _RESCAN.search(body) is not None


def extract_requested_commit(body: str) -> str | None:
    """Return an optional commit token from a maintainer comment body.

    Only tokens that follow the word ``commit`` are treated as SHA requests so
    issue numbers and other hex-looking IDs in a re-scan comment are ignored.
    """

    if not isinstance(body, str):
        return None
    match = _COMMIT_SHA.search(body)
    if match is None:
        return None
    token = match.group(1).casefold()
    if _GIT_SHA_FULL.fullmatch(token) or _GIT_SHA_PREFIX.fullmatch(token):
        return token
    return None


def _is_maintainer_command(body: object) -> bool:
    """Route valid command-shaped comments without importing the package.

    The trusted trigger resolver runs before ReviewSensei dependencies are
    installed. Keep this syntax-only check stdlib-only; the full parser and
    authorization remain in ``disposition.py`` for the command workflow.
    """

    if not isinstance(body, str) or len(body.encode("utf-8")) > 4096:
        return False
    mention = _SENSEI_COMMAND.search(body)
    if mention is None:
        return False
    match = _MAINTAINER_COMMAND.fullmatch(body[mention.end() :].strip())
    if match is None:
        return False
    reason = match.group("reason")
    if reason is None:
        return True
    normalized = reason.strip().strip('"').strip("'")
    return (
        bool(normalized)
        and len(normalized.encode("utf-8")) <= 512
        and normalized.isprintable()
    )


def choose_head_sha(pull: Mapping[str, Any], requested: str | None) -> str:
    """Pick the authoritative head SHA for a pull request trigger."""

    head = pull.get("head")
    if not isinstance(head, Mapping):
        raise ValueError("pull request head metadata is unavailable")
    head_sha = head.get("sha")
    if not isinstance(head_sha, str) or not _GIT_SHA_FULL.fullmatch(head_sha):
        raise ValueError("pull request head sha is invalid")
    if not requested:
        return head_sha
    requested = requested.casefold()
    if _GIT_SHA_FULL.fullmatch(requested):
        if requested != head_sha:
            raise ValueError("requested head sha does not match the pull request head")
        return requested
    if head_sha.startswith(requested):
        return head_sha
    raise ValueError("requested commit does not match the pull request head")


def _pull_request_fields(pull: Mapping[str, Any]) -> tuple[str, str, str, str]:
    head = pull.get("head")
    base = pull.get("base")
    if not isinstance(head, Mapping) or not isinstance(base, Mapping):
        raise ValueError("pull request identity metadata is unavailable")
    head_sha = head.get("sha")
    head_ref = head.get("ref")
    base_ref = base.get("ref")
    base_sha = base.get("sha")
    if (
        not isinstance(head_sha, str)
        or not _GIT_SHA_FULL.fullmatch(head_sha)
        or not isinstance(head_ref, str)
        or not _is_valid_git_ref(head_ref)
        or not isinstance(base_ref, str)
        or not _is_valid_git_ref(base_ref)
        or not isinstance(base_sha, str)
        or not _GIT_SHA_FULL.fullmatch(base_sha)
    ):
        raise ValueError("pull request identity metadata is invalid")
    return head_sha, head_ref, base_ref, base_sha


def _pull_request_number(pull: Mapping[str, Any]) -> str:
    number = pull.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise ValueError("pull request number is invalid")
    return str(number)


def _resolution_from_pull(
    pull: Mapping[str, Any],
    *,
    operation: str,
    head_sha: str | None = None,
) -> TriggerResolution:
    resolved_head, head_ref, base_ref, base_sha = _pull_request_fields(pull)
    return TriggerResolution(
        operation=operation,
        head_sha=resolved_head if head_sha is None else head_sha,
        head_ref=head_ref,
        base_ref=base_ref,
        base_sha=base_sha,
        pull_request_number=_pull_request_number(pull),
    )


def resolve_issue_comment(
    body: str,
    pull: Mapping[str, Any],
) -> TriggerResolution:
    """Resolve a PR issue comment into review or mention-reply operation inputs."""

    if issue_comment_requests_rescan(body):
        requested = extract_requested_commit(body)
        return _resolution_from_pull(
            pull,
            operation="review",
            head_sha=choose_head_sha(pull, requested),
        )
    if _is_maintainer_command(body):
        return _resolution_from_pull(pull, operation="command")
    return _resolution_from_pull(pull, operation="reply")


def resolve_pull_request_event(pull: Mapping[str, Any]) -> TriggerResolution:
    """Resolve a pull_request webhook event."""

    return _resolution_from_pull(pull, operation="review")


def resolve_review_comment_event(
    body: str,
    pull: Mapping[str, Any],
) -> TriggerResolution:
    """Resolve an inline review comment mention into mention-reply inputs."""

    return _resolution_from_pull(pull, operation="reply")


def _write_github_output_value(handle: TextIO, name: str, value: str) -> None:
    """Write one GitHub Actions output.

    Every field is single-line by construction - the operation is one of three
    literals, both SHAs are 40 hex characters, both refs are matched against
    the git ref grammar, and the number is decimal digits - so the
    multi-line heredoc form has nothing to escape here.
    """

    handle.write(f"{name}={value}\n")


def write_github_output(resolution: TriggerResolution) -> None:
    """Append workflow outputs for a trigger resolution."""

    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        raise RuntimeError("GITHUB_OUTPUT is unavailable")
    with open(output_path, "a", encoding="utf-8") as handle:
        _write_github_output_value(handle, "operation", resolution.operation)
        _write_github_output_value(handle, "head_sha", resolution.head_sha)
        _write_github_output_value(handle, "head_ref", resolution.head_ref)
        _write_github_output_value(handle, "base_ref", resolution.base_ref)
        _write_github_output_value(handle, "base_sha", resolution.base_sha)
        _write_github_output_value(
            handle, "pull_request_number", resolution.pull_request_number
        )


def _load_pull_json(path: str) -> Mapping[str, Any]:
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("pull request metadata must be a JSON object")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Resolve ReviewSensei workflow trigger metadata."
    )
    parser.add_argument("--event", required=True)
    parser.add_argument("--comment-body", default="")
    parser.add_argument("--pull-json", required=True)
    args = parser.parse_args(argv)
    pull = _load_pull_json(args.pull_json)
    event = args.event.strip()
    if event == "issue_comment":
        resolution = resolve_issue_comment(args.comment_body, pull)
    elif event == "pull_request":
        resolution = resolve_pull_request_event(pull)
    elif event == "pull_request_review_comment":
        resolution = resolve_review_comment_event(args.comment_body, pull)
    elif event == "workflow_dispatch":
        resolution = _resolution_from_pull(pull, operation="review")
    else:
        raise SystemExit(f"unsupported event: {event}")
    write_github_output(resolution)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        raise SystemExit(1) from exc

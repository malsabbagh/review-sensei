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


def _is_valid_git_ref(value: str) -> bool:
    """Return whether a GitHub ref is safe to emit as workflow output."""

    if not _GIT_REF.fullmatch(value):
        return False
    if ".." in value or value.startswith("/") or value.endswith("/") or "//" in value:
        return False
    return True


@dataclass(frozen=True)
class TriggerResolution:
    """Normalized inputs for the reusable ReviewSensei runner."""

    operation: str
    head_sha: str
    head_ref: str
    base_ref: str
    base_sha: str
    pull_request_number: str
    pull_request_title: str
    enable_review: str


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


def _pull_request_fields(pull: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
    head = pull.get("head")
    base = pull.get("base")
    if not isinstance(head, Mapping) or not isinstance(base, Mapping):
        raise ValueError("pull request identity metadata is unavailable")
    head_sha = head.get("sha")
    head_ref = head.get("ref")
    base_ref = base.get("ref")
    base_sha = base.get("sha")
    title = pull.get("title")
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
    return head_sha, head_ref, base_ref, base_sha, _normalize_pull_request_title(title)


def _normalize_pull_request_title(title: object) -> str:
    """Collapse PR title whitespace, including Unicode line separators."""

    if not isinstance(title, str):
        return ""
    return " ".join(title.split())


def _pull_request_number(pull: Mapping[str, Any]) -> str:
    number = pull.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise ValueError("pull request number is invalid")
    return str(number)


def _resolution_from_pull(
    pull: Mapping[str, Any],
    *,
    operation: str,
    enable_review: str,
    head_sha: str | None = None,
) -> TriggerResolution:
    resolved_head, head_ref, base_ref, base_sha, title = _pull_request_fields(pull)
    return TriggerResolution(
        operation=operation,
        head_sha=resolved_head if head_sha is None else head_sha,
        head_ref=head_ref,
        base_ref=base_ref,
        base_sha=base_sha,
        pull_request_number=_pull_request_number(pull),
        pull_request_title=title,
        enable_review="true" if enable_review == "true" else "false",
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
            enable_review="true",
            head_sha=choose_head_sha(pull, requested),
        )
    return _resolution_from_pull(pull, operation="reply", enable_review="false")


def resolve_pull_request_event(
    pull: Mapping[str, Any],
    *,
    auto_review: str,
) -> TriggerResolution:
    """Resolve a pull_request webhook event."""

    return _resolution_from_pull(pull, operation="review", enable_review=auto_review)


def resolve_review_comment_event(
    body: str,
    pull: Mapping[str, Any],
) -> TriggerResolution:
    """Resolve an inline review comment mention into mention-reply inputs."""

    return _resolution_from_pull(pull, operation="reply", enable_review="false")


def _write_github_output_value(
    handle: TextIO,
    name: str,
    value: str,
    *,
    multiline: bool = False,
) -> None:
    """Write one GitHub Actions output, using a heredoc when needed."""

    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    if multiline or "\n" in normalized:
        delimiter = f"RS_{name.upper()}"
        while delimiter in normalized:
            delimiter += "_EOF"
        handle.write(f"{name}<<{delimiter}\n{normalized}\n{delimiter}\n")
        return
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
        _write_github_output_value(
            handle,
            "pull_request_title",
            resolution.pull_request_title,
            multiline=True,
        )
        _write_github_output_value(handle, "enable_review", resolution.enable_review)


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
    parser.add_argument("--auto-review", default="false")
    args = parser.parse_args(argv)
    pull = _load_pull_json(args.pull_json)
    event = args.event.strip()
    if event == "issue_comment":
        resolution = resolve_issue_comment(args.comment_body, pull)
    elif event == "pull_request":
        resolution = resolve_pull_request_event(pull, auto_review=args.auto_review)
    elif event == "pull_request_review_comment":
        resolution = resolve_review_comment_event(args.comment_body, pull)
    elif event == "workflow_dispatch":
        resolution = _resolution_from_pull(
            pull, operation="review", enable_review="true"
        )
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

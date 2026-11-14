"""Pure negative read filters for a source-bound stopped drift route.

This module issues no source receipt, budget, transport, or dispatch permission.
The caller must authenticate and freeze the scope, original defining-module
query, check receipt, and uniquely matched owned review before using these
values. Constructing or copying these dataclasses proves none of those facts.
Every accepted response still needs its original source/native validation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from review_sensei.errors import ReviewInputError


def read_limits() -> dict[str, int]:
    """Return detached accounting data, never a permission to make a read."""
    return {
        "checks": 3,
        "check": 1,
        "pr": 2,
        "threads": 4,
        "reviews": 1,
        "inline_comment": 1,
        "review": 1,
        "review_comments": 1,
    }


def _number(value: object) -> bool:
    return type(value) is int and 0 < value <= 9_223_372_036_854_775_807


@dataclass(frozen=True)
class DriftReadScope:
    """Already-bound expected values, NOT an authenticated scope or receipt.

    ``thread_query`` must come from the caller's original defining-module
    capture, not a late lookup of mutable publication globals. ``review_id``
    and its expected inline count are usable only after the caller proves the
    unique owned pending review; neither field authenticates a supplied ID.
    """

    repository: str
    pull_request: int
    head_sha: str
    thread_query: str
    inline_comment_ids: frozenset[int] = frozenset()
    check_run_id: int | None = None
    review_id: int | None = None
    review_comment_count: int | None = None

    def __post_init__(self) -> None:
        if (
            type(self.repository) is not str
            or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository) is None
            or any(part in (".", "..") for part in self.repository.split("/"))
            or not _number(self.pull_request)
            or type(self.head_sha) is not str
            or re.fullmatch(r"[a-f0-9]{40}", self.head_sha) is None
            or type(self.thread_query) is not str
            or not self.thread_query.lstrip().startswith("query ReviewThreads(")
            or re.findall(r"\b(?:query|mutation|subscription)\b", self.thread_query)
            != ["query"]
            or type(self.inline_comment_ids) is not frozenset
            or any(not _number(value) for value in self.inline_comment_ids)
            or (self.check_run_id is not None and not _number(self.check_run_id))
            or (self.review_id is not None and not _number(self.review_id))
            or (
                self.review_comment_count is not None
                and (
                    type(self.review_comment_count) is not int
                    or not 0 <= self.review_comment_count < 100
                    or self.review_id is None
                )
            )
        ):
            raise ReviewInputError("ordinary drift expected read scope is invalid")


@dataclass(frozen=True)
class DriftReadTarget:
    """Classification data only; it does not authenticate any native object."""

    role: str
    identifier: int | None = None
    comment_count: int | None = None

    def __post_init__(self) -> None:
        if (
            type(self.role) is not str
            or self.role not in read_limits()
            or (self.identifier is not None and not _number(self.identifier))
            or (
                self.comment_count is not None
                and (
                    type(self.comment_count) is not int
                    or not 0 <= self.comment_count < 100
                )
            )
        ):
            raise ReviewInputError("ordinary drift read classification is invalid")


def classify_read(
    scope: DriftReadScope, method: str, path: str, body: Any = None
) -> DriftReadTarget:
    """Reject everything outside the caller's already-bound exact read shape."""
    if (
        type(scope) is not DriftReadScope
        or type(path) is not str
        or type(method) is not str
    ):
        raise ReviewInputError("ordinary drift read scope is unsupported")
    # Revalidate values as a negative filter, not as source authenticity.
    DriftReadScope.__post_init__(scope)
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or "#" in path or path.endswith("?"):
        raise ReviewInputError("ordinary drift read destination changed")
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    query = dict(pairs)
    if len(query) != len(pairs) or urlencode(pairs) != parsed.query:
        raise ReviewInputError("ordinary drift read query is ambiguous")
    prefix = f"/repos/{scope.repository}"
    if method == "POST" and path == "/graphql":
        owner, name = scope.repository.split("/")
        variables = body.get("variables") if type(body) is dict else None
        if (
            type(body) is not dict
            or any(type(key) is not str for key in body)
            or set(body) != {"operationName", "query", "variables"}
            or type(body["operationName"]) is not str
            or body["operationName"] != "ReviewThreads"
            or type(body["query"]) is not str
            or body["query"] != scope.thread_query
            or type(variables) is not dict
            or any(type(key) is not str for key in variables)
            or type(variables.get("owner")) is not str
            or type(variables.get("name")) is not str
            or variables.get("after") is not None
            or type(variables.get("number")) is not int
            or type(variables.get("first")) is not int
            or variables
            != {
                "owner": owner,
                "name": name,
                "number": scope.pull_request,
                "after": None,
                "first": 100,
            }
        ):
            raise ReviewInputError("ordinary drift thread read source changed")
        return DriftReadTarget("threads")
    if method != "GET" or body is not None:
        raise ReviewInputError("ordinary drift request is not a qualified read")
    if parsed.path == f"{prefix}/commits/{scope.head_sha}/check-runs":
        if query not in (
            {"check_name": "ReviewSensei", "per_page": "100"},
            {"check_name": "ReviewSensei", "per_page": "100", "page": "1"},
        ):
            raise ReviewInputError("ordinary drift check census is not the first page")
        return DriftReadTarget("checks")
    if parsed.path == f"{prefix}/pulls/{scope.pull_request}/reviews":
        if query != {"per_page": "100", "page": "1"}:
            raise ReviewInputError("ordinary drift review census is not the first page")
        return DriftReadTarget("reviews")
    if (
        scope.review_id is not None
        and scope.review_comment_count is not None
        and parsed.path
        == f"{prefix}/pulls/{scope.pull_request}/reviews/{scope.review_id}/comments"
    ):
        if query != {"per_page": "100", "page": "1"}:
            raise ReviewInputError("ordinary drift inline census is not the first page")
        return DriftReadTarget(
            "review_comments", scope.review_id, scope.review_comment_count
        )
    if query or parsed.query:
        raise ReviewInputError("ordinary drift object read has unexpected parameters")
    if parsed.path == f"{prefix}/pulls/{scope.pull_request}":
        return DriftReadTarget("pr", scope.pull_request)
    if (
        scope.check_run_id is not None
        and parsed.path == f"{prefix}/check-runs/{scope.check_run_id}"
    ):
        return DriftReadTarget("check", scope.check_run_id)
    if (
        scope.review_id is not None
        and parsed.path
        == f"{prefix}/pulls/{scope.pull_request}/reviews/{scope.review_id}"
    ):
        return DriftReadTarget("review", scope.review_id)
    for comment_id in scope.inline_comment_ids:
        if parsed.path == f"{prefix}/pulls/comments/{comment_id}":
            return DriftReadTarget("inline_comment", comment_id)
    raise ReviewInputError("ordinary drift read target is outside its expected scope")


def _complete_threads(value: Any) -> bool:
    if type(value) is not dict or value.get("errors") not in (None, []):
        return False
    threads = value
    for key in ("data", "repository", "pullRequest", "reviewThreads"):
        if type(threads) is not dict or key not in threads:
            return False
        threads = threads[key]
    if type(threads) is not dict:
        return False
    nodes, page = threads.get("nodes"), threads.get("pageInfo")
    if (
        type(threads) is not dict
        or type(nodes) is not list
        or len(nodes) > 100
        or type(page) is not dict
        or page.get("hasNextPage") is not False
        or "endCursor" not in page
        or (page["endCursor"] is not None and type(page["endCursor"]) is not str)
    ):
        return False
    for node in nodes:
        if type(node) is not dict or type(node.get("comments")) is not dict:
            return False
        roots = node["comments"].get("nodes")
        if type(roots) is not list or len(roots) > 1:
            return False
    pending = [value]
    seen: set[int] = set()
    while pending:
        item = pending.pop()
        if type(item) in (dict, list):
            if id(item) in seen:
                return False
            seen.add(id(item))
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                return False
            if "pageInfo" in item:
                page = item["pageInfo"]
                if (
                    type(page) is not dict
                    or page.get("hasNextPage") is not False
                    or (
                        "hasPreviousPage" in page
                        and page["hasPreviousPage"] is not False
                    )
                ):
                    return False
            pending.extend(item.values())
        elif type(item) is list:
            pending.extend(item)
        elif type(item) not in (str, int, float, bool, type(None)):
            return False
    return True


def require_complete_read(target: DriftReadTarget, status: int, value: Any) -> None:
    """Fail closed before any check-warning fallback or paginated continuation.

    Completeness is not ownership or criterion proof. The original journal and
    publication parsers must still validate every accepted response.
    """
    if type(target) is not DriftReadTarget or type(status) is not int or status != 200:
        raise ReviewInputError("ordinary drift bounded read failed")
    DriftReadTarget.__post_init__(target)
    role = target.role
    if role == "checks":
        runs = value.get("check_runs") if type(value) is dict else None
        complete = (
            type(runs) is list
            and len(runs) < 100
            and type(value.get("total_count")) is int
            and value["total_count"] == len(runs)
        )
    elif role in {"reviews", "review_comments"}:
        complete = type(value) is list and len(value) < 100
        if role == "review_comments":
            complete = (
                complete
                and type(target.comment_count) is int
                and len(value) == target.comment_count
            )
    elif role == "threads":
        complete = _complete_threads(value)
    elif role in {"pr", "check", "review", "inline_comment"}:
        field = "number" if role == "pr" else "id"
        complete = (
            _number(target.identifier)
            and type(value) is dict
            and type(value.get(field)) is int
            and value[field] == target.identifier
        )
    else:
        complete = False
    if not complete:
        raise ReviewInputError("ordinary drift bounded read is incomplete")


from review_sensei.source_registry import seal_module as _seal_source_module  # noqa: E402

_seal_source_module()

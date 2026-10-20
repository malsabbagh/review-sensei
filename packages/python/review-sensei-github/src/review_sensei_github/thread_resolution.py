"""Bounded, scoped reconciliation of durable finding decisions and App threads."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from review_sensei.errors import ReviewInputError

from .errors import GitHubHTTPError, GitHubPublicationError
from .http import BoundedGraphQLPages, GitHubHttp

RESOLUTION_PREFIX = "<!-- reviewsensei:resolution:v2 "
_RESOLUTION_RE = re.compile(re.escape(RESOLUTION_PREFIX) + r"([A-Za-z0-9_-]+) -->")
_QUERY = """query FindingResolutionThreads($owner: String!, $name: String!, $number: Int!, $after: String, $first: Int!) {
 repository(owner: $owner, name: $name) { pullRequest(number: $number) {
 reviewThreads(first: $first, after: $after) { nodes { id isResolved comments(first: 1) {
 nodes { databaseId body author { login __typename } } } } pageInfo { hasNextPage endCursor } }
 } } }"""
_MUTATION = """mutation ResolveReviewThread($input: ResolveReviewThreadInput!) {
 resolveReviewThread(input: $input) { thread { id isResolved } } }"""


def graphql_app_author(author: object, app_slug: str) -> bool:
    """Match one configured App across REST and native GraphQL Bot spelling.

    Unsuffixed aliases require GraphQL's Bot discriminator. Existing exact
    REST-login fixtures can omit it; an explicit different actor type refuses.
    Native REST root ownership is independently rechecked before mutations.
    """
    if not isinstance(author, dict) or not isinstance(author.get("login"), str):
        return False
    login = author["login"].casefold()
    slug = app_slug.casefold()
    kind = author.get("__typename")
    exact = login == slug
    native_alias = slug.endswith("[bot]") and login == slug[:-5]
    if not exact and not native_alias:
        return False
    if exact and kind is None:
        return True
    if not isinstance(kind, str) or not kind:
        # A possibly owned root with unreadable requested actor metadata must
        # hold discovery/finalization, rather than disappear as a foreign root.
        raise GitHubPublicationError("review thread App actor identity is unreadable")
    return kind == "Bot"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def resolution_marker(record: Mapping[str, Any]) -> str:
    encoded = (
        base64.urlsafe_b64encode(
            json.dumps(dict(record), sort_keys=True, separators=(",", ":")).encode()
        )
        .decode()
        .rstrip("=")
    )
    return f"{RESOLUTION_PREFIX}{encoded} -->"


def resolution_records(
    reviews: Sequence[Any], app_slug: str
) -> tuple[dict[str, Any], ...]:
    records = []
    fields = {
        "repo",
        "pr",
        "head",
        "base",
        "result",
        "instance",
        "root",
        "source",
        "updated",
        "source_digest",
        "actor",
        "evidence",
    }
    for review in reviews:
        if not isinstance(review, dict):
            continue
        user, body = review.get("user"), review.get("body")
        if (
            not isinstance(user, dict)
            or str(user.get("login", "")).casefold() != app_slug.casefold()
        ):
            continue
        if not isinstance(body, str) or RESOLUTION_PREFIX not in body:
            continue
        matches = _RESOLUTION_RE.findall(body)
        if len(matches) != 1 or body.count(RESOLUTION_PREFIX) != 1:
            raise GitHubPublicationError("resolution authority is invalid")
        try:
            raw = base64.urlsafe_b64decode(matches[0] + "=" * (-len(matches[0]) % 4))
            record = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise GitHubPublicationError("resolution authority is invalid") from exc
        if not isinstance(record, dict) or set(record) != fields:
            raise GitHubPublicationError("resolution authority is invalid")
        for key in ("repo", "pr", "root", "source"):
            if type(record[key]) is not int or record[key] < 1:
                raise GitHubPublicationError("resolution authority is invalid")
        for key, length in (
            ("head", 40),
            ("base", 40),
            ("result", 64),
            ("instance", 64),
            ("source_digest", 64),
            ("evidence", 64),
        ):
            if (
                not isinstance(record[key], str)
                or re.fullmatch(f"[a-f0-9]{{{length}}}", record[key]) is None
            ):
                raise GitHubPublicationError("resolution authority is invalid")
        if (
            any(
                not isinstance(record[key], str) or not record[key]
                for key in ("actor", "updated")
            )
            or review.get("commit_id") != record["head"]
        ):
            raise GitHubPublicationError("resolution authority is invalid")
        records.append(record)
    return tuple(records)


@dataclass(frozen=True)
class ThreadResolutionResult:
    resolved: tuple[str, ...] = ()
    unconfirmed: tuple[str, ...] = ()

    @property
    def diagnostic(self) -> str | None:
        return "thread_resolution_unconfirmed" if self.unconfirmed else None


class FindingThreadResolver:
    def __init__(self, http: GitHubHttp) -> None:
        self.http = http

    def source_valid(
        self, *, token: str, repository: str, record: Mapping[str, Any], app_slug: str
    ) -> bool:
        from .conversation import (
            authorized_human_comment,
            has_standalone_sensei_mention,
        )

        status, source = self.http.request(
            "GET",
            self.http.repository_path(
                repository, f"/pulls/comments/{record['source']}"
            ),
            token=token,
        )
        return bool(
            status == 200
            and isinstance(source, dict)
            and source.get("id") == record["source"]
            and source.get("pull_request_url")
            == self.http.api_url
            + self.http.repository_path(repository, f"/pulls/{record['pr']}")
            and source.get("in_reply_to_id") == record["root"]
            and source.get("updated_at") == record["updated"]
            and isinstance(source.get("body"), str)
            and digest(source["body"]) == record["source_digest"]
            and isinstance(source.get("user"), dict)
            and source["user"].get("login") == record["actor"]
            and authorized_human_comment(source, app_slug=app_slug)
            and has_standalone_sensei_mention(source["body"])
        )

    def _resolve_confirmed(self, *, token: str, thread_id: str) -> bool:
        """Reconcile an ambiguous response once, without repeating a mutation."""
        try:
            status, payload = self.http.request(
                "POST",
                "/graphql",
                token=token,
                body={
                    "operationName": "ResolveReviewThread",
                    "query": _MUTATION,
                    "variables": {"input": {"threadId": thread_id}},
                },
            )
            if (
                status == 200
                and isinstance(payload, dict)
                and payload.get("errors") in (None, [])
                and payload.get("data", {}).get("resolveReviewThread", {}).get("thread")
                == {"id": thread_id, "isResolved": True}
            ):
                return True
        except (GitHubHTTPError, AttributeError, TypeError):
            pass
        try:
            status, payload = self.http.request(
                "POST",
                "/graphql",
                token=token,
                body={
                    "operationName": "ResolutionReadback",
                    "query": "query ResolutionReadback($id: ID!) { node(id: $id) { ... on PullRequestReviewThread { id isResolved } } }",
                    "variables": {"id": thread_id},
                },
            )
            return bool(
                status == 200
                and isinstance(payload, dict)
                and payload.get("errors") in (None, [])
                and payload.get("data", {}).get("node")
                == {"id": thread_id, "isResolved": True}
            )
        except (GitHubHTTPError, AttributeError, TypeError):
            return False

    def resolve(
        self,
        *,
        token: str,
        repository: str,
        repository_id: int,
        pull_request: int,
        head_sha: str,
        base_sha: str,
        app_slug: str,
        instances: frozenset[str],
        fence: Callable[[], bool],
        records: Sequence[Mapping[str, Any]] = (),
        supersede: Mapping[str, str] | None = None,
        skip_resolved: bool = False,
    ) -> ThreadResolutionResult:
        from .publication import (
            finding_fingerprint_from_body,
            finding_instance_from_body,
            finding_publication_scope,
        )

        if not instances and not supersede:
            return ThreadResolutionResult()
        owner, name = repository.split("/", 1)
        after = None
        candidates = []
        seen_cursors: set[str] = set()
        # Complete discovery before any mutation. An incomplete scan grants nothing.
        pages = BoundedGraphQLPages(self.http, max_requests=10)
        for _ in range(10):
            status, payload = pages.request(
                "POST",
                "/graphql",
                token=token,
                body={
                    "operationName": "FindingResolutionThreads",
                    "query": _QUERY,
                    "variables": {
                        "owner": owner,
                        "name": name,
                        "number": pull_request,
                        "after": after,
                    },
                },
            )
            try:
                if (
                    status != 200
                    or not isinstance(payload, dict)
                    or payload.get("errors") not in (None, [])
                ):
                    raise ValueError
                threads = payload["data"]["repository"]["pullRequest"]["reviewThreads"]
                nodes, page = threads["nodes"], threads["pageInfo"]
                if (
                    not isinstance(nodes, list)
                    or len(nodes) > pages.page_size
                    or type(page["hasNextPage"]) is not bool
                ):
                    raise ValueError
                for node in nodes:
                    if (
                        not isinstance(node, dict)
                        or type(node.get("isResolved")) is not bool
                    ):
                        raise ValueError
                    roots = node["comments"]["nodes"]
                    if not isinstance(roots, list) or len(roots) != 1:
                        raise ValueError
                    root = roots[0]
                    author = root.get("author")
                    if not isinstance(author, dict):
                        raise ValueError
                    if not graphql_app_author(author, app_slug):
                        continue
                    scope = finding_publication_scope(root.get("body"))
                    instance = finding_instance_from_body(root.get("body"))
                    concern = finding_fingerprint_from_body(root.get("body"))
                    if scope is None or scope[:2] != (repository_id, pull_request):
                        continue
                    current = (
                        scope[2:4] == (head_sha, base_sha) and instance in instances
                    )
                    old = bool(
                        supersede
                        and node.get("id") in supersede
                        and supersede[node["id"]] == concern
                        and scope[2] != head_sha
                    )
                    if current or old:
                        if (
                            not isinstance(node.get("id"), str)
                            or not node["id"]
                            or type(root.get("databaseId")) is not int
                        ):
                            raise ValueError
                        candidates.append((node, root, instance, old))
                if not page["hasNextPage"]:
                    break
                after = page["endCursor"]
                if not isinstance(after, str) or not after or after in seen_cursors:
                    raise ValueError
                seen_cursors.add(after)
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                raise GitHubPublicationError(
                    "thread resolution lookup is unconfirmed"
                ) from exc
        else:
            raise GitHubPublicationError(
                "thread resolution pagination exceeded configured limit"
            )
        resolved, unconfirmed = [], []
        found = {instance for _, _, instance, old in candidates if not old}
        unconfirmed.extend(sorted(instances - found))
        for node, root, instance, old in candidates:
            identity = node["id"] if old else instance
            assert isinstance(identity, str)
            if skip_resolved and node["isResolved"]:
                # No write is needed. The opted-in caller has independently
                # proven the complete native report and owns final approval
                # fences; this is an observation, never a new decision grant.
                resolved.append(identity)
                continue
            try:
                # Re-read the root, decision, and PR immediately before each mutation.
                status, live_root = self.http.request(
                    "GET",
                    self.http.repository_path(
                        repository, f"/pulls/comments/{root['databaseId']}"
                    ),
                    token=token,
                )
                if (
                    status != 200
                    or not isinstance(live_root, dict)
                    or live_root.get("body") != root.get("body")
                    or live_root.get("pull_request_url")
                    != self.http.api_url
                    + self.http.repository_path(repository, f"/pulls/{pull_request}")
                    or str(live_root.get("user", {}).get("login", "")).casefold()
                    != app_slug.casefold()
                    or live_root.get("user", {}).get("type") != "Bot"
                ):
                    raise GitHubPublicationError("thread root changed")
                for record in records:
                    if record["instance"] == instance and (
                        record["root"] != root["databaseId"]
                        or not self.source_valid(
                            token=token,
                            repository=repository,
                            record=record,
                            app_slug=app_slug,
                        )
                    ):
                        raise GitHubPublicationError("resolution source changed")
                if not fence():
                    raise GitHubPublicationError("thread resolution authority changed")
                if not node["isResolved"] and not self._resolve_confirmed(
                    token=token, thread_id=node["id"]
                ):
                    raise GitHubPublicationError(
                        "thread resolution mutation is unconfirmed"
                    )
                resolved.append(identity)
            except (
                GitHubHTTPError,
                GitHubPublicationError,
                ReviewInputError,
                AttributeError,
                TypeError,
            ):
                unconfirmed.append(identity)
        return ThreadResolutionResult(tuple(resolved), tuple(unconfirmed))

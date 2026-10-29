"""Complete issue-comment bodies under a pre-existing joint allocation bound.

An allocation plan is capacity, never content or ownership authority. Opening
and closing enumerate the entire native membership. Every body is fetched in
full; no cached body, terminal hint, timestamp ordering or response prefix is
accepted as evidence. Unknown old and interrupted current allocations share
the same finite multiset and physical ten-request counter.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from review_sensei.errors import ReviewInputError

from .http import MAX_GITHUB_RESPONSE_BYTES, MAX_PAGINATION_PAGES, _read_allowance
from .quarantine_census import QUERY, _comment, _cursor, _numeric, _repository

MAX_COMMENTS = 200
NODE_METADATA_BYTES = 1024
OUTER_BYTES = 4096
_META = "fullDatabaseId createdAt updatedAt author{__typename login ... on Bot{databaseId} ... on User{databaseId}} pullRequest{number repository{databaseId nameWithOwner}}"
_CONNECTION = "totalCount pageInfo{hasNextPage endCursor} nodes{" + _META + "}"
METADATA_QUERY = (
    "query PackedCommentMembership($owner:String!,$name:String!,$number:Int!){"
    "repository(owner:$owner,name:$name){databaseId nameWithOwner "
    "pullRequest(number:$number){number headRefOid baseRefOid "
    "f:comments(first:100){" + _CONNECTION + "} "
    "l:comments(last:100){totalCount pageInfo{hasPreviousPage} nodes{" + _META + "}}}}}"
)


def wire(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode())


@dataclass(frozen=True)
class CommentAllocationPlan:
    """Per-ID retained maxima plus all possible unobserved allocation slots.

    Callers must include positive updates in the relevant retained maximum.
    The plan must originate in whole-transaction admission before effects; it
    cannot be reconstructed from a successful terminal-only discovery hint.
    """

    known: Mapping[int, int]
    unknown: tuple[int, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.known, Mapping)
            or not isinstance(self.unknown, tuple)
            or len(self.known) + len(self.unknown) > MAX_COMMENTS
            or any(type(key) is not int or key <= 0 for key in self.known)
            or any(
                type(bound) is not int
                or not 0
                < bound
                <= MAX_GITHUB_RESPONSE_BYTES - OUTER_BYTES - NODE_METADATA_BYTES
                for bound in (*self.known.values(), *self.unknown)
            )
        ):
            raise ReviewInputError("packed comment allocation plan is invalid")
        object.__setattr__(self, "known", MappingProxyType(dict(self.known)))


def is_metadata_query(query: object, variables: object) -> bool:
    from .quarantine_census import _variables

    return (
        query == METADATA_QUERY
        and isinstance(variables, dict)
        and set(variables) == {"owner", "name", "number"}
        and _variables({**variables, "first": 100, "after": None})
    )


def _membership(payload: Any, ledger: Any, identity: Any):
    try:
        repository = payload["data"]["repository"]
        _repository(repository, identity)
        pr = repository["pullRequest"]
        if pr["number"] != identity.pull_request or any(
            not isinstance(pr[key], str)
            or len(pr[key]) != 40
            or any(char not in "0123456789abcdef" for char in pr[key])
            for key in ("headRefOid", "baseRefOid")
        ):
            raise ReviewInputError("packed comment PR scope changed")
        first, last = pr["f"], pr["l"]
        count = first["totalCount"]
        if (
            type(count) is not int
            or not 0 <= count <= MAX_COMMENTS
            or last["totalCount"] != count
            or len(first["nodes"]) != min(100, count)
            or len(last["nodes"]) != min(100, count)
            or first["pageInfo"]["hasNextPage"] is not (count > 100)
            or last["pageInfo"]["hasPreviousPage"] is not (count > 100)
        ):
            raise ReviewInputError("packed comment metadata windows are incomplete")
        rows: dict[int, dict[str, Any]] = {}
        order = []
        for window in (first["nodes"], last["nodes"]):
            window_ids = set()
            for node in window:
                if wire(node) > NODE_METADATA_BYTES:
                    raise ReviewInputError("packed comment metadata exceeds its bound")
                normalized = _comment({**node, "body": ""}, ledger, identity)
                identifier = normalized["id"]
                if identifier in window_ids:
                    raise ReviewInputError(
                        "packed comment metadata duplicates a native identity"
                    )
                window_ids.add(identifier)
                if identifier in rows:
                    if rows[identifier] != node:
                        raise ReviewInputError(
                            "packed comment metadata overlap changed"
                        )
                else:
                    rows[identifier] = node
                    order.append(identifier)
        if len(rows) != count:
            raise ReviewInputError("packed comment membership is incomplete")
        # Overlap must be the actual suffix/prefix, not a permuted set.
        overlap = max(0, min(100, count) * 2 - count)
        if overlap and first["nodes"][-overlap:] != last["nodes"][:overlap]:
            raise ReviewInputError("packed comment metadata window order changed")
        return tuple(order), rows, (pr["headRefOid"], pr["baseRefOid"])
    except (KeyError, TypeError, ValueError) as error:
        raise ReviewInputError("packed comment metadata is malformed") from error


def fetch_packed_comments(
    ledger,
    identity,
    *,
    plan: CommentAllocationPlan,
    max_requests=None,
    timeout_seconds=None,
    before_read=None,
    observe_request=None,
):
    """One cold complete walk, including opening and closing membership."""
    requests = MAX_PAGINATION_PAGES if max_requests is None else max_requests
    if type(requests) is not int or not 2 <= requests <= MAX_PAGINATION_PAGES:
        raise ReviewInputError("packed comment physical request bound is invalid")
    deadline = time.monotonic() + min(
        60, 60 if timeout_seconds is None else timeout_seconds
    )
    calls = 0
    owner, name = identity.repository.split("/", 1)
    scope = {"owner": owner, "name": name, "number": identity.pull_request}

    def read(query, variables):
        nonlocal calls
        remaining = deadline - time.monotonic()
        if remaining <= 0 or calls >= requests:
            raise ReviewInputError(
                "packed comment census exceeded its physical request bound"
            )
        if before_read is not None:
            remaining = min(remaining, _read_allowance(before_read))
        if observe_request is not None:
            observe_request()
        calls += 1  # Includes errors and uncertain transport outcomes.
        status, response = ledger.http.request(
            "POST",
            "/graphql",
            token=ledger.token,
            body={"query": query, "variables": variables},
            timeout_seconds=remaining,
        )
        if (
            status != 200
            or not isinstance(response, dict)
            or response.get("errors")
            or set(response) - {"data", "errors", "extensions"}
            or wire(response) > MAX_GITHUB_RESPONSE_BYTES
            or time.monotonic() >= deadline
        ):
            raise ReviewInputError("packed comment complete response is unavailable")
        return response

    opening = read(METADATA_QUERY, scope)
    order, metadata, original_scope = _membership(opening, ledger, identity)
    unknown = sorted(plan.unknown)
    if sum(identifier not in plan.known for identifier in order) > len(unknown):
        raise ReviewInputError(
            "packed comment discovery exceeds reserved old/current allocations"
        )
    collected, cursor = [], None
    offset = 0
    while offset < len(order):
        first = 0
        for count in range(1, min(100, len(order) - offset) + 1):
            identifiers = order[offset : offset + count]
            unknown_count = sum(
                identifier not in plan.known for identifier in identifiers
            )
            bound = (
                OUTER_BYTES
                + NODE_METADATA_BYTES * count
                + sum(
                    plan.known[identifier]
                    for identifier in identifiers
                    if identifier in plan.known
                )
                + (sum(unknown[-unknown_count:]) if unknown_count else 0)
            )
            if bound > MAX_GITHUB_RESPONSE_BYTES:
                break
            first = count
        if not first or calls + 1 >= requests:  # Keep closing in this same budget.
            raise ReviewInputError("packed comment bodies do not fit the admitted walk")
        response = read(QUERY, {**scope, "first": first, "after": cursor})
        try:
            repository = response["data"]["repository"]
            _repository(repository, identity)
            pr = repository["pullRequest"]
            connection = pr["comments"]
            nodes, info = connection["nodes"], connection["pageInfo"]
            if (
                pr["number"] != identity.pull_request
                or connection["totalCount"] != len(order)
                or not isinstance(nodes, list)
                or len(nodes) != first
                or info["hasNextPage"] is not (offset + first < len(order))
                or not _cursor(info["endCursor"])
                or wire(response) - sum(wire(node) for node in nodes) > OUTER_BYTES
            ):
                raise ReviewInputError("packed comment body membership changed")
            for identifier, node in zip(
                order[offset : offset + first], nodes, strict=True
            ):
                if (
                    _numeric(node["fullDatabaseId"]) != identifier
                    or {key: value for key, value in node.items() if key != "body"}
                    != metadata[identifier]
                ):
                    raise ReviewInputError(
                        "packed comment body/version association changed"
                    )
                body_wire = wire({"body": node["body"]})
                if identifier in plan.known:
                    if body_wire > plan.known[identifier]:
                        raise ReviewInputError(
                            "packed comment positive update exceeded its reserve"
                        )
                else:
                    match = next(
                        (
                            index
                            for index, bound in enumerate(unknown)
                            if body_wire <= bound
                        ),
                        None,
                    )
                    if match is None:
                        raise ReviewInputError(
                            "packed comment allocation exceeded its reserve"
                        )
                    unknown.pop(match)
                collected.append(_comment(node, ledger, identity))
            cursor, offset = info["endCursor"], offset + first
        except (KeyError, TypeError, ValueError) as error:
            raise ReviewInputError(
                "packed comment complete body page is malformed"
            ) from error
    closing = _membership(read(METADATA_QUERY, scope), ledger, identity)
    if closing != (order, metadata, original_scope):
        raise ReviewInputError("packed comment fresh closing membership changed")
    return collected

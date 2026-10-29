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
from dataclasses import dataclass, field
from functools import lru_cache
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
CLOSING_QUERY = (
    "query PackedCommentClosing($owner:String!,$name:String!,$number:Int!,$first:Int!,$after:String){"
    "repository(owner:$owner,name:$name){databaseId nameWithOwner "
    "pullRequest(number:$number){number headRefOid baseRefOid "
    "f:comments(first:100){" + _CONNECTION + "} "
    "l:comments(last:100){totalCount pageInfo{hasPreviousPage} nodes{" + _META + "}} "
    "comments(first:$first,after:$after){totalCount pageInfo{hasNextPage endCursor} "
    "nodes{body " + _META + "}}}}}"
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
    metadata_known: Mapping[int, int] = field(default_factory=dict)
    metadata_unknown: int = NODE_METADATA_BYTES

    def __post_init__(self) -> None:
        if (
            not isinstance(self.known, Mapping)
            or not isinstance(self.unknown, tuple)
            or not isinstance(self.metadata_known, Mapping)
            or set(self.metadata_known) - set(self.known)
            or any(
                type(value) is not int or not 0 < value <= NODE_METADATA_BYTES
                for value in (*self.metadata_known.values(), self.metadata_unknown)
            )
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
            raise ReviewInputError("packed comment allocation capacity plan is invalid")
        object.__setattr__(self, "known", MappingProxyType(dict(self.known)))
        object.__setattr__(
            self, "metadata_known", MappingProxyType(dict(self.metadata_known))
        )

    def worst_read_calls(self) -> int:
        # Each window contains at most100nodes, including overlap. Twice the
        # largest100possible node envelopes bounds both complete windows.
        metadata = sorted(
            [self.metadata_known.get(key, NODE_METADATA_BYTES) for key in self.known]
            + [self.metadata_unknown] * len(self.unknown),
            reverse=True,
        )
        return packed_request_bound(
            tuple(self.known.values()),
            self.unknown,
            closing_metadata_bytes=2 * sum(metadata[:100]),
        )


def packed_request_bound(
    known: tuple[int, ...],
    unknown: tuple[int, ...],
    *,
    closing_metadata_bytes: int | None = None,
) -> int:
    """Certify the actual prefix algorithm under every unknown interleaving.

    Known native identities retain their observed relative order. All possible
    unknown slots may interleave anywhere. Matching an observed unknown body
    removes its smallest fitting slot; removing the smallest slot regardless
    of that body leaves a componentwise greater remaining capacity multiset.
    That dominating case suffices for the maximum number of subsequent pages.

    For a page, cost depends only on how many ordered known and unknown items
    it contains. Enumerate every maximal page boundary: a next known or unknown
    item must fail to fit, unless this is the final page. Memoized boundaries
    thus cover all interleavings without treating aggregate bytes as packing.
    Opening and fresh closing are included. Excess local proof work refuses.
    """
    rejected = MAX_PAGINATION_PAGES + 1
    framing_calls = 2 if closing_metadata_bytes is None else 1
    if len(known) + len(unknown) > MAX_COMMENTS:
        return rejected
    prefix = [0]
    for bound in known:
        prefix.append(prefix[-1] + bound)
    largest = sorted(unknown, reverse=True)
    upper = [0]
    for bound in largest:
        upper.append(upper[-1] + bound)
    work = 0

    class ProofWorkExceeded(Exception):
        pass

    @lru_cache(maxsize=40_401)
    def pages(ki: int, ui: int) -> int:
        nonlocal work
        if ki == len(known) and ui == len(unknown):
            return 0 if closing_metadata_bytes is None else 1
        worst = 0
        for end_known in range(ki, len(known) + 1):
            known_cost = prefix[end_known] - prefix[ki]
            for added_unknown in range(len(unknown) - ui + 1):
                work += 1
                if work > 500_000:
                    raise ProofWorkExceeded
                count = end_known - ki + added_unknown
                if count > 100:
                    break
                cost = (
                    OUTER_BYTES
                    + NODE_METADATA_BYTES * count
                    + known_cost
                    + upper[added_unknown]
                )
                if cost > MAX_GITHUB_RESPONSE_BYTES:
                    break
                if not count:
                    continue
                end_unknown = ui + added_unknown
                terminal = end_known == len(known) and end_unknown == len(unknown)
                next_known_fails = (
                    end_known < len(known)
                    and cost + NODE_METADATA_BYTES + known[end_known]
                    > MAX_GITHUB_RESPONSE_BYTES
                )
                next_unknown_fails = (
                    end_unknown < len(unknown)
                    and cost + NODE_METADATA_BYTES + largest[added_unknown]
                    > MAX_GITHUB_RESPONSE_BYTES
                )
                if terminal or count == 100 or next_known_fails or next_unknown_fails:
                    finish = (
                        1
                        + int(cost + closing_metadata_bytes > MAX_GITHUB_RESPONSE_BYTES)
                        if terminal and closing_metadata_bytes is not None
                        else 1 + pages(end_known, end_unknown)
                    )
                    worst = max(worst, finish)
                    if worst + framing_calls > MAX_PAGINATION_PAGES:
                        return rejected
        return worst if worst else rejected

    try:
        return min(rejected, framing_calls + pages(0, 0))
    except ProofWorkExceeded:
        return rejected


def is_metadata_query(query: object, variables: object) -> bool:
    from .quarantine_census import _variables

    return (
        query == METADATA_QUERY
        and isinstance(variables, dict)
        and set(variables) == {"owner", "name", "number"}
        and _variables({**variables, "first": 100, "after": None})
    )


def is_closing_query(query: object, variables: object) -> bool:
    from .quarantine_census import _variables

    return query == CLOSING_QUERY and _variables(variables)


def _membership(payload: Any, ledger: Any, identity: Any, plan: CommentAllocationPlan):
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
                normalized = _comment({**node, "body": ""}, ledger, identity)
                identifier = normalized["id"]
                bound = (
                    plan.metadata_known.get(identifier, NODE_METADATA_BYTES)
                    if identifier in plan.known
                    else plan.metadata_unknown
                )
                if wire(node) > bound:
                    raise ReviewInputError("packed comment metadata exceeds its bound")
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
    order, metadata, original_scope = _membership(opening, ledger, identity, plan)
    # A closing frame must repeat these exact metadata windows. Co-fetching
    # the final complete body page is optional and cannot increase the older
    # separate-closing certificate's physical request count.
    windows = opening["data"]["repository"]["pullRequest"]
    closing_metadata_wire = sum(
        wire(node) for name in ("f", "l") for node in windows[name]["nodes"]
    )
    if tuple(identifier for identifier in order if identifier in plan.known) != tuple(
        plan.known
    ):
        raise ReviewInputError("packed comment retained identity order changed")
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
        if not first:
            raise ReviewInputError("packed comment bodies do not fit the admitted walk")
        combined_closing = (
            offset + first == len(order)
            and bound + closing_metadata_wire <= MAX_GITHUB_RESPONSE_BYTES
        )
        if calls + int(not combined_closing) >= requests:
            raise ReviewInputError("packed comment bodies do not fit the admitted walk")
        response = read(
            CLOSING_QUERY if combined_closing else QUERY,
            {**scope, "first": first, "after": cursor},
        )
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
                or wire(response)
                - sum(wire(node) for node in nodes)
                - (closing_metadata_wire if combined_closing else 0)
                > OUTER_BYTES
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
            if combined_closing:
                closing = _membership(response, ledger, identity, plan)
                if closing != (order, metadata, original_scope):
                    raise ReviewInputError(
                        "packed comment fresh closing membership changed"
                    )
                return collected
        except (KeyError, TypeError, ValueError) as error:
            raise ReviewInputError(
                "packed comment complete body page is malformed"
            ) from error
    closing = _membership(read(METADATA_QUERY, scope), ledger, identity, plan)
    if closing != (order, metadata, original_scope):
        raise ReviewInputError("packed comment fresh closing membership changed")
    return collected

"""One complete publication walk: opening, bounded bodies, fresh closing.

Numeric root/parent identities carry membership. Opaque mutation target IDs are
an independently bound immutable association, never repeated per body packet.
Every mutable resolution and body/version fence is read in this same walk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from review_sensei.errors import ReviewInputError

from .native_review_groups import (
    MIDDLE_METADATA_BYTES,
    PART_QUERY,
    NativeGroupEvidence,
    NativeReviewPlan,
    NativeWalkBudget,
    _native_number,
    read_complete_group,
    verify_complete_group_response,
    wire_bytes,
)

ROOT_METADATA_BYTES = 336
REVIEW_METADATA_BYTES = 2560
CLOSING_REVIEW_METADATA_BYTES = 64
MAX_REVIEWS = 200
ROOT_OPEN_METADATA_BYTES = 80 * 1024
MAX_EXISTING_ROOTS = 50
MAX_ROOTS = 300
MAX_EXISTING_ROOT_PROJECTION_BYTES = 96 * 1024
MAX_EXISTING_ROOT_IDS_BYTES = 1536

_SCOPE = "n:number h:headRefOid b:baseRefOid"
_ROOT = "s:isResolved d:diffSide t:subjectType x:comments(first:1){totalCount nodes{n:fullDatabaseId r:pullRequestReview{n:fullDatabaseId} a:author{t:__typename ... on Bot{n:databaseId}} c:commit{o:oid} q:replyTo{n:fullDatabaseId} u:updatedAt}}"
_FIRST = (
    "f:reviewThreads(first:100){totalCount pageInfo{hasNextPage endCursor} nodes{"
    + _ROOT
    + "}}"
)
_LAST = (
    "l:reviewThreads(last:100){totalCount pageInfo{hasPreviousPage} nodes{"
    + _ROOT
    + "}}"
)
_REVIEW = "i:id n:fullDatabaseId s:state a:author{t:__typename ... on Bot{n:databaseId}} c:commit{o:oid} x:comments(first:1){totalCount nodes{n:fullDatabaseId q:replyTo{n:fullDatabaseId}}}"
_REVIEW_FIRST = (
    "r:reviews(first:100){totalCount pageInfo{hasNextPage} nodes{" + _REVIEW + "}}"
)
_REVIEW_LAST = (
    "v:reviews(last:100){totalCount pageInfo{hasPreviousPage} nodes{" + _REVIEW + "}}"
)
_REPOSITORY_SCOPE = (
    "repository(owner:$owner,name:$name){n:databaseId p:pullRequest(number:$number){"
    + _SCOPE
)
_OPEN = _REPOSITORY_SCOPE + _REVIEW_FIRST + _REVIEW_LAST + "}}"
OPEN_QUERY = (
    "query NativePublicationOpening($owner:String!,$name:String!,$number:Int!){"
    + _OPEN
    + "}"
)
_ROOT_OPEN = _REPOSITORY_SCOPE + _FIRST + _LAST + "}}"
ROOT_OPEN_QUERY = (
    "query NativeRootOpening($owner:String!,$name:String!,$number:Int!){"
    + _ROOT_OPEN
    + "}"
)
_MIDDLE = (
    "repository(owner:$owner,name:$name){n:databaseId p:pullRequest(number:$number){"
    + _SCOPE
    + " m:reviewThreads(first:$first,after:$after){totalCount pageInfo{hasNextPage} nodes{"
    + _ROOT
    + "}}}}"
)
MIDDLE_QUERY = (
    "query NativePublicationMiddle($owner:String!,$name:String!,$number:Int!,$first:Int!,$after:String!){"
    + _MIDDLE
    + "}"
)
_PART_SELECTION = PART_QUERY.partition("{")[2].rsplit("}", 1)[0]
PART_ROOTS_QUERY = (
    "query NativeReviewPartWithOpening($id:ID!,$owner:String!,$name:String!,$number:Int!){"
    + _PART_SELECTION
    + _ROOT_OPEN
    + "}"
)
PART_MIDDLE_QUERY = (
    "query NativeReviewPartWithMiddle($id:ID!,$owner:String!,$name:String!,$number:Int!,$first:Int!,$after:String!){"
    + _PART_SELECTION
    + _MIDDLE
    + "}"
)
_MAIN = "main:node(id:$main){... on PullRequestReview{n:fullDatabaseId b:body s:state a:author{t:__typename ... on Bot{n:databaseId}} c:commit{o:oid} p:pullRequest{n:number r:repository{n:databaseId}} x:comments(first:1){totalCount}}}"
_OLD = "old:nodes(ids:$roots){... on PullRequestReviewComment{n:fullDatabaseId b:body p:path l:line o:originalLine a:author{t:__typename ... on Bot{n:databaseId}} c:commit{o:oid} z:originalCommit{o:oid} r:pullRequestReview{n:fullDatabaseId} q:replyTo{n:fullDatabaseId} u:updatedAt}}"
_CLOSE_SCOPE = (
    "repository(owner:$owner,name:$name){n:databaseId p:pullRequest(number:$number){"
    + _SCOPE
    + " r:reviews(first:100){totalCount pageInfo{hasNextPage} nodes{n:fullDatabaseId s:state}}v:reviews(last:100){totalCount pageInfo{hasPreviousPage} nodes{n:fullDatabaseId s:state}} f:reviewThreads(first:$front){totalCount pageInfo{hasNextPage endCursor} nodes{"
    + _ROOT
    + "}} l:reviewThreads(last:$back){totalCount pageInfo{hasPreviousPage} nodes{"
    + _ROOT
    + "}}"
)
_CLOSE_MIDDLE = (
    "m:reviewThreads(first:$first,after:$after){totalCount pageInfo{hasNextPage} nodes{"
    + _ROOT
    + "}}"
)
_CLOSE_VARS = "$owner:String!,$name:String!,$number:Int!,$front:Int!,$back:Int!,$main:ID!,$roots:[ID!]!"
CLOSE_QUERY = (
    "query NativePublicationClosing("
    + _CLOSE_VARS
    + "){"
    + _CLOSE_SCOPE
    + "}}"
    + _MAIN
    + _OLD
    + "}"
)
CLOSE_MIDDLE_QUERY = (
    "query NativePublicationClosingWithMiddle("
    + _CLOSE_VARS
    + ",$first:Int!,$after:String!){"
    + _CLOSE_SCOPE
    + _CLOSE_MIDDLE
    + "}}"
    + _MAIN
    + _OLD
    + "}"
)


def _opaque(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 2048
        and all(33 <= ord(char) <= 126 for char in value)
        and wire_bytes(value) <= 2050
    )


def source_query_variables(query: str, variables: object) -> bool:
    """Exact source templates only; arbitrary GraphQL remains a mutation charge."""
    if not isinstance(variables, dict):
        return False
    fields = {"owner", "name", "number"}
    if query in {MIDDLE_QUERY, PART_MIDDLE_QUERY, CLOSE_MIDDLE_QUERY}:
        fields.update(("first", "after"))
        if (
            type(variables.get("first")) is not int
            or not 1 <= variables["first"] <= 100
            or not _opaque(variables.get("after"))
        ):
            return False
    if query in {PART_MIDDLE_QUERY, PART_ROOTS_QUERY}:
        fields.add("id")
        if not _opaque(variables.get("id")):
            return False
    if query in {CLOSE_QUERY, CLOSE_MIDDLE_QUERY}:
        fields.update(("front", "back", "main", "roots"))
        if any(
            type(variables.get(key)) is not int or not 1 <= variables[key] <= 100
            for key in ("front", "back")
        ) or not _opaque(variables.get("main")):
            return False
        roots = variables.get("roots")
        if (
            not isinstance(roots, list)
            or len(roots) > MAX_EXISTING_ROOTS
            or any(not _opaque(value) for value in roots)
            or wire_bytes(roots) > MAX_EXISTING_ROOT_IDS_BYTES
        ):
            return False
    if (
        query
        not in {
            OPEN_QUERY,
            ROOT_OPEN_QUERY,
            PART_ROOTS_QUERY,
            MIDDLE_QUERY,
            PART_MIDDLE_QUERY,
            CLOSE_QUERY,
            CLOSE_MIDDLE_QUERY,
        }
        or set(variables) != fields
    ):
        return False
    return (
        all(
            isinstance(variables[key], str)
            and re.fullmatch(r"[A-Za-z0-9_.-]{1,256}", variables[key]) is not None
            for key in ("owner", "name")
        )
        and type(variables["number"]) is int
        and 1 <= variables["number"] <= 2**31 - 1
    )


@dataclass(frozen=True)
class RootMetadata:
    comment_id: int
    review_id: int
    producer_id: int | None
    resolved: bool
    side: str | None
    subject_type: str
    head_sha: str | None
    updated_at: str


def _root(value: Any) -> RootMetadata:
    if (
        not isinstance(value, dict)
        or set(value) != {"s", "d", "t", "x"}
        or wire_bytes(value) > ROOT_METADATA_BYTES
    ):
        raise ReviewInputError("native root metadata exceeds its selected projection")
    connection = value["x"]
    if (
        type(connection["totalCount"]) is not int
        or not 1 <= connection["totalCount"] <= 1000
        or not isinstance(connection["nodes"], list)
        or len(connection["nodes"]) != 1
    ):
        raise ReviewInputError("native root comment association is incomplete")
    root = connection["nodes"][0]
    if (
        set(root) != {"n", "r", "a", "c", "q", "u"}
        or root["q"] is not None
        or type(value["s"]) is not bool
        or value["d"] not in {None, "LEFT", "RIGHT"}
        or value["t"] not in {"LINE", "FILE"}
        or not isinstance(root["u"], str)
        or re.fullmatch(r"[0-9TZ:.+-]{1,64}", root["u"]) is None
    ):
        raise ReviewInputError("native root metadata is invalid")
    author = root["a"]
    producer = (
        _native_number(author["n"])
        if isinstance(author, dict) and author.get("t") == "Bot"
        else None
    )
    commit = root["c"]["o"] if root["c"] is not None else None
    if commit is not None and (
        not isinstance(commit, str) or re.fullmatch(r"[a-f0-9]{40}", commit) is None
    ):
        raise ReviewInputError("native root commit is invalid")
    return RootMetadata(
        _native_number(root["n"]),
        _native_number(root["r"]["n"]),
        producer,
        value["s"],
        value["d"],
        value["t"],
        commit,
        root["u"],
    )


def _scope(payload: dict[str, Any], plan: NativeReviewPlan) -> dict[str, Any]:
    try:
        repository = payload["data"]["repository"]
        pr = repository["p"]
        if (
            _native_number(repository["n"]) != plan.repository_id
            or pr["n"] != plan.pull_request
            or pr["h"] != plan.head_sha
            or pr["b"] != plan.base_sha
        ):
            raise ReviewInputError("native publication census scope changed")
        return pr
    except (KeyError, TypeError) as exc:
        raise ReviewInputError("native publication census scope is incomplete") from exc


def _window(
    connection: Any, *, expected_count: int | None = None
) -> tuple[int, tuple[RootMetadata, ...]]:
    if (
        not isinstance(connection, dict)
        or type(connection.get("totalCount")) is not int
        or not 0 <= connection["totalCount"] <= MAX_ROOTS
        or expected_count is not None
        and connection["totalCount"] != expected_count
        or not isinstance(connection.get("nodes"), list)
        or len(connection["nodes"]) > 100
    ):
        raise ReviewInputError("native publication root census is incomplete")
    rows = tuple(_root(value) for value in connection["nodes"])
    if len({row.comment_id for row in rows}) != len(rows):
        raise ReviewInputError("native publication root census repeats an identity")
    return connection["totalCount"], rows


def _join(
    windows: tuple[tuple[RootMetadata, ...], ...],
    *,
    expected_count: int,
    complete: bool,
) -> dict[int, RootMetadata]:
    result: dict[int, RootMetadata] = {}
    for rows in windows:
        for row in rows:
            if row.comment_id in result and result[row.comment_id] != row:
                raise ReviewInputError("native publication overlap changed")
            result[row.comment_id] = row
    if len(result) > expected_count or complete and len(result) != expected_count:
        raise ReviewInputError("native publication exact root membership is incomplete")
    return result


@dataclass(frozen=True)
class Opening:
    review_ids: tuple[int, ...]
    review_nodes: dict[int, dict[str, Any]]
    scope_variables: dict[str, object]


def _review_union(pr: dict[str, Any], *, opaque: bool) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    expected = None
    for key, flag in (("r", "hasNextPage"), ("v", "hasPreviousPage")):
        connection = pr[key]
        count = connection["totalCount"]
        if (
            type(count) is not int
            or not 0 <= count <= MAX_REVIEWS
            or expected is not None
            and count != expected
            or not isinstance(connection["nodes"], list)
            or len(connection["nodes"]) != min(100, count)
            or connection["pageInfo"][flag] is not (count > 100)
        ):
            raise ReviewInputError(
                "native publication complete review windows are invalid"
            )
        expected = count
        for row in connection["nodes"]:
            identifier = _native_number(row["n"])
            maximum = REVIEW_METADATA_BYTES if opaque else CLOSING_REVIEW_METADATA_BYTES
            if (
                wire_bytes(row) > maximum
                or opaque
                and not _opaque(row["i"])
                or row["s"]
                not in {
                    "PENDING",
                    "COMMENTED",
                    "APPROVED",
                    "CHANGES_REQUESTED",
                    "DISMISSED",
                }
            ):
                raise ReviewInputError("native publication review metadata conflicts")
            if identifier in result and result[identifier] != row:
                raise ReviewInputError(
                    "native publication review-window overlap changed"
                )
            result[identifier] = row
    if len(result) != expected:
        raise ReviewInputError("native publication review union is incomplete")
    return result


def open_publication_census(
    http,
    *,
    token: str,
    repository: str,
    plan: NativeReviewPlan,
    budget: NativeWalkBudget,
) -> Opening:
    owner, separator, name = repository.partition("/")
    variables: dict[str, object] = {
        "owner": owner,
        "name": name,
        "number": plan.pull_request,
    }
    if not separator or not source_query_variables(OPEN_QUERY, variables):
        raise ReviewInputError("native publication repository is invalid")
    payload = budget.request(http, token=token, query=OPEN_QUERY, variables=variables)
    try:
        selected = _review_union(_scope(payload, plan), opaque=True)
        return Opening(tuple(sorted(selected)), selected, variables)
    except (KeyError, TypeError, AttributeError) as exc:
        raise ReviewInputError("native publication opening is incomplete") from exc


@dataclass(frozen=True)
class RootOpening:
    root_count: int
    roots: dict[int, RootMetadata]
    first_cursor: str | None


def _root_opening(payload: dict[str, Any], plan: NativeReviewPlan) -> RootOpening:
    pr = _scope(payload, plan)
    count, front = _window(pr["f"])
    _, back = _window(pr["l"], expected_count=count)
    if (
        len(front) != min(count, 100)
        or len(back) != min(count, 100)
        or pr["f"]["pageInfo"]["hasNextPage"] is not (count > 100)
        or pr["l"]["pageInfo"]["hasPreviousPage"] is not (count > 100)
    ):
        raise ReviewInputError("native publication root window boundary is incomplete")
    cursor = pr["f"]["pageInfo"]["endCursor"]
    if count and not _opaque(cursor):
        raise ReviewInputError("native publication first cursor is missing")
    roots = _join((front, back), expected_count=count, complete=count <= 200)
    return RootOpening(count, roots, cursor)


@dataclass(frozen=True)
class NativePublicationEvidence:
    main_review_id: int
    groups: tuple[NativeGroupEvidence, ...]
    roots: tuple[RootMetadata, ...]
    old_root_bodies: tuple[dict[str, Any], ...]
    physical_reads: int


def closing_envelope_bound() -> int:
    """Joint main/index/root metadata maximum for this finite source profile.

    A complete frame includes >=2048 unescaped ASCII guidance bytes and >=86
    ASCII index bytes per finding. Each such byte saves5 against the generic
    6x UTF-8 JSON bound. At most one new root per finding costs336; thus the
    maximum is already covered by the zero-finding conservative expression.
    Existing complete root projections include their positive old growth.
    """
    from review_sensei.presentation import MAINTAINER_RESPONSE_INSTRUCTIONS

    if (
        sum(
            32 <= ord(char) <= 126 and char not in '"\\<>&'
            for char in MAINTAINER_RESPONSE_INSTRUCTIONS
        )
        < 2048
    ):
        raise ReviewInputError(
            "native closing guidance no longer satisfies its source bound"
        )
    return (
        6 * 65_536
        - 5 * 2048
        + (MAX_EXISTING_ROOTS + 1) * ROOT_METADATA_BYTES
        + MAX_REVIEWS * CLOSING_REVIEW_METADATA_BYTES
        + 8192
        + MAX_EXISTING_ROOT_PROJECTION_BYTES
    )


def read_publication_union(
    http,
    *,
    token: str,
    repository: str,
    plan: NativeReviewPlan,
    producer_id: int,
    group_review_ids: tuple[int, ...],
    main_review_id: int,
    main_body: str,
    main_state: str,
    old_root_nodes: dict[int, str],
    budget: NativeWalkBudget,
) -> NativePublicationEvidence:
    """Exact selected union with one physical ten-call counter and no fallback.

    Caller authority, stopped-writer/parent-association certificates and complete
    transaction admission remain independently required by the guarded host.
    """
    if (
        len(group_review_ids) != len(plan.groups)
        or len(set(group_review_ids)) != len(group_review_ids)
        or main_review_id in group_review_ids
        or len(old_root_nodes) > MAX_EXISTING_ROOTS
        or wire_bytes(list(old_root_nodes.values())) > MAX_EXISTING_ROOT_IDS_BYTES
    ):
        raise ReviewInputError("native publication selected union is invalid")
    opening = open_publication_census(
        http, token=token, repository=repository, plan=plan, budget=budget
    )
    selected_ids = (*group_review_ids, main_review_id)
    if any(identifier not in opening.review_nodes for identifier in selected_ids):
        raise ReviewInputError("native publication selected review is absent")
    groups: list[NativeGroupEvidence | None] = [None] * len(plan.groups)
    middle = None
    root_open = None
    order = sorted(range(len(plan.groups)), key=plan.response_upper_bound)
    for position, ordinal in enumerate(order):
        review_id = group_review_ids[ordinal]
        node = opening.review_nodes[review_id]
        if position == 0:
            payload = budget.request(
                http,
                token=token,
                query=PART_ROOTS_QUERY,
                variables={**opening.scope_variables, "id": node["i"]},
            )
            if (
                wire_bytes({"data": {"repository": payload["data"]["repository"]}})
                > ROOT_OPEN_METADATA_BYTES
            ):
                raise ReviewInputError(
                    "native root opening metadata exceeds its packet"
                )
            root_open = _root_opening(payload, plan)
            if root_open.root_count != len(old_root_nodes) + len(plan.items):
                raise ReviewInputError("native publication contains an unselected root")
            groups[ordinal] = verify_complete_group_response(
                payload,
                plan=plan,
                ordinal=ordinal,
                expected_review_id=review_id,
                producer_id=producer_id,
            )
        elif root_open is not None and root_open.root_count > 200 and middle is None:
            payload = budget.request(
                http,
                token=token,
                query=PART_MIDDLE_QUERY,
                variables={
                    **opening.scope_variables,
                    "id": node["i"],
                    "after": root_open.first_cursor,
                    "first": root_open.root_count - 200,
                },
            )
            pr = _scope(payload, plan)
            if (
                wire_bytes({"data": {"repository": payload["data"]["repository"]}})
                > MIDDLE_METADATA_BYTES
            ):
                raise ReviewInputError(
                    "native publication middle metadata exceeds its packet"
                )
            _, middle = _window(pr["m"], expected_count=root_open.root_count)
            if len(middle) != root_open.root_count - 200:
                raise ReviewInputError(
                    "native publication middle membership is incomplete"
                )
            groups[ordinal] = verify_complete_group_response(
                payload,
                plan=plan,
                ordinal=ordinal,
                expected_review_id=review_id,
                producer_id=producer_id,
            )
        else:
            groups[ordinal] = read_complete_group(
                http,
                token=token,
                plan=plan,
                ordinal=ordinal,
                node_id=node["i"],
                expected_review_id=review_id,
                producer_id=producer_id,
                budget=budget,
            )
    if root_open is None:
        payload = budget.request(
            http, token=token, query=ROOT_OPEN_QUERY, variables=opening.scope_variables
        )
        root_open = _root_opening(payload, plan)
    if root_open.root_count != len(old_root_nodes) + len(plan.items):
        raise ReviewInputError("native publication contains an unselected root")
    needs_middle = root_open.root_count > 200
    if needs_middle and middle is None:
        payload = budget.request(
            http,
            token=token,
            query=MIDDLE_QUERY,
            variables={
                **opening.scope_variables,
                "after": root_open.first_cursor,
                "first": root_open.root_count - 200,
            },
        )
        _, middle = _window(
            _scope(payload, plan)["m"], expected_count=root_open.root_count
        )
    opened_roots = _join(
        (tuple(root_open.roots.values()), middle or ()),
        expected_count=root_open.root_count,
        complete=True,
    )
    variables = {
        **opening.scope_variables,
        "front": min(100, max(1, root_open.root_count)),
        "back": min(100, max(1, root_open.root_count - 100)),
        "main": opening.review_nodes[main_review_id]["i"],
        "roots": list(old_root_nodes.values()),
    }
    query = CLOSE_QUERY
    if needs_middle:
        variables.update(after=root_open.first_cursor, first=root_open.root_count - 200)
        query = CLOSE_MIDDLE_QUERY
    if not source_query_variables(query, variables):
        raise ReviewInputError("native publication closing request is unsupported")
    payload = budget.request(http, token=token, query=query, variables=variables)
    try:
        pr = _scope(payload, plan)
        selected = _review_union(pr, opaque=False)
        if {identifier: row["s"] for identifier, row in selected.items()} != {
            identifier: row["s"] for identifier, row in opening.review_nodes.items()
        }:
            raise ReviewInputError(
                "native publication closing review membership changed"
            )
        _, front = _window(pr["f"], expected_count=root_open.root_count)
        _, back = _window(pr["l"], expected_count=root_open.root_count)
        middle = ()
        if needs_middle:
            _, middle = _window(pr["m"], expected_count=root_open.root_count)
        closed_roots = _join(
            (front, back, middle), expected_count=root_open.root_count, complete=True
        )
        if (
            set(closed_roots) != set(opened_roots)
            or pr["f"]["pageInfo"]["endCursor"] != root_open.first_cursor
        ):
            raise ReviewInputError("native publication closing root membership changed")
        main = payload["data"]["main"]
        if (
            _native_number(main["n"]) != main_review_id
            or main["b"] != main_body
            or main["s"] != main_state
            or main["a"]["t"] != "Bot"
            or _native_number(main["a"]["n"]) != producer_id
            or main["c"]["o"] != plan.head_sha
            or main["p"]["n"] != plan.pull_request
            or _native_number(main["p"]["r"]["n"]) != plan.repository_id
            or main["x"]["totalCount"] != 0
        ):
            raise ReviewInputError("native publication final authority frame differs")
        seen = set()
        confirmed = tuple(group for group in groups if group is not None)
        for ordinal, group in enumerate(confirmed):
            expected = {item.instance: item for item in plan.groups[ordinal]}
            for identifier, instance, updated_at in zip(
                group.comment_ids, group.instances, group.updated_at, strict=True
            ):
                root = closed_roots.get(identifier)
                if (
                    identifier in seen
                    or root is None
                    or root.review_id != group.review_id
                    or root.producer_id != producer_id
                    or root.head_sha != plan.head_sha
                    or root.side != expected[instance].side
                    or root.subject_type != "LINE"
                    or root.updated_at != updated_at
                ):
                    raise ReviewInputError(
                        "native publication fresh root association or body version changed"
                    )
                seen.add(identifier)
        if set(closed_roots) != set(old_root_nodes) | seen:
            raise ReviewInputError(
                "native publication root union contains an unselected source"
            )
        old = payload["data"]["old"]
        if (
            not isinstance(old, list)
            or len(old) != len(old_root_nodes)
            or wire_bytes(old) > MAX_EXISTING_ROOT_PROJECTION_BYTES
            or {_native_number(row["n"]) for row in old} != set(old_root_nodes)
        ):
            raise ReviewInputError(
                "native publication existing root body closure is incomplete"
            )
        return NativePublicationEvidence(
            main_review_id,
            confirmed,
            tuple(closed_roots.values()),
            tuple(old),
            budget.used,
        )
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise ReviewInputError(
            "native publication complete closing response is invalid"
        ) from exc

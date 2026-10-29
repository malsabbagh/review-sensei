"""One complete publication walk: opening, bounded bodies, fresh closing.

Numeric root/parent identities carry membership. Opaque mutation target IDs are
an independently bound immutable association, never repeated per body packet.
Every mutable resolution and body/version fence is read in this same walk.
"""

from __future__ import annotations

import hashlib
import json
import re
import weakref
from dataclasses import asdict, dataclass
from typing import Any

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError

from .http import GitHubHttp
from .native_review_groups import (
    EXISTING_ROOT_PROJECTION_BYTES,
    MIDDLE_METADATA_BYTES,
    PART_QUERY,
    NativeGroupEvidence,
    NativeReviewPlan,
    NativeWalkBudget,
    _native_number,
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
MAX_EXISTING_ROOT_PROJECTION_BYTES = EXISTING_ROOT_PROJECTION_BYTES
MAX_APPROVAL_PROJECTION_BYTES = 4096
MAX_EXISTING_ROOT_IDS_BYTES = 1536

_SCOPE = "n:number h:headRefOid b:baseRefOid "
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
_MAIN = "frames:nodes(ids:$frames){... on PullRequestReview{n:fullDatabaseId b:body s:state a:author{t:__typename ... on Bot{n:databaseId}} c:commit{o:oid} p:pullRequest{n:number r:repository{n:databaseId}} x:comments(first:1){totalCount}}}"
_OLD = "old:nodes(ids:$roots){... on PullRequestReviewThread{i:id s:isResolved d:diffSide t:subjectType x:comments(first:1){totalCount nodes{n:fullDatabaseId b:body p:path l:line o:originalLine a:author{t:__typename ... on Bot{n:databaseId}} c:commit{o:oid} z:originalCommit{o:oid} r:pullRequestReview{n:fullDatabaseId} q:replyTo{n:fullDatabaseId} u:updatedAt}}}}"
_OLD_SCOPE = _REPOSITORY_SCOPE + "}}"
OLD_QUERY = (
    "query NativeExistingRoots($owner:String!,$name:String!,$number:Int!,$roots:[ID!]!){"
    + _ROOT_OPEN
    + _OLD
    + "}"
)
PART_OLD_QUERY = (
    "query NativeReviewPartWithOldRoots($id:ID!,$owner:String!,$name:String!,$number:Int!,$roots:[ID!]!){"
    + _PART_SELECTION
    + _OLD_SCOPE
    + _OLD
    + "}"
)
_CLOSE_FRAGMENT = "fragment NativeClosingRoot on PullRequestReviewThread{" + _ROOT + "}"
_CLOSE_SCOPE = (
    "repository(owner:$owner,name:$name){n:databaseId p:pullRequest(number:$number){"
    + _SCOPE
    + " r:reviews(first:100){totalCount pageInfo{hasNextPage} nodes{n:fullDatabaseId s:state}}v:reviews(last:100){totalCount pageInfo{hasPreviousPage} nodes{n:fullDatabaseId s:state}} f:reviewThreads(first:$front){totalCount pageInfo{hasNextPage endCursor} nodes{"
    + "...NativeClosingRoot"
    + "}} l:reviewThreads(last:$back){totalCount pageInfo{hasPreviousPage} nodes{"
    + "...NativeClosingRoot"
    + "}}"
)
_CLOSE_MIDDLE = (
    "m:reviewThreads(first:$first,after:$after){totalCount pageInfo{hasNextPage} nodes{"
    + "...NativeClosingRoot"
    + "}}"
)
_CLOSE_VARS = (
    "$owner:String!,$name:String!,$number:Int!,$front:Int!,$back:Int!,$frames:[ID!]!"
)
CLOSE_QUERY = (
    "query NativePublicationClosing("
    + _CLOSE_VARS
    + "){"
    + _CLOSE_SCOPE
    + "}}"
    + _MAIN
    + "}"
    + _CLOSE_FRAGMENT
)
CLOSE_MIDDLE_QUERY = (
    "query NativePublicationClosingWithMiddle("
    + _CLOSE_VARS
    + ",$first:Int!,$after:String!){"
    + _CLOSE_SCOPE
    + _CLOSE_MIDDLE
    + "}}"
    + _MAIN
    + "}"
    + _CLOSE_FRAGMENT
)


# Opaque IDs remain bounded at2048 ASCII bytes. They are fetched as a separate
# projection in spare space, not repeated in every body/closing metadata row.
ROOT_ID_ITEM_BYTES = 2112
ROOT_ID_FRAME_BYTES = 2400
_ID_NODES = "nodes{i:id x:comments(first:1){nodes{n:fullDatabaseId}}}"
_ID_FORWARD = (
    "g:reviewThreads(first:$u,after:$v){totalCount pageInfo{hasNextPage endCursor}"
    + _ID_NODES
    + "}"
)
_ID_LAST = (
    "g:reviewThreads(last:$u){totalCount pageInfo{hasPreviousPage}" + _ID_NODES + "}"
)
_ID_FIRST_LAST = (
    "g:reviewThreads(first:100){totalCount pageInfo{hasNextPage endCursor}"
    + _ID_NODES
    + "}"
    "j:reviewThreads(last:100){totalCount pageInfo{hasPreviousPage}" + _ID_NODES + "}"
)
ROOT_IDS_QUERY = (
    "query NativeRootIds($owner:String!,$name:String!,$number:Int!){"
    + _REPOSITORY_SCOPE
    + _ID_FIRST_LAST
    + "}}}"
)
ROOT_IDS_FORWARD_QUERY = (
    "query NativeRootIdsNext($owner:String!,$name:String!,$number:Int!,$u:Int!,$v:String){"
    + _REPOSITORY_SCOPE
    + _ID_FORWARD
    + "}}}"
)


def _with_root_ids(query: str, *, tail: bool = False) -> str:
    header, separator, body = query.partition("){")
    if not separator:
        raise ReviewInputError("native root ID query template changed")
    scope = ",$owner:String!,$name:String!,$number:Int!" if query == PART_QUERY else ""
    additions = ",$u:Int!" + ("" if tail else ",$v:String")
    selected = _REPOSITORY_SCOPE + (_ID_LAST if tail else _ID_FORWARD) + "}}"
    value = header + scope + additions + "){" + body[:-1] + selected + "}"
    # Only trusted source syntax is compacted. Opaque IDs and variables are
    # serialized unchanged; no query text supplied by a caller is admitted.
    value = (
        value.replace("NativeClosingRoot", "R")
        .replace("NativePublicationClosingWithMiddle", "C")
        .replace("NativePublicationClosing", "C")
    )
    return re.sub(r"\s*([{}():!,])\s*", r"\1", value)


ROOT_ID_QUERIES = {
    _with_root_ids(query, tail=tail): (query, tail)
    for query, tail in (
        (PART_QUERY, False),
        (PART_OLD_QUERY, False),
        (PART_ROOTS_QUERY, False),
        (PART_MIDDLE_QUERY, False),
        (CLOSE_QUERY, True),
        (CLOSE_MIDDLE_QUERY, True),
    )
}


def root_ids_base_query(query: str) -> str:
    return ROOT_ID_QUERIES.get(query, (query, False))[0]


def root_id_schedule_certificate(
    total: int, largest: int, *, source_bytes: int, findings: int
) -> dict[str, int]:
    from .http import MAX_GITHUB_RESPONSE_BYTES
    from .native_review_groups import PART_RESPONSE_FRAMING_BYTES

    space = (
        MAX_GITHUB_RESPONSE_BYTES - PART_RESPONSE_FRAMING_BYTES - ROOT_ID_FRAME_BYTES
    )
    companions = (
        EXISTING_ROOT_PROJECTION_BYTES
        + ROOT_OPEN_METADATA_BYTES
        + MIDDLE_METADATA_BYTES
    )
    # Sum floor(x_i) >= floor(sum(x_i)) -(k-1). If a packet reaches100
    # IDs, sorted round-robin balance bounds each other group by its body
    # plus one largest item; one such capped packet already yields>=300.
    body = (8 * space - companions - total) // ROOT_ID_ITEM_BYTES - 7
    closing = (
        MAX_GITHUB_RESPONSE_BYTES - closing_envelope_bound() - ROOT_ID_FRAME_BYTES
    ) // ROOT_ID_ITEM_BYTES
    capped = (
        100
        + (7 * (100 * ROOT_ID_ITEM_BYTES - largest) - companions) // ROOT_ID_ITEM_BYTES
        - 6
    )
    if findings != 250:
        raise ReviewInputError("native root ID source finding envelope changed")
    per_finding = (total - 2 * source_bytes) // findings
    seven = (
        7 * space - companions - (2 * source_bytes + 7 * 32 * per_finding)
    ) // ROOT_ID_ITEM_BYTES - 6
    if body + closing < MAX_ROOTS or capped < MAX_ROOTS or seven < 74:
        raise ReviewInputError(
            "native root ID schedule no longer covers the full output envelope"
        )
    return {
        "root_id_item_bytes": ROOT_ID_ITEM_BYTES,
        "root_id_frame_bytes": ROOT_ID_FRAME_BYTES,
        "eight_body_id_capacity": body,
        "closing_id_capacity": closing,
        "one_capped_body_id_capacity": capped,
        "seven_body_id_capacity": seven,
    }


class _RootIds:
    def __init__(self, count: int, old: dict[int, str]):
        self.count, self.old = count, old
        self.ids: dict[int, str] = {}
        self.cursor: str | None = None
        self.position = 0
        self.cursors: set[str] = set()

    @property
    def missing(self) -> int:
        return self.count - len(self.ids)

    def capacity(self, body_bound: int) -> int:
        return min(
            100,
            max(0, (524_288 - body_bound - ROOT_ID_FRAME_BYTES) // ROOT_ID_ITEM_BYTES),
        )

    def accept(self, connection, *, first: bool, requested: int) -> None:
        try:
            rows, page = connection["nodes"], connection["pageInfo"]
            expected = (
                min(requested, self.count - self.position)
                if first
                else min(requested, self.count)
            )
            if (
                connection["totalCount"] != self.count
                or not isinstance(rows, list)
                or len(rows) != expected
            ):
                raise ReviewInputError("native root ID membership is incomplete")
            if (
                wire_bytes(connection)
                > requested * ROOT_ID_ITEM_BYTES + ROOT_ID_FRAME_BYTES
            ):
                raise ReviewInputError(
                    "native root ID packet exceeds its admitted bound"
                )
            seen = set()
            for row in rows:
                if (
                    set(row) != {"i", "x"}
                    or not _opaque(row["i"])
                    or wire_bytes(row) + 1 > ROOT_ID_ITEM_BYTES
                ):
                    raise ReviewInputError("native root ID projection is invalid")
                nodes = row["x"]["nodes"]
                if (
                    not isinstance(nodes, list)
                    or len(nodes) != 1
                    or set(nodes[0]) != {"n"}
                ):
                    raise ReviewInputError("native root ID association is incomplete")
                root = _native_number(nodes[0]["n"])
                if root in seen or root in self.old and self.old[root] != row["i"]:
                    raise ReviewInputError("native root ID association changed")
                if root in self.ids and self.ids[root] != row["i"]:
                    raise ReviewInputError("native root ID overlap changed")
                if row["i"] in self.ids.values() and self.ids.get(root) != row["i"]:
                    raise ReviewInputError("native root ID association is duplicated")
                self.ids[root] = row["i"]
                seen.add(root)
            if first:
                self.position += len(rows)
                if type(page["hasNextPage"]) is not bool or page["hasNextPage"] != (
                    self.position < self.count
                ):
                    raise ReviewInputError("native root ID cursor membership changed")
                cursor = page["endCursor"]
                if rows and (not _opaque(cursor) or cursor in self.cursors):
                    raise ReviewInputError("native root ID cursor is incomplete")
                if cursor is not None:
                    self.cursors.add(cursor)
                self.cursor = cursor
            elif type(page["hasPreviousPage"]) is not bool or page[
                "hasPreviousPage"
            ] != (requested < self.count):
                raise ReviewInputError("native root ID closing membership changed")
        except (KeyError, TypeError, AttributeError) as error:
            raise ReviewInputError("native root ID response is incomplete") from error


def _without_root_ids(payload):
    data = dict(payload["data"])
    repository = dict(data["repository"])
    repository["p"] = {
        key: value for key, value in repository["p"].items() if key not in {"g", "j"}
    }
    data["repository"] = repository
    return {"data": data}


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
    if query in ROOT_ID_QUERIES:
        base, tail = ROOT_ID_QUERIES[query]
        extra = {"u"} if tail else {"u", "v"}
        if (
            not extra <= variables.keys()
            or type(variables["u"]) is not int
            or not 1 <= variables["u"] <= 100
        ):
            return False
        if not tail and variables["v"] is not None and not _opaque(variables["v"]):
            return False
        remainder = {key: value for key, value in variables.items() if key not in extra}
        if base == PART_QUERY:
            return (
                set(remainder) == {"id", "owner", "name", "number"}
                and _opaque(remainder["id"])
                and source_query_variables(
                    OPEN_QUERY,
                    {key: value for key, value in remainder.items() if key != "id"},
                )
            )
        return source_query_variables(base, remainder)
    if query in {ROOT_IDS_QUERY, ROOT_IDS_FORWARD_QUERY}:
        extra = {"u", "v"} if query == ROOT_IDS_FORWARD_QUERY else set()
        if extra and (
            not extra <= variables.keys()
            or type(variables["u"]) is not int
            or not 1 <= variables["u"] <= 100
            or variables["v"] is not None
            and not _opaque(variables["v"])
        ):
            return False
        return source_query_variables(
            OPEN_QUERY,
            {key: value for key, value in variables.items() if key not in extra},
        )
    fields = {"owner", "name", "number"}
    if query in {MIDDLE_QUERY, PART_MIDDLE_QUERY, CLOSE_MIDDLE_QUERY}:
        fields.update(("first", "after"))
        if (
            type(variables.get("first")) is not int
            or not 1 <= variables["first"] <= 100
            or not _opaque(variables.get("after"))
        ):
            return False
    if query in {PART_MIDDLE_QUERY, PART_ROOTS_QUERY, PART_OLD_QUERY}:
        fields.add("id")
        if not _opaque(variables.get("id")):
            return False
    if query in {CLOSE_QUERY, CLOSE_MIDDLE_QUERY}:
        fields.update(("front", "back", "frames"))
        if any(
            type(variables.get(key)) is not int or not 1 <= variables[key] <= 100
            for key in ("front", "back")
        ):
            return False
        frames = variables.get("frames")
        if (
            not isinstance(frames, list)
            or not 1 <= len(frames) <= 2
            or any(not _opaque(value) for value in frames)
            or len(set(frames)) != len(frames)
        ):
            return False
    if query in {OLD_QUERY, PART_OLD_QUERY}:
        fields.add("roots")
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
            OLD_QUERY,
            PART_OLD_QUERY,
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
    context_sha256: str
    main_body: str
    main_state: str
    document_sha256: str | None
    approvals: tuple[dict[str, Any], ...] = ()
    thread_ids: tuple[tuple[int, str], ...] = ()
    root_bodies: tuple[dict[str, Any], ...] = ()
    plan: NativeReviewPlan | None = None
    closing_bytes: bytes = b""
    review_states: tuple[tuple[int, str], ...] = ()


_ISSUED_ROOT_EVIDENCE: dict[
    int,
    tuple[
        weakref.ReferenceType[NativePublicationEvidence],
        str,
        weakref.ReferenceType[GitHubHttp],
        str,
        weakref.ReferenceType[object] | None,
    ],
] = {}


def _root_evidence_digest(value: NativePublicationEvidence) -> str:
    return hashlib.sha256(
        canonical_bytes(
            {
                "context": value.context_sha256,
                "main": value.main_review_id,
                "main_body": value.main_body,
                "main_state": value.main_state,
                "document": value.document_sha256,
                "approvals": value.approvals,
                "groups": [group.receipt_digest for group in value.groups],
                "roots": [asdict(root) for root in value.roots],
                "ids": value.thread_ids,
                "bodies": value.root_bodies,
                "plan": None if value.plan is None else value.plan.sha256,
                "closing": value.closing_bytes.decode("ascii"),
                "states": value.review_states,
            }
        )
    ).hexdigest()


def _remember_root_evidence(
    value: NativePublicationEvidence, *, http, token
) -> NativePublicationEvidence:
    from .caller_operation import caller_for_http

    identifier = id(value)
    caller = caller_for_http(http)

    def expire(reference):
        current = _ISSUED_ROOT_EVIDENCE.get(identifier)
        if current is not None and current[0] is reference:
            _ISSUED_ROOT_EVIDENCE.pop(identifier, None)

    _ISSUED_ROOT_EVIDENCE[identifier] = (
        weakref.ref(value, expire),
        _root_evidence_digest(value),
        weakref.ref(http),
        hashlib.sha256(token.encode()).hexdigest(),
        None if caller is None else weakref.ref(caller),
    )
    return value


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
        + MAX_APPROVAL_PROJECTION_BYTES
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
    main_body: str | None,
    main_state: str,
    old_root_nodes: dict[int, str],
    budget: NativeWalkBudget,
    opening: Opening | None = None,
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
    if opening is None:
        opening = open_publication_census(
            http, token=token, repository=repository, plan=plan, budget=budget
        )
    selected_ids = (*group_review_ids, main_review_id)
    if any(identifier not in opening.review_nodes for identifier in selected_ids):
        raise ReviewInputError("native publication selected review is absent")
    root_ids = _RootIds(len(old_root_nodes) + len(plan.items), old_root_nodes)
    order = sorted(range(len(plan.groups)), key=plan.response_upper_bound)
    companions = [MAX_EXISTING_ROOT_PROJECTION_BYTES, ROOT_OPEN_METADATA_BYTES]
    companions.append(MIDDLE_METADATA_BYTES if root_ids.count > 200 else 0)
    capacity = sum(
        root_ids.capacity(
            plan.response_upper_bound(ordinal)
            + (companions[position] if position < 3 else 0)
        )
        for position, ordinal in enumerate(order)
    )
    stream_only = capacity >= root_ids.count
    if not plan.items:
        root_ids.ids.update(old_root_nodes)
    elif len(plan.groups) <= 7 and not stream_only:
        id_payload = budget.request(
            http, token=token, query=ROOT_IDS_QUERY, variables=opening.scope_variables
        )
        id_scope = _scope(id_payload, plan)
        root_ids.accept(id_scope["g"], first=True, requested=100)
        root_ids.accept(id_scope["j"], first=False, requested=100)
        if root_ids.missing and len(plan.groups) <= 6:
            requested = min(100, root_ids.missing)
            id_payload = budget.request(
                http,
                token=token,
                query=ROOT_IDS_FORWARD_QUERY,
                variables={
                    **opening.scope_variables,
                    "u": requested,
                    "v": root_ids.cursor,
                },
            )
            root_ids.accept(
                _scope(id_payload, plan)["g"], first=True, requested=requested
            )

    def part_request(query, variables, ordinal, companion=0):
        requested = (
            min(
                root_ids.missing,
                root_ids.capacity(plan.response_upper_bound(ordinal) + companion),
            )
            if len(plan.groups) >= 7 or stream_only
            else 0
        )
        if requested:
            query = _with_root_ids(query)
            variables = {
                **opening.scope_variables,
                **variables,
                "u": requested,
                "v": root_ids.cursor,
            }
        payload = budget.request(http, token=token, query=query, variables=variables)
        if requested:
            root_ids.accept(_scope(payload, plan)["g"], first=True, requested=requested)
        return payload

    groups: list[NativeGroupEvidence | None] = [None] * len(plan.groups)
    middle = None
    root_open = None
    old_threads = None
    for position, ordinal in enumerate(order):
        review_id = group_review_ids[ordinal]
        node = opening.review_nodes[review_id]
        if position == 0:
            payload = part_request(
                query=PART_OLD_QUERY,
                variables={
                    **opening.scope_variables,
                    "id": node["i"],
                    "roots": list(old_root_nodes.values()),
                },
                ordinal=ordinal,
                companion=MAX_EXISTING_ROOT_PROJECTION_BYTES,
            )
            _scope(payload, plan)
            old_threads = payload["data"]["old"]
            if wire_bytes(old_threads) > MAX_EXISTING_ROOT_PROJECTION_BYTES:
                raise ReviewInputError("native existing root packet exceeds its bound")
            groups[ordinal] = verify_complete_group_response(
                payload,
                plan=plan,
                ordinal=ordinal,
                expected_review_id=review_id,
                producer_id=producer_id,
            )
        elif position == 1:
            payload = part_request(
                query=PART_ROOTS_QUERY,
                variables={**opening.scope_variables, "id": node["i"]},
                ordinal=ordinal,
                companion=ROOT_OPEN_METADATA_BYTES,
            )
            if (
                wire_bytes(
                    {
                        "data": {
                            "repository": _without_root_ids(payload)["data"][
                                "repository"
                            ]
                        }
                    }
                )
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
            payload = part_request(
                query=PART_MIDDLE_QUERY,
                variables={
                    **opening.scope_variables,
                    "id": node["i"],
                    "after": root_open.first_cursor,
                    "first": root_open.root_count - 200,
                },
                ordinal=ordinal,
                companion=MIDDLE_METADATA_BYTES,
            )
            pr = _scope(payload, plan)
            if (
                wire_bytes(
                    {
                        "data": {
                            "repository": _without_root_ids(payload)["data"][
                                "repository"
                            ]
                        }
                    }
                )
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
            payload = part_request(PART_QUERY, {"id": node["i"]}, ordinal)
            groups[ordinal] = verify_complete_group_response(
                payload,
                plan=plan,
                ordinal=ordinal,
                expected_review_id=review_id,
                producer_id=producer_id,
            )
    if old_threads is None:
        payload = budget.request(
            http,
            token=token,
            query=OLD_QUERY,
            variables={
                **opening.scope_variables,
                "roots": list(old_root_nodes.values()),
            },
        )
        _scope(payload, plan)
        old_threads = payload["data"]["old"]
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
    approvals = [
        identifier
        for identifier, row in opening.review_nodes.items()
        if row.get("s") == "APPROVED"
        and row.get("c") == {"o": plan.head_sha}
        and row.get("a") == {"t": "Bot", "n": producer_id}
    ]
    if len(approvals) > 1 or main_review_id in approvals:
        raise ReviewInputError("native publication approval membership is ambiguous")
    frame_ids = [main_review_id, *approvals]
    variables = {
        **opening.scope_variables,
        "front": min(100, max(1, root_open.root_count)),
        "back": min(100, max(1, root_open.root_count - 100)),
        "frames": [opening.review_nodes[identifier]["i"] for identifier in frame_ids],
    }
    query = CLOSE_QUERY
    if needs_middle:
        variables.update(after=root_open.first_cursor, first=root_open.root_count - 200)
        query = CLOSE_MIDDLE_QUERY
    closing_ids = root_ids.missing
    if closing_ids:
        if len(plan.groups) != 8 or closing_ids > root_ids.capacity(
            closing_envelope_bound()
        ):
            raise ReviewInputError(
                "native root ID schedule cannot complete its original walk"
            )
        query = _with_root_ids(query, tail=True)
        variables["u"] = closing_ids
    if not source_query_variables(query, variables):
        raise ReviewInputError("native publication closing request is unsupported")
    payload = budget.request(http, token=token, query=query, variables=variables)
    try:
        pr = _scope(payload, plan)
        if closing_ids:
            root_ids.accept(pr["g"], first=False, requested=closing_ids)
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
        frames = payload["data"]["frames"]
        if not isinstance(frames, list) or len(frames) != len(frame_ids):
            raise ReviewInputError(
                "native publication authority frame closure is incomplete"
            )
        by_id = {_native_number(row["n"]): row for row in frames}
        if len(by_id) != len(frames) or set(by_id) != set(frame_ids):
            raise ReviewInputError("native publication authority identities conflict")
        approval_rows = tuple(by_id[identifier] for identifier in approvals)
        if wire_bytes(approval_rows) > MAX_APPROVAL_PROJECTION_BYTES:
            raise ReviewInputError(
                "native publication approval frame exceeds its bound"
            )
        for row in approval_rows:
            if (
                row["s"] != "APPROVED"
                or not isinstance(row["b"], str)
                or row["a"]["t"] != "Bot"
                or _native_number(row["a"]["n"]) != producer_id
                or row["c"]["o"] != plan.head_sha
                or row["p"]["n"] != plan.pull_request
                or _native_number(row["p"]["r"]["n"]) != plan.repository_id
                or row["x"]["totalCount"] != 0
            ):
                raise ReviewInputError("native publication approval scope conflicts")
        main = by_id[main_review_id]
        if (
            _native_number(main["n"]) != main_review_id
            or not isinstance(main["b"], str)
            or main_body is not None
            and main["b"] != main_body
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
        if (
            not isinstance(old_threads, list)
            or len(old_threads) != len(old_root_nodes)
            or wire_bytes(old_threads) > MAX_EXISTING_ROOT_PROJECTION_BYTES
        ):
            raise ReviewInputError(
                "native publication existing root body closure is incomplete"
            )
        old = []
        observed_old = set()
        for thread in old_threads:
            comments = thread["x"]
            if (
                type(comments["totalCount"]) is not int
                or not 1 <= comments["totalCount"] <= 1000
                or not isinstance(comments["nodes"], list)
                or len(comments["nodes"]) != 1
            ):
                raise ReviewInputError("native historical thread root is incomplete")
            row = comments["nodes"][0]
            identifier = _native_number(row["n"])
            root = closed_roots.get(identifier)
            if (
                identifier in observed_old
                or identifier not in old_root_nodes
                or thread["i"] != old_root_nodes[identifier]
                or root is None
                or thread["s"] is not root.resolved
                or thread["d"] != root.side
                or thread["t"] != root.subject_type
                or row["q"] is not None
                or _native_number(row["r"]["n"]) != root.review_id
                or row["a"]["t"] != "Bot"
                or _native_number(row["a"]["n"]) != root.producer_id
                or row["c"]["o"] != root.head_sha
                or row["u"] != root.updated_at
                or not isinstance(row["b"], str)
            ):
                raise ReviewInputError(
                    "native historical thread identity or association changed"
                )
            observed_old.add(identifier)
            old.append(row)
        if observed_old != set(old_root_nodes):
            raise ReviewInputError("native historical thread closure omits a root")
        if set(root_ids.ids) != set(closed_roots):
            raise ReviewInputError("native complete root ID join omits or adds a root")
        from .native_union import native_union_frame_document

        try:
            document_sha256 = hashlib.sha256(
                canonical_bytes(native_union_frame_document(main["b"]))
            ).hexdigest()
        except ReviewInputError:
            document_sha256 = None
        evidence = NativePublicationEvidence(
            main_review_id,
            confirmed,
            tuple(closed_roots.values()),
            tuple(old),
            budget.used,
            hashlib.sha256(canonical_bytes(plan.context(producer_id))).hexdigest(),
            main["b"],
            main["s"],
            document_sha256,
            approval_rows,
            tuple(sorted(root_ids.ids.items())),
            tuple(old) + tuple(row for group in confirmed for row in group.root_rows),
            plan,
            canonical_bytes(
                {key: value for key, value in variables.items() if key != "u"}
            ),
            tuple(
                sorted(
                    (identifier, row["s"])
                    for identifier, row in opening.review_nodes.items()
                )
            ),
        )
        return _remember_root_evidence(evidence, http=http, token=token)
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise ReviewInputError(
            "native publication complete closing response is invalid"
        ) from exc


def require_native_root_evidence(http: GitHubHttp, *, token, evidence):
    """Check original in-process evidence identity without another HTTP read."""
    from .caller_operation import caller_for_http

    if type(evidence) is not NativePublicationEvidence or evidence.plan is None:
        raise ReviewInputError(
            "native root consumer requires a complete original union"
        )
    issued = _ISSUED_ROOT_EVIDENCE.get(id(evidence))
    caller = caller_for_http(http)
    if (
        issued is None
        or issued[0]() is not evidence
        or issued[1] != _root_evidence_digest(evidence)
        or issued[2]() is not http
        or issued[3] != hashlib.sha256(token.encode()).hexdigest()
        or (issued[4] is None and caller is not None)
        or (issued[4] is not None and (caller is None or issued[4]() is not caller))
    ):
        raise ReviewInputError(
            "native root consumer evidence was reconstructed or changed"
        )
    return evidence.plan


def refresh_native_thread_nodes(
    http: GitHubHttp, *, token, repository, evidence, app_slug, producer_id
):
    """Fence a completed same-operation union before a root consumer uses it.

    Every body in evidence was fetched completely in this finite operation.
    This single source-owned closing read checks membership, body versions,
    mutable resolutions and the actual authority frame again. It cannot finish
    an interrupted walk, authenticate a serialized hash, or fund another scan.
    The ordinary caller/EvidenceReadBudget remains attached to the transport.
    """
    from .caller_operation import http_read_operation

    plan = require_native_root_evidence(http, token=token, evidence=evidence)
    variables = json.loads(evidence.closing_bytes)
    owner, separator, name = repository.partition("/")
    query = CLOSE_MIDDLE_QUERY if "after" in variables else CLOSE_QUERY
    if (
        not separator
        or (variables.get("owner"), variables.get("name")) != (owner, name)
        or not source_query_variables(query, variables)
    ):
        raise ReviewInputError("native root consumer scope changed")
    with http_read_operation(
        http, "native-root-consumer:" + str(evidence.document_sha256)
    ):
        status, payload = http.request(
            "POST",
            "/graphql",
            token=token,
            body={"query": query, "variables": variables},
        )
    if (
        status != 200
        or not isinstance(payload, dict)
        or payload.get("errors") not in (None, [])
    ):
        raise ReviewInputError("native root consumer closing is unavailable")
    pr = _scope(payload, plan)
    selected = _review_union(pr, opaque=False)
    if (
        tuple(sorted((key, row["s"]) for key, row in selected.items()))
        != evidence.review_states
    ):
        raise ReviewInputError("native root consumer review membership changed")
    windows = []
    for key in ("f", "l", "m") if "after" in variables else ("f", "l"):
        _, rows = _window(pr[key], expected_count=len(evidence.roots))
        windows.append(rows)
    closed = _join(tuple(windows), expected_count=len(evidence.roots), complete=True)
    previous = {row.comment_id: row for row in evidence.roots}
    ids = dict(evidence.thread_ids)
    bodies = {_native_number(row["n"]): row for row in evidence.root_bodies}
    if (
        set(closed) != set(previous)
        or set(ids) != set(closed)
        or set(bodies) != set(closed)
    ):
        raise ReviewInputError("native root consumer complete membership changed")
    frames = payload["data"]["frames"]
    if not isinstance(frames, list):
        raise ReviewInputError("native root consumer authority frames are incomplete")
    by_id = {_native_number(row["n"]): row for row in frames}
    expected_frames = {
        evidence.main_review_id,
        *(_native_number(row["n"]) for row in evidence.approvals),
    }
    if len(by_id) != len(frames) or set(by_id) != expected_frames:
        raise ReviewInputError("native root consumer authority membership changed")
    main = by_id[evidence.main_review_id]
    producer = producer_id
    if (
        evidence.context_sha256
        != hashlib.sha256(canonical_bytes(plan.context(producer))).hexdigest()
    ):
        raise ReviewInputError("native root consumer original producer changed")
    if (
        main.get("b"),
        main.get("s"),
        main.get("a"),
        main.get("c"),
        main.get("p"),
        main.get("x"),
    ) != (
        evidence.main_body,
        evidence.main_state,
        {"t": "Bot", "n": producer},
        {"o": plan.head_sha},
        {"n": plan.pull_request, "r": {"n": plan.repository_id}},
        {"totalCount": 0},
    ):
        raise ReviewInputError("native root consumer authority frame changed")
    if any(by_id[_native_number(row["n"])] != row for row in evidence.approvals):
        raise ReviewInputError("native root consumer approval frame changed")
    result = []
    for identifier, current in closed.items():
        old = previous[identifier]
        if (
            current.comment_id,
            current.review_id,
            current.producer_id,
            current.side,
            current.subject_type,
            current.head_sha,
            current.updated_at,
        ) != (
            old.comment_id,
            old.review_id,
            old.producer_id,
            old.side,
            old.subject_type,
            old.head_sha,
            old.updated_at,
        ):
            raise ReviewInputError(
                "native root consumer body version or association changed"
            )
        row = bodies[identifier]
        if (
            row["u"] != current.updated_at
            or _native_number(row["r"]["n"]) != current.review_id
            or row["a"] != {"t": "Bot", "n": current.producer_id}
            or row["c"] != {"o": current.head_sha}
        ):
            raise ReviewInputError("native root consumer body proof conflicts")
        result.append(
            {
                "id": ids[identifier],
                "isResolved": current.resolved,
                "comments": {
                    "nodes": [
                        {
                            "databaseId": identifier,
                            "body": row["b"],
                            "path": row["p"],
                            "line": row["l"],
                            "author": {"login": app_slug, "__typename": "Bot"},
                        }
                    ]
                },
            }
        )
    return tuple(result)

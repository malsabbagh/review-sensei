"""Immutable complete inline groups, with no authority on staging reviews.

The selected native projection deliberately omits diffHunk, URLs, login text,
and opaque comment IDs. Exact numeric parent identities and original locations
remain. Thread identity/side/resolution are a separately fenced metadata view.
Schema: https://docs.github.com/en/graphql/reference/pulls#pullrequestreviewcomment
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.human_assessment import finding_instance_fingerprint
from review_sensei.models import ReviewComment
from review_sensei.presentation import MAINTAINER_RESPONSE_INSTRUCTIONS
from review_sensei.publication_staging import MAX_PUBLICATION_PARTS
from review_sensei.validation import validate_repository_path

from .http import MAX_GITHUB_RESPONSE_BYTES, MAX_PAGINATION_PAGES, GitHubHttp

GROUP_FORMAT = "bounded-inline-groups-v1"
SELECTED_COMMENT_METADATA_BYTES = 512
PART_RESPONSE_FRAMING_BYTES = 8192
MIDDLE_METADATA_BYTES = 36 * 1024
ROOT_OPEN_METADATA_BYTES = 80 * 1024
EXISTING_ROOT_PROJECTION_BYTES = 96 * 1024
MAX_GROUP_ROOTS = 32
_SHA = re.compile(r"[a-f0-9]{64}")
_GIT_SHA = re.compile(r"[a-f0-9]{40}")

# One source-owned query, not a caller-supplied selection language. A complete
# group has at most100 roots. No nested cursor ever grants another allowance.
PART_QUERY = """query NativeReviewPart($id:ID!){node(id:$id){... on PullRequestReview{
n:fullDatabaseId b:body s:state a:author{t:__typename ... on Bot{n:databaseId}}
c:commit{o:oid} p:pullRequest{n:number r:repository{n:databaseId}}
x:comments(first:100){totalCount pageInfo{hasNextPage endCursor} nodes{
n:fullDatabaseId b:body p:path l:line o:originalLine
a:author{t:__typename ... on Bot{n:databaseId}}
c:commit{o:oid} z:originalCommit{o:oid} r:pullRequestReview{n:fullDatabaseId}
q:replyTo{n:fullDatabaseId} u:updatedAt}}}}}"""


def wire_bytes(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode())


@dataclass(frozen=True)
class NativeInline:
    path: str
    line: int
    side: str
    body: str
    instance: str

    def __post_init__(self) -> None:
        validate_repository_path(self.path, allow_glob_chars=True)
        if (
            type(self.line) is not int
            or not 1 <= self.line <= 2_147_483_647
            or self.side not in {"LEFT", "RIGHT"}
            or not isinstance(self.body, str)
            or len(self.body.encode()) > 16_384
            or not isinstance(self.instance, str)
            or _SHA.fullmatch(self.instance) is None
        ):
            raise ReviewInputError("native inline plan item is invalid")

    @classmethod
    def from_prepared(
        cls, comment: ReviewComment, body: str, *, anchor: str
    ) -> NativeInline:
        if anchor not in {"left", "right"} or comment.line is None:
            raise ReviewInputError("native inline anchor is invalid")
        return cls(
            path=comment.path,
            line=comment.line,
            side=anchor.upper(),
            body=body,
            instance=finding_instance_fingerprint(comment),
        )

    def payload(self) -> dict[str, object]:
        return {
            "path": self.path,
            "line": self.line,
            "side": self.side,
            "body": self.body,
        }

    @property
    def selected_upper_bound(self) -> int:
        # Includes commas, keys, quotes and every bounded selected field except
        # these two untrusted strings, whose full encoded values are charged.
        return (
            wire_bytes(self.body)
            + wire_bytes(self.path)
            + SELECTED_COMMENT_METADATA_BYTES
        )

    def descriptor(self) -> dict[str, object]:
        return {
            "path": self.path,
            "line": self.line,
            "side": self.side,
            "instance": self.instance,
            "body_sha256": hashlib.sha256(self.body.encode()).hexdigest(),
        }


@dataclass(frozen=True)
class NativeReviewPlan:
    repository_id: int
    pull_request: int
    base_sha: str
    head_sha: str
    transaction_id: str
    result_sha256: str
    items: tuple[NativeInline, ...]
    ordinals: tuple[tuple[int, ...], ...]
    sha256: str

    @classmethod
    def prepare(
        cls,
        *,
        repository_id: int,
        pull_request: int,
        base_sha: str,
        head_sha: str,
        transaction_id: str,
        result_sha256: str,
        items: tuple[NativeInline, ...],
    ) -> NativeReviewPlan:
        if (
            type(repository_id) is not int
            or not 1 <= repository_id <= 9_007_199_254_740_991
            or type(pull_request) is not int
            or not 1 <= pull_request <= 2_147_483_647
            or any(
                not isinstance(value, str) or _GIT_SHA.fullmatch(value) is None
                for value in (base_sha, head_sha)
            )
            or any(
                not isinstance(value, str) or _SHA.fullmatch(value) is None
                for value in (transaction_id, result_sha256)
            )
            or not isinstance(items, tuple)
            or len(items) > 250
            or any(type(item) is not NativeInline for item in items)
            or len({item.instance for item in items}) != len(items)
        ):
            raise ReviewInputError("native review plan scope or inventory is invalid")
        ordered = sorted(
            range(len(items)),
            key=lambda index: (-items[index].selected_upper_bound, index),
        )
        groups: tuple[tuple[int, ...], ...] = ()
        for count in range(1, MAX_PUBLICATION_PARTS + 1):
            bins: list[list[int]] = [[] for _ in range(count)]
            for rank, ordinal in enumerate(ordered):
                bins[rank % count].append(ordinal)
            candidate = tuple(tuple(sorted(group)) for group in bins if group)
            if (
                all(
                    len(group) <= MAX_GROUP_ROOTS
                    and sum(items[index].selected_upper_bound for index in group)
                    + PART_RESPONSE_FRAMING_BYTES
                    <= MAX_GITHUB_RESPONSE_BYTES
                    for group in candidate
                )
                and (
                    not candidate
                    or min(
                        sum(items[index].selected_upper_bound for index in group)
                        for group in candidate
                    )
                    + PART_RESPONSE_FRAMING_BYTES
                    + EXISTING_ROOT_PROJECTION_BYTES
                    <= MAX_GITHUB_RESPONSE_BYTES
                )
                and (
                    len(candidate) < 2
                    or sorted(
                        sum(items[index].selected_upper_bound for index in group)
                        for group in candidate
                    )[1]
                    + PART_RESPONSE_FRAMING_BYTES
                    + ROOT_OPEN_METADATA_BYTES
                    <= MAX_GITHUB_RESPONSE_BYTES
                )
                and (
                    len(candidate) < 3
                    or sorted(
                        sum(items[index].selected_upper_bound for index in group)
                        for group in candidate
                    )[2]
                    + PART_RESPONSE_FRAMING_BYTES
                    + MIDDLE_METADATA_BYTES
                    <= MAX_GITHUB_RESPONSE_BYTES
                )
            ):
                groups = candidate
                break
        if items and not groups:
            raise ReviewInputError(
                "complete native groups exceed the original eight-body-call allocation"
            )
        descriptor = {
            "schema_version": GROUP_FORMAT,
            "repository_id": repository_id,
            "pull_request": pull_request,
            "base_sha": base_sha,
            "head_sha": head_sha,
            "transaction_id": transaction_id,
            "result_sha256": result_sha256,
            "items": [item.descriptor() for item in items],
            "groups": [list(group) for group in groups],
        }
        digest = hashlib.sha256(canonical_bytes(descriptor)).hexdigest()
        return cls(
            repository_id,
            pull_request,
            base_sha,
            head_sha,
            transaction_id,
            result_sha256,
            items,
            groups,
            digest,
        )

    @property
    def groups(self) -> tuple[tuple[NativeInline, ...], ...]:
        return tuple(
            tuple(self.items[index] for index in group) for group in self.ordinals
        )

    def response_upper_bound(self, ordinal: int) -> int:
        return PART_RESPONSE_FRAMING_BYTES + sum(
            self.items[index].selected_upper_bound for index in self.ordinals[ordinal]
        )

    def intent(self) -> dict[str, object]:
        # The independently authenticated original result reconstructs the
        # bodies, original instance mapping, locations and deterministic bins.
        return {
            "schema_version": GROUP_FORMAT,
            "transaction_id": self.transaction_id,
            "result_sha256": self.result_sha256,
            "plan_sha256": self.sha256,
            "part_count": len(self.ordinals),
        }

    def context(self, producer_id: int) -> dict[str, object]:
        return {
            "repository_id": self.repository_id,
            "pull_request": self.pull_request,
            "producer_id": producer_id,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "transaction_id": self.transaction_id,
            "result_sha256": self.result_sha256,
            "plan_sha256": self.sha256,
            "inline_count": len(self.items),
        }

    def part_body(self, ordinal: int) -> str:
        if type(ordinal) is not int or not 0 <= ordinal < len(self.ordinals):
            raise ReviewInputError("native review part ordinal is invalid")
        marker = f"<!-- reviewsensei:native-part:v1 plan={self.sha256} repo={self.repository_id} pr={self.pull_request} head={self.head_sha} base={self.base_sha} transaction={self.transaction_id} result={self.result_sha256} ordinal={ordinal} count={len(self.ordinals)} -->"
        return f"ReviewSensei publication part {ordinal + 1} of {len(self.ordinals)}. Approval remains withheld until the complete final review is confirmed.\n\n{MAINTAINER_RESPONSE_INSTRUCTIONS}\n\n{marker}"

    def part_payload(self, ordinal: int) -> dict[str, object]:
        return {
            "body": self.part_body(ordinal),
            "event": "COMMENT",
            "commit_id": self.head_sha,
            "comments": [item.payload() for item in self.groups[ordinal]],
        }

    def part_digest(self, ordinal: int) -> str:
        payload = self.part_payload(ordinal)
        payload["comments"] = [
            item.payload()
            for item in sorted(self.groups[ordinal], key=lambda item: item.instance)
        ]
        return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def _native_number(value: object) -> int:
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,18}", value):
        value = int(value)
    if type(value) is not int or not 1 <= value <= 2**63 - 1:
        raise ReviewInputError("native review projection identity is invalid")
    return value


@dataclass
class NativeWalkBudget:
    """One whole native walk, including opening, bodies and closing reads."""

    used: int = 0
    deadline: float = 0

    def __post_init__(self) -> None:
        if self.used != 0 or self.deadline != 0:
            raise ReviewInputError(
                "native walk cannot import or replenish an allowance"
            )
        self.deadline = time.monotonic() + 60

    def request(
        self, http: GitHubHttp, *, token: str, query: str, variables: dict[str, object]
    ) -> dict[str, Any]:
        if query == PART_QUERY:
            supported = (
                set(variables) == {"id"}
                and isinstance(variables["id"], str)
                and 1 <= len(variables["id"]) <= 2048
                and all(33 <= ord(char) <= 126 for char in variables["id"])
            )
        else:
            from .native_review_census import source_query_variables

            supported = source_query_variables(query, variables)
        if not supported:
            raise ReviewInputError("native walk only admits exact source read queries")
        body: dict[str, object] = {"query": query, "variables": variables}
        if wire_bytes(body) > 8192:
            raise ReviewInputError("native selected request exceeds its byte limit")
        if self.used >= MAX_PAGINATION_PAGES or time.monotonic() >= self.deadline:
            raise ReviewInputError("native complete walk allowance exhausted")
        # Consume before transport, including errors and lost responses. The
        # transport independently charges the caller's original physical budget.
        self.used += 1
        status, payload = http.request("POST", "/graphql", token=token, body=body)
        if time.monotonic() >= self.deadline:
            raise ReviewInputError("native complete walk deadline exhausted")
        if (
            status != 200
            or not isinstance(payload, dict)
            or payload.get("errors") not in (None, [])
        ):
            raise ReviewInputError("native selected read is unavailable")
        if wire_bytes(payload) > MAX_GITHUB_RESPONSE_BYTES:
            raise ReviewInputError("native selected response exceeds its byte limit")
        return payload


@dataclass(frozen=True)
class NativeGroupEvidence:
    review_id: int
    comment_ids: tuple[int, ...]
    content_sha256: str
    instances: tuple[str, ...]
    updated_at: tuple[str, ...]
    root_rows: tuple[dict[str, Any], ...] = ()

    @property
    def receipt_digest(self) -> str:
        return hashlib.sha256(
            canonical_bytes(
                {
                    "schema_version": "native-group-evidence-v1",
                    "review_id": self.review_id,
                    "prepared_content_sha256": self.content_sha256,
                    "members": [
                        {"instance": instance, "id": identifier}
                        for instance, identifier in sorted(
                            zip(self.instances, self.comment_ids, strict=True)
                        )
                    ],
                }
            )
        ).hexdigest()


def read_complete_group(
    http: GitHubHttp,
    *,
    token: str,
    plan: NativeReviewPlan,
    ordinal: int,
    node_id: str,
    expected_review_id: int,
    producer_id: int,
    budget: NativeWalkBudget,
) -> NativeGroupEvidence:
    """One exact complete COMMENT group; no thread or approval authority.

    The caller independently selected this review in the opening scope census.
    Its fresh closing metadata must still join every root to its native thread
    and confirm current resolution before cleanup or approval can use the union.
    """
    if (
        not isinstance(node_id, str)
        or not 1 <= len(node_id) <= 2048
        or any(not 33 <= ord(char) <= 126 for char in node_id)
        or type(budget) is not NativeWalkBudget
    ):
        raise ReviewInputError("native complete group selection is invalid")
    _native_number(expected_review_id)
    _native_number(producer_id)
    payload = budget.request(
        http, token=token, query=PART_QUERY, variables={"id": node_id}
    )
    return verify_complete_group_response(
        payload,
        plan=plan,
        ordinal=ordinal,
        expected_review_id=expected_review_id,
        producer_id=producer_id,
    )


def verify_complete_group_response(
    payload: dict[str, Any],
    *,
    plan: NativeReviewPlan,
    ordinal: int,
    expected_review_id: int,
    producer_id: int,
) -> NativeGroupEvidence:
    """Verify the same complete projection when metadata shares its one call."""

    try:
        review = payload["data"]["node"]
        review_id = _native_number(review["n"])
        author = review["a"]
        scope = review["p"]
        comments = review["x"]
        nodes, page = comments["nodes"], comments["pageInfo"]
        expected = {item.instance: item for item in plan.groups[ordinal]}
        if (
            review_id != expected_review_id
            or review["b"] != plan.part_body(ordinal)
            or review["s"] != "COMMENTED"
            or author["t"] != "Bot"
            or _native_number(author["n"]) != producer_id
            or review["c"]["o"] != plan.head_sha
            or scope["n"] != plan.pull_request
            or _native_number(scope["r"]["n"]) != plan.repository_id
            or type(comments["totalCount"]) is not int
            or comments["totalCount"] != len(expected)
            or not isinstance(nodes, list)
            or len(nodes) != len(expected)
            or page["hasNextPage"] is not False
            or page["endCursor"] is not None
            and (
                not isinstance(page["endCursor"], str)
                or len(page["endCursor"]) > 2048
                or any(not 33 <= ord(char) <= 126 for char in page["endCursor"])
            )
        ):
            raise ReviewInputError(
                "native complete group differs from its immutable plan"
            )
        from .publication import finding_instance_from_body

        observed, ids, instances, updated_at = set(), [], [], []
        for node in nodes:
            native_id = _native_number(node["n"])
            instance = finding_instance_from_body(node["b"])
            item = expected.get(instance) if instance is not None else None
            metadata = {
                key: value for key, value in node.items() if key not in {"b", "p"}
            }
            if (
                item is None
                or instance in observed
                or native_id in ids
                or node["b"] != item.body
                or node["p"] != item.path
                or type(node["l"]) is not int
                or node["l"] != item.line
                or type(node["o"]) is not int
                or node["o"] != item.line
                or node["a"]["t"] != "Bot"
                or _native_number(node["a"]["n"]) != producer_id
                or node["c"]["o"] != plan.head_sha
                or node["z"]["o"] != plan.head_sha
                or _native_number(node["r"]["n"]) != review_id
                or node["q"] is not None
                or not isinstance(node["u"], str)
                or len(node["u"]) > 64
                or any(not 32 <= ord(char) <= 126 for char in node["u"])
                or wire_bytes(metadata) + 16 > SELECTED_COMMENT_METADATA_BYTES
            ):
                raise ReviewInputError(
                    "native complete group inline membership or prose differs"
                )
            observed.add(instance)
            ids.append(native_id)
            assert isinstance(instance, str)
            instances.append(instance)
            updated_at.append(node["u"])
        # Charge the actual enclosing projection independently of per-item
        # bounds. Oversized cursors/fields cannot borrow another body packet.
        framing = wire_bytes({"data": {"node": review}}) - sum(
            wire_bytes(node) + 1 for node in nodes
        )
        if framing > PART_RESPONSE_FRAMING_BYTES or observed != set(expected):
            raise ReviewInputError("native complete group framing or inventory differs")
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise ReviewInputError("native complete group response is incomplete") from exc
    return NativeGroupEvidence(
        review_id,
        tuple(ids),
        plan.part_digest(ordinal),
        tuple(instances),
        tuple(updated_at),
        tuple(json.loads(canonical_bytes(node)) for node in nodes),
    )


def full_inline_envelope() -> dict[str, int]:
    """Source maximum for complete-body packets, not a whole-walk certificate.

    2R pays every escaped prose/path source byte. Each of two UTF-8-bounded
    classification labels can add at most one further encoded source copy
    beyond2R (Unicode case mapping and Markdown escaping together are <=3J).
    The finite source formatter shell is measured with full-width finding IDs,
    not merely today's common19-character abbreviation. Opening/closing, old
    root bodies, full reply readers and caller work are separate admission gates.
    """
    from review_sensei.models import ReviewResult
    from review_sensei.presentation import FindingView, render_finding
    from review_sensei.validation import MAX_REPOSITORY_PATH_BYTES

    from .operation_comments import _MAX_RESULT_BYTES
    from .publication import (
        _with_discussion_instruction,
        finding_instance_marker,
        finding_marker,
    )

    comment = ReviewComment(path="x", line=1, body="x", blocking=True)
    result = ReviewResult(
        summary="x",
        comments=(comment,),
        provider="fixture",
        model="fixture",
        review_status="complete",
    )
    sizes = []
    for requirement in ("required", "optional"):
        for human in (False, True):
            for advisory in (False, True):
                for state in ("new", "still-present", "uncertain", "fixed", "outdated"):
                    view = FindingView(
                        identifier="RS-" + "A" * 64,
                        requirement=requirement,
                        severity="x",
                        title="X",
                        detail="x",
                        placement="inline",
                        lifecycle_state=state,
                        advisory=advisory,
                        needs_human=human,
                    )
                    body = f"{_with_discussion_instruction(render_finding(view))}\n\n{finding_instance_marker(comment)}\n\n{finding_marker(repository_id=9_007_199_254_740_991, pull_request=2_147_483_647, head_sha='b' * 40, base_sha='a' * 40, result=result, blocking=requirement == 'required', fingerprint='c' * 64, state=state, needs_human=human)}"
                    # Remove the single scalar detail and two label values;
                    # keep quotes, trusted suffixes, separators and every marker.
                    sizes.append(wire_bytes(body) - 3)
    count = 250
    fixed = max(sizes)
    total = (
        2 * _MAX_RESULT_BYTES
        + 2 * count * 3 * 256
        + count * (fixed + 18 + SELECTED_COMMENT_METADATA_BYTES)
    )
    largest = (
        6 * 16_384 + 3 * MAX_REPOSITORY_PATH_BYTES + 4 + SELECTED_COMMENT_METADATA_BYTES
    )
    # Sorted round-robin groups differ from the average by at most7/8 of the
    # largest item: the sum of every full block is paid exactly once in B.
    packet = (total + 7 * largest + 7) // 8 + PART_RESPONSE_FRAMING_BYTES
    from .native_review_census import root_id_schedule_certificate

    root_id_certificate = root_id_schedule_certificate(
        total, largest, source_bytes=_MAX_RESULT_BYTES, findings=count
    )
    return {
        **root_id_certificate,
        "accepted_source_bytes": _MAX_RESULT_BYTES,
        "findings": count,
        "fixed_inline_bytes": fixed,
        "selected_total_bytes": total,
        "largest_selected_item_bytes": largest,
        "body_calls": 8,
        "largest_body_response_bytes": packet,
        "body_response_margin_bytes": MAX_GITHUB_RESPONSE_BYTES - packet,
        "smallest_with_old_roots_bytes": (total + 7) // 8
        + PART_RESPONSE_FRAMING_BYTES
        + EXISTING_ROOT_PROJECTION_BYTES,
        "second_with_root_opening_bytes": (total + largest + 7) // 8
        + PART_RESPONSE_FRAMING_BYTES
        + ROOT_OPEN_METADATA_BYTES,
        "third_with_middle_bytes": (total + 2 * largest + 7) // 8
        + PART_RESPONSE_FRAMING_BYTES
        + MIDDLE_METADATA_BYTES,
    }

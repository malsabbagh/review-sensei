"""Source-bound native reader domains; projection never settles old effects.

Only installed reviewed provenance may supply this certificate. Fresh native
parent association and a separately authenticated ceased-execution census are
still required at every use. A matching head alone is not a projection proof.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from review_sensei.errors import ReviewInputError

PROJECTION_VERSION = "native-reader-projection-v1"
_SHA = re.compile(r"[a-f0-9]{64}")
_GIT_SHA = re.compile(r"[a-f0-9]{40}")
_ROOT_DOMAINS = frozenset({"publication-replay", "suppression", "approval", "cleanup"})
_FULL_DOMAINS = frozenset({"conversation-replies", "legacy-report-conflicts"})


def _number(value: object) -> bool:
    return type(value) is int and 1 <= value <= 2**63 - 1


def _hash(value: object, *, git: bool = False) -> bool:
    return (
        isinstance(value, str)
        and (_GIT_SHA if git else _SHA).fullmatch(value) is not None
    )


def _opaque(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 2048
        and all(33 <= ord(char) <= 126 for char in value)
    )


@dataclass(frozen=True)
class HistoricalRoot:
    root_id: int
    thread_id: str
    review_id: int
    head_sha: str


@dataclass(frozen=True)
class ReaderProjection:
    repository_id: int
    pull_request: int
    producer_id: int
    execution_manifest_sha256: str
    source_contract_sha256: str
    fixed_explicit_review_heads: tuple[str, ...]
    fixed_check_heads: tuple[str, ...]
    implicit_review_objects: int
    roots: tuple[HistoricalRoot, ...]

    def require_fresh_associations(
        self,
        *,
        repository_id: int,
        pull_request: int,
        producer_id: int,
        successor_head: str,
        execution_manifest_sha256: str,
        ceased_census_sha256: str,
        current_census_sha256: str,
        rows: tuple[dict[str, object], ...],
    ) -> None:
        """No row/head/name assertion can replace the independent ceased census."""
        if (
            (repository_id, pull_request, producer_id)
            != (self.repository_id, self.pull_request, self.producer_id)
            or execution_manifest_sha256 != self.execution_manifest_sha256
            or not _hash(ceased_census_sha256)
            or ceased_census_sha256 != current_census_sha256
            or not _hash(successor_head, git=True)
            or successor_head in self.fixed_explicit_review_heads
            or successor_head in self.fixed_check_heads
            or not isinstance(rows, tuple)
        ):
            raise ReviewInputError(
                "native reader projection scope or cessation fence changed"
            )
        observed = {}
        for row in rows:
            if (
                not isinstance(row, dict)
                or set(row)
                != {"root_id", "thread_id", "review_id", "head_sha", "producer_id"}
                or not _number(row["root_id"])
                or row["root_id"] in observed
                or not _number(row["review_id"])
                or not _opaque(row["thread_id"])
                or not _hash(row["head_sha"], git=True)
                or row["producer_id"] != self.producer_id
            ):
                raise ReviewInputError(
                    "native reader parent census is incomplete or conflicting"
                )
            observed[row["root_id"]] = row
        for root in self.roots:
            if observed.get(root.root_id) != {
                "root_id": root.root_id,
                "thread_id": root.thread_id,
                "review_id": root.review_id,
                "head_sha": root.head_sha,
                "producer_id": self.producer_id,
            }:
                raise ReviewInputError(
                    "native reader historical parent association changed"
                )

    def body_domain(self, purpose: str) -> str:
        if purpose in _ROOT_DOMAINS:
            return "complete-root-bodies"
        if purpose in _FULL_DOMAINS:
            # Old replies remain fully charged. In particular a legacy report
            # receipt under the wrong parent is still a conflict, not invisible.
            return "complete-inline-conversations"
        raise ReviewInputError("native reader purpose has no installed projection")


def validate_reader_projection(
    value: object,
    contract: dict,
    *,
    source_contract_sha256: str,
    surface_exposure: dict,
) -> ReaderProjection:
    from .quarantine_published_tail import execution_manifest_digest

    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "schema_version",
            "repository_id",
            "pull_request",
            "producer_id",
            "execution_manifest_sha256",
            "source_contract_sha256",
            "executions",
            "roots",
            "reply_review_allocation",
        }
        or value["schema_version"] != PROJECTION_VERSION
        or not all(
            _number(value[key])
            for key in ("repository_id", "pull_request", "producer_id")
        )
        or value["pull_request"] > 2**31 - 1
        or value["execution_manifest_sha256"] != execution_manifest_digest(contract)
        or not _hash(source_contract_sha256)
        or value["source_contract_sha256"] != source_contract_sha256
        or not isinstance(value["executions"], list)
        or not isinstance(value["roots"], list)
        or len(value["roots"]) > 1000
    ):
        raise ReviewInputError("native reader projection certificate is unsupported")
    allocation = value["reply_review_allocation"]
    fields = {
        "explicit_new_objects",
        "explicit_new_wire_bytes",
        "implicit_new_objects",
        "implicit_new_wire_bytes",
        "implicit_max_object_wire_bytes",
        "implicit_head_binding",
    }
    if (
        not isinstance(allocation, dict)
        or set(allocation) != fields
        or allocation["implicit_head_binding"] != "api-selected-head"
        or any(
            type(allocation[key]) is not int
            or not 0 <= allocation[key] <= 9_007_199_254_740_991
            for key in fields - {"implicit_head_binding"}
        )
    ):
        raise ReviewInputError(
            "native implicit reply-review allocation proof is absent"
        )
    reviews, replies = surface_exposure["reviews"], surface_exposure["inline_replies"]
    if (
        allocation["implicit_new_objects"] != replies["new_objects"]
        or allocation["explicit_new_objects"] + allocation["implicit_new_objects"]
        > reviews["new_objects"]
        or allocation["explicit_new_wire_bytes"] + allocation["implicit_new_wire_bytes"]
        > reviews["new_wire_bytes"]
        or allocation["implicit_max_object_wire_bytes"]
        > reviews["max_object_wire_bytes"]
        or allocation["implicit_new_wire_bytes"]
        < allocation["implicit_new_objects"]
        * allocation["implicit_max_object_wire_bytes"]
        or allocation["implicit_new_objects"]
        and allocation["implicit_max_object_wire_bytes"] < 11
    ):
        raise ReviewInputError(
            "native review reserve omits possible implicit reply allocations"
        )
    roots = []
    root_ids, thread_ids = set(), set()
    for root in value["roots"]:
        if (
            not isinstance(root, dict)
            or set(root) != {"root_id", "thread_id", "review_id", "head_sha"}
            or not _number(root["root_id"])
            or root["root_id"] in root_ids
            or not _number(root["review_id"])
            or not _opaque(root["thread_id"])
            or root["thread_id"] in thread_ids
            or not _hash(root["head_sha"], git=True)
        ):
            raise ReviewInputError(
                "native reader projection root association is invalid"
            )
        root_ids.add(root["root_id"])
        thread_ids.add(root["thread_id"])
        roots.append(HistoricalRoot(**root))
    originals = {item["run_id"]: item for item in contract["executions"]}
    runs, heads, check_heads = set(), set(), set()
    for row in value["executions"]:
        if (
            not isinstance(row, dict)
            or set(row)
            != {
                "run_id",
                "route",
                "writer_source_sha256",
                "explicit_review_head_sha",
                "check_head_sha",
                "reply_parent_root_ids",
                "resolution_root_ids",
                "new_thread_roots",
            }
            or not _number(row["run_id"])
            or row["run_id"] in runs
            or row["run_id"] not in originals
            or row["route"] != originals[row["run_id"]]["route"]
            or row["writer_source_sha256"] != source_contract_sha256
            or not _hash(row["explicit_review_head_sha"], git=True)
            or not _hash(row["check_head_sha"], git=True)
            or type(row["new_thread_roots"]) is not int
            or row["new_thread_roots"] != 0
            or any(
                not isinstance(row[key], list)
                or len(row[key]) > len(root_ids)
                or any(not _number(identifier) for identifier in row[key])
                or len(set(row[key])) != len(row[key])
                or not set(row[key]) <= root_ids
                for key in ("reply_parent_root_ids", "resolution_root_ids")
            )
        ):
            raise ReviewInputError(
                "native reader projection execution domain is incomplete"
            )
        source = originals[row["run_id"]]["source"]
        if source is not None and source["root_id"] not in row["reply_parent_root_ids"]:
            raise ReviewInputError(
                "native reader projection omitted its original reply parent"
            )
        runs.add(row["run_id"])
        heads.add(row["explicit_review_head_sha"])
        check_heads.add(row["check_head_sha"])
    # A reply's parent may come from an older published origin. Its native
    # parent association is checked independently; do not relabel it with the
    # command's explicit review/check head or an API-selected implicit head.
    if runs != set(originals):
        raise ReviewInputError(
            "native reader projection execution/parent union is incomplete"
        )
    return ReaderProjection(
        value["repository_id"],
        value["pull_request"],
        value["producer_id"],
        value["execution_manifest_sha256"],
        source_contract_sha256,
        tuple(sorted(heads)),
        tuple(sorted(check_heads)),
        allocation["implicit_new_objects"],
        tuple(roots),
    )

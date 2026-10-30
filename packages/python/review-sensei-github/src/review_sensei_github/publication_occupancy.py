"""Inactive publication-scoped occupancy from an already complete native walk.

This projection performs no HTTP, grants no mutation/reopening authority and is
not a global PR occupancy census. Body-placed/legacy inventories and unsettled
creation attempts require separate evidence before strict publication can run.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.publication_selection import PublishedSlot

from .http import GitHubHttp
from .native_review_census import (
    NativePublicationEvidence,
    require_native_root_evidence,
)
from .native_union import NativeUnionReceipt, native_union_frame_document
from .publication import parse_finding_index


def _sha(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


@dataclass(frozen=True)
class NativeFindingTarget:
    comment_id: int
    review_id: int
    thread_id: str
    body_sha256: str
    resolved: bool
    proof_sha256: str

    @property
    def slot(self) -> PublishedSlot:
        # UI resolution alone proves neither obligation settlement nor absence
        # of an automatic reopening liability. Keep the reservation uncertain.
        return PublishedSlot(
            f"native-comment:{self.comment_id}",
            "uncertain" if self.resolved else "active",
            self.proof_sha256,
        )


@dataclass(frozen=True)
class NativePublicationOccupancy:
    """One original publication's complete native thread union, not PR authority."""

    repository_id: int
    pull_request: int
    base_sha: str
    head_sha: str
    plan_sha256: str
    main_review_id: int
    targets: tuple[NativeFindingTarget, ...]
    source_sha256: str

    @property
    def slots(self) -> tuple[PublishedSlot, ...]:
        return tuple(target.slot for target in self.targets)


def native_publication_occupancy(
    http: GitHubHttp,
    *,
    token: str,
    evidence: NativePublicationEvidence,
    producer_id: int,
) -> NativePublicationOccupancy:
    """Project original same-operation evidence without fetching another page.

    Callers cannot restore authority by parsing a digest or constructing the
    evidence dataclass. Its original transport, credential, caller and complete
    closed native walk must still match the source reader's issued proof.
    """
    plan = require_native_root_evidence(http, token=token, evidence=evidence)
    if (
        type(producer_id) is not int
        or producer_id <= 0
        or evidence.context_sha256 != _sha(plan.context(producer_id))
    ):
        raise ReviewInputError("publication occupancy producer differs")
    frame = native_union_frame_document(evidence.main_body)
    receipt = NativeUnionReceipt.from_document(frame["native_union"])
    receipt.require_evidence(evidence, document_sha256=_sha(frame))
    index = parse_finding_index(
        evidence.main_body,
        repository_id=plan.repository_id,
        pull_request=plan.pull_request,
        head_sha=plan.head_sha,
    )
    if (
        index is None
        or index.result_digest != plan.result_sha256
        or index.base_sha != plan.base_sha
        or any(entry.body_placed for entry in index.entries)
        or {entry.instance for entry in index.entries}
        != {item.instance for item in plan.items}
        or len(index.entries) != len(plan.items)
    ):
        raise ReviewInputError("publication occupancy needs exact inline inventory")
    roots = {root.comment_id: root for root in evidence.roots}
    ids = dict(evidence.thread_ids)
    bodies = {int(row["n"]): row for row in evidence.root_bodies}
    if (
        len(roots) != len(evidence.roots)
        or len(ids) != len(evidence.thread_ids)
        or len(bodies) != len(evidence.root_bodies)
        or set(roots) != set(ids)
        or set(roots) != set(bodies)
        or any(root.producer_id != producer_id for root in roots.values())
    ):
        raise ReviewInputError("publication occupancy needs complete owned roots")
    targets = tuple(
        NativeFindingTarget(
            identifier,
            roots[identifier].review_id,
            ids[identifier],
            hashlib.sha256(bodies[identifier]["b"].encode()).hexdigest(),
            roots[identifier].resolved,
            _sha(
                {
                    "scope": plan.context(producer_id),
                    "root": asdict(roots[identifier]),
                    "thread_id": ids[identifier],
                    "body": bodies[identifier],
                }
            ),
        )
        for identifier in sorted(roots)
    )
    source = _sha(
        {
            "schema_version": "native-thread-occupancy-v1",
            "scope": plan.context(producer_id),
            "main_review_id": evidence.main_review_id,
            "authority_frame": _sha(frame),
            "finding_index": asdict(index),
            "targets": [asdict(target) for target in targets],
        }
    )
    return NativePublicationOccupancy(
        plan.repository_id,
        plan.pull_request,
        plan.base_sha,
        plan.head_sha,
        plan.sha256,
        evidence.main_review_id,
        targets,
        source,
    )


def require_publication_occupancy(
    occupancy: NativePublicationOccupancy,
    http: GitHubHttp,
    *,
    token: str,
    evidence: NativePublicationEvidence,
    producer_id: int,
) -> None:
    """Check the entire projection against original independent read authority."""
    expected = native_publication_occupancy(
        http, token=token, evidence=evidence, producer_id=producer_id
    )
    if type(occupancy) is not NativePublicationOccupancy or occupancy != expected:
        raise ReviewInputError("publication occupancy source differs")

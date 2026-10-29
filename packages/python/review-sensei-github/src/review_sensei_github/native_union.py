"""Closed final native-part union; neither an approval nor a new allowance."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.publication_staging import PublicationReceipt, PublicationStaging

if TYPE_CHECKING:
    from .native_review_census import NativePublicationEvidence
    from .native_review_groups import NativeReviewPlan

UNION_VERSION = "native-review-union-v1"
MAX_UNION_BYTES = 4096
_SHA = re.compile(r"[a-f0-9]{64}")
_GIT_SHA = re.compile(r"[a-f0-9]{40}")


@dataclass(frozen=True)
class NativeUnionPart:
    native_id: str
    content_sha256: str
    finding_count: int

    def __post_init__(self) -> None:
        PublicationReceipt(self.native_id, self.content_sha256)
        if (
            re.fullmatch(r"[1-9][0-9]{0,18}", self.native_id) is None
            or int(self.native_id) > 2**63 - 1
            or type(self.finding_count) is not int
            or not 1 <= self.finding_count <= 32
        ):
            raise ReviewInputError("native union part identity or count is invalid")

    def to_document(self) -> dict[str, object]:
        return {
            "native_id": self.native_id,
            "content_sha256": self.content_sha256,
            "finding_count": self.finding_count,
        }

    @classmethod
    def from_document(cls, value: object) -> NativeUnionPart:
        if not isinstance(value, dict) or set(value) != {
            "native_id",
            "content_sha256",
            "finding_count",
        }:
            raise ReviewInputError("native union part is unsupported")
        return cls(**value)


@dataclass(frozen=True)
class NativeUnionReceipt:
    repository_id: int
    pull_request: int
    producer_id: int
    base_sha: str
    head_sha: str
    transaction_id: str
    result_sha256: str
    plan_sha256: str
    inline_count: int
    parts: tuple[NativeUnionPart, ...]

    def __post_init__(self) -> None:
        if (
            any(
                type(value) is not int or not 1 <= value <= 2**63 - 1
                for value in (self.repository_id, self.pull_request, self.producer_id)
            )
            or self.pull_request > 2**31 - 1
            or any(
                not isinstance(value, str) or _GIT_SHA.fullmatch(value) is None
                for value in (self.head_sha, self.base_sha)
            )
            or any(
                not isinstance(value, str) or _SHA.fullmatch(value) is None
                for value in (self.transaction_id, self.result_sha256, self.plan_sha256)
            )
            or type(self.inline_count) is not int
            or not 0 <= self.inline_count <= 250
            or not isinstance(self.parts, tuple)
            or len(self.parts) > 8
            or any(type(part) is not NativeUnionPart for part in self.parts)
            or sum(part.finding_count for part in self.parts) != self.inline_count
            or len({part.native_id for part in self.parts}) != len(self.parts)
        ):
            raise ReviewInputError("native union scope or complete part set is invalid")
        if len(canonical_bytes(self.to_document())) > MAX_UNION_BYTES:
            raise ReviewInputError("native union exceeds its reserved frame capacity")

    def context(self) -> dict[str, object]:
        return {
            "repository_id": self.repository_id,
            "pull_request": self.pull_request,
            "producer_id": self.producer_id,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "transaction_id": self.transaction_id,
            "result_sha256": self.result_sha256,
            "plan_sha256": self.plan_sha256,
            "inline_count": self.inline_count,
        }

    @property
    def result_digest(self) -> str:
        return hashlib.sha256(
            canonical_bytes(
                {
                    "domain": "reviewsensei:transaction-publication:v1",
                    "transaction_id": self.transaction_id,
                    "result_content_sha256": self.result_sha256,
                }
            )
        ).hexdigest()

    def to_document(self) -> dict[str, object]:
        return {
            "schema_version": UNION_VERSION,
            **self.context(),
            "parts": [part.to_document() for part in self.parts],
        }

    @classmethod
    def from_document(cls, value: object) -> NativeUnionReceipt:
        fields = {
            "schema_version",
            "repository_id",
            "pull_request",
            "producer_id",
            "base_sha",
            "head_sha",
            "transaction_id",
            "result_sha256",
            "plan_sha256",
            "inline_count",
            "parts",
        }
        if (
            not isinstance(value, dict)
            or set(value) != fields
            or value["schema_version"] != UNION_VERSION
            or not isinstance(value["parts"], list)
            or len(value["parts"]) > 8
        ):
            raise ReviewInputError("native union document is unsupported")
        context = {
            key: item
            for key, item in value.items()
            if key not in {"schema_version", "parts"}
        }
        return cls(
            **context,
            parts=tuple(NativeUnionPart.from_document(part) for part in value["parts"]),
        )

    @classmethod
    def from_staging(
        cls, plan: NativeReviewPlan, state: PublicationStaging, *, producer_id: int
    ) -> NativeUnionReceipt:
        if (
            (
                state.transaction_id,
                state.result_sha256,
                state.plan_sha256,
                state.part_count,
            )
            != (plan.transaction_id, plan.result_sha256, plan.sha256, len(plan.groups))
            or state.outstanding_part is not None
            or len(state.receipts) != len(plan.groups)
        ):
            raise ReviewInputError(
                "native union cannot precede complete positive part receipts"
            )
        return cls.from_document(
            {
                "schema_version": UNION_VERSION,
                **plan.context(producer_id),
                "parts": [
                    NativeUnionPart(
                        receipt.native_id, receipt.content_sha256, len(group)
                    ).to_document()
                    for receipt, group in zip(state.receipts, plan.groups, strict=True)
                ],
            }
        )

    def require_evidence(self, evidence: NativePublicationEvidence) -> None:
        from .native_review_census import NativePublicationEvidence

        if (
            type(evidence) is not NativePublicationEvidence
            or evidence.context_sha256
            != hashlib.sha256(canonical_bytes(self.context())).hexdigest()
            or type(evidence.main_review_id) is not int
            or evidence.main_review_id <= 0
            or type(evidence.physical_reads) is not int
            or not 1 <= evidence.physical_reads <= 10
            or len(evidence.groups) != len(self.parts)
        ):
            raise ReviewInputError("native union complete read evidence conflicts")
        seen: set[int] = set()
        for part, group in zip(self.parts, evidence.groups, strict=True):
            if (
                str(group.review_id) != part.native_id
                or group.receipt_digest != part.content_sha256
                or len(group.comment_ids) != part.finding_count
                or any(identifier in seen for identifier in group.comment_ids)
            ):
                raise ReviewInputError("native union part or native membership changed")
            seen.update(group.comment_ids)
        if len(seen) != self.inline_count:
            raise ReviewInputError(
                "native union does not cover the complete inline set"
            )


NativeUnionReader = Callable[[NativeUnionReceipt], "NativePublicationEvidence"]

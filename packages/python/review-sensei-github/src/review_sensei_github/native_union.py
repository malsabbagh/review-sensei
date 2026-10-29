"""Closed final native-part union; neither an approval nor a new allowance."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.publication_staging import PublicationReceipt, PublicationStaging

if TYPE_CHECKING:
    from review_sensei.models import ReviewResult

    from .native_review_census import NativePublicationEvidence
    from .native_review_groups import NativeReviewPlan
    from .publication import FindingIndex

UNION_VERSION = "native-review-union-v1"
MAX_UNION_BYTES = 4096
_SHA = re.compile(r"[a-f0-9]{64}")
_GIT_SHA = re.compile(r"[a-f0-9]{40}")


def native_union_frame_document(
    body: object, receipt: NativeUnionReceipt | None = None
) -> dict:
    """Decode framing only; full original admission and native proof are separate."""
    from .publication import (
        _APPROVAL_ELIGIBILITY_RE,
        APPROVAL_ELIGIBILITY_MARKER_PREFIX,
    )

    if not isinstance(body, str) or body.count(APPROVAL_ELIGIBILITY_MARKER_PREFIX) != 1:
        raise ReviewInputError("native union requires one final eligibility frame")
    match = _APPROVAL_ELIGIBILITY_RE.search(body)
    if match is None:
        raise ReviewInputError("native union final eligibility marker is invalid")
    try:
        encoded = match.group("payload").rstrip("=")
        raw = base64.b64decode(
            (encoded + "=" * (-len(encoded) % 4)).encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
        document = json.loads(raw)
        if (
            not isinstance(document, dict)
            or set(document) != {"schema_version", "current", "native_union"}
            or document["schema_version"] != "8"
            or canonical_bytes(document) != raw
        ):
            raise ValueError("native union frame is unsupported")
        selected = NativeUnionReceipt.from_document(document["native_union"])
        if receipt is not None and selected != receipt:
            raise ValueError("native union frame names different parts")
        return document
    except (ValueError, TypeError, UnicodeError, KeyError) as error:
        raise ReviewInputError(
            "native union final eligibility frame conflicts"
        ) from error


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

    def require_evidence(
        self, evidence: NativePublicationEvidence, *, document_sha256: str | None = None
    ) -> None:
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
            or document_sha256 is not None
            and evidence.document_sha256 != document_sha256
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

    def reconstruct_plan(
        self, result: ReviewResult, index: FindingIndex
    ) -> NativeReviewPlan:
        """Replay exact native bodies from the authenticated original admission.

        The stored index restores the original flags and placement. No diff,
        provider, mutable branch policy or new admission decision is consulted.
        The two finite presentation modes are selected only by the immutable
        complete plan hash; this never changes the retained approval facts.
        """
        from dataclasses import replace

        from review_sensei.context import finding_lifecycle_for_comment
        from review_sensei.human_assessment import finding_instance_fingerprint
        from review_sensei.presentation import (
            assign_finding_identifiers,
            build_finding_view,
        )

        from .native_review_groups import NativeInline, NativeReviewPlan
        from .publication import render_native_inline

        transaction = result.transaction
        if (
            transaction is None
            or transaction.transaction_id != self.transaction_id
            or result.content_digest() != self.result_sha256
            or (transaction.head_sha, transaction.base_sha)
            != (self.head_sha, self.base_sha)
            or index.result_digest != self.result_digest
            or index.base_sha != self.base_sha
        ):
            raise ReviewInputError(
                "native union original result/admission scope conflicts"
            )
        entries = {entry.instance: entry for entry in index.entries}
        if len(entries) != len(index.entries):
            raise ReviewInputError(
                "native union original admission duplicates an instance"
            )
        states = {item.fingerprint: item.state for item in result.finding_lifecycles}
        originals = {}
        instances = [
            finding_instance_fingerprint(comment) for comment in result.comments
        ]
        identifiers = assign_finding_identifiers(instances)
        for comment, instance in zip(result.comments, instances, strict=True):
            fingerprint = finding_lifecycle_for_comment(comment).fingerprint
            if states.get(fingerprint, "new") in {"fixed", "outdated"}:
                continue
            if instance not in originals:
                originals[instance] = (comment, fingerprint)
        if set(originals) != set(entries) or any(
            entries[instance].lifecycle != fingerprint
            for instance, (_, fingerprint) in originals.items()
        ):
            raise ReviewInputError(
                "native union original admission membership conflicts"
            )
        for advisory in (False, True):
            items = []
            for instance, (comment, fingerprint) in originals.items():
                entry = entries[instance]
                if entry.body_placed:
                    continue
                comment = replace(
                    comment,
                    effective_blocking=entry.blocking,
                    needs_human=entry.needs_human,
                )
                if comment.side not in {"LEFT", "RIGHT"} or comment.line is None:
                    raise ReviewInputError(
                        "native union retained inline location is invalid"
                    )
                state = states.get(fingerprint, "new")
                view = replace(
                    build_finding_view(
                        comment,
                        fingerprint=fingerprint,
                        identifier=identifiers[instance],
                        lifecycle_state=state,
                        placement="inline",
                        advisory=advisory,
                        literal_malformed_fences=True,
                    ),
                    repeat=True,
                )
                body = render_native_inline(
                    comment,
                    view=view,
                    result=result,
                    repository_id=self.repository_id,
                    pull_request=self.pull_request,
                    head_sha=self.head_sha,
                    base_sha=self.base_sha,
                    fingerprint=fingerprint,
                    state=state,
                )
                items.append(
                    NativeInline.from_prepared(
                        comment, body, anchor=comment.side.lower()
                    )
                )
            plan = NativeReviewPlan.prepare(
                repository_id=self.repository_id,
                pull_request=self.pull_request,
                base_sha=self.base_sha,
                head_sha=self.head_sha,
                transaction_id=self.transaction_id,
                result_sha256=self.result_sha256,
                items=tuple(items),
            )
            if plan.sha256 == self.plan_sha256 and len(plan.items) == self.inline_count:
                return plan
        raise ReviewInputError("native union complete original rendering changed")


NativeUnionReader = Callable[[NativeUnionReceipt], "NativePublicationEvidence"]

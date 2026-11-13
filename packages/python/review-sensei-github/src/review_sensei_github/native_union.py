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
REUSED_UNION_VERSION = "native-review-union-v2"
MAX_UNION_BYTES = 4096
# The v8 envelope wraps the existing canonical eligibility document. Base64
# growth is at most four bytes per added three input bytes, plus one quantum
# for the old document's alignment. Keep the full validated union ceiling.
_UNION_ENVELOPE_BYTES = (
    len(canonical_bytes({"schema_version": "8", "current": None, "native_union": None}))
    - len(b"null") * 2
)
NATIVE_UNION_FRAME_RESERVE = (
    (MAX_UNION_BYTES + _UNION_ENVELOPE_BYTES + 2) // 3
) * 4 + 4
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
class NativeUnionReusedRoot:
    """Current inline membership on an authenticated existing native root.

    Repository, PR, current head/base and producer are inherited from the union.
    Native review and native commit remain immutable when GitHub edits a body.
    """

    instance: str
    thread_id: str
    comment_id: int
    review_id: int
    native_head_sha: str
    body_sha256: str

    def __post_init__(self) -> None:
        if (
            any(
                type(value) is not int or not 1 <= value <= 2**63 - 1
                for value in (self.comment_id, self.review_id)
            )
            or not isinstance(self.thread_id, str)
            or not self.thread_id
            or len(self.thread_id.encode("utf-8")) > 256
            or self.thread_id.startswith("intent:")
            or any(
                not isinstance(value, str) or _SHA.fullmatch(value) is None
                for value in (self.instance, self.body_sha256)
            )
            or not isinstance(self.native_head_sha, str)
            or _GIT_SHA.fullmatch(self.native_head_sha) is None
        ):
            raise ReviewInputError("native reused-root identity or digest is invalid")

    def to_document(self) -> dict[str, object]:
        return {
            "instance": self.instance,
            "thread_id": self.thread_id,
            "comment_id": self.comment_id,
            "review_id": self.review_id,
            "native_head_sha": self.native_head_sha,
            "body_sha256": self.body_sha256,
        }

    @classmethod
    def from_document(cls, value: object) -> NativeUnionReusedRoot:
        if not isinstance(value, dict) or set(value) != {
            "instance",
            "thread_id",
            "comment_id",
            "review_id",
            "native_head_sha",
            "body_sha256",
        }:
            raise ReviewInputError("native reused-root document is unsupported")
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
    reused_roots: tuple[NativeUnionReusedRoot, ...] = ()

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
            or not isinstance(self.reused_roots, tuple)
            or len(self.reused_roots) > 50
            or any(
                type(root) is not NativeUnionReusedRoot for root in self.reused_roots
            )
            or sum(part.finding_count for part in self.parts) + len(self.reused_roots)
            != self.inline_count
            or len({part.native_id for part in self.parts}) != len(self.parts)
            or len({root.instance for root in self.reused_roots})
            != len(self.reused_roots)
            or len({root.comment_id for root in self.reused_roots})
            != len(self.reused_roots)
            or len({root.thread_id for root in self.reused_roots})
            != len(self.reused_roots)
            or any(
                str(root.review_id) in {part.native_id for part in self.parts}
                for root in self.reused_roots
            )
        ):
            raise ReviewInputError("native union scope or complete part set is invalid")
        if len(canonical_bytes(self.to_document())) > MAX_UNION_BYTES:
            raise ReviewInputError("native union exceeds its reserved frame capacity")

    def context(self) -> dict[str, object]:
        """Existing bounded census context for newly allocated part items.

        Reused-root membership is additionally bound by the complete v2 frame
        digest and exact old-root evidence; it is not a new part allocation.
        """
        return {
            "repository_id": self.repository_id,
            "pull_request": self.pull_request,
            "producer_id": self.producer_id,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "transaction_id": self.transaction_id,
            "result_sha256": self.result_sha256,
            "plan_sha256": self.plan_sha256,
            "inline_count": self.inline_count - len(self.reused_roots),
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
        value = {
            "schema_version": REUSED_UNION_VERSION
            if self.reused_roots
            else UNION_VERSION,
            **self.context(),
            "inline_count": self.inline_count,
            "parts": [part.to_document() for part in self.parts],
        }
        if self.reused_roots:
            value["reused_roots"] = [root.to_document() for root in self.reused_roots]
        return value

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
        reused = (
            isinstance(value, dict)
            and value.get("schema_version") == REUSED_UNION_VERSION
        )
        if reused:
            fields.add("reused_roots")
        if (
            not isinstance(value, dict)
            or set(value) != fields
            or value["schema_version"] not in {UNION_VERSION, REUSED_UNION_VERSION}
            or (
                reused
                and (
                    not isinstance(value["reused_roots"], list)
                    or not 1 <= len(value["reused_roots"]) <= 50
                )
            )
            or not isinstance(value["parts"], list)
            or len(value["parts"]) > 8
        ):
            raise ReviewInputError("native union document is unsupported")
        context = {
            key: item
            for key, item in value.items()
            if key not in {"schema_version", "parts", "reused_roots"}
        }
        return cls(
            **context,
            parts=tuple(NativeUnionPart.from_document(part) for part in value["parts"]),
            reused_roots=tuple(
                NativeUnionReusedRoot.from_document(root)
                for root in value.get("reused_roots", [])
            ),
        )

    @classmethod
    def from_staging(
        cls,
        plan: NativeReviewPlan,
        state: PublicationStaging,
        *,
        producer_id: int,
        reused_roots: tuple[NativeUnionReusedRoot, ...] = (),
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
                "schema_version": REUSED_UNION_VERSION
                if reused_roots
                else UNION_VERSION,
                **plan.context(producer_id),
                "inline_count": len(plan.items) + len(reused_roots),
                **(
                    {"reused_roots": [root.to_document() for root in reused_roots]}
                    if reused_roots
                    else {}
                ),
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
        if self.reused_roots:
            from .publication import (
                finding_instance_from_body,
                finding_publication_scope,
            )

            # The census context intentionally counts only new allocations.
            # Bind the extra membership to the exact authenticated final frame,
            # even when the caller did not supply an additional document hash.
            frame = native_union_frame_document(evidence.main_body, self)
            if (
                evidence.document_sha256
                != hashlib.sha256(canonical_bytes(frame)).hexdigest()
            ):
                raise ReviewInputError(
                    "native union reused-root frame evidence conflicts"
                )
            roots = {root.comment_id: root for root in evidence.roots}
            threads = dict(evidence.thread_ids)
            bodies = {}
            for row in evidence.root_bodies:
                raw_identifier = row.get("n") if isinstance(row, dict) else None
                if type(raw_identifier) is int:
                    identifier = raw_identifier
                elif isinstance(raw_identifier, str) and re.fullmatch(
                    r"[1-9][0-9]*", raw_identifier
                ):
                    identifier = int(raw_identifier)
                else:
                    raise ReviewInputError("native union root body identity is invalid")
                if identifier in bodies:
                    raise ReviewInputError("native union root bodies are duplicated")
                bodies[identifier] = row
            if len(roots) != len(evidence.roots) or len(threads) != len(
                evidence.thread_ids
            ):
                raise ReviewInputError("native union root identities are duplicated")
            for reused in self.reused_roots:
                root = roots.get(reused.comment_id)
                observed = bodies.get(reused.comment_id)
                body = observed.get("b") if isinstance(observed, dict) else None
                scope = finding_publication_scope(body)
                if (
                    reused.comment_id in seen
                    or root is None
                    or not isinstance(observed, dict)
                    or threads.get(reused.comment_id) != reused.thread_id
                    or root.review_id != reused.review_id
                    or root.producer_id != self.producer_id
                    or root.head_sha != reused.native_head_sha
                    or root.resolved
                    or not isinstance(body, str)
                    or hashlib.sha256(body.encode("utf-8")).hexdigest()
                    != reused.body_sha256
                    or finding_instance_from_body(body) != reused.instance
                    or scope is None
                    or scope[:4]
                    != (
                        self.repository_id,
                        self.pull_request,
                        self.head_sha,
                        self.base_sha,
                    )
                    or observed.get("a") != {"t": "Bot", "n": self.producer_id}
                    or observed.get("c") != {"o": reused.native_head_sha}
                    or observed.get("r")
                    not in ({"n": reused.review_id}, {"n": str(reused.review_id)})
                    or observed.get("q") is not None
                    or observed.get("u") != root.updated_at
                ):
                    raise ReviewInputError(
                        "native union reused root membership changed"
                    )
                seen.add(reused.comment_id)
        if len(seen) != self.inline_count:
            raise ReviewInputError(
                "native union does not cover the complete inline set"
            )

    def reconstruct_plan(
        self, result: ReviewResult, index: FindingIndex, *, main_body: str | None = None
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
            render_finding,
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
        reused = {root.instance: root for root in self.reused_roots}
        if not set(reused) <= set(entries) or any(
            entries[instance].body_placed for instance in reused
        ):
            raise ReviewInputError(
                "native union reused roots conflict with original index"
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
            body_complete = True
            for instance, (comment, fingerprint) in originals.items():
                entry = entries[instance]
                comment = replace(
                    comment,
                    effective_blocking=entry.blocking,
                    needs_human=entry.needs_human,
                )
                if not entry.body_placed and (
                    comment.side not in {"LEFT", "RIGHT"} or comment.line is None
                ):
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
                        placement="body" if entry.body_placed else "inline",
                        advisory=advisory,
                        literal_malformed_fences=True,
                    ),
                    repeat=True,
                )
                if entry.body_placed:
                    if main_body is not None and render_finding(view) not in main_body:
                        body_complete = False
                    continue
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
                if instance in reused:
                    if (
                        hashlib.sha256(body.encode("utf-8")).hexdigest()
                        != reused[instance].body_sha256
                    ):
                        body_complete = False
                    continue
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
            if (
                plan.sha256 == self.plan_sha256
                and len(plan.items) + len(reused) == self.inline_count
                and body_complete
            ):
                return plan
        raise ReviewInputError("native union complete original rendering changed")


NativeUnionReader = Callable[[NativeUnionReceipt], "NativePublicationEvidence"]

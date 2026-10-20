"""Explicit v4 readers; root activation requires a separate trusted host."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Mapping, Protocol

from review_sensei.bounded_evidence import (
    EvidenceReadBudget,
    canonical_bytes,
    validate_binding,
    validate_manifest,
)
from review_sensei.errors import ReviewInputError
from review_sensei.human_assessment import HumanInventoryResolution, PendingHumanReview
from review_sensei.human_inventory import inventory_from_result_document

from .prose_publication import CommentReviewProseStore, VisibleProseReceipt

if TYPE_CHECKING:
    from .approval import ReviewApprovalEligibility

PARTITIONED_ELIGIBILITY_VERSION = "4"
MAX_PARTITIONED_ELIGIBILITY_BYTES = 32 * 1024
_MARKER_PREFIX = "<!-- reviewsensei:eligibility:v1"
_ROOT_FIELDS = {
    "schema_version",
    "head_sha",
    "result_digest",
    "facts",
    "immutable_generation",
    "activation_generation",
    "human_inventory",
    "visible_prose",
    "human_resolution",
}


def _hex(value: object, width: int = 64) -> bool:
    return (
        isinstance(value, str)
        and len(value) == width
        and all(character in "0123456789abcdef" for character in value)
    )


class PartitionReader(Protocol):
    def __call__(
        self,
        manifest: dict[str, object],
        *,
        expected_binding: Mapping[str, object],
        budget: EvidenceReadBudget,
    ) -> object: ...


class VisibleReader(Protocol):
    def __call__(
        self, receipts: tuple[VisibleProseReceipt, ...], *, budget: EvidenceReadBudget
    ) -> None: ...


@dataclass(frozen=True)
class PartitionedEligibilityAllocation:
    """Independent host allocation; no numeric root identity or authority."""

    max_root_bytes: int
    framing_bytes: int
    future_lifecycle_bytes: int

    def __post_init__(self) -> None:
        if (
            type(self.max_root_bytes) is not int
            or not 1 <= self.max_root_bytes <= MAX_PARTITIONED_ELIGIBILITY_BYTES
            or any(
                type(value) is not int
                or not 0 <= value <= MAX_PARTITIONED_ELIGIBILITY_BYTES
                for value in (self.framing_bytes, self.future_lifecycle_bytes)
            )
        ):
            raise ReviewInputError("partition eligibility actual allocation is invalid")


@dataclass(frozen=True)
class PartitionedEligibilityContext:
    """Independent authenticated host/result/lifecycle inputs, never root-derived.

    The host authenticates these before construction. This value does not create
    an authentication proof; it makes the trust boundary and shared budget explicit.
    Activation generation is the actual owned root lifecycle observation.
    """

    binding: Mapping[str, object]
    result_digest: str
    activation_generation: int
    owned_root_id: int
    visible_instances: tuple[str, ...]
    budget: EvidenceReadBudget = field(repr=False, compare=False)
    max_root_bytes: int
    framing_bytes: int
    future_lifecycle_bytes: int

    def __post_init__(self) -> None:
        checked = validate_binding(dict(self.binding))
        if (
            checked["purpose"] != "human-inventory"
            or not _hex(self.result_digest)
            or type(self.activation_generation) is not int
            or not 0 <= self.activation_generation <= 2_147_483_647
            or not isinstance(self.visible_instances, tuple)
            or len(self.visible_instances) > 250
            or len(set(self.visible_instances)) != len(self.visible_instances)
            or any(not _hex(item) for item in self.visible_instances)
            or not isinstance(self.budget, EvidenceReadBudget)
            or type(self.owned_root_id) is not int
            or not 0 < self.owned_root_id <= 2**63 - 1
        ):
            raise ReviewInputError("partition eligibility trusted context is invalid")
        PartitionedEligibilityAllocation(
            self.max_root_bytes, self.framing_bytes, self.future_lifecycle_bytes
        )
        object.__setattr__(self, "binding", MappingProxyType(checked))


@dataclass(frozen=True)
class BoundPartitionedEligibilityReader:
    """Explicit operation-local reader; mutable authority is always read fresh.

    The trusted host supplies the independent context for this original source
    operation. Only authenticated content-addressed payloads are reused; latest
    root identity and every complete visible prose read remain live.
    """

    context: PartitionedEligibilityContext
    partition_reader: PartitionReader
    prose_store: CommentReviewProseStore
    expected_root_body: str = field(repr=False)
    _payloads: dict[bytes, object] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self):
        if (
            type(self.context) is not PartitionedEligibilityContext
            or not isinstance(self.expected_root_body, str)
            or len(self.expected_root_body.encode()) > self.context.max_root_bytes
            or not self.expected_root_body
            or not callable(self.partition_reader)
            or type(self.prose_store) is not CommentReviewProseStore
            or self.prose_store.budget is not self.context.budget
            or f"github-bot:{self.prose_store.producer_id}"
            != self.context.binding["producer"]
        ):
            raise ReviewInputError("bound partition reader is invalid")

    def _with_owned_context(self, context, *, expected_root_body):
        if (
            type(context) is not PartitionedEligibilityContext
            or context.budget is not self.context.budget
            or dict(context.binding) != dict(self.context.binding)
            or context.result_digest != self.context.result_digest
            or context.visible_instances != self.context.visible_instances
            or context.activation_generation < self.context.activation_generation
            or (
                context.max_root_bytes,
                context.framing_bytes,
                context.future_lifecycle_bytes,
            )
            != (
                self.context.max_root_bytes,
                self.context.framing_bytes,
                self.context.future_lifecycle_bytes,
            )
        ):
            raise ReviewInputError("bound partition reader owned context changed")
        result = BoundPartitionedEligibilityReader(
            context, self.partition_reader, self.prose_store, expected_root_body
        )
        result._payloads.update(copy.deepcopy(self._payloads))
        return result

    def _visible_from_fresh_listing(self, receipts, *, records, budget):
        if budget is not self.context.budget:
            raise ReviewInputError("bound visible reader original budget changed")
        self.prose_store._revalidate_listing(receipts, records)

    def read(self, manifest, *, expected_binding, budget):
        if budget is not self.context.budget:
            raise ReviewInputError("bound partition reader original budget changed")
        checked = validate_manifest(manifest)
        if checked["binding"] != dict(expected_binding):
            raise ReviewInputError("bound partition reader association changed")
        budget.check()
        key = canonical_bytes(checked)
        if key not in self._payloads:
            value = self.partition_reader(
                checked, expected_binding=expected_binding, budget=budget
            )
            raw = canonical_bytes(value)
            if (
                len(raw) != checked["decoded_bytes"]
                or hashlib.sha256(raw).hexdigest() != checked["sha256"]
            ):
                raise ReviewInputError("bound partition reader foreign payload")
            self._payloads[key] = copy.deepcopy(value)
        budget.check()
        return copy.deepcopy(self._payloads[key])


def visible_result_document(
    receipts: tuple[VisibleProseReceipt, ...],
    *,
    result_digest: str,
    inventory_digest: str,
    expected_instances: tuple[str, ...],
) -> dict[str, object]:
    if (
        not _hex(result_digest)
        or not _hex(inventory_digest)
        or not isinstance(expected_instances, tuple)
        or len(expected_instances) > 250
        or len(set(expected_instances)) != len(expected_instances)
        or any(not _hex(item) for item in expected_instances)
    ):
        raise ReviewInputError("visible result envelope identity is invalid")
    identities = tuple(
        identity for receipt in receipts for identity in receipt.instances
    )
    if (
        not isinstance(receipts, tuple)
        or any(not isinstance(item, VisibleProseReceipt) for item in receipts)
        or len(receipts) > 32
        or len(set(identities)) != len(identities)
        or set(identities) != set(expected_instances)
        or len({item.review_id for item in receipts}) != len(receipts)
        or any(item.index != index for index, item in enumerate(receipts))
        or sum(item.bytes for item in receipts) > 1_048_576
    ):
        raise ReviewInputError("visible result receipt set is incomplete")
    return {
        "schema_version": "1.0",
        "kind": "visible-prose-receipts",
        "result_digest": result_digest,
        "inventory_digest": inventory_digest,
        "finding_count": len(expected_instances),
        "parts": [item.to_dict() for item in receipts],
    }


def _visible_from_document(
    value: object,
    *,
    context: PartitionedEligibilityContext,
    inventory: PendingHumanReview,
) -> tuple[VisibleProseReceipt, ...]:
    fields = {
        "schema_version",
        "kind",
        "result_digest",
        "inventory_digest",
        "finding_count",
        "parts",
    }
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or value["schema_version"] != "1.0"
        or value["kind"] != "visible-prose-receipts"
        or value["result_digest"] != context.result_digest
        or value["inventory_digest"] != inventory.inventory_digest
        or type(value["finding_count"]) is not int
        or value["finding_count"] != len(context.visible_instances)
        or not isinstance(value["parts"], list)
        or len(value["parts"]) > 32
    ):
        raise ReviewInputError("visible result envelope conflicts")
    receipts = tuple(VisibleProseReceipt.from_dict(item) for item in value["parts"])
    if (
        visible_result_document(
            receipts,
            result_digest=context.result_digest,
            inventory_digest=inventory.inventory_digest,
            expected_instances=context.visible_instances,
        )
        != value
        or not {item.fingerprint for item in inventory.findings}.issubset(
            context.visible_instances
        )
        or any(
            item.head_sha != context.binding["head_sha"]
            or f"github-bot:{item.producer_id}" != context.binding["producer"]
            for item in receipts
        )
    ):
        raise ReviewInputError("visible result snapshot or coverage conflicts")
    return receipts


def _preflight(
    value: dict[str, object],
    *,
    context: PartitionedEligibilityContext | PartitionedEligibilityAllocation,
    inventory: PendingHumanReview,
) -> None:
    fully_resolved = HumanInventoryResolution(
        inventory.inventory_digest,
        tuple(item.fingerprint for item in inventory.findings),
    )
    maximum = dict(value, human_resolution=fully_resolved.to_dict())
    maximum["activation_generation"] = 2_147_483_647
    # Worst possible facts width as delayed resolution/diagnostics evolve.
    facts = maximum["facts"]
    assert isinstance(facts, dict)
    maximum["facts"] = dict(facts, has_human_adjudication_findings=False)
    wire = canonical_bytes(maximum)
    marker_bytes = (
        len(_MARKER_PREFIX.encode())
        + 1
        + len(base64.urlsafe_b64encode(wire))
        + len(" -->")
    )
    if (
        marker_bytes + context.framing_bytes + context.future_lifecycle_bytes
        > context.max_root_bytes
    ):
        raise ReviewInputError(
            "partition eligibility exceeds complete future root allocation"
        )


@dataclass(frozen=True)
class PartitionedEligibilityRoot:
    """Immutable read-backed references; serialization grants no host authority."""

    document_bytes: bytes = field(repr=False)
    inventory_digest: str
    context: PartitionedEligibilityContext = field(repr=False, compare=False)

    def to_document(self, eligibility: ReviewApprovalEligibility) -> dict[str, object]:
        value = json.loads(self.document_bytes)
        inventory = eligibility.human_review
        if (
            inventory is None
            or inventory.inventory_digest != self.inventory_digest
            or eligibility.head_sha != value["head_sha"]
            or eligibility.result_digest != value["result_digest"]
        ):
            raise ReviewInputError("partition eligibility immutable authority changed")
        value["facts"] = eligibility.facts.to_dict()
        value["human_resolution"] = HumanInventoryResolution.from_inventory(
            inventory
        ).to_dict()
        _preflight(value, context=self.context, inventory=inventory)
        return value


def parse_partitioned_eligibility(
    value: object,
    *,
    partition_reader: PartitionReader | None,
    expected_context: PartitionedEligibilityContext | None,
    visible_reader: VisibleReader | None,
) -> ReviewApprovalEligibility:
    from .approval import ApprovalFacts, ReviewApprovalEligibility

    if (
        not isinstance(expected_context, PartitionedEligibilityContext)
        or partition_reader is None
        or visible_reader is None
    ):
        raise ReviewInputError(
            "partition eligibility requires authenticated explicit readers"
        )
    context = expected_context
    context.budget.check()
    if (
        not isinstance(value, dict)
        or set(value) != _ROOT_FIELDS
        or value["schema_version"] != PARTITIONED_ELIGIBILITY_VERSION
        or len(canonical_bytes(value)) > MAX_PARTITIONED_ELIGIBILITY_BYTES
        or value["head_sha"] != context.binding["head_sha"]
        or value["result_digest"] != context.result_digest
        or type(value["immutable_generation"]) is not int
        or value["immutable_generation"] != context.binding["generation"]
        or type(value["activation_generation"]) is not int
        or value["activation_generation"] != context.activation_generation
    ):
        raise ReviewInputError(
            "partition eligibility root or trusted lifecycle conflicts"
        )
    manifests = tuple(
        validate_manifest(value[field])
        for field in ("human_inventory", "visible_prose")
    )
    bindings = (dict(context.binding), dict(context.binding, purpose="evidence"))
    for manifest, binding in zip(manifests, bindings, strict=True):
        if manifest["binding"] != binding:
            raise ReviewInputError(
                "partition eligibility manifest association conflicts"
            )
    facts = ApprovalFacts.from_dict(value["facts"])
    resolution = HumanInventoryResolution.from_dict(value["human_resolution"])
    human_count = manifests[0]["item_count"]
    if type(human_count) is not int or not 0 <= human_count <= 250:
        raise ReviewInputError(
            "partition eligibility human count exceeds domain admission"
        )
    # Bound even a malicious unresolved root before performing part reads.
    maximum = dict(
        value,
        human_resolution=dict(resolution.to_dict(), resolved=["f" * 64] * human_count),
    )
    maximum["activation_generation"] = 2_147_483_647
    wire = canonical_bytes(maximum)
    if (
        len(_MARKER_PREFIX.encode())
        + 1
        + len(base64.urlsafe_b64encode(wire))
        + 4
        + context.framing_bytes
        + context.future_lifecycle_bytes
        > context.max_root_bytes
    ):
        raise ReviewInputError("partition eligibility exceeds future root allocation")
    payloads = []
    for manifest, binding in zip(manifests, bindings, strict=True):
        context.budget.check()
        payload = partition_reader(
            manifest, expected_binding=binding, budget=context.budget
        )
        context.budget.check()
        raw = canonical_bytes(payload)
        if (
            len(raw) != manifest["decoded_bytes"]
            or hashlib.sha256(raw).hexdigest() != manifest["sha256"]
        ):
            raise ReviewInputError(
                "partition eligibility resolver returned foreign content"
            )
        payloads.append(payload)
    inventory = inventory_from_result_document(
        payloads[0],
        result_digest=context.result_digest,
        base_sha=str(context.binding["base_sha"]),
    )
    if len(inventory.findings) != manifests[0]["item_count"]:
        raise ReviewInputError("partition eligibility inventory count conflicts")
    inventory = resolution.restore(inventory)
    receipts = _visible_from_document(payloads[1], context=context, inventory=inventory)
    if len(context.visible_instances) != manifests[1][
        "item_count"
    ] or facts.has_human_adjudication_findings != bool(inventory.pending):
        raise ReviewInputError(
            "partition eligibility complete counts or pending facts conflict"
        )
    _preflight(value, context=context, inventory=inventory)
    # The callback performs full current numeric-ID/owner/placement/body GETs;
    # it cannot authorize from a receipt digest alone or refill the budget.
    context.budget.check()
    if visible_reader(receipts, budget=context.budget) is not None:
        raise ReviewInputError(
            "partition eligibility visible reader contract is invalid"
        )
    context.budget.check()
    return ReviewApprovalEligibility(
        head_sha=value["head_sha"],
        result_digest=value["result_digest"],
        facts=facts,
        human_review=inventory,
        partitioned_root=PartitionedEligibilityRoot(
            canonical_bytes(value), inventory.inventory_digest, context
        ),
    )

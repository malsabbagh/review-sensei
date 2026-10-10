"""Complete human inventory through the reviewed authenticated partition codec.

These seams stage or reconstruct evidence. Only a separately authenticated latest
root and operation receipt may activate it. Transport callbacks are uncharged;
this layer charges each physical dispatch and supplies the remaining deadline.
"""

from __future__ import annotations

from typing import Callable, Mapping

from .bounded_evidence import (
    AuthenticatedPart,
    EvidenceReadBudget,
    canonical_bytes,
    read_partitioned_evidence,
    stage_partitioned_evidence,
    validate_binding,
    validate_manifest,
)
from .errors import ReviewInputError
from .human_assessment import PendingHumanReview

HUMAN_INVENTORY_INTERFACE_VERSION = "human-inventory-v1"
PartWriter = Callable[[dict[str, object], float], str]
PartReader = Callable[[str, float], AuthenticatedPart]


def inventory_result_document(
    inventory: PendingHumanReview, *, result_digest: str
) -> dict[str, object]:
    """Result-bound envelope for eligibility v4, distinct from raw prototype v1."""
    if (
        not isinstance(result_digest, str)
        or len(result_digest) != 64
        or any(character not in "0123456789abcdef" for character in result_digest)
    ):
        raise ReviewInputError("human inventory result digest is invalid")
    return {
        "schema_version": "1.0",
        "kind": "human-inventory",
        "result_digest": result_digest,
        "inventory_digest": inventory.inventory_digest,
        "inventory": inventory.inventory_document(),
    }


def inventory_from_result_document(
    value: object, *, result_digest: str, base_sha: str
) -> PendingHumanReview:
    fields = {
        "schema_version",
        "kind",
        "result_digest",
        "inventory_digest",
        "inventory",
    }
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or value["schema_version"] != "1.0"
        or value["kind"] != "human-inventory"
        or value["result_digest"] != result_digest
    ):
        raise ReviewInputError("human inventory result envelope conflicts")
    inventory = PendingHumanReview.from_inventory_document(value["inventory"])
    if (
        inventory.base_sha != base_sha
        or inventory.inventory_digest != value["inventory_digest"]
    ):
        raise ReviewInputError("human inventory result envelope authority conflicts")
    # Validate even an independently supplied expected digest.
    inventory_result_document(inventory, result_digest=result_digest)
    return inventory


def stage_result_human_inventory(
    inventory: PendingHumanReview,
    *,
    result_digest: str,
    binding: Mapping[str, object],
    max_manifest_bytes: int,
    writer: PartWriter,
    reader: PartReader,
    budget: EvidenceReadBudget,
) -> dict[str, object]:
    """Stage the closed v4 envelope without reinterpreting raw v1 manifests."""
    trusted = _binding(binding)
    if inventory.base_sha != trusted["base_sha"]:
        raise ReviewInputError("human inventory base conflicts with trusted binding")
    return stage_partitioned_evidence(
        inventory_result_document(inventory, result_digest=result_digest),
        binding=trusted,
        item_count=len(inventory.findings),
        max_manifest_bytes=max_manifest_bytes,
        writer=lambda document: writer(document, budget.consume()),
        reader=lambda identifier: reader(identifier, budget.consume()),
        budget=budget,
    )


def _binding(value: Mapping[str, object]) -> dict[str, object]:
    binding = validate_binding(dict(value))
    if binding["purpose"] != "human-inventory":
        raise ReviewInputError("human inventory partition purpose conflicts")
    return binding


def stage_human_inventory(
    inventory: PendingHumanReview,
    *,
    binding: Mapping[str, object],
    max_manifest_bytes: int,
    writer: PartWriter,
    reader: PartReader,
    budget: EvidenceReadBudget,
) -> dict[str, object]:
    """Stage the entire immutable inventory, reserving actual root allocation.

    No eligibility marker, check conclusion, resolution or activation is made.
    Partial failures leave retained nonauthoritative parts for reconciliation.
    The binding comes from trusted snapshot/producer state, not staged content.
    """
    trusted = _binding(binding)
    if inventory.base_sha != trusted["base_sha"]:
        raise ReviewInputError("human inventory base conflicts with trusted binding")
    return stage_partitioned_evidence(
        inventory.inventory_document(),
        binding=trusted,
        item_count=len(inventory.findings),
        max_manifest_bytes=max_manifest_bytes,
        writer=lambda document: writer(document, budget.consume()),
        reader=lambda identifier: reader(identifier, budget.consume()),
        budget=budget,
    )


def read_human_inventory(
    manifest: object,
    *,
    expected_binding: Mapping[str, object],
    reader: PartReader,
    budget: EvidenceReadBudget,
) -> PendingHumanReview:
    """Restore all findings only after exact authenticated complete readback."""
    trusted = _binding(expected_binding)
    checked = validate_manifest(manifest)
    value = read_partitioned_evidence(
        checked,
        reader=lambda identifier: reader(identifier, budget.consume()),
        expected_binding=trusted,
        budget=budget,
    )
    inventory = PendingHumanReview.from_inventory_document(value)
    if (
        inventory.base_sha != trusted["base_sha"]
        or len(inventory.findings) != checked["item_count"]
        or inventory.inventory_digest != checked["sha256"]
        or len(canonical_bytes(inventory.inventory_document()))
        != checked["decoded_bytes"]
    ):
        raise ReviewInputError("human inventory complete readback conflicts")
    return inventory

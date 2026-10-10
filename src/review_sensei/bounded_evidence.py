"""Lossless inline evidence; the owning ledger authenticates this envelope.

Decode before parsing with a hard expansion budget. No external objects, URLs,
file access or partial inventories are involved.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import time
import zlib
from dataclasses import dataclass
from typing import Callable, Mapping

from .errors import ReviewInputError

ENCODING = "zlib-json-v1"

# Storage ceilings, independent of the downward-only analysis profiles. The
# framing reserve is charged for every object, including short final parts.
PARTITION_ENCODING = "partitioned-json-v1"
PART_ENCODING = "evidence-part-v1"
MAX_PART_BYTES = 32_768
PART_FRAMING_RESERVE = 512
MAX_PARTS = 32
MAX_PARTITION_BYTES = 1_048_576
MAX_PARTITION_DECODED_BYTES = 2_097_152
MAX_MANIFEST_BYTES = 8_192
MAX_PART_READS = 64
MAX_STORED_PARTS = 256
MAX_STORED_PART_BYTES = 8_388_608
PART_READ_SECONDS = 60.0
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_STORAGE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True)
class AuthenticatedPart:
    """Adapter-checked payload and observed owner, never a payload assertion."""

    document: object
    producer: str


def _integer(value: object, maximum: int, *, minimum: int = 0) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ReviewInputError("partition integer exceeds its contract")
    return value


def _sha(value: object) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ReviewInputError("partition digest is invalid")
    return value


def validate_binding(value: object) -> dict[str, object]:
    """Validate the closed snapshot/owner binding without authenticating it."""
    fields = {
        "repository",
        "repository_id",
        "pull_request",
        "base_sha",
        "head_sha",
        "policy_digest",
        "configuration_digest",
        "generation",
        "producer",
        "purpose",
        "schema_version",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ReviewInputError("partition binding has an invalid shape")
    repository = value["repository"]
    if (
        not isinstance(repository, str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
        or len(repository.encode()) > 256
    ):
        raise ReviewInputError("partition repository is invalid")
    if value["repository_id"] is not None:
        _integer(value["repository_id"], 2_147_483_647, minimum=1)
    _integer(value["pull_request"], 2_147_483_647, minimum=1)
    _integer(value["generation"], 2_147_483_647)
    for field in ("base_sha", "head_sha"):
        if not isinstance(value[field], str) or not re.fullmatch(
            r"[a-f0-9]{40}", value[field]
        ):
            raise ReviewInputError("partition snapshot is invalid")
    for field in ("policy_digest", "configuration_digest"):
        _sha(value[field])
    producer = value["producer"]
    if not isinstance(producer, str) or not re.fullmatch(
        r"(?:local-ledger|github-bot:[1-9][0-9]{0,18})", producer
    ):
        raise ReviewInputError("partition producer is invalid")
    if not isinstance(value["purpose"], str) or value["purpose"] not in {
        "baseline",
        "human-inventory",
        "feedback",
        "evidence",
        "queue",
    }:
        raise ReviewInputError("partition purpose is invalid")
    if value["schema_version"] != "1.0":
        raise ReviewInputError("partition document schema is unsupported")
    return dict(value)


class EvidenceReadBudget:
    """One absolute allowance shared by metadata, parts and final fences.

    Callers pass the same instance throughout an operation. No resolver resets
    its deadline or calls. Four calls are reserved until explicit finalization.
    Transport adapters charge dispatches themselves; the codec only checks time.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        snapshot: object | None = None,
    ) -> None:
        self.clock = clock
        if snapshot is None:
            self.calls = 0
            remaining = PART_READ_SECONDS
            self.wall_deadline_ms = int((wall_clock() + remaining) * 1000)
        else:
            if (
                not isinstance(snapshot, dict)
                or set(snapshot) != {"schema_version", "calls", "deadline_unix_ms"}
                or snapshot["schema_version"] != "1.0"
            ):
                raise ReviewInputError("partition budget snapshot is invalid")
            self.calls = _integer(snapshot["calls"], MAX_PART_READS)
            self.wall_deadline_ms = _integer(
                snapshot["deadline_unix_ms"], 253_402_300_799_999, minimum=1
            )
            remaining = self.wall_deadline_ms / 1000 - wall_clock()
            if not 0 < remaining <= PART_READ_SECONDS:
                raise ReviewInputError(
                    "partition original activation deadline expired or invalid"
                )
        self.deadline = clock() + remaining

    def snapshot(self) -> dict[str, object]:
        """Persist in an authenticated operation receipt before dispatch."""
        return {
            "schema_version": "1.0",
            "calls": self.calls,
            "deadline_unix_ms": self.wall_deadline_ms,
        }

    def check(self) -> None:
        if self.clock() >= self.deadline:
            raise ReviewInputError("partition read deadline exhausted")

    def consume(self, *, fence: bool = False) -> float:
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise ReviewInputError("partition read deadline exhausted")
        if self.calls >= MAX_PART_READS - (0 if fence else 4):
            raise ReviewInputError("partition shared request budget exhausted")
        self.calls += 1
        # Addition/subtraction near a float exponent boundary can round above
        # the transport ceiling. Clamp the timeout, never the original deadline.
        return min(remaining, PART_READ_SECONDS)

    def preflight(self, calls: int) -> None:
        self.check()
        if self.calls + calls > MAX_PART_READS - 4:
            raise ReviewInputError(
                "partition shared request budget cannot stage complete inventory"
            )


def partition_evidence(
    value: object, *, binding: dict[str, object], item_count: int
) -> tuple[dict[str, object], ...]:
    """Preflight the entire canonical inventory before the first storage write."""
    binding = validate_binding(binding)
    _integer(item_count, 4096)
    raw = canonical_bytes(value)
    if not 0 < len(raw) <= MAX_PARTITION_DECODED_BYTES:
        raise ReviewInputError("partition capacity exceeded: decoded bytes")
    packed = zlib.compress(raw, level=9)
    # Fixed conservative payload allocation includes base64 and closed framing.
    chunks = [packed[i : i + 23_500] for i in range(0, len(packed), 23_500)]
    if len(chunks) > MAX_PARTS:
        raise ReviewInputError("partition capacity exceeded: part count")
    inventory = hashlib.sha256(raw).hexdigest()
    binding_digest = hashlib.sha256(canonical_bytes(binding)).hexdigest()
    parts = tuple(
        {
            "encoding": PART_ENCODING,
            "binding_sha256": binding_digest,
            "inventory_sha256": inventory,
            "index": i,
            "count": len(chunks),
            "data": base64.b64encode(chunk).decode("ascii"),
        }
        for i, chunk in enumerate(chunks)
    )
    sizes = [len(canonical_bytes(part)) for part in parts]
    if (
        any(size + PART_FRAMING_RESERVE > MAX_PART_BYTES for size in sizes)
        or sum(sizes) + len(parts) * PART_FRAMING_RESERVE > MAX_PARTITION_BYTES
    ):
        raise ReviewInputError("partition capacity exceeded: framed bytes")
    return parts


def evidence_manifest(
    value: object,
    *,
    binding: dict[str, object],
    item_count: int,
    parts: tuple[dict[str, object], ...],
    storage_ids: tuple[str, ...],
) -> dict[str, object]:
    if len(parts) != len(storage_ids):
        raise ReviewInputError("partition storage identities are incomplete")
    raw = canonical_bytes(value)
    manifest: dict[str, object] = {
        "encoding": PARTITION_ENCODING,
        "binding": dict(binding),
        "item_count": item_count,
        "decoded_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "encoded_bytes": sum(
            len(canonical_bytes(part)) + PART_FRAMING_RESERVE for part in parts
        ),
        "parts": [
            {
                "storage_id": storage_id,
                "bytes": len(canonical_bytes(part)),
                "sha256": hashlib.sha256(canonical_bytes(part)).hexdigest(),
            }
            for storage_id, part in zip(storage_ids, parts)
        ],
    }
    validate_manifest(manifest)
    return manifest


def validate_manifest(value: object) -> dict[str, object]:
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "encoding",
            "binding",
            "item_count",
            "decoded_bytes",
            "sha256",
            "encoded_bytes",
            "parts",
        }
        or value.get("encoding") != PARTITION_ENCODING
    ):
        raise ReviewInputError("partition manifest is unsupported or malformed")
    if len(canonical_bytes(value)) > MAX_MANIFEST_BYTES:
        raise ReviewInputError("partition manifest capacity exceeded")
    validate_binding(value["binding"])
    _integer(value["item_count"], 4096)
    _integer(value["decoded_bytes"], MAX_PARTITION_DECODED_BYTES, minimum=1)
    _integer(value["encoded_bytes"], MAX_PARTITION_BYTES, minimum=1)
    _sha(value["sha256"])
    parts = value["parts"]
    if not isinstance(parts, list) or not 1 <= len(parts) <= MAX_PARTS:
        raise ReviewInputError("partition manifest part count is invalid")
    seen = set()
    total = 0
    for part in parts:
        if not isinstance(part, dict) or set(part) != {"storage_id", "bytes", "sha256"}:
            raise ReviewInputError("partition reference is invalid")
        identity = part["storage_id"]
        if (
            not isinstance(identity, str)
            or not _STORAGE_ID.fullmatch(identity)
            or identity in seen
        ):
            raise ReviewInputError(
                "partition storage identity is invalid or duplicated"
            )
        seen.add(identity)
        total += (
            _integer(part["bytes"], MAX_PART_BYTES - PART_FRAMING_RESERVE, minimum=1)
            + PART_FRAMING_RESERVE
        )
        _sha(part["sha256"])
    if total != value["encoded_bytes"]:
        raise ReviewInputError("partition aggregate length does not match")
    return value


def stage_partitioned_evidence(
    value: object,
    *,
    binding: dict[str, object],
    item_count: int,
    max_manifest_bytes: int,
    writer: Callable[[dict[str, object]], str],
    reader: Callable[[str], AuthenticatedPart],
    budget: EvidenceReadBudget,
) -> dict[str, object]:
    """Preflight every bound, stage immutable objects, then verify all readbacks.

    This does not activate authority. The owner must commit the manifest through
    the existing conditional generation/reload seam. Worst-width IDs and future
    root lifecycle allocation are charged before any write.
    """
    parts = partition_evidence(value, binding=binding, item_count=item_count)
    prospective = evidence_manifest(
        value,
        binding=binding,
        item_count=item_count,
        parts=parts,
        storage_ids=tuple(f"{index:064d}" for index in range(len(parts))),
    )
    if len(canonical_bytes(prospective)) > max_manifest_bytes:
        raise ReviewInputError("partition manifest exceeds actual lifecycle allocation")
    # Creation, readback and preactivation validation plus bounded metadata and
    # root revalidation. Large plans can be legal codec objects but not legal
    # single-operation writes under the shared allowance.
    budget.preflight(4 * len(parts) + 16)
    storage_ids = tuple(writer(part) for part in parts)
    manifest = evidence_manifest(
        value,
        binding=binding,
        item_count=item_count,
        parts=parts,
        storage_ids=storage_ids,
    )
    restored = read_partitioned_evidence(
        manifest, reader=reader, expected_binding=binding, budget=budget
    )
    if canonical_bytes(restored) != canonical_bytes(value):
        raise ReviewInputError("partition readback differs from staged inventory")
    return manifest


def read_partitioned_evidence(
    value: object,
    *,
    reader: Callable[[str], AuthenticatedPart],
    expected_binding: Mapping[str, object],
    budget: EvidenceReadBudget,
) -> object:
    """Resolve only an authenticated root, with one deadline and strict ordering."""
    manifest = validate_manifest(value)
    if manifest["binding"] != dict(expected_binding):
        raise ReviewInputError("partition snapshot or producer does not match")
    binding_digest = hashlib.sha256(canonical_bytes(manifest["binding"])).hexdigest()
    parts = manifest["parts"]
    assert isinstance(parts, list)
    packed = bytearray()
    for index, reference in enumerate(parts):
        budget.check()
        loaded = reader(reference["storage_id"])
        budget.check()
        if (
            not isinstance(loaded, AuthenticatedPart)
            or loaded.producer != expected_binding["producer"]
        ):
            raise ReviewInputError("partition owner authentication failed")
        part = loaded.document
        if not isinstance(part, dict) or set(part) != {
            "encoding",
            "binding_sha256",
            "inventory_sha256",
            "index",
            "count",
            "data",
        }:
            raise ReviewInputError("partition part is malformed")
        data = part["data"]
        if not isinstance(data, str) or len(data) > MAX_PART_BYTES:
            raise ReviewInputError("partition part exceeds its contract")
        wire = canonical_bytes(part)
        if (
            len(wire) != reference["bytes"]
            or hashlib.sha256(wire).hexdigest() != reference["sha256"]
            or part["encoding"] != PART_ENCODING
            or part["binding_sha256"] != binding_digest
            or part["inventory_sha256"] != manifest["sha256"]
            or _integer(part["index"], MAX_PARTS - 1) != index
            or _integer(part["count"], MAX_PARTS, minimum=1) != len(parts)
        ):
            raise ReviewInputError("partition integrity or ordering failed")
        try:
            packed.extend(base64.b64decode(data, validate=True))
        except (ValueError, binascii.Error) as exc:
            raise ReviewInputError("partition data is invalid") from exc
    envelope = {
        "encoding": ENCODING,
        "decoded_bytes": manifest["decoded_bytes"],
        "sha256": manifest["sha256"],
        "data": base64.b64encode(packed).decode("ascii"),
    }
    return decode_evidence(
        envelope,
        max_encoded_bytes=MAX_PARTITION_BYTES,
        max_decoded_bytes=MAX_PARTITION_DECODED_BYTES,
    )


def canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ReviewInputError("evidence is not bounded canonical JSON") from exc


def encode_evidence(value: object, *, max_decoded_bytes: int) -> dict[str, object]:
    raw = canonical_bytes(value)
    if len(raw) > max_decoded_bytes:
        raise ReviewInputError("evidence exceeds the decoded byte bound")
    return {
        "encoding": ENCODING,
        "decoded_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "data": base64.b64encode(zlib.compress(raw, level=9)).decode("ascii"),
    }


def decode_evidence(
    value: object, *, max_encoded_bytes: int, max_decoded_bytes: int
) -> object:
    """Bound canonical envelope bytes and decoded expansion independently.

    The encoded bound includes base64 framing; max_decoded_bytes caps expansion.
    The cheap inner string check avoids serializing an already oversized
    payload; it does not allocate a separate allowance for that payload.
    """
    if not isinstance(value, dict) or set(value) != {
        "encoding",
        "decoded_bytes",
        "sha256",
        "data",
    }:
        raise ReviewInputError("encoded evidence has an invalid shape")
    size = value["decoded_bytes"]
    digest = value["sha256"]
    data = value["data"]
    if (
        value["encoding"] != ENCODING
        or isinstance(size, bool)
        or not isinstance(size, int)
        or not 0 < size <= max_decoded_bytes
        or not isinstance(digest, str)
        or len(digest) != 64
        or not isinstance(data, str)
        or len(data) > max_encoded_bytes
        or len(canonical_bytes(value)) > max_encoded_bytes
    ):
        raise ReviewInputError("encoded evidence exceeds its contract")
    try:
        packed = base64.b64decode(data, validate=True)
        decoder = zlib.decompressobj()
        raw = decoder.decompress(packed, size + 1)
        if (
            len(raw) != size
            or not decoder.eof
            or decoder.unused_data
            or decoder.unconsumed_tail
        ):
            raise ValueError("invalid stream")
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("invalid digest")
        document = json.loads(
            raw,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("nonfinite number")
            ),
        )
        # Reject duplicate keys, noncanonical forms and nonfinite JSON numbers.
        if canonical_bytes(document) != raw:
            raise ValueError("noncanonical document")
        return document
    except (
        ValueError,
        UnicodeError,
        binascii.Error,
        zlib.error,
        RecursionError,
    ) as exc:
        raise ReviewInputError("encoded evidence is invalid") from exc

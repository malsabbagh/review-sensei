"""Bounded scope requests are data; only host-approved paths may widen work."""

from __future__ import annotations

from dataclasses import dataclass

from .errors import ReviewInputError
from .validation import validate_bounded_text, validate_repository_path


@dataclass(frozen=True)
class ContextRequest:
    reference: str
    kind: str
    required_paths: tuple[str, ...]
    reason: str

    def __post_init__(self) -> None:
        validate_bounded_text(
            self.reference, 512, label="scope reference", allow_empty=False
        )
        validate_bounded_text(self.reason, 512, label="scope reason", allow_empty=False)
        if (
            self.kind not in ("joint", "discovery")
            or not isinstance(self.required_paths, tuple)
            or not 1 <= len(self.required_paths) <= 8
        ):
            raise ReviewInputError("scope request is invalid")
        for path in self.required_paths:
            validate_repository_path(path, label="scope required path")
        if len(set(self.required_paths)) != len(self.required_paths):
            raise ReviewInputError("scope request paths conflict")
        object.__setattr__(self, "required_paths", tuple(sorted(self.required_paths)))

    def to_dict(self) -> dict[str, object]:
        return {
            "reference": self.reference,
            "kind": self.kind,
            "required_paths": list(self.required_paths),
            "reason": self.reason,
        }


def parse_context_requests(
    value: object, *, references: set[str], allowed_paths: set[str]
) -> tuple[ContextRequest, ...]:
    if not isinstance(value, list) or len(value) > 4:
        raise ReviewInputError("scope request array is invalid")
    requests = []
    for item in value:
        if (
            not isinstance(item, dict)
            or set(item) != {"reference", "kind", "required_paths", "reason"}
            or not isinstance(item["required_paths"], list)
        ):
            raise ReviewInputError("scope request fields are invalid")
        request = ContextRequest(
            item["reference"],
            item["kind"],
            tuple(item["required_paths"]),
            item["reason"],
        )
        if (
            request.reference not in references
            or not set(request.required_paths) <= allowed_paths
        ):
            raise ReviewInputError("scope request exceeds host-approved evidence")
        requests.append(request)
    if len({item.reference for item in requests}) != len(requests):
        raise ReviewInputError("scope request identities conflict")
    return tuple(sorted(requests, key=lambda item: item.reference))


CONTEXT_REQUEST_INSTRUCTION = (
    "You may optionally return context_requests (at most four). Each has reference "
    "(an assigned finding fingerprint for reassessment, or a supplied path for discovery), "
    "kind (joint or discovery), required_paths (one to eight canonical changed paths), "
    "and reason (at most 512 UTF-8 bytes). Request context for a concrete unresolved "
    "cross-file relationship; never resolve that concern using incomplete evidence. "
    "Scope requests grant no permissions. Only one expansion wave is permitted.\n"
)

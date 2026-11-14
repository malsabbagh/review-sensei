"""Pure proposed staging-v3 declarations; not installed in any ledger writer.

Declarations retain logical liabilities, not physical request debits or positive
receipts. No method sends, retries, settles or grants a native request. Existing
staging-v2 readers intentionally reject this separate version.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from .bounded_evidence import canonical_bytes
from .errors import ReviewInputError
from .publication_staging import MAX_STAGING_BYTES, PublicationReceipt

VERSION = "publication-staging-v3"
PURPOSE = "finite-grouped-declarations"
ROLES = ("part", "manifest", "final")
PHASES = frozenset({"declared", "final-intent", "finished", "held"})
FIELDS = frozenset(
    {
        "schema_version",
        "purpose",
        "transaction_id",
        "result_sha256",
        "plan_sha256",
        "part_count",
        "declarations",
        "phase",
        "part_receipt",
        "manifest_receipt",
        "final_request_sha256",
        "final_receipt",
    }
)


def _digest(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _receipt(value, role):
    if value is None:
        return
    if type(value) is not PublicationReceipt:
        raise ReviewInputError("grouped declaration receipt is invalid")
    native = value.native_id
    if role == "manifest":
        if not native.startswith("issue:"):
            raise ReviewInputError("grouped declaration manifest domain changed")
        native = native[6:]
    if re.fullmatch(r"[1-9][0-9]{0,18}", native) is None or int(native) > 2**63 - 1:
        raise ReviewInputError("grouped declaration native identity exceeds its domain")


@dataclass(frozen=True)
class GroupedDeclarations:
    transaction_id: str
    result_sha256: str
    plan_sha256: str
    tokens: tuple[str, str, str]
    phase: str = "declared"
    part_receipt: PublicationReceipt | None = None
    manifest_receipt: PublicationReceipt | None = None
    final_request_sha256: str | None = None
    final_receipt: PublicationReceipt | None = None

    def __post_init__(self):
        if (
            not all(
                _digest(v)
                for v in (self.transaction_id, self.result_sha256, self.plan_sha256)
            )
            or type(self.tokens) is not tuple
            or len(self.tokens) != 3
            or any(
                not isinstance(v, str) or re.fullmatch(r"[a-f0-9]{32}", v) is None
                for v in self.tokens
            )
            or len(set(self.tokens)) != 3
            or not isinstance(self.phase, str)
            or self.phase not in PHASES
            or self.final_request_sha256 is not None
            and not _digest(self.final_request_sha256)
        ):
            raise ReviewInputError("grouped declaration shape is invalid")
        for role in ROLES:
            _receipt(getattr(self, role + "_receipt"), role)
        if (
            self.manifest_receipt is not None
            and self.part_receipt is None
            or self.final_request_sha256 is not None
            and self.manifest_receipt is None
            or self.final_receipt is not None
            and (
                self.final_request_sha256 is None
                or self.final_receipt.content_sha256 != self.final_request_sha256
                or self.part_receipt is None
                or self.final_receipt.native_id == self.part_receipt.native_id
            )
            or self.phase == "declared"
            and any(
                v is not None
                for v in (
                    self.part_receipt,
                    self.manifest_receipt,
                    self.final_request_sha256,
                    self.final_receipt,
                )
            )
            or self.phase == "final-intent"
            and (
                self.manifest_receipt is None
                or self.final_request_sha256 is None
                or self.final_receipt is not None
            )
            or self.phase == "finished"
            and self.final_receipt is None
        ):
            raise ReviewInputError("grouped declaration receipt ordering is invalid")
        if len(canonical_bytes(self.to_document())) > MAX_STAGING_BYTES:
            raise ReviewInputError(
                "grouped declarations exceed original staging capacity"
            )

    def to_document(self):
        return {
            "schema_version": VERSION,
            "purpose": PURPOSE,
            "transaction_id": self.transaction_id,
            "result_sha256": self.result_sha256,
            "plan_sha256": self.plan_sha256,
            "part_count": 1,
            "declarations": [
                {"role": role, "token": token}
                for role, token in zip(ROLES, self.tokens, strict=True)
            ],
            "phase": self.phase,
            "part_receipt": self.part_receipt.to_document()
            if self.part_receipt
            else None,
            "manifest_receipt": self.manifest_receipt.to_document()
            if self.manifest_receipt
            else None,
            "final_request_sha256": self.final_request_sha256,
            "final_receipt": self.final_receipt.to_document()
            if self.final_receipt
            else None,
        }

    @classmethod
    def from_document(cls, value):
        if (
            not isinstance(value, dict)
            or set(value) != FIELDS
            or value.get("schema_version") != VERSION
            or value.get("purpose") != PURPOSE
            or type(value.get("part_count")) is not int
            or value["part_count"] != 1
            or not isinstance(value.get("declarations"), list)
            or len(value["declarations"]) != 3
        ):
            raise ReviewInputError("grouped declarations version is unsupported")
        tokens = []
        for role, item in zip(ROLES, value["declarations"], strict=True):
            if (
                not isinstance(item, dict)
                or set(item) != {"role", "token"}
                or item["role"] != role
            ):
                raise ReviewInputError("grouped declaration role ordering changed")
            tokens.append(item["token"])
        receipts = {
            role + "_receipt": None
            if value[role + "_receipt"] is None
            else PublicationReceipt.from_document(value[role + "_receipt"])
            for role in ROLES
        }
        return cls(
            value["transaction_id"],
            value["result_sha256"],
            value["plan_sha256"],
            tuple(tokens),
            phase=value["phase"],
            final_request_sha256=value["final_request_sha256"],
            **receipts,
        )

    def prepare_final(self, *, part, manifest, request_sha256):
        if self.phase != "declared":
            raise ReviewInputError(
                "grouped declaration cannot prepare another final intent"
            )
        candidate = replace(
            self,
            phase="final-intent",
            part_receipt=part,
            manifest_receipt=manifest,
            final_request_sha256=request_sha256,
        )
        require_declaration_transition(self, candidate)
        return candidate

    def finish(self, *, final):
        if self.phase != "final-intent":
            raise ReviewInputError("grouped declaration has no exact final intent")
        candidate = replace(self, phase="finished", final_receipt=final)
        require_declaration_transition(self, candidate)
        return candidate

    def hold(self, *, part=None, manifest=None):
        candidate = replace(
            self,
            phase="held",
            part_receipt=part or self.part_receipt,
            manifest_receipt=manifest or self.manifest_receipt,
        )
        require_declaration_transition(self, candidate)
        return candidate

    def cold_status(self):
        # No expiry, missing receipt or declared token is execution/settlement
        # authority. An independently authorized successor protocol is separate.
        return {
            "mode": "read-only",
            "logical_unconfirmed": tuple(
                role for role in ROLES if getattr(self, role + "_receipt") is None
            ),
            "exact_final_intent_present": self.final_request_sha256 is not None,
        }


def require_declaration_transition(before, after):
    if (
        type(before) is not GroupedDeclarations
        or type(after) is not GroupedDeclarations
    ):
        raise ReviewInputError("grouped declaration transition requires typed data")
    if (
        (before.transaction_id, before.result_sha256, before.plan_sha256, before.tokens)
        != (after.transaction_id, after.result_sha256, after.plan_sha256, after.tokens)
        or before.phase in {"held", "finished"}
        and after != before
        or before.phase == "declared"
        and after.phase not in {"declared", "final-intent", "held"}
        or before.phase == "final-intent"
        and after.phase not in {"final-intent", "finished", "held"}
        or before.final_request_sha256 is not None
        and after.final_request_sha256 != before.final_request_sha256
        or any(
            getattr(before, r + "_receipt") is not None
            and getattr(before, r + "_receipt") != getattr(after, r + "_receipt")
            for r in ROLES
        )
    ):
        raise ReviewInputError(
            "grouped declaration transition changes retained liabilities"
        )

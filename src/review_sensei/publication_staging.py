"""Bounded durable publication frontier, independent of any native transport.

Attempt is recorded before dispatch. A receipt is positive exact reconciliation,
never an inference from absence, time, cancellation, or a failed response.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Mapping

if TYPE_CHECKING:
    from datetime import datetime

    from .models import ReviewTransaction
    from .session import SessionIdentity, SessionLedger

from .bounded_evidence import canonical_bytes
from .errors import ReviewInputError

STAGING_VERSION = "publication-staging-v1"
MAX_PUBLICATION_PARTS = 8
MAX_STAGING_BYTES = 4096
_DIGEST = re.compile(r"[a-f0-9]{64}")
_CLAIM = re.compile(r"[a-f0-9]{32}")


@dataclass(frozen=True)
class PublicationReceipt:
    native_id: str
    content_sha256: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.native_id, str)
            or re.fullmatch(r"[A-Za-z0-9_:.=-]{1,64}", self.native_id) is None
            or not isinstance(self.content_sha256, str)
            or _DIGEST.fullmatch(self.content_sha256) is None
        ):
            raise ReviewInputError("publication receipt is invalid")

    def to_document(self) -> dict[str, object]:
        return {"native_id": self.native_id, "content_sha256": self.content_sha256}

    @classmethod
    def from_document(cls, value: object) -> PublicationReceipt:
        if not isinstance(value, dict) or set(value) != {"native_id", "content_sha256"}:
            raise ReviewInputError("publication receipt has an unsupported shape")
        return cls(**value)


@dataclass(frozen=True)
class PublicationStaging:
    transaction_id: str
    result_sha256: str
    plan_sha256: str
    part_count: int
    attempted_parts: int = 0
    attempt_tokens: tuple[str, ...] = ()
    receipts: tuple[PublicationReceipt, ...] = ()
    final_attempted: bool = False
    final_token: str | None = None
    final_receipt: PublicationReceipt | None = None

    def __post_init__(self) -> None:
        if (
            any(
                not isinstance(value, str) or _DIGEST.fullmatch(value) is None
                for value in (self.transaction_id, self.result_sha256, self.plan_sha256)
            )
            or type(self.part_count) is not int
            or not 0 <= self.part_count <= MAX_PUBLICATION_PARTS
            or type(self.attempted_parts) is not int
            or not 0 <= self.attempted_parts <= self.part_count
            or not isinstance(self.attempt_tokens, tuple)
            or len(self.attempt_tokens) != self.attempted_parts
            or any(
                not isinstance(value, str) or _CLAIM.fullmatch(value) is None
                for value in self.attempt_tokens
            )
            or len(set(self.attempt_tokens)) != len(self.attempt_tokens)
            or not isinstance(self.receipts, tuple)
            or any(type(value) is not PublicationReceipt for value in self.receipts)
            or not self.attempted_parts - 1
            <= len(self.receipts)
            <= self.attempted_parts
            or type(self.final_attempted) is not bool
            or self.final_attempted != (self.final_token is not None)
            or self.final_token is not None
            and (
                not isinstance(self.final_token, str)
                or _CLAIM.fullmatch(self.final_token) is None
                or self.final_token in self.attempt_tokens
            )
            or self.final_attempted
            and len(self.receipts) != self.part_count
            or self.final_receipt is not None
            and (
                type(self.final_receipt) is not PublicationReceipt
                or not self.final_attempted
            )
        ):
            raise ReviewInputError("publication staging frontier is invalid")
        native_ids = [receipt.native_id for receipt in self.receipts]
        if self.final_receipt is not None:
            native_ids.append(self.final_receipt.native_id)
        if len(native_ids) != len(set(native_ids)):
            raise ReviewInputError("publication staging reuses a native identity")
        if len(canonical_bytes(self.to_document())) > MAX_STAGING_BYTES:
            raise ReviewInputError("publication staging exceeds its reserved capacity")

    @classmethod
    def prepare(
        cls,
        *,
        transaction_id: str,
        result_sha256: str,
        plan_sha256: str,
        part_count: int,
    ) -> PublicationStaging:
        return cls(transaction_id, result_sha256, plan_sha256, part_count)

    @property
    def outstanding_part(self) -> int | None:
        return len(self.receipts) if len(self.receipts) < self.attempted_parts else None

    @property
    def complete(self) -> bool:
        return self.final_receipt is not None

    def attempt_part(
        self, ordinal: int, *, token: str | None = None
    ) -> PublicationStaging:
        if (
            type(ordinal) is not int
            or ordinal != self.attempted_parts
            or ordinal >= self.part_count
            or self.outstanding_part is not None
            or self.final_attempted
        ):
            raise ReviewInputError(
                "publication part is already attempted or out of order"
            )
        return replace(
            self,
            attempted_parts=self.attempted_parts + 1,
            attempt_tokens=(
                *self.attempt_tokens,
                secrets.token_hex(16) if token is None else token,
            ),
        )

    def confirm_part(
        self, ordinal: int, *, native_id: str, content_sha256: str
    ) -> PublicationStaging:
        receipt = PublicationReceipt(native_id, content_sha256)
        if type(ordinal) is not int or not 0 <= ordinal < self.attempted_parts:
            raise ReviewInputError("publication part was not attempted")
        if ordinal < len(self.receipts):
            if self.receipts[ordinal] != receipt:
                raise ReviewInputError(
                    "publication receipt conflicts with its immutable settlement"
                )
            return self
        return replace(self, receipts=(*self.receipts, receipt))

    def attempt_final(self, *, token: str | None = None) -> PublicationStaging:
        if self.final_attempted or len(self.receipts) != self.part_count:
            raise ReviewInputError(
                "publication final frame is already attempted or parts are unconfirmed"
            )
        return replace(
            self,
            final_attempted=True,
            final_token=secrets.token_hex(16) if token is None else token,
        )

    def confirm_final(
        self, *, native_id: str, content_sha256: str
    ) -> PublicationStaging:
        receipt = PublicationReceipt(native_id, content_sha256)
        if not self.final_attempted:
            raise ReviewInputError("publication final frame was not attempted")
        if self.final_receipt is not None:
            if self.final_receipt != receipt:
                raise ReviewInputError("publication final receipt conflicts")
            return self
        return replace(self, final_receipt=receipt)

    def require_successor(self, after: PublicationStaging) -> None:
        """Each durable transition crosses at most one effect frontier."""
        if self == after:
            return
        candidates = []
        if (
            self.outstanding_part is None
            and not self.final_attempted
            and self.attempted_parts < self.part_count
            and len(after.attempt_tokens) == self.attempted_parts + 1
        ):
            candidates.append(
                self.attempt_part(self.attempted_parts, token=after.attempt_tokens[-1])
            )
        if (
            self.outstanding_part is not None
            and len(after.receipts) == len(self.receipts) + 1
        ):
            receipt = after.receipts[-1]
            candidates.append(
                self.confirm_part(
                    self.outstanding_part,
                    native_id=receipt.native_id,
                    content_sha256=receipt.content_sha256,
                )
            )
        if (
            len(self.receipts) == self.part_count
            and not self.final_attempted
            and after.final_token is not None
        ):
            candidates.append(self.attempt_final(token=after.final_token))
        if (
            self.final_attempted
            and self.final_receipt is None
            and after.final_receipt is not None
        ):
            receipt = after.final_receipt
            candidates.append(
                self.confirm_final(
                    native_id=receipt.native_id, content_sha256=receipt.content_sha256
                )
            )
        if after not in candidates:
            raise ReviewInputError(
                "publication staging changed its plan, receipt, or durable frontier"
            )

    def to_document(self) -> dict[str, object]:
        return {
            "schema_version": STAGING_VERSION,
            "transaction_id": self.transaction_id,
            "result_sha256": self.result_sha256,
            "plan_sha256": self.plan_sha256,
            "part_count": self.part_count,
            "attempted_parts": self.attempted_parts,
            "attempt_tokens": list(self.attempt_tokens),
            "receipts": [value.to_document() for value in self.receipts],
            "final_attempted": self.final_attempted,
            "final_token": self.final_token,
            "final_receipt": self.final_receipt.to_document()
            if self.final_receipt is not None
            else None,
        }

    @classmethod
    def from_document(cls, value: object) -> PublicationStaging:
        fields = {
            "schema_version",
            "transaction_id",
            "result_sha256",
            "plan_sha256",
            "part_count",
            "attempted_parts",
            "attempt_tokens",
            "receipts",
            "final_attempted",
            "final_token",
            "final_receipt",
        }
        if (
            not isinstance(value, Mapping)
            or set(value) != fields
            or value["schema_version"] != STAGING_VERSION
            or not isinstance(value["receipts"], list)
            or not isinstance(value["attempt_tokens"], list)
            or len(value["receipts"]) > MAX_PUBLICATION_PARTS
        ):
            raise ReviewInputError("publication staging document is unsupported")
        return cls(
            transaction_id=value["transaction_id"],  # type: ignore[arg-type]
            result_sha256=value["result_sha256"],  # type: ignore[arg-type]
            plan_sha256=value["plan_sha256"],  # type: ignore[arg-type]
            part_count=value["part_count"],  # type: ignore[arg-type]
            attempted_parts=value["attempted_parts"],  # type: ignore[arg-type]
            attempt_tokens=tuple(value["attempt_tokens"]),
            receipts=tuple(
                PublicationReceipt.from_document(item) for item in value["receipts"]
            ),
            final_attempted=value["final_attempted"],  # type: ignore[arg-type]
            final_token=value["final_token"],  # type: ignore[arg-type]
            final_receipt=PublicationReceipt.from_document(value["final_receipt"])
            if value["final_receipt"] is not None
            else None,
        )


def normalize_staging(value: object) -> dict[str, object] | None:
    return (
        None if value is None else PublicationStaging.from_document(value).to_document()
    )


def require_staging_successor(before: object, after: object) -> None:
    old = None if before is None else PublicationStaging.from_document(before)
    new = None if after is None else PublicationStaging.from_document(after)
    if old == new:
        return
    if new is None:
        raise ReviewInputError("publication staging cannot be silently discarded")
    if old is None or old.complete and old.transaction_id != new.transaction_id:
        if new != PublicationStaging.prepare(
            transaction_id=new.transaction_id,
            result_sha256=new.result_sha256,
            plan_sha256=new.plan_sha256,
            part_count=new.part_count,
        ):
            raise ReviewInputError(
                "publication staging must begin before its first attempt"
            )
        return
    old.require_successor(new)


def checkpoint_publication_staging(
    ledger: SessionLedger,
    identity: SessionIdentity,
    transaction: ReviewTransaction,
    *,
    expected: PublicationStaging | None,
    after: PublicationStaging,
    now: datetime | None = None,
) -> PublicationStaging:
    """CAS the original transaction's frontier without new work or allowance.

    A host still owns the authorization and fresh native fences. This function
    provides no native write capability and never acknowledges a host effect.
    """
    from .session import MAX_GENERATION, SessionRecord

    def mutate(record: SessionRecord) -> SessionRecord:
        current = record.transaction
        if (
            current is None
            or not current.logical_identity_matches(transaction)
            or current.phase not in {"publication_pending", "publication_failed"}
            or after.transaction_id != current.transaction_id
            or after.result_sha256 != current.result_sha256
            or record.generation >= MAX_GENERATION
        ):
            raise ReviewInputError(
                "publication staging transaction is stale or conflicting"
            )
        retained = (
            None
            if record.publication_staging is None
            else PublicationStaging.from_document(record.publication_staging)
        )
        if retained == after:
            return record
        if retained != expected:
            raise ReviewInputError("publication staging expected frontier changed")
        require_staging_successor(record.publication_staging, after.to_document())
        return record.evolve(
            now=now,
            generation=record.generation + 1,
            publication_staging=after.to_document(),
        )

    retained = ledger.replace(identity, mutate, now=now)
    return PublicationStaging.from_document(retained.publication_staging)

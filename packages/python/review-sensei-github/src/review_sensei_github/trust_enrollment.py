"""Source-issued current observations and immutable original enrollment data.

No receipt survives process restart. Cold state is historical data authenticated
by the existing native session reader, never a live permission or writer grant.
"""

from __future__ import annotations

import hashlib
import json
import time
import weakref
from dataclasses import dataclass
from typing import Any

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.trust_enrollment import (
    DOCUMENT_VERSION,
    VERSION,
    normalize_reference,
)

from .run_authority import parse_authority


@dataclass(frozen=True, eq=False)
class _Observation:
    broker: object
    session: Any
    document: bytes


_ISSUED: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _record_session_observation(broker, session):
    """Called only by the successful native broker exchange producer."""
    authority = session.attestation
    if authority is None or authority.get("version") != 4:
        return session
    authority = parse_authority(authority, grant=True)
    document = canonical_bytes(
        {
            "schema_version": DOCUMENT_VERSION,
            "app_id": session.app_id,
            "authority": authority,
        }
    )
    receipt = _Observation(broker, session, document)
    _ISSUED[receipt] = True
    # Session object equality or caller-supplied attestation is never issuance.
    object.__setattr__(session, "_trust_observation", receipt)
    return session


def current_observation(broker, session):
    receipt = getattr(session, "_trust_observation", None)
    if (
        type(receipt) is not _Observation
        or receipt not in _ISSUED
        or receipt.broker is not broker
        or receipt.session is not session
    ):
        raise ReviewInputError("trust current observation requires source issuance")
    document = json.loads(receipt.document)
    authority = parse_authority(document["authority"], grant=True)
    if not authority["issued_at"] <= int(time.time()) < authority["expires_at"]:
        raise ReviewInputError("trust current observation is expired")
    if authority != session.attestation or document["app_id"] != session.app_id:
        raise ReviewInputError("trust current observation changed")
    return document


def prepare_original_enrollment(broker, session, record):
    """Pure complete root-size preflight; no write, census or new allowance."""
    if record.trust_enrollment is not None:
        raise ReviewInputError("original trust enrollment already exists")
    document = {**current_observation(broker, session), "generation": record.generation}
    authority = document["authority"]
    if (
        authority["repository"] != record.repository
        or authority["repository_id"] != record.repository_id
        or authority["pull_request"] != record.pull_request
    ):
        raise ReviewInputError("trust enrollment source scope conflicts")
    reference = normalize_reference(
        {
            "schema_version": VERSION,
            "document": document,
            "sha256": hashlib.sha256(canonical_bytes(document)).hexdigest(),
        }
    )
    return record.evolve(generation=record.generation, trust_enrollment=reference)


def original_observation(record):
    """Decode historical facts only; native source authentication is separate."""
    reference = normalize_reference(record.trust_enrollment)
    if reference is None:
        raise ReviewInputError("trust original enrollment unavailable legacy state")
    document = reference["document"]
    parse_authority(document["authority"], grant=True)
    return document


def compare_current_observation(broker, session, record):
    """Compare observed endpoints, never infer continuous permission history."""
    original, current = (
        original_observation(record),
        current_observation(broker, session),
    )
    before, after = original["authority"], current["authority"]
    if any(
        before[key] != after[key]
        for key in ("repository_id", "repository", "pull_request")
    ):
        raise ReviewInputError("trust current repository identity changed")
    old, new = before["current_observation"], after["current_observation"]
    if original["app_id"] != current["app_id"] or any(
        old[key] != new[key] for key in ("repository", "installation", "capability")
    ):
        raise ReviewInputError(
            "trust current installation or repository observation changed"
        )
    # Actor identity may legitimately change between runs. Association is not a
    # repository permission. This comparison grants no actor/effect authority.
    return {
        "original_sha256": hashlib.sha256(canonical_bytes(original)).hexdigest(),
        "current_sha256": hashlib.sha256(canonical_bytes(current)).hexdigest(),
        "qualification": "current_observations_only",
        "continuous_history": "unavailable",
        "actor_permission": "requires_current_authenticated_lookup",
    }


@dataclass(frozen=True, eq=False)
class _RestoredEnrollment:
    ledger: object
    record: Any
    producer: str


_RESTORED: weakref.WeakSet[_RestoredEnrollment] = weakref.WeakSet()


def load_original_enrollment(ledger, identity, *, now=None):
    """Use the ordinary bounded native load once; do not accept supplied roots.

    Intended to replace, not follow, the admission's existing session load.
    Its caller must retain the original read allowance and terminal reservations.
    Missing legacy enrollment remains explicit and issues no historical proof.
    """
    from .api_run_requests import ApiRunSessionLedger
    from .session_ledger import GitHubIssueCommentSessionLedger

    if (
        type(ledger) not in {GitHubIssueCommentSessionLedger, ApiRunSessionLedger}
        or ledger.app_slug is None
        or ledger.evidence_budget is None
    ):
        raise ReviewInputError("trust cold restore requires bounded native source")
    loaded = ledger.load(identity, now=now)
    if loaded.status not in {"ok", "migrated"} or loaded.record is None:
        raise ReviewInputError("trust cold original root unavailable")
    if loaded.record.trust_enrollment is None:
        return loaded, None
    original_observation(loaded.record)
    receipt = _RestoredEnrollment(ledger, loaded.record, ledger.evidence_producer())
    _RESTORED.add(receipt)
    return loaded, receipt


def compare_restored_observation(broker, session, restored):
    if type(restored) is not _RestoredEnrollment or restored not in _RESTORED:
        raise ReviewInputError(
            "trust original enrollment requires native source restore"
        )
    return compare_current_observation(broker, session, restored.record)

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
from functools import wraps
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


_SOURCE_INSTALLED = False
_observation_lookup = None
_refresh_observation = None


def _install_observation_source(source_class):
    """One-time binding; only the captured real exchange can issue a receipt."""
    global _SOURCE_INSTALLED, _observation_lookup, _refresh_observation
    if _SOURCE_INSTALLED:
        raise ReviewInputError("trust observation source already installed")
    from .broker_client import BrokerClient

    if source_class is not BrokerClient:
        raise ReviewInputError("trust observation source class is unsupported")
    source, post = source_class.open_session, source_class._post
    refresh = source_class.refresh_session_observation
    issued: weakref.WeakSet[_Observation] = weakref.WeakSet()

    @wraps(source)
    def exchange(self, *args, **kwargs):
        observed = self.authority is not None and self.authority.get("version") == 4
        if observed and (
            type(self) is not source_class
            or getattr(self.open_session, "__func__", None) is not exchange
            or getattr(self._post, "__func__", None) is not post
            or source_class.open_session is not exchange
            or source_class._post is not post
        ):
            raise ReviewInputError("trust observation source dispatch changed")
        session = source(self, *args, **kwargs)
        if not observed:
            return session
        authority = parse_authority(session.attestation, grant=True)
        if authority["version"] != 4:
            raise ReviewInputError("trust observation source downgraded")
        document = canonical_bytes(
            {
                "schema_version": DOCUMENT_VERSION,
                "app_id": session.app_id,
                "authority": authority,
            }
        )
        receipt = _Observation(self, session, document)
        issued.add(receipt)
        object.__setattr__(session, "_trust_observation", receipt)
        return session

    def lookup(broker, session):
        receipt = getattr(session, "_trust_observation", None)
        if (
            type(receipt) is not _Observation
            or receipt not in issued
            or receipt.broker is not broker
            or receipt.session is not session
        ):
            raise ReviewInputError("trust current observation requires source issuance")
        return receipt

    def refresh_source(broker, session, effect_sha256):
        current_observation(broker, session)
        if (
            type(broker) is not source_class
            or getattr(broker.open_session, "__func__", None) is not exchange
            or getattr(broker._post, "__func__", None) is not post
            or getattr(broker.refresh_session_observation, "__func__", None)
            is not refresh
            or source_class.open_session is not exchange
            or source_class._post is not post
            or source_class.refresh_session_observation is not refresh
        ):
            raise ReviewInputError("trust refresh source dispatch changed")
        result = refresh(broker, session.grant, session.attestation, effect_sha256)
        current_observation(broker, session)
        return result

    _refresh_observation = refresh_source
    _SOURCE_INSTALLED = True
    _observation_lookup = lookup
    setattr(source_class, "open_session", exchange)


def current_observation(broker, session):
    if _observation_lookup is None:
        raise ReviewInputError("trust observation source is unavailable")
    receipt = _observation_lookup(broker, session)
    document = json.loads(receipt.document)
    authority = parse_authority(document["authority"], grant=True)
    if not authority["issued_at"] <= int(time.time()) < authority["expires_at"]:
        raise ReviewInputError("trust current observation is expired")
    if authority != session.attestation or document["app_id"] != session.app_id:
        raise ReviewInputError("trust current observation changed")
    return document


def refresh_current_observation(broker, session, effect_sha256):
    if _refresh_observation is None:
        raise ReviewInputError("trust refresh source is unavailable")
    return _refresh_observation(broker, session, effect_sha256)


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
    ledger: Any
    record_bytes: bytes
    enrollment_bytes: bytes
    root: str
    producer: str


_native_sources: dict[str, Any] = {}


def _install_native_source(source_class):
    """Pin each known ordinary source once during its defining module import."""
    role = source_class.__name__
    if role in _native_sources:
        raise ReviewInputError("trust native source already installed")
    expected: Any
    if role == "GitHubIssueCommentSessionLedger":
        from .session_ledger import GitHubIssueCommentSessionLedger

        expected = GitHubIssueCommentSessionLedger
    elif role == "StartupRunLedger":
        from .review_run import StartupRunLedger

        expected = StartupRunLedger
    else:
        raise ReviewInputError("trust native source class is unsupported")
    if source_class is not expected:
        raise ReviewInputError("trust native source class is unsupported")
    methods = {
        name: getattr(source_class, name)
        for name in dir(source_class)
        if not name.startswith("__") and callable(getattr(source_class, name))
    }
    issued: weakref.WeakSet[_RestoredEnrollment] = weakref.WeakSet()

    def guard(ledger):
        if type(ledger) is not source_class or any(
            name in ledger.__dict__ or getattr(source_class, name) is not method
            for name, method in methods.items()
        ):
            raise ReviewInputError("trust native source implementation changed")
        if ledger.app_slug is None or ledger.evidence_budget is None:
            raise ReviewInputError("trust cold restore requires bounded native source")

    def load(ledger, identity, now):
        guard(ledger)
        loaded = methods["load"](ledger, identity, now=now)
        guard(ledger)
        if loaded.status not in {"ok", "migrated"} or loaded.record is None:
            return loaded, None
        if loaded.record.trust_enrollment is None:
            return loaded, None
        original_observation(loaded.record)
        receipt = _RestoredEnrollment(
            ledger,
            canonical_bytes(loaded.record.to_dict()),
            canonical_bytes(loaded.record.trust_enrollment),
            loaded.record.record_sha256,
            ledger.evidence_producer(),
        )
        issued.add(receipt)
        return loaded, receipt

    def restore(receipt):
        from review_sensei.session import SessionRecord

        if type(receipt) is not _RestoredEnrollment or receipt not in issued:
            raise ReviewInputError(
                "trust original enrollment requires native source restore"
            )
        guard(receipt.ledger)
        if receipt.ledger.evidence_producer() != receipt.producer:
            raise ReviewInputError("trust original native producer changed")
        record = SessionRecord.from_dict(json.loads(receipt.record_bytes))
        if (
            record.record_sha256 != receipt.root
            or canonical_bytes(record.trust_enrollment) != receipt.enrollment_bytes
        ):
            raise ReviewInputError("trust original receipt commitment changed")
        return record

    _native_sources[role] = (source_class, load, restore)


def load_original_enrollment(ledger, identity, *, now=None):
    """Replace the ordinary bounded native load; do not add a second census."""
    source = _native_sources.get(type(ledger).__name__)
    if source is None or source[0] is not type(ledger):
        raise ReviewInputError("trust cold restore requires bounded native source")
    return source[1](ledger, identity, now)


def compare_restored_observation(broker, session, restored):
    if type(restored) is not _RestoredEnrollment:
        raise ReviewInputError(
            "trust original enrollment requires native source restore"
        )
    source = _native_sources.get(type(restored.ledger).__name__)
    if source is None:
        raise ReviewInputError(
            "trust original enrollment requires native source restore"
        )
    return compare_current_observation(broker, session, source[2](restored))

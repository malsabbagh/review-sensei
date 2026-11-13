"""Closed, default-off source binding for freshly observed native mutations.

The profile is a pricing subject, never a replacement budget or durable grant.
Only actual synchronous broker refresh can create a one-use dispatch receipt.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import wraps
from typing import Any
from urllib.parse import urlsplit
from weakref import WeakKeyDictionary

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError

from .trust_enrollment import (
    current_observation,
    original_observation,
    refresh_current_observation,
    require_native_implementation,
)


@dataclass(frozen=True, eq=False)
class _Profile:
    caller: object
    broker: object
    session: object
    original: bytes
    grant_sha256: str
    api_origin: str
    broker_url: str
    native_opener: object


_source = None


def _unavailable(*args, **kwargs):
    raise ReviewInputError("trust effect source registration is unavailable")


enable = verification_profile = verification_calls = before_request = consume = (
    require_native_attempt
) = _unavailable


def _install_transport_source(source_class):
    global \
        _source, \
        enable, \
        verification_profile, \
        verification_calls, \
        before_request, \
        consume, \
        require_native_attempt
    from .api_run_requests import ApiRunRequestTransport

    if _source is not None or source_class is not ApiRunRequestTransport:
        raise ReviewInputError("trust effect transport source registration is invalid")
    _source = (source_class, source_class.open)
    source = _source
    _profiles: WeakKeyDictionary = WeakKeyDictionary()
    _pending: WeakKeyDictionary = WeakKeyDictionary()
    _receipts: WeakKeyDictionary = WeakKeyDictionary()
    _attempts: WeakKeyDictionary = WeakKeyDictionary()
    _admitted_routes: WeakKeyDictionary = WeakKeyDictionary()
    _initializing: WeakKeyDictionary = WeakKeyDictionary()
    initialize_source = source_class.initialize_and_activate
    require_ledger_source = source_class._require_ledger

    @wraps(initialize_source)
    def initialize(self, ledger, identity, configuration, owner, **kwargs):
        profile = verification_profile(self)
        if profile is None:
            return initialize_source(
                self, ledger, identity, configuration, owner, **kwargs
            )
        if (
            source_class.initialize_and_activate is not initialize
            or getattr(self.initialize_and_activate, "__func__", None) is not initialize
            or kwargs.get("trust_source") != (profile.broker, profile.session)
            or self in _initializing
            or self._state is not None
            or ledger.token != profile.session.token
            or ledger.authenticated_app_id != json.loads(profile.original)["app_id"]
            or ledger.http.api_url != profile.api_origin
        ):
            raise ReviewInputError("trust initialization source binding is unavailable")
        require_native_implementation(ledger)
        require_ledger_source(self, ledger, identity)
        _initializing[self] = (ledger, identity)
        try:
            return initialize_source(
                self, ledger, identity, configuration, owner, **kwargs
            )
        finally:
            _initializing.pop(self, None)

    setattr(source_class, "initialize_and_activate", initialize)

    def bound_to(opener, owner):
        return (
            getattr(opener, "__self__", None) is owner
            and getattr(opener, "__func__", None) is source[1]
        )

    def enable(caller: Any, broker, session, *, api_url):
        """Install source mode after admission checks, before the first native write.

        This only establishes the quote subject. Actual route admission remains with
        the source-priced lifecycle producers; unsupported routes must refuse there.
        """
        if caller is None or type(caller) is not source[0] or caller in _profiles:
            raise ReviewInputError(
                "trust effect source is unsupported or already installed"
            )
        document = current_observation(broker, session)
        authority = document["authority"]
        observation = authority["current_observation"]
        if (
            observation["schema_version"] != "current-observation-v2"
            or not bound_to(broker.opener, caller)
            or authority["repository_id"] != caller.run.repository_id
            or authority["run_id"] != str(caller.run.run_id)
            or authority["run_attempt"] != caller.attempt
            or authority["job"] != caller.job
            or getattr(caller, "_controlled_restart_admission", None) is not None
            or caller._physical_mutation_ids - caller._confirmed_control_writes.keys()
        ):
            raise ReviewInputError("trust effect original source scope is unavailable")
        url = urlsplit(api_url)
        if (
            url.scheme != "https"
            or not url.netloc
            or url.path
            or url.query
            or url.fragment
        ):
            raise ReviewInputError("trust effect native origin is invalid")
        profile = _Profile(
            caller,
            broker,
            session,
            canonical_bytes(document),
            hashlib.sha256(session.grant.encode()).hexdigest(),
            api_url,
            broker.broker_url,
            caller._opener,
        )
        _profiles[caller] = profile
        return verification_profile(caller)

    def verification_profile(caller):
        profile = _profiles.get(caller)
        if profile is None:
            return None
        if (
            _source is None
            or type(caller) is not source[0]
            or source[0].open is not source[1]
            or source[0].initialize_and_activate is not initialize
            or source[0]._require_ledger is not require_ledger_source
            or getattr(caller.open, "__func__", None) is not source[1]
            or canonical_bytes(current_observation(profile.broker, profile.session))
            != profile.original
            or hashlib.sha256(profile.session.grant.encode()).hexdigest()
            != profile.grant_sha256
        ):
            raise ReviewInputError("trust effect source binding changed")
        authority = json.loads(profile.original)["authority"]
        if (
            authority["repository_id"] != caller.run.repository_id
            or authority["run_id"] != str(caller.run.run_id)
            or authority["run_attempt"] != caller.attempt
            or authority["job"] != caller.job
            or profile.broker.broker_url != profile.broker_url
            or not bound_to(profile.broker.opener, caller)
            or caller._opener is not profile.native_opener
        ):
            raise ReviewInputError("trust effect current caller source changed")
        return profile

    def verification_calls(caller):
        return int(verification_profile(caller) is not None)

    def _binding(profile, request, *, sequence=None):
        # No token bytes are retained or sent. Conditional headers and exact bytes,
        # including GraphQL operation/variables, are part of the effect identity.
        return canonical_bytes(
            {
                "schema_version": "native-trust-effect-v1",
                "method": request.get_method(),
                "url": request.full_url,
                "body_sha256": hashlib.sha256(request.data or b"").hexdigest(),
                "authorization_sha256": hashlib.sha256(
                    str(request.get_header("Authorization") or "").encode("utf-8")
                ).hexdigest(),
                "content_type": request.get_header("Content-type"),
                "accept": request.get_header("Accept"),
                "api_version": request.get_header("X-github-api-version"),
                "if_match": request.get_header("If-match"),
                "if_none_match": request.get_header("If-none-match"),
                "original_sha256": hashlib.sha256(profile.original).hexdigest(),
                "grant_sha256": profile.grant_sha256,
                "sequence": profile.caller._sequence + 1
                if sequence is None
                else sequence,
            }
        )

    def before_request(caller, request):
        """Return true only for the single nested, source-owned verification POST."""
        profile = verification_profile(caller)
        if profile is None:
            return False
        pending = _pending.get(caller)
        if pending is not None:
            digest, used = pending
            try:
                body = json.loads(request.data)
            except (ValueError, TypeError):
                raise ReviewInputError(
                    "trust effect verification request is invalid"
                ) from None
            if (
                used
                or request.get_method() != "POST"
                or request.full_url
                != profile.broker.broker_url.removesuffix("/token") + "/session-grant"
                or set(body)
                - {
                    "session_grant",
                    "session_attestation",
                    "refresh_effect_sha256",
                    "control_budget",
                }
                or body.get("session_grant") != profile.session.grant
                or body.get("session_attestation") != profile.session.attestation
                or body.get("refresh_effect_sha256") != digest
            ):
                raise ReviewInputError("trust effect verification dispatch changed")
            _pending[caller] = (digest, True)
            return True
        url = urlsplit(request.full_url)
        origin = f"{url.scheme}://{url.netloc}"
        if request.get_method() not in {"POST", "PUT", "PATCH", "DELETE"}:
            return False
        if request.full_url in caller._authentication_endpoints:
            return False
        if origin != profile.api_origin:
            raise ReviewInputError("trust effect native destination is unsupported")
        from .quarantine_census import is_readonly_census

        if is_readonly_census(request, caller._quarantine_census_api_url):
            return False
        # Stronger routes are deliberately not inferred from this transport mode.
        if getattr(caller, "_controlled_restart_admission", None) is not None:
            raise ReviewInputError("trust effect restart pricing is unsupported")
        if profile not in _admitted_routes:
            initial = _initializing.get(caller)
            if initial is None:
                raise ReviewInputError(
                    "trust effect complete route admission unavailable"
                )
            ledger, identity = initial
            require_native_implementation(ledger)
            require_ledger_source(caller, ledger, identity)
            from review_sensei.errors import EvidenceBudgetExhausted

            from .api_run_plan import WRITE_CONFIRMATION_TAIL
            from .session_ledger import parse_session_comment

            activation = caller._activation
            try:
                body = json.loads(request.data)
                parsed = parse_session_comment(body.get("body"), identity=identity)
            except (ValueError, TypeError, AttributeError):
                raise ReviewInputError(
                    "trust initialization candidate is invalid"
                ) from None
            if (
                activation is None
                or parsed is None
                or canonical_bytes(parsed.to_dict())
                != canonical_bytes(activation[0].to_dict())
                or parsed.trust_enrollment is None
                or original_observation(parsed)["authority"]
                != profile.session.attestation
                or ledger.token != profile.session.token
                or request.get_header("Authorization")
                != f"Bearer {profile.session.token}"
                or request.get_method() != "POST"
                or request.full_url
                != profile.api_origin
                + f"/repos/{identity.repository}/issues/{identity.pull_request}/comments"
            ):
                raise ReviewInputError("trust initialization candidate changed")
            if (
                len(caller._slot.requests) - caller.startup_dispatches
                < 1 + WRITE_CONFIRMATION_TAIL
            ):
                raise EvidenceBudgetExhausted(
                    "trust initialization refresh and confirmation tail are unfunded"
                )
        bound = _binding(profile, request, sequence=caller._sequence + 2)
        digest = hashlib.sha256(bound).hexdigest()
        _pending[caller] = (digest, False)
        try:
            refresh_current_observation(profile.broker, profile.session, digest)
            if _pending.get(caller) != (digest, True):
                raise ReviewInputError(
                    "trust effect refresh lacked physical source dispatch"
                )
        finally:
            _pending.pop(caller, None)
        # Exactly one verification dispatch must lead to the precommitted
        # native sequence. Never substitute a different locally recomputed
        # digest for the one actually refreshed by the broker.
        if _binding(profile, request) != bound:
            raise ReviewInputError("trust effect verification sequence changed")
        _receipts[request] = (profile, bound)
        return False

    def consume(caller, request):
        profile = verification_profile(caller)
        if profile is None:
            return
        if caller in _initializing:
            ledger, identity = _initializing[caller]
            require_native_implementation(ledger)
            require_ledger_source(caller, ledger, identity)
            if (
                ledger.token != profile.session.token
                or ledger.http.api_url != profile.api_origin
            ):
                raise ReviewInputError("trust initialization current source changed")
        receipt = _receipts.pop(request, None)
        if receipt is None:
            # Non-native requests were not candidates for a receipt.
            url = urlsplit(request.full_url)
            from .quarantine_census import is_readonly_census

            if (
                request.get_method() in {"POST", "PUT", "PATCH", "DELETE"}
                and f"{url.scheme}://{url.netloc}" == profile.api_origin
                and not is_readonly_census(request, caller._quarantine_census_api_url)
            ):
                raise ReviewInputError("trust effect fresh receipt is unavailable")
            return
        if caller in _initializing:
            from .session_ledger import parse_session_comment

            activation = caller._activation
            value = json.loads(request.data)
            record = parse_session_comment(
                value.get("body"), identity=_initializing[caller][1]
            )
            if (
                activation is None
                or record is None
                or canonical_bytes(record.to_dict())
                != canonical_bytes(activation[0].to_dict())
            ):
                raise ReviewInputError(
                    "trust initialization activation changed after refresh"
                )
        # Caller.open increments its own physical sequence before this boundary.
        binding = json.loads(_binding(profile, request))
        binding["sequence"] -= 1
        if receipt[0] is not profile or receipt[1] != canonical_bytes(binding):
            raise ReviewInputError("trust effect fresh receipt changed or was reused")

        # Capture only a fully consumed, exact fresh receipt after Caller.open's
        # physical debit. A later lost response cannot confuse the preceding
        # broker refresh POST with this native attempt.
        observation = caller.observations[-1]
        if not observation.get("mutation") or not isinstance(
            observation.get("request_id"), str
        ):
            raise ReviewInputError("trust native attempt debit is unavailable")
        _attempts.setdefault(caller, []).append(
            (
                len(caller.observations) - 1,
                observation,
                observation["request_id"],
                receipt[1],
            )
        )

    def require_native_attempt(caller, *, observations_start, method, url, body):
        """Return one exact source-consumed native ID, never refund its charge."""
        profile = verification_profile(caller)
        if (
            profile is None
            or type(observations_start) is not int
            or not 0 <= observations_start <= len(caller.observations)
        ):
            raise ReviewInputError("trust native attempt scope is unavailable")
        rows = [
            row for row in _attempts.get(caller, ()) if row[0] >= observations_start
        ]
        if len(rows) != 1:
            raise ReviewInputError("trust native attempt identity is ambiguous")
        index, observation, identifier, raw = rows[0]
        for extra in caller.observations[observations_start:]:
            if not extra.get("mutation") or extra is observation:
                continue
            control = caller._confirmed_control_writes.get(extra.get("request_id"))
            if control != ("broker_authentication", str(caller.run.run_id)):
                raise ReviewInputError("trust native attempt identity is ambiguous")
        binding = json.loads(raw)
        if (
            caller.observations[index] is not observation
            or observation.get("request_id") != identifier
            or observation.get("mutation") is not True
            or binding["method"] != method
            or binding["url"] != url
            or binding["body_sha256"] != hashlib.sha256(body).hexdigest()
            or binding["original_sha256"]
            != hashlib.sha256(profile.original).hexdigest()
            or binding["grant_sha256"] != profile.grant_sha256
        ):
            raise ReviewInputError("trust native attempt binding changed")
        return identifier

"""Read-only provenance of actual, already-funded capability exchanges.

This is not freshness or an effect permit. Reusing an issued token still needs
an independently prepaid exact-effect observation at each native dispatch.
"""

from __future__ import annotations

import hashlib
import json
import time
from functools import wraps
from weakref import WeakKeyDictionary

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError

from .errors import GitHubBrokerClientError
from .run_authority import parse_authority

_installed = False
_lookup = None
_register_transport = None


def _install_capability_source(source_class):
    global _installed, _lookup, _register_transport
    from .broker_client import BrokerClient

    if _installed or source_class is not BrokerClient:
        raise ReviewInputError("trust capability source registration is invalid")
    original, post = source_class.exchange, source_class._post
    caller_source = None
    receipts: WeakKeyDictionary = WeakKeyDictionary()
    pending: WeakKeyDictionary = WeakKeyDictionary()

    def register_transport(source):
        nonlocal caller_source
        from .api_run_requests import ApiRunRequestTransport

        if caller_source is not None or source is not ApiRunRequestTransport:
            raise ReviewInputError("trust capability transport registration is invalid")
        caller_source = (source, source.open)

    _register_transport = register_transport

    def supported(broker):
        return (
            type(broker) is source_class
            and source_class.exchange is exchange
            and source_class._post is source_post
            and getattr(broker.exchange, "__func__", None) is exchange
            and getattr(broker._post, "__func__", None) is source_post
        )

    @wraps(post)
    def source_post(self, payload, **kwargs):
        result = post(self, payload, **kwargs)
        capture = pending.get(self)
        if capture is not None:
            if capture["response"] is not None:
                capture["invalid"] = True
            capture["response"] = result.copy()
        return result

    @wraps(original)
    def exchange(self, oidc_token, *, capability=None):
        # Preserve legacy/custom client behavior. Unsupported implementations
        # may return their normal token but cannot mint a provenance receipt.
        qualified = supported(self) and self not in pending
        if self in pending:
            pending[self]["invalid"] = True
        opener, destination = self.opener, self.broker_url
        before = None
        if qualified:
            try:
                before = parse_authority(self.authority)
            except GitHubBrokerClientError:
                # Existing exchange input validation owns its error precedence;
                # malformed/unconfigured clients gain no provenance receipt.
                qualified = False
        capture = {"response": None, "invalid": False}
        if qualified:
            pending[self] = capture
        try:
            token = original(self, oidc_token, capability=capability)
        finally:
            if qualified:
                pending.pop(self, None)
        if (
            not qualified
            or not supported(self)
            or self.opener is not opener
            or self.broker_url != destination
        ):
            return token
        response = capture["response"]
        if (
            capture["invalid"]
            or not isinstance(response, dict)
            or response.get("token") != token
            or response.get("capability") != (capability or "review_publish")
        ):
            return token
        after = parse_authority(self.authority)
        if before is None or any(
            before[k] != after[k]
            for k in before
            if k not in {"issued_at", "expires_at"}
        ):
            return token
        row = (canonical_bytes(after), destination, opener)
        receipts.setdefault(self, {})[
            (capability or "review_publish", hashlib.sha256(token.encode()).hexdigest())
        ] = row
        return token

    def lookup(caller, token, capability, authority):
        if caller_source is None:
            raise ReviewInputError("trust capability transport is unavailable")
        caller_class, caller_open = caller_source
        if (
            type(caller) is not caller_class
            or getattr(caller.open, "__func__", None) is not caller_open
            or caller_class.open is not caller_open
            or not isinstance(token, str)
            or not token
        ):
            raise ReviewInputError("trust capability caller is unsupported")
        from .trust_effects import verification_profile

        profile = verification_profile(caller)
        expected = parse_authority(authority, grant=True)
        if profile is None or expected != json.loads(profile.original)["authority"]:
            raise ReviewInputError(
                "trust capability requires the original source profile"
            )
        observation = expected.get("current_observation")
        required = {
            "review_publish": {"pull_requests": "write"},
            "check_publish": {"checks": "write"},
            "thread_resolve": {"pull_requests": "write", "contents": "write"},
            "inline_reply": {"pull_requests": "write"},
            "issue_reply": {"pull_requests": "write"},
            "learning_write": {"pull_requests": "write", "contents": "write"},
            "review_session": {"pull_requests": "write"},
            "review_status": {"pull_requests": "read"},
            "review_actions": {"actions": "read"},
        }.get(capability)
        if (
            required is None
            or not isinstance(observation, dict)
            or observation.get("schema_version") != "current-observation-v2"
        ):
            raise ReviewInputError(
                "trust capability current permission observation is unavailable"
            )
        installed = observation["installation"]["permissions"]
        if any(
            installed.get(key)
            not in ({"read", "write"} if value == "read" else {"write"})
            for key, value in required.items()
        ):
            raise ReviewInputError(
                "trust capability exceeds observed installation permissions"
            )
        scope = (
            "purpose",
            "repository",
            "repository_id",
            "pull_request",
            "head_sha",
            "run_id",
            "run_attempt",
            "job",
            "job_workflow_ref",
            "job_workflow_sha",
            "audience",
            "source",
        )
        if (
            expected["repository_id"] != caller.run.repository_id
            or expected["run_id"] != str(caller.run.run_id)
            or expected["run_attempt"] != caller.attempt
            or expected["job"] != caller.job
        ):
            raise ReviewInputError("trust capability caller scope changed")
        key = (capability, hashlib.sha256(token.encode()).hexdigest())
        for broker, issued in list(receipts.items()):
            row = issued.get(key)
            if row is None or not supported(broker):
                continue
            document, destination, opener = row
            observed = json.loads(document)
            if (
                broker.opener is not opener
                or destination != profile.broker_url
                or broker.broker_url != destination
                or getattr(opener, "__self__", None) is not caller
                or getattr(opener, "__func__", None) is not caller_open
                or any(observed[k] != expected[k] for k in scope)
                or not observed["issued_at"]
                <= int(time.time())
                < observed["expires_at"]
            ):
                continue
            return
        raise ReviewInputError("trust capability lacks matching source issuance")

    _installed = True
    _lookup = lookup
    setattr(source_class, "_post", source_post)
    setattr(source_class, "exchange", exchange)


def require_source_capability(caller, token, *, capability, authority):
    """Validate exact issued capability/run only; grants no dispatch/freshness."""
    if _lookup is None:
        raise ReviewInputError("trust capability source is unavailable")
    _lookup(caller, token, capability, authority)


def _install_capability_transport(source_class):
    if _register_transport is None:
        raise ReviewInputError("trust capability source is unavailable")
    _register_transport(source_class)

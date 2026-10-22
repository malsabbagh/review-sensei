"""Authenticated maintainer commands and finding dispositions for issue #136 C6.

C5 enforces round admission but maps handoff onto ``skipped_policy``. GitHub
treats skipped/neutral required checks as passing. C6 publishes a clearly
non-passing ``action_required`` outcome, parses bounded ``@reviewsensei`` / ``@sensei`` commands,
and records human dispositions without claiming an independently verified fix.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import cast

from .errors import ReviewInputError
from .session import (
    MAX_DISPOSITION_BYTES,
    MAX_DISPOSITIONS_PER_HEAD,
    MAX_UNHEADED_DISPOSITIONS,
    SessionIdentity,
    SessionLedger,
    SessionRecord,
    disposition_array_bytes,
)

PUBLIC_SCHEMA_VERSION = "1.0"
MAINTAINER_ACTIONS = frozenset(
    {
        "status",
        "pause",
        "verify",
        "continue",
        "reenroll",
        "dismiss",
        "defer",
        "accept-risk",
        "accept-coverage-uncertainty",
        "approve-media",
    }
)
FINDING_ACTIONS = frozenset({"dismiss", "defer", "accept-risk"})
DISPOSITION_ACTIONS = frozenset(
    {"dismiss", "defer", "accept-risk", "accept-coverage-uncertainty"}
)
AUTHORIZED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
MAX_REASON_BYTES = 512
MAX_ACTOR_BYTES = 256
# The Cloudflare broker mirrors this parser before it attests a maintainer
# command. Keep command separators deliberately ASCII so Python and JavaScript
# cannot disagree about control/Unicode whitespace at the trust boundary.
_COMMAND_WHITESPACE = " \t\r\n"
_COMMAND_WS = r"[ \t\r\n]"
_COMMAND_HWS = r"[ \t]"
_COMMAND_NON_WS = r"[^ \t\r\n]"
# A command mention must be a standalone command at the very start of the
# comment body (index 0) with horizontal whitespace separating tokens.
# Leading indentation, blockquotes (`>`), fenced code blocks, and surrounding
# prose are rejected before any normalization.
# Scope ASCII case folding to the canonical handle: legacy commands keep their
# lowercase gate, while ``a`` rejects Unicode lookalikes such as long-s/dotless-i.
# The broker spells the same canonical rule with explicit ASCII letter pairs.
_SENSEI = re.compile(r"\A(?:@sensei|(?ai:@reviewsensei))(?=[ \t]+)")
_CONTINUE = re.compile(
    rf"^review{_COMMAND_HWS}+continue{_COMMAND_WS}*$",
    re.IGNORECASE,
)
_REVIEW_STATUS = re.compile(
    rf"^review{_COMMAND_HWS}+status{_COMMAND_WS}*$", re.IGNORECASE
)
_REVIEW_PAUSE = re.compile(
    rf"^review{_COMMAND_HWS}+pause{_COMMAND_WS}*$", re.IGNORECASE
)
_VERIFY = re.compile(rf"^verify{_COMMAND_WS}*$", re.IGNORECASE)
_REENROLL = re.compile(rf"^review{_COMMAND_HWS}+reenroll{_COMMAND_WS}*$", re.IGNORECASE)
_FINDING = re.compile(
    rf"^(dismiss|defer|accept-risk){_COMMAND_HWS}+([a-f0-9]{{16,64}}){_COMMAND_HWS}+--reason{_COMMAND_HWS}+({_COMMAND_NON_WS}[^\r\n]*)$",
    re.IGNORECASE,
)
_COVERAGE_UNCERTAINTY = re.compile(
    rf"^(?:review{_COMMAND_HWS}+)?accept-coverage-uncertainty{_COMMAND_HWS}+--reason{_COMMAND_HWS}+({_COMMAND_NON_WS}[^\r\n]*)$",
    re.IGNORECASE,
)
_FINGERPRINT = re.compile(r"^[a-f0-9]{16,64}$")
_FINDING_REFERENCE = re.compile(r"^(?:RS-)?[A-Fa-f0-9]{6,64}$")
MAX_OVERRIDE_IDS = 10
_ID = r"(?:RS-)?[A-Fa-f0-9]{6,64}"
_ID_SEP = rf"(?:{_COMMAND_HWS}*,{_COMMAND_HWS}*|{_COMMAND_HWS}+)"
_ID_LIST = rf"({_ID}(?:{_ID_SEP}{_ID}){{0,{MAX_OVERRIDE_IDS - 1}}})"
_ID_SPLIT = re.compile(_ID_SEP)
_OVERRIDE = re.compile(
    rf"^override{_COMMAND_HWS}+{_ID_LIST}({_COMMAND_HWS}+acceptable{_COMMAND_HWS}+risk)?{_COMMAND_WS}*$",
    re.IGNORECASE,
)
_ACCEPTABLE_RISK = re.compile(
    rf"^{_ID_LIST}{_COMMAND_HWS}+(?:is|are){_COMMAND_HWS}+acceptable{_COMMAND_HWS}+risk{_COMMAND_WS}*$",
    re.IGNORECASE,
)
_MEDIA_APPROVAL = re.compile(
    rf"^i{_COMMAND_HWS}+reviewed{_COMMAND_HWS}+the{_COMMAND_HWS}+media{_COMMAND_HWS}+files{_COMMAND_HWS}+and{_COMMAND_HWS}+i{_COMMAND_HWS}+approve{_COMMAND_WS}*$",
    re.IGNORECASE,
)
_HEAD_SHA = re.compile(r"^[a-f0-9]{40,64}$")


def _aware_now(now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _require_reason(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewInputError("maintainer disposition requires a reason")
    reason = value.strip(_COMMAND_WHITESPACE)
    if (
        len(reason.encode("utf-8")) > MAX_REASON_BYTES
        or not reason
        or not reason.isprintable()
        or any(
            character != " " and unicodedata.category(character).startswith("Z")
            for character in reason
        )
    ):
        raise ReviewInputError("maintainer disposition reason exceeds the bound")
    return reason


def authorized_maintainer(
    *,
    login: object,
    user_type: object,
    association: object,
    app_slug: str,
) -> bool:
    """Accept only human collaborators; never the App or a casual commenter."""

    if not isinstance(login, str) or not login.strip():
        return False
    if login.casefold() == app_slug.casefold():
        return False
    if isinstance(user_type, str) and user_type.lower() == "bot":
        return False
    if not isinstance(association, str):
        return False
    return association.upper() in AUTHORIZED_ASSOCIATIONS


def authorized_override(*, association: object, permission: object = None) -> bool:
    """Maintainer or admin override. Write access alone is not enough.

    An explicit GitHub permission wins. ``maintain`` and ``admin`` qualify.
    When the permission was not loaded, a repository owner or organization
    member still qualifies; an outside collaborator does not.
    """

    if isinstance(permission, str):
        return permission in {"maintain", "admin"}
    return isinstance(association, str) and association.upper() in {"OWNER", "MEMBER"}


@dataclass(frozen=True)
class FindingIdentity:
    """One displayed finding the current review can resolve."""

    instance: str
    concern: str | None = None
    blocking: bool = False
    needs_human: bool = False
    overridable: bool = True

    def __post_init__(self) -> None:
        if not _FINGERPRINT.fullmatch(self.instance):
            raise ReviewInputError("finding instance is invalid")
        if self.concern is not None and not _FINGERPRINT.fullmatch(self.concern):
            raise ReviewInputError("finding concern is invalid")


@dataclass(frozen=True)
class OverrideReceipt:
    """One finding's outcome inside a maintainer override command."""

    reference: str
    fingerprint: str | None
    outcome: str
    detail: str = ""

    def __post_init__(self) -> None:
        if self.outcome not in {
            "recorded",
            "already-recorded",
            "unknown",
            "ambiguous",
            "earlier-head",
            "not-applied",
        }:
            raise ReviewInputError("override receipt outcome is invalid")


class OverrideRejected(ReviewInputError):
    """An all-or-nothing override that recorded nothing."""

    def __init__(self, message: str, receipts: tuple[OverrideReceipt, ...]) -> None:
        super().__init__(message)
        self.receipts = receipts


@dataclass(frozen=True)
class MaintainerCommand:
    """One authenticated, head-bound maintainer action."""

    action: str
    actor: str
    reason: str | None = None
    finding_fingerprint: str | None = None
    finding_reference: str | None = None
    head_sha: str | None = None
    command_id: str | None = None
    finding_references: tuple[str, ...] = ()
    finding_fingerprints: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.action not in MAINTAINER_ACTIONS:
            raise ReviewInputError("maintainer action is unsupported")
        if not isinstance(self.actor, str) or not self.actor.strip():
            raise ReviewInputError("maintainer actor is invalid")
        if self.head_sha is not None and (
            not isinstance(self.head_sha, str) or not _HEAD_SHA.fullmatch(self.head_sha)
        ):
            raise ReviewInputError("maintainer head_sha is invalid")
        if self.command_id is not None and (
            not isinstance(self.command_id, str) or not self.command_id
        ):
            raise ReviewInputError("maintainer command_id is invalid")
        if self.command_id is not None and self.action not in {"continue", "reenroll"}:
            raise ReviewInputError("maintainer command_id only applies to continuation")
        if self.action in FINDING_ACTIONS:
            references = self.finding_references or (
                (self.finding_reference,) if self.finding_reference is not None else ()
            )
            fingerprints = self.finding_fingerprints or (
                (self.finding_fingerprint,)
                if self.finding_fingerprint is not None
                else ()
            )
            if references and fingerprints:
                raise ReviewInputError("finding reference is invalid")
            if (
                len(references) > MAX_OVERRIDE_IDS
                or len(fingerprints) > MAX_OVERRIDE_IDS
            ):
                raise ReviewInputError(
                    "maintainer override accepts at most 10 finding ids"
                )
            if references:
                if any(
                    not isinstance(reference, str)
                    or _FINDING_REFERENCE.fullmatch(reference) is None
                    for reference in references
                ):
                    raise ReviewInputError("finding reference is invalid")
                if (
                    self.finding_reference is not None
                    and self.finding_references
                    and self.finding_reference != self.finding_references[0]
                ):
                    raise ReviewInputError("finding reference is invalid")
            elif fingerprints:
                if any(
                    not isinstance(fingerprint, str)
                    or not _FINGERPRINT.fullmatch(fingerprint)
                    for fingerprint in fingerprints
                ):
                    raise ReviewInputError("finding disposition requires a fingerprint")
            else:
                raise ReviewInputError("finding disposition requires a fingerprint")
            _require_reason(self.reason)
        elif self.action == "accept-coverage-uncertainty":
            if (
                self.finding_reference
                or self.finding_references
                or self.finding_fingerprint
                or self.finding_fingerprints
            ):
                raise ReviewInputError(
                    "coverage uncertainty acceptance does not take finding ids"
                )
            _require_reason(self.reason)
        elif self.reason is not None:
            _require_reason(self.reason)


def parse_maintainer_command(
    body: object,
    *,
    actor: str,
    head_sha: str | None = None,
    command_id: str | None = None,
) -> MaintainerCommand | None:
    """Parse a bounded standalone ``@reviewsensei`` or legacy ``@sensei`` command. Unknown text is not a command."""

    if not isinstance(body, str) or len(body.encode("utf-8")) > 4096:
        return None
    match = _SENSEI.match(body)
    if match is None:
        return None
    remainder = body[match.end() :].lstrip(" \t").rstrip(_COMMAND_WHITESPACE)
    if not remainder or "\r" in remainder or "\n" in remainder:
        return None
    if _REVIEW_STATUS.fullmatch(remainder):
        return MaintainerCommand(action="status", actor=actor, head_sha=head_sha)
    if _REVIEW_PAUSE.fullmatch(remainder):
        return MaintainerCommand(action="pause", actor=actor, head_sha=head_sha)
    if _VERIFY.fullmatch(remainder):
        return MaintainerCommand(action="verify", actor=actor, head_sha=head_sha)
    if _REENROLL.fullmatch(remainder):
        return MaintainerCommand(
            action="reenroll",
            actor=actor,
            head_sha=head_sha,
            command_id=command_id,
        )
    if _CONTINUE.fullmatch(remainder):
        # Continuation is only an unpause unless an authorized maintainer grants
        # a bounded continuation window on an exhausted session.
        return MaintainerCommand(
            action="continue",
            actor=actor,
            head_sha=head_sha,
            command_id=command_id,
        )
    coverage = _COVERAGE_UNCERTAINTY.fullmatch(remainder)
    if coverage is not None:
        return MaintainerCommand(
            action="accept-coverage-uncertainty",
            actor=actor,
            reason=coverage.group(1).strip(_COMMAND_WHITESPACE).strip('"').strip("'"),
            head_sha=head_sha,
        )
    finding = _FINDING.fullmatch(remainder)
    if finding is not None:
        return MaintainerCommand(
            action=finding.group(1).lower(),
            actor=actor,
            finding_fingerprint=finding.group(2).lower(),
            reason=finding.group(3).strip(_COMMAND_WHITESPACE).strip('"').strip("'"),
            head_sha=head_sha,
        )
    if _MEDIA_APPROVAL.fullmatch(remainder):
        return MaintainerCommand(action="approve-media", actor=actor, head_sha=head_sha)
    override = _OVERRIDE.fullmatch(remainder)
    if override is not None:
        reason = "acceptable risk" if override.group(2) else "override"
        return _override_command(
            actor, override.group(1), reason=reason, head_sha=head_sha
        )
    acceptable = _ACCEPTABLE_RISK.fullmatch(remainder)
    if acceptable is not None:
        return _override_command(
            actor,
            acceptable.group(1),
            reason="acceptable risk",
            head_sha=head_sha,
        )
    return None


def _split_override_ids(text: str) -> tuple[str, ...]:
    """Split a matched ID list. The caller regex already bounded the shape."""

    parts = [part for part in _ID_SPLIT.split(text) if part]
    if not parts or len(parts) > MAX_OVERRIDE_IDS:
        raise ReviewInputError("maintainer override accepts at most 10 finding ids")
    if any(
        re.fullmatch(r"(?:rs-)?[A-Fa-f0-9]{6,64}", part, re.IGNORECASE) is None
        for part in parts
    ):
        raise ReviewInputError("finding reference is invalid")
    return tuple(parts)


def _override_command(
    actor: str, text: str, *, reason: str, head_sha: str | None
) -> MaintainerCommand:
    """Map an easy override onto accept-risk.

    A full fingerprint can be stored immediately. A displayed ``RS-`` id stays
    unresolved until the current review's fingerprints are available. Every ID
    in one comment is one command.
    """

    references: list[str] = []
    fingerprints: list[str] = []
    for token in _split_override_ids(text):
        body = token[3:] if token.lower().startswith("rs-") else token
        if token.lower().startswith("rs-") or not _FINGERPRINT.fullmatch(body.lower()):
            reference = f"RS-{body.upper()}"
            if reference not in references:
                references.append(reference)
        else:
            fingerprint = body.lower()
            if fingerprint not in fingerprints:
                fingerprints.append(fingerprint)
    if references and fingerprints:
        raise ReviewInputError("finding reference is invalid")
    if references:
        return MaintainerCommand(
            action="accept-risk",
            actor=actor,
            finding_reference=references[0] if len(references) == 1 else None,
            finding_references=tuple(references),
            reason=reason,
            head_sha=head_sha,
        )
    return MaintainerCommand(
        action="accept-risk",
        actor=actor,
        finding_fingerprint=fingerprints[0] if len(fingerprints) == 1 else None,
        finding_fingerprints=tuple(fingerprints),
        reason=reason,
        head_sha=head_sha,
    )


def resolve_finding_reference(reference: str, fingerprints: Sequence[str]) -> str:
    """Resolve one displayed finding id to exactly one current fingerprint."""

    if (
        not isinstance(reference, str)
        or _FINDING_REFERENCE.fullmatch(reference) is None
    ):
        raise ReviewInputError("finding id is invalid")
    body = reference[3:] if reference.lower().startswith("rs-") else reference
    from .presentation import assign_finding_identifiers

    assigned = assign_finding_identifiers(fingerprints)
    wanted = f"RS-{body.upper()}"
    matches = [
        fingerprint
        for fingerprint, identifier in assigned.items()
        if identifier.upper() == wanted
    ]
    if len(matches) == 1:
        return matches[0]
    exact = [
        fingerprint
        for fingerprint in fingerprints
        if fingerprint.lower() == body.lower()
        and _FINGERPRINT.fullmatch(fingerprint.lower())
    ]
    if len(exact) == 1:
        return exact[0]
    raise ReviewInputError("finding id does not match one current finding")


def resolve_finding_identity(
    reference: str, inventory: Sequence[FindingIdentity]
) -> FindingIdentity:
    """Resolve one displayed ID against the review's instance fingerprints.

    Identifier width follows the same set publication displayed, including
    optional findings. Optional matches are not overridable. A full fingerprint
    may match one instance or one concern; a concern shared by several
    instances is ambiguous.
    """

    if (
        not isinstance(reference, str)
        or _FINDING_REFERENCE.fullmatch(reference) is None
    ):
        raise ReviewInputError("finding id is invalid")
    if not inventory:
        raise ReviewInputError("finding id needs the current review")
    body = reference[3:] if reference.lower().startswith("rs-") else reference
    from .presentation import assign_finding_identifiers

    assigned = assign_finding_identifiers(item.instance for item in inventory)
    wanted = f"RS-{body.upper()}"
    matches = [item for item in inventory if assigned[item.instance].upper() == wanted]
    if len(matches) > 1:
        raise ReviewInputError("finding id is ambiguous")
    if len(matches) == 1:
        found = matches[0]
        if not found.overridable:
            raise ReviewInputError("finding id is not a current required finding")
        return found
    exact_instances = [
        item for item in inventory if item.instance.lower() == body.lower()
    ]
    exact_concerns = [
        item
        for item in inventory
        if item.concern is not None and item.concern.lower() == body.lower()
    ]
    if len(exact_instances) == 1 and not exact_concerns:
        found = exact_instances[0]
        if not found.overridable:
            raise ReviewInputError("finding id is not a current required finding")
        return found
    if len(exact_concerns) == 1 and not exact_instances:
        found = exact_concerns[0]
        if not found.overridable:
            raise ReviewInputError("finding id is not a current required finding")
        return found
    if exact_instances or exact_concerns:
        raise ReviewInputError("finding id is ambiguous")
    raise ReviewInputError("finding id does not match one current finding")


@dataclass(frozen=True)
class FindingDisposition:
    """A human decision bound to one finding or coverage-uncertainty scope. Never a verified fix."""

    fingerprint: str
    action: str
    reason: str
    actor: str
    head_sha: str | None = None
    expires_at: str | None = None
    actor_role: str | None = None
    finding_reference: str | None = None
    recorded_at: str | None = None
    policy_digest: str | None = None
    provenance: str | None = None
    target_kind: str | None = None
    incomplete_coverage: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _FINGERPRINT.fullmatch(self.fingerprint):
            raise ReviewInputError("disposition fingerprint is invalid")
        if self.action not in DISPOSITION_ACTIONS:
            raise ReviewInputError("disposition action is unsupported")
        _require_reason(self.reason)
        if (
            not isinstance(self.actor, str)
            or not self.actor.strip()
            or len(self.actor.encode("utf-8")) > MAX_ACTOR_BYTES
            or not self.actor.isprintable()
        ):
            raise ReviewInputError("disposition actor is invalid")
        if self.head_sha is not None and (
            not isinstance(self.head_sha, str) or not _HEAD_SHA.fullmatch(self.head_sha)
        ):
            raise ReviewInputError("disposition head_sha is invalid")
        if self.expires_at is not None:
            if not isinstance(self.expires_at, str):
                raise ReviewInputError("disposition expires_at is invalid")
            try:
                parsed_expiry = datetime.fromisoformat(
                    self.expires_at.replace("Z", "+00:00")
                )
            except ValueError as exc:
                raise ReviewInputError("disposition expires_at is invalid") from exc
            if parsed_expiry.tzinfo is None or parsed_expiry.utcoffset() is None:
                raise ReviewInputError("disposition expires_at must include a timezone")
        if self.actor_role is not None and (
            not isinstance(self.actor_role, str)
            or not self.actor_role.strip()
            or len(self.actor_role.encode("utf-8")) > 64
            or not self.actor_role.isprintable()
        ):
            raise ReviewInputError("disposition actor_role is invalid")
        if self.finding_reference is not None and (
            not isinstance(self.finding_reference, str)
            or _FINDING_REFERENCE.fullmatch(self.finding_reference) is None
        ):
            raise ReviewInputError("disposition finding_reference is invalid")
        if self.recorded_at is not None:
            if not isinstance(self.recorded_at, str):
                raise ReviewInputError("disposition recorded_at is invalid")
            try:
                parsed_recorded = datetime.fromisoformat(
                    self.recorded_at.replace("Z", "+00:00")
                )
            except ValueError as exc:
                raise ReviewInputError("disposition recorded_at is invalid") from exc
            if parsed_recorded.tzinfo is None or parsed_recorded.utcoffset() is None:
                raise ReviewInputError(
                    "disposition recorded_at must include a timezone"
                )
        if self.policy_digest is not None and (
            not isinstance(self.policy_digest, str)
            or not re.fullmatch(r"^[a-f0-9]{64}$", self.policy_digest)
        ):
            raise ReviewInputError("disposition policy_digest is invalid")
        if self.provenance is not None and (
            not isinstance(self.provenance, str)
            or not self.provenance.strip()
            or len(self.provenance.encode("utf-8")) > 256
            or not self.provenance.isprintable()
        ):
            raise ReviewInputError("disposition provenance is invalid")
        if self.target_kind is not None and self.target_kind not in {
            "finding",
            "coverage_uncertainty",
        }:
            raise ReviewInputError("disposition target_kind is invalid")
        if (
            not isinstance(self.incomplete_coverage, tuple)
            or len(self.incomplete_coverage) > 256
        ):
            raise ReviewInputError("disposition incomplete_coverage is invalid")
        for item in self.incomplete_coverage:
            if (
                not isinstance(item, str)
                or not item.strip()
                or len(item.encode("utf-8")) > 256
                or not item.isprintable()
            ):
                raise ReviewInputError("disposition incomplete_coverage is invalid")

    @property
    def resolution_kind(self) -> str:
        if self.action == "dismiss":
            return "disputed"
        if self.action == "defer":
            return "deferred"
        if self.action == "accept-coverage-uncertainty":
            return "coverage_uncertainty_accepted"
        return "accepted_unresolved"

    @property
    def invalidation_behavior(self) -> str:
        if self.head_sha is not None:
            return "invalidated_on_new_push"
        return "reassessed_each_round"

    def audit_trail(
        self, *, identity: SessionIdentity | None = None
    ) -> dict[str, object]:
        is_coverage = (
            self.action == "accept-coverage-uncertainty"
            or self.target_kind == "coverage_uncertainty"
        )
        return {
            "actor": self.actor,
            "actor_role": self.actor_role,
            "repository": identity.repository if identity is not None else None,
            "pull_request": identity.pull_request if identity is not None else None,
            "head_sha": self.head_sha,
            "policy_digest": self.policy_digest,
            "target_kind": "coverage_uncertainty" if is_coverage else "finding",
            "finding_id": None
            if is_coverage
            else (self.finding_reference or self.fingerprint),
            "finding_fingerprint": None if is_coverage else self.fingerprint,
            "incomplete_coverage": list(self.incomplete_coverage),
            "action": self.action,
            "resolution_kind": self.resolution_kind,
            "reason": self.reason,
            "recorded_at": self.recorded_at,
            "provenance": self.provenance,
            "invalidation_behavior": self.invalidation_behavior,
        }

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "fingerprint": self.fingerprint,
            "action": self.action,
            "reason": self.reason,
            "actor": self.actor,
            "head_sha": self.head_sha,
            "expires_at": self.expires_at,
        }
        if self.actor_role is not None:
            payload["actor_role"] = self.actor_role
        if self.finding_reference is not None:
            payload["finding_reference"] = self.finding_reference
        if self.recorded_at is not None:
            payload["recorded_at"] = self.recorded_at
        if self.policy_digest is not None:
            payload["policy_digest"] = self.policy_digest
        if self.provenance is not None:
            payload["provenance"] = self.provenance
        if self.target_kind is not None:
            payload["target_kind"] = self.target_kind
        if self.incomplete_coverage:
            payload["incomplete_coverage"] = list(self.incomplete_coverage)
        if (
            self.actor_role is not None
            or self.finding_reference is not None
            or self.recorded_at is not None
            or self.policy_digest is not None
            or self.provenance is not None
            or self.target_kind is not None
            or self.incomplete_coverage
            or self.action == "accept-coverage-uncertainty"
        ):
            payload["resolution_kind"] = self.resolution_kind
        return payload

    def honors(
        self,
        fingerprint: str,
        *,
        head_sha: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        if (
            self.action == "accept-coverage-uncertainty"
            or self.target_kind == "coverage_uncertainty"
        ):
            return False
        if self.fingerprint != fingerprint:
            return False
        # A disposition recorded for one reviewed head must never silently
        # carry forward to a different head. A missing current head is also
        # fail-closed when the disposition is explicitly head-bound.
        if self.head_sha is not None and self.head_sha != head_sha:
            return False
        if self.expires_at is None:
            return True
        expires = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        return expires > _aware_now(now)

    def honors_coverage_uncertainty(
        self,
        *,
        head_sha: str | None,
        incomplete_coverage: Sequence[str] | None = None,
        policy_digest: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        if self.action != "accept-coverage-uncertainty":
            return False
        if self.head_sha is None or head_sha is None or self.head_sha != head_sha:
            return False
        if (
            self.policy_digest is not None
            and policy_digest is not None
            and self.policy_digest != policy_digest
        ):
            return False
        if incomplete_coverage is not None and self.incomplete_coverage:
            required = {
                item.strip()
                for item in incomplete_coverage
                if isinstance(item, str) and item.strip()
            }
            if not required.issubset(set(self.incomplete_coverage)):
                return False
        if self.expires_at is None:
            return True
        expires = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        return expires > _aware_now(now)


PAUSED_BY_MAINTAINER_SUMMARY = (
    "paused by maintainer, comment @sensei review continue to resume"
)


@dataclass(frozen=True)
class MaintainerCommandResult:
    """Outcome of applying one command. Never converts a withheld decision."""

    action: str
    applied: bool
    operator_paused: bool
    summary: str = ""
    disposition: FindingDisposition | None = None
    dispositions: tuple[FindingDisposition, ...] = ()
    receipts: tuple[OverrideReceipt, ...] = ()
    # True when the command could not recompute eligibility. Approval still
    # being blocked is a successful command with evaluation_failed left false.
    evaluation_failed: bool = False


def command_exit_code(result: MaintainerCommandResult) -> int:
    """Exit 1 only when eligibility could not be evaluated.

    A completed command that withholds approval is exit 0. The review check
    carries the action-required state. An evaluation failure must not look
    like a successful workflow run.
    """

    return 1 if getattr(result, "evaluation_failed", False) else 0


def _reference_receipt(
    reference: str,
    inventory: Sequence[FindingIdentity],
    prior_inventories: Sequence[tuple[str, Sequence[FindingIdentity]]],
) -> tuple[FindingIdentity | None, OverrideReceipt]:
    try:
        found = resolve_finding_identity(reference, inventory)
    except ReviewInputError as exc:
        text = str(exc)
        if "ambiguous" in text:
            return None, OverrideReceipt(reference, None, "ambiguous")
        if "not a current required finding" in text:
            return None, OverrideReceipt(
                reference, None, "unknown", "not a current required finding"
            )
        for head_sha, entries in prior_inventories:
            try:
                resolve_finding_identity(reference, entries)
            except ReviewInputError:
                continue
            short = head_sha[:7] if len(head_sha) >= 7 else head_sha
            return None, OverrideReceipt(reference, None, "earlier-head", short)
        detail = (
            "needs the current review"
            if "needs the current review" in text
            else "does not match one current finding"
        )
        return None, OverrideReceipt(reference, None, "unknown", detail)
    return found, OverrideReceipt(reference, found.instance, "recorded")


def _plan_override(
    command: MaintainerCommand,
    *,
    finding_fingerprints: Sequence[str] | None,
    finding_inventory: Sequence[FindingIdentity] | None,
    prior_inventories: Sequence[tuple[str, Sequence[FindingIdentity]]],
    actor_role: str | None = None,
    recorded_at: str | None = None,
    policy_digest: str | None = None,
    provenance: str | None = None,
) -> tuple[tuple[FindingDisposition, ...], tuple[OverrideReceipt, ...]]:
    """Resolve every ID before any ledger write. One failure records nothing."""

    references = command.finding_references or (
        (command.finding_reference,) if command.finding_reference is not None else ()
    )
    explicit = command.finding_fingerprints or (
        (command.finding_fingerprint,)
        if command.finding_fingerprint is not None and not references
        else ()
    )
    inventory = tuple(finding_inventory or ())
    if references and not inventory and finding_fingerprints:
        inventory = tuple(
            FindingIdentity(instance=fingerprint, concern=fingerprint)
            for fingerprint in finding_fingerprints
            if _FINGERPRINT.fullmatch(fingerprint)
        )
    planned: list[FindingDisposition] = []
    receipts: list[OverrideReceipt] = []
    seen: set[str] = set()
    failed = False
    if references and not inventory and not prior_inventories:
        raise OverrideRejected(
            "finding id needs the current review",
            tuple(
                OverrideReceipt(reference, None, "unknown", "needs the current review")
                for reference in references
            ),
        )
    has_audit_fields = any(
        value is not None
        for value in (actor_role, recorded_at, policy_digest, provenance)
    )
    for reference in references:
        found, receipt = _reference_receipt(reference, inventory, prior_inventories)
        if found is None:
            failed = True
            receipts.append(receipt)
            continue
        if found.instance in seen:
            receipts.append(
                OverrideReceipt(reference, found.instance, "already-recorded")
            )
            continue
        seen.add(found.instance)
        planned.append(
            FindingDisposition(
                fingerprint=found.instance,
                action=command.action,
                reason=command.reason or "",
                actor=command.actor,
                head_sha=command.head_sha,
                actor_role=actor_role,
                finding_reference=reference if has_audit_fields else None,
                recorded_at=recorded_at,
                policy_digest=policy_digest,
                provenance=provenance,
                target_kind="finding" if has_audit_fields else None,
            )
        )
        receipts.append(receipt)
    for fingerprint in explicit:
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        planned.append(
            FindingDisposition(
                fingerprint=fingerprint,
                action=command.action,
                reason=command.reason or "",
                actor=command.actor,
                head_sha=command.head_sha,
                actor_role=actor_role,
                recorded_at=recorded_at,
                policy_digest=policy_digest,
                provenance=provenance,
                target_kind="finding" if has_audit_fields else None,
            )
        )
        receipts.append(OverrideReceipt(fingerprint, fingerprint, "recorded"))
    if failed:
        frozen = tuple(
            OverrideReceipt(
                item.reference,
                item.fingerprint,
                "not-applied" if item.outcome == "recorded" else item.outcome,
                item.detail,
            )
            for item in receipts
        )
        names = ", ".join(
            item.reference for item in frozen if item.outcome != "not-applied"
        )
        if any(item.detail == "needs the current review" for item in frozen):
            message = "finding id needs the current review"
        else:
            message = "Nothing was recorded because one ID could not be applied" + (
                f": {names}" if names else ""
            )
        raise OverrideRejected(message, frozen)
    return tuple(planned), tuple(receipts)


def apply_session_command(
    ledger: SessionLedger,
    identity: SessionIdentity,
    command: MaintainerCommand,
    *,
    now: datetime | None = None,
    finding_fingerprints: Sequence[str] | None = None,
    finding_inventory: Sequence[FindingIdentity] | None = None,
    prior_inventories: Sequence[tuple[str, Sequence[FindingIdentity]]] = (),
    incomplete_coverage: Sequence[str] | None = None,
    actor_role: str | None = None,
    policy_digest: str | None = None,
    provenance: str | None = None,
) -> tuple[SessionRecord, MaintainerCommandResult]:
    """Mutate pause/continuation and persist finding decisions on the ledger."""

    if command.action == "approve-media":
        return SessionRecord.create(identity, now=now), MaintainerCommandResult(
            action="approve-media",
            applied=False,
            operator_paused=False,
            summary=(
                "media approval is applied by the file reviewer for the current head"
            ),
        )
    has_audit_fields = any(
        value is not None for value in (actor_role, policy_digest, provenance)
    )
    recorded_at = (
        _aware_now(now).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        if has_audit_fields or command.action == "accept-coverage-uncertainty"
        else None
    )
    planned: tuple[FindingDisposition, ...] = ()
    planned_receipts: tuple[OverrideReceipt, ...] = ()
    if command.action in FINDING_ACTIONS:
        planned, planned_receipts = _plan_override(
            command,
            finding_fingerprints=finding_fingerprints,
            finding_inventory=finding_inventory,
            prior_inventories=prior_inventories,
            actor_role=actor_role,
            recorded_at=recorded_at,
            policy_digest=policy_digest,
            provenance=provenance,
        )
    elif command.action == "accept-coverage-uncertainty":
        if command.head_sha is None:
            raise ReviewInputError(
                "coverage uncertainty acceptance requires an exact head_sha"
            )
        enumerated = tuple(
            sorted(
                {
                    item.strip()
                    for item in (incomplete_coverage or ())
                    if isinstance(item, str) and item.strip()
                }
            )
        )
        scope_key = f"coverage-uncertainty:{command.head_sha}:{','.join(enumerated)}"
        coverage_fp = hashlib.sha256(scope_key.encode("utf-8")).hexdigest()
        planned = (
            FindingDisposition(
                fingerprint=coverage_fp,
                action="accept-coverage-uncertainty",
                reason=command.reason or "",
                actor=command.actor,
                head_sha=command.head_sha,
                actor_role=actor_role,
                recorded_at=recorded_at,
                policy_digest=policy_digest,
                provenance=provenance,
                target_kind="coverage_uncertainty",
                incomplete_coverage=enumerated,
            ),
        )
        planned_receipts = (
            OverrideReceipt("coverage-uncertainty", coverage_fp, "recorded"),
        )
    if (
        command.action not in {"status", "verify"}
        and getattr(ledger, "_broker", None) is not None
        and not callable(getattr(ledger, "initialize_with_mutation", None))
    ):
        raise ReviewInputError(
            "broker-bound session ledger requires atomic initialization"
        )
    loaded = ledger.load(identity, now=now)
    if command.action == "reenroll":
        from .convergence import DEFAULT_MAX_FAILED_ATTEMPTS, MAX_FAILED_ATTEMPTS
        from .session import (
            failed_attempts_for_head,
            grant_successor_continuation,
            has_unconsumed_irrecoverable_transaction_grant,
            transition_irrecoverable_transaction,
        )

        if actor_role is not None and actor_role not in {
            "maintain",
            "admin",
            "OWNER",
            "MEMBER",
        }:
            raise ReviewInputError("reenroll requires maintain or admin permission")
        if loaded.status in {"ok", "migrated"} and loaded.record is not None:
            tx = loaded.record.transaction
            if (
                tx is not None
                and tx.phase in {"publication_pending", "publication_failed"}
                and (command.head_sha is None or tx.head_sha == command.head_sha)
            ) or (
                tx is None
                and has_unconsumed_irrecoverable_transaction_grant(
                    loaded.record, command.head_sha, policy_digest=policy_digest
                )
                is not None
            ):
                record, applied = transition_irrecoverable_transaction(
                    ledger,
                    identity,
                    actor=command.actor,
                    head_sha=command.head_sha,
                    policy_digest=policy_digest,
                    command_id=command.command_id,
                    now=now,
                )
                target_head = command.head_sha or record.failed_attempts_head_sha
                if failed_attempts_for_head(record, target_head) >= MAX_FAILED_ATTEMPTS:
                    return record, MaintainerCommandResult(
                        action="reenroll",
                        applied=applied,
                        operator_paused=bool(getattr(record, "operator_paused", False)),
                        summary=(
                            "irrecoverable transaction retired, but hard failed-attempt "
                            "ceiling is reached; push a new head to continue"
                        ),
                    )
                return record, MaintainerCommandResult(
                    action="reenroll",
                    applied=applied,
                    operator_paused=bool(getattr(record, "operator_paused", False)),
                    summary=(
                        "irrecoverable transaction retired; successor analysis is "
                        "authorized for the current head; original failure, costs, "
                        "and provenance were preserved"
                    ),
                )
            target_head = command.head_sha or loaded.record.failed_attempts_head_sha
            if (
                failed_attempts_for_head(loaded.record, target_head)
                >= DEFAULT_MAX_FAILED_ATTEMPTS
            ):
                record, applied = grant_successor_continuation(
                    ledger,
                    identity,
                    actor=command.actor,
                    actor_role=actor_role or "maintain",
                    head_sha=target_head,
                    policy_digest=policy_digest,
                    command_id=command.command_id,
                    now=now,
                )
                return record, MaintainerCommandResult(
                    action="reenroll",
                    applied=applied,
                    operator_paused=bool(getattr(record, "operator_paused", False)),
                    summary=(
                        "successor continuation granted; failed-attempt eligibility "
                        "restored for one bounded window on the current head"
                    ),
                )
        # Expired-session recovery is the one path allowed to retire durable
        # state, and it is only reachable through this authenticated maintainer
        # command. Every other status needs investigation rather than a reset,
        # which is why the ledger refuses them rather than this branch guessing.
        record = ledger.reenroll(identity, now=now)
        return record, MaintainerCommandResult(
            action="reenroll",
            applied=True,
            operator_paused=bool(getattr(record, "operator_paused", False)),
            summary=(
                "session re-enrolled with fresh round counters; the previous "
                "expired or witness-only state was retired"
            ),
        )
    initialized_with_mutation = False
    if loaded.status in {"missing", "expired"} or loaded.record is None:
        # Status is a read-only command: do not create a hosted issue comment
        # merely to report that no durable session exists.
        if command.action == "status":
            record = SessionRecord.create(identity, now=now)
        else:
            initialize_with_mutation = getattr(ledger, "initialize_with_mutation", None)
            if callable(initialize_with_mutation):

                def mutate_initial(initial: SessionRecord) -> SessionRecord:
                    if command.action == "pause":
                        return _evolve_operator_paused(initial, paused=True, now=now)
                    if command.action == "continue":
                        return _evolve_operator_paused(initial, paused=False, now=now)
                    if command.action in DISPOSITION_ACTIONS:
                        if not planned:
                            raise ReviewInputError("finding disposition is invalid")
                        return _evolve_dispositions(
                            initial, planned, head_sha=command.head_sha, now=now
                        )
                    return initial

                record = initialize_with_mutation(identity, mutate_initial, now=now)
                initialized_with_mutation = True
            else:
                record = ledger.initialize(identity, now=now)
    elif loaded.status in {"ok", "migrated"}:
        record = loaded.record
    else:
        raise ReviewInputError(f"session ledger load failed: {loaded.status}")
    paused = bool(getattr(record, "operator_paused", False))
    if command.action == "pause":
        if not initialized_with_mutation:
            record = _set_operator_paused(
                ledger, identity, record, paused=True, now=now
            )
        return record, MaintainerCommandResult(
            action="pause",
            applied=True,
            operator_paused=True,
            summary=PAUSED_BY_MAINTAINER_SUMMARY,
        )
    if command.action == "verify":
        dispositions = session_dispositions(record)
        from .presentation import finding_identifier

        stale: list[str] = []
        current: list[FindingDisposition] = []
        for item in dispositions:
            if item.honors(item.fingerprint, head_sha=command.head_sha):
                current.append(item)
            elif item.head_sha is not None and item.head_sha != command.head_sha:
                stale.append(finding_identifier(item.fingerprint))
        if stale:
            summary = (
                "stale override "
                + ", ".join(stale)
                + "; a new push needs a new override"
            )
            applied = False
        elif current:
            summary = (
                "overrides for this commit are loaded; "
                "approval is re-checked without a new AI review"
            )
            applied = True
        else:
            summary = (
                "verification requires an evidence-backed review result; "
                "session pause state is unchanged"
            )
            applied = False
        return record, MaintainerCommandResult(
            action="verify",
            applied=applied,
            operator_paused=paused,
            summary=summary,
        )
    if command.action == "continue":
        from .convergence import DEFAULT_MAX_FAILED_ATTEMPTS
        from .session import (
            failed_attempts_for_head,
            grant_successor_continuation,
        )

        if record.reservation_id is not None:
            recover = getattr(ledger, "continue_after_abandoned_analysis", None)
            if not callable(recover):
                raise ReviewInputError(
                    "held reservation requires authenticated Actions recovery"
                )
            record = recover(identity, now=now)
            return record, MaintainerCommandResult(
                action="continue",
                applied=True,
                operator_paused=False,
                summary="abandoned analysis recovered; one failed attempt recorded; automated review may continue",
            )
        target_head = command.head_sha or record.failed_attempts_head_sha
        if failed_attempts_for_head(record, target_head) >= DEFAULT_MAX_FAILED_ATTEMPTS:
            if (
                actor_role in {"maintain", "admin", "OWNER", "MEMBER"}
                and isinstance(target_head, str)
                and bool(_HEAD_SHA.fullmatch(target_head))
            ):
                record, applied = grant_successor_continuation(
                    ledger,
                    identity,
                    actor=command.actor,
                    actor_role=actor_role,
                    head_sha=target_head,
                    policy_digest=policy_digest,
                    command_id=command.command_id,
                    now=now,
                )
                return record, MaintainerCommandResult(
                    action="continue",
                    applied=applied,
                    operator_paused=False,
                    summary=(
                        "successor continuation granted; failed-attempt eligibility "
                        "restored for one bounded window on the current head"
                    ),
                )
            if paused and not initialized_with_mutation:
                record = _set_operator_paused(
                    ledger, identity, record, paused=False, now=now
                )
            return record, MaintainerCommandResult(
                action="continue",
                applied=paused,
                operator_paused=False,
                summary=(
                    "session unpaused, but failed-attempt budget remains exhausted; "
                    "a maintainer or admin must grant successor continuation to "
                    "restore eligibility"
                ),
            )
        if not initialized_with_mutation:
            record = _set_operator_paused(
                ledger, identity, record, paused=False, now=now
            )
        return record, MaintainerCommandResult(
            action="continue",
            applied=True,
            operator_paused=False,
            summary="automated review may continue; rounds are uncapped",
        )
    if command.action == "status":
        head_suffix = f" head={command.head_sha}" if command.head_sha else ""
        # Tell the two blocked states apart (ADR 0079): a maintainer pause
        # is only ever cleared by an explicit continue, while a held
        # reservation names the run that owns it.
        clauses = []
        if paused:
            clauses.append(PAUSED_BY_MAINTAINER_SUMMARY)
        if record.reservation_id is not None:
            owner = record.reservation_owner
            if owner is not None:
                clauses.append(
                    f"blocked by held reservation, owner run {owner.get('run_id')}"
                )
            else:
                clauses.append("blocked by a held reservation, owner unknown")
        # No clause when nothing is blocked: the paused= field already
        # carries the state, and a redundant "not paused" suffix only
        # duplicates it.
        state_suffix = f"; {'; '.join(clauses)}" if clauses else ""
        return record, MaintainerCommandResult(
            action="status",
            applied=False,
            operator_paused=paused,
            summary=(
                f"initial={record.completed_initial_reviews} "
                f"verification={record.completed_verification_rounds} "
                f"failed_attempts={record.failed_attempts} "
                f"paused={paused}{head_suffix}{state_suffix}"
            ),
        )
    if not planned:
        raise ReviewInputError("finding disposition is invalid")
    before = {
        (item.get("fingerprint"), item.get("head_sha")) for item in record.dispositions
    }
    if not initialized_with_mutation:
        record = _append_dispositions(
            ledger, identity, record, planned, head_sha=command.head_sha, now=now
        )
    receipts: list[OverrideReceipt] = []
    for receipt in planned_receipts:
        key = (receipt.fingerprint, command.head_sha)
        if receipt.outcome == "recorded" and key in before:
            receipts.append(
                OverrideReceipt(
                    receipt.reference, receipt.fingerprint, "already-recorded"
                )
            )
        else:
            receipts.append(receipt)
    stored = session_dispositions(record)
    summary_text = (
        "coverage uncertainty accepted for current head; analysis remains incomplete and is not marked complete"
        if command.action == "accept-coverage-uncertainty"
        else "human disposition recorded; not an independently verified fix"
    )
    return record, MaintainerCommandResult(
        action=command.action,
        applied=True,
        operator_paused=paused,
        summary=summary_text,
        disposition=stored[-1] if stored else planned[0],
        dispositions=tuple(
            item
            for item in stored
            if (item.fingerprint, item.head_sha)
            in {(d.fingerprint, d.head_sha) for d in planned}
        )
        or planned,
        receipts=tuple(receipts),
    )


def _evolve_operator_paused(
    record: SessionRecord,
    *,
    paused: bool,
    now: datetime | None,
) -> SessionRecord:
    if bool(getattr(record, "operator_paused", False)) == paused:
        return record
    return record.evolve(
        now=now,
        generation=record.generation + 1,
        operator_paused=paused,
    )


def _disposition_expired(item: Mapping[str, object], now: datetime | None) -> bool:
    expires_at = item.get("expires_at")
    if not isinstance(expires_at, str):
        return False
    try:
        expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if expires.tzinfo is None or expires.utcoffset() is None:
        return False
    return expires <= _aware_now(now)


def _evolve_dispositions(
    record: SessionRecord,
    new: Sequence[FindingDisposition],
    *,
    head_sha: str | None,
    now: datetime | None,
) -> SessionRecord:
    """Prune other heads, keep the first decision, and enforce the current-head budget."""

    kept: list[dict[str, object]] = []
    for item in record.dispositions:
        if not isinstance(item, Mapping):
            continue
        if _disposition_expired(item, now):
            continue
        item_head = item.get("head_sha")
        if (
            head_sha is not None
            and isinstance(item_head, str)
            and item_head != head_sha
        ):
            continue
        kept.append(dict(item))
    existing = {(item.get("fingerprint"), item.get("head_sha")) for item in kept}
    for disposition in new:
        key = (disposition.fingerprint, disposition.head_sha)
        if key in existing:
            continue
        kept.append(disposition.to_dict())
        existing.add(key)
    unheaded = sum(1 for item in kept if item.get("head_sha") is None)
    if unheaded > MAX_UNHEADED_DISPOSITIONS:
        raise ReviewInputError("session disposition limit reached")
    if head_sha is not None:
        headed = sum(1 for item in kept if item.get("head_sha") == head_sha)
        if headed > MAX_DISPOSITIONS_PER_HEAD:
            raise ReviewInputError("session disposition limit reached")
    if disposition_array_bytes(kept) > MAX_DISPOSITION_BYTES:
        raise ReviewInputError("session disposition limit reached")
    if kept == [dict(item) for item in record.dispositions]:
        return record
    return record.evolve(
        now=now,
        generation=record.generation + 1,
        dispositions=tuple(kept),
    )


def _evolve_disposition(
    record: SessionRecord,
    disposition: FindingDisposition,
    *,
    now: datetime | None,
) -> SessionRecord:
    return _evolve_dispositions(
        record,
        (disposition,),
        head_sha=disposition.head_sha,
        now=now,
    )


def _set_operator_paused(
    ledger: SessionLedger,
    identity: SessionIdentity,
    record: SessionRecord,
    *,
    paused: bool,
    now: datetime | None,
) -> SessionRecord:
    if bool(getattr(record, "operator_paused", False)) == paused:
        return record
    replace = getattr(ledger, "replace", None)
    if not callable(replace):
        raise ReviewInputError("session ledger does not support CAS mutation")

    def mutate(current: SessionRecord) -> SessionRecord:
        if current.generation != record.generation:
            raise ReviewInputError("session generation conflict")
        return _evolve_operator_paused(current, paused=paused, now=now)

    return replace(identity, mutate, now=now)


def _append_dispositions(
    ledger: SessionLedger,
    identity: SessionIdentity,
    record: SessionRecord,
    dispositions: Sequence[FindingDisposition],
    *,
    head_sha: str | None,
    now: datetime | None,
) -> SessionRecord:
    replace = getattr(ledger, "replace", None)
    if not callable(replace):
        raise ReviewInputError("session ledger does not support CAS mutation")

    def mutate(current: SessionRecord) -> SessionRecord:
        if current.generation != record.generation:
            raise ReviewInputError("session generation conflict")
        return _evolve_dispositions(current, dispositions, head_sha=head_sha, now=now)

    return replace(identity, mutate, now=now)


def _append_disposition(
    ledger: SessionLedger,
    identity: SessionIdentity,
    record: SessionRecord,
    disposition: FindingDisposition,
    *,
    now: datetime | None,
) -> SessionRecord:
    return _append_dispositions(
        ledger,
        identity,
        record,
        (disposition,),
        head_sha=disposition.head_sha,
        now=now,
    )


def disposition_honors_fingerprint(
    dispositions: Sequence[FindingDisposition],
    fingerprint: str,
    *,
    head_sha: str | None = None,
    now: datetime | None = None,
) -> bool:
    return any(
        item.honors(fingerprint, head_sha=head_sha, now=now) for item in dispositions
    )


def disposition_honors_coverage_uncertainty(
    dispositions: Sequence[FindingDisposition],
    *,
    head_sha: str | None,
    incomplete_coverage: Sequence[str] | None = None,
    policy_digest: str | None = None,
    now: datetime | None = None,
) -> bool:
    return any(
        callable(getattr(item, "honors_coverage_uncertainty", None))
        and item.honors_coverage_uncertainty(
            head_sha=head_sha,
            incomplete_coverage=incomplete_coverage,
            policy_digest=policy_digest,
            now=now,
        )
        for item in dispositions
    )


def session_honors_coverage_uncertainty(
    record: SessionRecord,
    *,
    head_sha: str | None,
    incomplete_coverage: Sequence[str] | None = None,
    policy_digest: str | None = None,
    now: datetime | None = None,
) -> bool:
    return disposition_honors_coverage_uncertainty(
        session_dispositions(record),
        head_sha=head_sha,
        incomplete_coverage=incomplete_coverage,
        policy_digest=policy_digest,
        now=now,
    )


def session_dispositions(record: SessionRecord) -> tuple[FindingDisposition, ...]:
    """Decode the bounded dispositions persisted on one session record."""

    if not isinstance(record, SessionRecord):
        raise ReviewInputError("session record is invalid")
    return tuple(
        FindingDisposition(
            fingerprint=cast(str, item["fingerprint"]),
            action=cast(str, item["action"]),
            reason=cast(str, item["reason"]),
            actor=cast(str, item["actor"]),
            head_sha=cast(str | None, item["head_sha"]),
            expires_at=cast(str | None, item["expires_at"]),
            actor_role=cast(str | None, item.get("actor_role")),
            finding_reference=cast(str | None, item.get("finding_reference")),
            recorded_at=cast(str | None, item.get("recorded_at")),
            policy_digest=cast(str | None, item.get("policy_digest")),
            provenance=cast(str | None, item.get("provenance")),
            target_kind=cast(str | None, item.get("target_kind")),
            incomplete_coverage=tuple(
                cast(Sequence[str], item.get("incomplete_coverage") or ())
            ),
        )
        for item in record.dispositions
    )


def render_convergence_summary(
    *,
    mode: str,
    round_kind: str,
    completed_rounds: int,
    verified_fixed: int = 0,
    new_regressions: int = 0,
    advisory: int = 0,
    handoff: bool = False,
    handoff_reason: str | None = None,
) -> str:
    """One durable author-facing summary. Numbers are caller-supplied facts.

    Rounds are uncapped, so the summary reports counted history rather than a
    remaining allowance: nothing here may read as a budget the reader is
    running out of.
    """

    for label, value in (
        ("completed_rounds", completed_rounds),
        ("verified_fixed", verified_fixed),
        ("new_regressions", new_regressions),
        ("advisory", advisory),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ReviewInputError(f"{label} must be a non-negative integer")
    if not isinstance(handoff_reason, (str, type(None))):
        raise ReviewInputError("handoff_reason is invalid")
    if handoff_reason is not None and (
        len(handoff_reason.encode("utf-8")) > 128 or not handoff_reason.isprintable()
    ):
        raise ReviewInputError("handoff_reason is invalid")

    next_action = (
        "human review before another automated pass" if handoff else "continue"
    )
    reason = f" ({handoff_reason})" if handoff and handoff_reason else ""
    return (
        f"{round_kind.capitalize()} rounds={completed_rounds}: "
        f"{verified_fixed} concerns fixed; {new_regressions} fix-introduced "
        f"regressions; {advisory} advisory suggestions; mode={mode}; "
        f"next={next_action}{reason}."
    )


__all__ = [
    "AUTHORIZED_ASSOCIATIONS",
    "DISPOSITION_ACTIONS",
    "FINDING_ACTIONS",
    "FindingDisposition",
    "MAINTAINER_ACTIONS",
    "MaintainerCommand",
    "MaintainerCommandResult",
    "apply_session_command",
    "authorized_maintainer",
    "disposition_honors_coverage_uncertainty",
    "disposition_honors_fingerprint",
    "parse_maintainer_command",
    "render_convergence_summary",
    "session_dispositions",
    "session_honors_coverage_uncertainty",
]

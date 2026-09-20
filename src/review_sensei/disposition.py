"""Authenticated maintainer commands and finding dispositions for issue #136 C6.

C5 enforces round admission but maps handoff onto ``skipped_policy``. GitHub
treats skipped/neutral required checks as passing. C6 publishes a clearly
non-passing ``action_required`` outcome, parses bounded ``@sensei`` commands,
and records human dispositions without claiming an independently verified fix.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence, cast

from .convergence import ReviewConvergencePolicy
from .errors import ReviewInputError
from .session import (
    MAX_STORED_DISPOSITIONS,
    SessionIdentity,
    SessionLedger,
    SessionRecord,
    issue_continuation_grant,
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
    }
)
FINDING_ACTIONS = frozenset({"dismiss", "defer", "accept-risk"})
AUTHORIZED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
MAX_REASON_BYTES = 512
MAX_ACTOR_BYTES = 256
# A command mention must start at the beginning of a line or after whitespace.
# This keeps prose/markdown prefixes valid while rejecting punctuation-adjacent
# text such as ``!@sensei`` and ``(@sensei``.
_SENSEI = re.compile(r"(?m)(?<!\S)@sensei(?=\s+)")
_CONTINUE_ROUNDS = re.compile(
    r"^review\s+continue(?:\s+--rounds\s+(0|1))?\s*$", re.IGNORECASE
)
_REVIEW_STATUS = re.compile(r"^review\s+status\s*$", re.IGNORECASE)
_REVIEW_PAUSE = re.compile(r"^review\s+pause\s*$", re.IGNORECASE)
_VERIFY = re.compile(r"^verify\s*$", re.IGNORECASE)
_REENROLL = re.compile(r"^review\s+reenroll\s*$", re.IGNORECASE)
_FINDING = re.compile(
    r"^(dismiss|defer|accept-risk)\s+([a-f0-9]{16,64})\s+--reason\s+(\S.*)$",
    re.IGNORECASE | re.DOTALL,
)
_FINGERPRINT = re.compile(r"^[a-f0-9]{16,64}$")
_HEAD_SHA = re.compile(r"^[a-f0-9]{40,64}$")


def _aware_now(now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _require_reason(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReviewInputError("maintainer disposition requires a reason")
    reason = value.strip()
    if len(reason.encode("utf-8")) > MAX_REASON_BYTES or not reason.isprintable():
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


@dataclass(frozen=True)
class MaintainerCommand:
    """One authenticated, head-bound maintainer action."""

    action: str
    actor: str
    reason: str | None = None
    finding_fingerprint: str | None = None
    continuation_rounds: int = 0
    head_sha: str | None = None
    command_id: str | None = None

    def __post_init__(self) -> None:
        if self.action not in MAINTAINER_ACTIONS:
            raise ReviewInputError("maintainer action is unsupported")
        if not isinstance(self.actor, str) or not self.actor.strip():
            raise ReviewInputError("maintainer actor is invalid")
        if self.continuation_rounds not in {0, 1}:
            raise ReviewInputError("continuation_rounds must be 0 or 1")
        if self.head_sha is not None and (
            not isinstance(self.head_sha, str) or not _HEAD_SHA.fullmatch(self.head_sha)
        ):
            raise ReviewInputError("maintainer head_sha is invalid")
        if self.command_id is not None and (
            not isinstance(self.command_id, str) or not self.command_id
        ):
            raise ReviewInputError("maintainer command_id is invalid")
        if self.command_id is not None and self.action != "continue":
            raise ReviewInputError("maintainer command_id only applies to continuation")
        if self.action in FINDING_ACTIONS:
            if self.finding_fingerprint is None or not _FINGERPRINT.fullmatch(
                self.finding_fingerprint
            ):
                raise ReviewInputError("finding disposition requires a fingerprint")
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
    """Parse a bounded ``@sensei`` command. Unknown text is not a command."""

    if not isinstance(body, str) or "@sensei" not in body:
        return None
    match = _SENSEI.search(body)
    if match is None:
        return None
    remainder = body[match.end() :].strip()
    if _REVIEW_STATUS.fullmatch(remainder):
        return MaintainerCommand(action="status", actor=actor, head_sha=head_sha)
    if _REVIEW_PAUSE.fullmatch(remainder):
        return MaintainerCommand(action="pause", actor=actor, head_sha=head_sha)
    if _VERIFY.fullmatch(remainder):
        return MaintainerCommand(action="verify", actor=actor, head_sha=head_sha)
    if _REENROLL.fullmatch(remainder):
        return MaintainerCommand(action="reenroll", actor=actor, head_sha=head_sha)
    continued = _CONTINUE_ROUNDS.fullmatch(remainder)
    if continued is not None:
        rounds = int(continued.group(1) or "1")
        return MaintainerCommand(
            action="continue",
            actor=actor,
            continuation_rounds=rounds,
            head_sha=head_sha,
            command_id=command_id,
        )
    finding = _FINDING.fullmatch(remainder)
    if finding is not None:
        return MaintainerCommand(
            action=finding.group(1).lower(),
            actor=actor,
            finding_fingerprint=finding.group(2).lower(),
            reason=finding.group(3).strip().strip('"').strip("'"),
            head_sha=head_sha,
        )
    return None


@dataclass(frozen=True)
class FindingDisposition:
    """A human decision bound to one finding identity. Never a verified fix."""

    fingerprint: str
    action: str
    reason: str
    actor: str
    head_sha: str | None = None
    expires_at: str | None = None

    def __post_init__(self) -> None:
        if not _FINGERPRINT.fullmatch(self.fingerprint):
            raise ReviewInputError("disposition fingerprint is invalid")
        if self.action not in FINDING_ACTIONS:
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

    def to_dict(self) -> dict[str, object]:
        return {
            "fingerprint": self.fingerprint,
            "action": self.action,
            "reason": self.reason,
            "actor": self.actor,
            "head_sha": self.head_sha,
            "expires_at": self.expires_at,
        }

    def honors(
        self,
        fingerprint: str,
        *,
        head_sha: str | None = None,
        now: datetime | None = None,
    ) -> bool:
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


@dataclass(frozen=True)
class MaintainerCommandResult:
    """Outcome of applying one command. Never converts a withheld decision."""

    action: str
    applied: bool
    operator_paused: bool
    continuation_rounds: int = 0
    summary: str = ""
    disposition: FindingDisposition | None = None


def apply_session_command(
    ledger: SessionLedger,
    identity: SessionIdentity,
    command: MaintainerCommand,
    *,
    now: datetime | None = None,
    policy: ReviewConvergencePolicy | None = None,
) -> tuple[SessionRecord, MaintainerCommandResult]:
    """Mutate pause/continuation and persist finding decisions on the ledger."""

    if (
        command.action not in {"status", "verify"}
        and getattr(ledger, "_broker", None) is not None
        and not callable(getattr(ledger, "initialize_with_mutation", None))
    ):
        raise ReviewInputError(
            "grant-bound session ledger requires atomic initialization"
        )
    loaded = ledger.load(identity, now=now)
    if command.action == "reenroll":
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
    disposition: FindingDisposition | None = None
    if command.action in FINDING_ACTIONS:
        disposition = FindingDisposition(
            fingerprint=command.finding_fingerprint or "",
            action=command.action,
            reason=command.reason or "",
            actor=command.actor,
            head_sha=command.head_sha,
        )
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
                        if command.command_id is not None:
                            if not isinstance(policy, ReviewConvergencePolicy):
                                raise ReviewInputError(
                                    "identified continuation requires a review policy"
                                )
                            if command.head_sha is None:
                                raise ReviewInputError(
                                    "identified continuation requires an exact head_sha"
                                )
                            return issue_continuation_grant(
                                initial,
                                command_id=command.command_id,
                                actor=command.actor,
                                head_sha=command.head_sha,
                                policy_digest=policy.digest(),
                                now=now,
                            )
                        return _evolve_operator_paused(initial, paused=False, now=now)
                    if command.action in FINDING_ACTIONS:
                        if disposition is None:
                            raise ReviewInputError("finding disposition is invalid")
                        return _evolve_disposition(initial, disposition, now=now)
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
            summary="automated review paused pending maintainer continuation",
        )
    if command.action == "verify":
        return record, MaintainerCommandResult(
            action="verify",
            applied=False,
            operator_paused=paused,
            summary=(
                "verification requires an evidence-backed review result; "
                "session pause state is unchanged"
            ),
        )
    if command.action == "continue":
        if command.command_id is not None:
            if not isinstance(policy, ReviewConvergencePolicy):
                raise ReviewInputError(
                    "identified continuation requires a review policy"
                )
            if command.head_sha is None:
                raise ReviewInputError(
                    "identified continuation requires an exact head_sha"
                )
            if not initialized_with_mutation:
                record = _issue_continuation_grant(
                    ledger,
                    identity,
                    record,
                    command=command,
                    policy=policy,
                    now=now,
                )
            return record, MaintainerCommandResult(
                action="continue",
                applied=True,
                operator_paused=False,
                continuation_rounds=0,
                summary="one-use continuation grant issued for the exact head and policy",
            )
        if not initialized_with_mutation:
            record = _set_operator_paused(
                ledger, identity, record, paused=False, now=now
            )
        return record, MaintainerCommandResult(
            action="continue",
            applied=True,
            operator_paused=False,
            continuation_rounds=command.continuation_rounds,
            summary="automated review may continue under C5 admission",
        )
    if command.action == "status":
        head_suffix = f" head={command.head_sha}" if command.head_sha else ""
        return record, MaintainerCommandResult(
            action="status",
            applied=False,
            operator_paused=paused,
            summary=(
                f"initial={record.completed_initial_reviews} "
                f"verification={record.completed_verification_rounds} "
                f"failed_attempts={record.failed_attempts} "
                f"paused={paused}{head_suffix}"
            ),
        )
    if disposition is None:
        raise ReviewInputError("finding disposition is invalid")
    if not initialized_with_mutation:
        record = _append_disposition(ledger, identity, record, disposition, now=now)
    return record, MaintainerCommandResult(
        action=command.action,
        applied=True,
        operator_paused=paused,
        summary="human disposition recorded; not an independently verified fix",
        disposition=disposition,
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


def _evolve_disposition(
    record: SessionRecord,
    disposition: FindingDisposition,
    *,
    now: datetime | None,
) -> SessionRecord:
    if len(record.dispositions) >= MAX_STORED_DISPOSITIONS:
        raise ReviewInputError("session disposition limit reached")
    return record.evolve(
        now=now,
        generation=record.generation + 1,
        dispositions=(*record.dispositions, disposition.to_dict()),
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


def _issue_continuation_grant(
    ledger: SessionLedger,
    identity: SessionIdentity,
    record: SessionRecord,
    *,
    command: MaintainerCommand,
    policy: ReviewConvergencePolicy,
    now: datetime | None,
) -> SessionRecord:
    replace = getattr(ledger, "replace", None)
    if not callable(replace):
        raise ReviewInputError("session ledger does not support CAS mutation")

    def mutate(current: SessionRecord) -> SessionRecord:
        if current.generation != record.generation:
            raise ReviewInputError("session generation conflict")
        # The command id, actor, exact head, and policy digest are rechecked
        # by the durable constructor.  Returning an equal record makes a
        # delivery replay idempotent without reviving an already-consumed grant.
        return issue_continuation_grant(
            current,
            command_id=command.command_id or "",
            actor=command.actor,
            head_sha=command.head_sha or "",
            policy_digest=policy.digest(),
            now=now,
        )

    return replace(identity, mutate, now=now)


def _append_disposition(
    ledger: SessionLedger,
    identity: SessionIdentity,
    record: SessionRecord,
    disposition: FindingDisposition,
    *,
    now: datetime | None,
) -> SessionRecord:
    if len(record.dispositions) >= MAX_STORED_DISPOSITIONS:
        raise ReviewInputError("session disposition limit reached")
    replace = getattr(ledger, "replace", None)
    if not callable(replace):
        raise ReviewInputError("session ledger does not support CAS mutation")

    def mutate(current: SessionRecord) -> SessionRecord:
        if current.generation != record.generation:
            raise ReviewInputError("session generation conflict")
        return _evolve_disposition(current, disposition, now=now)

    return replace(identity, mutate, now=now)


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
        )
        for item in record.dispositions
    )


def render_convergence_summary(
    *,
    mode: str,
    round_kind: str,
    remaining_verification: int,
    verified_fixed: int = 0,
    new_regressions: int = 0,
    advisory: int = 0,
    handoff: bool = False,
    handoff_reason: str | None = None,
) -> str:
    """One durable author-facing summary. Numbers are caller-supplied facts."""

    for label, value in (
        ("remaining_verification", remaining_verification),
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
        f"{round_kind.capitalize()} remaining={remaining_verification}: "
        f"{verified_fixed} concerns fixed; {new_regressions} fix-introduced "
        f"regressions; {advisory} advisory suggestions; mode={mode}; "
        f"next={next_action}{reason}."
    )


__all__ = [
    "AUTHORIZED_ASSOCIATIONS",
    "FINDING_ACTIONS",
    "FindingDisposition",
    "MAINTAINER_ACTIONS",
    "MaintainerCommand",
    "MaintainerCommandResult",
    "apply_session_command",
    "authorized_maintainer",
    "disposition_honors_fingerprint",
    "parse_maintainer_command",
    "render_convergence_summary",
    "session_dispositions",
]

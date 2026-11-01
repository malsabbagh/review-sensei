"""Bounded local observations, never completion evidence or replay authority.

The context is only a diagnostic sink. Business code does not read its values.
No HTTP, file reads, ledger mutations or exception text are used to fill gaps.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Mapping

PREFIX = "review-sensei: lifecycle "
MAX_LINE_BYTES = 4096
_ACTIVE: ContextVar[LifecycleDiagnostics | None] = ContextVar(
    "review_lifecycle_diagnostics", default=None
)
_PHASES = {
    "entry",
    "bootstrap",
    "operation",
    "analysis",
    "publication",
    "outcome_write",
    "complete",
    "returned",
    "thread_cleanup",
    "finalization",
    "caller_close",
}
_STEPS = {
    "entry",
    "policy_checkout",
    "repository_policy",
    "default_policy_ref",
    "policy_content",
    "live_pr",
    "run_authority",
    "broker_oidc",
    "broker_actions",
    "actions_owner",
    "broker_session",
    "ledger_discovery",
    "predecessor_admission",
    "initial_enrollment",
    "analysis",
    "publication",
    "outcome_write",
    "operation",
    "caller_close",
}
_REASONS = {
    "thread_cleanup_unconfirmed",
    "publication_finalization_incomplete",
    "terminal_close_unconfirmed",
}
_PUBLICATION = {
    "published",
    "already_published",
    "auto_approval_disabled",
    "published_terminal_pending",
    "skipped",
    "refused",
    "stale_head",
    "stale_base",
    "disabled",
    "conflict",
    "failed",
    "not_published",
}
_DIAGNOSTICS = {
    "human_adjudication_open",
    "approval_withheld",
    "review_incomplete",
    "required_fixes_open",
    "app_authored",
    "qualification_unverified",
    "transaction-publication-complete",
}


def _hex(value: object, size: int) -> str | None:
    return (
        value
        if isinstance(value, str) and re.fullmatch(rf"[a-f0-9]{{{size}}}", value)
        else None
    )


def _number(value: object) -> int | None:
    return value if type(value) is int and 0 <= value <= 9_007_199_254_740_991 else None


def _name(value: object, *, ref: bool = False) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 200:
        return None
    pattern = r"[A-Za-z0-9._/-]+" if ref else r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+"
    return (
        value
        if re.fullmatch(pattern, value) and ".." not in value and "//" not in value
        else None
    )


def _label(value: object, choices: set[str]) -> str | None:
    return value if isinstance(value, str) and value in choices else None


def _scope(args: Any, policy_commit: object = None) -> dict[str, object]:
    return {
        # Slugs are recorded only after live admission, not from CLI text.
        "repository": None,
        "repository_id": _number(getattr(args, "repository_id", None)),
        "pull_request": _number(getattr(args, "pull_request", None)),
        "base_sha": _hex(getattr(args, "base_sha", None), 40),
        "head_sha": _hex(getattr(args, "head_sha", None), 40),
        "policy_commit": _hex(policy_commit, 40),
    }


class LifecycleDiagnostics:
    """One entry's observations; none are accepted as authority by a caller."""

    def __init__(self, kind: str) -> None:
        self.kind = _label(kind, {"review", "restart", "command", "reply"})
        self.phase = "entry"
        self.step = "entry"
        self.reason = "entry_failed"
        self.exit_code: int | None = None
        self.scope: dict[str, object] = {
            "requested": None,
            "observed": None,
            "admitted": None,
            "frozen": None,
            "observed_policy": None,
            "transition_reason": "not_evaluated",
        }
        self.predecessor: dict[str, object] | None = None
        self.publication: dict[str, object] | None = None
        self.caller: Any = None
        self.exception_kind: str | None = None
        self.broker: dict[str, object] | None = None

    def returned(self, code: int) -> int:
        self.exit_code = _number(code)
        if self.exception_kind is not None:
            return code
        if self.publication is not None:
            reason = self.publication["terminal_reason"]
            if reason is not None:
                self.reason = str(reason)
                self.phase = {
                    "thread_cleanup_unconfirmed": "thread_cleanup",
                    "terminal_close_unconfirmed": "caller_close",
                }.get(self.reason, "finalization")
            elif self.publication["accounting_terminal"] is True:
                self.phase, self.reason = "complete", "completed"
            else:
                self.reason = "publication_not_confirmed"
        elif code == 0:
            self.phase, self.reason = "returned", "operation_returned"
        else:
            self.reason = (
                "engine_stopped" if self.phase == "analysis" else "operation_failed"
            )
        return code

    def failed(self, error: BaseException) -> None:
        from review_sensei.broker_diagnostics import BrokerDiagnostic
        from review_sensei.errors import ReviewSenseiError

        if isinstance(error, KeyboardInterrupt):
            # The outer hosted CLI maps this interruption to exit 130.
            self.exit_code = 130

        # Use the existing validated diagnostic type, never exception text or
        # arbitrary response fields. Lookup details are intentionally omitted.
        diagnostic = getattr(error, "broker_diagnostic", None)
        if isinstance(diagnostic, BrokerDiagnostic):
            self.broker = {
                "code": diagnostic.code,
                "stage": diagnostic.stage,
                "upstream_status": diagnostic.upstream_status,
                "correlation_id": diagnostic.correlation_id,
            }

        self.exception_kind = (
            "interrupted"
            if isinstance(error, KeyboardInterrupt)
            else "input_or_protocol"
            if isinstance(error, ReviewSenseiError)
            else "os"
            if isinstance(error, OSError)
            else "value"
            if isinstance(error, ValueError)
            else "system_exit"
            if isinstance(error, SystemExit)
            else "unexpected"
        )
        self.reason = (
            "predecessor_admission_failed"
            if self.phase == "bootstrap"
            and self.step == "predecessor_admission"
            and self.predecessor is not None
            else self.phase + "_failed"
        )

    def _caller_view(self) -> dict[str, object]:
        caller = self.caller
        state = None if caller is None else caller._state
        if state is None:
            return {
                "accounting_available": False,
                "recorded_pending": None,
                "observed_dispatches": None
                if caller is None
                else len(caller.observations),
            }
        return {
            "accounting_available": True,
            "run_id": state.identity.run_id,
            "attempt": state.owner.run_attempt,
            "job": state.owner.job,
            "owner_generation": state.owner.generation,
            "frozen_api_request_limit": state.configuration.limit,
            "last_record_sha256": _hex(caller._root, 64),
            "review_terminal_transaction": state.terminal_transaction,
            "action_phase": None if state.action is None else state.action.get("phase"),
            "recorded_pending": sum(len(block.pending) for block in state.blocks),
            "recorded_completed": len(state.completed),
            "checkpoint_pending": caller._checkpoint is not None,
            "counts_are_complete_historical_effect_proof": False,
        }

    def _minimal(self, reason: str) -> str:
        return PREFIX + json.dumps(
            {
                "schema_version": "review-lifecycle-outcome-v1",
                "kind": _label(self.kind, {"review", "restart", "command", "reply"}),
                "phase": _label(self.phase, _PHASES),
                "step": _label(self.step, _STEPS),
                "reason": reason,
                "exit_code": _number(self.exit_code),
                "authority": False,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def emit(self) -> None:
        # Logging must not mask the original operation or expose opaque objects.
        try:
            value = {
                "schema_version": "review-lifecycle-outcome-v1",
                "kind": self.kind,
                "phase": self.phase,
                "step": self.step,
                "reason": self.reason,
                "exit_code": self.exit_code,
                "exception_kind": self.exception_kind,
                "broker": self.broker,
                "scope": self.scope,
                "predecessor": self.predecessor,
                "publication": self.publication,
                "caller": self._caller_view(),
                "artifacts": _label(
                    os.environ.get("ARTIFACTS"), {"none", "all", "errors"}
                )
                or "unavailable",
                "recovery_classification": "not_evaluated",
                "authority": False,
            }
            line = PREFIX + json.dumps(value, sort_keys=True, separators=(",", ":"))
        except Exception:
            line = self._minimal("diagnostic_unavailable")
        if len((line + "\n").encode()) > MAX_LINE_BYTES:
            line = self._minimal("diagnostic_size_limit")
        try:
            sys.stderr.write(line + "\n")
        except Exception:
            # Sink failure does not settle, retry or fail business work.
            pass


@contextmanager
def lifecycle(kind: str) -> Iterator[LifecycleDiagnostics]:
    value = LifecycleDiagnostics(kind)
    token = _ACTIVE.set(value)
    try:
        yield value
    except BaseException as error:
        value.failed(error)
        raise
    finally:
        try:
            value.emit()
        finally:
            _ACTIVE.reset(token)


def phase(value: str) -> None:
    current = _ACTIVE.get()
    if current is not None and value in _PHASES:
        current.phase = value
        if value in _STEPS:
            current.step = value


def step(value: str) -> None:
    current = _ACTIVE.get()
    if current is not None and value in _STEPS:
        current.step = value


def requested(args: Any, policy_commit: str) -> None:
    current = _ACTIVE.get()
    if current is not None:
        current.scope["requested"] = _scope(args, policy_commit)


def caller(value: Any) -> None:
    current = _ACTIVE.get()
    if current is not None:
        current.caller = value


def observed(pull: dict[str, Any]) -> None:
    current = _ACTIVE.get()
    if current is None:
        return
    base, head = pull.get("base"), pull.get("head")
    if isinstance(base, dict) and isinstance(head, dict):
        current.scope["observed"] = {
            "source": "bootstrap_pr_read",
            "base_ref": _name(base.get("ref"), ref=True),
            "base_sha": _hex(base.get("sha"), 40),
            "head_sha": _hex(head.get("sha"), 40),
        }


def observed_policy(
    branch: str, commit: object, content_sha256: str | None = None
) -> None:
    current = _ACTIVE.get()
    if current is not None:
        current.scope["observed_policy"] = {
            "source": "authenticated_policy_file"
            if content_sha256 is not None
            else "default_ref_read",
            "policy_ref": _name(branch, ref=True),
            "policy_commit": _hex(commit, 40),
            "policy_content_sha256": _hex(content_sha256, 64),
        }


def predecessor(record: Any, accounting: Any) -> None:
    current = _ACTIVE.get()
    if current is None:
        return
    tx = record.transaction
    current.predecessor = {
        "record_sha256": _hex(record.record_sha256, 64),
        "generation": _number(record.generation),
        "run_id": _number(accounting.identity.run_id),
        "transaction_present": tx is not None,
        "transaction_id": None if tx is None else _hex(tx.transaction_id, 64),
        "phase": None
        if tx is None
        else _label(
            tx.phase,
            {
                "analysis",
                "publication_pending",
                "publication_failed",
                "publication_succeeded",
                "publication_suppressed",
            },
        ),
        "base_sha": None if tx is None else _hex(tx.base_sha, 40),
        "base_ref": None,
        "head_sha": None if tx is None else _hex(tx.head_sha, 40),
        "policy_commit": _hex(accounting.configuration.commit, 40),
        "policy_content_sha256": _hex(accounting.configuration.digest, 64),
        "terminal_transaction": _hex(accounting.terminal_transaction, 64),
        "frozen_api_request_limit": accounting.configuration.limit,
        "recorded_completed": len(accounting.completed),
        "recorded_pending": sum(len(block.pending) for block in accounting.blocks),
        "counts_are_complete_effect_history": False,
        "original_tail_reason": "unavailable",
    }


def admitted(run: Any) -> None:
    current = _ACTIVE.get()
    if current is None:
        return
    state = run.caller.accounting
    current.scope["admitted"] = {
        "repository": _name(run.identity.repository),
        "repository_id": _number(run.identity.repository_id),
        "pull_request": _number(run.identity.pull_request),
        "base_ref": _name(run.base_branch, ref=True),
        "base_sha": _hex(run.base_sha, 40),
        "head_sha": _hex(run.head_sha, 40),
        "head_repository": _name(run.head_repository),
        "policy_commit": _hex(state.configuration.commit, 40),
        "policy_content_sha256": _hex(state.configuration.digest, 64),
    }
    # Commands/replies execute outside ReviewRun.execute. Successful admission
    # must not leave their subsequent failures attributed to an old root.
    current.phase = current.step = "operation"


def analysis(value: Any) -> None:
    current = _ACTIVE.get()
    if current is None or value is None:
        return
    tx = value.result.transaction
    current.scope["frozen"] = {
        "base_sha": _hex(tx.base_sha, 40),
        "head_sha": _hex(tx.head_sha, 40),
        "transaction_id": _hex(tx.transaction_id, 64),
        "review_configuration_sha256": _hex(tx.configuration_digest, 64),
        "diff_sha256": hashlib.sha256(value.diff.encode("utf-8")).hexdigest(),
        "effective_merge_base": None,
    }


def publication(value: Mapping[str, object]) -> None:
    current = _ACTIVE.get()
    if current is not None:
        current.publication = {
            "status": _label(value.get("status"), _PUBLICATION),
            "publication_status": _label(value.get("publication_status"), _PUBLICATION),
            "accounting_terminal": value.get("accounting_terminal")
            if type(value.get("accounting_terminal")) is bool
            else None,
            "terminal_reason": _label(value.get("terminal_reason"), _REASONS),
            "diagnostic": _label(value.get("diagnostic"), _DIAGNOSTICS),
        }

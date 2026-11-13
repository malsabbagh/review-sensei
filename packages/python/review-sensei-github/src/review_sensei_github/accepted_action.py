"""Cold-readable accepted action observations, never execution capabilities.

The existing caller accounting writer is the durable producer. This adapter
normalizes its native-root observation for ordinary source lifecycle consumers.
An action has no recorded review base or finding-state inventory: its outcome,
request counters and root hash cannot establish semantic convergence progress.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from review_sensei.api_work import ApiRunAccounting
from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.session import SessionIdentity, SessionRecord

from .api_run_history import read_action_result
from .session_ledger import GitHubIssueCommentSessionLedger


@dataclass(frozen=True)
class AcceptedActionObservation:
    """Immutable diagnostics bound to one observed root, not a latest-state claim.

    ``execution_started=False`` is not non-dispatch or cessation evidence.
    ``terminal_outcome`` describes the authenticated retained result, not current
    approval, finding closure, or a semantic-progress receipt. No conversion to
    the G13 ``AcceptedOperationRecord`` is supplied because base/inventory proof
    is absent. In particular an explicit no-op may still have finalization work.
    """

    root_sha256: str
    accounting_sha256: str
    run_id: int
    operation_key: str
    purpose: str
    head_sha: str
    source_kind: str
    source_id: int
    source_updated_at: str
    source_body_sha256: str
    phase: str
    execution_started: bool
    uncertain_requests: int
    terminal_outcome: str | None
    result_sha256: str | None

    def source_document(self) -> dict[str, Any]:
        return {
            "kind": self.source_kind,
            "id": self.source_id,
            "updated_at": self.source_updated_at,
            "body_sha256": self.source_body_sha256,
        }


def observe_selected_action(
    ledger: GitHubIssueCommentSessionLedger,
    identity: SessionIdentity,
    record: SessionRecord,
) -> AcceptedActionObservation | None:
    """Normalize an already native-selected record, without a second root read.

    The caller owns native discovery and freshness, just as for
    ``read_action_result``. A parsed/copied record alone grants no authority.
    Unsupported prospective accounting versions refuse on this ordinary route;
    terminal results still pass the existing exact binding/result decoder.
    """
    if not isinstance(record, SessionRecord) or record.identity() != identity:
        raise ReviewInputError("accepted action record scope conflicts")
    # Frozen records still expose nested mappings. Revalidate and detach the
    # selected snapshot so post-construction mutation cannot keep the old root
    # label while changing the observed action. This is integrity, not native
    # discovery, freshness, writer exclusion or recovery authority.
    record = SessionRecord.from_dict(record.to_dict())
    if record.identity() != identity:
        raise ReviewInputError("accepted action record scope conflicts")
    if record.api_run_accounting is None:
        raise ReviewInputError("accepted action record lacks caller accounting")
    state = ApiRunAccounting.from_document(record.api_run_accounting)
    state.require_writer_version()
    action = state.action
    if action is None:
        return None
    intent = action["intent"]
    source = intent["source"]
    result_sha256 = None
    terminal_outcome = None
    if action["phase"] == "terminal":
        result = read_action_result(ledger, identity, record)
        result_sha256 = hashlib.sha256(canonical_bytes(result)).hexdigest()
        terminal_outcome = result["outcome"]["kind"]
    return AcceptedActionObservation(
        root_sha256=record.record_sha256,
        accounting_sha256=hashlib.sha256(
            canonical_bytes(record.api_run_accounting)
        ).hexdigest(),
        run_id=state.identity.run_id,
        operation_key=intent["operation_key"],
        purpose=intent["purpose"],
        head_sha=intent["head_sha"],
        source_kind=source["kind"],
        source_id=source["id"],
        source_updated_at=source["updated_at"],
        source_body_sha256=source["body_sha256"],
        phase=action["phase"],
        execution_started=action["execution_started"],
        uncertain_requests=state.uncertain,
        terminal_outcome=terminal_outcome,
        result_sha256=result_sha256,
    )


def read_current_action(
    ledger: GitHubIssueCommentSessionLedger,
    identity: SessionIdentity,
) -> AcceptedActionObservation | None:
    """Read exactly one native root under the existing ledger/caller budget.

    No ancestor walk, replacement budget, background reconciliation, write,
    history truncation or retained-result replay is performed. Missing/invalid
    authority refuses rather than becoming an empty accepted-operation history.
    An actionless authenticated root returns None, not proof of no earlier work.
    """
    if not isinstance(ledger, GitHubIssueCommentSessionLedger):
        raise ReviewInputError("accepted action requires a native ledger")
    if not isinstance(ledger.app_slug, str) or not ledger.app_slug.strip():
        raise ReviewInputError("accepted action requires configured Bot ownership")
    if ledger.evidence_budget is None:
        raise ReviewInputError("accepted action requires the existing read budget")
    loaded = ledger.load(identity)
    if loaded.status != "ok" or loaded.record is None:
        raise ReviewInputError("accepted action current root is unavailable")
    return observe_selected_action(ledger, identity, loaded.record)

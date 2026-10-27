"""Read-only historical evidence under a separately authenticated current run.

These process-issued selections cannot be restored from diagnostic JSON. The
original base/head identifies evidence; the current base/head fences authority.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field, replace
from types import SimpleNamespace
from typing import Any

from review_sensei.api_work import ApiRunAccounting
from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.configuration import MAX_CONFIG_BYTES, parse_configuration_text
from review_sensei.convergence import resolve_review_convergence_policy
from review_sensei.models import ReviewResult
from review_sensei.session import SessionLoadResult

from .action_completion import CompletionState, _issue, _issued
from .api_run_host import review_startup_plan
from .default_human_inventory import HumanInventoryStore, build_human_inventory_context
from .default_human_reader import (
    restore_admitted_result,
    transaction_publication_digest,
)
from .publication_recovery import _event_key, _payload
from .review_completion_recovery import (
    _confirm_native_effects,
    _digest,
    _get,
    _ReadOnlyHttp,
    _refuse,
)
from .session_ledger import GitHubIssueCommentSessionLedger


@dataclass(frozen=True)
class HistoricalReadSelection:
    caller: Any = field(repr=False)
    ledger: Any = field(repr=False)
    identity: Any
    configuration: Any
    owner: Any
    record: Any = field(repr=False)
    actions: Any = field(repr=False)
    stopped: Any
    current_base: str
    current_head: str
    authority: Any = field(repr=False)
    state: CompletionState = CompletionState.UNCONFIRMED

    def require_process(self):
        from .run_authority import parse_authority

        authority = parse_authority(self.authority, grant=True)
        if (
            not _issued(self)
            or self.caller._state is not None
            or self.caller.attempt != 1
            or self.caller.plan != review_startup_plan(self.caller.job)
            or self.owner.run_attempt != 1
            or self.owner.generation != 0
            or authority["purpose"] != "review"
            or authority["run_id"] != str(self.caller.run.run_id)
            or authority["run_attempt"] != 1
            or authority["job"] != self.owner.job
            or authority["repository"] != self.identity.repository
            or authority["repository_id"] != self.identity.repository_id
            or authority["pull_request"] != self.identity.pull_request
            or authority["head_sha"] != self.current_head
        ):
            _refuse("historical reader lacks current startup authority")
        self.caller._require_ledger(self.ledger, self.identity)

    def select(self, loaded):
        self.require_process()
        if (
            loaded.status != "ok"
            or loaded.record is None
            or loaded.record.record_sha256 != self.record.record_sha256
        ):
            _refuse("historical source root changed")
        return self.record.transaction

    def require_current_pull(self, pull):
        self.require_process()
        if (
            not isinstance(pull, dict)
            or pull.get("number") != self.identity.pull_request
            or pull.get("state") != "open"
            or pull.get("draft") is not False
            or not isinstance(pull.get("user"), dict)
            or not isinstance(pull["user"].get("login"), str)
            or pull.get("head", {}).get("sha") != self.current_head
            or pull.get("base", {}).get("sha") != self.current_base
            or pull.get("base", {}).get("repo", {}).get("id")
            != self.identity.repository_id
            or pull.get("base", {}).get("repo", {}).get("full_name")
            != self.identity.repository
        ):
            _refuse("fresh current PR authority changed")

    def require_current_owner(self):
        self.require_process()
        old = ApiRunAccounting.from_document(self.record.api_run_accounting)
        if (
            self.actions.qualified_publication_cessation(
                self.identity,
                old.identity.run_id,
                old.owner,
                head_sha=self.record.transaction.head_sha,
            )
            != self.stopped
            or self.actions.current_owner(self.identity, attempt=1, generation=0)
            != self.owner
        ):
            _refuse("historical cessation or current owner changed")


@dataclass(frozen=True)
class HistoricalPublicationCompletion:
    selection: HistoricalReadSelection = field(repr=False)
    receipt_bytes: bytes = field(repr=False)
    state: CompletionState = CompletionState.UNCONFIRMED

    @property
    def receipt(self):
        return json.loads(self.receipt_bytes)

    def require(self, *, caller, ledger, identity, configuration, owner, previous):
        from review_sensei.historical_completion import validate_receipt

        s = self.selection
        if (
            not _issued(self)
            or not _issued(s)
            or s.caller is not caller
            or s.ledger is not ledger
            or s.identity != identity
            or s.configuration != configuration
            or s.owner != owner
            or s.record.record_sha256 != previous.record_sha256
            or self.receipt["historical_accounting_sha256"]
            != _digest(previous.api_run_accounting)
        ):
            _refuse("historical completion is stale, restored or foreign")
        s.require_process()
        validate_receipt(self.receipt)


def _complete_selection(selection, effects):
    from review_sensei.historical_completion import RECEIPT_VERSION, validate_receipt

    s, t = selection, selection.record.transaction
    old = ApiRunAccounting.from_document(s.record.api_run_accounting)
    receipt = {
        "schema_version": RECEIPT_VERSION,
        "verifier_version": "review-sensei-github/historical-completion-v1",
        "repository": s.identity.repository,
        "repository_id": s.identity.repository_id,
        "pull_request": s.identity.pull_request,
        "historical_run_id": old.identity.run_id,
        "historical_job_id": s.stopped["job_id"],
        "historical_review_step": s.stopped["step_number"],
        "historical_executor_sha": s.stopped["executor_sha"],
        "historical_executor_commit": s.stopped["executor_commit"],
        "historical_workflow_blob": s.stopped["workflow_blob"],
        "writer_inventory_sha256": s.stopped["writer_inventory_sha256"],
        "termination_basis": s.stopped["contract"],
        "historical_root_sha256": s.record.record_sha256,
        "historical_accounting_sha256": _digest(old.to_document()),
        "transaction_id": t.transaction_id,
        "historical_base_sha": t.base_sha,
        "historical_head_sha": t.head_sha,
        "historical_policy_sha256": t.policy_digest,
        "historical_configuration_sha256": t.configuration_digest,
        "historical_evidence_sha256": t.evidence_digest,
        "historical_content_sha256": t.result_sha256,
        "publication_result_sha256": transaction_publication_digest(t),
        "cessation_sha256": _digest(s.stopped),
        "current_run_id": s.caller.run.run_id,
        "current_authority_sha256": _digest(s.authority),
        "current_executor_sha": s.authority["job_workflow_sha"],
        "current_owner_sha256": _digest(asdict(s.owner)),
        "current_policy_commit": s.configuration.commit,
        "current_configuration_sha256": s.configuration.digest,
        "current_base_sha": s.current_base,
        "current_head_sha": s.current_head,
        **effects,
    }
    validate_receipt(receipt)
    return _issue(
        HistoricalPublicationCompletion(
            s, canonical_bytes(receipt), CompletionState.CONFIRMED
        )
    )


class HistoricalSnapshotLedger(GitHubIssueCommentSessionLedger):
    """An explicitly historical snapshot; never pretends to be the live root."""

    def __init__(self, selection):
        self.__dict__.update(selection.ledger.__dict__)
        self.selection = selection

    def load(self, identity, **kwargs):
        self.selection.require_process()
        if identity != self.selection.identity:
            _refuse("historical ledger identity changed")
        return SessionLoadResult(status="ok", record=self.selection.record)

    def replace(self, *args, **kwargs):
        _refuse("historical ledger permits reads only")

    def _request(self, method, path, *, body=None):
        if method != "GET":
            _refuse("historical ledger permits reads only")
        return super()._request(method, path, body=body)


def _historical_configuration(selection):
    """Read a hash-selected blob, without executing historical code or hooks."""
    old = ApiRunAccounting.from_document(selection.record.api_run_accounting)
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(
            [
                "git",
                "--no-pager",
                "show",
                "--no-textconv",
                old.configuration.commit + ":.reviewsensei.yml",
            ],
            stdout=output,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        output.seek(0)
        raw = output.read(MAX_CONFIG_BYTES + 1)
    if (
        result.returncode
        or len(raw) > MAX_CONFIG_BYTES
        or hashlib.sha256(raw).hexdigest() != old.configuration.digest
    ):
        _refuse("original frozen policy blob is unavailable or changed")
    configuration = parse_configuration_text(
        raw.decode("utf-8"), source="historical frozen policy"
    )
    if (
        not configuration.github.writes
        or configuration.api_request_limit != old.configuration.limit
    ):
        _refuse("original frozen policy conflicts with historical accounting")
    return configuration


def authenticate_historical_publication(
    *,
    caller,
    ledger,
    identity,
    configuration,
    owner,
    previous,
    actions,
    current_base,
    current_head,
    pull,
    authority,
):
    """Confirm an old publication before a distinct, freshly authorized run."""
    caller._require_ledger(ledger, identity)
    old = ApiRunAccounting.from_document(previous.api_run_accounting)
    transaction = previous.transaction
    from .api_run_history import inherited_preprovider_reference

    if inherited_preprovider_reference(previous, old) is not None:
        _refuse("inherited publication needs its original run's pre-provider successor")
    if (
        caller._state is not None
        or caller.attempt != 1
        or old.identity == caller.run
        or old.identity.repository_id != caller.run.repository_id
        or old.terminal_transaction is not None
        or old.action is not None
        or old.analysis_abort is not None
        or transaction is None
        or transaction.phase != "publication_succeeded"
        or previous.reservation_id is not None
        or previous.reserved_slot is not None
        or previous.operator_paused
        or (transaction.base_sha, transaction.head_sha) == (current_base, current_head)
    ):
        _refuse("predecessor is outside historical completion scope")
    stopped = actions.qualified_publication_cessation(
        identity, old.identity.run_id, old.owner, head_sha=transaction.head_sha
    )
    selection = _issue(
        HistoricalReadSelection(
            caller,
            ledger,
            identity,
            configuration,
            owner,
            previous,
            actions,
            stopped,
            current_base,
            current_head,
            authority,
            CompletionState.CONFIRMED,
        )
    )
    selection.require_current_pull(pull)
    http = _ReadOnlyHttp(ledger.http)
    token, budget = ledger.token, caller.budget_view
    snapshot = HistoricalSnapshotLedger(selection)
    original_config = _historical_configuration(selection)
    policy = resolve_review_convergence_policy(mode="merge-focused")
    if policy.digest() != transaction.policy_digest:
        _refuse("original convergence format is unsupported for historical reading")
    context = build_human_inventory_context(
        http=http,
        token=token,
        ledger=snapshot,
        identity=identity,
        base_sha=transaction.base_sha,
        head_sha=transaction.head_sha,
        app_slug=ledger.app_slug,
        publication_digest=transaction_publication_digest(transaction),
        policy_digest=transaction.policy_digest,
        configuration_digest=transaction.configuration_digest,
        evidence_digest=transaction.evidence_digest,
        budget=budget,
        _historical_source=selection,
    )
    from .operation_comments import GitHubIssueCommentOperationPort, ResultPartStore

    port = GitHubIssueCommentOperationPort(
        http,
        token=token,
        repository=identity.repository,
        pull_request=identity.pull_request,
        app_user_id=context.producer_id,
        strict_association=True,
    )
    store = ResultPartStore(port, app_user_id=context.producer_id)
    available = store.read_available(event_key=_event_key(identity, transaction))
    if available is None:
        _refuse("original retained result is missing")
    result = ReviewResult.from_dict(json.loads(available[1]))
    if (
        _payload(result) != available[1]
        or result.content_digest() != transaction.result_sha256
        or result.transaction is None
        or result.transaction.transaction_id != transaction.transaction_id
        or result.coverage_only_partial
    ):
        _refuse("original retained result or tail is unsupported")
    from .prior_rescan_publication import admitted_rescan

    if admitted_rescan(previous) is not None:
        _refuse("explicit rescan needs its separate historical tail receipt")
    result = replace(result, transaction=transaction)
    admitted = HumanInventoryStore(http, token, snapshot, context)
    result = restore_admitted_result(result, admitted)
    from .historical_projection import historical_projection
    from .publication import ReviewPublisher

    projected = historical_projection(result, admitted, identity, policy)
    historical_run = SimpleNamespace(
        base_sha=transaction.base_sha,
        head_sha=transaction.head_sha,
        plan=SimpleNamespace(policy=original_config.github),
    )
    effects = _confirm_native_effects(
        run=historical_run,
        analysis=SimpleNamespace(policy=policy),
        context=context,
        store=admitted,
        result=result,
        assembled=projected,
        publisher=ReviewPublisher(http=http),
        http=http,
        token=token,
        identity=identity,
        pull=pull,
        current_base=current_base,
        current_head=current_head,
    )
    loaded = ledger.load(identity)
    selection.select(loaded)
    selection.require_current_owner()
    selection.require_current_pull(
        _get(
            http,
            http.repository_path(
                identity.repository, f"/pulls/{identity.pull_request}"
            ),
            token,
        )
    )
    budget.check()
    return _complete_selection(selection, effects)

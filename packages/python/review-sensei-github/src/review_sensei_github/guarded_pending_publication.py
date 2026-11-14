"""Closed pending-history publication sizing and negative execution fences.

No projection is installed as state or authority. Every actual native request,
current quarantine read and checkpoint still uses its ordinary source fences.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import sys
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import Any
from weakref import WeakKeyDictionary

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError


def checkpoint_branches(journal, branches):
    """Price coalesced success prefixes and every independently reachable stop.

    Unlike ordinary source recovery, guarded publication does not resume a new
    native effect after an unknown predecessor. Normal receipts may coalesce
    into the next pending checkpoint. A final checkpoint is required whether
    the invocation succeeds, is rejected, or retains a pending outcome.
    """
    from .finding_lifecycle import MAX_EFFECTS, _digest, _require_effect_scope
    from .ordinary_publication_plan import _capacity_request_identity

    if not branches or journal.effects:
        raise ReviewInputError("guarded initial publication prefix is unsupported")
    alternatives = []
    for requests in branches:
        if not requests or len(requests) > MAX_EFFECTS:
            raise ReviewInputError("guarded publication effect count is unsupported")
        shadow = copy.copy(journal)
        shadow.effects = []
        prefix = []
        for method, path, body, precondition in requests:
            request = _capacity_request_identity(method, path, body)
            effect = {
                "method": method,
                "path": path,
                "request": request,
                "request_sha256": _digest(
                    {"method": method, "path": path, "request": request}
                ),
                "status": "pending",
                "response": None,
            }
            if precondition is not None:
                effect["check_update_precondition"] = precondition
            shadow.effects.append(effect)
            _require_effect_scope(
                shadow._document(), journal.identity, journal.head_sha
            )
            pending, future, confirmed = shadow._preflight_effect(capacity_only=True)
            prefix.append(pending)
            # Future includes confirmed, rejected and still-pending outcomes,
            # including the source's unknown-effect hold and allocation widths.
            alternatives.append((*prefix, future))
            shadow.effects = confirmed
    return tuple(alternatives)


def preflight_branches(journal, branches, *, read_calls):
    """Pure complete checkpoint quote; caller must supply source-bound reads."""
    from review_sensei.finding_lifecycle_store import preflight_finding_lifecycle

    if type(read_calls) is not int or read_calls < 0:
        raise ReviewInputError("guarded publication native read quote is invalid")
    alternatives = checkpoint_branches(journal, branches)
    documents = tuple(
        max(
            (branch[min(index, len(branch) - 1)] for branch in alternatives),
            key=lambda document: len(canonical_bytes(document)),
        )
        for index in range(max(map(len, alternatives)))
    )
    return preflight_finding_lifecycle(
        journal.ledger,
        journal._record,
        document=documents[0],
        future_documents=documents[1:],
        operation_id=journal.operation_id,
        base_sha=journal.base_sha,
        head_sha=journal.head_sha,
        policy_digest=journal.policy_digest,
        configuration_digest=journal.configuration_digest,
        item_count=max(map(len, branches)) + 1,
        reserved_tail_calls=read_calls + max(map(len, branches)) + 6,
    )


def _original_methods(owner):
    from review_sensei.source_registry import require_instance_source

    require_instance_source(owner, methods=())
    names = tuple(n for n in dir(type(owner)) if callable(getattr(type(owner), n)))
    if any(name in vars(owner) for name in names):
        raise ReviewInputError("guarded publication source callable was shadowed")
    require_instance_source(
        owner,
        methods=tuple(
            n
            for n in names
            if hasattr(getattr(type(owner), n), "__code__")
            or hasattr(getattr(type(owner), n), "__func__")
        ),
    )


def _fixed_record(record):
    value = record.to_dict()
    for key in (
        "generation",
        "record_sha256",
        "updated_at",
        "api_run_accounting",
        "finding_lifecycle",
    ):
        value.pop(key, None)
    return canonical_bytes(value)


def _original_accounting_graph(value, *, models=False):
    """Reject callback-bearing data before reading a price or comparing blocks."""
    from dataclasses import fields

    from review_sensei import api_work
    from review_sensei.source_registry import require_instance_source

    count = 0

    def visit(item, ancestors, depth):
        nonlocal count
        count += 1
        if count > 20_000 or depth > 32:
            raise ReviewInputError("guarded publication accounting graph is unbounded")
        kind = type(item)
        if item is None or kind in (str, int, bool, float):
            return
        if id(item) in ancestors:
            raise ReviewInputError("guarded publication accounting graph is cyclic")
        ancestors = {*ancestors, id(item)}
        if kind in (tuple, list):
            for child in item:
                visit(child, ancestors, depth + 1)
            return
        if kind is dict:
            for key, child in item.items():
                if type(key) is not str:
                    raise ReviewInputError(
                        "guarded publication accounting keys changed"
                    )
                visit(child, ancestors, depth + 1)
            return
        if not models or kind.__module__ != api_work.__name__:
            raise ReviewInputError(
                "guarded publication accounting source is unsupported"
            )
        require_instance_source(item, methods=())
        for field in fields(kind):
            visit(getattr(item, field.name), ancestors, depth + 1)

    visit(value, set(), 0)


def _terminal_reservation(journal):
    """Prove the original, untouched terminal tranche owns completion's CAS."""
    from review_sensei import api_work, session
    from review_sensei.api_work import LEGACY_ACCOUNTING_VERSION, ApiRunAccounting
    from review_sensei.source_registry import require_module_seal

    from . import api_run_plan, api_run_requests, caller_operation, trust_effects
    from .api_run_plan import terminal_requests
    from .caller_operation import prewrite_fence_requests

    for source in (session, api_work, api_run_plan, api_run_requests, caller_operation):
        require_module_seal(source)
    if (
        trust_effects.verification_calls
        is not api_run_requests._original_trust_verification_calls
        or trust_effects.verification_profile
        is not api_run_requests._original_trust_verification_profile
    ):
        raise ReviewInputError("guarded publication verification source changed")
    ledger = journal.ledger
    caller = ledger.caller
    _original_methods(caller)
    require_module_seal(session)
    application = sys.modules.get("review_sensei_github.application")
    if (
        application is None
        or application.complete_review_publication
        is not session.complete_review_publication
    ):
        raise ReviewInputError("guarded publication completion source changed")
    state = caller.accounting
    _original_accounting_graph(state, models=True)
    _original_accounting_graph(journal._record.api_run_accounting)
    if getattr(caller, "_controlled_restart_admission", None) is not None:
        raise ReviewInputError("guarded publication restart tail is unqualified")
    expected_terminal = (
        "hosted-terminal" if state.owner.generation == 0 else "successor-terminal"
    )
    if caller._terminal_block != expected_terminal:
        raise ReviewInputError("guarded publication original terminal name changed")
    required = terminal_requests(
        state.configuration.limit,
        startup=state.startup.liability,
        successor=state.owner.run_attempt > 1,
        legacy=state.version == LEGACY_ACCOUNTING_VERSION,
    )
    blocks = [b for b in state.blocks if b.block_id == caller._terminal_block]
    selected = ApiRunAccounting.from_document(journal._record.api_run_accounting)
    durable = [b for b in selected.blocks if b.block_id == caller._terminal_block]
    if (
        required < 50
        or len(blocks) != 1
        or len(durable) != 1
        or blocks[0] != durable[0]
        or blocks[0].issued_by != state.owner
        or blocks[0].undispatched != required
        or blocks[0].pending
        or ledger._terminal_claimed
        or caller._terminal_reason is not None
        or caller._block == caller._terminal_block
        or prewrite_fence_requests(caller) != 0
    ):
        raise ReviewInputError(
            "guarded publication terminal reservation is unqualified"
        )
    return blocks[0]


def _source(journal):
    """Require the actual current issued reader and its unchanged cold sources."""

    from .application import GitHubApplication
    from .quarantine_capacity import installed_capacity
    from .quarantine_origin import require_authenticated_read_owner

    _original_methods(journal)
    ledger, record = journal.ledger, journal._record
    _original_methods(ledger)
    caller = ledger.caller
    _original_methods(caller)
    _original_methods(record)
    if (
        record is not caller._effect_record
        or record.record_sha256 != caller._root
        or caller._checkpoint is not None
    ):
        raise ReviewInputError("guarded publication selected root changed")
    _original_methods(journal.reviewer)
    _original_methods(journal.reviewer.checks)
    _original_methods(journal.reviewer.finalizer)
    _terminal_reservation(journal)
    from .ordinary_publication_reads import issued_witness

    if issued_witness(journal) is not None:
        raise ReviewInputError(
            "guarded publication cannot resume ordinary hot recovery"
        )
    factory = journal.reviewer.finalizer.quarantine_reader_factory
    app = getattr(factory, "__self__", None)
    if (
        type(app) is not GitHubApplication
        or getattr(factory, "__func__", None)
        is not GitHubApplication._quarantine_hold_reader
    ):
        raise ReviewInputError("guarded publication current reader factory changed")
    _original_methods(app)
    from .quarantine_holds import QuarantineHoldReader

    hold_reader = app._quarantine_hold_readers.get(
        (journal.identity.repository, journal.identity.pull_request)
    )
    if (
        app.session_ledger is not ledger
        or type(hold_reader) is not QuarantineHoldReader
        or hold_reader.ledger is not ledger
        or hold_reader.repository != journal.identity.repository
        or hold_reader.pull_request != journal.identity.pull_request
    ):
        raise ReviewInputError("guarded publication cached reader custody changed")
    _original_methods(hold_reader)
    finalizer = journal.reviewer.finalizer
    if (
        any(
            getattr(finalizer, name) is not None
            for name in (
                "historical_assessment_reader_factory",
                "native_union_reader_factory",
                "prior_publication_reader_factory",
            )
        )
        or journal.reviewer._pending_thread_cleanup is not None
    ):
        raise ReviewInputError("guarded publication additional reader is unpriced")
    reader = getattr(ledger, "_quarantine_origin_reader", None)
    if reader is None:
        raise ReviewInputError("guarded publication origin reader is unavailable")
    _original_methods(reader)
    _original_methods(reader.current_scope)
    authenticated = getattr(ledger, "_quarantine_authenticated", None)
    if authenticated is None:
        raise ReviewInputError(
            "guarded publication authenticated origin is unavailable"
        )
    require_authenticated_read_owner(authenticated, ledger, caller, journal.identity)
    if authenticated.record_sha256 != record.record_sha256:
        from .quarantine_checkpoint import confirmed_checkpoint_capacity

        receipt = confirmed_checkpoint_capacity(caller, ledger, journal.identity)
        if receipt.record is not record:
            raise ReviewInputError(
                "guarded publication current origin receipt is stale"
            )
    lineage = authenticated.lineage
    if canonical_bytes(record.quarantine_lineage) != authenticated.lineage_bytes:
        raise ReviewInputError("guarded publication origin manifest changed")
    cache_key = (lineage["origin_manifest"]["sha256"], lineage["scope_sha256"])
    expected = canonical_bytes(
        {
            "sources": json.loads(authenticated.source_documents_bytes),
            "inventories": authenticated.inventories,
        }
    )
    if reader._authenticated_sources.get(cache_key) != expected:
        raise ReviewInputError(
            "guarded publication cold source reconstruction is unpriced"
        )
    guard = installed_capacity(ledger, journal.identity)
    return authenticated, guard, reader, cache_key, expected


def _empty_state(journal):
    from review_sensei.finding_lifecycle import decode_lifecycle_state

    state = decode_lifecycle_state(journal._document()["lifecycle"]).state
    inventory = getattr(state, "inventory", None) or getattr(
        state, "authoritative_decision_inventory", None
    )
    if (
        inventory is None
        or inventory.obligations
        or journal._document()["native_receipts"]
    ):
        raise ReviewInputError("guarded publication fresh native cleanup is unpriced")


def _request_shape(method, path, body):
    import re

    return canonical_bytes(
        {
            "method": method,
            "path": re.sub(r"/check-runs/[1-9][0-9]*$", "/check-runs/{id}", path),
            "body": body,
        }
    )


_ACTIVE: ContextVar[Any] = ContextVar("guarded_pending_publication", default=None)
_ISSUED: WeakKeyDictionary[Any, Any] = WeakKeyDictionary()
_INSTALLED: set[str] = set()


@dataclass(frozen=True, eq=False)
class _BatchContext:
    payload: bytes


def install_source(cls):
    current_frame = inspect.currentframe()
    frame = current_frame.f_back if current_frame is not None else None
    module = sys.modules.get(cls.__module__)
    spec = getattr(module, "__spec__", None)
    get_code = getattr(getattr(spec, "loader", None), "get_code", None)
    if (
        _INSTALLED
        or frame is None
        or module is None
        or frame.f_globals is not vars(module)
        or frame.f_locals is not frame.f_globals
        or not getattr(spec, "_initializing", False)
        or not callable(get_code)
        or frame.f_code != get_code(cls.__module__)
    ):
        raise ReviewInputError("guarded publication requires original source installer")
    _INSTALLED.add("publication")
    original = cls._publish_impl

    @wraps(original)
    def invoke(owner, *args, **kwargs):
        scope = [owner, True]
        token = _ACTIVE.set(scope)
        try:
            return original(owner, *args, **kwargs)
        finally:
            scope[1] = False
            _ACTIVE.reset(token)

    cls._publish_impl = invoke


def batch_context(journal):
    issued = _ISSUED.get(journal)
    if issued is None:
        return None
    (
        context,
        payload,
        scope,
        fixed,
        lineage,
        owner,
        capacity,
        budget,
        transport,
        tokens,
        operation,
    ) = issued
    from review_sensei.publication_intent_fusion import committed_transition

    transition = committed_transition(journal)
    if transition is not None:
        before, after = transition
        if _fixed_record(before) != fixed:
            raise ReviewInputError("guarded publication fused predecessor changed")
        fixed = _fixed_record(after)
    if (
        type(context) is not _BatchContext
        or context.payload != payload
        or not scope[1]
        or _ACTIVE.get() is not scope
        or scope[0] is not journal.reviewer
        or journal.ledger.caller.accounting.owner != owner
        or journal.ledger.evidence_budget is not budget
        or journal.ledger.http is not transport
        or _fixed_record(journal._record) != fixed
        or (
            journal.operation_id,
            journal.head_sha,
            journal.base_sha,
            journal.policy_digest,
            journal.configuration_digest,
        )
        != operation
        or tuple(
            hashlib.sha256((value or "").encode()).digest()
            for value in (journal._review_token, journal._check_token)
        )
        != tokens
    ):
        raise ReviewInputError("guarded publication batch invocation changed")
    authenticated, guard, reader, key, expected = _source(journal)
    if (
        authenticated.lineage_bytes != lineage
        or (guard, reader, key, expected) != capacity
    ):
        raise ReviewInputError("guarded publication batch historical source changed")
    _empty_state(journal)
    return context


def _three_checkpoints(journal, branch):
    from .finding_lifecycle import _digest, _pack_journal
    from .guarded_publication_batch import describe_pair
    from .ordinary_publication_plan import _capacity_request_identity

    def effect(request):
        method, path, raw, precondition = request
        body = _capacity_request_identity(method, path, raw)
        value = dict(
            method=method,
            path=path,
            request=body,
            request_sha256=_digest(dict(method=method, path=path, request=body)),
            status="pending",
            response=None,
        )
        if precondition is not None:
            value["check_update_precondition"] = dict(precondition)
        return value

    shadow = copy.copy(journal)
    shadow.effects = [effect(branch[0])]
    first, first_stop, confirmed_start = shadow._preflight_effect(capacity_only=True)
    shadow.effects = copy.deepcopy(confirmed_start) + [effect(branch[1])]
    _, complete_stop, confirmed_complete = shadow._preflight_effect(capacity_only=True)
    shadow.effects = copy.deepcopy(confirmed_start) + [
        effect(branch[1]),
        effect(branch[2]),
    ]
    shadow._guarded_publication_pair = describe_pair(shadow.effects)
    batch = _pack_journal(shadow._document())
    shadow.effects = copy.deepcopy(confirmed_complete) + [effect(branch[2])]
    _, review_stop, confirmed_review = shadow._preflight_effect(capacity_only=True)
    # Every stop keeps all precommitted liabilities. Include incomplete batch
    # layouts, not just the two successful endpoints.
    stops = [first_stop, complete_stop, review_stop, batch]
    for complete_status in ("pending", "rejected", "confirmed"):
        for review_status in ("pending", "rejected", "confirmed"):
            if complete_status != "confirmed" and review_status != "pending":
                continue
            candidate = copy.copy(shadow)
            candidate.effects = copy.deepcopy(confirmed_review)
            candidate.effects[1]["status"] = complete_status
            if complete_status != "confirmed":
                candidate.effects[1]["response"] = (
                    None if complete_status == "pending" else [403, None]
                )
            candidate.effects[2]["status"] = review_status
            if review_status != "confirmed":
                candidate.effects[2]["response"] = (
                    None if review_status == "pending" else [403, None]
                )
            else:
                _, maximum, _ = candidate._preflight_effect(capacity_only=True)
                stops.append(maximum)
            document = candidate._document()
            document["unknown_trust_effect"] = True
            stops.append(_pack_journal(document))
    final = max(stops, key=lambda item: len(canonical_bytes(item)))
    return first, batch, final


def admit(
    publisher,
    journal,
    *,
    result,
    assembled,
    eligibility,
    marker,
    outcome,
    policy,
    complete_inventory,
    checks_enabled,
    grouped,
    planned_window,
    planned_drift,
    continuation,
    retained_eligibility,
    superseded_threads,
    convergence_policy,
):
    from review_sensei.finding_lifecycle_store import preflight_finding_lifecycle

    from .ordinary_publication_plan import current_request_branches
    from .ordinary_publication_reads import issued_witness

    scope = _ACTIVE.get()
    if journal is None or journal._record.quarantine_lineage is None:
        return False
    from .quarantine_capacity import installed_capacity

    try:
        installed_capacity(journal.ledger, journal.identity)
    except ReviewInputError:
        return False
    authenticated, guard, reader, key, expected = _source(journal)
    if (
        scope is None
        or scope[0] is not publisher
        or not scope[1]
        or journal.effects
        or issued_witness(journal) is not None
        or not checks_enabled
        or grouped
        or not complete_inventory
        or result.comments
        or assembled.inline_comments
        or assembled.index_entries
        or superseded_threads
        or continuation is not None
        or retained_eligibility is not None
        or eligibility.historical_assessments is not None
        or not eligibility.quarantine_assessments
        or not eligibility.quarantine_assessments.get("pending")
        or not eligibility.facts.has_human_adjudication_findings
        or outcome.conclusion == "success"
        or journal._record.transaction is None
        or result.content_digest() != journal._record.transaction.result_sha256
    ):
        raise ReviewInputError(
            "guarded pending publication complete source is unsupported"
        )
    _empty_state(journal)
    for state in (planned_window, planned_drift):
        if state is not None:
            inventory = getattr(state, "inventory", None) or getattr(
                state, "authoritative_decision_inventory", None
            )
            if inventory is None or inventory.obligations:
                raise ReviewInputError(
                    "guarded publication projected findings are unpriced"
                )
    assembled = publisher._adjust_assembled_for_drift(
        publisher._adjust_assembled_for_window(
            assembled,
            planned_window,
            convergence_policy=convergence_policy,
            auto_approval=eligibility.facts.enabled,
        ),
        planned_drift,
        convergence_policy=convergence_policy,
        auto_approval=eligibility.facts.enabled,
    )
    branches = current_request_branches(
        publisher,
        journal,
        result=result,
        assembled=assembled,
        eligibility=eligibility,
        marker=marker,
        outcome=outcome,
        policy=policy,
        complete_inventory=True,
        check_published=True,
    )
    successful = tuple(
        branch
        for branch in branches
        if len(branch) == 3
        and branch[-1][0] == "POST"
        and branch[-1][1].endswith("/reviews")
    )
    if len(successful) != 2 or successful[0][2] != successful[1][2]:
        raise ReviewInputError("guarded publication successful request shape changed")
    from .quarantine_census import lifecycle_checkpoint_pricing

    prices = []

    def quote(owner, selected, *, calls, validate, **unused):
        if (
            owner is not journal.ledger
            or selected is not journal._record
            or not validate()
        ):
            raise ReviewInputError("guarded publication source quote changed")
        prices.extend(calls)
        return calls[0]

    checkpoint_price = lifecycle_checkpoint_pricing(
        journal.ledger,
        journal._record,
        added_parts=0,
        added_bytes=0,
        checkpoint_parts=(0,),
        factory=quote,
        inline=True,
    )
    if checkpoint_price is None or checkpoint_price < 6:
        raise ReviewInputError("guarded publication checkpoint source is unqualified")
    # Source checkpoint price is complete census + unchanged six-call write
    # envelope. Each of the two independent current readers adds one PR GET.
    current_read = checkpoint_price - 6 + 1
    read_calls = 2 * current_read + 2  # start and completion check lookups
    alternatives = tuple(_three_checkpoints(journal, branch) for branch in successful)
    documents = tuple(
        max(
            (branch[i] for branch in alternatives),
            key=lambda item: len(canonical_bytes(item)),
        )
        for i in range(3)
    )
    plan = preflight_finding_lifecycle(
        journal.ledger,
        journal._record,
        document=documents[0],
        future_documents=documents[1:],
        operation_id=journal.operation_id,
        base_sha=journal.base_sha,
        head_sha=journal.head_sha,
        policy_digest=journal.policy_digest,
        configuration_digest=journal.configuration_digest,
        item_count=4,
        # Success includes both mandatory fresh carried reads. Stopped native
        # branches forgo the finalizer read and need at most three exact receipt
        # GETs. The separately authenticated terminal tranche owns the final
        # transaction CAS; it is not borrowed for any ordinary checkpoint.
        reserved_tail_calls=max(read_calls + 3, current_read + 2 + 3 + 3),
    )
    if plan["added_parts"] or any(not item["inline"] for item in plan["checkpoints"]):
        raise ReviewInputError(
            "guarded publication batch requires separately priced parts"
        )
    final_price = plan["checkpoints"][-1]["conditional_calls"]
    context = _BatchContext(
        canonical_bytes(
            dict(
                start_requests=[list(branch[0][:3]) for branch in successful],
                complete_requests=[list(branch[1][:3]) for branch in successful],
                review_request=list(successful[0][2][:3]),
                checkpoint_tail_calls=final_price
                + max(2 * current_read + 2, current_read + 2 + 3),
                future_wire_bound=max(len(canonical_bytes(item)) for item in documents),
            )
        )
    )
    from review_sensei.publication_intent_fusion import approve_plan

    approve_plan(journal)
    _ISSUED[journal] = (
        context,
        context.payload,
        scope,
        _fixed_record(journal._record),
        authenticated.lineage_bytes,
        journal.ledger.caller.accounting.owner,
        (guard, reader, key, expected),
        journal.ledger.evidence_budget,
        journal.ledger.http,
        tuple(
            hashlib.sha256((value or "").encode()).digest()
            for value in (journal._review_token, journal._check_token)
        ),
        (
            journal.operation_id,
            journal.head_sha,
            journal.base_sha,
            journal.policy_digest,
            journal.configuration_digest,
        ),
    )
    return True


from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()

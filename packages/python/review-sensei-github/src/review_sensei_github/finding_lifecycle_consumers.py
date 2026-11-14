"""Selected-root lifecycle context for non-review publication consumers.

Reply creation retains its separate idempotency protocol. Only explicitly passed
finalizers are journalled here; selecting this context grants no new authority.
"""

from __future__ import annotations

from contextlib import ContextDecorator

from review_sensei.errors import ReviewInputError

from .finding_lifecycle import FindingLifecycleJournal, restore_finding_lifecycle

_SHARED = (
    "_active_window_states",
    "_drift_states",
    "_trust_snapshots",
    "_trust_decisions",
    "_unknown_effect_holds",
    "_response_loss_holds",
    "_artifact_decisions",
    "_native_finding_receipt_records",
)


def project_confirmed_slot_release(state, *, key, receipts, instances, head_sha):
    """Pure same-source projection; callers separately establish confirmation.

    Capacity consumers may size the result but cannot install it as authority.
    """
    import hashlib
    from dataclasses import replace

    from review_sensei.active_finding_window import (
        evaluate_window_approval_eligibility,
    )
    from review_sensei.bounded_evidence import canonical_bytes
    from review_sensei.finding_drift import (
        DriftEvaluationResult,
        _recompute_drift_verdict,
    )

    if state is None or state.head_sha != head_sha:
        return state
    inventory_attr = (
        "inventory"
        if isinstance(state, DriftEvaluationResult)
        else "authoritative_decision_inventory"
    )
    targets = {
        receipt["thread_id"]: receipt
        for instance in instances
        if (receipt := receipts.get(instance)) is not None
        and receipt["head_sha"] == head_sha
        and receipt["repository_id"] == key[0]
        and receipt["pull_request"] == key[1]
        and receipt["instance"] == instance
        and receipt["thread_id"] is not None
    }
    inventory = getattr(state, inventory_attr)
    released = {}
    obligations = []
    for item in inventory.obligations:
        drift = inventory_attr == "inventory"
        binding = item.thread_binding if drift else None
        target = (
            (binding.thread_id if binding else None) if drift else item.native_target
        )
        status = (
            (binding.slot_state if binding else None) if drift else item.target_state
        )
        if (
            item.lifecycle_state == "verified-fixed"
            and item.resolution_proof_sha256 is not None
            and status == "contingent"
            and target in targets
        ):
            proof = hashlib.sha256(
                canonical_bytes(
                    {
                        "kind": "confirmed-finding-slot-release-v1",
                        "resolution": item.resolution_proof_sha256,
                        "receipt": targets[target],
                        "head_sha": head_sha,
                    }
                )
            ).hexdigest()
            released[target] = proof
            if drift:
                assert binding is not None
                item = replace(
                    item,
                    thread_binding=replace(
                        binding,
                        slot_state="released",
                        proof_sha256=proof,
                        host_marked_resolved=True,
                    ),
                )
            else:
                item = replace(item, target_state="released", target_proof_sha256=proof)
        obligations.append(item)
    if not released:
        return state
    inventory = replace(inventory, obligations=tuple(obligations))
    old_slots = (
        state.slots
        if inventory_attr == "inventory"
        else state.display_active_findings.slots
    )
    slots = tuple(
        replace(slot, state="released", proof_sha256=released[slot.target])
        if slot.target in released
        else slot
        for slot in old_slots
    )
    if inventory_attr == "inventory":
        approved, reasons, steps = _recompute_drift_verdict(
            coverage_state=state.changed_file_coverage,
            review_complete=state.review_complete,
            inventory=inventory,
            active_entries=state.active_entries,
            slots=slots,
        )
        return replace(
            state,
            inventory=inventory,
            slots=slots,
            approved=approved,
            withheld_reasons=reasons,
            recovery_steps=steps,
        )
    else:
        window = replace(state.display_active_findings, slots=slots)
        eligibility = evaluate_window_approval_eligibility(
            changed_file_coverage=state.changed_file_coverage,
            authoritative_decision_inventory=inventory,
            display_active_findings=window,
            review_complete=state.review_complete,
        )
        return replace(
            state,
            authoritative_decision_inventory=inventory,
            display_active_findings=window,
            approval_eligibility=eligibility,
        )


class _Consumer(ContextDecorator):
    def __init__(
        self,
        reviewer,
        ledger,
        identity,
        *,
        head_sha,
        app_slug,
        review_token=None,
        check_token=None,
        finalizers=(),
    ):
        self.reviewer, self.ledger, self.identity = reviewer, ledger, identity
        self.head_sha, self.app_slug = head_sha, app_slug
        self.review_token, self.check_token = review_token, check_token
        self.finalizers = tuple(finalizers)
        self.journal = None
        self.record = None
        self.enrolled = False
        self._previous = []
        self._previous_journals = []
        self._entered = False
        self._bound_snapshot = False

    def authenticated_producer_id(self):
        """Reuse only the producer authenticated by this selected-root read."""
        if not self.enrolled or self.record is None:
            return None
        producer = self.record.finding_lifecycle["manifest"]["binding"]["producer"]
        if producer.startswith("github-bot:"):
            value = producer.removeprefix("github-bot:")
            return int(value) if value.isdecimal() and int(value) > 0 else None
        key = (self.identity.repository_id, self.identity.pull_request)
        receipts = self.reviewer._native_finding_receipt_records.get(key, {})
        producers = {receipt["producer_id"] for receipt in receipts.values()}
        if len(producers) == 1:
            value = next(iter(producers))
            if type(value) is int and value > 0:
                return value
        return None

    def release_confirmed_slots(self, *, head_sha, instances):
        """Retire capacity only after exact resolution of a proved criterion."""
        if not self.enrolled or not self._entered or head_sha != self.head_sha:
            return
        key = (self.identity.repository_id, self.identity.pull_request)
        receipts = self.reviewer._native_finding_receipt_records.get(key, {})
        for attr in ("_active_window_states", "_drift_states"):
            states = getattr(self.reviewer, attr)
            state = states.get(key)
            if state is not None:
                states[key] = project_confirmed_slot_release(
                    state,
                    key=key,
                    receipts=receipts,
                    instances=instances,
                    head_sha=head_sha,
                )

    def run(self, callback, *args, **kwargs):
        with self:
            return callback(*args, **kwargs)

    def bind(self, finalizer):
        """Bind an invocation finalizer without opening a write context."""
        if not self._bound_snapshot:
            self._prepare(read_only=True)
            self._bound_snapshot = True
        for name in _SHARED:
            if hasattr(self.reviewer, name):
                setattr(finalizer, name, getattr(self.reviewer, name))
        finalizer._finding_lifecycle_consumer = self
        if not any(owner is finalizer for owner in self.finalizers):
            self.finalizers += (finalizer,)
        return finalizer

    def restore_only(self):
        """Authenticate current root before inference; do not checkpoint or wrap."""
        return self._prepare(read_only=True)

    def _prepare(self, *, read_only=False):
        if self._entered:
            raise ReviewInputError(
                "finding lifecycle consumer context is already active"
            )
        if self.ledger is None:
            raise ReviewInputError(
                "finding lifecycle consumer requires selected session authority"
            )
        self.record, document = restore_finding_lifecycle(
            self.reviewer, self.ledger, self.identity
        )
        if document is None:
            record = self.record
            key = (self.identity.repository_id, self.identity.pull_request)
            prior = record is not None and (
                record.completed_initial_reviews > 0
                or record.completed_verification_rounds > 0
                or record.convergence_history is not None
                or record.transaction is not None
                or record.publication_staging is not None
            )
            if (
                prior
                or key in self.reviewer._drift_states
                or key in self.reviewer._active_window_states
            ):
                raise ReviewInputError("durable_finding_inventory_recovery_required")
            if not read_only:
                raise ReviewInputError("durable_finding_inventory_recovery_required")
            self.journal, self.enrolled = None, False
            return self
        reference = self.record.finding_lifecycle
        binding = reference["manifest"]["binding"]
        if not read_only and binding["head_sha"] != self.head_sha:
            raise ReviewInputError(
                "finding lifecycle consumer head has not been reviewed"
            )
        if not read_only and not getattr(
            self.ledger, "enable_finding_lifecycle_writes", False
        ):
            raise ReviewInputError(
                "finding lifecycle consumer checkpoint authority unavailable"
            )
        journal = FindingLifecycleJournal(
            self.reviewer,
            self.ledger,
            self.identity,
            operation_id=reference["operation_id"],
            base_sha=binding["base_sha"],
            head_sha=binding["head_sha"],
            policy_digest=binding["policy_digest"],
            configuration_digest=binding["configuration_digest"],
            document=document,
            app_slug=self.app_slug,
            record=self.record,
            review_token=self.review_token,
            check_token=self.check_token,
        )
        budgets = []
        for owner in (*self.finalizers, self.ledger):
            budget = getattr(owner, "evidence_budget", None)
            if budget is None:
                budget = getattr(owner, "_operation_budget", None)
            if budget is not None:
                budgets.append(budget)
        if not read_only and budgets:
            if any(budget is not budgets[0] for budget in budgets[1:]):
                raise ReviewInputError(
                    "finding lifecycle consumer original caller budgets conflict"
                )
            journal._native_budget = budgets[0]
        journal._armed = True
        journal._readback_http = self.reviewer.http
        self.journal, self.enrolled = journal, True
        for finalizer in self.finalizers:
            for name in _SHARED:
                if hasattr(self.reviewer, name):
                    setattr(finalizer, name, getattr(self.reviewer, name))
        return self

    def __enter__(self):
        reply_plan = getattr(self, "_reply_capacity_plan", None)
        if reply_plan is not None and reply_plan.reuses_retained_lifecycle():
            self.journal = reply_plan.reuse_retained_lifecycle(self)
        else:
            self._prepare()
        if self.journal is None:
            self._entered = True
            return self
        journal = self.journal
        reply_plan = getattr(self, "_reply_capacity_plan", None)
        if reply_plan is not None:
            reply_plan.transfer(self, journal)

        class Transport:
            def __init__(self, delegate):
                self.delegate = delegate

            def __getattr__(self, name):
                return getattr(self.delegate, name)

            def request(self, method, path, **kwargs):
                return journal.request(self.delegate, method, path, **kwargs)

        # Deduplicate aliases: wrapping one finalizer twice would journal twice.
        owners = {id(owner): owner for owner in self.finalizers}.values()
        try:
            for owner in owners:
                for name in _SHARED:
                    if hasattr(self.reviewer, name):
                        setattr(owner, name, getattr(self.reviewer, name))
                self._previous.append((owner, owner.http))
                if reply_plan is not None:
                    from .finding_lifecycle import _JournalTransport

                    self._previous_journals.append(
                        (owner, getattr(owner, "_finding_lifecycle_journal", None))
                    )
                    owner._finding_lifecycle_journal = journal
                    owner.http = _JournalTransport(journal, owner.http)
                else:
                    owner.http = Transport(owner.http)
        except BaseException:
            self._restore_transports()
            raise
        self.journal, self.enrolled, self._entered = journal, True, True
        return self

    def _restore_transports(self):
        for owner, http in reversed(self._previous):
            owner.http = http
        for owner, journal in reversed(self._previous_journals):
            owner._finding_lifecycle_journal = journal
        self._previous_journals.clear()
        self._previous.clear()

    def __exit__(self, exc_type, exc, traceback):
        try:
            if self.journal is not None:
                # finish checkpoints changed obligations and uncertainty even
                # when the callback failed. Pending intent forbids settlement.
                self.journal.finish()
                self.record = self.journal._record
        finally:
            self._restore_transports()
            self._entered = False
        return False


def finding_lifecycle_consumer(
    reviewer,
    ledger,
    identity,
    *,
    head_sha,
    app_slug,
    review_token=None,
    check_token=None,
    finalizers=(),
):
    """Return a context/decorator, or call its ``run(callback, ...)`` method.

    A missing/empty selected session allows discussion only. Prior review state
    lacking a complete carrier is not, and cannot use this consumer to finalize.
    Pass actual invocation finalizers, not the outer reply transport.
    """
    return _Consumer(
        reviewer,
        ledger,
        identity,
        head_sha=head_sha,
        app_slug=app_slug,
        review_token=review_token,
        check_token=check_token,
        finalizers=finalizers,
    )

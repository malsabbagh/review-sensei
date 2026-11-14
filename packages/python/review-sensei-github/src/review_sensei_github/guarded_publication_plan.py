"""Negative-only durable-work floor before a guarded publication starts.

Passing this test is NOT whole-route admission. Native discovery, source reads,
finalization and terminal liabilities still require their original guards.
"""

from review_sensei.errors import ReviewInputError


def require_durable_floor(journal, *, checks_enabled):
    """Reject a demonstrably unfunded initial effect sequence before its check.

    Even normal success needs a pending checkpoint before each native effect,
    followed by a final durable receipt checkpoint. The next pending checkpoint
    may coalesce the preceding confirmation; this floor allows that saving.
    Parts, native readback and finalization can only add liabilities.
    No receipt, capability, admission marker or funding is created here.
    """
    if journal is None or not checks_enabled or journal.effects:
        return
    from .checks import ReviewCheckPublisher
    from .finding_lifecycle import FindingLifecycleJournal
    from .publication import ReviewPublisher

    if (
        type(journal) is not FindingLifecycleJournal
        or type(journal.reviewer) is not ReviewPublisher
        or type(journal.reviewer.checks) is not ReviewCheckPublisher
    ):
        return  # Unsupported custom producers retain their existing guards.
    from review_sensei.source_registry import require_instance_source

    try:
        for owner in (journal.reviewer, journal.reviewer.checks, journal):
            # First authenticate the class namespace against its definition,
            # then enumerate that original source rather than a supplied list
            # of just the visible entry methods.
            require_instance_source(owner, methods=())
            methods = tuple(
                name
                for name in dir(type(owner))
                if callable(getattr(type(owner), name))
            )
            if any(name in vars(owner) for name in methods):
                return
            require_instance_source(
                owner,
                methods=tuple(
                    name
                    for name in methods
                    if hasattr(getattr(type(owner), name), "__code__")
                    or hasattr(getattr(type(owner), name), "__func__")
                ),
            )
    except ReviewInputError:
        return  # A custom source cannot inherit the closed success floor.
    record, ledger = journal._record, journal.ledger
    if record.quarantine_lineage is None:
        return
    from .publication_recovery import _publication_transport
    from .quarantine_capacity import installed_capacity
    from .quarantine_census import lifecycle_checkpoint_pricing

    # The authentic plain isolation profile intentionally has no native guard.
    # The checkpoint planner below authenticates that profile independently and
    # rejects a missing guard for every profile that requires one.
    if _publication_transport(ledger.http)._quarantine_capacity_guard is not None:
        installed_capacity(ledger, journal.identity)

    def quote(owner, selected, *, calls, validate, **_unused):
        if owner is not ledger or selected is not record or not validate():
            raise ReviewInputError("guarded publication checkpoint source changed")
        if len(calls) != 1 or type(calls[0]) is not int or calls[0] < 1:
            raise ReviewInputError("guarded publication checkpoint quote is invalid")
        return calls[0]

    minimum_checkpoint = lifecycle_checkpoint_pricing(
        ledger,
        record,
        added_parts=0,
        added_bytes=0,
        checkpoint_parts=(0,),
        factory=quote,
        inline=True,
    )
    if minimum_checkpoint is None:
        return  # No source quote: never manufacture a cheaper admission.
    # Direct, non-App-authored publication with an available check credential:
    # start writes in_progress; complete writes completed; the direct review
    # branch POSTs its main review. Permission/failure diagnostics may stop this
    # successful sequence, but cannot turn an underfunded success into admission.
    # The caller excludes grouped writers. Existing effect replay is excluded
    # above because it may need fewer sends/checkpoints. This floor omits
    # approval, native reads and terminal work; it cannot authorize their
    # execution. Four checkpoints cover the three pending intents and the last
    # receipt, allowing all intermediate confirmations to coalesce.
    minimum_calls = 4 * minimum_checkpoint + 3
    ledger.caller.budget_view.preflight(minimum_calls)


def require_grouped_durable_floor(publisher, journal, host, *, checks_enabled):
    """Negative-only floor for the original fresh grouped producer.

    Each group and the final union review pass through the original lifecycle
    journal. Their independently durable staging/manifest protocol adds work;
    it does not replace journal intent or the last receipt checkpoint.
    """
    import inspect

    from review_sensei.source_registry import (
        require_instance_source,
        require_module_seal,
    )

    from . import native_group_publication, native_review_groups, publication
    from .finding_lifecycle import FindingLifecycleJournal
    from .quarantine_capacity import installed_capacity
    from .quarantine_census import lifecycle_checkpoint_pricing

    if journal is None or journal.effects:
        return
    if journal._record.quarantine_lineage is None:
        return
    require_module_seal(publication)
    require_module_seal(native_group_publication)
    require_module_seal(native_review_groups)
    if (
        type(journal) is not FindingLifecycleJournal
        or type(publisher) is not publication.ReviewPublisher
        or journal.reviewer is not publisher
        or type(host) is not native_group_publication.NativeGroupPublication
        or type(host.plan) is not native_review_groups.NativeReviewPlan
    ):
        raise ReviewInputError("grouped durable floor requires original source")
    require_instance_source(publisher, methods=("_publish_impl",))
    require_instance_source(journal, methods=("request", "checkpoint", "finish"))
    require_instance_source(
        host,
        methods=(
            "_publish_parts",
            "publish_final",
            "checkpoint",
            "require_claim",
            "reconcile_part",
        ),
    )
    require_instance_source(host.plan, methods=("part_payload",))
    original = publication.ReviewPublisher._publish_impl
    while hasattr(original, "__wrapped__"):
        original = original.__wrapped__
    frame = inspect.currentframe()
    frame = frame.f_back if frame is not None else None
    if (
        frame is None
        or frame.f_code is not original.__code__
        or frame.f_globals is not vars(publication)
        or not {"self", "lifecycle_journal", "native_host"} <= frame.f_locals.keys()
        or frame.f_locals["self"] is not publisher
        or frame.f_locals["lifecycle_journal"] is not journal
        or frame.f_locals["native_host"] is not host
        or frame.f_locals.get("grouped_publication") is not True
        or host.http is not publisher.http
        or host.ledger is not journal.ledger
        or host.identity != journal.identity
    ):
        raise ReviewInputError("grouped durable floor producer frame changed")
    record = journal._record
    # Retained staging may replay fewer effects. This bound covers only fresh
    # source publication and must not turn a recovered prefix into a new plan.
    if record.publication_staging is not None:
        return
    if (
        record.transaction is None
        or host.plan.transaction_id != record.transaction.transaction_id
    ):
        raise ReviewInputError("grouped durable floor transaction changed")
    installed_capacity(journal.ledger, journal.identity)

    def quote(owner, selected, *, calls, validate, **_unused):
        if owner is not journal.ledger or selected is not record or not validate():
            raise ReviewInputError("grouped durable floor checkpoint source changed")
        if len(calls) != 1 or type(calls[0]) is not int or calls[0] < 1:
            raise ReviewInputError("grouped durable floor checkpoint quote changed")
        return calls[0]

    checkpoint = lifecycle_checkpoint_pricing(
        journal.ledger,
        record,
        added_parts=0,
        added_bytes=0,
        checkpoint_parts=(0,),
        factory=quote,
        inline=True,
    )
    if checkpoint is None:
        return  # No source quote can manufacture admission.
    groups = len(host.plan.groups)
    # Checks may be unavailable while review publication still proceeds.
    # Count only their actual branch, never skip the review-only floor.
    # One POST per group and one final union POST are mandatory. Every
    # pending intent needs a checkpoint; finish needs one final checkpoint.
    # The original fresh producer also reads back each own part, manifest and
    # final claim (groups + 2). Each confirmed part needs an opening census,
    # complete group read and closing census (3 * groups), and the final union
    # has its own opening census (1). These are disjoint from the journal's
    # checkpoint quote and cannot be removed by reservation/transaction fusion.
    # This still omits staging CAS, manifest writes/confirmation, the rest of
    # the final union walk and trust fences. Passing it is never admission.
    effects = groups + 1 + (2 if checks_enabled else 0)
    minimum_native_reads = 4 * groups + 3
    journal.ledger.caller.budget_view.preflight(
        (effects + 1) * checkpoint + effects + minimum_native_reads
    )

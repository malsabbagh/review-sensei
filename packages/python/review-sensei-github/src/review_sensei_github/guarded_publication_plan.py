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

"""Read-only source preparation for one ordinary disposition, not a permit.

The execution route remains held until the native finalization/receipt branches
and own-CAS transfer have complete source pricing and custody qualification.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
from weakref import WeakKeyDictionary

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.terminal_representability import (
    action_intent_sha256,
    build_action_result_binding,
    preview_session_command_representability,
)


@dataclass(frozen=True, eq=False)
class DispositionPreparation:
    """Opaque current-source observation, explicitly not action admission."""

    root: str
    intent_sha256: str


def _build_preparation():
    issued: WeakKeyDictionary[DispositionPreparation, Any] = WeakKeyDictionary()

    def collect(run, args):
        """Collect actual sources and a detached preview without admitting intent."""
        from .action_representability import _command_pricing
        from .action_run import application, current_inventory, parse_maintainer_command
        from .commands import lookup_collaborator_permission
        from .finding_lifecycle import restore_finding_lifecycle
        from .review_run import ActionRunLedger, ReviewRun
        from .run_authority import VerifiedRunAction
        from .trust_effects import verification_profile
        from .trust_enrollment import compare_current_observation
        from .trust_status_source import capture_status_source

        require_source = capture_status_source()
        require_source()
        caller = run.caller
        profile = verification_profile(caller)
        if profile is None:
            raise ReviewInputError("trust disposition source profile is unavailable")
        original = json.loads(profile.original)["authority"]
        if (
            type(run) is not ReviewRun
            or type(run.ledger) is not ActionRunLedger
            or profile is None
            or caller.accounting.action is not None
            or caller._action_unresolved()
            or run.ledger._quarantine_required
            or caller._terminal_reason is not None
            or run.session_grant != profile.session.grant
            or run.session_authority != original
            or hashlib.sha256(args.comment_body.encode()).hexdigest()
            != original["source"]["body_sha256"]
            or args.source_comment_id != original["source"]["id"]
        ):
            raise ReviewInputError("trust disposition pre-entry source is unsupported")
        authority = VerifiedRunAction(run)
        authority.fence()
        command = parse_maintainer_command(
            args.comment_body,
            actor=authority.document["actor"]["login"],
            head_sha=run.head_sha,
            command_id=str(authority.document["source"]["id"]),
        )
        if command is None or command.action != "accept-risk":
            raise ReviewInputError(
                "trust disposition preview requires exact accept-risk"
            )
        app = application(run)
        record, document = restore_finding_lifecycle(
            app.reviewer, run.ledger, run.identity
        )
        if (
            record is None
            or document is None
            or record.record_sha256 != caller._root
            or record.reservation_id is not None
            or record.disposition_snapshot is not None
            or record.quarantine_lineage is not None
            or record.historical_assessments
            or any(effect["status"] != "confirmed" for effect in document["effects"])
        ):
            raise ReviewInputError("trust disposition retained source is unsupported")
        compare_current_observation(profile.broker, profile.session, record)
        inventory = current_inventory(run)
        if len(inventory) != 1:
            raise ReviewInputError(
                "trust disposition preview requires one current criterion"
            )
        role = lookup_collaborator_permission(
            run.http,
            token=run.ledger.token,
            repository=run.identity.repository,
            actor=authority.document["actor"]["login"],
        )
        if role not in {"maintain", "admin"}:
            raise ReviewInputError(
                "trust disposition current elevated permission is missing"
            )
        from review_sensei.convergence import resolve_review_convergence_policy

        mode = getattr(args, "review_mode", None)
        if (
            mode is None
            and not os.environ.get("REVIEWSENSEI_REVIEW_MODE", "").strip()
            and run.plan.policy.reviews == "advisory"
        ):
            mode = "advisory"
        policy = resolve_review_convergence_policy(
            mode=mode,
            quality=run.plan.policy.review_quality,
        )
        source = authority.document["source"]
        key = hashlib.sha256(
            canonical_bytes(
                {
                    "purpose": "command",
                    "repository_id": run.identity.repository_id,
                    "pull_request": run.identity.pull_request,
                    "head_sha": run.head_sha,
                    "source": source,
                    "configuration": caller.accounting.configuration.digest,
                }
            )
        ).hexdigest()
        intent = caller.action_intent_document(
            purpose="command",
            source=source,
            head_sha=run.head_sha,
            operation_key=key,
        )
        digest = action_intent_sha256(intent)
        binding = build_action_result_binding(
            identity=run.identity,
            policy_commit=caller.accounting.configuration.commit,
            head_sha=run.head_sha,
            intent_sha256=digest,
            configuration_digest=caller.accounting.configuration.digest,
            owner_generation=caller.accounting.owner.generation,
            producer=run.ledger.evidence_producer(),
        )
        details: dict[str, Any] = {}
        result, preview, report = preview_session_command_representability(
            identity=run.identity,
            current_record=record,
            command=command,
            intent_sha256=digest,
            binding=binding,
            finding_inventory=inventory,
            actor_role=role,
            policy_digest=policy.digest(),
            provenance=f"comment:{source['id']}",
            enable_disposition_snapshot_writes=True,
            supports_paged_disposition_snapshot_writes=False,
            evidence_budget=caller.budget_view,
            source_ledger=run.ledger,
            accounting_version=str(caller.accounting.version),
            pricing=_command_pricing(
                caller,
                action=command.action,
                writes_enabled=True,
                ledger=run.ledger,
                current_record=record,
            ),
            now=datetime.now(timezone.utc),
            capacity_details=details,
        )
        require_source()
        if caller.accounting.action is not None or caller._root != record.record_sha256:
            raise ReviewInputError(
                "trust disposition preview changed current action root"
            )
        # Detached sizing data only. No private dispatch registry is populated and
        # no caller state, intent, checkpoint or native operation is installed.
        return dict(
            run=run,
            args=args,
            app=app,
            record=record,
            document=document,
            command=command,
            policy=policy,
            result=result,
            preview=preview,
            report=report,
            details=details,
            profile=profile,
            require_source=require_source,
            intent=intent,
            budget=caller.budget_view,
        )

    def fingerprint(context):
        run, args = context["run"], context["args"]
        return canonical_bytes(
            {
                "scope": [
                    run.identity.repository,
                    run.identity.repository_id,
                    run.identity.pull_request,
                    run.head_sha,
                    args.app_slug,
                    args.source_comment_id,
                    hashlib.sha256(args.comment_body.encode()).hexdigest(),
                    hashlib.sha256(run.oidc.encode()).hexdigest(),
                ],
                "record": context["record"].to_dict(),
                "document": context["document"],
                "preview": context["preview"].to_dict(),
                "result": asdict(context["result"]),
                "intent": context["intent"],
                "details": {
                    key: (
                        [part.hex() for part in value]
                        if key == "staged_parts"
                        else value.hex()
                        if isinstance(value, bytes)
                        else value
                    )
                    for key, value in context["details"].items()
                },
            }
        )

    def prepare_source_preview(run, args):
        context = collect(run, args)
        return tuple(context[key] for key in ("result", "preview", "report", "details"))

    def prepare_source_disposition(run, args):
        context = collect(run, args)
        if not context["report"].representable or not context["result"].applied:
            raise ReviewInputError(
                "trust disposition source preview is not representable"
            )
        receipt = DispositionPreparation(
            context["record"].record_sha256, action_intent_sha256(context["intent"])
        )
        issued[receipt] = (
            context,
            fingerprint(context),
            receipt.root,
            receipt.intent_sha256,
        )
        return receipt

    def observe_disposition_capacity(receipt):
        """Run real bounded source readers, never admitted-action branch pricing.

        The returned native plan has not bound a projected action or checkpoint
        branches. It cannot discharge or authorize the predicted disposition.
        """
        from copy import copy

        from review_sensei.disposition import decode_session_dispositions

        from .finding_lifecycle import FindingLifecycleJournal
        from .publication import ReviewApprovalFinalizer
        from .trust_effects import verification_profile

        entry = issued.get(receipt) if type(receipt) is DispositionPreparation else None
        if entry is None:
            raise ReviewInputError("trust disposition preparation is unissued")
        context, frozen, root, intent_digest = entry
        run, app = context["run"], context["app"]
        caller, record = run.caller, context["record"]

        def require():
            context["require_source"]()
            if (
                verification_profile(caller) is not context["profile"]
                or caller._root != root
                or caller.budget_view is not context["budget"]
                or caller._effect_record is None
                or canonical_bytes(caller._effect_record.to_dict())
                != canonical_bytes(record.to_dict())
                or caller.accounting.action is not None
                or (receipt.root, receipt.intent_sha256) != (root, intent_digest)
                or fingerprint(context) != frozen
            ):
                raise ReviewInputError("trust disposition preparation custody changed")

        require()
        # One actual observation attempt per receipt, including failed reads.
        # A retry must collect current sources again; no cached observation is
        # reinterpreted as freshness or free work.
        del issued[receipt]
        finalizer = app.reviewer.finalizer
        budget = caller.budget_view
        if (
            type(finalizer) is not ReviewApprovalFinalizer
            or (finalizer.evidence_budget or finalizer._operation_budget)
            not in (None, budget)
            or app.broker.control is None
            or app.broker.control.budget is not budget
        ):
            raise ReviewInputError("trust disposition capacity source is unavailable")
        finalizer = copy(finalizer)
        finalizer.evidence_budget = budget
        reference = record.finding_lifecycle
        binding = reference["manifest"]["binding"]
        journal = FindingLifecycleJournal(
            app.reviewer,
            run.ledger,
            run.identity,
            operation_id=reference["operation_id"],
            base_sha=binding["base_sha"],
            head_sha=binding["head_sha"],
            policy_digest=binding["policy_digest"],
            configuration_digest=binding["configuration_digest"],
            app_slug=context["args"].app_slug,
            record=record,
            document=context["document"],
            review_token=run.ledger.token,
            native_budget=budget,
        )
        budget.check()
        check_token = app.broker.exchange(run.oidc, capability="check_publish")
        budget.check()
        require()
        with journal.transports(finalizer=finalizer):
            plan = finalizer.prepare_command_capacity(
                token=run.ledger.token,
                check_token=check_token,
                repository=run.identity.repository,
                repository_id=run.identity.repository_id,
                pull_request=run.identity.pull_request,
                head_sha=run.head_sha,
                app_slug=context["args"].app_slug,
                actor=context["command"].actor,
                finding_ids=tuple(
                    item.reference
                    for item in context["result"].receipts
                    if item.outcome in {"recorded", "already-recorded"}
                    and item.reference.startswith("RS-")
                ),
                dispositions=decode_session_dispositions(
                    json.loads(context["details"]["dispositions"])
                ),
                incomplete_coverage=(),
                policy_digest=context["policy"].digest(),
                command_finalization_reserve=7,
            )
        require()
        plan.require(journal, http=plan.http, budget=budget)
        return plan

    return (
        prepare_source_preview,
        prepare_source_disposition,
        observe_disposition_capacity,
    )


(prepare_source_preview, prepare_source_disposition, observe_disposition_capacity) = (
    _build_preparation()
)
del _build_preparation


from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()

"""Read-only source preparation for one ordinary disposition, not a permit.

The execution route remains held until the native finalization/receipt branches
and own-CAS transfer have complete source pricing and custody qualification.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.terminal_representability import (
    action_intent_sha256,
    build_action_result_binding,
    preview_session_command_representability,
)


def prepare_source_preview(run, args):
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
        raise ReviewInputError("trust disposition preview requires exact accept-risk")
    app = application(run)
    record, document = restore_finding_lifecycle(app.reviewer, run.ledger, run.identity)
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
        raise ReviewInputError("trust disposition preview changed current action root")
    # Detached sizing data only. No private dispatch registry is populated and
    # no caller state, intent, checkpoint or native operation is installed.
    return result, preview, report, details


from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()

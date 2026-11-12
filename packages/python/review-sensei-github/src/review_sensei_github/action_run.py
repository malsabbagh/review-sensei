"""One authenticated process for an Actions command or mention reply.

Policy checkout, dependency installation and artifact upload are infrastructure.
Every business request made here uses the selected caller-run transport.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from review_sensei.api_work import ApiRunAccounting
from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.convergence import resolve_review_convergence_policy
from review_sensei.disposition import (
    FindingIdentity,
    command_exit_code,
    parse_maintainer_command,
)
from review_sensei.errors import ReviewInputError
from review_sensei.writer_containment import (
    ContainedForwardingCoordinator,
    IssuedWriterToken,
)

from .action_representability import require_action_settlement_representable
from .api_run_owner import ApiRunActionsEvidence
from .api_run_requests import ApiRunRequestTransport
from .application import GitHubApplication, GitHubWriteOptions
from .comment_intent import verify_live_comment_source
from .conversation import ConversationPublisher
from .errors import GitHubMergeGroupUnsupportedError
from .event_routing_contract import (
    MAX_NO_PROGRESS_RECONCILIATION_ATTEMPTS,
    RECONCILIATION_OWNERS,
    AcceptedOperationRecord,
    ConvergenceDecision,
    LivePullRequestTarget,
    reconcile_event_convergence,
)
from .learning_pr import LearningPRPublisher
from .publication import ReviewPublisher, parse_finding_index
from .review_run import ActionRunLedger, ReviewRun, RolloverService, bootstrap
from .run_authority import VerifiedRunAction
from .source_comment_lifecycle import (
    SourceLifecycleClassification,
    admit_event_comment_source,
    execute_source_lifecycle_route,
)
from .unstarted_action_recovery import NoBusinessDispatchEvidence


@dataclass(frozen=True)
class UnstartedCallerRecoveryGate:
    """Explicit caller authority gate for G32 unstarted-action recovery.

    Requires BOTH positive ``NoBusinessDispatchEvidence`` AND qualified owner
    cessation (``use_actions_evidence=True``) or canonical writer epoch fencing.
    """

    target_run_id: int
    no_business_dispatch_evidence: NoBusinessDispatchEvidence | None
    mode: str = "same_run"
    use_actions_evidence: bool = False
    canonical_coordinator: ContainedForwardingCoordinator | None = None
    canonical_token: IssuedWriterToken | None = None

    def validate_preconditions(self) -> NoBusinessDispatchEvidence:
        if type(self.target_run_id) is not int or self.target_run_id <= 0:
            raise ReviewInputError("unstarted recovery target_run_id must be positive")
        if self.mode not in {"same_run", "cross_run"}:
            raise ReviewInputError("unstarted recovery mode is unsupported")
        if not isinstance(
            self.no_business_dispatch_evidence, NoBusinessDispatchEvidence
        ):
            raise ReviewInputError(
                "ordinary unstarted action recovery requires explicit NoBusinessDispatchEvidence; "
                "comment deletion, execution_started=false, null markers, and cancelled jobs alone "
                "are insufficient authority"
            )
        if self.no_business_dispatch_evidence.target_run_id != self.target_run_id:
            raise ReviewInputError(
                "unstarted recovery gate target_run_id conflicts with dispatch evidence"
            )
        has_canonical = (
            self.canonical_coordinator is not None or self.canonical_token is not None
        )
        if not self.use_actions_evidence and not has_canonical:
            raise ReviewInputError(
                "unstarted action cancellation requires owner cessation or canonical fence proof"
            )
        return self.no_business_dispatch_evidence


@contextmanager
def _cross_run_unstarted_rollover_guard(
    gate: UnstartedCallerRecoveryGate | None,
) -> Iterator[RolloverService | None]:
    """Yield one explicit bootstrap dependency; never mutate shared transport methods.

    Gate evidence is internal authenticated caller authority, not a CLI document.
    Nested or concurrent ordinary bootstraps receive no recovery dependency.
    """
    if gate is None:
        yield None
        return
    evidence_obj = gate.validate_preconditions()
    if gate.mode != "cross_run":
        yield None
        return

    original_rollover = ApiRunRequestTransport.rollover_and_activate
    applied = False

    def _guarded_rollover(
        self: ApiRunRequestTransport,
        native: Any,
        identity: Any,
        frozen: Any,
        owner: Any,
        previous_root: Any,
        require_cessation: Any,
    ) -> Any:
        nonlocal applied
        if applied:
            return original_rollover(
                self,
                native,
                identity,
                frozen,
                owner,
                previous_root,
                require_cessation,
            )
        if previous_root is None or previous_root.api_run_accounting is None:
            raise ReviewInputError(
                "cross-run unstarted recovery requires a predecessor accounting record"
            )
        prev_state = ApiRunAccounting.from_document(previous_root.api_run_accounting)
        if prev_state.identity.run_id != gate.target_run_id:
            raise ReviewInputError(
                "cross-run unstarted recovery gate target_run_id conflicts with predecessor"
            )
        actions_ev: ApiRunActionsEvidence | None = None
        if gate.use_actions_evidence:
            origin_reader = getattr(native, "_quarantine_origin_reader", None)
            actions_tok = getattr(origin_reader, "actions_token", None)
            if not isinstance(actions_tok, str) or not actions_tok.strip():
                raise ReviewInputError(
                    "cross-run unstarted recovery requires authenticated actions_token"
                )
            actions_ev = ApiRunActionsEvidence(
                native.http,
                token=actions_tok,
                command_run_id=str(self.run.run_id),
                job=self.job,
            )
        require_cessation()
        classification, activated = execute_source_lifecycle_route(
            caller=self,
            ledger=native,
            identity=identity,
            record=previous_root,
            candidate_source=None,
            mode="cross_run",
            actions_evidence=actions_ev,
            canonical_coordinator=gate.canonical_coordinator,
            canonical_token=gate.canonical_token,
            no_business_dispatch_evidence=evidence_obj,
            require_positive_dispatch_proof=True,
            rollover_configuration=frozen,
            rollover_owner=owner,
        )
        if classification.route != "g32_unstarted_cancellation":
            raise ReviewInputError(
                f"cross-run unstarted recovery refused on route {classification.route!r}"
            )
        applied = True
        return activated

    yield _guarded_rollover
    if not applied:
        raise ReviewInputError(
            "cross-run unstarted recovery gate did not settle a stranded predecessor"
        )


def event_source(args: Any, *, purpose: str) -> dict[str, object]:
    """The runner event selects a source; live Worker reads authenticate it."""
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    if event_name.strip() == "merge_group":
        raise GitHubMergeGroupUnsupportedError(
            "GitHub merge_group event is unsupported by action admission"
        )
    path = Path(os.environ.get("GITHUB_EVENT_PATH", ""))
    with path.open("rb") as stream:
        raw = stream.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ReviewInputError("action event exceeds its byte bound")
    value = json.loads(raw)
    return admit_event_comment_source(
        value,
        pull_request=args.pull_request,
        source_comment_id=args.source_comment_id,
        purpose=purpose,
        event_name=event_name,
        expected_body=getattr(args, "comment_body", None),
        expected_updated_at=getattr(args, "source_updated_at", None),
    )


def evaluate_action_convergence_progress(
    run: ReviewRun,
    *,
    purpose: str,
    source: Mapping[str, Any],
    comment_body: str = "",
    records: Sequence[AcceptedOperationRecord] = (),
    delivery_id: str | None = None,
    was_queued_replaced: bool = False,
    current_state_fingerprint: str | None = None,
    max_no_progress_attempts: int = MAX_NO_PROGRESS_RECONCILIATION_ATTEMPTS,
) -> ConvergenceDecision:
    """Evaluate convergence after native bootstrap, never grant caller authority.

    Synthetic routing fields select a policy branch only. Live source/permission
    fences remain in the executor; this helper cannot replace those checks.
    """

    kind = str(source.get("kind", "issue_comment"))
    event_name = (
        "pull_request_review_comment" if kind == "review_comment" else "issue_comment"
    )
    comment_id = source.get("id")
    updated_at = str(source.get("updated_at", "1970-01-01T00:00:00Z"))
    if type(run.identity.repository_id) is not int or run.identity.repository_id <= 0:
        raise ReviewInputError(
            "action convergence requires a positive integer repository_id"
        )
    live_target = LivePullRequestTarget(
        repository_id=run.identity.repository_id,
        pull_request_number=run.identity.pull_request,
        base_sha=run.base_sha,
        base_ref=run.base_branch or "main",
        head_sha=run.head_sha,
        head_ref="HEAD",
    )
    if event_name == "pull_request_review_comment":
        payload: dict[str, Any] = {
            "action": "created",
            "pull_request": live_target.to_pull_mapping(),
            "comment": {
                "id": comment_id,
                "body": comment_body,
                "updated_at": updated_at,
                "pull_request_url": (
                    f"https://api.github.com/repos/{run.identity.repository}"
                    f"/pulls/{run.identity.pull_request}"
                ),
                "author_association": "MEMBER",
                "user": {"login": "authenticated-caller", "type": "User"},
            },
        }
    else:
        payload = {
            "action": "created",
            "issue": {
                "number": run.identity.pull_request,
                "pull_request": {
                    "url": (
                        f"https://api.github.com/repos/{run.identity.repository}"
                        f"/pulls/{run.identity.pull_request}"
                    )
                },
            },
            "comment": {
                "id": comment_id,
                "body": comment_body,
                "updated_at": updated_at,
                "author_association": "MEMBER",
                "user": {"login": "authenticated-caller", "type": "User"},
            },
        }
    decision = reconcile_event_convergence(
        event_name,
        payload,
        live_target=live_target,
        records=records,
        delivery_id=delivery_id,
        was_queued_replaced=was_queued_replaced,
        current_state_fingerprint=current_state_fingerprint,
        max_no_progress_attempts=max_no_progress_attempts,
    )
    if decision.reconciliation_owner not in RECONCILIATION_OWNERS:
        raise ReviewInputError("action reconciliation owner is invalid")
    if decision.stopped_no_progress:
        raise ReviewInputError(
            "action reconciliation halted by finite no-progress stop rule"
        )
    if decision.status == "held_queued_replaced":
        raise ReviewInputError(
            f"queued {purpose} run was replaced before execution; "
            f"recover via {decision.recovery_command}"
        )
    if decision.status == "held_uncertain_operation":
        raise ReviewInputError(
            "action reconciliation held due to uncertain prior dispatch"
        )
    return decision


def reconcile_action_source_lifecycle(
    run: ReviewRun,
    *,
    candidate_source: Mapping[str, Any] | None = None,
    event_delivery: Mapping[str, Any] | None = None,
    has_pending_receipt: bool = False,
    verify_before_intent: bool = False,
    unstarted_recovery: UnstartedCallerRecoveryGate | None = None,
) -> SourceLifecycleClassification | None:
    """Evaluate and enforce G27/G32 source lifecycle before or across action entry."""
    evidence_obj: NoBusinessDispatchEvidence | None = None
    actions_ev: ApiRunActionsEvidence | None = None
    if unstarted_recovery is not None:
        evidence_obj = unstarted_recovery.validate_preconditions()
        if unstarted_recovery.mode != "same_run":
            raise ReviewInputError(
                "in-run source lifecycle reconciliation requires same_run recovery gate"
            )
        if run.caller.run.run_id != unstarted_recovery.target_run_id:
            raise ReviewInputError(
                "same-run unstarted recovery gate target_run_id conflicts with active run"
            )
        if unstarted_recovery.use_actions_evidence:
            if not isinstance(run.actions_token, str) or not run.actions_token.strip():
                raise ReviewInputError(
                    "same-run unstarted recovery requires authenticated actions_token"
                )
            actions_ev = ApiRunActionsEvidence(
                run.http,
                token=run.actions_token,
                command_run_id=str(run.caller.run.run_id),
                job=run.caller.job,
            )
    elif run.caller.accounting.action is None and not verify_before_intent:
        return None
    loaded = run.ledger.load(run.identity)
    record = loaded.record if loaded.status == "ok" else None
    classification, _ = execute_source_lifecycle_route(
        caller=run.caller,
        ledger=run.ledger,
        identity=run.identity,
        record=record,
        candidate_source=candidate_source,
        event_delivery=event_delivery,
        has_pending_receipt=has_pending_receipt,
        mode="same_run",
        actions_evidence=actions_ev,
        canonical_coordinator=(
            None
            if unstarted_recovery is None
            else unstarted_recovery.canonical_coordinator
        ),
        canonical_token=(
            None if unstarted_recovery is None else unstarted_recovery.canonical_token
        ),
        no_business_dispatch_evidence=evidence_obj,
        require_positive_dispatch_proof=unstarted_recovery is not None,
    )
    if (
        unstarted_recovery is not None
        and classification.route != "g32_unstarted_cancellation"
    ):
        raise ReviewInputError(
            "unstarted recovery gate requires an unstarted mutated source action"
        )
    return classification


def application(run: ReviewRun) -> GitHubApplication:
    app = GitHubApplication(
        broker=run.broker,
        http=run.http,
        reviewer=ReviewPublisher(http=run.http),
        replier=ConversationPublisher(http=run.http),
        learner=LearningPRPublisher(http=run.http),
        session_ledger=run.ledger,
        enable_disposition_snapshot_writes=True,
    )
    app.reviewer.finalizer.evidence_budget = run.caller.budget_view
    app.replier.finalizer.evidence_budget = run.caller.budget_view
    # Source/admission reads retain the original metadata credential. Review
    # mutations use their independently selected write capability; a later
    # finalizer need not rebuild this same operation's immutable read context.
    if run.ledger._quarantine_required:
        app.reviewer.finalizer.human_inventory_read_token = run.ledger.token
        app.replier.finalizer.human_inventory_read_token = run.ledger.token
    return app


def current_inventory(run: ReviewRun) -> tuple[FindingIdentity, ...]:
    """Read the newest current App review's current index; no old fallback."""
    assert run.identity.repository_id is not None
    reviews = run.http.paginate(
        path=run.http.repository_path(
            run.identity.repository, f"/pulls/{run.identity.pull_request}/reviews"
        ),
        token=run.ledger.token,
    )
    owned = [
        r
        for r in reviews
        if isinstance(r, dict)
        and isinstance(r.get("user"), dict)
        and r["user"].get("type") == "Bot"
        and r["user"].get("id") == run.ledger._evidence_author_id
        and r.get("commit_id") == run.head_sha
        and type(r.get("id")) is int
    ]
    if not owned:
        raise ReviewInputError("current command review inventory is unavailable")
    selected = max(owned, key=lambda r: r["id"])
    index = parse_finding_index(
        selected.get("body"),
        repository_id=run.identity.repository_id,
        pull_request=run.identity.pull_request,
        head_sha=run.head_sha,
    )
    if index is None:
        raise ReviewInputError(
            "current command requires a complete current review index"
        )
    return tuple(
        FindingIdentity(
            instance=i.instance,
            concern=i.lifecycle,
            blocking=i.blocking,
            needs_human=i.needs_human,
            overridable=i.overridable,
        )
        for i in index.entries
    )


def _options(run: ReviewRun) -> GitHubWriteOptions:
    return GitHubWriteOptions(
        github_writes=True,
        github_session_ledger=True,
        reviews_policy=run.plan.policy.reviews,
    )


def execute_command(run: ReviewRun, args: Any) -> int:
    assert run.identity.repository_id is not None
    authority = VerifiedRunAction(run)
    if not isinstance(run.ledger, ActionRunLedger):
        raise ReviewInputError("command requires its current action ledger")
    run.ledger.business_guard = authority.fence
    source = authority.document["source"]
    actor = authority.document["actor"]
    from .historical_assessment_action import parse_historical_command
    from .quarantine_action import parse_quarantine_command

    historical = parse_historical_command(args.comment_body)
    quarantine = parse_quarantine_command(args.comment_body)
    command = (
        historical
        or quarantine
        or parse_maintainer_command(
            args.comment_body,
            actor=actor["login"],
            head_sha=run.head_sha,
            command_id=str(source["id"]),
        )
    )
    if command is None:
        raise ReviewInputError("authenticated source is not a supported command")
    key = hashlib.sha256(
        canonical_bytes(
            {
                "purpose": "command",
                "repository_id": run.identity.repository_id,
                "pull_request": run.identity.pull_request,
                "head_sha": run.head_sha,
                "source": source,
                "configuration": run.caller.accounting.configuration.digest,
            }
        )
    ).hexdigest()
    caller = run.caller
    intent = caller.admit_action(
        run.ledger,
        run.identity,
        purpose="command",
        source=source,
        head_sha=run.head_sha,
        operation_key=key,
    )
    inventory = (
        current_inventory(run)
        if command.finding_reference or command.finding_references
        else None
    )
    app = application(run)
    from .commands import lookup_collaborator_permission

    permission = None
    if command.action in {
        "accept-risk",
        "accept-coverage-uncertainty",
        "continue",
        "reenroll",
        "dismiss",
        "defer",
        "approve-media",
    }:
        permission = lookup_collaborator_permission(
            run.http,
            token=run.ledger.token,
            repository=run.identity.repository,
            actor=actor["login"],
        )
    quarantine_refusal = None
    if quarantine is not None:
        from .quarantine_action import (
            QuarantineTargetRefusal,
            decision_authority,
            prepare_quarantine_target,
        )

        try:
            quarantine_target = prepare_quarantine_target(
                run, authority, quarantine, app, permission
            )
            run.ledger.business_guard = lambda: decision_authority(run, authority)
        except QuarantineTargetRefusal as error:
            quarantine_refusal = str(error)
    historical_refusal = None
    if historical is not None:
        from .historical_assessment_action import (
            HistoricalTargetRefusal,
            prepare_historical_target,
        )

        try:
            historical_target = prepare_historical_target(
                run, authority, historical, app, permission
            )
        except HistoricalTargetRefusal as error:
            # This process has not entered a decision mutation. A definite
            # selector/role refusal can finish as a charged explicit no-op;
            # transport/budget uncertainty and stale authority still escape.
            historical_refusal = str(error)
    fenced_comment = authority.fence()
    verify_live_comment_source(
        bound_source=source,
        live_comment=fenced_comment,
        phase="unstarted_intent",
    )
    boundary = caller.begin_action_effect(run.ledger, run.identity, intent)
    explicit_mode = getattr(args, "review_mode", None)
    if (
        explicit_mode is None
        and not os.environ.get("REVIEWSENSEI_REVIEW_MODE", "").strip()
        and run.plan.policy.reviews == "advisory"
    ):
        explicit_mode = "advisory"
    if quarantine_refusal is not None:
        from review_sensei.disposition import MaintainerCommandResult

        result = MaintainerCommandResult(
            action=command.action,
            applied=False,
            operator_paused=run.ledger.load(run.identity).record.operator_paused,
            summary="Carried historical assessment refused: " + quarantine_refusal,
        )
    elif quarantine is not None:
        from .quarantine_action import apply_quarantine_target

        result = apply_quarantine_target(
            run, authority, quarantine, quarantine_target, app
        )
    elif historical_refusal is not None:
        from review_sensei.disposition import MaintainerCommandResult

        result = MaintainerCommandResult(
            action=command.action,
            applied=False,
            operator_paused=run.ledger.load(run.identity).record.operator_paused,
            summary="Historical assessment refused: " + historical_refusal,
        )
    elif historical is not None:
        from .historical_assessment_action import apply_historical_target

        result = apply_historical_target(
            run, authority, historical, historical_target, app
        )
    else:
        result = app.apply_maintainer_command(
            options=_options(run),
            oidc_token=run.oidc,
            repository=run.identity.repository,
            repository_id=run.identity.repository_id,
            pull_request=run.identity.pull_request,
            head_sha=run.head_sha,
            body=args.comment_body,
            actor_login=actor["login"],
            actor_type=actor["type"],
            association=actor["association"],
            app_slug=args.app_slug,
            source_comment_id=args.source_comment_id,
            current_action=authority,
            finding_inventory=inventory,
            actor_permission=permission,
            command_budget=caller.budget_view,
            media_budget=caller.budget_view,
            media_read_token=run.ledger.token,
            operation_entry="enabled",
            app_id=run.ledger.authenticated_app_id,
            run_id=str(caller.run.run_id),
            run_attempt=str(caller.attempt),
            reviews_policy=run.plan.policy.reviews,
            convergence_policy=resolve_review_convergence_policy(
                mode=explicit_mode, quality=run.plan.policy.review_quality
            ),
        )
    if result.evaluation_failed:
        raise ReviewInputError(
            "command effect or required finalization remains unconfirmed: "
            + result.summary
        )
    outcome = (
        {"kind": "applied", "reason": None}
        if result.applied
        else {
            "kind": "explicit_noop",
            "reason": "status" if command.action == "status" else "no_change",
        }
    )
    require_action_settlement_representable(
        run,
        outcome=outcome,
        result_document=asdict(result),
        extra_work_requests=2 if result.receipts else 0,
        action_name=command.action,
    )
    # Receipt publication is part of this same action and must be confirmed.
    if result.receipts:
        from .action_receipt import publish_command_receipt

        publish_command_receipt(run, authority, args.comment_body, result)
    proof = caller.settle_action_effect(
        run.ledger,
        run.identity,
        boundary,
        outcome=outcome,
        result_document=asdict(result),
    )
    caller.begin_terminal("action-completion")
    caller.close_action(run.ledger, run.identity, proof)
    print(result.summary)
    return command_exit_code(result)


def run_action(
    argv: Sequence[str],
    *,
    purpose: str,
    verify_before_intent: bool = False,
    event_delivery: Mapping[str, Any] | None = None,
    has_pending_receipt: bool = False,
    unstarted_recovery: UnstartedCallerRecoveryGate | None = None,
    convergence_records: Sequence[AcceptedOperationRecord] = (),
    current_state_fingerprint: str | None = None,
    was_queued_replaced: bool = False,
) -> int:
    parser = argparse.ArgumentParser(prog="review-sensei-github " + purpose + "-run")
    parser.add_argument("--policy-commit", required=True)
    parser.add_argument("--run-directory", type=Path, required=True)
    parser.add_argument(
        "--verify-source-before-intent",
        action="store_true",
        default=False,
    )
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    options = parser.parse_args(list(argv))
    arguments = (
        options.arguments[1:] if options.arguments[:1] == ["--"] else options.arguments
    )
    from .commands import github_parser

    args = github_parser().parse_args([purpose, *arguments])
    if not args.allow_write:
        raise ReviewInputError("current Actions routes require --allow-write")
    if (
        getattr(args, "session_attestation", None) is not None
        or getattr(args, "oidc_token", None) is not None
    ):
        raise ReviewInputError(
            "current action authority is obtained inside its lifecycle"
        )
    if getattr(args, "session_ledger", None) is not None:
        raise ReviewInputError("current Actions routes require their native run ledger")
    if purpose == "command":
        from .historical_assessment_action import parse_historical_command
        from .quarantine_action import parse_quarantine_command

        parse_historical_command(args.comment_body)
        parse_quarantine_command(args.comment_body)
    source = event_source(args, purpose=purpose)
    args.provider = getattr(args, "provider", None)
    args.model = getattr(args, "model", None)
    args.base_sha = None
    if not args.head_sha:
        args.head_sha = None
    args.rescan = args.review_remaining = False
    with _cross_run_unstarted_rollover_guard(unstarted_recovery) as rollover_service:
        run = bootstrap(
            args,
            run_directory=options.run_directory,
            policy_commit=options.policy_commit,
            purpose=purpose,
            source=source,
            rollover_service=rollover_service,
        )
    same_run_gate = (
        unstarted_recovery
        if (unstarted_recovery is not None and unstarted_recovery.mode == "same_run")
        else None
    )
    lifecycle = reconcile_action_source_lifecycle(
        run,
        candidate_source=source,
        event_delivery=event_delivery,
        has_pending_receipt=has_pending_receipt,
        verify_before_intent=(
            verify_before_intent or options.verify_source_before_intent
        ),
        unstarted_recovery=same_run_gate,
    )
    if lifecycle is not None and lifecycle.route == "preserve_terminal_no_replay":
        return 0
    if lifecycle is not None and lifecycle.route == "g32_unstarted_cancellation":
        print("Unstarted action canceled after source change")
        return 0
    if was_queued_replaced or convergence_records:
        evaluate_action_convergence_progress(
            run,
            purpose=purpose,
            source=source,
            comment_body=getattr(args, "comment_body", "") or "",
            records=convergence_records,
            was_queued_replaced=was_queued_replaced,
            current_state_fingerprint=current_state_fingerprint,
        )
    if purpose == "command":
        return execute_command(run, args)
    from .action_reply import execute_reply

    return execute_reply(run, args)

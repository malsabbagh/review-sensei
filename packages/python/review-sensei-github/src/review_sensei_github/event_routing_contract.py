"""Explicit GitHub event routing, convergence, queue, thread, and review contracts (G01, G07, G13, G26, G28).

Separates caller workflow subscriptions from runtime admission guards, models
current-state convergence and Actions queue replacement, enforces the
``merge_group`` unsupported boundary, qualifies bounded ``@reviewsensei verify``
thread reconciliation, and proves native human ``pull_request_review``
independence from ReviewSensei ledger accounting and snapshot certification.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from typing import Any, Mapping, Sequence

from .approval import (
    AutoApprovalDecision,
    ReviewApprovalEligibility,
    approval_withheld_diagnostic,
    evaluate_approval_facts,
)
from .checks import REVIEW_CHECK_NAME, review_check_outcome
from .thread_resolution import ThreadResolutionResult, resolution_records
from .trigger import (
    TriggerResolution,
    comment_mentions_sensei,
    issue_comment_requests_remaining,
    issue_comment_requests_rescan,
    resolve_issue_comment,
    resolve_pull_request_event,
    resolve_review_comment_event,
)

_GIT_SHA_FULL = re.compile(r"^[a-f0-9]{40}$")
_GIT_REF = re.compile(r"^[A-Za-z0-9._/-]+$")

CALLER_SUBSCRIBED_EVENTS: Mapping[str, tuple[str, ...]] = {
    "pull_request": ("opened", "reopened", "synchronize", "ready_for_review"),
    "issue_comment": ("created",),
    "pull_request_review_comment": ("created",),
    "workflow_dispatch": ("manual",),
}

ALLOWED_COMMENT_ASSOCIATIONS: frozenset[str] = frozenset(
    {"OWNER", "MEMBER", "COLLABORATOR"}
)

REQUIRED_EVENT_FAMILIES: tuple[str, ...] = (
    "opened",
    "reopened",
    "synchronize",
    "ready_for_review",
    "edited",
    "converted_to_draft",
    "closed",
    "branch_recreation",
    "pull_request_review_thread",
    "comment_lifecycle",
    "pull_request_review",
    "merge_group",
    "workflow_dispatch",
)


@dataclass(frozen=True)
class EventDecisionRule:
    """Static routing and recovery contract for one GitHub event classification."""

    rule_id: str
    family: str
    event_name: str
    actions: tuple[str, ...]
    subtype: str
    caller_subscribed: bool
    runtime_disposition: str
    allows_model_inference: bool
    preserves_prior_obligations: bool
    read_time_guard: str
    recovery_command: str | None


EVENT_DECISION_TABLE: tuple[EventDecisionRule, ...] = (
    EventDecisionRule(
        rule_id="pull_request.opened",
        family="opened",
        event_name="pull_request",
        actions=("opened",),
        subtype="initial_open",
        caller_subscribed=True,
        runtime_disposition="review",
        allows_model_inference=True,
        preserves_prior_obligations=True,
        read_time_guard="live_pr_open_and_exact_boolean_draft_false",
        recovery_command="workflow_dispatch",
    ),
    EventDecisionRule(
        rule_id="pull_request.reopened",
        family="reopened",
        event_name="pull_request",
        actions=("reopened",),
        subtype="reopen",
        caller_subscribed=True,
        runtime_disposition="review_or_reuse_retained",
        allows_model_inference=True,
        preserves_prior_obligations=True,
        read_time_guard=(
            "live_pr_open_exact_boolean_draft_false_and_snapshot_reuse_check"
        ),
        recovery_command="workflow_dispatch",
    ),
    EventDecisionRule(
        rule_id="pull_request.synchronize",
        family="synchronize",
        event_name="pull_request",
        actions=("synchronize",),
        subtype="head_advance_or_push",
        caller_subscribed=True,
        runtime_disposition="review_or_reuse_retained",
        allows_model_inference=True,
        preserves_prior_obligations=True,
        read_time_guard="live_pr_open_exact_boolean_draft_false_and_snapshot_binding",
        recovery_command="workflow_dispatch",
    ),
    EventDecisionRule(
        rule_id="pull_request.ready_for_review",
        family="ready_for_review",
        event_name="pull_request",
        actions=("ready_for_review",),
        subtype="draft_exit",
        caller_subscribed=True,
        runtime_disposition="review_or_reuse_retained",
        allows_model_inference=True,
        preserves_prior_obligations=True,
        read_time_guard=(
            "live_pr_open_exact_boolean_draft_false_and_snapshot_reuse_check"
        ),
        recovery_command="workflow_dispatch",
    ),
    EventDecisionRule(
        rule_id="pull_request.edited.metadata",
        family="edited",
        event_name="pull_request",
        actions=("edited",),
        subtype="title_or_body",
        caller_subscribed=False,
        runtime_disposition="explicit_noop",
        allows_model_inference=False,
        preserves_prior_obligations=True,
        read_time_guard="shared_bootstrap_title_byte_limit_on_next_admitted_run",
        recovery_command="@reviewsensei verify",
    ),
    EventDecisionRule(
        rule_id="pull_request.edited.base",
        family="edited",
        event_name="pull_request",
        actions=("edited",),
        subtype="base_retarget",
        caller_subscribed=False,
        runtime_disposition="read_time_invalidation",
        allows_model_inference=False,
        preserves_prior_obligations=True,
        read_time_guard="exact_base_sha_and_base_ref_snapshot_binding",
        recovery_command="workflow_dispatch",
    ),
    EventDecisionRule(
        rule_id="pull_request.converted_to_draft",
        family="converted_to_draft",
        event_name="pull_request",
        actions=("converted_to_draft",),
        subtype="draft_enter",
        caller_subscribed=False,
        runtime_disposition="explicit_noop",
        allows_model_inference=False,
        preserves_prior_obligations=True,
        read_time_guard="exact_boolean_draft_gate_blocks_new_publication",
        recovery_command="workflow_dispatch",
    ),
    EventDecisionRule(
        rule_id="pull_request.closed",
        family="closed",
        event_name="pull_request",
        actions=("closed",),
        subtype="close_or_merge",
        caller_subscribed=False,
        runtime_disposition="explicit_noop",
        allows_model_inference=False,
        preserves_prior_obligations=True,
        read_time_guard="live_pr_state_open_gate_blocks_new_publication",
        recovery_command="@reviewsensei review status",
    ),
    EventDecisionRule(
        rule_id="branch.recreation",
        family="branch_recreation",
        event_name="create_or_delete",
        actions=("created", "deleted"),
        subtype="branch_ref_delete_or_recreate",
        caller_subscribed=False,
        runtime_disposition="read_time_invalidation",
        allows_model_inference=False,
        preserves_prior_obligations=True,
        read_time_guard="exact_pr_number_and_head_sha_binding_rejects_ref_aliasing",
        recovery_command="workflow_dispatch",
    ),
    EventDecisionRule(
        rule_id="pull_request_review_thread.manual",
        family="pull_request_review_thread",
        event_name="pull_request_review_thread",
        actions=("resolved", "unresolved"),
        subtype="native_thread_toggle",
        caller_subscribed=False,
        runtime_disposition="no_inference_manual_reconcile_only",
        allows_model_inference=False,
        preserves_prior_obligations=True,
        read_time_guard=(
            "live_graphql_thread_scan_separated_from_durable_finding_inventory"
        ),
        recovery_command="@reviewsensei verify",
    ),
    EventDecisionRule(
        rule_id="comment.created",
        family="comment_lifecycle",
        event_name="issue_comment_or_pull_request_review_comment",
        actions=("created",),
        subtype="comment_created",
        caller_subscribed=True,
        runtime_disposition="command_or_review_or_reply",
        allows_model_inference=True,
        preserves_prior_obligations=True,
        read_time_guard=(
            "live_source_comment_digest_non_bot_maintainer_and_pr_head_binding"
        ),
        recovery_command="@reviewsensei verify",
    ),
    EventDecisionRule(
        rule_id="comment.edited_or_deleted",
        family="comment_lifecycle",
        event_name="issue_comment_or_pull_request_review_comment",
        actions=("edited", "deleted"),
        subtype="comment_mutation",
        caller_subscribed=False,
        runtime_disposition="read_time_invalidation",
        allows_model_inference=False,
        preserves_prior_obligations=True,
        read_time_guard="immutable_source_comment_id_updated_at_and_body_sha256_fence",
        recovery_command="@reviewsensei verify",
    ),
    EventDecisionRule(
        rule_id="pull_request_review.human",
        family="pull_request_review",
        event_name="pull_request_review",
        actions=("submitted", "edited", "dismissed"),
        subtype="native_human_review",
        caller_subscribed=False,
        runtime_disposition="explicit_noop",
        allows_model_inference=False,
        preserves_prior_obligations=True,
        read_time_guard="app_slug_author_isolation_and_independent_ledger_accounting",
        recovery_command="@reviewsensei verify",
    ),
    EventDecisionRule(
        rule_id="merge_group.queue",
        family="merge_group",
        event_name="merge_group",
        actions=("checks_requested", "destroyed"),
        subtype="merge_queue",
        caller_subscribed=False,
        runtime_disposition="unsupported",
        allows_model_inference=False,
        preserves_prior_obligations=True,
        read_time_guard=(
            "merge_group_unsupported_and_exact_pr_base_head_sha_certification"
        ),
        recovery_command=None,
    ),
    EventDecisionRule(
        rule_id="workflow_dispatch.manual",
        family="workflow_dispatch",
        event_name="workflow_dispatch",
        actions=("manual",),
        subtype="operator_dispatch",
        caller_subscribed=True,
        runtime_disposition="manual_review_or_reuse_retained",
        allows_model_inference=True,
        preserves_prior_obligations=True,
        read_time_guard="live_pr_fetch_policy_freeze_and_retained_snapshot_reuse",
        recovery_command="workflow_dispatch",
    ),
)

EVENT_DECISION_BY_ID: Mapping[str, EventDecisionRule] = {
    rule.rule_id: rule for rule in EVENT_DECISION_TABLE
}


def decision_rule_for(rule_id: str) -> EventDecisionRule:
    """Return the canonical decision rule by identifier."""

    try:
        return EVENT_DECISION_BY_ID[rule_id]
    except KeyError as exc:
        raise KeyError(f"unknown event decision rule: {rule_id!r}") from exc


@dataclass(frozen=True)
class CallerSubscriptionReport:
    """Inspection summary for a caller workflow YAML file."""

    pull_request_types: tuple[str, ...]
    issue_comment_types: tuple[str, ...]
    review_comment_types: tuple[str, ...]
    has_workflow_dispatch: bool
    unapproved_triggers: tuple[str, ...]
    enforces_non_bot_comments: bool
    enforces_maintainer_associations: bool
    enforces_pull_request_issue_guard: bool
    read_only_caller_permissions: bool

    @property
    def matches_approved_boundary(self) -> bool:
        return (
            self.pull_request_types == CALLER_SUBSCRIBED_EVENTS["pull_request"]
            and self.issue_comment_types == CALLER_SUBSCRIBED_EVENTS["issue_comment"]
            and self.review_comment_types
            == CALLER_SUBSCRIBED_EVENTS["pull_request_review_comment"]
            and self.has_workflow_dispatch
            and not self.unapproved_triggers
            and self.enforces_non_bot_comments
            and self.enforces_maintainer_associations
            and self.enforces_pull_request_issue_guard
            and self.read_only_caller_permissions
        )


def _parse_inline_or_block_types(
    lines: list[str], start_index: int, indent: int
) -> tuple[str, ...]:
    """Extract ``types: [...]`` or block list items under one workflow trigger."""

    values: list[str] = []
    idx = start_index
    in_types_block = False
    types_indent = indent + 2
    while idx < len(lines):
        raw = lines[idx]
        stripped = raw.split("#", 1)[0].rstrip()
        if not stripped.strip():
            idx += 1
            continue
        current_indent = len(stripped) - len(stripped.lstrip(" "))
        if current_indent <= indent:
            break
        content = stripped.strip()
        if content.startswith("types:"):
            remainder = content[len("types:") :].strip()
            if remainder.startswith("[") and remainder.endswith("]"):
                inner = remainder[1:-1].strip()
                if inner:
                    values.extend(
                        item.strip().strip("'\"")
                        for item in inner.split(",")
                        if item.strip()
                    )
                in_types_block = False
            elif not remainder:
                in_types_block = True
                types_indent = current_indent
            idx += 1
            continue
        if in_types_block:
            if current_indent <= types_indent:
                in_types_block = False
                continue
            if content.startswith("- "):
                values.append(content[2:].strip().strip("'\""))
        idx += 1
    return tuple(values)


def inspect_caller_subscription_contract(
    workflow_text: str,
) -> CallerSubscriptionReport:
    """Inspect caller YAML subscriptions and runtime admission guards without PyYAML."""

    if not isinstance(workflow_text, str) or not workflow_text.strip():
        raise ValueError("caller workflow text must be a non-empty string")
    lines = workflow_text.splitlines()
    on_triggers: dict[str, tuple[str, ...]] = {}
    top_permissions: dict[str, str] = {}

    idx = 0
    while idx < len(lines):
        raw = lines[idx]
        stripped = raw.split("#", 1)[0].rstrip()
        if not stripped.strip():
            idx += 1
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        content = stripped.strip()
        if indent == 0 and content == "on:":
            idx += 1
            while idx < len(lines):
                sub_raw = lines[idx]
                sub_stripped = sub_raw.split("#", 1)[0].rstrip()
                if not sub_stripped.strip():
                    idx += 1
                    continue
                sub_indent = len(sub_stripped) - len(sub_stripped.lstrip(" "))
                if sub_indent == 0:
                    break
                if sub_indent == 2 and sub_stripped.strip().endswith(":"):
                    trigger_name = sub_stripped.strip()[:-1].strip().strip("'\"")
                    on_triggers[trigger_name] = _parse_inline_or_block_types(
                        lines, idx + 1, sub_indent
                    )
                elif sub_indent == 2 and ":" in sub_stripped.strip():
                    trigger_name, rest = sub_stripped.strip().split(":", 1)
                    trigger_key = trigger_name.strip().strip("'\"")
                    rest_clean = rest.strip()
                    if rest_clean.startswith("[") and rest_clean.endswith("]"):
                        on_triggers[trigger_key] = tuple(
                            item.strip().strip("'\"")
                            for item in rest_clean[1:-1].split(",")
                            if item.strip()
                        )
                    else:
                        on_triggers[trigger_key] = ()
                idx += 1
            continue
        if indent == 0 and content == "permissions:":
            idx += 1
            while idx < len(lines):
                perm_raw = lines[idx]
                perm_stripped = perm_raw.split("#", 1)[0].rstrip()
                if not perm_stripped.strip():
                    idx += 1
                    continue
                perm_indent = len(perm_stripped) - len(perm_stripped.lstrip(" "))
                if perm_indent == 0:
                    break
                if perm_indent == 2 and ":" in perm_stripped:
                    key, val = perm_stripped.strip().split(":", 1)
                    top_permissions[key.strip()] = val.strip()
                idx += 1
            continue
        idx += 1

    unapproved: list[str] = []
    for trigger_name, types in sorted(on_triggers.items()):
        if trigger_name not in CALLER_SUBSCRIBED_EVENTS:
            unapproved.append(trigger_name)
            continue
        if trigger_name == "workflow_dispatch":
            continue
        expected = set(CALLER_SUBSCRIBED_EVENTS[trigger_name])
        if not types:
            unapproved.append(f"{trigger_name}:*")
            continue
        for item in types:
            if item not in expected:
                unapproved.append(f"{trigger_name}:{item}")

    enforces_non_bot = "github.event.comment.user.type != 'Bot'" in workflow_text
    enforces_associations = all(
        f"github.event.comment.author_association == '{role}'" in workflow_text
        for role in ("OWNER", "MEMBER", "COLLABORATOR")
    )
    enforces_pr_issue = "github.event.issue.pull_request" in workflow_text

    expected_permissions = {
        "contents": "read",
        "pull-requests": "read",
        "issues": "read",
        "id-token": "write",
    }
    read_only_caller_permissions = top_permissions == expected_permissions

    return CallerSubscriptionReport(
        pull_request_types=on_triggers.get("pull_request", ()),
        issue_comment_types=on_triggers.get("issue_comment", ()),
        review_comment_types=on_triggers.get("pull_request_review_comment", ()),
        has_workflow_dispatch="workflow_dispatch" in on_triggers,
        unapproved_triggers=tuple(unapproved),
        enforces_non_bot_comments=enforces_non_bot,
        enforces_maintainer_associations=enforces_associations,
        enforces_pull_request_issue_guard=enforces_pr_issue,
        read_only_caller_permissions=read_only_caller_permissions,
    )


@dataclass(frozen=True)
class SourceEventIdentity:
    """Immutable per-source event identity preserved through parser -> admission."""

    event_name: str
    action: str
    delivery_id: str | None = None
    pull_request_number: int | None = None
    source_kind: str | None = None
    source_comment_id: int | None = None
    source_updated_at: str | None = None
    source_body_sha256: str | None = None
    actor_login: str | None = None
    actor_type: str | None = None
    author_association: str | None = None

    @property
    def source_dedup_key(self) -> str | None:
        if (
            self.source_kind is not None
            and self.source_comment_id is not None
            and self.source_body_sha256 is not None
        ):
            return (
                f"{self.source_kind}:{self.source_comment_id}:{self.source_body_sha256}"
            )
        if self.delivery_id:
            return f"delivery:{self.delivery_id}"
        return None


@dataclass(frozen=True)
class RetainedSnapshotState:
    """Retained snapshot and durable obligation summary for a pull request."""

    reviewed_base_sha: str
    reviewed_base_ref: str
    reviewed_head_sha: str
    review_status: str = "complete"
    open_finding_ids: tuple[str, ...] = ()
    accepted_source_keys: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.reviewed_base_sha, str) or not _GIT_SHA_FULL.fullmatch(
            self.reviewed_base_sha
        ):
            raise ValueError("retained reviewed_base_sha must be a 40-hex SHA")
        if not isinstance(self.reviewed_head_sha, str) or not _GIT_SHA_FULL.fullmatch(
            self.reviewed_head_sha
        ):
            raise ValueError("retained reviewed_head_sha must be a 40-hex SHA")
        if not isinstance(self.reviewed_base_ref, str) or not _GIT_REF.fullmatch(
            self.reviewed_base_ref
        ):
            raise ValueError("retained reviewed_base_ref must be a valid git ref")


@dataclass(frozen=True)
class EventRoutingDecision:
    """Complete outcome of evaluating one GitHub event through parser -> admission."""

    rule_id: str
    family: str
    event_name: str
    action: str
    caller_subscribed: bool
    admitted: bool
    disposition: str
    outcome_code: str
    operation: str | None
    command_precedence_tier: str | None
    resolution: TriggerResolution | None
    source_identity: SourceEventIdentity
    requires_model_inference: bool
    reused_retained_snapshot: bool
    invalidates_retained_baseline: bool
    ref_only_base_retarget: bool
    preserved_open_finding_ids: tuple[str, ...]
    read_time_guard: str
    recovery_command: str | None


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _as_mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _extract_pr_number_from_url(url: object) -> int | None:
    if not isinstance(url, str):
        return None
    match = re.search(r"/pulls/([1-9][0-9]*)\Z", url)
    if match is None:
        return None
    return int(match.group(1))


def _coerce_payload_mapping(
    raw_payload: Mapping[str, Any] | bytes | bytearray | str | object,
) -> Mapping[str, Any] | None:
    if isinstance(raw_payload, Mapping):
        return raw_payload
    if isinstance(raw_payload, (bytes, bytearray)):
        try:
            decoded = bytes(raw_payload).decode("utf-8", errors="strict")
            parsed = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    if isinstance(raw_payload, str):
        try:
            parsed = json.loads(raw_payload)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _validate_exact_draft_flag(pull: Mapping[str, Any]) -> tuple[bool, str | None]:
    """Enforce G24 exact boolean draft validation (False/True vs malformed)."""

    if "draft" not in pull:
        return False, "draft_boolean_invalid"
    draft = pull.get("draft")
    if type(draft) is not bool:
        return False, "draft_boolean_invalid"
    if draft is True:
        return False, "draft_skipped"
    return True, None


def _snapshot_compatibility(
    pull: Mapping[str, Any],
    retained: RetainedSnapshotState | None,
) -> tuple[bool, bool, bool]:
    """Return ``(exact_reuse, invalidates_baseline, ref_only_retarget)``."""

    if retained is None:
        return False, False, False
    head = pull.get("head")
    base = pull.get("base")
    if not isinstance(head, Mapping) or not isinstance(base, Mapping):
        return False, False, False
    head_sha = head.get("sha")
    base_sha = base.get("sha")
    base_ref = base.get("ref")
    if not isinstance(head_sha, str) or not isinstance(base_sha, str):
        return False, False, False
    if base_sha != retained.reviewed_base_sha:
        return False, True, False
    ref_only = isinstance(base_ref, str) and base_ref != retained.reviewed_base_ref
    if head_sha != retained.reviewed_head_sha:
        return False, False, ref_only
    exact_reuse = retained.review_status == "complete" and not ref_only
    return exact_reuse, False, ref_only


def _build_comment_identity(
    *,
    event_name: str,
    action: str,
    delivery_id: str | None,
    pr_number: int | None,
    source_kind: str,
    comment: Mapping[str, Any] | None,
) -> SourceEventIdentity:
    if not isinstance(comment, Mapping):
        return SourceEventIdentity(
            event_name=event_name,
            action=action,
            delivery_id=delivery_id,
            pull_request_number=pr_number,
            source_kind=source_kind,
        )
    comment_id = _positive_int(comment.get("id"))
    updated_at = _opt_str(comment.get("updated_at"))
    body = comment.get("body")
    body_digest = _sha256_text(body) if isinstance(body, str) else None
    user = _as_mapping(comment.get("user"))
    actor_login = _opt_str(user.get("login"))
    actor_type = _opt_str(user.get("type"))
    association = _opt_str(comment.get("author_association"))
    return SourceEventIdentity(
        event_name=event_name,
        action=action,
        delivery_id=delivery_id,
        pull_request_number=pr_number,
        source_kind=source_kind,
        source_comment_id=comment_id,
        source_updated_at=updated_at,
        source_body_sha256=body_digest,
        actor_login=actor_login,
        actor_type=actor_type,
        author_association=association,
    )


def evaluate_github_event(
    event_name: str,
    payload: Mapping[str, Any] | bytes | bytearray | str | object,
    *,
    delivery_id: str | None = None,
    live_pull_request: Mapping[str, Any] | None = None,
    retained_state: RetainedSnapshotState | None = None,
) -> EventRoutingDecision:
    """Evaluate any GitHub event payload safely from parser through admission."""

    preserved_findings = (
        retained_state.open_finding_ids if retained_state is not None else ()
    )
    normalized_event = event_name.strip() if isinstance(event_name, str) else ""
    clean_delivery = (
        delivery_id.strip()
        if isinstance(delivery_id, str) and delivery_id.strip()
        else None
    )
    parsed = _coerce_payload_mapping(payload)
    if not normalized_event or parsed is None:
        return EventRoutingDecision(
            rule_id="malformed.envelope",
            family="malformed",
            event_name=normalized_event or "<empty>",
            action="<invalid>",
            caller_subscribed=False,
            admitted=False,
            disposition="malformed_payload",
            outcome_code="malformed_event_payload",
            operation=None,
            command_precedence_tier=None,
            resolution=None,
            source_identity=SourceEventIdentity(
                event_name=normalized_event or "<empty>",
                action="<invalid>",
                delivery_id=clean_delivery,
            ),
            requires_model_inference=False,
            reused_retained_snapshot=False,
            invalidates_retained_baseline=False,
            ref_only_base_retarget=False,
            preserved_open_finding_ids=preserved_findings,
            read_time_guard="json_object_envelope_and_event_name_required",
            recovery_command="workflow_dispatch",
        )

    raw_action = parsed.get("action")
    action = (
        raw_action.strip()
        if isinstance(raw_action, str) and raw_action.strip()
        else ("manual" if normalized_event == "workflow_dispatch" else "<missing>")
    )

    if normalized_event == "merge_group":
        rule = decision_rule_for("merge_group.queue")
        return EventRoutingDecision(
            rule_id=rule.rule_id,
            family=rule.family,
            event_name=normalized_event,
            action=action,
            caller_subscribed=False,
            admitted=False,
            disposition="unsupported",
            outcome_code="merge_group_unsupported",
            operation=None,
            command_precedence_tier=None,
            resolution=None,
            source_identity=SourceEventIdentity(
                event_name=normalized_event,
                action=action,
                delivery_id=clean_delivery,
            ),
            requires_model_inference=False,
            reused_retained_snapshot=False,
            invalidates_retained_baseline=False,
            ref_only_base_retarget=False,
            preserved_open_finding_ids=preserved_findings,
            read_time_guard=rule.read_time_guard,
            recovery_command=rule.recovery_command,
        )

    if normalized_event in {"create", "delete"}:
        ref_type = parsed.get("ref_type")
        rule = decision_rule_for("branch.recreation")
        return EventRoutingDecision(
            rule_id=rule.rule_id,
            family=rule.family,
            event_name=normalized_event,
            action=f"{normalized_event}:{ref_type}"
            if isinstance(ref_type, str)
            else normalized_event,
            caller_subscribed=False,
            admitted=False,
            disposition="read_time_invalidation",
            outcome_code="branch_recreation_invalidation",
            operation=None,
            command_precedence_tier=None,
            resolution=None,
            source_identity=SourceEventIdentity(
                event_name=normalized_event,
                action=normalized_event,
                delivery_id=clean_delivery,
            ),
            requires_model_inference=False,
            reused_retained_snapshot=False,
            invalidates_retained_baseline=False,
            ref_only_base_retarget=False,
            preserved_open_finding_ids=preserved_findings,
            read_time_guard=rule.read_time_guard,
            recovery_command=rule.recovery_command,
        )

    if normalized_event == "pull_request_review_thread":
        rule = decision_rule_for("pull_request_review_thread.manual")
        pr_obj = _as_mapping(parsed.get("pull_request"))
        thread_pr_number = _positive_int(pr_obj.get("number"))
        sender = _as_mapping(parsed.get("sender"))
        return EventRoutingDecision(
            rule_id=rule.rule_id,
            family=rule.family,
            event_name=normalized_event,
            action=action,
            caller_subscribed=False,
            admitted=False,
            disposition="no_inference_manual_reconcile_only",
            outcome_code="thread_event_manual_verify_only",
            operation=None,
            command_precedence_tier=None,
            resolution=None,
            source_identity=SourceEventIdentity(
                event_name=normalized_event,
                action=action,
                delivery_id=clean_delivery,
                pull_request_number=thread_pr_number,
                actor_login=_opt_str(sender.get("login")),
                actor_type=_opt_str(sender.get("type")),
            ),
            requires_model_inference=False,
            reused_retained_snapshot=False,
            invalidates_retained_baseline=False,
            ref_only_base_retarget=False,
            preserved_open_finding_ids=preserved_findings,
            read_time_guard=rule.read_time_guard,
            recovery_command=rule.recovery_command,
        )

    if normalized_event == "pull_request_review":
        rule = decision_rule_for("pull_request_review.human")
        pr_obj = _as_mapping(parsed.get("pull_request"))
        review_pr_number = _positive_int(pr_obj.get("number"))
        review_obj = _as_mapping(parsed.get("review"))
        reviewer = _as_mapping(review_obj.get("user"))
        return EventRoutingDecision(
            rule_id=rule.rule_id,
            family=rule.family,
            event_name=normalized_event,
            action=action,
            caller_subscribed=False,
            admitted=False,
            disposition="explicit_noop",
            outcome_code="human_review_independent_noop",
            operation=None,
            command_precedence_tier=None,
            resolution=None,
            source_identity=SourceEventIdentity(
                event_name=normalized_event,
                action=action,
                delivery_id=clean_delivery,
                pull_request_number=review_pr_number,
                actor_login=_opt_str(reviewer.get("login")),
                actor_type=_opt_str(reviewer.get("type")),
                author_association=_opt_str(review_obj.get("author_association")),
            ),
            requires_model_inference=False,
            reused_retained_snapshot=False,
            invalidates_retained_baseline=False,
            ref_only_base_retarget=False,
            preserved_open_finding_ids=preserved_findings,
            read_time_guard=rule.read_time_guard,
            recovery_command=rule.recovery_command,
        )

    if normalized_event == "pull_request":
        pull = parsed.get("pull_request")
        pr_event_number = (
            _positive_int(pull.get("number")) if isinstance(pull, Mapping) else None
        )
        sender = _as_mapping(parsed.get("sender"))
        identity = SourceEventIdentity(
            event_name=normalized_event,
            action=action,
            delivery_id=clean_delivery,
            pull_request_number=pr_event_number,
            actor_login=_opt_str(sender.get("login")),
            actor_type=_opt_str(sender.get("type")),
        )

        if action == "edited":
            changes = parsed.get("changes")
            base_changed = isinstance(changes, Mapping) and "base" in changes
            if (
                not base_changed
                and isinstance(pull, Mapping)
                and retained_state is not None
            ):
                _, invalidates_base, ref_only = _snapshot_compatibility(
                    pull, retained_state
                )
                base_changed = invalidates_base or ref_only
            if base_changed:
                rule = decision_rule_for("pull_request.edited.base")
                ref_only = False
                invalidates_base = True
                if isinstance(pull, Mapping) and retained_state is not None:
                    _, invalidates_base_sha, ref_only = _snapshot_compatibility(
                        pull, retained_state
                    )
                    invalidates_base = invalidates_base_sha or not ref_only
                return EventRoutingDecision(
                    rule_id=rule.rule_id,
                    family=rule.family,
                    event_name=normalized_event,
                    action=action,
                    caller_subscribed=False,
                    admitted=False,
                    disposition="read_time_invalidation",
                    outcome_code=(
                        "base_ref_retargeted_without_sha_change"
                        if ref_only and not invalidates_base
                        else "base_retargeted_invalidation"
                    ),
                    operation=None,
                    command_precedence_tier=None,
                    resolution=None,
                    source_identity=identity,
                    requires_model_inference=False,
                    reused_retained_snapshot=False,
                    invalidates_retained_baseline=invalidates_base,
                    ref_only_base_retarget=ref_only,
                    preserved_open_finding_ids=preserved_findings,
                    read_time_guard=rule.read_time_guard,
                    recovery_command=rule.recovery_command,
                )
            rule = decision_rule_for("pull_request.edited.metadata")
            return EventRoutingDecision(
                rule_id=rule.rule_id,
                family=rule.family,
                event_name=normalized_event,
                action=action,
                caller_subscribed=False,
                admitted=False,
                disposition="explicit_noop",
                outcome_code="title_or_metadata_edited_noop",
                operation=None,
                command_precedence_tier=None,
                resolution=None,
                source_identity=identity,
                requires_model_inference=False,
                reused_retained_snapshot=False,
                invalidates_retained_baseline=False,
                ref_only_base_retarget=False,
                preserved_open_finding_ids=preserved_findings,
                read_time_guard=rule.read_time_guard,
                recovery_command=rule.recovery_command,
            )

        if action == "converted_to_draft":
            rule = decision_rule_for("pull_request.converted_to_draft")
            return EventRoutingDecision(
                rule_id=rule.rule_id,
                family=rule.family,
                event_name=normalized_event,
                action=action,
                caller_subscribed=False,
                admitted=False,
                disposition="explicit_noop",
                outcome_code="converted_to_draft_noop",
                operation=None,
                command_precedence_tier=None,
                resolution=None,
                source_identity=identity,
                requires_model_inference=False,
                reused_retained_snapshot=False,
                invalidates_retained_baseline=False,
                ref_only_base_retarget=False,
                preserved_open_finding_ids=preserved_findings,
                read_time_guard=rule.read_time_guard,
                recovery_command=rule.recovery_command,
            )

        if action == "closed":
            rule = decision_rule_for("pull_request.closed")
            return EventRoutingDecision(
                rule_id=rule.rule_id,
                family=rule.family,
                event_name=normalized_event,
                action=action,
                caller_subscribed=False,
                admitted=False,
                disposition="explicit_noop",
                outcome_code="closed_pr_noop",
                operation=None,
                command_precedence_tier=None,
                resolution=None,
                source_identity=identity,
                requires_model_inference=False,
                reused_retained_snapshot=False,
                invalidates_retained_baseline=False,
                ref_only_base_retarget=False,
                preserved_open_finding_ids=preserved_findings,
                read_time_guard=rule.read_time_guard,
                recovery_command=rule.recovery_command,
            )

        if action not in CALLER_SUBSCRIBED_EVENTS["pull_request"]:
            return EventRoutingDecision(
                rule_id="pull_request.unsubscribed",
                family="unsubscribed_pull_request_action",
                event_name=normalized_event,
                action=action,
                caller_subscribed=False,
                admitted=False,
                disposition="explicit_noop",
                outcome_code="unsubscribed_pull_request_action",
                operation=None,
                command_precedence_tier=None,
                resolution=None,
                source_identity=identity,
                requires_model_inference=False,
                reused_retained_snapshot=False,
                invalidates_retained_baseline=False,
                ref_only_base_retarget=False,
                preserved_open_finding_ids=preserved_findings,
                read_time_guard="caller_pull_request_action_allowlist",
                recovery_command="workflow_dispatch",
            )

        rule = decision_rule_for(f"pull_request.{action}")
        effective_pull = (
            live_pull_request if isinstance(live_pull_request, Mapping) else pull
        )
        if not isinstance(effective_pull, Mapping):
            return EventRoutingDecision(
                rule_id=rule.rule_id,
                family=rule.family,
                event_name=normalized_event,
                action=action,
                caller_subscribed=True,
                admitted=False,
                disposition="malformed_payload",
                outcome_code="malformed_pull_request_payload",
                operation=None,
                command_precedence_tier=None,
                resolution=None,
                source_identity=identity,
                requires_model_inference=False,
                reused_retained_snapshot=False,
                invalidates_retained_baseline=False,
                ref_only_base_retarget=False,
                preserved_open_finding_ids=preserved_findings,
                read_time_guard=rule.read_time_guard,
                recovery_command=rule.recovery_command,
            )

        state = effective_pull.get("state")
        if state is not None and state != "open":
            return EventRoutingDecision(
                rule_id=rule.rule_id,
                family=rule.family,
                event_name=normalized_event,
                action=action,
                caller_subscribed=True,
                admitted=False,
                disposition="rejected_guard",
                outcome_code="closed_pr_rejected_at_admission",
                operation=None,
                command_precedence_tier=None,
                resolution=None,
                source_identity=identity,
                requires_model_inference=False,
                reused_retained_snapshot=False,
                invalidates_retained_baseline=False,
                ref_only_base_retarget=False,
                preserved_open_finding_ids=preserved_findings,
                read_time_guard=rule.read_time_guard,
                recovery_command="@reviewsensei review status",
            )

        draft_ok, draft_code = _validate_exact_draft_flag(effective_pull)
        if not draft_ok:
            assert draft_code is not None
            return EventRoutingDecision(
                rule_id=rule.rule_id,
                family=rule.family,
                event_name=normalized_event,
                action=action,
                caller_subscribed=True,
                admitted=False,
                disposition=(
                    "explicit_noop"
                    if draft_code == "draft_skipped"
                    else "rejected_guard"
                ),
                outcome_code=draft_code,
                operation=None,
                command_precedence_tier=None,
                resolution=None,
                source_identity=identity,
                requires_model_inference=False,
                reused_retained_snapshot=False,
                invalidates_retained_baseline=False,
                ref_only_base_retarget=False,
                preserved_open_finding_ids=preserved_findings,
                read_time_guard=rule.read_time_guard,
                recovery_command=rule.recovery_command,
            )

        try:
            resolution = resolve_pull_request_event(effective_pull)
        except ValueError:
            return EventRoutingDecision(
                rule_id=rule.rule_id,
                family=rule.family,
                event_name=normalized_event,
                action=action,
                caller_subscribed=True,
                admitted=False,
                disposition="malformed_payload",
                outcome_code="invalid_pull_request_identity",
                operation=None,
                command_precedence_tier=None,
                resolution=None,
                source_identity=identity,
                requires_model_inference=False,
                reused_retained_snapshot=False,
                invalidates_retained_baseline=False,
                ref_only_base_retarget=False,
                preserved_open_finding_ids=preserved_findings,
                read_time_guard=rule.read_time_guard,
                recovery_command=rule.recovery_command,
            )

        exact_reuse, invalidates_base, ref_only = _snapshot_compatibility(
            effective_pull, retained_state
        )
        if exact_reuse:
            return EventRoutingDecision(
                rule_id=rule.rule_id,
                family=rule.family,
                event_name=normalized_event,
                action=action,
                caller_subscribed=True,
                admitted=False,
                disposition="reuse_retained_snapshot",
                outcome_code="unchanged_snapshot_reused",
                operation=None,
                command_precedence_tier=None,
                resolution=resolution,
                source_identity=identity,
                requires_model_inference=False,
                reused_retained_snapshot=True,
                invalidates_retained_baseline=False,
                ref_only_base_retarget=False,
                preserved_open_finding_ids=preserved_findings,
                read_time_guard=rule.read_time_guard,
                recovery_command="@reviewsensei rescan",
            )

        return EventRoutingDecision(
            rule_id=rule.rule_id,
            family=rule.family,
            event_name=normalized_event,
            action=action,
            caller_subscribed=True,
            admitted=True,
            disposition="admitted_review",
            outcome_code="review_scheduled",
            operation=resolution.operation,
            command_precedence_tier=None,
            resolution=resolution,
            source_identity=identity,
            requires_model_inference=True,
            reused_retained_snapshot=False,
            invalidates_retained_baseline=invalidates_base,
            ref_only_base_retarget=ref_only,
            preserved_open_finding_ids=preserved_findings,
            read_time_guard=rule.read_time_guard,
            recovery_command=rule.recovery_command,
        )

    if normalized_event in {"issue_comment", "pull_request_review_comment"}:
        source_kind = (
            "inline" if normalized_event == "pull_request_review_comment" else "issue"
        )
        comment = (
            parsed.get("comment")
            if isinstance(parsed.get("comment"), Mapping)
            else None
        )
        issue = (
            parsed.get("issue") if isinstance(parsed.get("issue"), Mapping) else None
        )
        pr_payload = (
            parsed.get("pull_request")
            if isinstance(parsed.get("pull_request"), Mapping)
            else None
        )
        pr_number = (
            _positive_int(issue.get("number"))
            if issue is not None
            else (
                _positive_int(pr_payload.get("number"))
                if pr_payload is not None
                else _extract_pr_number_from_url(
                    comment.get("pull_request_url") if comment is not None else None
                )
            )
        )
        identity = _build_comment_identity(
            event_name=normalized_event,
            action=action,
            delivery_id=clean_delivery,
            pr_number=pr_number,
            source_kind=source_kind,
            comment=comment,
        )

        if action in {"edited", "deleted"}:
            rule = decision_rule_for("comment.edited_or_deleted")
            return EventRoutingDecision(
                rule_id=rule.rule_id,
                family=rule.family,
                event_name=normalized_event,
                action=action,
                caller_subscribed=False,
                admitted=False,
                disposition="read_time_invalidation",
                outcome_code="comment_mutation_invalidated",
                operation=None,
                command_precedence_tier=None,
                resolution=None,
                source_identity=identity,
                requires_model_inference=False,
                reused_retained_snapshot=False,
                invalidates_retained_baseline=False,
                ref_only_base_retarget=False,
                preserved_open_finding_ids=preserved_findings,
                read_time_guard=rule.read_time_guard,
                recovery_command=rule.recovery_command,
            )

        if action != "created":
            rule = decision_rule_for("comment.edited_or_deleted")
            return EventRoutingDecision(
                rule_id=rule.rule_id,
                family=rule.family,
                event_name=normalized_event,
                action=action,
                caller_subscribed=False,
                admitted=False,
                disposition="explicit_noop",
                outcome_code="unsubscribed_comment_action",
                operation=None,
                command_precedence_tier=None,
                resolution=None,
                source_identity=identity,
                requires_model_inference=False,
                reused_retained_snapshot=False,
                invalidates_retained_baseline=False,
                ref_only_base_retarget=False,
                preserved_open_finding_ids=preserved_findings,
                read_time_guard=rule.read_time_guard,
                recovery_command=rule.recovery_command,
            )

        rule = decision_rule_for("comment.created")
        if comment is None or not isinstance(comment.get("body"), str):
            return EventRoutingDecision(
                rule_id=rule.rule_id,
                family=rule.family,
                event_name=normalized_event,
                action=action,
                caller_subscribed=True,
                admitted=False,
                disposition="malformed_payload",
                outcome_code="malformed_comment_payload",
                operation=None,
                command_precedence_tier=None,
                resolution=None,
                source_identity=identity,
                requires_model_inference=False,
                reused_retained_snapshot=False,
                invalidates_retained_baseline=False,
                ref_only_base_retarget=False,
                preserved_open_finding_ids=preserved_findings,
                read_time_guard=rule.read_time_guard,
                recovery_command=rule.recovery_command,
            )

        if normalized_event == "issue_comment" and (
            issue is None or not isinstance(issue.get("pull_request"), Mapping)
        ):
            return EventRoutingDecision(
                rule_id=rule.rule_id,
                family=rule.family,
                event_name=normalized_event,
                action=action,
                caller_subscribed=True,
                admitted=False,
                disposition="rejected_guard",
                outcome_code="not_pull_request_comment",
                operation=None,
                command_precedence_tier=None,
                resolution=None,
                source_identity=identity,
                requires_model_inference=False,
                reused_retained_snapshot=False,
                invalidates_retained_baseline=False,
                ref_only_base_retarget=False,
                preserved_open_finding_ids=preserved_findings,
                read_time_guard=rule.read_time_guard,
                recovery_command=None,
            )

        if identity.actor_type == "Bot" or identity.actor_type is None:
            return EventRoutingDecision(
                rule_id=rule.rule_id,
                family=rule.family,
                event_name=normalized_event,
                action=action,
                caller_subscribed=True,
                admitted=False,
                disposition="rejected_guard",
                outcome_code=(
                    "bot_actor_ignored"
                    if identity.actor_type == "Bot"
                    else "actor_type_missing"
                ),
                operation=None,
                command_precedence_tier=None,
                resolution=None,
                source_identity=identity,
                requires_model_inference=False,
                reused_retained_snapshot=False,
                invalidates_retained_baseline=False,
                ref_only_base_retarget=False,
                preserved_open_finding_ids=preserved_findings,
                read_time_guard=rule.read_time_guard,
                recovery_command=None,
            )

        if identity.author_association not in ALLOWED_COMMENT_ASSOCIATIONS:
            return EventRoutingDecision(
                rule_id=rule.rule_id,
                family=rule.family,
                event_name=normalized_event,
                action=action,
                caller_subscribed=True,
                admitted=False,
                disposition="rejected_guard",
                outcome_code="association_unauthorized",
                operation=None,
                command_precedence_tier=None,
                resolution=None,
                source_identity=identity,
                requires_model_inference=False,
                reused_retained_snapshot=False,
                invalidates_retained_baseline=False,
                ref_only_base_retarget=False,
                preserved_open_finding_ids=preserved_findings,
                read_time_guard=rule.read_time_guard,
                recovery_command=None,
            )

        body = comment["body"]
        assert isinstance(body, str)
        if not comment_mentions_sensei(body):
            return EventRoutingDecision(
                rule_id=rule.rule_id,
                family=rule.family,
                event_name=normalized_event,
                action=action,
                caller_subscribed=True,
                admitted=False,
                disposition="explicit_noop",
                outcome_code="no_sensei_mention",
                operation=None,
                command_precedence_tier=None,
                resolution=None,
                source_identity=identity,
                requires_model_inference=False,
                reused_retained_snapshot=False,
                invalidates_retained_baseline=False,
                ref_only_base_retarget=False,
                preserved_open_finding_ids=preserved_findings,
                read_time_guard=rule.read_time_guard,
                recovery_command=None,
            )

        dedup_key = identity.source_dedup_key
        if (
            retained_state is not None
            and dedup_key is not None
            and dedup_key in retained_state.accepted_source_keys
        ):
            return EventRoutingDecision(
                rule_id=rule.rule_id,
                family=rule.family,
                event_name=normalized_event,
                action=action,
                caller_subscribed=True,
                admitted=False,
                disposition="reuse_retained_snapshot",
                outcome_code="duplicate_source_event_reused",
                operation=None,
                command_precedence_tier=None,
                resolution=None,
                source_identity=identity,
                requires_model_inference=False,
                reused_retained_snapshot=True,
                invalidates_retained_baseline=False,
                ref_only_base_retarget=False,
                preserved_open_finding_ids=preserved_findings,
                read_time_guard=rule.read_time_guard,
                recovery_command="@reviewsensei review status",
            )

        effective_pull = (
            live_pull_request if isinstance(live_pull_request, Mapping) else pr_payload
        )
        if not isinstance(effective_pull, Mapping):
            return EventRoutingDecision(
                rule_id=rule.rule_id,
                family=rule.family,
                event_name=normalized_event,
                action=action,
                caller_subscribed=True,
                admitted=False,
                disposition="malformed_payload",
                outcome_code="live_pull_request_unavailable",
                operation=None,
                command_precedence_tier=None,
                resolution=None,
                source_identity=identity,
                requires_model_inference=False,
                reused_retained_snapshot=False,
                invalidates_retained_baseline=False,
                ref_only_base_retarget=False,
                preserved_open_finding_ids=preserved_findings,
                read_time_guard=rule.read_time_guard,
                recovery_command="workflow_dispatch",
            )

        try:
            if normalized_event == "issue_comment":
                resolution = resolve_issue_comment(body, effective_pull)
            else:
                resolution = resolve_review_comment_event(body, effective_pull)
        except ValueError as exc:
            message = str(exc)
            code = (
                "requested_commit_mismatch"
                if "does not match the pull request head" in message
                else "invalid_comment_trigger"
            )
            return EventRoutingDecision(
                rule_id=rule.rule_id,
                family=rule.family,
                event_name=normalized_event,
                action=action,
                caller_subscribed=True,
                admitted=False,
                disposition="rejected_guard",
                outcome_code=code,
                operation=None,
                command_precedence_tier=None,
                resolution=None,
                source_identity=identity,
                requires_model_inference=False,
                reused_retained_snapshot=False,
                invalidates_retained_baseline=False,
                ref_only_base_retarget=False,
                preserved_open_finding_ids=preserved_findings,
                read_time_guard=rule.read_time_guard,
                recovery_command="@reviewsensei review status",
            )

        _, invalidates_base, ref_only = _snapshot_compatibility(
            effective_pull, retained_state
        )
        if resolution.operation == "review":
            tier = "rescan_or_remaining_review"
            disposition = "admitted_review"
            outcome_code = (
                "remaining_review_scheduled"
                if issue_comment_requests_remaining(body)
                else "rescan_review_scheduled"
                if issue_comment_requests_rescan(body)
                else "review_scheduled"
            )
            needs_inference = True
        elif resolution.operation == "command":
            tier = "maintainer_command"
            disposition = "admitted_command"
            outcome_code = "command_scheduled"
            needs_inference = False
        else:
            tier = "mention_reply"
            disposition = "admitted_reply"
            outcome_code = "reply_scheduled"
            needs_inference = True

        return EventRoutingDecision(
            rule_id=rule.rule_id,
            family=rule.family,
            event_name=normalized_event,
            action=action,
            caller_subscribed=True,
            admitted=True,
            disposition=disposition,
            outcome_code=outcome_code,
            operation=resolution.operation,
            command_precedence_tier=tier,
            resolution=resolution,
            source_identity=identity,
            requires_model_inference=needs_inference,
            reused_retained_snapshot=False,
            invalidates_retained_baseline=invalidates_base,
            ref_only_base_retarget=ref_only,
            preserved_open_finding_ids=preserved_findings,
            read_time_guard=rule.read_time_guard,
            recovery_command=rule.recovery_command,
        )

    if normalized_event == "workflow_dispatch":
        rule = decision_rule_for("workflow_dispatch.manual")
        raw_inputs = parsed.get("inputs")
        inputs: Mapping[str, Any] = (
            raw_inputs if isinstance(raw_inputs, Mapping) else parsed
        )
        raw_pr = inputs.get("pull_request_number")
        dispatch_pr_number: int | None = None
        if isinstance(raw_pr, int) and not isinstance(raw_pr, bool) and raw_pr >= 1:
            dispatch_pr_number = raw_pr
        elif isinstance(raw_pr, str) and re.fullmatch(r"[1-9][0-9]*", raw_pr):
            dispatch_pr_number = int(raw_pr)
        sender = _as_mapping(parsed.get("sender"))
        identity = SourceEventIdentity(
            event_name=normalized_event,
            action="manual",
            delivery_id=clean_delivery,
            pull_request_number=dispatch_pr_number,
            actor_login=_opt_str(sender.get("login")),
            actor_type=_opt_str(sender.get("type")),
        )
        if dispatch_pr_number is None:
            return EventRoutingDecision(
                rule_id=rule.rule_id,
                family=rule.family,
                event_name=normalized_event,
                action="manual",
                caller_subscribed=True,
                admitted=False,
                disposition="malformed_payload",
                outcome_code="invalid_workflow_dispatch_pr_number",
                operation=None,
                command_precedence_tier=None,
                resolution=None,
                source_identity=identity,
                requires_model_inference=False,
                reused_retained_snapshot=False,
                invalidates_retained_baseline=False,
                ref_only_base_retarget=False,
                preserved_open_finding_ids=preserved_findings,
                read_time_guard=rule.read_time_guard,
                recovery_command="workflow_dispatch",
            )
        if not isinstance(live_pull_request, Mapping):
            return EventRoutingDecision(
                rule_id=rule.rule_id,
                family=rule.family,
                event_name=normalized_event,
                action="manual",
                caller_subscribed=True,
                admitted=False,
                disposition="malformed_payload",
                outcome_code="live_pull_request_unavailable",
                operation=None,
                command_precedence_tier=None,
                resolution=None,
                source_identity=identity,
                requires_model_inference=False,
                reused_retained_snapshot=False,
                invalidates_retained_baseline=False,
                ref_only_base_retarget=False,
                preserved_open_finding_ids=preserved_findings,
                read_time_guard=rule.read_time_guard,
                recovery_command="workflow_dispatch",
            )

        state = live_pull_request.get("state")
        if state is not None and state != "open":
            return EventRoutingDecision(
                rule_id=rule.rule_id,
                family=rule.family,
                event_name=normalized_event,
                action="manual",
                caller_subscribed=True,
                admitted=False,
                disposition="rejected_guard",
                outcome_code="closed_pr_rejected_at_admission",
                operation=None,
                command_precedence_tier=None,
                resolution=None,
                source_identity=identity,
                requires_model_inference=False,
                reused_retained_snapshot=False,
                invalidates_retained_baseline=False,
                ref_only_base_retarget=False,
                preserved_open_finding_ids=preserved_findings,
                read_time_guard=rule.read_time_guard,
                recovery_command="@reviewsensei review status",
            )

        draft_ok, draft_code = _validate_exact_draft_flag(live_pull_request)
        if not draft_ok:
            assert draft_code is not None
            return EventRoutingDecision(
                rule_id=rule.rule_id,
                family=rule.family,
                event_name=normalized_event,
                action="manual",
                caller_subscribed=True,
                admitted=False,
                disposition=(
                    "explicit_noop"
                    if draft_code == "draft_skipped"
                    else "rejected_guard"
                ),
                outcome_code=draft_code,
                operation=None,
                command_precedence_tier=None,
                resolution=None,
                source_identity=identity,
                requires_model_inference=False,
                reused_retained_snapshot=False,
                invalidates_retained_baseline=False,
                ref_only_base_retarget=False,
                preserved_open_finding_ids=preserved_findings,
                read_time_guard=rule.read_time_guard,
                recovery_command="workflow_dispatch",
            )

        try:
            resolution = resolve_pull_request_event(live_pull_request)
        except ValueError:
            return EventRoutingDecision(
                rule_id=rule.rule_id,
                family=rule.family,
                event_name=normalized_event,
                action="manual",
                caller_subscribed=True,
                admitted=False,
                disposition="malformed_payload",
                outcome_code="invalid_pull_request_identity",
                operation=None,
                command_precedence_tier=None,
                resolution=None,
                source_identity=identity,
                requires_model_inference=False,
                reused_retained_snapshot=False,
                invalidates_retained_baseline=False,
                ref_only_base_retarget=False,
                preserved_open_finding_ids=preserved_findings,
                read_time_guard=rule.read_time_guard,
                recovery_command="workflow_dispatch",
            )

        if int(resolution.pull_request_number) != dispatch_pr_number:
            return EventRoutingDecision(
                rule_id=rule.rule_id,
                family=rule.family,
                event_name=normalized_event,
                action="manual",
                caller_subscribed=True,
                admitted=False,
                disposition="rejected_guard",
                outcome_code="pull_request_number_mismatch",
                operation=None,
                command_precedence_tier=None,
                resolution=None,
                source_identity=identity,
                requires_model_inference=False,
                reused_retained_snapshot=False,
                invalidates_retained_baseline=False,
                ref_only_base_retarget=False,
                preserved_open_finding_ids=preserved_findings,
                read_time_guard=rule.read_time_guard,
                recovery_command="workflow_dispatch",
            )

        review_remaining = inputs.get("review_remaining") is True
        exact_reuse, invalidates_base, ref_only = _snapshot_compatibility(
            live_pull_request, retained_state
        )
        if exact_reuse and not review_remaining:
            return EventRoutingDecision(
                rule_id=rule.rule_id,
                family=rule.family,
                event_name=normalized_event,
                action="manual",
                caller_subscribed=True,
                admitted=False,
                disposition="reuse_retained_snapshot",
                outcome_code="unchanged_snapshot_reused",
                operation=None,
                command_precedence_tier=None,
                resolution=resolution,
                source_identity=identity,
                requires_model_inference=False,
                reused_retained_snapshot=True,
                invalidates_retained_baseline=False,
                ref_only_base_retarget=False,
                preserved_open_finding_ids=preserved_findings,
                read_time_guard=rule.read_time_guard,
                recovery_command="@reviewsensei rescan",
            )

        return EventRoutingDecision(
            rule_id=rule.rule_id,
            family=rule.family,
            event_name=normalized_event,
            action="manual",
            caller_subscribed=True,
            admitted=True,
            disposition="admitted_review",
            outcome_code=(
                "remaining_review_scheduled"
                if review_remaining
                else "manual_review_scheduled"
            ),
            operation=resolution.operation,
            command_precedence_tier=None,
            resolution=resolution,
            source_identity=identity,
            requires_model_inference=True,
            reused_retained_snapshot=False,
            invalidates_retained_baseline=invalidates_base,
            ref_only_base_retarget=ref_only,
            preserved_open_finding_ids=preserved_findings,
            read_time_guard=rule.read_time_guard,
            recovery_command=rule.recovery_command,
        )

    return EventRoutingDecision(
        rule_id="unsupported.event",
        family="unsupported",
        event_name=normalized_event,
        action=action,
        caller_subscribed=False,
        admitted=False,
        disposition="unsupported",
        outcome_code="unsupported_event",
        operation=None,
        command_precedence_tier=None,
        resolution=None,
        source_identity=SourceEventIdentity(
            event_name=normalized_event,
            action=action,
            delivery_id=clean_delivery,
        ),
        requires_model_inference=False,
        reused_retained_snapshot=False,
        invalidates_retained_baseline=False,
        ref_only_base_retarget=False,
        preserved_open_finding_ids=preserved_findings,
        read_time_guard="caller_event_allowlist",
        recovery_command=None,
    )


OPERATION_LIFECYCLE_PHASES: frozenset[str] = frozenset(
    {
        "intent_accepted_unstarted",
        "execution_started",
        "uncertain_dispatch",
        "result_retained_publication_pending",
        "completed",
        "queued_replaced_before_admission",
    }
)


@dataclass(frozen=True)
class LivePullRequestTarget:
    """Authenticated live pull-request target state (never a webhook FIFO)."""

    repository_id: int
    pull_request_number: int
    base_sha: str
    base_ref: str
    head_sha: str
    head_ref: str
    state: str = "open"
    draft: bool = False

    def __post_init__(self) -> None:
        if _positive_int(self.repository_id) is None:
            raise ValueError("repository_id must be a positive integer")
        if _positive_int(self.pull_request_number) is None:
            raise ValueError("pull_request_number must be a positive integer")
        if not isinstance(self.base_sha, str) or not _GIT_SHA_FULL.fullmatch(
            self.base_sha
        ):
            raise ValueError("base_sha must be a 40-hex SHA")
        if not isinstance(self.head_sha, str) or not _GIT_SHA_FULL.fullmatch(
            self.head_sha
        ):
            raise ValueError("head_sha must be a 40-hex SHA")
        if not isinstance(self.base_ref, str) or not _GIT_REF.fullmatch(self.base_ref):
            raise ValueError("base_ref must be a valid git ref")
        if not isinstance(self.head_ref, str) or not _GIT_REF.fullmatch(self.head_ref):
            raise ValueError("head_ref must be a valid git ref")
        if type(self.draft) is not bool:
            raise ValueError("draft must be an exact boolean")

    def to_pull_mapping(self) -> dict[str, Any]:
        return {
            "number": self.pull_request_number,
            "state": self.state,
            "draft": self.draft,
            "head": {"sha": self.head_sha, "ref": self.head_ref},
            "base": {"sha": self.base_sha, "ref": self.base_ref},
        }


@dataclass(frozen=True)
class AcceptedOperationRecord:
    """Ledger-visible operation record inspected before scheduling new work."""

    operation_id: str
    operation_kind: str
    target_base_sha: str
    target_head_sha: str
    lifecycle_phase: str
    target_base_ref: str = "main"
    source_dedup_key: str | None = None
    command_text: str | None = None
    open_finding_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.operation_id, str) or not self.operation_id.strip():
            raise ValueError("operation_id must be non-empty")
        if self.operation_kind not in {"review", "command", "reply"}:
            raise ValueError("operation_kind must be review, command, or reply")
        if self.lifecycle_phase not in OPERATION_LIFECYCLE_PHASES:
            raise ValueError(f"unsupported lifecycle_phase: {self.lifecycle_phase!r}")
        if not isinstance(self.target_base_sha, str) or not _GIT_SHA_FULL.fullmatch(
            self.target_base_sha
        ):
            raise ValueError("target_base_sha must be a 40-hex SHA")
        if not isinstance(self.target_head_sha, str) or not _GIT_SHA_FULL.fullmatch(
            self.target_head_sha
        ):
            raise ValueError("target_head_sha must be a 40-hex SHA")


@dataclass(frozen=True)
class QueuedRunCandidate:
    """One candidate Actions run arriving at the per-PR business concurrency group."""

    run_id: str
    event_name: str
    operation_kind: str
    target_head_sha: str
    source_dedup_key: str | None = None
    command_text: str | None = None
    accepted_before_queue: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise ValueError("run_id must be non-empty")
        if self.operation_kind not in {"review", "command", "reply"}:
            raise ValueError("operation_kind must be review, command, or reply")
        if not isinstance(self.target_head_sha, str) or not _GIT_SHA_FULL.fullmatch(
            self.target_head_sha
        ):
            raise ValueError("target_head_sha must be a 40-hex SHA")


@dataclass(frozen=True)
class ReplacedQueueNotice:
    """Explicit operator-visible diagnostic when Actions concurrency evicts a pending run."""

    replaced_run_id: str
    replaced_by_run_id: str
    operation_kind: str
    outcome_code: str
    accepted_before_queue: bool
    recovery_command: str


@dataclass(frozen=True)
class ConcurrencySimulationResult:
    """Deterministic trace of per-PR GitHub Actions concurrency slot behavior."""

    concurrency_group: str
    active_run: QueuedRunCandidate | None
    pending_run: QueuedRunCandidate | None
    completed_run_ids: tuple[str, ...]
    replaced_notices: tuple[ReplacedQueueNotice, ...]


def simulate_actions_concurrency_queue(
    *,
    repository: str,
    pull_request_number: int,
    events: Sequence[tuple[str, QueuedRunCandidate | None]],
) -> ConcurrencySimulationResult:
    """Simulate ``reviewsensei-provider-review-<repo>-<pr>`` (``cancel-in-progress: false``).

    Each item in ``events`` is either ``("enqueue", candidate)`` or
    ``("finish_active", None)``. At most one active run and one pending run
    occupy the group; a second arrival while active is running replaces the
    pending slot and emits a ``ReplacedQueueNotice`` with an explicit recovery
    command instead of silently dropping the displaced run.
    """

    if not isinstance(repository, str) or "/" not in repository.strip("/"):
        raise ValueError("repository must be an owner/repo slug")
    if _positive_int(pull_request_number) is None:
        raise ValueError("pull_request_number must be a positive integer")

    group = f"reviewsensei-provider-review-{repository}-{pull_request_number}"
    active: QueuedRunCandidate | None = None
    pending: QueuedRunCandidate | None = None
    completed: list[str] = []
    notices: list[ReplacedQueueNotice] = []

    for kind, candidate in events:
        if kind == "enqueue":
            if candidate is None:
                raise ValueError("enqueue event requires a QueuedRunCandidate")
            if active is None:
                active = candidate
            elif pending is None:
                pending = candidate
            else:
                displaced = pending
                pending = candidate
                if displaced.operation_kind == "command":
                    code = "queued_command_replaced"
                    recovery = (
                        displaced.command_text.strip()
                        if isinstance(displaced.command_text, str)
                        and displaced.command_text.strip()
                        else "@reviewsensei verify"
                    )
                elif displaced.operation_kind == "reply":
                    code = "queued_reply_replaced"
                    recovery = "workflow_dispatch"
                else:
                    code = "queued_review_superseded"
                    recovery = (
                        "workflow_dispatch"
                        if displaced.target_head_sha != candidate.target_head_sha
                        else "@reviewsensei review status"
                    )
                notices.append(
                    ReplacedQueueNotice(
                        replaced_run_id=displaced.run_id,
                        replaced_by_run_id=candidate.run_id,
                        operation_kind=displaced.operation_kind,
                        outcome_code=code,
                        accepted_before_queue=displaced.accepted_before_queue,
                        recovery_command=recovery,
                    )
                )
        elif kind == "finish_active":
            if active is not None:
                completed.append(active.run_id)
                active = pending
                pending = None
        else:
            raise ValueError(f"unknown queue simulation step: {kind!r}")

    return ConcurrencySimulationResult(
        concurrency_group=group,
        active_run=active,
        pending_run=pending,
        completed_run_ids=tuple(completed),
        replaced_notices=tuple(notices),
    )


@dataclass(frozen=True)
class ConvergenceDecision:
    """Current-state reconciliation verdict for missed, delayed, or replaced events."""

    status: str
    outcome_code: str
    target_base_sha: str
    target_head_sha: str
    routing: EventRoutingDecision
    schedules_new_inference: bool
    schedules_native_reconciliation: bool
    reused_operation_id: str | None
    preserved_open_finding_ids: tuple[str, ...]
    recovery_command: str | None


def _collect_open_findings(
    records: Sequence[AcceptedOperationRecord],
) -> tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()
    for record in records:
        for finding_id in record.open_finding_ids:
            if finding_id not in seen:
                seen.add(finding_id)
                ordered.append(finding_id)
    return tuple(ordered)


def _is_verify_only_command(body: object) -> bool:
    if not isinstance(body, str):
        return False
    return (
        re.fullmatch(
            r"\A(?:@sensei|(?ai:@reviewsensei))[ \t]+(?i:verify)[ \t\r\n]*\Z",
            body,
        )
        is not None
    )


def reconcile_event_convergence(
    event_name: str,
    payload: Mapping[str, Any] | bytes | bytearray | str | object,
    *,
    live_target: LivePullRequestTarget,
    records: Sequence[AcceptedOperationRecord] = (),
    delivery_id: str | None = None,
    was_queued_replaced: bool = False,
) -> ConvergenceDecision:
    """Converge an event or manual recovery action onto ``live_target``.

    Enforces G13 invariants:
    - Reconciles against authenticated current PR state rather than arrival order.
    - Inspects existing accepted intents, retained results, and uncertain
      dispatches BEFORE scheduling any new model inference.
    - Reuses compatible retained work with zero duplicate inference.
    - Surfaces explicit held/superseded diagnostics and bounded recovery commands
      when queued runs were replaced in GitHub Actions concurrency.
    """

    preserved = _collect_open_findings(records)
    live_mapping = live_target.to_pull_mapping()

    completed_for_live: AcceptedOperationRecord | None = None
    retained_pending_for_live: AcceptedOperationRecord | None = None
    latest_completed: AcceptedOperationRecord | None = None
    accepted_keys: set[str] = set()

    for record in records:
        if record.source_dedup_key and record.lifecycle_phase == "completed":
            accepted_keys.add(record.source_dedup_key)
        if record.lifecycle_phase == "completed":
            latest_completed = record
            if (
                record.operation_kind == "review"
                and record.target_base_sha == live_target.base_sha
                and record.target_head_sha == live_target.head_sha
            ):
                completed_for_live = record
        elif (
            record.lifecycle_phase == "result_retained_publication_pending"
            and record.operation_kind == "review"
            and record.target_base_sha == live_target.base_sha
            and record.target_head_sha == live_target.head_sha
        ):
            retained_pending_for_live = record

    retained_state: RetainedSnapshotState | None = None
    if completed_for_live is not None:
        retained_state = RetainedSnapshotState(
            reviewed_base_sha=completed_for_live.target_base_sha,
            reviewed_base_ref=completed_for_live.target_base_ref,
            reviewed_head_sha=completed_for_live.target_head_sha,
            review_status="complete",
            open_finding_ids=preserved,
            accepted_source_keys=frozenset(accepted_keys),
        )
    elif latest_completed is not None:
        retained_state = RetainedSnapshotState(
            reviewed_base_sha=latest_completed.target_base_sha,
            reviewed_base_ref=latest_completed.target_base_ref,
            reviewed_head_sha=latest_completed.target_head_sha,
            review_status="complete",
            open_finding_ids=preserved,
            accepted_source_keys=frozenset(accepted_keys),
        )
    elif preserved or accepted_keys:
        retained_state = RetainedSnapshotState(
            reviewed_base_sha=live_target.base_sha,
            reviewed_base_ref=live_target.base_ref,
            reviewed_head_sha=live_target.head_sha,
            review_status="incomplete",
            open_finding_ids=preserved,
            accepted_source_keys=frozenset(accepted_keys),
        )

    parsed = _coerce_payload_mapping(payload)
    routing = evaluate_github_event(
        event_name,
        payload,
        delivery_id=delivery_id,
        live_pull_request=live_mapping,
        retained_state=retained_state,
    )

    if was_queued_replaced:
        is_cmd = routing.operation == "command"
        comment_map = _as_mapping(parsed.get("comment")) if parsed is not None else {}
        cmd_body = _opt_str(comment_map.get("body"))
        recovery = (
            cmd_body.strip()
            if is_cmd and cmd_body and cmd_body.strip()
            else ("@reviewsensei verify" if is_cmd else "workflow_dispatch")
        )
        return ConvergenceDecision(
            status="held_queued_replaced",
            outcome_code=(
                "queued_command_replaced"
                if is_cmd
                else "queued_run_superseded_before_admission"
            ),
            target_base_sha=live_target.base_sha,
            target_head_sha=live_target.head_sha,
            routing=routing,
            schedules_new_inference=False,
            schedules_native_reconciliation=False,
            reused_operation_id=None,
            preserved_open_finding_ids=preserved,
            recovery_command=recovery,
        )

    for record in records:
        if record.lifecycle_phase == "uncertain_dispatch":
            return ConvergenceDecision(
                status="held_uncertain_operation",
                outcome_code="uncertain_dispatch_reconciliation_required",
                target_base_sha=live_target.base_sha,
                target_head_sha=live_target.head_sha,
                routing=routing,
                schedules_new_inference=False,
                schedules_native_reconciliation=False,
                reused_operation_id=record.operation_id,
                preserved_open_finding_ids=preserved,
                recovery_command="@reviewsensei review status",
            )

    if event_name.strip() == "pull_request" and parsed is not None:
        event_pull = _as_mapping(parsed.get("pull_request"))
        event_head = _as_mapping(event_pull.get("head"))
        event_base = _as_mapping(event_pull.get("base"))
        payload_head_sha = _opt_str(event_head.get("sha"))
        payload_base_sha = _opt_str(event_base.get("sha"))
        if (
            payload_head_sha is not None
            and _GIT_SHA_FULL.fullmatch(payload_head_sha)
            and payload_head_sha != live_target.head_sha
        ) or (
            payload_base_sha is not None
            and _GIT_SHA_FULL.fullmatch(payload_base_sha)
            and payload_base_sha != live_target.base_sha
        ):
            return ConvergenceDecision(
                status="ignored_stale_event",
                outcome_code="delayed_out_of_order_head_superseded",
                target_base_sha=live_target.base_sha,
                target_head_sha=live_target.head_sha,
                routing=routing,
                schedules_new_inference=False,
                schedules_native_reconciliation=False,
                reused_operation_id=(
                    completed_for_live.operation_id
                    if completed_for_live is not None
                    else None
                ),
                preserved_open_finding_ids=preserved,
                recovery_command=(
                    None if completed_for_live is not None else "workflow_dispatch"
                ),
            )

    incoming_key = routing.source_identity.source_dedup_key
    if incoming_key is not None:
        for record in records:
            if record.source_dedup_key != incoming_key:
                continue
            if record.lifecycle_phase == "completed":
                return ConvergenceDecision(
                    status="converged_reused",
                    outcome_code="duplicate_delivery_reused",
                    target_base_sha=live_target.base_sha,
                    target_head_sha=live_target.head_sha,
                    routing=routing,
                    schedules_new_inference=False,
                    schedules_native_reconciliation=False,
                    reused_operation_id=record.operation_id,
                    preserved_open_finding_ids=preserved,
                    recovery_command=None,
                )
            if record.lifecycle_phase == "result_retained_publication_pending":
                return ConvergenceDecision(
                    status="reconcile_retained_publication",
                    outcome_code="retained_result_reused_without_inference",
                    target_base_sha=live_target.base_sha,
                    target_head_sha=live_target.head_sha,
                    routing=routing,
                    schedules_new_inference=False,
                    schedules_native_reconciliation=True,
                    reused_operation_id=record.operation_id,
                    preserved_open_finding_ids=preserved,
                    recovery_command=None,
                )
            if record.lifecycle_phase == "intent_accepted_unstarted":
                if (
                    record.target_base_sha == live_target.base_sha
                    and record.target_head_sha == live_target.head_sha
                ):
                    needs_inf = record.operation_kind in {"review", "reply"}
                    return ConvergenceDecision(
                        status="resume_accepted_intent",
                        outcome_code="unstarted_accepted_intent_resumed",
                        target_base_sha=live_target.base_sha,
                        target_head_sha=live_target.head_sha,
                        routing=routing,
                        schedules_new_inference=needs_inf,
                        schedules_native_reconciliation=not needs_inf,
                        reused_operation_id=record.operation_id,
                        preserved_open_finding_ids=preserved,
                        recovery_command=None,
                    )
                return ConvergenceDecision(
                    status="held_intent_superseded",
                    outcome_code="accepted_intent_head_drifted",
                    target_base_sha=live_target.base_sha,
                    target_head_sha=live_target.head_sha,
                    routing=routing,
                    schedules_new_inference=False,
                    schedules_native_reconciliation=False,
                    reused_operation_id=record.operation_id,
                    preserved_open_finding_ids=preserved,
                    recovery_command=(
                        record.command_text.strip()
                        if isinstance(record.command_text, str)
                        and record.command_text.strip()
                        else "workflow_dispatch"
                    ),
                )

    for record in records:
        if (
            record.lifecycle_phase == "execution_started"
            and record.target_base_sha == live_target.base_sha
            and record.target_head_sha == live_target.head_sha
        ):
            return ConvergenceDecision(
                status="held_active_writer",
                outcome_code="active_operation_in_progress",
                target_base_sha=live_target.base_sha,
                target_head_sha=live_target.head_sha,
                routing=routing,
                schedules_new_inference=False,
                schedules_native_reconciliation=False,
                reused_operation_id=record.operation_id,
                preserved_open_finding_ids=preserved,
                recovery_command="@reviewsensei review status",
            )

    if retained_pending_for_live is not None and (
        routing.admitted or routing.reused_retained_snapshot
    ):
        return ConvergenceDecision(
            status="reconcile_retained_publication",
            outcome_code="retained_result_reused_without_inference",
            target_base_sha=live_target.base_sha,
            target_head_sha=live_target.head_sha,
            routing=routing,
            schedules_new_inference=False,
            schedules_native_reconciliation=True,
            reused_operation_id=retained_pending_for_live.operation_id,
            preserved_open_finding_ids=preserved,
            recovery_command=None,
        )

    if routing.reused_retained_snapshot:
        return ConvergenceDecision(
            status="converged_reused",
            outcome_code=routing.outcome_code,
            target_base_sha=live_target.base_sha,
            target_head_sha=live_target.head_sha,
            routing=routing,
            schedules_new_inference=False,
            schedules_native_reconciliation=False,
            reused_operation_id=(
                completed_for_live.operation_id
                if completed_for_live is not None
                else None
            ),
            preserved_open_finding_ids=preserved,
            recovery_command=routing.recovery_command,
        )

    if routing.admitted and routing.operation == "command":
        comment_map = _as_mapping(parsed.get("comment")) if parsed is not None else {}
        body_str = _opt_str(comment_map.get("body"))
        if _is_verify_only_command(body_str) and completed_for_live is None:
            return ConvergenceDecision(
                status="held_unreviewed_live_head",
                outcome_code="verify_requires_reviewed_live_snapshot",
                target_base_sha=live_target.base_sha,
                target_head_sha=live_target.head_sha,
                routing=routing,
                schedules_new_inference=False,
                schedules_native_reconciliation=False,
                reused_operation_id=None,
                preserved_open_finding_ids=preserved,
                recovery_command="workflow_dispatch",
            )
        return ConvergenceDecision(
            status="schedule_command_reconciliation",
            outcome_code="command_reconciles_current_target",
            target_base_sha=live_target.base_sha,
            target_head_sha=live_target.head_sha,
            routing=routing,
            schedules_new_inference=False,
            schedules_native_reconciliation=True,
            reused_operation_id=(
                completed_for_live.operation_id
                if completed_for_live is not None
                else None
            ),
            preserved_open_finding_ids=preserved,
            recovery_command=None,
        )

    if routing.admitted and routing.operation == "review":
        had_prior_different_head = (
            latest_completed is not None
            and latest_completed.target_head_sha != live_target.head_sha
        )
        return ConvergenceDecision(
            status="schedule_current_target_review",
            outcome_code=(
                "missed_or_advanced_head_converged"
                if had_prior_different_head
                else routing.outcome_code
            ),
            target_base_sha=live_target.base_sha,
            target_head_sha=live_target.head_sha,
            routing=routing,
            schedules_new_inference=True,
            schedules_native_reconciliation=False,
            reused_operation_id=None,
            preserved_open_finding_ids=preserved,
            recovery_command=None,
        )

    if routing.admitted and routing.operation == "reply":
        return ConvergenceDecision(
            status="schedule_reply",
            outcome_code=routing.outcome_code,
            target_base_sha=live_target.base_sha,
            target_head_sha=live_target.head_sha,
            routing=routing,
            schedules_new_inference=True,
            schedules_native_reconciliation=False,
            reused_operation_id=None,
            preserved_open_finding_ids=preserved,
            recovery_command=None,
        )

    return ConvergenceDecision(
        status="not_admitted",
        outcome_code=routing.outcome_code,
        target_base_sha=live_target.base_sha,
        target_head_sha=live_target.head_sha,
        routing=routing,
        schedules_new_inference=False,
        schedules_native_reconciliation=False,
        reused_operation_id=None,
        preserved_open_finding_ids=preserved,
        recovery_command=routing.recovery_command,
    )


MERGE_QUEUE_REF_PREFIXES: tuple[str, ...] = (
    "gh-readonly-queue/",
    "refs/heads/gh-readonly-queue/",
)


def is_merge_queue_ref(ref: object) -> bool:
    """Return whether ``ref`` names a GitHub merge-queue temporary branch."""

    if not isinstance(ref, str):
        return False
    normalized = ref.strip()
    return any(normalized.startswith(prefix) for prefix in MERGE_QUEUE_REF_PREFIXES)


@dataclass(frozen=True)
class MergeGroupRejectionOutcome:
    """Typed, zero-side-effect refusal record for unsupported ``merge_group`` events."""

    outcome_code: str
    merge_group_action: str
    queue_head_sha: str | None
    queue_base_sha: str | None
    queue_head_ref: str | None
    token_acquired: bool = False
    provider_called: bool = False
    ledger_mutated: bool = False
    check_published: bool = False


def reject_merge_group_event(
    payload: Mapping[str, Any] | bytes | bytearray | str | object,
    *,
    delivery_id: str | None = None,
) -> MergeGroupRejectionOutcome:
    """Deterministically reject any ``merge_group`` payload before side effects."""

    parsed = _coerce_payload_mapping(payload)
    routing = evaluate_github_event(
        "merge_group",
        payload if parsed is not None else {},
        delivery_id=delivery_id,
    )
    mg = _as_mapping(parsed.get("merge_group")) if parsed is not None else {}
    raw_action = _opt_str(parsed.get("action")) if parsed is not None else None
    action = raw_action.strip() if raw_action and raw_action.strip() else "<missing>"
    return MergeGroupRejectionOutcome(
        outcome_code=routing.outcome_code,
        merge_group_action=action,
        queue_head_sha=_opt_str(mg.get("head_sha")),
        queue_base_sha=_opt_str(mg.get("base_sha")),
        queue_head_ref=_opt_str(mg.get("head_ref")),
        token_acquired=False,
        provider_called=False,
        ledger_mutated=False,
        check_published=False,
    )


@dataclass(frozen=True)
class ReviewCertificationReceipt:
    """Authoritative PR-bound check/approval receipt for one exact snapshot."""

    repository_id: int
    pull_request_number: int
    reviewed_base_sha: str
    reviewed_head_sha: str
    result_digest: str
    approved: bool
    check_conclusion: str
    check_name: str = REVIEW_CHECK_NAME

    def __post_init__(self) -> None:
        if _positive_int(self.repository_id) is None:
            raise ValueError("repository_id must be a positive integer")
        if _positive_int(self.pull_request_number) is None:
            raise ValueError("pull_request_number must be a positive integer")
        if not isinstance(self.reviewed_base_sha, str) or not _GIT_SHA_FULL.fullmatch(
            self.reviewed_base_sha
        ):
            raise ValueError("reviewed_base_sha must be a 40-hex SHA")
        if not isinstance(self.reviewed_head_sha, str) or not _GIT_SHA_FULL.fullmatch(
            self.reviewed_head_sha
        ):
            raise ValueError("reviewed_head_sha must be a 40-hex SHA")
        if (
            not isinstance(self.result_digest, str)
            or re.fullmatch(r"[a-f0-9]{64}", self.result_digest) is None
        ):
            raise ValueError("result_digest must be a 64-hex SHA-256 digest")


@dataclass(frozen=True)
class CertificationCandidate:
    """Candidate commit/group asking whether a ReviewSensei PR receipt certifies it."""

    repository_id: int
    base_sha: str
    head_sha: str
    head_ref: str
    pull_request_numbers: tuple[int, ...]
    is_merge_group: bool = False


@dataclass(frozen=True)
class SnapshotCertificationVerdict:
    """Verdict proving whether ``receipt`` certifies ``candidate``."""

    certified: bool
    outcome_code: str
    check_name: str
    reason: str


def verify_snapshot_certification(
    receipt: ReviewCertificationReceipt,
    candidate: CertificationCandidate,
) -> SnapshotCertificationVerdict:
    """Bind certification strictly to one PR's exact ``(base_sha, head_sha)`` pair.

    Ordinary PR-head approvals and ``ReviewSensei`` check runs NEVER certify a
    synthetic merge-queue commit SHA, a ``gh-readonly-queue/*`` ref, an advanced
    queue base SHA, or a multi-PR merge group.
    """

    if candidate.is_merge_group or is_merge_queue_ref(candidate.head_ref):
        return SnapshotCertificationVerdict(
            certified=False,
            outcome_code="merge_group_unsupported",
            check_name=receipt.check_name,
            reason="ordinary pull-request receipts cannot certify merge-queue refs or merge_group commits",
        )
    if candidate.repository_id != receipt.repository_id:
        return SnapshotCertificationVerdict(
            certified=False,
            outcome_code="repository_mismatch",
            check_name=receipt.check_name,
            reason="candidate repository_id does not match certified receipt",
        )
    if (
        len(candidate.pull_request_numbers) != 1
        or candidate.pull_request_numbers[0] != receipt.pull_request_number
    ):
        return SnapshotCertificationVerdict(
            certified=False,
            outcome_code="multi_pr_or_mismatched_pr_unauthenticated",
            check_name=receipt.check_name,
            reason="certification applies to exactly one matching pull_request_number",
        )
    if candidate.head_sha != receipt.reviewed_head_sha:
        return SnapshotCertificationVerdict(
            certified=False,
            outcome_code="head_sha_mismatch_uncertified",
            check_name=receipt.check_name,
            reason="candidate head_sha differs from reviewed_head_sha",
        )
    if candidate.base_sha != receipt.reviewed_base_sha:
        return SnapshotCertificationVerdict(
            certified=False,
            outcome_code="base_sha_mismatch_uncertified",
            check_name=receipt.check_name,
            reason="candidate base_sha differs from reviewed_base_sha",
        )
    if not receipt.approved or receipt.check_conclusion != "success":
        return SnapshotCertificationVerdict(
            certified=False,
            outcome_code="receipt_not_certifying",
            check_name=receipt.check_name,
            reason="receipt did not grant approval and passing check conclusion",
        )
    return SnapshotCertificationVerdict(
        certified=True,
        outcome_code="exact_pr_snapshot_certified",
        check_name=receipt.check_name,
        reason="exact repository, pull_request_number, base_sha, and head_sha match",
    )


@dataclass(frozen=True)
class MergeQueueEvictionOutcome:
    """State of the underlying PR after removal/eviction from a GitHub merge queue."""

    outcome_code: str
    pr_certification_intact: bool
    requires_new_review: bool
    preserved_open_finding_ids: tuple[str, ...]
    recovery_command: str | None


def reconcile_merge_queue_eviction(
    *,
    destroyed_payload: Mapping[str, Any] | bytes | bytearray | str | object,
    live_pr_target: LivePullRequestTarget,
    pr_receipt: ReviewCertificationReceipt | None,
    open_finding_ids: tuple[str, ...] = (),
) -> MergeQueueEvictionOutcome:
    """Verify PR-head state and recovery remain unaffected after queue eviction."""

    rejection = reject_merge_group_event(destroyed_payload)
    assert rejection.outcome_code == "merge_group_unsupported"

    if pr_receipt is None:
        return MergeQueueEvictionOutcome(
            outcome_code="pr_unreviewed_after_queue_eviction",
            pr_certification_intact=False,
            requires_new_review=True,
            preserved_open_finding_ids=open_finding_ids,
            recovery_command="workflow_dispatch",
        )

    pr_candidate = CertificationCandidate(
        repository_id=live_pr_target.repository_id,
        base_sha=live_pr_target.base_sha,
        head_sha=live_pr_target.head_sha,
        head_ref=live_pr_target.head_ref,
        pull_request_numbers=(live_pr_target.pull_request_number,),
        is_merge_group=False,
    )
    verdict = verify_snapshot_certification(pr_receipt, pr_candidate)
    if verdict.certified and not open_finding_ids:
        return MergeQueueEvictionOutcome(
            outcome_code="pr_head_certification_intact_after_queue_eviction",
            pr_certification_intact=True,
            requires_new_review=False,
            preserved_open_finding_ids=(),
            recovery_command=None,
        )
    if (
        live_pr_target.base_sha == pr_receipt.reviewed_base_sha
        and live_pr_target.head_sha == pr_receipt.reviewed_head_sha
    ):
        return MergeQueueEvictionOutcome(
            outcome_code="pr_head_obligations_preserved_after_queue_eviction",
            pr_certification_intact=False,
            requires_new_review=False,
            preserved_open_finding_ids=open_finding_ids,
            recovery_command="@reviewsensei verify",
        )
    return MergeQueueEvictionOutcome(
        outcome_code="pr_head_advanced_after_queue_eviction_requires_review",
        pr_certification_intact=False,
        requires_new_review=True,
        preserved_open_finding_ids=open_finding_ids,
        recovery_command="workflow_dispatch",
    )


@dataclass(frozen=True)
class ManualThreadFindingState:
    """Authoritative finding obligation paired with observed native thread state."""

    finding_id: str
    instance_fingerprint: str
    blocking: bool
    needs_human: bool
    disposition_proven: bool
    thread_id: str | None
    thread_base_sha: str | None
    thread_head_sha: str | None
    native_thread_resolved: bool

    def __post_init__(self) -> None:
        if not isinstance(self.finding_id, str) or not self.finding_id.strip():
            raise ValueError("finding_id must be non-empty")
        if (
            not isinstance(self.instance_fingerprint, str)
            or re.fullmatch(r"[a-f0-9]{64}", self.instance_fingerprint) is None
        ):
            raise ValueError("instance_fingerprint must be a 64-hex SHA-256 digest")


@dataclass(frozen=True)
class ManualThreadConvergenceOutcome:
    """Bounded ``@reviewsensei verify`` outcome for G26 thread reconciliation."""

    status: str
    outcome_code: str
    approved: bool
    approval_decision: AutoApprovalDecision
    withheld_diagnostic: str | None
    requires_model_inference: bool
    finite_api_reads_charged: int
    threads_observed_already_resolved: tuple[str, ...]
    threads_reconciled_by_mutation: tuple[str, ...]
    unconfirmed_instances: tuple[str, ...]
    preserved_open_finding_ids: tuple[str, ...]
    recovery_command: str | None


def reconcile_manual_thread_convergence(
    *,
    live_target: LivePullRequestTarget,
    eligibility: ReviewApprovalEligibility,
    reviewed_base_sha: str,
    findings: Sequence[ManualThreadFindingState],
    thread_resolution_result: ThreadResolutionResult | None = None,
    finite_api_reads_charged: int = 1,
    app_authored: bool = False,
) -> ManualThreadConvergenceOutcome:
    """Evaluate bounded ``@reviewsensei verify`` convergence over native threads.

    Enforces G26 invariants:
    - Clicking GitHub's native "Resolve conversation" never clears an unfixed or
      unoverridden ReviewSensei finding obligation and never manufactures native
      approval.
    - Explicit ``@reviewsensei verify`` runs with zero model inference, charges
      finite GitHub API reads/mutations normally, idempotently observes already-
      resolved decided threads, resolves reopened decided threads under exact
      ``(base_sha, head_sha)`` authority, refuses stale/changed snapshots, and
      converges cleanly to approval once legitimate fixes or overrides exist.
    """

    if not isinstance(reviewed_base_sha, str) or not _GIT_SHA_FULL.fullmatch(
        reviewed_base_sha
    ):
        raise ValueError("reviewed_base_sha must be a 40-hex SHA")
    if (
        isinstance(finite_api_reads_charged, bool)
        or not isinstance(finite_api_reads_charged, int)
        or finite_api_reads_charged < 0
    ):
        raise ValueError("finite_api_reads_charged must be a non-negative integer")

    open_obligations = tuple(
        item.finding_id
        for item in findings
        if (item.blocking or item.needs_human) and not item.disposition_proven
    )

    if (
        live_target.head_sha != eligibility.head_sha
        or live_target.base_sha != reviewed_base_sha
    ):
        decision = evaluate_approval_facts(
            replace(
                eligibility.facts,
                app_authored=app_authored,
                has_open_review_threads=None,
            )
        )
        return ManualThreadConvergenceOutcome(
            status="held_head_or_base_changed",
            outcome_code="verify_head_or_base_changed",
            approved=False,
            approval_decision=decision,
            withheld_diagnostic=approval_withheld_diagnostic(decision),
            requires_model_inference=False,
            finite_api_reads_charged=finite_api_reads_charged,
            threads_observed_already_resolved=(),
            threads_reconciled_by_mutation=(),
            unconfirmed_instances=(),
            preserved_open_finding_ids=open_obligations,
            recovery_command="workflow_dispatch",
        )

    stale_or_missing: list[str] = []
    for item in findings:
        if (
            item.thread_id is None
            or not item.thread_id.strip()
            or item.thread_base_sha != live_target.base_sha
            or item.thread_head_sha != live_target.head_sha
        ):
            stale_or_missing.append(item.instance_fingerprint)

    resolver_unconfirmed = (
        thread_resolution_result.unconfirmed
        if thread_resolution_result is not None
        else ()
    )
    unconfirmed = tuple(dict.fromkeys((*stale_or_missing, *resolver_unconfirmed)))
    if unconfirmed:
        decision = evaluate_approval_facts(
            replace(
                eligibility.facts,
                app_authored=app_authored,
                has_blocking_findings=any(
                    item.blocking and not item.disposition_proven for item in findings
                ),
                has_human_adjudication_findings=any(
                    item.needs_human and not item.disposition_proven
                    for item in findings
                ),
                has_open_review_threads=None,
            )
        )
        return ManualThreadConvergenceOutcome(
            status="held_stale_or_unconfirmed_thread_mapping",
            outcome_code="stale_or_unconfirmed_thread_mapping",
            approved=False,
            approval_decision=decision,
            withheld_diagnostic=approval_withheld_diagnostic(decision),
            requires_model_inference=False,
            finite_api_reads_charged=finite_api_reads_charged,
            threads_observed_already_resolved=(),
            threads_reconciled_by_mutation=(),
            unconfirmed_instances=unconfirmed,
            preserved_open_finding_ids=open_obligations,
            recovery_command="@reviewsensei verify",
        )

    resolved_by_resolver = set(
        thread_resolution_result.resolved
        if thread_resolution_result is not None
        else ()
    )
    observed_already_resolved: list[str] = []
    reconciled_by_mutation: list[str] = []
    any_blocking_thread_still_open = False

    for item in findings:
        is_now_resolved = item.native_thread_resolved or (
            item.disposition_proven
            and item.instance_fingerprint in resolved_by_resolver
        )
        if item.disposition_proven:
            if item.native_thread_resolved:
                observed_already_resolved.append(item.instance_fingerprint)
            elif item.instance_fingerprint in resolved_by_resolver:
                reconciled_by_mutation.append(item.instance_fingerprint)
        if (item.blocking or item.needs_human) and not is_now_resolved:
            any_blocking_thread_still_open = True

    durable_blocking_open = any(
        item.blocking and not item.disposition_proven for item in findings
    )
    durable_human_open = any(
        item.needs_human and not item.disposition_proven for item in findings
    )

    updated_facts = replace(
        eligibility.facts,
        app_authored=app_authored,
        has_blocking_findings=durable_blocking_open,
        has_human_adjudication_findings=durable_human_open,
        has_open_review_threads=any_blocking_thread_still_open,
    )
    decision = evaluate_approval_facts(updated_facts)
    if decision.approved:
        return ManualThreadConvergenceOutcome(
            status="converged_approved",
            outcome_code="verify_converged_to_approval",
            approved=True,
            approval_decision=decision,
            withheld_diagnostic=None,
            requires_model_inference=False,
            finite_api_reads_charged=finite_api_reads_charged,
            threads_observed_already_resolved=tuple(observed_already_resolved),
            threads_reconciled_by_mutation=tuple(reconciled_by_mutation),
            unconfirmed_instances=(),
            preserved_open_finding_ids=(),
            recovery_command=None,
        )

    manually_resolved_unfixed = any(
        (item.blocking or item.needs_human)
        and not item.disposition_proven
        and item.native_thread_resolved
        for item in findings
    )
    code = (
        "manual_thread_resolve_withheld_unfixed_obligation"
        if manually_resolved_unfixed
        else "reopened_decided_thread_requires_verify_resolution"
        if not durable_blocking_open
        and not durable_human_open
        and any_blocking_thread_still_open
        else "verify_withheld_open_obligations"
    )
    return ManualThreadConvergenceOutcome(
        status="withheld",
        outcome_code=code,
        approved=False,
        approval_decision=decision,
        withheld_diagnostic=approval_withheld_diagnostic(decision),
        requires_model_inference=False,
        finite_api_reads_charged=finite_api_reads_charged,
        threads_observed_already_resolved=tuple(observed_already_resolved),
        threads_reconciled_by_mutation=tuple(reconciled_by_mutation),
        unconfirmed_instances=(),
        preserved_open_finding_ids=open_obligations,
        recovery_command="@reviewsensei verify",
    )


RECOVERY_COMMANDS_UNBLOCKED_BY_HUMAN_REVIEW: tuple[str, ...] = (
    "@reviewsensei verify",
    "@reviewsensei rescan",
    "@reviewsensei review remaining",
    "workflow_dispatch",
)


@dataclass(frozen=True)
class HumanReviewIndependenceOutcome:
    """Proof that native human ``pull_request_review`` events are orthogonal to ReviewSensei."""

    outcome_code: str
    branch_protection_owner: str
    review_action: str
    human_review_state: str
    routing: EventRoutingDecision
    ledger_generation_before: int
    ledger_generation_after: int
    ledger_mutated: bool
    requires_model_inference: bool
    accepted_resolution_records_from_review: int
    sensei_approved: bool
    sensei_check_conclusion: str
    stale_snapshot_unlocked: bool
    actor_can_invoke_sensei_commands: bool
    preserved_open_finding_ids: tuple[str, ...]
    recovery_commands_available: tuple[str, ...]


def evaluate_human_pr_review_independence(
    review_payload: Mapping[str, Any] | bytes | bytearray | str | object,
    *,
    live_target: LivePullRequestTarget,
    app_slug: str,
    eligibility: ReviewApprovalEligibility,
    reviewed_base_sha: str,
    policy_mode: str = "auto-approve",
    ledger_generation: int = 1,
    open_finding_ids: tuple[str, ...] = (),
    has_open_sensei_threads: bool = False,
    current_actor_association: str | None = None,
    delivery_id: str | None = None,
) -> HumanReviewIndependenceOutcome:
    """Evaluate a human ``pull_request_review`` event under G28 independence rules.

    Leaves human review gates strictly to GitHub branch protection and proves:
    - zero mutation to ReviewSensei ledger generation or open finding obligations;
    - zero authority from forged markers inside human review bodies;
    - zero substitution for ReviewSensei coverage/completion or stale snapshots;
    - zero blocking of ReviewSensei's own recovery commands (``verify``,
      ``rescan``, ``review remaining``, ``workflow_dispatch``), while enforcing
      live maintainer association when an actor loses repository privilege.
    """

    if not isinstance(app_slug, str) or not app_slug.strip():
        raise ValueError("app_slug must be a non-empty string")
    if not isinstance(reviewed_base_sha, str) or not _GIT_SHA_FULL.fullmatch(
        reviewed_base_sha
    ):
        raise ValueError("reviewed_base_sha must be a 40-hex SHA")
    if (
        isinstance(ledger_generation, bool)
        or not isinstance(ledger_generation, int)
        or ledger_generation < 0
    ):
        raise ValueError("ledger_generation must be a non-negative integer")

    normalized_review_status = (
        eligibility.facts.review_status
        if eligibility.facts.review_status
        in {"complete", "partial", "incomplete", "summary-only"}
        else "incomplete"
    )
    retained = RetainedSnapshotState(
        reviewed_base_sha=reviewed_base_sha,
        reviewed_base_ref=live_target.base_ref,
        reviewed_head_sha=eligibility.head_sha,
        review_status=normalized_review_status,
        open_finding_ids=open_finding_ids,
    )
    parsed = _coerce_payload_mapping(review_payload)
    routing = evaluate_github_event(
        "pull_request_review",
        review_payload if parsed is not None else {},
        delivery_id=delivery_id,
        live_pull_request=live_target.to_pull_mapping(),
        retained_state=retained,
    )
    review_map = _as_mapping(parsed.get("review")) if parsed is not None else {}
    raw_state = _opt_str(review_map.get("state"))
    human_state = (
        raw_state.strip().lower() if raw_state and raw_state.strip() else "unknown"
    )
    reviewer_map = _as_mapping(review_map.get("user"))
    reviewer_login = _opt_str(reviewer_map.get("login")) or ""
    reviewer_type = _opt_str(reviewer_map.get("type")) or "User"

    if reviewer_login.casefold() == app_slug.strip().casefold():
        raise ValueError(
            "evaluate_human_pr_review_independence requires a non-App review author"
        )

    # Even if a human review body embeds a forged resolution marker,
    # resolution_records ignores every review not authored by app_slug.
    accepted_records = resolution_records([dict(review_map)], app_slug.strip())

    snapshot_matches_live = (
        eligibility.head_sha == live_target.head_sha
        and reviewed_base_sha == live_target.base_sha
    )
    if snapshot_matches_live:
        approval_decision = eligibility.evaluate(
            app_authored=eligibility.facts.app_authored,
            has_open_review_threads=has_open_sensei_threads,
        )
        sensei_approved = approval_decision.approved
        check_outcome = review_check_outcome(
            policy=policy_mode,
            review_status=normalized_review_status,
            required_fixes=(
                eligibility.facts.has_blocking_findings or has_open_sensei_threads
            ),
            required_decisions=eligibility.facts.has_human_adjudication_findings,
            review_verdict=eligibility.facts.review_verdict,
        )
        sensei_conclusion = check_outcome.conclusion
    else:
        sensei_approved = False
        sensei_conclusion = "failure" if policy_mode != "advisory" else "neutral"

    effective_association = (
        current_actor_association
        if current_actor_association is not None
        else routing.source_identity.author_association
    )
    actor_can_command = (
        reviewer_type == "User"
        and effective_association in ALLOWED_COMMENT_ASSOCIATIONS
    )

    return HumanReviewIndependenceOutcome(
        outcome_code="human_review_independent_noop",
        branch_protection_owner="github_branch_protection",
        review_action=routing.action,
        human_review_state=human_state,
        routing=routing,
        ledger_generation_before=ledger_generation,
        ledger_generation_after=ledger_generation,
        ledger_mutated=False,
        requires_model_inference=False,
        accepted_resolution_records_from_review=len(accepted_records),
        sensei_approved=sensei_approved,
        sensei_check_conclusion=sensei_conclusion,
        stale_snapshot_unlocked=False,
        actor_can_invoke_sensei_commands=actor_can_command,
        preserved_open_finding_ids=open_finding_ids,
        recovery_commands_available=RECOVERY_COMMANDS_UNBLOCKED_BY_HUMAN_REVIEW,
    )


__all__ = [
    "ALLOWED_COMMENT_ASSOCIATIONS",
    "AcceptedOperationRecord",
    "CALLER_SUBSCRIBED_EVENTS",
    "CallerSubscriptionReport",
    "CertificationCandidate",
    "ConcurrencySimulationResult",
    "ConvergenceDecision",
    "EVENT_DECISION_BY_ID",
    "EVENT_DECISION_TABLE",
    "EventDecisionRule",
    "EventRoutingDecision",
    "HumanReviewIndependenceOutcome",
    "LivePullRequestTarget",
    "MERGE_QUEUE_REF_PREFIXES",
    "ManualThreadConvergenceOutcome",
    "ManualThreadFindingState",
    "MergeGroupRejectionOutcome",
    "MergeQueueEvictionOutcome",
    "OPERATION_LIFECYCLE_PHASES",
    "QueuedRunCandidate",
    "RECOVERY_COMMANDS_UNBLOCKED_BY_HUMAN_REVIEW",
    "REQUIRED_EVENT_FAMILIES",
    "ReplacedQueueNotice",
    "RetainedSnapshotState",
    "ReviewCertificationReceipt",
    "SnapshotCertificationVerdict",
    "SourceEventIdentity",
    "decision_rule_for",
    "evaluate_github_event",
    "evaluate_human_pr_review_independence",
    "inspect_caller_subscription_contract",
    "is_merge_queue_ref",
    "reconcile_event_convergence",
    "reconcile_manual_thread_convergence",
    "reconcile_merge_queue_eviction",
    "reject_merge_group_event",
    "simulate_actions_concurrency_queue",
    "verify_snapshot_certification",
]

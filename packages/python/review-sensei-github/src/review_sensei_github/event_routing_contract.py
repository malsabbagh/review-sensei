"""Explicit GitHub event routing decision table and admission guards (G01).

Separates caller workflow subscriptions from runtime admission guards so every
subscribed, unsubscribed, malformed, or duplicate GitHub event resolves to an
explicit, bounded, zero-surprise outcome without adding background polling or
unapproved automatic model spend.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

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


__all__ = [
    "ALLOWED_COMMENT_ASSOCIATIONS",
    "CALLER_SUBSCRIBED_EVENTS",
    "CallerSubscriptionReport",
    "EVENT_DECISION_BY_ID",
    "EVENT_DECISION_TABLE",
    "EventDecisionRule",
    "EventRoutingDecision",
    "REQUIRED_EVENT_FAMILIES",
    "RetainedSnapshotState",
    "SourceEventIdentity",
    "decision_rule_for",
    "evaluate_github_event",
    "inspect_caller_subscription_contract",
]

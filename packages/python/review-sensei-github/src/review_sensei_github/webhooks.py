"""GitHub App webhook signature verification and delivery safety."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from review_sensei.api_work import ApiRunAccounting
from review_sensei.session import SessionIdentity, SessionRecord

from .errors import (
    GitHubMergeGroupUnsupportedError,
    GitHubWebhookError,
    GitHubWebhookSignatureError,
)
from .source_comment_lifecycle import (
    SourceLifecycleClassification,
    SourceRevisionObservation,
    admit_event_comment_source,
    build_source_comment_descriptor,
    classify_source_comment_lifecycle,
    execute_source_lifecycle_route,
)

if TYPE_CHECKING:
    from .api_run_requests import ApiRunRequestTransport
    from .event_routing_contract import (
        AcceptedOperationRecord,
        ConvergenceDecision,
        EventRoutingDecision,
        LivePullRequestTarget,
        RetainedSnapshotState,
    )
    from .session_ledger import GitHubIssueCommentSessionLedger

MAX_WEBHOOK_BODY_BYTES = 1024 * 1024
SUPPORTED_WEBHOOK_EVENTS = frozenset(
    {
        "installation",
        "installation_repositories",
    }
)
COMMENT_LIFECYCLE_WEBHOOK_EVENTS = frozenset(
    {
        "issue_comment",
        "pull_request_review_comment",
    }
)
COMMENT_LIFECYCLE_ACTIONS = frozenset({"created", "edited", "deleted"})
REPOSITORY_SLUG_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


class WebhookSecretSource(Protocol):
    """Provide the GitHub App webhook secret for signature verification."""

    def load_webhook_secret(self) -> bytes:
        """Return the webhook secret bytes."""


class EnvWebhookSecretSource:
    """Load the GitHub App webhook secret from an environment variable."""

    def __init__(self, variable: str = "GITHUB_APP_WEBHOOK_SECRET") -> None:
        if not variable.strip():
            raise GitHubWebhookError(
                "GitHub App webhook secret environment variable must be non-empty"
            )
        self.variable = variable

    def load_webhook_secret(self) -> bytes:
        value = os.environ.get(self.variable)
        if value is None:
            raise GitHubWebhookError(f"{self.variable} is not set")
        if not value.strip():
            raise GitHubWebhookError(
                f"{self.variable} must be a non-empty webhook secret"
            )
        return value.encode("utf-8")


@dataclass(frozen=True)
class VerifiedDelivery:
    """Validated metadata from one verified GitHub webhook delivery."""

    app_id: int
    event: str
    action: str
    installation_id: int
    delivery_id: str
    digest: str
    repository: str | None = None
    repositories: tuple[str, ...] = ()
    repository_id: int | None = None
    suspended: bool = False
    permissions: dict[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if isinstance(self.app_id, bool) or not isinstance(self.app_id, int):
            raise GitHubWebhookError("GitHub App id must be an integer")
        if self.app_id <= 0:
            raise GitHubWebhookError("GitHub App id must be a positive integer")
        if not self.event or not self.event.strip():
            raise GitHubWebhookError("GitHub webhook event must be non-empty")
        if not self.action or not self.action.strip():
            raise GitHubWebhookError("GitHub webhook action must be non-empty")
        if isinstance(self.installation_id, bool) or not isinstance(
            self.installation_id, int
        ):
            raise GitHubWebhookError(
                "GitHub webhook installation id must be a positive integer"
            )
        if self.installation_id <= 0:
            raise GitHubWebhookError(
                "GitHub webhook installation id must be a positive integer"
            )
        if not self.delivery_id or not self.delivery_id.strip():
            raise GitHubWebhookError("GitHub webhook delivery id must be non-empty")
        if not self.digest or not self.digest.strip():
            raise GitHubWebhookError("GitHub webhook digest must be non-empty")
        if self.repository is not None:
            if not isinstance(self.repository, str):
                raise GitHubWebhookError("GitHub webhook repository must be a slug")
            if not REPOSITORY_SLUG_PATTERN.fullmatch(self.repository):
                raise GitHubWebhookError("GitHub webhook repository must be a slug")
        if not isinstance(self.repositories, tuple):
            raise GitHubWebhookError("GitHub webhook repositories must be a tuple")
        for repository in self.repositories:
            if not isinstance(repository, str):
                raise GitHubWebhookError("GitHub webhook repository must be a slug")
            if not REPOSITORY_SLUG_PATTERN.fullmatch(repository):
                raise GitHubWebhookError("GitHub webhook repository must be a slug")
        if not isinstance(self.permissions, dict):
            raise GitHubWebhookError("GitHub webhook permissions must be an object")
        for key, value in self.permissions.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise GitHubWebhookError("GitHub webhook permissions were invalid")


@dataclass(frozen=True)
class VerifiedCommentLifecycleDelivery:
    """Validated metadata and G27 lifecycle routing for a signed comment webhook."""

    app_id: int
    event: str
    action: str
    installation_id: int
    delivery_id: str
    digest: str
    repository: str
    repository_id: int
    pull_request: int
    comment_id: int
    comment_kind: str
    comment_body: str
    comment_updated_at: str
    source_descriptor: dict[str, Any]
    body_changed: bool = False
    duplicate_delivery_id: bool = False
    lifecycle_classification: SourceLifecycleClassification | None = None
    lifecycle_outcome: SessionRecord | ApiRunAccounting | None = None


@dataclass(frozen=True)
class VerifiedEventRouteDelivery:
    """Validated signed webhook delivery with G01/G07/G13/G26/G28 routing and convergence."""

    app_id: int
    event: str
    action: str
    delivery_id: str
    digest: str
    repository: str | None
    repository_id: int | None
    duplicate_delivery_id: bool
    routing: EventRoutingDecision
    convergence: ConvergenceDecision | None = None


class DeliveryLedger(Protocol):
    """Record and test webhook delivery deduplication."""

    def is_known(self, *, app_id: int, delivery_id: str) -> bool:
        """Return whether the delivery has already been accepted."""

    def record(self, *, app_id: int, delivery_id: str) -> None:
        """Mark a delivery as accepted."""


class InMemoryDeliveryLedger:
    """In-memory webhook delivery deduplication ledger."""

    def __init__(self, max_entries: int = 10_000) -> None:
        if (
            isinstance(max_entries, bool)
            or not isinstance(max_entries, int)
            or max_entries <= 0
        ):
            raise GitHubWebhookError(
                "GitHub webhook delivery ledger size must be positive"
            )
        self.max_entries = max_entries
        self._keys: list[tuple[int, str]] = []
        self._seen: set[tuple[int, str]] = set()

    def is_known(self, *, app_id: int, delivery_id: str) -> bool:
        return (app_id, delivery_id) in self._seen

    def record(self, *, app_id: int, delivery_id: str) -> None:
        key = (app_id, delivery_id)
        if key in self._seen:
            return
        self._seen.add(key)
        self._keys.append(key)
        while len(self._keys) > self.max_entries:
            old_key = self._keys.pop(0)
            self._seen.discard(old_key)


class WebhookVerifier:
    """Verify GitHub webhook signatures and validate delivery metadata."""

    def __init__(
        self,
        *,
        app_id: int,
        secret_source: WebhookSecretSource,
        ledger: DeliveryLedger | None = None,
        max_body_bytes: int = MAX_WEBHOOK_BODY_BYTES,
    ) -> None:
        if isinstance(app_id, bool) or not isinstance(app_id, int) or app_id <= 0:
            raise GitHubWebhookError("GitHub App id must be a positive integer")
        self.app_id = app_id
        self.secret_source = secret_source
        self.ledger = ledger or InMemoryDeliveryLedger()
        if (
            isinstance(max_body_bytes, bool)
            or not isinstance(max_body_bytes, int)
            or max_body_bytes <= 0
        ):
            raise GitHubWebhookError(
                "GitHub webhook body limit must be a positive integer"
            )
        self.max_body_bytes = max_body_bytes

    def _verify_signed_envelope(
        self,
        *,
        body: bytes,
        signature: str | None,
        event: str,
        delivery_id: str,
    ) -> None:
        if not isinstance(body, (bytes, bytearray)):
            raise GitHubWebhookError("GitHub webhook body was invalid")
        if len(body) > self.max_body_bytes:
            raise GitHubWebhookError(
                "GitHub webhook body exceeded the configured size limit"
            )
        if not isinstance(signature, str) or not signature:
            raise GitHubWebhookSignatureError("GitHub webhook signature is required")
        expected = self._signature_for(body)
        if not self._constant_time_equals(signature, expected):
            raise GitHubWebhookSignatureError("GitHub webhook signature is invalid")
        if not event or not event.strip():
            raise GitHubWebhookError("GitHub webhook event must be non-empty")
        if not delivery_id or not delivery_id.strip():
            raise GitHubWebhookError("GitHub webhook delivery id must be non-empty")

    @staticmethod
    def _parse_json_object(body: bytes) -> dict[str, object]:
        try:
            payload = json.loads(bytes(body).decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubWebhookError("GitHub webhook body was invalid JSON") from exc
        if not isinstance(payload, dict):
            raise GitHubWebhookError("GitHub webhook body must be a JSON object")
        return payload

    def verify(
        self,
        *,
        body: bytes,
        signature: str | None,
        event: str,
        delivery_id: str,
    ) -> VerifiedDelivery:
        self._verify_signed_envelope(
            body=body,
            signature=signature,
            event=event,
            delivery_id=delivery_id,
        )
        if self.ledger.is_known(app_id=self.app_id, delivery_id=delivery_id):
            raise GitHubWebhookError("GitHub webhook delivery was already processed")
        if event not in SUPPORTED_WEBHOOK_EVENTS:
            if event.strip() == "merge_group":
                raise GitHubMergeGroupUnsupportedError(
                    "GitHub merge_group webhook event is unsupported"
                )
            raise GitHubWebhookError("GitHub webhook event is not supported")
        self.ledger.record(app_id=self.app_id, delivery_id=delivery_id)
        payload = self._parse_json_object(body)
        delivery = self._validate_payload(payload, event=event, delivery_id=delivery_id)
        return delivery

    def verify_comment_lifecycle(
        self,
        *,
        body: bytes,
        signature: str | None,
        event: str,
        delivery_id: str,
        caller: ApiRunRequestTransport | None = None,
        session_ledger: GitHubIssueCommentSessionLedger | None = None,
        identity: SessionIdentity | None = None,
        record: SessionRecord | None = None,
        has_pending_receipt: bool = False,
        execute_route: bool = False,
    ) -> VerifiedCommentLifecycleDelivery:
        """Verify a signed comment webhook and optionally classify/execute G27 routing."""

        self._verify_signed_envelope(
            body=body,
            signature=signature,
            event=event,
            delivery_id=delivery_id,
        )
        normalized_event = event.strip()
        if normalized_event == "merge_group":
            raise GitHubMergeGroupUnsupportedError(
                "GitHub merge_group webhook event is unsupported"
            )
        if normalized_event not in COMMENT_LIFECYCLE_WEBHOOK_EVENTS:
            raise GitHubWebhookError(
                "GitHub comment lifecycle webhook event is not supported"
            )
        has_admitted_action = False
        if record is not None and record.api_run_accounting is not None:
            has_admitted_action = (
                ApiRunAccounting.from_document(record.api_run_accounting).action
                is not None
            )
        duplicate_delivery_id = self.ledger.is_known(
            app_id=self.app_id, delivery_id=delivery_id
        )

        payload = self._parse_json_object(body)
        action = payload.get("action")
        if (
            not isinstance(action, str)
            or action.strip() not in COMMENT_LIFECYCLE_ACTIONS
        ):
            raise GitHubWebhookError("GitHub comment webhook action is invalid")
        normalized_action = action.strip()

        installation = payload.get("installation")
        if not isinstance(installation, Mapping):
            raise GitHubWebhookError("GitHub webhook installation is missing")
        installation_id = installation.get("id")
        if (
            isinstance(installation_id, bool)
            or not isinstance(installation_id, int)
            or installation_id <= 0
        ):
            raise GitHubWebhookError("GitHub webhook installation id is invalid")

        repo_obj = payload.get("repository")
        if not isinstance(repo_obj, Mapping):
            raise GitHubWebhookError("GitHub webhook repository is missing")
        repository_slug = repo_obj.get("full_name")
        if not isinstance(
            repository_slug, str
        ) or not REPOSITORY_SLUG_PATTERN.fullmatch(repository_slug):
            raise GitHubWebhookError("GitHub webhook repository must be a slug")
        raw_repo_id = repo_obj.get("id")
        if (
            isinstance(raw_repo_id, bool)
            or not isinstance(raw_repo_id, int)
            or raw_repo_id <= 0
        ):
            raise GitHubWebhookError("GitHub webhook repository id is invalid")
        repository_id = raw_repo_id

        if normalized_event == "issue_comment":
            issue_obj = payload.get("issue")
            if (
                not isinstance(issue_obj, Mapping)
                or "pull_request" not in issue_obj
                or isinstance(issue_obj.get("number"), bool)
                or not isinstance(issue_obj.get("number"), int)
                or int(issue_obj["number"]) <= 0
            ):
                raise GitHubWebhookError(
                    "GitHub issue_comment webhook is not bound to a pull request"
                )
            pull_request = int(issue_obj["number"])
            comment_kind = "issue_comment"
        else:
            pr_obj = payload.get("pull_request")
            if (
                not isinstance(pr_obj, Mapping)
                or isinstance(pr_obj.get("number"), bool)
                or not isinstance(pr_obj.get("number"), int)
                or int(pr_obj["number"]) <= 0
            ):
                raise GitHubWebhookError(
                    "GitHub review comment webhook pull_request is invalid"
                )
            pull_request = int(pr_obj["number"])
            comment_kind = "review_comment"

        comment_obj = payload.get("comment")
        if (
            not isinstance(comment_obj, Mapping)
            or isinstance(comment_obj.get("id"), bool)
            or not isinstance(comment_obj.get("id"), int)
            or int(comment_obj["id"]) <= 0
            or not isinstance(comment_obj.get("body"), str)
            or not isinstance(comment_obj.get("updated_at"), str)
        ):
            raise GitHubWebhookError(
                "GitHub comment webhook comment payload is invalid"
            )
        comment_id = int(comment_obj["id"])
        comment_body = str(comment_obj["body"])
        comment_updated_at = str(comment_obj["updated_at"])
        source_descriptor = build_source_comment_descriptor(
            kind=comment_kind,
            comment_id=comment_id,
            updated_at=comment_updated_at,
            body=comment_body,
        )
        changes_obj = payload.get("changes")
        body_changed = normalized_action == "edited" or (
            isinstance(changes_obj, Mapping) and "body" in changes_obj
        )
        classification: SourceLifecycleClassification | None = None
        outcome: SessionRecord | ApiRunAccounting | None = None
        if caller is not None or session_ledger is not None or identity is not None:
            if caller is None or session_ledger is None or identity is None:
                raise GitHubWebhookError(
                    "GitHub comment lifecycle routing requires caller, session_ledger, and identity"
                )
            if (
                identity.repository != repository_slug
                or identity.pull_request != pull_request
                or identity.repository_id != repository_id
            ):
                raise GitHubWebhookError(
                    "GitHub comment webhook scope conflicts with session identity"
                )
            if not has_admitted_action and (
                normalized_action != "created" or body_changed
            ):
                if execute_route:
                    admit_event_comment_source(
                        payload,
                        pull_request=pull_request,
                        source_comment_id=comment_id,
                        purpose="command",
                        event_name=normalized_event,
                        expected_body=comment_body,
                        expected_updated_at=comment_updated_at,
                    )
                mut_state = (
                    "source_deleted"
                    if normalized_action == "deleted"
                    else "source_edited"
                )
                disp_state = (
                    "deleted_source"
                    if normalized_action == "deleted"
                    else "mutated_revision"
                )
                classification = SourceLifecycleClassification(
                    identity=identity,
                    phase="before_admission",
                    original_source=dict(source_descriptor),
                    observation=SourceRevisionObservation(
                        comment_id=comment_id,
                        kind=comment_kind,
                        status=404 if normalized_action == "deleted" else 200,
                        observed_updated_at=(
                            None
                            if normalized_action == "deleted"
                            else comment_updated_at
                        ),
                        observed_body_sha256=(
                            None
                            if normalized_action == "deleted"
                            else str(source_descriptor["body_sha256"])
                        ),
                        mutation_state=mut_state,
                        delivery_disposition=disp_state,
                    ),
                    route="refuse_before_admission",
                    g32_eligible=False,
                    replay_permitted=False,
                    prior_charges_preserved=True,
                    guidance=(
                        f"pre-admission source check refused ({mut_state}/{disp_state}): "
                        "zero session mutation allowed; submit or dispatch a fresh event "
                        "bound to the current live comment revision"
                    ),
                )
                outcome = record
            else:
                event_delivery = (
                    None if normalized_action == "deleted" else source_descriptor
                )
                if execute_route:
                    classification, outcome = execute_source_lifecycle_route(
                        caller=caller,
                        ledger=session_ledger,
                        identity=identity,
                        record=record,
                        candidate_source=source_descriptor,
                        event_delivery=event_delivery,
                        has_pending_receipt=has_pending_receipt,
                    )
                else:
                    classification = classify_source_comment_lifecycle(
                        caller=caller,
                        ledger=session_ledger,
                        identity=identity,
                        record=record,
                        candidate_source=source_descriptor,
                        event_delivery=event_delivery,
                        has_pending_receipt=has_pending_receipt,
                    )
                    outcome = record

        digest = hashlib.sha256(
            bytes(json.dumps(payload, sort_keys=True), "utf-8")
        ).hexdigest()
        delivery = VerifiedCommentLifecycleDelivery(
            app_id=self.app_id,
            event=normalized_event,
            action=normalized_action,
            installation_id=installation_id,
            delivery_id=delivery_id,
            digest=digest,
            repository=repository_slug,
            repository_id=repository_id,
            pull_request=pull_request,
            comment_id=comment_id,
            comment_kind=comment_kind,
            comment_body=comment_body,
            comment_updated_at=comment_updated_at,
            source_descriptor=source_descriptor,
            body_changed=body_changed,
            duplicate_delivery_id=duplicate_delivery_id,
            lifecycle_classification=classification,
            lifecycle_outcome=outcome,
        )
        if not duplicate_delivery_id:
            self.ledger.record(app_id=self.app_id, delivery_id=delivery_id)
        return delivery

    def verify_event_route(
        self,
        *,
        body: bytes,
        signature: str | None,
        event: str,
        delivery_id: str,
        live_pull_request: Mapping[str, Any] | None = None,
        retained_state: RetainedSnapshotState | None = None,
        live_target: LivePullRequestTarget | None = None,
        records: Sequence[AcceptedOperationRecord] = (),
        was_queued_replaced: bool = False,
        current_state_fingerprint: str | None = None,
        max_no_progress_attempts: int = 2,
        reject_merge_group: bool = False,
    ) -> VerifiedEventRouteDelivery:
        """Verify a signed GitHub webhook and evaluate G01/G07/G13/G26/G28 routing."""

        from .event_routing_contract import (
            evaluate_github_event,
            reconcile_event_convergence,
        )

        self._verify_signed_envelope(
            body=body,
            signature=signature,
            event=event,
            delivery_id=delivery_id,
        )
        normalized_event = event.strip()
        if reject_merge_group and normalized_event == "merge_group":
            raise GitHubMergeGroupUnsupportedError(
                "GitHub merge_group webhook event is unsupported"
            )
        payload = self._parse_json_object(body)
        duplicate_delivery_id = self.ledger.is_known(
            app_id=self.app_id,
            delivery_id=delivery_id,
        )
        raw_action = payload.get("action")
        action = (
            raw_action.strip()
            if isinstance(raw_action, str) and raw_action.strip()
            else ("manual" if normalized_event == "workflow_dispatch" else "")
        )
        repository_slug: str | None = None
        repository_id: int | None = None
        repository_obj = payload.get("repository")
        if isinstance(repository_obj, dict):
            full_name = repository_obj.get("full_name")
            if isinstance(full_name, str) and REPOSITORY_SLUG_PATTERN.fullmatch(
                full_name
            ):
                repository_slug = full_name
            rid = repository_obj.get("id")
            if not isinstance(rid, bool) and isinstance(rid, int) and rid > 0:
                repository_id = rid

        effective_live_pr = (
            live_target.to_pull_mapping()
            if (live_pull_request is None and live_target is not None)
            else live_pull_request
        )
        routing = evaluate_github_event(
            normalized_event,
            payload,
            delivery_id=delivery_id,
            live_pull_request=effective_live_pr,
            retained_state=retained_state,
        )
        convergence: ConvergenceDecision | None = None
        if live_target is not None:
            convergence = reconcile_event_convergence(
                normalized_event,
                payload,
                live_target=live_target,
                records=records,
                delivery_id=delivery_id,
                was_queued_replaced=was_queued_replaced,
                current_state_fingerprint=current_state_fingerprint,
                max_no_progress_attempts=max_no_progress_attempts,
            )
        # Seeing a delivery is not proof that its route was admitted or completed.
        # Only accepted-operation history may suppress work on redelivery; a
        # caller can fail after receiving this routing decision and before admission.
        digest = hashlib.sha256(
            bytes(json.dumps(payload, sort_keys=True), "utf-8")
        ).hexdigest()
        delivery = VerifiedEventRouteDelivery(
            app_id=self.app_id,
            event=normalized_event,
            action=action,
            delivery_id=delivery_id,
            digest=digest,
            repository=repository_slug,
            repository_id=repository_id,
            duplicate_delivery_id=duplicate_delivery_id,
            routing=routing,
            convergence=convergence,
        )
        if not duplicate_delivery_id:
            self.ledger.record(app_id=self.app_id, delivery_id=delivery_id)
        return delivery

    def _signature_for(self, body: bytes) -> str:
        secret = self.secret_source.load_webhook_secret()
        if not secret:
            raise GitHubWebhookError("GitHub webhook secret must be non-empty")
        digest = hmac.new(secret, bytes(body), hashlib.sha256).hexdigest()
        return f"sha256={digest}"

    @staticmethod
    def _constant_time_equals(left: str, right: str) -> bool:
        try:
            return hmac.compare_digest(
                left.encode("ascii"),
                right.encode("ascii"),
            )
        except UnicodeEncodeError:
            return False

    def _validate_payload(
        self,
        payload: dict[str, object],
        *,
        event: str,
        delivery_id: str,
    ) -> VerifiedDelivery:
        action = payload.get("action")
        if not isinstance(action, str) or not action.strip():
            raise GitHubWebhookError("GitHub webhook action is missing")
        installation = payload.get("installation")
        if not isinstance(installation, dict):
            raise GitHubWebhookError("GitHub webhook installation is missing")
        installation_id = installation.get("id")
        if isinstance(installation_id, bool) or not isinstance(installation_id, int):
            raise GitHubWebhookError("GitHub webhook installation id is invalid")
        if installation_id <= 0:
            raise GitHubWebhookError("GitHub webhook installation id is invalid")
        suspended = installation.get("suspended_at") is not None
        repository_slug: str | None = None
        repository_slugs: tuple[str, ...] = ()
        repository_id: int | None = None
        permissions = installation.get("permissions")
        normalized_permissions: dict[str, str] = {}
        if isinstance(permissions, dict):
            for key, value in permissions.items():
                if isinstance(key, str) and isinstance(value, str):
                    normalized_permissions[key.strip().lower().replace("-", "_")] = (
                        value.strip().lower()
                    )
        needs_repository = (
            event == "installation_repositories" and action == "added"
        ) or action in {
            "created",
            "new_permissions_accepted",
        }
        if needs_repository:
            repository_slugs = self._extract_repository_slugs(payload)
            if repository_slugs:
                repository_slug = repository_slugs[0]
            else:
                single = payload.get("repository")
                if isinstance(single, dict):
                    full_name = single.get("full_name")
                    if isinstance(full_name, str):
                        if not REPOSITORY_SLUG_PATTERN.fullmatch(full_name):
                            raise GitHubWebhookError(
                                "GitHub webhook repository must be a slug"
                            )
                        repository_slug = full_name
                        repository_slugs = (full_name,)
                        rid = single.get("id")
                        if isinstance(rid, bool) or not isinstance(rid, int):
                            repository_id = None
                        elif rid > 0:
                            repository_id = rid
            if not repository_slugs:
                raise GitHubWebhookError("GitHub webhook repository is missing")
        digest = hashlib.sha256(
            bytes(json.dumps(payload, sort_keys=True), "utf-8")
        ).hexdigest()
        return VerifiedDelivery(
            app_id=self.app_id,
            event=event,
            action=action,
            installation_id=installation_id,
            delivery_id=delivery_id,
            digest=digest,
            repository=repository_slug,
            repositories=repository_slugs,
            repository_id=repository_id,
            suspended=suspended,
            permissions=normalized_permissions,
        )

    def _extract_repository_slugs(self, payload: dict[str, object]) -> tuple[str, ...]:
        for key in ("repositories", "repositories_added"):
            raw = payload.get(key)
            if not isinstance(raw, list):
                continue
            slugs: list[str] = []
            for item in raw:
                if not isinstance(item, dict):
                    continue
                full_name = item.get("full_name")
                if isinstance(full_name, str):
                    if not REPOSITORY_SLUG_PATTERN.fullmatch(full_name):
                        raise GitHubWebhookError(
                            "GitHub webhook repository must be a slug"
                        )
                    slugs.append(full_name)
            if slugs:
                return tuple(slugs)
        return ()

"""GitHub App webhook signature verification and delivery safety."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass, field
from typing import Protocol

from .errors import (
    GitHubWebhookError,
    GitHubWebhookSignatureError,
)

MAX_WEBHOOK_BODY_BYTES = 1024 * 1024
SUPPORTED_WEBHOOK_EVENTS = frozenset(
    {
        "installation",
        "installation_repositories",
    }
)
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

    def verify(
        self,
        *,
        body: bytes,
        signature: str | None,
        event: str,
        delivery_id: str,
    ) -> VerifiedDelivery:
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
        if self.ledger.is_known(app_id=self.app_id, delivery_id=delivery_id):
            raise GitHubWebhookError("GitHub webhook delivery was already processed")
        if event not in SUPPORTED_WEBHOOK_EVENTS:
            raise GitHubWebhookError("GitHub webhook event is not supported")
        self.ledger.record(app_id=self.app_id, delivery_id=delivery_id)
        try:
            payload = json.loads(bytes(body).decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubWebhookError("GitHub webhook body was invalid JSON") from exc
        if not isinstance(payload, dict):
            raise GitHubWebhookError("GitHub webhook body must be a JSON object")
        delivery = self._validate_payload(payload, event=event, delivery_id=delivery_id)
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

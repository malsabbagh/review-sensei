"""Provider-neutral concurrency policy for review hosts.

The library does not own a process-wide queue or a workflow runner.  Instead,
it exposes the stable groups and admission rules that an embedding application
can enforce with its scheduler.  Keeping this policy in the review package
lets different hosts agree on the same latest-wins and per-pull-request
provider behavior without importing GitHub or provider SDKs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import ReviewInputError
from .models import ReviewRequest

_SAFE_REPOSITORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
_SAFE_PROVIDER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SAFE_TRIGGER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _safe_identifier(value: object, *, label: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str):
        raise ReviewInputError(f"{label} must be a non-empty safe identifier")
    normalized = value.strip()
    if not normalized or not pattern.fullmatch(normalized):
        raise ReviewInputError(f"{label} must be a non-empty safe identifier")
    return normalized


def _positive_number(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ReviewInputError(f"{label} must be a positive integer")
    if value > 2147483647:
        raise ReviewInputError(f"{label} is too large (maximum is 2147483647)")
    return value


def _encode_components(namespace: str, *components: str) -> str:
    """Encode components with length prefixing to guarantee collision-free keys."""
    encoded_parts = [f"{len(part)}:{part}" for part in components]
    return f"review-sensei:{namespace}:" + ":".join(encoded_parts)


def _trigger_identifier(value: object) -> str:
    if isinstance(value, bool):
        raise ReviewInputError(
            "trigger_id must be a positive integer or safe identifier"
        )
    if isinstance(value, int):
        return str(_positive_number(value, label="trigger_id"))
    return _safe_identifier(value, label="trigger_id", pattern=_SAFE_TRIGGER)


@dataclass(frozen=True)
class ConcurrencyGroup:
    """Admission policy for one host-enforced concurrency group."""

    key: str
    max_active: int = 1
    cancel_in_progress: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key.strip():
            raise ReviewInputError("concurrency group key must be a non-empty string")
        if any(ord(character) < 32 or ord(character) == 127 for character in self.key):
            raise ReviewInputError(
                "concurrency group key must not contain control characters"
            )
        if (
            isinstance(self.max_active, bool)
            or not isinstance(self.max_active, int)
            or self.max_active < 1
        ):
            raise ReviewInputError("concurrency max_active must be a positive integer")
        if not isinstance(self.cancel_in_progress, bool):
            raise ReviewInputError("concurrency cancel_in_progress must be a boolean")

    def to_dict(self) -> dict[str, object]:
        return {
            "key": self.key,
            "max_active": self.max_active,
            "cancel_in_progress": self.cancel_in_progress,
        }


@dataclass(frozen=True)
class ReviewConcurrencyPlan:
    """Host-enforced concurrency policy for one review trigger.

    A pull-request review has two scopes:

    * ``workflow`` is latest-wins, so a newer revision supersedes an older
      review for the same repository and pull request.
    * ``provider`` admits one provider-consuming stage for that pull request at
      a time, without cancelling the active stage.  Its key includes the
      provider and pull request, so unrelated pull requests remain independent.

    A non-review trigger receives only an isolated workflow group.  This is for
    ignored or informational triggers that must not cancel an active review.
    The plan describes policy; the caller remains responsible for enforcing it
    across processes, workers, or hosted workflow runs.
    """

    workflow: ConcurrencyGroup
    provider: ConcurrencyGroup | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.workflow, ConcurrencyGroup):
            raise ReviewInputError("workflow must be a ConcurrencyGroup")
        if self.provider is not None and not isinstance(
            self.provider, ConcurrencyGroup
        ):
            raise ReviewInputError("provider must be a ConcurrencyGroup or None")

    @classmethod
    def for_pull_request(
        cls,
        repository: str,
        pull_request_number: int,
        *,
        provider_name: str = "ollama",
    ) -> "ReviewConcurrencyPlan":
        """Build the latest-wins and per-provider policy for a pull request."""

        repository_id = _safe_identifier(
            repository,
            label="repository",
            pattern=_SAFE_REPOSITORY,
        )
        number = _positive_number(pull_request_number, label="pull_request_number")
        provider_id = _safe_identifier(
            provider_name,
            label="provider_name",
            pattern=_SAFE_PROVIDER,
        )
        return cls(
            workflow=ConcurrencyGroup(
                key=_encode_components("review", repository_id, str(number)),
                max_active=1,
                cancel_in_progress=True,
            ),
            provider=ConcurrencyGroup(
                key=_encode_components(
                    "provider", provider_id, repository_id, str(number)
                ),
                max_active=1,
                cancel_in_progress=False,
            ),
        )

    @classmethod
    def for_request(
        cls,
        request: ReviewRequest,
        *,
        provider_name: str = "ollama",
    ) -> "ReviewConcurrencyPlan":
        """Build a plan from review metadata, failing when it is incomplete."""

        if not isinstance(request, ReviewRequest):
            raise ReviewInputError("request must be a ReviewRequest")
        if request.repository is None:
            raise ReviewInputError("concurrency requires review repository metadata")
        if request.pull_request_number is None:
            raise ReviewInputError("concurrency requires a pull request number")
        return cls.for_pull_request(
            request.repository,
            request.pull_request_number,
            provider_name=provider_name,
        )

    @classmethod
    def for_non_review_trigger(
        cls,
        repository: str,
        trigger_id: str | int,
    ) -> "ReviewConcurrencyPlan":
        """Isolate a trigger that will not run a provider review.

        Hosts can use this for ignored comments, duplicate notifications, or
        other events that should never cancel a pull-request review.  The
        trigger identifier is part of the group, so separate triggers do not
        contend with one another.
        """

        repository_id = _safe_identifier(
            repository,
            label="repository",
            pattern=_SAFE_REPOSITORY,
        )
        trigger = _trigger_identifier(trigger_id)
        return cls(
            workflow=ConcurrencyGroup(
                key=_encode_components("trigger", repository_id, trigger),
                max_active=1,
                cancel_in_progress=True,
            )
        )

    @property
    def workflow_key(self) -> str:
        return self.workflow.key

    @property
    def provider_key(self) -> str | None:
        return self.provider.key if self.provider is not None else None

    def to_dict(self) -> dict[str, object]:
        return {
            "workflow": self.workflow.to_dict(),
            "provider": self.provider.to_dict() if self.provider is not None else None,
        }

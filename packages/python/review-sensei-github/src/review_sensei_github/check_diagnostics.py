"""Closed, invocation-local check failure evidence; never retain remote text."""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.error import HTTPError

from review_sensei.outcomes import CHECK_PUBLICATION_DIAGNOSTICS

from .errors import GitHubPublicationError, GitHubPublicationTransientError

CHECK_FAILURE_CODES = CHECK_PUBLICATION_DIAGNOSTICS - {"check_permission"}


@dataclass(frozen=True)
class CheckFailure:
    code: str
    operation: str
    status: int | None = None

    def __post_init__(self) -> None:
        if self.code not in CHECK_FAILURE_CODES:
            raise ValueError("invalid check diagnostic")
        if self.operation not in {"capability", "lookup", "create", "update"}:
            raise ValueError("invalid check operation")
        if self.status is not None and (
            type(self.status) is not int or not 100 <= self.status <= 599
        ):
            raise ValueError("invalid check response status")


@dataclass(frozen=True)
class CheckCapability:
    token: str | None = field(default=None, repr=False)
    failure: CheckFailure | None = None


def observed_http_status(error: Exception) -> int | None:
    """Read only an observed typed HTTP status, never parse exception text."""
    cause = error.__cause__
    if (
        isinstance(cause, HTTPError)
        and type(cause.code) is int
        and 100 <= cause.code <= 599
    ):
        return cause.code
    return None


def check_failure(
    operation: str, *, status: int | None = None, transient: bool = False
) -> CheckFailure:
    if type(status) is not int or not 100 <= status <= 599:
        status = None
    if status in {401, 403}:
        code = "check_access_denied"
    elif transient or status == 429 or (status is not None and 500 <= status <= 599):
        code = "check_temporarily_unavailable"
    elif operation == "capability":
        code = "check_capability_unavailable"
    else:
        code = "check_publication_failed"
    return CheckFailure(code, operation, status)


class CheckOperationError(GitHubPublicationError):
    def __init__(self, failure: CheckFailure) -> None:
        self.failure = failure
        super().__init__(f"check {failure.operation} failed: {failure.code}")


class CheckOperationTransientError(
    CheckOperationError, GitHubPublicationTransientError
):
    """Preserve the existing typed transient error contract."""

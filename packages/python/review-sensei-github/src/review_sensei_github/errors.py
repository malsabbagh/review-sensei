"""Sanitized GitHub App authentication errors."""

from __future__ import annotations

import math

from review_sensei.broker_diagnostics import BrokerDiagnostic
from review_sensei.errors import ReviewSenseiError

VALID_RATE_LIMIT_KINDS: frozenset[str] = frozenset(
    {"primary", "secondary", "graphql_200"}
)


class GitHubAuthError(ReviewSenseiError):
    """Base class for GitHub App authentication failures."""

    error_category = "github_auth"


class GitHubAuthConfigurationError(GitHubAuthError):
    """Raised when GitHub App authentication is not configured correctly."""

    error_category = "github_auth_configuration"


class GitHubAuthUnavailableInstallationError(GitHubAuthError):
    """Raised when the GitHub App installation is unavailable or removed."""

    error_category = "github_auth_unavailable_installation"


class GitHubAuthInsufficientPermissionError(GitHubAuthError):
    """Raised when the installation does not grant the requested permission."""

    error_category = "github_auth_insufficient_permission"


class GitHubAuthTransientError(GitHubAuthError):
    """Raised for transient GitHub API or rate-limit failures."""

    error_category = "github_auth_transient"


class GitHubWebhookError(ReviewSenseiError):
    """Base class for GitHub webhook delivery failures."""

    error_category = "github_webhook"


class GitHubWebhookSignatureError(GitHubWebhookError):
    """Raised when a GitHub webhook signature is missing or invalid."""

    error_category = "github_webhook_signature"


class GitHubMergeGroupUnsupportedError(GitHubWebhookError):
    """Raised when a merge_group webhook event is rejected at the queue boundary."""

    error_category = "github_webhook"
    outcome_code = "merge_group_unsupported"


class GitHubSetupError(ReviewSenseiError):
    """Base class for GitHub App setup bootstrap failures."""

    error_category = "github_setup"


class GitHubSetupTransientError(GitHubSetupError):
    """Raised for transient GitHub setup bootstrap failures."""

    error_category = "github_setup_transient"


class GitHubOIDCError(ReviewSenseiError):
    """Base class for GitHub Actions OIDC verification failures."""

    error_category = "github_oidc"


class GitHubOIDCInvalidTokenError(GitHubOIDCError):
    """Raised when a GitHub Actions OIDC token is malformed or invalid."""

    error_category = "github_oidc_invalid_token"


class GitHubOIDCAudienceError(GitHubOIDCError):
    """Raised when a GitHub Actions OIDC token has the wrong audience."""

    error_category = "github_oidc_audience"


class GitHubOIDCIssuerError(GitHubOIDCError):
    """Raised when a GitHub Actions OIDC token has the wrong issuer."""

    error_category = "github_oidc_issuer"


class GitHubOIDCExpiredError(GitHubOIDCError):
    """Raised when a GitHub Actions OIDC token has expired."""

    error_category = "github_oidc_expired"


class GitHubOIDCSignatureError(GitHubOIDCError):
    """Raised when a GitHub Actions OIDC token signature is invalid."""

    error_category = "github_oidc_signature"


class GitHubOIDCClaimsError(GitHubOIDCError):
    """Raised when a GitHub Actions OIDC token has invalid claims."""

    error_category = "github_oidc_claims"


class BrokerPolicyError(ReviewSenseiError):
    """Raised when broker policy configuration is invalid."""

    error_category = "broker_policy"


class BrokerRateLimitError(ReviewSenseiError):
    """Raised when the broker rate limit is exceeded."""

    error_category = "broker_rate_limit"


class BrokerRejectionError(ReviewSenseiError):
    """Raised when the broker rejects an OIDC workflow identity."""

    error_category = "broker_rejection"


class GitHubHTTPError(ReviewSenseiError):
    """Raised for bounded GitHub REST transport failures."""

    error_category = "github_http"

    def __init__(
        self,
        message: str = "",
        *,
        http_status: int | None = None,
        failed_lookup: str | None = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.failed_lookup = failed_lookup


class GitHubHTTPResponseTooLargeError(GitHubHTTPError):
    """Raised when a GitHub response exceeds the transport byte ceiling."""

    error_category = "github_http_response_too_large"

    def __init__(
        self,
        message,
        *,
        completed_json_objects: int = 0,
        completed_comment_nodes: int = 0,
    ):
        super().__init__(message)
        # Size hint only; never retain or expose partial response bytes.
        self.completed_json_objects = completed_json_objects
        self.completed_comment_nodes = completed_comment_nodes


class GitHubHTTPPaginationLimitError(GitHubHTTPError):
    """Raised when a bounded GitHub pagination walk reaches its page limit."""

    error_category = "github_http_pagination_limit"


class GitHubHTTPTransientError(GitHubHTTPError):
    """Raised for transient GitHub REST failures."""

    error_category = "github_http_transient"

    def __init__(
        self, message: str = "", *, broker_diagnostic: BrokerDiagnostic | None = None
    ) -> None:
        super().__init__(message)
        self.broker_diagnostic = broker_diagnostic


class GitHubHTTPPermissionError(GitHubHTTPError):
    """Raised when a GitHub API request is rejected due to missing permissions."""

    error_category = "github_http_permission"

    def __init__(
        self,
        message: str = "",
        *,
        http_status: int | None = 403,
        failed_lookup: str | None = None,
        endpoint_id: str | None = None,
        credential_kind: str | None = None,
        missing_scope: str | None = None,
    ) -> None:
        super().__init__(
            message,
            http_status=http_status,
            failed_lookup=failed_lookup if failed_lookup is not None else endpoint_id,
        )
        self.endpoint_id = endpoint_id
        self.credential_kind = credential_kind
        self.missing_scope = missing_scope


class GitHubHTTPRateLimitError(GitHubHTTPTransientError):
    """Raised when GitHub signals REST or GraphQL rate-limit throttling."""

    error_category = "github_http_rate_limit"

    def __init__(
        self,
        message: str = "",
        *,
        credential_kind: str | None = None,
        rate_limit_kind: str = "primary",
        retry_after_seconds: float | None = None,
        reset_epoch: int | None = None,
        remaining: int | None = None,
        resource: str | None = None,
        http_status: int | None = None,
        endpoint_id: str | None = None,
        failed_lookup: str | None = None,
        broker_diagnostic: BrokerDiagnostic | None = None,
    ) -> None:
        if rate_limit_kind not in VALID_RATE_LIMIT_KINDS:
            raise ValueError(f"invalid rate_limit_kind: {rate_limit_kind!r}")
        if retry_after_seconds is not None:
            if (
                isinstance(retry_after_seconds, bool)
                or not isinstance(retry_after_seconds, (int, float))
                or not math.isfinite(retry_after_seconds)
                or retry_after_seconds < 0
            ):
                raise ValueError(
                    f"invalid retry_after_seconds: {retry_after_seconds!r}"
                )
            retry_after_seconds = float(retry_after_seconds)
        if reset_epoch is not None and (
            isinstance(reset_epoch, bool)
            or not isinstance(reset_epoch, int)
            or reset_epoch < 0
        ):
            raise ValueError(f"invalid reset_epoch: {reset_epoch!r}")
        if remaining is not None and (
            isinstance(remaining, bool)
            or not isinstance(remaining, int)
            or remaining < 0
        ):
            raise ValueError(f"invalid remaining: {remaining!r}")
        if http_status is not None and (
            isinstance(http_status, bool)
            or not isinstance(http_status, int)
            or not 100 <= http_status <= 599
        ):
            raise ValueError(f"invalid http_status: {http_status!r}")
        super().__init__(message, broker_diagnostic=broker_diagnostic)
        self.http_status = http_status
        self.failed_lookup = failed_lookup if failed_lookup is not None else endpoint_id
        self.credential_kind = credential_kind
        self.rate_limit_kind = rate_limit_kind
        self.retry_after_seconds = retry_after_seconds
        self.reset_epoch = reset_epoch
        self.remaining = remaining
        self.resource = resource
        self.endpoint_id = endpoint_id


class GitHubBrokerClientError(ReviewSenseiError):
    """Raised when the Actions workflow cannot exchange an OIDC capability."""

    error_category = "github_broker_client"

    def __init__(
        self, message: str = "", *, broker_diagnostic: BrokerDiagnostic | None = None
    ) -> None:
        super().__init__(message)
        self.broker_diagnostic = broker_diagnostic


class GitHubBrokerAdmissionHeldError(GitHubBrokerClientError):
    """Raised when the operator temporarily holds new broker admissions.

    This is a named operator state, not an automatic retry instruction.
    """

    error_category = "github_broker_admission_held"


class GitHubPublicationError(ReviewSenseiError):
    """Raised when a validated review cannot be published."""

    error_category = "github_publication"

    def __init__(
        self,
        message: str = "",
        *,
        http_status: int | None = None,
        failed_lookup: str | None = None,
        operation_diagnostics: object | None = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.failed_lookup = failed_lookup
        self.operation_diagnostics = operation_diagnostics


class GitHubPublicationTransientError(GitHubPublicationError):
    """Raised for transient review publication failures."""

    error_category = "github_publication_transient"


class GitHubLearningProposalError(ReviewSenseiError):
    """Raised when a learning proposal cannot be published safely."""

    error_category = "github_learning_proposal"


class GitHubLearningProposalTransientError(GitHubLearningProposalError):
    """Raised for transient learning proposal publication failures."""

    error_category = "github_learning_proposal_transient"


class GitHubConversationError(ReviewSenseiError):
    """Raised when a conversation reply cannot be published safely."""

    error_category = "github_conversation"


class GitHubConversationTransientError(GitHubConversationError):
    """Raised for transient conversation reply publication failures."""

    error_category = "github_conversation_transient"

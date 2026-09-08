"""Sanitized GitHub App authentication errors."""

from __future__ import annotations

from ...errors import ReviewSenseiError


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


class GitHubHTTPResponseTooLargeError(GitHubHTTPError):
    """Raised when a GitHub response exceeds the transport byte ceiling."""

    error_category = "github_http_response_too_large"


class GitHubHTTPTransientError(GitHubHTTPError):
    """Raised for transient GitHub REST failures."""

    error_category = "github_http_transient"


class GitHubBrokerClientError(ReviewSenseiError):
    """Raised when the Actions workflow cannot exchange an OIDC capability."""

    error_category = "github_broker_client"


class GitHubPublicationError(ReviewSenseiError):
    """Raised when a validated review cannot be published."""

    error_category = "github_publication"


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

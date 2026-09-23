class ReviewSenseiError(Exception):
    """Base class for expected ReviewSensei failures."""

    error_category = "unknown"


class ReviewInputError(ReviewSenseiError, ValueError):
    """Raised when a review request is not usable."""

    error_category = "input"

    def __init__(self, message: str = "", *, diagnostic: str | None = None) -> None:
        super().__init__(message)
        self.diagnostic = diagnostic


class ChunkPreflightError(ReviewInputError):
    """Raised when a chunked sub-run diff fails bounded planning preflight."""


class ReviewModeRetiredError(ReviewInputError):
    """Raised when configuration still selects the retired `legacy` engine.

    Callers that must distinguish retirement from ordinary invalid input can
    catch this subclass; the message always names the migration target.
    """


class ReviewFormatError(ReviewSenseiError, ValueError):
    """Raised when a provider response cannot be safely used as a review."""

    error_category = "format"


class LearningLoadError(ReviewSenseiError, ValueError):
    """Raised when repository-local learning data cannot be trusted."""

    error_category = "learning"


class ContextLoadError(ReviewSenseiError, ValueError):
    """Raised when repository context documents cannot be loaded safely."""

    error_category = "context"


class ProviderError(ReviewSenseiError):
    """Raised when a provider cannot produce a response."""

    error_category = "provider"

    def __init__(
        self,
        message: str = "",
        *,
        transient: bool = False,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.transient = bool(transient)
        self.retry_after_seconds = _bounded_retry_after(retry_after_seconds)


class UnknownProviderProfileError(ProviderError):
    """Raised when a named provider profile does not exist.

    Configuration callers such as ``Stage`` translate this to
    ``ReviewInputError``; routing code may catch the broader ``ProviderError``.
    """

    error_category = "provider"


class AdmissionRejected(ReviewSenseiError):
    """Raised when in-process admission cannot grant a slot."""

    error_category = "concurrency"


class AdmissionCancelled(ReviewSenseiError):
    """Raised when in-process admission waits are cancelled before a lease."""

    error_category = "concurrency"


def _bounded_retry_after(value: object) -> float | None:
    """Return a finite retry delay, or ``None`` when the hint is unusable."""

    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    seconds = float(value)
    if seconds <= 0 or seconds != seconds or seconds == float("inf"):
        return None
    return min(seconds, 60.0)

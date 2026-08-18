class ReviewSenseiError(Exception):
    """Base class for expected ReviewSensei failures."""

    error_category = "unknown"


class ReviewInputError(ReviewSenseiError, ValueError):
    """Raised when a review request is not usable."""

    error_category = "input"


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

    def __init__(self, message: str = "", *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = bool(transient)

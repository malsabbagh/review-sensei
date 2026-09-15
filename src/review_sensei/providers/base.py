from __future__ import annotations

from inspect import getattr_static
from typing import Protocol, cast, runtime_checkable

from ..errors import ProviderError
from ..models import ProviderRequest, ProviderResponse


@runtime_checkable
class ReviewProvider(Protocol):
    """Small adapter seam for any model provider."""

    name: str
    model: str | None

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        """Return the provider's text response for a review prompt."""


def validate_provider_contract(provider: object) -> ReviewProvider:
    """Validate the documented ``ReviewProvider`` contract at the registry boundary.

    The protocol requires ``name``, ``model``, and ``complete(request)``.  This
    check keeps malformed adapters from failing later, after a review has already
    been prepared.  It does not invoke the provider or inspect credentials.
    """

    for attribute in ("name", "model"):
        try:
            getattr_static(provider, attribute)
        except AttributeError:
            raise ProviderError(
                "provider must expose name, model, and complete(request)"
            ) from None
    if not callable(getattr(provider, "complete", None)):
        raise ProviderError("provider must expose name, model, and complete(request)")
    try:
        name = getattr(provider, "name")
    except Exception as exc:
        raise ProviderError("provider name could not be read") from exc
    if not isinstance(name, str) or not name.strip():
        raise ProviderError("provider name must be a non-empty string")
    try:
        model = getattr(provider, "model")
    except Exception as exc:
        raise ProviderError("provider model could not be read") from exc
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ProviderError("provider model must be a non-empty string or None")
    return cast(ReviewProvider, provider)

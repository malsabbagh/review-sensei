from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import ProviderRequest, ProviderResponse


@runtime_checkable
class ReviewProvider(Protocol):
    """Small adapter seam for any model provider."""

    name: str
    model: str | None

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        """Return the provider's text response for a review prompt."""


def validate_provider_contract(provider: object) -> ReviewProvider:
    """Validate the small provider contract at the registry boundary.

    Providers are deliberately duck-typed so downstream users can supply an
    adapter without inheriting a ReviewSensei class.  This check keeps malformed
    adapters from failing later, after a review has already been prepared.
    It does not invoke the provider or inspect credentials.
    """

    if not isinstance(provider, ReviewProvider) or not callable(
        getattr(provider, "complete", None)
    ):
        raise TypeError("provider must expose name, model, and complete(request)")
    name = provider.name
    if not isinstance(name, str) or not name.strip():
        raise TypeError("provider name must be a non-empty string")
    model = provider.model
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise TypeError("provider model must be a non-empty string or None")
    return provider

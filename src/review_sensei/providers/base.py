from __future__ import annotations

from typing import Protocol

from ..models import ProviderRequest, ProviderResponse


class ReviewProvider(Protocol):
    """Small adapter seam for any model provider."""

    name: str
    model: str | None

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        """Return the provider's text response for a review prompt."""

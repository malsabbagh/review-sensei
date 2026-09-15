from __future__ import annotations

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
    Property getters are read with ``getattr`` so exceptions become
    ``ProviderError`` instead of leaking adapter-specific exception text.
    """

    values: dict[str, object] = {}
    for attribute in ("name", "model", "complete"):
        try:
            values[attribute] = getattr(provider, attribute)
        except AttributeError:
            raise ProviderError(
                "provider must expose name, model, and complete(request)"
            ) from None
        except Exception as exc:
            raise ProviderError(f"provider {attribute} could not be read") from exc
    name = values["name"]
    if not isinstance(name, str) or not name.strip():
        raise ProviderError("provider name must be a non-empty string")
    model = values["model"]
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ProviderError("provider model must be a non-empty string or None")
    if not callable(values["complete"]):
        raise ProviderError("provider must expose name, model, and complete(request)")
    return cast(ReviewProvider, provider)

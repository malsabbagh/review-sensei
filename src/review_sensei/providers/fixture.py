from __future__ import annotations

from pathlib import Path

from ..errors import ProviderError, ReviewInputError
from ..models import ProviderRequest, ProviderResponse
from ..validation import read_bounded_utf8, validate_bounded_text


class FixtureProvider:
    """Bounded credential-free file transport for deterministic reviews."""

    name = "fixture"
    model: str | None

    def __init__(self, response_path: Path, model: str | None = "fixture-v1") -> None:
        self.response_path = Path(response_path)
        self.model = model

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        model = request.model or self.model
        try:
            validate_bounded_text(
                model,
                request.limits.max_model_bytes,
                label="fixture model",
                allow_empty=False,
            )
        except ReviewInputError as exc:
            raise ProviderError(
                "fixture model exceeds the configured size limit"
            ) from exc
        try:
            text = read_bounded_utf8(
                self.response_path,
                maximum=request.max_response_bytes,
                label="fixture response",
            )
        except ReviewInputError as exc:
            raise ProviderError("fixture response could not be read safely") from exc
        return ProviderResponse(
            text=text,
            provider=self.name,
            model=model,
            limits=request.limits,
        )

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ..errors import ProviderError, ReviewInputError
from ..models import ProviderRequest, ProviderResponse
from ..validation import validate_bounded_text


class OllamaProvider:
    """Ollama ``/api/generate`` adapter for local or hosted Ollama servers."""

    name = "ollama"
    model: str | None

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:11434/api",
        model: str = "qwen3.5:4b",
        api_key: str | None = None,
        timeout_seconds: float = 900,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        if not base_url.strip():
            raise ValueError("Ollama base_url must be non-empty")
        if not model.strip():
            raise ValueError("Ollama model must be non-empty")
        if timeout_seconds <= 0:
            raise ValueError("Ollama timeout_seconds must be positive")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self._opener = opener

    @property
    def endpoint(self) -> str:
        if self.base_url.endswith("/generate"):
            return self.base_url
        return f"{self.base_url}/generate"

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        model = request.model or self.model
        try:
            validate_bounded_text(
                model,
                request.limits.max_model_bytes,
                label="Ollama model",
                allow_empty=False,
            )
        except ReviewInputError as exc:
            raise ProviderError(
                "Ollama model exceeds the configured size limit"
            ) from exc
        payload: dict[str, object] = {
            "model": model,
            "prompt": request.prompt,
            "stream": False,
            "think": False,
        }
        if request.json_mode:
            payload["format"] = "json"

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        http_request = Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        try:
            with self._opener(http_request, timeout=self.timeout_seconds) as response:
                read_limit = request.max_response_bytes + 1
                body = bytearray()
                while len(body) <= request.max_response_bytes:
                    chunk = response.read(read_limit - len(body))
                    if not chunk:
                        break
                    if not isinstance(chunk, (bytes, bytearray)):
                        raise ProviderError("Ollama returned an invalid response body")
                    body.extend(chunk)
        except HTTPError as exc:
            raise ProviderError(f"Ollama request failed with HTTP {exc.code}") from exc
        except (TimeoutError, URLError) as exc:
            if isinstance(exc, TimeoutError) or "timed out" in str(exc).lower():
                raise ProviderError("Ollama request timed out", transient=True) from exc
            raise ProviderError("Ollama request failed") from exc
        except OSError as exc:
            if "timed out" in str(exc).lower():
                raise ProviderError("Ollama request timed out", transient=True) from exc
            raise ProviderError("Ollama request failed") from exc
        except TypeError as exc:
            raise ProviderError("Ollama request failed") from exc

        if len(body) > request.max_response_bytes:
            raise ProviderError("Ollama response exceeded the configured size limit")
        try:
            body_text = body.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ProviderError("Ollama response was not valid UTF-8") from exc
        try:
            data = json.loads(body_text)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProviderError("Ollama returned invalid JSON") from exc

        text = data.get("response") if isinstance(data, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise ProviderError("Ollama response did not contain review text")
        try:
            validate_bounded_text(
                text,
                request.max_response_bytes,
                label="Ollama review response",
                allow_empty=False,
            )
        except Exception as exc:
            raise ProviderError(
                "Ollama review response exceeded the configured size limit"
            ) from exc

        return ProviderResponse(
            text=text,
            provider=self.name,
            model=model,
            limits=request.limits,
        )

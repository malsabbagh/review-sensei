"""Explicit OpenAI-compatible Chat Completions adapter.

This adapter is intentionally standalone and has no fallback to Ollama or any
other provider.  A non-empty API key is required by the constructor and is sent
only to the configured HTTPS endpoint.
"""

from __future__ import annotations

import json
import os
import ssl
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import certifi

from ..errors import ProviderError, ReviewInputError
from ..models import ProviderRequest, ProviderResponse
from ..validation import validate_bounded_text


class OpenAICompatibleProvider:
    """Bounded adapter for ``/v1/chat/completions`` compatible APIs."""

    name = "openai-compatible"
    model: str | None

    def __init__(
        self,
        *,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o-mini",
        api_key: str | None = None,
        timeout_seconds: float = 120,
        max_output_tokens: int = 2048,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        if not base_url.strip():
            raise ValueError("OpenAI-compatible base_url must be non-empty")
        if not model.strip():
            raise ValueError("OpenAI-compatible model must be non-empty")
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("OpenAI-compatible provider requires an API key")
        if timeout_seconds <= 0:
            raise ValueError("OpenAI-compatible timeout_seconds must be positive")
        if isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int):
            raise ValueError("OpenAI-compatible max_output_tokens must be a positive integer")
        if max_output_tokens < 1 or max_output_tokens > 16_384:
            raise ValueError("OpenAI-compatible max_output_tokens exceeds the configured limit")
        if not base_url.lower().startswith("https://"):
            raise ValueError("OpenAI-compatible endpoint must use HTTPS")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens = max_output_tokens
        self._opener = opener

    @property
    def endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        model = request.model or self.model
        try:
            validate_bounded_text(
                model,
                request.limits.max_model_bytes,
                label="OpenAI-compatible model",
                allow_empty=False,
            )
        except ReviewInputError as exc:
            raise ProviderError(
                "OpenAI-compatible model exceeds the configured size limit"
            ) from exc

        payload: dict[str, object] = {
            "model": model,
            "messages": [{"role": "user", "content": request.prompt}],
            "stream": False,
            "max_tokens": self.max_output_tokens,
        }
        if request.json_mode:
            payload["response_format"] = {"type": "json_object"}
        http_request = Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

        try:
            open_kwargs: dict[str, object] = {"timeout": self.timeout_seconds}
            cafile = os.getenv("SSL_CERT_FILE") or certifi.where()
            open_kwargs["context"] = ssl.create_default_context(cafile=cafile)
            with self._opener(http_request, **open_kwargs) as response:
                read_limit = request.max_response_bytes + 1
                body = bytearray()
                while len(body) <= request.max_response_bytes:
                    chunk = response.read(read_limit - len(body))
                    if not chunk:
                        break
                    if not isinstance(chunk, (bytes, bytearray)):
                        raise ProviderError("OpenAI-compatible response body is invalid")
                    body.extend(chunk)
        except HTTPError as exc:
            raise ProviderError(
                f"OpenAI-compatible request failed with HTTP {exc.code}"
            ) from exc
        except (TimeoutError, URLError) as exc:
            if isinstance(exc, TimeoutError) or "timed out" in str(exc).lower():
                raise ProviderError(
                    "OpenAI-compatible request timed out", transient=True
                ) from exc
            raise ProviderError("OpenAI-compatible request failed") from exc
        except OSError as exc:
            if "timed out" in str(exc).lower():
                raise ProviderError(
                    "OpenAI-compatible request timed out", transient=True
                ) from exc
            raise ProviderError("OpenAI-compatible request failed") from exc
        except TypeError as exc:
            raise ProviderError("OpenAI-compatible request failed") from exc

        if len(body) > request.max_response_bytes:
            raise ProviderError("OpenAI-compatible response exceeded the configured size limit")
        try:
            body_text = body.decode("utf-8", errors="strict")
            data = json.loads(body_text)
        except UnicodeDecodeError as exc:
            raise ProviderError("OpenAI-compatible response was not valid UTF-8") from exc
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProviderError("OpenAI-compatible response was not valid JSON") from exc

        text: object = None
        if isinstance(data, dict):
            choices = data.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                message = choices[0].get("message")
                if isinstance(message, dict):
                    text = message.get("content")
        if not isinstance(text, str) or not text.strip():
            raise ProviderError("OpenAI-compatible response did not contain review text")
        try:
            validate_bounded_text(
                text,
                request.max_response_bytes,
                label="OpenAI-compatible review response",
                allow_empty=False,
            )
        except Exception as exc:
            raise ProviderError(
                "OpenAI-compatible review response exceeded the configured size limit"
            ) from exc
        return ProviderResponse(text=text, provider=self.name, model=model, limits=request.limits)


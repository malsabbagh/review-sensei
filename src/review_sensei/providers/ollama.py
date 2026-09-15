from __future__ import annotations

import json
import os
import ssl
import unicodedata
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import (
    HTTPHandler,
    HTTPRedirectHandler,
    HTTPSHandler,
    Request,
    build_opener,
    urlopen,
)

import certifi

from ..errors import ProviderError, ReviewInputError
from ..models import ProviderRequest, ProviderResponse
from ..validation import validate_bounded_text

MAX_API_KEY_BYTES = 4_096


class _NoRedirect(HTTPRedirectHandler):
    """Reject redirects before urllib can replay a credentialed request."""

    def redirect_request(self, req, fp, code, msg, headers, new):
        raise ProviderError("Ollama endpoint redirected")


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
        max_output_tokens: int | None = None,
        allow_model_override: bool = True,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        if not base_url.strip():
            raise ValueError("Ollama base_url must be non-empty")
        if not model.strip():
            raise ValueError("Ollama model must be non-empty")
        if timeout_seconds <= 0:
            raise ValueError("Ollama timeout_seconds must be positive")
        if api_key is not None:
            if not isinstance(api_key, str):
                raise ValueError("Ollama api_key must be a string")
            try:
                validate_bounded_text(
                    api_key,
                    MAX_API_KEY_BYTES,
                    label="Ollama API key",
                    allow_empty=False,
                )
            except ReviewInputError as exc:
                raise ValueError(
                    "Ollama API key exceeds the configured size limit"
                ) from exc
            if any(
                unicodedata.category(character) in {"Cc", "Cf", "Cs"}
                for character in api_key
            ):
                raise ValueError("Ollama API key must not contain control characters")
            try:
                api_key.encode("ascii")
            except UnicodeEncodeError as exc:
                raise ValueError(
                    "Ollama API key must contain only ASCII characters"
                ) from exc
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens = max_output_tokens
        self.allow_model_override = allow_model_override
        self._opener = opener
        self._ssl_context: ssl.SSLContext | None = None
        if self.base_url.lower().startswith("https://"):
            # Standalone PyInstaller bundles do not inherit a usable system
            # CA path on every supported host. Prefer an explicit operator
            # override, then the bundled certifi roots, while retaining
            # normal certificate verification.
            cafile = os.getenv("SSL_CERT_FILE") or certifi.where()
            try:
                self._ssl_context = ssl.create_default_context(cafile=cafile)
            except (OSError, ssl.SSLError) as exc:
                raise ProviderError(
                    "Ollama TLS trust store could not be loaded"
                ) from exc
        self._safe_opener: Any | None = None
        if opener is urlopen:
            handlers: list[Any] = [_NoRedirect()]
            if self._ssl_context is not None:
                handlers.append(HTTPSHandler(context=self._ssl_context))
            else:
                handlers.append(HTTPHandler())
            self._safe_opener = build_opener(*handlers)

    def _call_custom_opener(self, http_request: Request) -> Any:
        """Invoke an injected opener, falling back when kwargs are unsupported."""

        opener = getattr(self._opener, "open", self._opener)
        try:
            kwargs: dict[str, object] = {"timeout": self.timeout_seconds}
            if self._ssl_context is not None:
                kwargs["context"] = self._ssl_context
            return opener(http_request, **kwargs)
        except TypeError:
            try:
                return opener(http_request, timeout=self.timeout_seconds)
            except TypeError:
                return opener(http_request)

    @property
    def endpoint(self) -> str:
        if self.base_url.endswith("/generate"):
            return self.base_url
        return f"{self.base_url}/generate"

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        model = (request.model if self.allow_model_override else None) or self.model
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
        if self.max_output_tokens is not None:
            payload["options"] = {"num_predict": self.max_output_tokens}

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
            if self._opener is urlopen:
                assert self._safe_opener is not None
                response_ctx = self._safe_opener.open(
                    http_request, timeout=self.timeout_seconds
                )
            else:
                response_ctx = self._call_custom_opener(http_request)
            with response_ctx as response:
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
        except ProviderError as exc:
            # Keep errors from an injected transport from reflecting a bearer
            # token supplied by the caller.  The built-in redirect error is
            # already generic, but this guard preserves that property for
            # custom openers too.
            rendered = str(exc)
            if self.api_key and (
                self.api_key in rendered
                or self.api_key.casefold() in rendered.casefold()
            ):
                raise ProviderError("Ollama request failed") from exc
            raise
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
        except ReviewInputError as exc:
            raise ProviderError(
                "Ollama review response exceeded the configured size limit"
            ) from exc

        return ProviderResponse(
            text=text,
            provider=self.name,
            model=model,
            limits=request.limits,
        )

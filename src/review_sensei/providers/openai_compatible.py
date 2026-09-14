"""Explicit OpenAI-compatible Chat Completions adapter.

This adapter is intentionally standalone and has no fallback to Ollama or any
other provider.  A non-empty API key is required by the constructor and is sent
only to the configured HTTPS endpoint.
"""

from __future__ import annotations

import inspect
import json
import math
import os
import ssl
import unicodedata
from collections.abc import Callable, Mapping
from http.client import HTTPException
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    Request,
    build_opener,
    urlopen,
)

from ..errors import ProviderError, ReviewInputError
from ..models import ProviderRequest, ProviderResponse
from ..validation import validate_bounded_text

# Provider credentials are untrusted configuration input.  Keep a generous
# but finite ceiling so a malformed environment value cannot become an
# unbounded request header, and reject all Unicode control/format/surrogate
# code points before constructing ``Authorization``.
MAX_API_KEY_BYTES = 4_096


class _NoRedirect(HTTPRedirectHandler):
    """Reject redirects so the configured bearer token cannot be forwarded."""

    def redirect_request(self, req, fp, code, msg, headers, new):
        raise ProviderError("OpenAI-compatible endpoint redirected")


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
        allow_model_override: bool = True,
        allow_custom_endpoint: bool = False,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("OpenAI-compatible base_url must be non-empty")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("OpenAI-compatible model must be non-empty")
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("OpenAI-compatible provider requires an API key")
        try:
            validate_bounded_text(
                api_key,
                MAX_API_KEY_BYTES,
                label="OpenAI-compatible API key",
                allow_empty=False,
            )
        except ReviewInputError as exc:
            raise ValueError(
                "OpenAI-compatible API key exceeds the configured size limit"
            ) from exc
        if any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs"}
            for character in api_key
        ):
            raise ValueError(
                "OpenAI-compatible API key contains a forbidden control character"
            )
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError(
                "OpenAI-compatible timeout_seconds must be a finite positive number"
            )
        if isinstance(max_output_tokens, bool) or not isinstance(
            max_output_tokens, int
        ):
            raise ValueError(
                "OpenAI-compatible max_output_tokens must be a positive integer"
            )
        if max_output_tokens < 1 or max_output_tokens > 16_384:
            raise ValueError(
                "OpenAI-compatible max_output_tokens exceeds the configured limit"
            )
        if not isinstance(allow_custom_endpoint, bool):
            raise ValueError("allow_custom_endpoint must be a boolean")
        parsed = urlsplit(base_url)
        if parsed.scheme.casefold() != "https" or parsed.hostname is None:
            raise ValueError("OpenAI-compatible endpoint must use HTTPS")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("OpenAI-compatible endpoint must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError(
                "OpenAI-compatible endpoint must not contain a query or fragment"
            )
        try:
            hostname = parsed.hostname.encode("idna").decode("ascii").casefold()
        except UnicodeError as exc:
            raise ValueError("OpenAI-compatible endpoint host is invalid") from exc
        if not allow_custom_endpoint and hostname != "api.openai.com":
            raise ValueError(
                "OpenAI-compatible endpoint is not allowlisted; pass "
                "allow_custom_endpoint=True only for an explicitly trusted service"
            )
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("OpenAI-compatible endpoint port is invalid") from exc
        if port is not None and port != 443 and not allow_custom_endpoint:
            raise ValueError(
                "OpenAI-compatible endpoint must use the default HTTPS port"
            )
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens = max_output_tokens
        self.allow_model_override = allow_model_override
        self.allow_custom_endpoint = allow_custom_endpoint
        self._opener = opener
        self._ssl_context = self._build_ssl_context()
        self._safe_opener = build_opener(
            _NoRedirect(), HTTPSHandler(context=self._ssl_context)
        )

    @staticmethod
    def _build_ssl_context() -> ssl.SSLContext:
        """Build one verified context, using only an explicit CA override."""

        configured = os.getenv("SSL_CERT_FILE")
        if configured:
            try:
                cert_path = Path(configured)
                if cert_path.is_symlink() or not cert_path.is_file():
                    raise ProviderError(
                        "configured SSL_CERT_FILE is not a regular file"
                    )
            except OSError as exc:
                raise ProviderError("configured SSL_CERT_FILE is unavailable") from exc
            try:
                return ssl.create_default_context(cafile=str(cert_path))
            except (OSError, ssl.SSLError) as exc:
                raise ProviderError(
                    "configured SSL_CERT_FILE could not be loaded"
                ) from exc
        try:
            return ssl.create_default_context()
        except (OSError, ssl.SSLError) as exc:
            raise ProviderError("the system CA store could not be loaded") from exc

    @property
    def endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    def _call_custom_opener(self, http_request: Request) -> Any:
        """Invoke an injected opener using only kwargs it declares.

        The default transport is always the no-redirect opener created at
        construction.  Injected openers are an explicit seam for tests and
        callers that own their transport policy; introspection avoids turning a
        harmless signature difference into an opaque response-read failure.
        """

        opener = getattr(self._opener, "open", self._opener)
        kwargs: dict[str, object] = {}
        positional: list[object] = [http_request]
        parameters: Mapping[str, inspect.Parameter]
        try:
            parameters = inspect.signature(opener).parameters
        except (TypeError, ValueError):
            parameters = {}
        accepts_var_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        for name, value in (
            ("timeout", self.timeout_seconds),
            ("context", self._ssl_context),
        ):
            parameter = parameters.get(name)
            if accepts_var_kwargs or (
                parameter is not None
                and parameter.kind is not inspect.Parameter.POSITIONAL_ONLY
            ):
                kwargs[name] = value
            elif (
                parameter is not None
                and parameter.kind is inspect.Parameter.POSITIONAL_ONLY
            ):
                positional.append(value)
        try:
            return opener(*positional, **kwargs)
        except TypeError as exc:
            # This catches only errors raised while invoking the opener.  The
            # response reader below handles its own type errors separately.
            raise ProviderError("OpenAI-compatible opener could not be called") from exc

    @staticmethod
    def _read_bounded_body(response: Any, maximum: int) -> bytearray:
        body = bytearray()
        while True:
            # Read one byte beyond the ceiling so an exact-limit response can
            # be accepted while an oversized response fails closed.  Check the
            # chunk length before extending the buffer because some injected
            # responses ignore the requested size.
            requested = maximum - len(body) + 1
            try:
                chunk = response.read(requested)
            except TypeError as exc:
                raise ProviderError(
                    "OpenAI-compatible response body could not be read"
                ) from exc
            if not chunk:
                break
            if not isinstance(chunk, (bytes, bytearray)):
                raise ProviderError("OpenAI-compatible response body is invalid")
            if len(chunk) > requested or len(body) + len(chunk) > maximum:
                raise ProviderError(
                    "OpenAI-compatible response exceeded the configured size limit"
                )
            body.extend(chunk)
        return body

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        model = (request.model if self.allow_model_override else None) or self.model
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
            if self._opener is urlopen:
                with self._safe_opener.open(
                    http_request, timeout=self.timeout_seconds
                ) as response:
                    body = self._read_bounded_body(response, request.max_response_bytes)
            else:
                with self._call_custom_opener(http_request) as response:
                    body = self._read_bounded_body(response, request.max_response_bytes)
        except HTTPError as exc:
            raise ProviderError(
                f"OpenAI-compatible request failed with HTTP {exc.code}"
            ) from exc
        except (TimeoutError, URLError) as exc:
            if isinstance(exc, TimeoutError) or "timed out" in str(exc).lower():
                raise ProviderError(
                    "OpenAI-compatible request timed out", transient=True
                ) from exc
            # urllib reports DNS, connection, and proxy failures as URLError.
            # They are retryable transport failures even when they are not
            # phrased as a timeout; do not leak the underlying reason.
            raise ProviderError(
                "OpenAI-compatible request failed", transient=True
            ) from exc
        except HTTPException as exc:
            if "timed out" in str(exc).lower():
                raise ProviderError(
                    "OpenAI-compatible request timed out", transient=True
                ) from exc
            raise ProviderError("OpenAI-compatible request failed") from exc
        except TypeError as exc:
            # A custom transport that does not return a context manager is a
            # transport-contract failure, not a provider implementation leak.
            # Response-reader TypeErrors are normalized inside
            # ``_read_bounded_body`` before reaching this handler.
            raise ProviderError(
                "OpenAI-compatible response could not be opened"
            ) from exc
        except OSError as exc:
            if "timed out" in str(exc).lower():
                raise ProviderError(
                    "OpenAI-compatible request timed out", transient=True
                ) from exc
            raise ProviderError("OpenAI-compatible request failed") from exc
        try:
            body_text = body.decode("utf-8", errors="strict")
            data = json.loads(body_text)
        except UnicodeDecodeError as exc:
            raise ProviderError(
                "OpenAI-compatible response was not valid UTF-8"
            ) from exc
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProviderError(
                "OpenAI-compatible response was not valid JSON"
            ) from exc

        text: object = None
        if isinstance(data, dict):
            choices = data.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                message = choices[0].get("message")
                if isinstance(message, dict):
                    text = message.get("content")
        if not isinstance(text, str) or not text.strip():
            raise ProviderError(
                "OpenAI-compatible response did not contain review text"
            )
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
        return ProviderResponse(
            text=text, provider=self.name, model=model, limits=request.limits
        )

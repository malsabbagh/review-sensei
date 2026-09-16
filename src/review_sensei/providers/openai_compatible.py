"""Explicit OpenAI-compatible Chat Completions adapter.

This adapter is intentionally standalone and has no fallback to Ollama or any
other provider.  A non-empty API key is required by the constructor and is sent
only to the configured HTTPS endpoint.
"""

from __future__ import annotations

import errno
import http.client
import json
import math
import os
import ssl
import stat
import unicodedata
from collections.abc import Callable
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

import certifi

from ..errors import ProviderError, ReviewInputError
from ..models import ProviderRequest, ProviderResponse
from ..validation import validate_bounded_text
from .transport import (
    parse_retry_after_seconds,
    read_bounded_body,
    urllib_error_is_transient,
)

# Provider credentials are untrusted configuration input.  Keep a generous
# but finite ceiling so a malformed environment value cannot become an
# unbounded request header, and reject all Unicode control/format/surrogate
# code points before constructing ``Authorization``.
MAX_API_KEY_BYTES = 4_096
MAX_CA_BUNDLE_BYTES = 1_048_576
ALLOWLISTED_OPENAI_HOSTNAME = "api.openai.com"


def _openai_endpoint_has_userinfo(parsed: Any) -> bool:
    """Return whether a parsed URL carries userinfo in ``netloc``."""

    return "@" in parsed.netloc or bool(parsed.username) or bool(parsed.password)


def is_allowlisted_openai_compatible_endpoint(base_url: str) -> bool:
    """Return whether ``base_url`` is the built-in OpenAI Chat Completions host."""

    if not isinstance(base_url, str) or not base_url.strip():
        return False
    parsed = urlsplit(base_url)
    if parsed.scheme.casefold() != "https" or parsed.hostname is None:
        return False
    if _openai_endpoint_has_userinfo(parsed):
        return False
    if parsed.query or parsed.fragment:
        return False
    try:
        hostname = parsed.hostname.encode("idna").decode("ascii").casefold()
        port = parsed.port
    except (UnicodeError, ValueError):
        return False
    path = parsed.path.rstrip("/") or "/"
    return (
        hostname == ALLOWLISTED_OPENAI_HOSTNAME
        and (port is None or port == 443)
        and (path == "/v1" or path.startswith("/v1/"))
    )


class _NoRedirect(HTTPRedirectHandler):
    """Reject redirects so the configured bearer token cannot be forwarded."""

    def redirect_request(self, req, fp, code, msg, headers, new):
        raise ProviderError("OpenAI-compatible endpoint redirected")


class _VerifiedHTTPSHandler(HTTPSHandler):
    """Verify TLS against the configured hostname, not only the resolved peer."""

    def __init__(self, context: ssl.SSLContext, *, expected_hostname: str) -> None:
        super().__init__(context=context)
        self._expected_hostname = expected_hostname
        self._verified_context = context

    def https_open(self, req: Request) -> http.client.HTTPResponse:
        verified_context = self._verified_context
        expected_hostname = self._expected_hostname

        def connection_factory(
            host: str,
            port: int | None = 443,
            timeout: float | object = object(),
            **kwargs: object,
        ) -> http.client.HTTPSConnection:
            return http.client.HTTPSConnection(
                host,
                port=port or 443,
                timeout=timeout,
                context=verified_context,
                server_hostname=expected_hostname,  # type: ignore[call-overload]
            )

        return self.do_open(connection_factory, req)


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
        try:
            api_key.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError(
                "OpenAI-compatible API key must contain only ASCII characters"
            ) from exc
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
        if _openai_endpoint_has_userinfo(parsed):
            raise ValueError("OpenAI-compatible endpoint must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError(
                "OpenAI-compatible endpoint must not contain a query or fragment"
            )
        try:
            hostname = parsed.hostname.encode("idna").decode("ascii").casefold()
        except UnicodeError as exc:
            raise ValueError("OpenAI-compatible endpoint host is invalid") from exc
        path = parsed.path.rstrip("/") or "/"
        if not allow_custom_endpoint and (
            hostname != "api.openai.com"
            or not (path == "/v1" or path.startswith("/v1/"))
        ):
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
        self._expected_hostname = hostname
        self._opener = opener
        self._ssl_context = self._build_ssl_context()
        self._safe_opener = build_opener(
            _NoRedirect(),
            _VerifiedHTTPSHandler(
                self._ssl_context, expected_hostname=self._expected_hostname
            ),
        )

    @staticmethod
    def _load_ca_bundle(cert_path: Path) -> ssl.SSLContext:
        """Load a CA bundle from a regular file without following symlinks."""

        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(str(cert_path), flags)
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                raise ProviderError("configured SSL_CERT_FILE is missing") from exc
            if exc.errno in {errno.EACCES, errno.EPERM}:
                raise ProviderError("configured SSL_CERT_FILE is unreadable") from exc
            if exc.errno == errno.ELOOP:
                raise ProviderError(
                    "configured SSL_CERT_FILE is not a regular file"
                ) from exc
            raise ProviderError("configured SSL_CERT_FILE is unavailable") from exc
        try:
            try:
                file_stat_before = os.fstat(fd)
                if not stat.S_ISREG(file_stat_before.st_mode):
                    raise ProviderError(
                        "configured SSL_CERT_FILE is not a regular file"
                    )
                with os.fdopen(fd, "rb") as handle:
                    fd = -1
                    contents = handle.read(MAX_CA_BUNDLE_BYTES + 1)
                    file_stat_after = os.fstat(handle.fileno())
                if (
                    file_stat_before.st_dev != file_stat_after.st_dev
                    or file_stat_before.st_ino != file_stat_after.st_ino
                ):
                    raise ProviderError("configured SSL_CERT_FILE changed during read")
                if len(contents) > MAX_CA_BUNDLE_BYTES:
                    raise ProviderError(
                        "configured SSL_CERT_FILE exceeds the configured size limit"
                    )
            finally:
                if fd >= 0:
                    os.close(fd)
        except ProviderError:
            raise
        except OSError as exc:
            raise ProviderError("configured SSL_CERT_FILE is unavailable") from exc
        try:
            context = ssl.create_default_context()
            context.load_verify_locations(cadata=contents.decode("ascii"))
            return context
        except (OSError, ssl.SSLError, UnicodeDecodeError) as exc:
            raise ProviderError("configured SSL_CERT_FILE could not be loaded") from exc

    @staticmethod
    def _build_ssl_context() -> ssl.SSLContext:
        """Build one verified context, using only an explicit CA override."""

        configured = os.getenv("SSL_CERT_FILE")
        if configured:
            context = OpenAICompatibleProvider._load_ca_bundle(Path(configured))
        else:
            # Standalone PyInstaller bundles do not inherit a usable system CA path
            # on every supported host. Prefer certifi after an explicit override.
            try:
                context = ssl.create_default_context(cafile=certifi.where())
            except (OSError, ssl.SSLError) as exc:
                raise ProviderError(
                    "OpenAI-compatible TLS trust store could not be loaded"
                ) from exc
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        return context

    @property
    def endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    def _call_custom_opener(self, http_request: Request) -> Any:
        """Invoke an injected opener with the verified TLS context."""

        opener = getattr(self._opener, "open", self._opener)
        try:
            return opener(
                http_request,
                timeout=self.timeout_seconds,
                context=self._ssl_context,
            )
        except TypeError as exc:
            raise ProviderError(
                "OpenAI-compatible opener must accept timeout and context kwargs"
            ) from exc

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
            # The adapter returns message.content as text; ReviewService validates JSON.
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
                response_ctx = self._safe_opener.open(
                    http_request, timeout=self.timeout_seconds
                )
            else:
                response_ctx = self._call_custom_opener(http_request)
            with response_ctx as response:
                body = read_bounded_body(
                    response,
                    request.max_response_bytes,
                    label="OpenAI-compatible response",
                )
        except ProviderError:
            raise
        except HTTPError as exc:
            transient = exc.code == 429 or 500 <= exc.code < 600
            raise ProviderError(
                f"OpenAI-compatible request failed with HTTP {exc.code}",
                transient=transient,
                retry_after_seconds=parse_retry_after_seconds(exc),
            ) from exc
        except (TimeoutError, URLError) as exc:
            if isinstance(exc, HTTPError):
                transient = exc.code == 429 or 500 <= exc.code < 600
                raise ProviderError(
                    f"OpenAI-compatible request failed with HTTP {exc.code}",
                    transient=transient,
                ) from exc
            if isinstance(exc, TimeoutError) or "timed out" in str(exc).lower():
                raise ProviderError(
                    "OpenAI-compatible request timed out", transient=True
                ) from exc
            transient = isinstance(exc, URLError) and urllib_error_is_transient(exc)
            raise ProviderError(
                "OpenAI-compatible request failed", transient=transient
            ) from exc
        except TypeError as exc:
            # Context-manager protocol and builtin opener TypeErrors only.
            # Custom opener TypeErrors are normalized in ``_call_custom_opener``;
            # reader TypeErrors are normalized in ``read_bounded_body``.
            raise ProviderError(
                "OpenAI-compatible response could not be opened"
            ) from exc
        except OSError as exc:
            if "timed out" in str(exc).lower():
                raise ProviderError(
                    "OpenAI-compatible request timed out", transient=True
                ) from exc
            if isinstance(exc, ConnectionError):
                raise ProviderError(
                    "OpenAI-compatible request failed", transient=True
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
        except ReviewInputError as exc:
            raise ProviderError(
                "OpenAI-compatible review response exceeded the configured size limit"
            ) from exc
        revision = None
        if isinstance(data, dict):
            fingerprint = data.get("system_fingerprint")
            observed_model = data.get("model")
            candidate = (
                fingerprint
                if isinstance(fingerprint, str) and fingerprint.strip()
                else observed_model
            )
            if isinstance(candidate, str) and candidate.strip():
                try:
                    validate_bounded_text(
                        candidate.strip(),
                        request.limits.max_revision_bytes,
                        label="OpenAI-compatible observed revision",
                        allow_empty=False,
                    )
                except ReviewInputError as exc:
                    raise ProviderError(
                        "OpenAI-compatible observed revision exceeded the "
                        "configured size limit"
                    ) from exc
                revision = candidate.strip()
        return ProviderResponse(
            text=text,
            provider=self.name,
            model=model,
            limits=request.limits,
            revision=revision,
        )

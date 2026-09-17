"""Explicit OpenRouter Chat Completions adapter with bounded routing policy.

This adapter is intentionally standalone.  It sends requests only to the
canonical OpenRouter HTTPS API and applies a typed, immutable routing and
privacy policy.  It does not fall back to Ollama, OpenAI, or any other
provider.
"""

from __future__ import annotations

import errno
import http.client
import json
import math
import os
import re
import ssl
import stat
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
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

MAX_API_KEY_BYTES = 4_096
MAX_CA_BUNDLE_BYTES = 1_048_576
MAX_UPSTREAM_PROVIDER_BYTES = 128
ALLOWLISTED_OPENROUTER_HOSTNAME = "openrouter.ai"
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_APP_REFERER = "https://reviewsensei.dev"
DEFAULT_APP_TITLE = "ReviewSensei"
_UPSTREAM_PROVIDER_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _endpoint_has_userinfo(parsed: Any) -> bool:
    return "@" in parsed.netloc or bool(parsed.username) or bool(parsed.password)


def is_allowlisted_openrouter_endpoint(base_url: str) -> bool:
    """Return whether ``base_url`` is the built-in OpenRouter Chat Completions host."""

    if not isinstance(base_url, str) or not base_url.strip():
        return False
    parsed = urlsplit(base_url)
    if parsed.scheme.casefold() != "https" or parsed.hostname is None:
        return False
    if _endpoint_has_userinfo(parsed):
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
        hostname == ALLOWLISTED_OPENROUTER_HOSTNAME
        and (port is None or port == 443)
        and (path == "/api/v1" or path.startswith("/api/v1/"))
    )


@dataclass(frozen=True)
class OpenRouterRoutingPolicy:
    """Immutable OpenRouter upstream routing and privacy constraints."""

    upstream_provider: str

    def __post_init__(self) -> None:
        try:
            validate_bounded_text(
                self.upstream_provider,
                MAX_UPSTREAM_PROVIDER_BYTES,
                label="OpenRouter upstream provider",
                allow_empty=False,
            )
        except ReviewInputError as exc:
            raise ValueError(
                "OpenRouter upstream provider exceeds the configured size limit"
            ) from exc
        slug = self.upstream_provider.strip().casefold()
        if slug != self.upstream_provider:
            raise ValueError("OpenRouter upstream provider must be normalized")
        if not _UPSTREAM_PROVIDER_SLUG.fullmatch(slug):
            raise ValueError("OpenRouter upstream provider slug is invalid")

    def to_request_provider(self) -> dict[str, object]:
        return {
            "order": [self.upstream_provider],
            "allow_fallbacks": False,
            "require_parameters": True,
            "data_collection": "deny",
            "zdr": True,
        }

    def identity_fields(self) -> Mapping[str, object]:
        """Return non-secret fields for configuration identity digests."""

        return {
            "upstream_provider": self.upstream_provider,
            "allow_fallbacks": False,
            "require_parameters": True,
            "data_collection": "deny",
            "zdr": True,
        }


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, new):
        raise ProviderError("OpenRouter endpoint redirected")


class _VerifiedHTTPSHandler(HTTPSHandler):
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


def _http_failure_is_transient(code: int) -> bool:
    return code in {408, 429} or 500 <= code < 600


class OpenRouterProvider:
    """Bounded adapter for OpenRouter ``/v1/chat/completions``."""

    name = "openrouter"
    model: str | None

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_OPENROUTER_BASE_URL,
        model: str,
        api_key: str,
        routing_policy: OpenRouterRoutingPolicy,
        timeout_seconds: float = 120,
        max_output_tokens: int = 2048,
        allow_model_override: bool = True,
        app_referer: str = DEFAULT_APP_REFERER,
        app_title: str = DEFAULT_APP_TITLE,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("OpenRouter base_url must be non-empty")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("OpenRouter model must be non-empty")
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("OpenRouter provider requires an API key")
        if not isinstance(routing_policy, OpenRouterRoutingPolicy):
            raise ValueError(
                "OpenRouter routing_policy must be an OpenRouterRoutingPolicy"
            )
        try:
            validate_bounded_text(
                api_key,
                MAX_API_KEY_BYTES,
                label="OpenRouter API key",
                allow_empty=False,
            )
        except ReviewInputError as exc:
            raise ValueError(
                "OpenRouter API key exceeds the configured size limit"
            ) from exc
        if any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs"}
            for character in api_key
        ):
            raise ValueError(
                "OpenRouter API key contains a forbidden control character"
            )
        try:
            api_key.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError(
                "OpenRouter API key must contain only ASCII characters"
            ) from exc
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError(
                "OpenRouter timeout_seconds must be a finite positive number"
            )
        if isinstance(max_output_tokens, bool) or not isinstance(
            max_output_tokens, int
        ):
            raise ValueError("OpenRouter max_output_tokens must be a positive integer")
        if max_output_tokens < 1 or max_output_tokens > 16_384:
            raise ValueError(
                "OpenRouter max_output_tokens exceeds the configured limit"
            )
        if not is_allowlisted_openrouter_endpoint(base_url):
            raise ValueError("OpenRouter endpoint is not allowlisted")
        parsed = urlsplit(base_url)
        if parsed.hostname is None:
            raise ValueError("OpenRouter endpoint host is invalid")
        try:
            hostname = parsed.hostname.encode("idna").decode("ascii").casefold()
        except UnicodeError as exc:
            raise ValueError("OpenRouter endpoint host is invalid") from exc
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.routing_policy = routing_policy
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens = max_output_tokens
        self.allow_model_override = allow_model_override
        self.app_referer = app_referer
        self.app_title = app_title
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
        configured = os.getenv("SSL_CERT_FILE")
        if configured:
            context = OpenRouterProvider._load_ca_bundle(Path(configured))
        else:
            try:
                context = ssl.create_default_context(cafile=certifi.where())
            except (OSError, ssl.SSLError) as exc:
                raise ProviderError(
                    "OpenRouter TLS trust store could not be loaded"
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
        opener = getattr(self._opener, "open", self._opener)
        try:
            return opener(
                http_request,
                timeout=self.timeout_seconds,
                context=self._ssl_context,
            )
        except TypeError as exc:
            raise ProviderError(
                "OpenRouter opener must accept timeout and context kwargs"
            ) from exc

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        model = (request.model if self.allow_model_override else None) or self.model
        try:
            validate_bounded_text(
                model,
                request.limits.max_model_bytes,
                label="OpenRouter model",
                allow_empty=False,
            )
        except ReviewInputError as exc:
            raise ProviderError(
                "OpenRouter model exceeds the configured size limit"
            ) from exc

        payload: dict[str, object] = {
            "model": model,
            "messages": [{"role": "user", "content": request.prompt}],
            "stream": False,
            "max_tokens": self.max_output_tokens,
            "provider": self.routing_policy.to_request_provider(),
        }
        if request.json_mode:
            payload["response_format"] = {"type": "json_object"}
        http_request = Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "Referer": self.app_referer,
                "X-OpenRouter-Title": self.app_title,
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
                    label="OpenRouter response",
                )
        except ProviderError:
            raise
        except HTTPError as exc:
            raise ProviderError(
                f"OpenRouter request failed with HTTP {exc.code}",
                transient=_http_failure_is_transient(exc.code),
                retry_after_seconds=parse_retry_after_seconds(exc),
            ) from exc
        except (TimeoutError, URLError) as exc:
            if isinstance(exc, HTTPError):
                raise ProviderError(
                    f"OpenRouter request failed with HTTP {exc.code}",
                    transient=_http_failure_is_transient(exc.code),
                ) from exc
            if isinstance(exc, TimeoutError) or "timed out" in str(exc).lower():
                raise ProviderError(
                    "OpenRouter request timed out", transient=True
                ) from exc
            transient = isinstance(exc, URLError) and urllib_error_is_transient(exc)
            raise ProviderError(
                "OpenRouter request failed", transient=transient
            ) from exc
        except TypeError as exc:
            raise ProviderError("OpenRouter response could not be opened") from exc
        except OSError as exc:
            if "timed out" in str(exc).lower():
                raise ProviderError(
                    "OpenRouter request timed out", transient=True
                ) from exc
            if isinstance(exc, ConnectionError):
                raise ProviderError(
                    "OpenRouter request failed", transient=True
                ) from exc
            raise ProviderError("OpenRouter request failed") from exc

        try:
            body_text = body.decode("utf-8", errors="strict")
            data = json.loads(body_text)
        except UnicodeDecodeError as exc:
            raise ProviderError("OpenRouter response was not valid UTF-8") from exc
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProviderError("OpenRouter response was not valid JSON") from exc

        if isinstance(data, dict) and data.get("error") is not None:
            raise ProviderError("OpenRouter response reported an error")

        text: object = None
        finish_reason: object = None
        if isinstance(data, dict):
            choices = data.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                choice = choices[0]
                finish_reason = choice.get("finish_reason")
                message = choice.get("message")
                if isinstance(message, dict):
                    refusal = message.get("refusal")
                    if isinstance(refusal, str) and refusal.strip():
                        raise ProviderError("OpenRouter response was refused")
                    text = message.get("content")
        if finish_reason == "length":
            raise ProviderError("OpenRouter response was truncated")
        if not isinstance(text, str) or not text.strip():
            raise ProviderError("OpenRouter response did not contain review text")
        try:
            validate_bounded_text(
                text,
                request.max_response_bytes,
                label="OpenRouter review response",
                allow_empty=False,
            )
        except ReviewInputError as exc:
            raise ProviderError(
                "OpenRouter review response exceeded the configured size limit"
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
                        label="OpenRouter observed revision",
                        allow_empty=False,
                    )
                except ReviewInputError as exc:
                    raise ProviderError(
                        "OpenRouter observed revision exceeded the configured size limit"
                    ) from exc
                revision = candidate.strip()
        return ProviderResponse(
            text=text,
            provider=self.name,
            model=model,
            limits=request.limits,
            revision=revision,
        )


__all__ = [
    "DEFAULT_OPENROUTER_BASE_URL",
    "OpenRouterProvider",
    "OpenRouterRoutingPolicy",
    "is_allowlisted_openrouter_endpoint",
]

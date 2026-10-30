from __future__ import annotations

import errno
import http.client
import math
import re
import socket
import ssl
from threading import TIMEOUT_MAX
from typing import Any
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, Request

from ..errors import ProviderError

_TRANSIENT_NETWORK_ERRNOS = frozenset(
    {
        errno.ECONNREFUSED,
        errno.ECONNRESET,
        errno.ETIMEDOUT,
        errno.EHOSTUNREACH,
        errno.ENETUNREACH,
    }
)
# Bound configured values before a request deadline can hide an unrepresentable
# transport setting. This is the platform duration range, not a product ceiling.
MAX_TRANSPORT_TIMEOUT_SECONDS = TIMEOUT_MAX
MAX_RETRY_AFTER_SECONDS = 60.0
_DECIMAL_RETRY_AFTER = re.compile(r"^[0-9]+(\.[0-9]+)?$")


def bounded_transport_timeout(
    configured: float, requested: float | None, *, label: str
) -> float:
    """Validate mutable transport configuration before applying a request cap."""
    if (
        isinstance(configured, bool)
        or not isinstance(configured, (int, float))
        or configured > MAX_TRANSPORT_TIMEOUT_SECONDS
        or not math.isfinite(configured)
        or configured <= 0
    ):
        raise ProviderError(
            f"{label} timeout_seconds must be a finite positive number within the platform transport range"
        )
    return min(configured, requested) if requested is not None else configured


def urllib_error_is_transient(exc: URLError) -> bool:
    """Return whether ``exc`` represents a retryable transport failure.

    DNS lookups that may succeed on retry (``EAI_AGAIN``) and connection-class
    failures are treated as transient; permanent resolver or URL configuration
    errors are not.
    """

    reason = exc.reason
    if isinstance(reason, (TimeoutError, ConnectionError)):
        return True
    if isinstance(reason, socket.gaierror):
        return reason.errno == socket.EAI_AGAIN
    if isinstance(reason, OSError) and getattr(reason, "errno", None) in (
        _TRANSIENT_NETWORK_ERRNOS
    ):
        return True
    return False


def parse_retry_after_seconds(error: Any) -> float | None:
    """Return a bounded Retry-After delay from a transport error.

    Only integer or decimal second hints are honored. HTTP-date values are
    ignored so retry timing stays deterministic and clock-independent.
    """

    headers = getattr(error, "headers", None)
    if headers is None or not hasattr(headers, "get"):
        return None
    raw = headers.get("Retry-After")
    if raw is None:
        return None
    text = str(raw).strip()
    if not _DECIMAL_RETRY_AFTER.fullmatch(text):
        return None
    value = float(text)
    if value <= 0:
        return None
    return min(value, MAX_RETRY_AFTER_SECONDS)


def read_bounded_body(response: Any, maximum: int, *, label: str) -> bytearray:
    """Read up to *maximum* bytes from *response*, failing closed on excess."""

    body = bytearray()
    while True:
        remaining = maximum - len(body)
        # Probe one extra byte so an exact-limit body is accepted and any
        # overflow, including a 1-byte overage, fails closed before the extra
        # byte is kept.  Check the chunk against *remaining* before extending
        # because some injected responses ignore the requested size.
        requested = remaining + 1
        try:
            chunk = response.read(requested)
        except TypeError as exc:
            raise ProviderError(f"{label} body could not be read") from exc
        if not chunk:
            break
        if not isinstance(chunk, (bytes, bytearray)):
            raise ProviderError(f"{label} body is invalid")
        if len(chunk) > remaining:
            raise ProviderError(f"{label} exceeded the configured size limit")
        body.extend(chunk)
    return body


class DestinationBoundHTTPSHandler(HTTPSHandler):
    """Keep direct and proxied HTTPS requests bound to an admitted destination.

    urllib owns proxy discovery and CONNECT. Its standard HTTPSConnection parses
    the authority and selects SNI from the origin, including through a tunnel.
    """

    def __init__(
        self,
        context: ssl.SSLContext,
        *,
        expected_hostname: str,
        expected_port: int,
        label: str,
    ) -> None:
        super().__init__(context=context)
        self._destination = (expected_hostname, expected_port)
        self._label = label

    def _authority(self, url: str) -> tuple[str, int]:
        try:
            parsed = urlsplit(url)
            if (
                parsed.scheme.casefold() != "https"
                or parsed.hostname is None
                or "@" in parsed.netloc
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("invalid destination")
            return (
                parsed.hostname.encode("idna").decode("ascii").casefold(),
                443 if parsed.port is None else parsed.port,
            )
        except (UnicodeError, ValueError) as exc:
            raise ProviderError(f"{self._label} TLS host mismatch") from exc

    def https_open(self, req: Request) -> http.client.HTTPResponse:
        tunnel_host = getattr(req, "_tunnel_host", None)
        if tunnel_host is not None and not isinstance(tunnel_host, str):
            raise ProviderError(f"{self._label} TLS host mismatch")
        destination = tunnel_host or req.host
        if (
            self._authority(req.full_url) != self._destination
            or self._authority(f"https://{destination}") != self._destination
        ):
            raise ProviderError(f"{self._label} TLS host mismatch")
        return super().https_open(req)

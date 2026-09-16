from __future__ import annotations

import errno
import re
import socket
from typing import Any
from urllib.error import URLError

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
MAX_RETRY_AFTER_SECONDS = 60.0
_DECIMAL_RETRY_AFTER = re.compile(r"^[0-9]+(\.[0-9]+)?$")


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

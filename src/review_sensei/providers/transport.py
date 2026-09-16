from __future__ import annotations

import errno
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

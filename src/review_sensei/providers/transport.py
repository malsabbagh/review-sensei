from __future__ import annotations

from typing import Any

from ..errors import ProviderError


def read_bounded_body(response: Any, maximum: int, *, label: str) -> bytearray:
    """Read up to *maximum* bytes from *response*, failing closed on excess."""

    body = bytearray()
    while True:
        # Read one byte beyond the ceiling so an exact-limit response can be
        # accepted while an oversized response fails closed.  Check the chunk
        # length before extending the buffer because some injected responses
        # ignore the requested size.
        requested = maximum - len(body) + 1
        try:
            chunk = response.read(requested)
        except TypeError as exc:
            raise ProviderError(f"{label} body could not be read") from exc
        if not chunk:
            break
        if not isinstance(chunk, (bytes, bytearray)):
            raise ProviderError(f"{label} body is invalid")
        if len(chunk) > requested or len(body) + len(chunk) > maximum:
            raise ProviderError(f"{label} exceeded the configured size limit")
        body.extend(chunk)
    return body

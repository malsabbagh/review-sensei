"""Read one bounded broker error without replaying its HTTP request."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError

from review_sensei.broker_diagnostics import (
    MAX_DIAGNOSTIC_BYTES,
    BrokerDiagnostic,
    parse_broker_diagnostic,
)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate broker error field")
        value[key] = item
    return value


class _BoundedDiagnosticBody:
    """In-memory bounded copy for the existing closed diagnostic parser."""

    def __init__(self, raw: bytes, headers: Any) -> None:
        self.raw, self.headers = raw, headers

    def read(self, limit: int) -> bytes:
        return self.raw[:limit]

    def close(self) -> None:
        pass


@dataclass(frozen=True)
class BrokerFailureInspection:
    admission_held: bool
    diagnostic: BrokerDiagnostic


def inspect_broker_http_error(error: HTTPError) -> BrokerFailureInspection:
    """Inspect exactly one response; malformed bodies retain ordinary handling.

    The original stream is read and closed once. Only its bounded bytes reach
    the existing diagnostic parser, and no remote text becomes a message.
    """
    raw = b""
    try:
        supplied = error.read(MAX_DIAGNOSTIC_BYTES + 1)
        if isinstance(supplied, bytes):
            raw = supplied[: MAX_DIAGNOSTIC_BYTES + 1]
    except Exception:
        pass
    finally:
        try:
            error.close()
        except Exception:
            pass

    held = False
    if (
        type(error.code) is int
        and error.code == 503
        and len(raw) <= MAX_DIAGNOSTIC_BYTES
    ):
        try:
            value = json.loads(
                raw.decode("utf-8", errors="strict"),
                object_pairs_hook=_unique_object,
            )
            held = (
                isinstance(value, dict)
                and set(value) == {"error", "temporary"}
                and value["error"] == "broker_admission_held"
                and value["temporary"] is True
            )
        except Exception:
            pass

    diagnostic = parse_broker_diagnostic(_BoundedDiagnosticBody(raw, error.headers))
    return BrokerFailureInspection(held, diagnostic)

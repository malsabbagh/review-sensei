"""Untrusted JWT expiry hints schedule authentication reads, never authorize work."""

from __future__ import annotations

import base64
import json


def expiry_hint(token: object) -> int | None:
    if not isinstance(token, str) or len(token) > 65536:
        return None
    pieces = token.split(".")
    if len(pieces) != 3:
        return None
    try:
        raw = base64.urlsafe_b64decode(pieces[1] + "=" * (-len(pieces[1]) % 4))
        if len(raw) > 32768:
            return None
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        return None
    deadline = value.get("exp") if isinstance(value, dict) else None
    return (
        deadline
        if type(deadline) is int and 0 < deadline <= 9_007_199_254_740_991
        else None
    )

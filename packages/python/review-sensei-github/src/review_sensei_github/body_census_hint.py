"""Lexical query-size hints, never evidence, allocation or funding authority.

Any caller may forge these DATA values. The packed reader independently checks
its actual complete response against its unchanged original allocation plan.
"""

import json
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_HINT: ContextVar[Any] = ContextVar("body_complete_census_hint", default=None)


@contextmanager
def window():
    token = _HINT.set((False, None))
    try:
        yield
    finally:
        _HINT.reset(token)


def enable(archive):
    value = _HINT.get()
    if type(value) is not tuple or len(value) != 2 or type(archive) is not dict:
        return
    isolation = archive.get("target_isolation")
    if (
        type(isolation) is dict
        and type(isolation.get("contract")) is str
        and isolation["contract"] == "body-limit-prepublication-v1"
    ):
        _HINT.set((True, value[1]))


def observed(rows):
    value = _HINT.get()
    if type(value) is not tuple or len(value) != 2 or value[1] is not None:
        return rows
    if type(rows) is not list or not 1 <= len(rows) < 100:
        return rows
    size = 4096 + 1024 * len(rows)
    for row in rows:
        if type(row) is not dict or type(row.get("body")) is not str:
            return rows
        size += len(
            json.dumps(
                {"body": row["body"]}, ensure_ascii=True, separators=(",", ":")
            ).encode()
        )
    _HINT.set((value[0], (len(rows), size)))
    return rows


def first():
    value = _HINT.get()
    if type(value) is not tuple or len(value) != 2 or value[0] is not True:
        return None
    estimate = value[1]
    if (
        type(estimate) is tuple
        and len(estimate) == 2
        and type(estimate[0]) is int
        and 1 <= estimate[0] < 100
        and type(estimate[1]) is int
        and 0 < estimate[1] <= 512 * 1024
    ):
        return estimate[0] + 1
    return None


from review_sensei.source_registry import seal_module as _seal_source  # noqa: E402

_seal_source()

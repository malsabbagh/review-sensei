"""Bounded process observations retained in logs, never recovery authority."""

from __future__ import annotations

import json
import sys
from typing import Any

CLOSE_NOT_ENTERED_REASONS = frozenset(
    {
        "thread_cleanup_unconfirmed",
        "publication_finalization_incomplete",
        "terminal_entry_failed",
    }
)


def _emit(value: dict[str, object]) -> None:
    try:
        sys.stderr.write(
            "review-sensei: finalization "
            + json.dumps(value, sort_keys=True, separators=(",", ":"))
            + "\n"
        )
    except BaseException:
        pass


def close_review_run(caller: Any, ledger: Any, identity: Any, transaction: Any) -> Any:
    """Observe the actual close interval without changing its result or charges.

    Entry deliberately has unknown counts: after process death it cannot prove
    that no request was sent. A returned summary concerns only this interval;
    mutation observations need not be terminal PATCHes, and a response is not
    proof of its effect. No exception text, request bodies or URLs are emitted.
    """
    value: dict[str, object] = {
        "schema_version": "review-finalization-observation-v1",
        "stage": "entered",
        "close_returned": False,
        "mutation_dispatches": None,
        "unknown_mutations": None,
        "authority": False,
    }
    _emit(value)
    try:
        before = len(caller.observations)
    except BaseException:
        before = None
    returned = False
    try:
        result = caller.finish_review_run(ledger, identity, transaction)
        returned = True
        return result
    finally:
        # Observation failures must neither hide a business failure nor turn a
        # successful close into a failed operation. Missing data stays unknown.
        try:
            if before is not None:
                writes = [o for o in caller.observations[before:] if o.get("mutation")]
                value["mutation_dispatches"] = len(writes)
                value["unknown_mutations"] = sum(
                    o.get("outcome") != "response" or bool(o.get("uncertain_effect"))
                    for o in writes
                )
        except BaseException:
            value["mutation_dispatches"] = value["unknown_mutations"] = None
        value.update(
            stage="returned" if returned else "raised", close_returned=returned
        )
        _emit(value)


def close_not_entered(reason: object) -> None:
    _emit(
        {
            "schema_version": "review-finalization-observation-v1",
            "stage": "not_entered",
            "reason": reason
            if type(reason) is str and reason in CLOSE_NOT_ENTERED_REASONS
            else "unrecognized_reason",
            "close_returned": False,
            "mutation_dispatches": None,
            "unknown_mutations": None,
            "authority": False,
        }
    )

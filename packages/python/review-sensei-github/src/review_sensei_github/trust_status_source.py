"""Immutable bindings for the explicit empty-status execution dependency set.

No source discovery, dispatch admission, or budget is inferred by this seal.
The route planner supplies the closed scope; this only prevents replacing its
helpers/constructors with implementations outside that measured source path.
"""

from __future__ import annotations

import importlib
import inspect
from typing import Any

from review_sensei.errors import ReviewInputError

# Includes locally imported helpers, not just execute_command's module globals.
# Keep this list aligned with the empty-current-root status route. Broader
# finding/assessment/refinalization routes are not admitted by this contract.
_MODULES = (
    "review_sensei_github.action_run",
    "review_sensei_github.application",
    "review_sensei_github.action_representability",
    "review_sensei_github.accepted_semantic_history",
    "review_sensei_github.finding_lifecycle",
    "review_sensei_github.run_authority",
    "review_sensei_github.session_ledger",
    "review_sensei_github.api_run_requests",
    "review_sensei_github.api_run_budget",
    "review_sensei_github.api_run_plan",
    "review_sensei_github.http",
    "review_sensei_github.broker_client",
    "review_sensei_github.publication_recovery",
    "review_sensei_github.quarantine_checkpoint",
    "review_sensei_github.api_run_history",
    "review_sensei_github.caller_operation",
    "review_sensei_github.comment_intent",
    "review_sensei_github.historical_assessment_action",
    "review_sensei_github.quarantine_action",
    "review_sensei_github.publication",
    "review_sensei_github.conversation",
    "review_sensei_github.learning_pr",
    "review_sensei_github.trust_status",
    "review_sensei.disposition",
    "review_sensei.schemas",
    "review_sensei.finding_lifecycle",
    "review_sensei.finding_lifecycle_store",
    "review_sensei.session",
    "review_sensei.terminal_representability",
    "review_sensei.action_result_codec",
    "review_sensei.bounded_evidence",
    "review_sensei.api_work",
    "review_sensei.convergence",
)


def _callable(value):
    return getattr(value, "__func__", value)


def _snapshot():
    bindings: list[tuple[Any, str, Any, Any]] = []
    methods: list[tuple[Any, str, Any, Any]] = []
    for name in _MODULES:
        module = importlib.import_module(name)
        for key, value in vars(module).items():
            if key.isupper() and isinstance(
                value, (str, int, float, bool, tuple, frozenset)
            ):
                bindings.append((module, key, value, None))
                continue
            if not (inspect.isfunction(value) or inspect.isclass(value)) or not getattr(
                value, "__module__", ""
            ).startswith("review_sensei"):
                continue
            bindings.append((module, key, value, getattr(value, "__code__", None)))
            if inspect.isclass(value) and value.__module__ == name:
                for attribute in dir(value):
                    if attribute.startswith("__") and attribute not in {
                        "__init__",
                        "__new__",
                        "__call__",
                    }:
                        continue
                    member = _callable(getattr(value, attribute))
                    if isinstance(member, property):
                        methods.append((value, attribute, member, None))
                        for accessor in ("fget", "fset", "fdel"):
                            function = getattr(member, accessor)
                            if function is not None:
                                methods.append(
                                    (member, accessor, function, function.__code__)
                                )
                    if inspect.isfunction(member):
                        methods.append((value, attribute, member, member.__code__))
    return tuple(bindings), tuple(methods)


def capture_status_source():
    """Return a read-only validation closure, never a route issuer."""
    bindings, methods = _snapshot()
    normalize = _callable

    def require():
        for owner, name, original, code in (*bindings, *methods):
            current = normalize(getattr(owner, name, None))
            if (
                current is not original
                or getattr(current, "__code__", None) is not code
            ):
                raise ReviewInputError("trust status transitive source changed")

    return require

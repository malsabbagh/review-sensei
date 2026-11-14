"""Immutable bindings for the explicit empty-status execution dependency set.

No source discovery, dispatch admission, or budget is inferred by this seal.
The route planner supplies the closed scope; this only prevents replacing its
helpers/constructors with implementations outside that measured source path.
"""

from __future__ import annotations

import importlib

# Includes locally imported helpers, not just execute_command's module globals.
# Keep this list aligned with the empty-current-root status route. Broader
# finding/assessment/refinalization routes are not admitted by this contract.
_MODULES = (
    "review_sensei_github.action_run",
    "review_sensei_github.commands",
    "review_sensei_github.command_bodies",
    "review_sensei_github.review_run",
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
    "review_sensei_github.trust_disposition",
    "review_sensei_github.trust_status_source",
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


def capture_status_source():
    """Consume already-sealed original modules; never bless current attributes."""
    from review_sensei.source_registry import require_module_seal

    modules = tuple(importlib.import_module(name) for name in _MODULES)

    def require():
        for module in modules:
            require_module_seal(module)

    return require


from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()

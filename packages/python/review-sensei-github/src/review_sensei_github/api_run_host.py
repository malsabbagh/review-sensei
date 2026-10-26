"""Finite caller-run selection for the first offline hosted review integration.

The trusted host supplies the native, already authenticated startup result. This
module is not selected by repository JSON, an incoming receipt, or the released
workflow. It introduces no live activation or implicit old-phase fallback.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from review_sensei.api_work import StartupPlan, StartupSlot
from review_sensei.configuration import MAX_CONFIG_BYTES
from review_sensei.errors import ReviewInputError
from review_sensei.session import SessionIdentity

from .api_run_requests import ApiRunSessionLedger
from .http import MAX_PAGINATION_PAGES
from .review_hooks import GitHubReviewHooks

# Source-owned one-job policy, including caller-side OIDC, frozen config read,
# and authoritative workflow PR read outside the engine command. Each scan's
# existing physical pagination ceiling includes fallback and the terminal page.
_STARTUP_PREFIX = (
    "repository-policy",
    "default-policy-ref",
    "configuration",
    "pull",
    "oidc",
    "actions-token",
    "run",
    "jobs",
    "session",
)


def review_startup_plan(job: str) -> StartupPlan:
    if job not in {"hosted", "local"}:
        raise ReviewInputError("review startup job is unsupported")
    return StartupPlan(
        (
            StartupSlot(
                job,
                1,
                _STARTUP_PREFIX
                # Different-run rollover is the largest supported startup:
                # initial discovery; old run/jobs twice; native replacement's
                # discovery/latest/preactivation/readback; one archive POST,
                # lost-response listing plus exact part readback; head + PATCH.
                + ("initial-discovery",) * MAX_PAGINATION_PAGES
                + ("previous-owner",) * 4
                + ("rollover-lists",) * (4 * MAX_PAGINATION_PAGES)
                + ("retained-create",)
                + ("retained-reconcile",) * MAX_PAGINATION_PAGES
                + ("retained-readback", "head", "update"),
            ),
            StartupSlot(
                job,
                2,
                _STARTUP_PREFIX
                + ("ceased-job",) * 2
                + ("recovery-lists",) * (4 * MAX_PAGINATION_PAGES)
                + ("head", "update"),
            ),
        )
    )


HOSTED_API_STARTUP_PLAN = review_startup_plan("hosted")


def policy_bytes(config: Path | str | None) -> bytes:
    path = Path(config) if config is not None else Path(".reviewsensei.yml")
    if config is None and not path.exists():
        return b""
    with path.open("rb") as handle:
        raw = handle.read(MAX_CONFIG_BYTES + 1)
    if len(raw) > MAX_CONFIG_BYTES:
        raise ReviewInputError("frozen policy file exceeds its byte bound")
    return raw


class ApiRunReviewHooks:
    """One confirmed native ledger and caller owner across analysis phases."""

    def __init__(
        self,
        ledger: ApiRunSessionLedger,
        identity: SessionIdentity,
        *,
        base_sha: str,
        head_sha: str,
    ):
        if not isinstance(
            ledger, ApiRunSessionLedger
        ) or ledger.caller.plan != review_startup_plan(ledger.caller.job):
            raise ReviewInputError(
                "caller hosted selection requires its native source policy"
            )
        self.ledger, self.identity = ledger, identity
        self.base_sha, self.head_sha = base_sha, head_sha
        self.configuration = ledger.caller.accounting.configuration
        self._provider_started = self._completion_started = False

    def resolve_ledger(self, args, *, resolved_head_sha: str):
        state = self.ledger.caller.accounting
        if (
            (args.repository, args.pull_request, args.repository_id)
            != (
                self.identity.repository,
                self.identity.pull_request,
                self.identity.repository_id,
            )
            or resolved_head_sha != self.head_sha
            or args.base_sha != self.base_sha
            or hashlib.sha256(policy_bytes(args.config)).hexdigest()
            != state.configuration.digest
            or state.configuration != self.configuration
        ):
            raise ReviewInputError("caller hosted admission scope conflicts")
        self.ledger.caller.label_phase("admission")
        return self.ledger

    def begin_provider_phase(self, ledger):
        if ledger is not self.ledger or self._provider_started:
            raise ReviewInputError(
                "caller provider phase does not own the admitted run"
            )
        from .api_run_plan import TERMINAL_REQUESTS

        terminal = next(
            (
                b
                for b in self.ledger.caller.accounting.blocks
                if b.block_id == self.ledger.caller._terminal_block
            ),
            None,
        )
        if (
            terminal is None
            or terminal.issued_by != self.ledger.caller.accounting.owner
            or terminal.undispatched != TERMINAL_REQUESTS
        ):
            raise ReviewInputError("provider has no original protected terminal tail")
        # Attempt metadata within the existing work tranche; terminal cleanup is
        # already protected. There is no promise that all histories will fit.
        self.ledger.caller.budget_view.preflight(1)
        self._provider_started = True
        self.ledger.caller.label_phase("provider")
        return self.ledger

    def begin_completion_phase(self, ledger):
        if ledger is not self.ledger or not self._provider_started:
            raise ReviewInputError("caller completion has no admitted provider phase")
        self._completion_started = True
        self.ledger.caller.label_phase("completion")
        return self.ledger

    def run_provider(
        self, service: object, request: object, **kwargs: object
    ) -> object:
        return GitHubReviewHooks().run_provider(service, request, **kwargs)

    def retain_checkpoint_result(self, *args: Any, **kwargs: Any) -> None:
        GitHubReviewHooks().retain_checkpoint_result(*args, **kwargs)

    def load_retained_checkpoint_result(
        self, *args: Any, **kwargs: Any
    ) -> object | None:
        return GitHubReviewHooks().load_retained_checkpoint_result(*args, **kwargs)

    def report_failure(self, error: BaseException, args: Any) -> bool:
        return GitHubReviewHooks().report_failure(error, args)

    def publish_review(self, application: Any, **kwargs: Any) -> Any:
        from .api_run_plan import publication_attempt_plan
        from .publication_recovery import _payload

        if (
            application.session_ledger is not self.ledger
            or not self._completion_started
        ):
            raise ReviewInputError("publication does not own the analyzed caller run")
        self.ledger.caller.label_phase("publication")
        options = kwargs["options"]
        plan = publication_attempt_plan(
            _payload(kwargs["result"]),
            inventory=options.complete_inventory,
            approve=options.auto_approve,
        )
        self.ledger.caller.budget_view.preflight(plan.minimum_requests)
        # The workflow's pre-publication current-head read is also caller work.
        status, pull = self.ledger.http.request(
            "GET",
            self.ledger.http.repository_path(
                self.identity.repository, f"/pulls/{self.identity.pull_request}"
            ),
            token=self.ledger.token,
        )
        if (
            status != 200
            or not isinstance(pull, dict)
            or pull.get("head", {}).get("sha") != self.head_sha
        ):
            raise ReviewInputError("caller workflow publication head changed")
        return application.publish_review(
            publication_budget=self.ledger.caller.budget_view, **kwargs
        )

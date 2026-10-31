"""Finite caller-run policy shared by the current Actions lifecycle routes.

The trusted bootstrap supplies authenticated startup state. Serialized records
and incoming receipts cannot select a new allowance or an old phase fallback.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Unpack

from review_sensei.api_work import LEGACY_ACCOUNTING_VERSION, StartupPlan, StartupSlot
from review_sensei.configuration import MAX_CONFIG_BYTES
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewRequest
from review_sensei.rescan import AuthorizedRescan
from review_sensei.service import ReviewService
from review_sensei.session import SessionIdentity

from .api_run_requests import ApiRunSessionLedger
from .command_bodies import _HostedProviderOptions
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


def review_startup_plan(job: str, *, quarantine_v3: bool = False) -> StartupPlan:
    from .quarantine_publication import STARTUP_READ_SLOTS

    if job not in {"hosted", "local", "command"} or type(quarantine_v3) is not bool:
        raise ReviewInputError("review startup job is unsupported")
    return StartupPlan(
        (
            StartupSlot(
                job,
                1,
                _STARTUP_PREFIX
                # V3: one 14-read publication proof, bounded immutable-tag
                # revalidation and archive growth; prepaid from the same 512.
                + (("v3-publication",) * STARTUP_READ_SLOTS if quarantine_v3 else ())
                + (("source",) if job == "command" else ())
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
                + (("v3-publication",) * STARTUP_READ_SLOTS if quarantine_v3 else ())
                + (("source",) if job == "command" else ())
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
        oidc_token: str | None = None,
        actions_token: str | None = None,
    ):
        if not isinstance(ledger, ApiRunSessionLedger) or ledger.caller.plan not in (
            review_startup_plan(ledger.caller.job),
            review_startup_plan(ledger.caller.job, quarantine_v3=True),
        ):
            raise ReviewInputError(
                "caller hosted selection requires its native source policy"
            )
        self.ledger, self.identity = ledger, identity
        self.base_sha, self.head_sha = base_sha, head_sha
        self.oidc_token = oidc_token
        self.actions_token = actions_token
        self.rescan_request: AuthorizedRescan | None = None
        self.prior_inventory_reference = None
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
        from .api_run_plan import terminal_requests

        state = self.ledger.caller.accounting
        terminal = next(
            (
                b
                for b in state.blocks
                if b.block_id == self.ledger.caller._terminal_block
            ),
            None,
        )
        if (
            terminal is None
            or terminal.issued_by != state.owner
            or terminal.undispatched
            != terminal_requests(
                self.configuration.limit,
                startup=state.startup.liability,
                successor=state.owner.run_attempt > 1,
                legacy=state.version == LEGACY_ACCOUNTING_VERSION,
            )
        ):
            raise ReviewInputError("provider has no original protected terminal tail")
        # Attempt metadata within the existing work tranche; terminal cleanup is
        # already protected. There is no promise that all histories will fit.
        self.ledger.caller.budget_view.preflight(1)
        self._provider_started = True
        self.ledger.caller.label_phase("provider")
        return self.ledger

    def resolve_rescan(self, args, ledger):
        import os

        from .rescan import resolve_rescan

        if ledger is not self.ledger:
            raise ReviewInputError("rescan does not own the admitted caller ledger")
        actions_token = ""
        if os.environ.get("GITHUB_EVENT_NAME") != "issue_comment":
            if not isinstance(self.actions_token, str) or not self.actions_token:
                raise ReviewInputError(
                    "current dispatch Actions capability is unavailable"
                )
            # Reuse this process's startup capability, then independently read
            # and authenticate the dispatch run/source in the same allowance.
            actions_token = self.actions_token

        def capture(request, reference):
            self.rescan_request = request
            self.prior_inventory_reference = reference

        return resolve_rescan(
            args, ledger, actions_token=actions_token, _source_observer=capture
        )

    def begin_completion_phase(self, ledger):
        if ledger is not self.ledger or not self._provider_started:
            raise ReviewInputError("caller completion has no admitted provider phase")
        self._completion_started = True
        self.ledger.caller.label_phase("completion")
        return self.ledger

    def run_provider(
        self,
        service: ReviewService,
        request: ReviewRequest,
        **kwargs: Unpack[_HostedProviderOptions],
    ) -> object:
        from review_sensei.errors import ProviderError

        # This is earlier than run_actions_analysis admission. Every exception
        # from here onward lacks a supported settlement witness, regardless of
        # its class or diagnostic. Absence of an error is never abort authority.
        self.ledger.caller._operation_entry_started = True
        try:
            return GitHubReviewHooks().run_provider(service, request, **kwargs)
        except ReviewInputError as error:
            if error.diagnostic == "operation_entry_refused":
                self.ledger.caller._provider_effect_unresolved = True
            raise
        except (ProviderError, KeyboardInterrupt):
            # Neither exception proves a remote inference has settled. Keep
            # cleanup possible, but do not mint terminal rollover authority.
            self.ledger.caller._provider_effect_unresolved = True
            raise

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

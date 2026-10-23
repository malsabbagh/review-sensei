"""Hooks the engine review command calls without importing this package."""

from __future__ import annotations

import argparse
import hashlib

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.errors import ReviewInputError
from review_sensei.session import SessionLedger
from review_sensei_github.application import resolve_hosted_session_ledger
from review_sensei_github.broker_client import BrokerClient
from review_sensei_github.broker_control import BrokerDispatchAccounting
from review_sensei_github.commands import report_broker_failure, run_hosted_provider
from review_sensei_github.http import GitHubHttp


class GitHubReviewHooks:
    """Broker ledger, operation-entry provider, and broker diagnostics."""

    def resolve_ledger(
        self, args: argparse.Namespace, *, resolved_head_sha: str
    ) -> SessionLedger:
        budget = EvidenceReadBudget()
        return resolve_hosted_session_ledger(
            broker=self._admission_broker(args, budget),
            http=GitHubHttp(),
            oidc_token=getattr(args, "oidc_token", None),
            repository=args.repository,
            repository_id=args.repository_id,
            pull_request=args.pull_request,
            head_sha=resolved_head_sha,
            app_slug=getattr(args, "app_slug", None),
            evidence_budget=budget,
            analysis_metadata_phases=True,
        )

    @staticmethod
    def begin_provider_phase(ledger: SessionLedger) -> SessionLedger:
        phases = getattr(ledger, "_analysis_phases", None)
        return phases.begin_provider(ledger) if phases is not None else ledger

    @staticmethod
    def begin_completion_phase(ledger: SessionLedger) -> SessionLedger:
        phases = getattr(ledger, "_analysis_phases", None)
        return phases.completion(ledger) if phases is not None else ledger

    def resolve_rescan(self, args, ledger):
        from .rescan import resolve_rescan

        budget = getattr(ledger, "evidence_budget", None)
        if not isinstance(budget, EvidenceReadBudget):
            raise ReviewInputError("analysis admission has no original metadata budget")
        broker = self._admission_broker(args, budget)
        actions_token = broker.exchange(
            args.oidc_token or broker.request_oidc_token(), capability="review_actions"
        )
        return resolve_rescan(args, ledger, actions_token=actions_token)

    @staticmethod
    def _admission_broker(args, budget):
        operation = hashlib.sha256(
            f"analysis-admission:{args.repository_id}:{args.pull_request}:{args.head_sha}".encode()
        ).hexdigest()
        return BrokerClient(control=BrokerDispatchAccounting(budget, operation, 10, 1))

    def run_provider(
        self, service: object, request: object, **kwargs: object
    ) -> object:
        return run_hosted_provider(service, request, **kwargs)

    def retain_checkpoint_result(
        self,
        args: argparse.Namespace,
        ledger: SessionLedger,
        identity: object,
        result: object,
        *,
        base_sha: str,
        head_sha: str,
        configuration_context: object,
        evidence_context: object,
        policy: object,
    ) -> None:
        from .publication_recovery import (
            publication_http,
            retain_checkpoint_review_result,
        )
        from .session_ledger import GitHubIssueCommentSessionLedger

        if not isinstance(ledger, GitHubIssueCommentSessionLedger):
            return
        app_slug = (
            ledger.app_slug or getattr(args, "app_slug", None) or "reviewsensei[bot]"
        )
        pending_transaction = getattr(result, "transaction", None)
        if pending_transaction is None:
            return
        retain_checkpoint_review_result(
            http=publication_http(ledger.http, ledger.evidence_budget)
            if ledger.evidence_budget is not None
            else ledger.http,
            token=ledger.token,
            result=result,  # type: ignore[arg-type]
            identity=identity,  # type: ignore[arg-type]
            base_sha=base_sha,
            head_sha=head_sha,
            app_slug=app_slug,
            pending_transaction=pending_transaction,
            configuration_context=configuration_context,  # type: ignore[arg-type]
            evidence_context=evidence_context,  # type: ignore[arg-type]
            policy=policy,  # type: ignore[arg-type]
        )

    def load_retained_checkpoint_result(
        self,
        args: argparse.Namespace,
        ledger: SessionLedger,
        identity: object,
        *,
        transaction: object | None = None,
        head_sha: str | None = None,
    ) -> object | None:
        load_retained = getattr(ledger, "load_retained_checkpoint_result", None)
        if not callable(load_retained):
            return None
        return load_retained(identity, transaction=transaction, head_sha=head_sha)

    def report_failure(self, error: BaseException, args: argparse.Namespace) -> bool:
        return bool(report_broker_failure(error, args))

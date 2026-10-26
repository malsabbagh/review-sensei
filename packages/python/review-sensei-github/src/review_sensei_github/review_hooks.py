"""Hooks the engine review command calls without importing this package."""

from __future__ import annotations

import argparse

from review_sensei.errors import ReviewInputError
from review_sensei.session import SessionLedger
from review_sensei_github.commands import report_broker_failure, run_hosted_provider


class GitHubReviewHooks:
    """Broker ledger, operation-entry provider, and broker diagnostics."""

    def resolve_ledger(
        self, args: argparse.Namespace, *, resolved_head_sha: str
    ) -> SessionLedger:
        raise ReviewInputError(
            "hosted phase entry is retired; use the current review-run lifecycle"
        )

    def resolve_rescan(self, args, ledger):
        raise ReviewInputError(
            "hosted phase rescan entry is retired; use the current review-run lifecycle"
        )

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

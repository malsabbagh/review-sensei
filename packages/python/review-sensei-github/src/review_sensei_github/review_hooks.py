"""Hooks the engine review command calls without importing this package."""

from __future__ import annotations

import argparse

from review_sensei.session import SessionLedger
from review_sensei_github.application import resolve_hosted_session_ledger
from review_sensei_github.broker_client import BrokerClient
from review_sensei_github.commands import report_broker_failure, run_hosted_provider
from review_sensei_github.http import GitHubHttp


class GitHubReviewHooks:
    """Broker ledger, operation-entry provider, and broker diagnostics."""

    def resolve_ledger(
        self, args: argparse.Namespace, *, resolved_head_sha: str
    ) -> SessionLedger:
        return resolve_hosted_session_ledger(
            broker=BrokerClient(),
            http=GitHubHttp(),
            oidc_token=getattr(args, "oidc_token", None),
            repository=args.repository,
            repository_id=args.repository_id,
            pull_request=args.pull_request,
            head_sha=resolved_head_sha,
            app_slug=getattr(args, "app_slug", None),
        )

    def run_provider(
        self, service: object, request: object, **kwargs: object
    ) -> object:
        return run_hosted_provider(service, request, **kwargs)

    def report_failure(self, error: BaseException, args: argparse.Namespace) -> bool:
        return bool(report_broker_failure(error, args))

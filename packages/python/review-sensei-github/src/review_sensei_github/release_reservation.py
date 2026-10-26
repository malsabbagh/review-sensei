"""Owner-only release of this run's session reservation.

Used by the always-run workflow cleanup step after a review step that was
cancelled or killed: the Python process may never have run its own abort,
so the reservation it held would otherwise block the next review until
session expiry. Releasing publishes nothing, so the owning run may release
its own hold even after the PR head has moved. Ownership is proven twice:
the broker derives the owner run from verified OIDC, and the durable
ledger record must name that same run as its reservation owner.
"""

from __future__ import annotations

import re

from review_sensei.errors import ReviewInputError

from .broker_client import BrokerClient
from .http import GitHubHttp

_HEAD_SHA = re.compile(r"[a-f0-9]{40}")
_RUN_ID = re.compile(r"[1-9][0-9]{0,18}")
_REPOSITORY = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")


def release_own_reservation(
    *,
    broker: BrokerClient,
    http: GitHubHttp,
    repository: str,
    repository_id: int,
    pull_request: int,
    head_sha: str,
    run_id: str,
    oidc_token: str | None = None,
) -> str:
    raise ReviewInputError(
        "separate reservation cleanup is retired; the current lifecycle owns cleanup"
    )

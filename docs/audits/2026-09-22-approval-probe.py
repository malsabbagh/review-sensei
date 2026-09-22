"""Historical offline publication probe; use the artifact pinned in the audit.

Only external HTTP is mocked. This prints observations, not a passing safety
assertion or release attestation. Do not add a test expecting unsafe behavior
to persist in future versions. No real model or GitHub calls are made.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
from pathlib import Path

from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.hosting.github.observed import _ObservedGitHub
from review_sensei.hosting.github.publication import ReviewPublisher
from review_sensei.models import ProviderResponse, ReviewResult
from review_sensei.openrouter_qualification import PUBLISHED_QUALIFICATION_SLICES
from review_sensei.provider_config import HOSTED_OPENROUTER_DEFAULTS
from review_sensei.providers.openrouter import OpenRouterRoutingPolicy

DIFF = (
    "diff --git a/src/pagination.py b/src/pagination.py\n"
    "--- a/src/pagination.py\n+++ b/src/pagination.py\n"
    "@@ -1,3 +1,3 @@\n a\n b\n-old\n+new\n"
    "diff --git a/src/runner.py b/src/runner.py\n"
    "--- a/src/runner.py\n+++ b/src/runner.py\n"
    "@@ -1 +1 @@\n-old\n+new\n"
)


def events(host: _ObservedGitHub) -> list[object]:
    """Read actual review events sent to the fake external HTTP boundary."""
    return [
        body.get("event")
        for method, path, body in host.calls
        if method == "POST" and path.endswith("/reviews") and body
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    root = parser.parse_args().source_root
    fixture_path = root / "docs/site/examples/index.html"
    if not fixture_path.is_file():
        parser.error("source root must contain docs/site/examples/index.html")
    html = fixture_path.read_text(encoding="utf-8")
    match = re.search(
        r'<script[^>]*id="example-partial"[^>]*>(.*?)</script>', html, re.S
    )
    if match is None:
        parser.error("No exact embedded example-partial fixture found")
    partial = ReviewResult.from_dict(json.loads(match.group(1)))
    clean = ReviewResult(
        summary="Offline audit control",
        comments=(),
        provider="fixture",
        model="fixture-v1",
    )
    cases = {
        "complete-control": clean,
        "partial-status": dataclasses.replace(clean, review_status="partial"),
        "incomplete-status": dataclasses.replace(clean, review_status="incomplete"),
        "summary-only-status": dataclasses.replace(clean, review_status="summary-only"),
        "actual-website-partial-json": partial,
        "unqualified-openrouter-complete": dataclasses.replace(
            clean, provider="openrouter", model="deepseek/deepseek-v4.1-flash"
        ),
    }
    results: list[dict[str, object]] = []
    for name, result in cases.items():
        for mode in ("legacy", "merge-focused"):
            host = _ObservedGitHub()
            ReviewPublisher(http=host.http).publish(
                token="fake-audit-token",
                repository="owner/repo",
                repository_id=136,
                pull_request=136,
                head_sha=host.head_sha,
                base_branch="main",
                base_sha="f" * 40,
                result=result,
                diff=DIFF,
                app_slug="reviewsensei[bot]",
                convergence_policy=ReviewConvergencePolicy(mode=mode),
            )
            results.append({"case": name, "policy": mode, "events": events(host)})
    for name, approve, head in (
        ("approval-disabled", False, "0" * 40),
        ("stale-head", True, "1" * 40),
    ):
        host = _ObservedGitHub()
        error = None
        try:
            ReviewPublisher(http=host.http).publish(
                token="fake-audit-token",
                repository="owner/repo",
                repository_id=136,
                pull_request=136,
                head_sha=head,
                base_branch="main",
                base_sha="f" * 40,
                result=clean,
                diff=DIFF,
                app_slug="reviewsensei[bot]",
                auto_approve=approve,
            )
        except Exception as exc:
            # An error is reported, not treated as a passing negative control.
            error = type(exc).__name__
        results.append({"case": name, "events": events(host), "error": error})
    try:
        OpenRouterRoutingPolicy(upstream_provider="deepinfra/turbo")
        endpoint_result = "accepted"
    except Exception as exc:
        endpoint_result = str(exc)
    report = {
        "historical_audited_main_sha": "1d1f9389c09a1e575b292029c013ea3dbc9af516",
        "provenance_note": (
            "This label identifies the original audit; separately verify the "
            "wheel/source artifact when reproducing."
        ),
        "scope": (
            "Pinned CI wheel; real publisher/finalizer; external GitHub HTTP mocked. "
            "Not full application/workflow or live qualification."
        ),
        "default_mode": ReviewConvergencePolicy().mode,
        "publication_cases": results,
        "hosted_targets": sorted(HOSTED_OPENROUTER_DEFAULTS),
        "qualification_targets": sorted(PUBLISHED_QUALIFICATION_SLICES),
        "target_intersection": sorted(
            set(HOSTED_OPENROUTER_DEFAULTS)
            & {(model, upstream) for model, upstream, _ in PUBLISHED_QUALIFICATION_SLICES}
        ),
        "endpoint_variant": endpoint_result,
        "provider_response_fields": [
            field.name for field in dataclasses.fields(ProviderResponse)
        ],
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

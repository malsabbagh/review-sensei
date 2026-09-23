"""Validate the GitHub REST workflow run selected for an npm bundle resume."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


class ResumeRunError(ValueError):
    """The selected run cannot supply the attested npm release bundle."""


def validate_resume_run(
    run: dict[str, Any],
    *,
    run_id: str,
    repository: str,
    default_branch: str,
    version: str,
    dispatch_source_sha: str,
) -> str:
    if str(run.get("id")) != run_id:
        raise ResumeRunError(
            "resume_bundle_run_id does not match attested workflow run"
        )
    if run.get("repository") != repository:
        raise ResumeRunError("resume_bundle_run_id must reference this repository")
    if run.get("path") != ".github/workflows/publish-npm.yml":
        raise ResumeRunError(
            "resume_bundle_run_id must reference a publish-npm workflow run"
        )
    if run.get("conclusion") not in {"failure", "cancelled"}:
        raise ResumeRunError(
            "resume_bundle_run_id must reference a completed failed or cancelled run"
        )
    if run.get("event") != "workflow_dispatch":
        raise ResumeRunError(
            "resume_bundle_run_id must reference a manual publish-npm dispatch"
        )
    if run.get("head_branch") != default_branch:
        raise ResumeRunError(
            "resume_bundle_run_id must reference a default-branch dispatch"
        )
    if run.get("display_title") != f"publish-npm {version}":
        raise ResumeRunError(
            "resume_bundle_run_id must reference a publish-npm run for the same version"
        )
    head_sha = run.get("head_sha")
    if not isinstance(head_sha, str) or not head_sha:
        raise ResumeRunError("resume_bundle_run_id is missing attested head SHA")
    # A newer default-branch commit cannot resume bytes built from an older one.
    if head_sha != dispatch_source_sha:
        raise ResumeRunError(
            "resume_bundle_run_id must reference a run from the dispatch source commit"
        )
    return head_sha


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--default-branch", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--dispatch-source-sha", required=True)
    args = parser.parse_args()
    try:
        run = json.load(sys.stdin)
        if not isinstance(run, dict):
            raise ResumeRunError("resume_bundle_run_id did not return a workflow run")
        head_sha = validate_resume_run(
            run,
            run_id=args.run_id,
            repository=args.repository,
            default_branch=args.default_branch,
            version=args.version,
            dispatch_source_sha=args.dispatch_source_sha,
        )
    except (json.JSONDecodeError, ResumeRunError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(head_sha)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

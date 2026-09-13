"""Validate and optionally compare the repository's read-only protection policy.

The command never mutates GitHub.  Pass ``--readback`` with a JSON response
captured from ``gh api repos/OWNER/REPO/rulesets/ID`` to detect drift.  Without
that option it validates the checked-in policy document, which is safe to run
in ordinary pull-request CI where administration read permission is unavailable.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to read JSON file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON document {path} must be an object")
    return value


def validate_policy(policy: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if policy.get("version") != 1:
        errors.append("policy version must be 1")
    if policy.get("branch") != "main":
        errors.append("policy branch must be main")
    ruleset = policy.get("ruleset")
    if not isinstance(ruleset, dict):
        return [*errors, "policy ruleset must be an object"]
    required = {
        "require_pull_request": True,
        "required_approving_reviews": 1,
        "require_code_owner_review": True,
        "dismiss_stale_reviews": True,
        "require_conversation_resolution": True,
        "require_last_push_approval": True,
        "strict_required_status_checks": True,
    }
    for key, expected in required.items():
        if ruleset.get(key) != expected:
            errors.append(f"ruleset.{key} must be {expected!r}")
    if ruleset.get("required_status_checks") != ["Required checks"]:
        errors.append("ruleset.required_status_checks must contain Required checks")
    actors = policy.get("bypass_actors")
    if not isinstance(actors, list) or not actors:
        errors.append("policy must declare a narrow bypass actor")
    else:
        for actor in actors:
            if (
                not isinstance(actor, dict)
                or actor.get("bypass_mode") != "pull_request"
            ):
                errors.append("bypass actors may only use pull_request mode")
    tags = policy.get("tags")
    if not isinstance(tags, dict) or tags.get("movable_channels") != ["v4"]:
        errors.append("policy must protect immutable tags and declare only v4 movable")
    return errors


def compare_readback(policy: dict[str, Any], readback: dict[str, Any]) -> list[str]:
    """Compare the subset of GitHub ruleset fields needed for this policy."""

    errors = validate_policy(policy)
    rules = readback.get("rules", [])
    if not isinstance(rules, list):
        return [*errors, "ruleset readback.rules must be an array"]
    required_contexts: set[str] = set()
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if rule.get("type") != "required_status_checks":
            continue
        parameters = rule.get("parameters", {})
        checks = (
            parameters.get("required_status_checks", [])
            if isinstance(parameters, dict)
            else []
        )
        if isinstance(checks, list):
            required_contexts.update(
                str(check.get("context"))
                for check in checks
                if isinstance(check, dict) and check.get("context")
            )
    if "Required checks" not in required_contexts:
        errors.append(
            "ruleset readback is missing required status check: Required checks"
        )
    bypass = readback.get("bypass_actors", [])
    if not isinstance(bypass, list):
        errors.append("ruleset readback.bypass_actors must be an array")
    elif any(
        isinstance(actor, dict) and actor.get("bypass_mode") == "always"
        for actor in bypass
    ):
        errors.append("ruleset readback contains a blanket always bypass actor")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--readback", type=Path)
    args = parser.parse_args(argv)
    try:
        policy = load_object(args.policy)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    errors = validate_policy(policy)
    if args.readback:
        try:
            errors.extend(compare_readback(policy, load_object(args.readback)))
        except ValueError as exc:
            errors.append(str(exc))
    if errors:
        print("\n".join(dict.fromkeys(errors)), file=sys.stderr)
        return 1
    mode = " against readback" if args.readback else " (document only)"
    print(f"Protection policy check passed{mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

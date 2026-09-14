"""Validate and optionally compare the repository's read-only protection policy.

The command never mutates GitHub.  Pass ``--readback`` with a JSON response
captured from ``gh api repos/OWNER/REPO/rulesets/ID`` to detect drift.  Without
that option it validates the checked-in policy document, which is safe to run
in ordinary pull-request CI where ruleset API credentials are unavailable.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

_IMMUTABLE_TAG_PATTERN = r"^v[0-9]+\.[0-9]+\.[0-9]+$"


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
        "target": "branch",
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
    max_bypass_actors = policy.get("max_bypass_actors")
    if (
        isinstance(max_bypass_actors, bool)
        or not isinstance(max_bypass_actors, int)
        or max_bypass_actors != 1
    ):
        errors.append("policy.max_bypass_actors must be exactly the integer 1")
        max_bypass_actors = None
    actors = policy.get("bypass_actors")
    if not isinstance(actors, list) or not actors:
        errors.append("policy must declare a narrow bypass actor")
    else:
        if max_bypass_actors is not None and len(actors) > max_bypass_actors:
            errors.append("policy.bypass_actors must not exceed max_bypass_actors")
        seen_names: set[str] = set()
        seen_identities: set[tuple[int, str]] = set()
        for actor in actors:
            if not isinstance(actor, dict):
                errors.append("bypass actors must be objects")
                continue
            mode = actor.get("bypass_mode")
            if mode != "pull_request":
                errors.append("bypass actors may only use pull_request mode")
            name = actor.get("name")
            if not isinstance(name, str) or not name.strip():
                errors.append("bypass actors must have a non-empty name")
            elif name in seen_names:
                errors.append("bypass actors must have unique names")
            else:
                seen_names.add(name)
            actor_id = actor.get("actor_id")
            actor_type = actor.get("actor_type")
            if (
                isinstance(actor_id, bool)
                or not isinstance(actor_id, int)
                or actor_id <= 0
            ):
                errors.append("bypass actors must have a positive actor_id")
            elif isinstance(actor_type, str) and actor_type.strip():
                identity = (actor_id, actor_type)
                if identity in seen_identities:
                    errors.append("bypass actors must have unique actor identities")
                else:
                    seen_identities.add(identity)
            if not isinstance(actor_type, str) or not actor_type.strip():
                errors.append("bypass actors must have a non-empty actor_type")
            reason = actor.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                errors.append("bypass actors must document a non-empty reason")
    tags = policy.get("tags")
    expected_tags = {
        "immutable_pattern": _IMMUTABLE_TAG_PATTERN,
        "movable_channels": ["v4"],
        "promotion_record": ".publication/publication-ledger.jsonl",
    }
    if not isinstance(tags, dict):
        errors.append("policy tags must be an object")
    else:
        for key, expected in expected_tags.items():
            if tags.get(key) != expected:
                errors.append(f"tags.{key} must be {expected!r}")
    return errors


def compare_readback(policy: dict[str, Any], readback: dict[str, Any]) -> list[str]:
    """Compare every enforceable branch-ruleset control with its API readback.

    GitHub's ruleset response uses rule-specific parameter names rather than
    the concise names in our checked-in policy.  Keeping the translation here
    makes drift checks deterministic and fail closed when a control is absent,
    malformed, or weaker than the documented contract.
    """

    errors = validate_policy(policy)
    ruleset = policy.get("ruleset")
    if not isinstance(ruleset, dict):
        return errors

    if "target" not in readback:
        errors.append("ruleset readback.target is missing")
    elif readback["target"] != ruleset.get("target"):
        errors.append(f"ruleset readback.target must be {ruleset.get('target')!r}")
    if readback.get("enforcement") != "active":
        errors.append("ruleset readback.enforcement must be 'active'")
    conditions = readback.get("conditions")
    if conditions is None:
        errors.append("ruleset readback.conditions is missing")
    else:
        if not isinstance(conditions, dict):
            errors.append("ruleset readback.conditions must be an object")
        else:
            ref_name = conditions.get("ref_name")
            include = ref_name.get("include") if isinstance(ref_name, dict) else None
            expected_ref = f"refs/heads/{policy.get('branch')}"
            exclude = ref_name.get("exclude") if isinstance(ref_name, dict) else None
            if include is None:
                errors.append("ruleset readback.conditions.ref_name.include is missing")
            elif not isinstance(include, list) or include != [expected_ref]:
                errors.append(f"ruleset readback must target {expected_ref}")
            if not isinstance(exclude, list):
                errors.append(
                    "ruleset readback.conditions.ref_name.exclude must be an array"
                )
            elif exclude:
                errors.append(
                    "ruleset readback.conditions.ref_name.exclude must be empty"
                )

    rules = readback.get("rules", [])
    if not isinstance(rules, list):
        errors.append("ruleset readback.rules must be an array")
        # Continue with an empty list so bypass actors are still checked and
        # the caller receives every independently actionable drift signal.
        rules = []

    def _rule_parameters(rule_type: str) -> dict[str, Any] | None:
        matching = [
            rule
            for rule in rules
            if isinstance(rule, dict) and rule.get("type") == rule_type
        ]
        if not matching:
            return None
        if len(matching) > 1:
            errors.append(f"ruleset readback contains duplicate {rule_type} rules")
        parameters = matching[0].get("parameters", {})
        if not isinstance(parameters, dict):
            errors.append(f"ruleset readback {rule_type}.parameters must be an object")
            return None
        return parameters

    if ruleset.get("require_pull_request"):
        parameters = _rule_parameters("pull_request")
        if parameters is None:
            errors.append("ruleset readback is missing pull_request rule")
        else:
            expected_parameters = {
                "required_approving_review_count": ruleset.get(
                    "required_approving_reviews"
                ),
                "require_code_owner_review": ruleset.get("require_code_owner_review"),
                "dismiss_stale_reviews_on_push": ruleset.get("dismiss_stale_reviews"),
                "require_last_push_approval": ruleset.get("require_last_push_approval"),
                "required_review_thread_resolution": ruleset.get(
                    "require_conversation_resolution"
                ),
            }
            for name, expected in expected_parameters.items():
                actual = parameters.get(name)
                if type(actual) is not type(expected) or actual != expected:
                    errors.append(
                        f"ruleset readback pull_request.parameters.{name} must be {expected!r}"
                    )

    if ruleset.get("strict_required_status_checks"):
        parameters = _rule_parameters("required_status_checks")
        if parameters is None:
            errors.append(
                "ruleset readback is missing required_status_checks rule "
                "(Required checks)"
            )
        else:
            expected_strict = ruleset.get("strict_required_status_checks")
            actual_strict = parameters.get("strict_required_status_checks_policy")
            if (
                type(actual_strict) is not type(expected_strict)
                or actual_strict != expected_strict
            ):
                errors.append(
                    "ruleset readback required_status_checks.parameters."
                    f"strict_required_status_checks_policy must be {expected_strict!r}"
                )
            checks = parameters.get("required_status_checks")
            expected_checks = ruleset.get("required_status_checks", [])
            if not isinstance(checks, list):
                errors.append(
                    "ruleset readback required_status_checks.parameters.required_status_checks must be an array"
                )
            else:
                actual_contexts = []
                for check in checks:
                    if not isinstance(check, dict) or not isinstance(
                        check.get("context"), str
                    ):
                        errors.append(
                            "ruleset readback required status checks must contain context strings"
                        )
                        continue
                    actual_contexts.append(check["context"])
                if sorted(actual_contexts) != sorted(expected_checks):
                    errors.append(
                        "ruleset readback required status checks do not match policy: "
                        + f"expected {sorted(expected_checks)!r}, got {sorted(actual_contexts)!r}"
                    )

    bypass = readback.get("bypass_actors", [])
    if not isinstance(bypass, list):
        errors.append("ruleset readback.bypass_actors must be an array")
    else:
        # The ruleset API returns actor_id, actor_type, and bypass_mode but no
        # descriptive actor name; the numeric/type identity is authoritative.
        actual_actors: list[tuple[int, str, str]] = []
        for actor in bypass:
            if not isinstance(actor, dict):
                errors.append("ruleset readback bypass actors must be objects")
                continue
            mode = actor.get("bypass_mode")
            if not isinstance(mode, str):
                errors.append("ruleset readback bypass actors require bypass_mode")
                continue
            actor_id = actor.get("actor_id")
            actor_type = actor.get("actor_type")
            if (
                isinstance(actor_id, bool)
                or not isinstance(actor_id, int)
                or actor_id <= 0
                or not isinstance(actor_type, str)
                or not actor_type.strip()
            ):
                errors.append(
                    "ruleset readback bypass actors require actor_id and actor_type"
                )
                continue
            actual_actors.append((actor_id, actor_type, mode))
            if mode == "always":
                errors.append("ruleset readback contains a blanket always bypass actor")
            elif mode != "pull_request":
                errors.append(
                    f"ruleset readback bypass actor mode {mode!r} is not allowed"
                )
        expected_actors = [
            (actor["actor_id"], actor["actor_type"], actor["bypass_mode"])
            for actor in policy.get("bypass_actors", [])
            if (
                isinstance(actor, dict)
                and isinstance(actor.get("actor_id"), int)
                and not isinstance(actor.get("actor_id"), bool)
                and isinstance(actor.get("actor_type"), str)
                and isinstance(actor.get("bypass_mode"), str)
            )
        ]
        if Counter(actual_actors) != Counter(expected_actors):
            errors.append("ruleset readback bypass actors do not match policy")
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

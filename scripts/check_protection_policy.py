"""Validate and optionally compare the repository's read-only protection policy.

The command never mutates GitHub.  Pass ``--readback`` with a JSON response
captured from ``gh api repos/OWNER/REPO/rulesets/ID`` to detect branch-ruleset
drift.  Pass ``--tag-readback`` and ``--channel-readback`` (or ``--readback-dir``)
for tag rulesets.  Without those options it validates the checked-in policy
document, which is configuration evidence only and is safe to run in ordinary
pull-request CI where ruleset API credentials are unavailable.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

_IMMUTABLE_TAG_PATTERN = r"^v[0-9]+\.[0-9]+\.[0-9]+$"
# GitHub rulesets use fnmatch in conditions.ref_name.include, not the policy
# regex.  This is the only accepted conservative translation of
# tags.immutable_pattern; it is broader than the regex (see docs/protection-policy.md).
_GITHUB_IMMUTABLE_TAG_INCLUDE = ("refs/tags/v[0-9]*.[0-9]*.[0-9]*",)
def _channel_ref_include(channel: str) -> tuple[str, ...]:
    return (f"refs/tags/{channel}",)


def _movable_channels(policy: dict[str, Any]) -> list[str]:
    tags = policy.get("tags")
    if not isinstance(tags, dict):
        return ["v4"]
    channels = tags.get("movable_channels")
    if not isinstance(channels, list) or not channels:
        return ["v4"]
    return [str(channel) for channel in channels]


def _channel_tag_from_include(
    policy: dict[str, Any], include: list[str] | None
) -> str | None:
    if include is None:
        return None
    for channel in _movable_channels(policy):
        if include == list(_channel_ref_include(channel)):
            return channel
    return None
_V4_PROMOTION_RECORD_TYPE = "v4_promotion"
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_OPERATOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SECRET_OPERATOR_RE = re.compile(
    r"^(?:ghp_|gho_|ghu_|ghs_|ghr_|github_pat_)", re.IGNORECASE
)
_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)
_V4_PROMOTION_KEYS = {
    "record_type",
    "tag",
    "previous_sha",
    "new_sha",
    "operator",
    "timestamp",
    "reason",
}
MAX_REASON_CHARS = 500
MAX_LEDGER_LINE_CHARS = 8192
MAX_LEDGER_LINES = 4096
MAX_READBACK_FILES = 16


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
        "movable_channels": ["v4", "v5"],
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


def _ref_include_list(
    readback: dict[str, Any], label: str
) -> tuple[list[Any] | None, list[str]]:
    errors: list[str] = []
    conditions = readback.get("conditions")
    if conditions is None:
        return None, [f"{label} readback.conditions is missing"]
    if not isinstance(conditions, dict):
        return None, [f"{label} readback.conditions must be an object"]
    ref_name = conditions.get("ref_name")
    if not isinstance(ref_name, dict):
        return None, [f"{label} readback.conditions.ref_name is missing"]
    include = ref_name.get("include")
    exclude = ref_name.get("exclude")
    if include is None:
        errors.append(f"{label} readback.conditions.ref_name.include is missing")
        include_list: list[Any] | None = None
    elif not isinstance(include, list):
        errors.append(f"{label} readback.conditions.ref_name.include must be an array")
        include_list = None
    else:
        include_list = include
    if not isinstance(exclude, list):
        errors.append(f"{label} readback.conditions.ref_name.exclude must be an array")
    elif exclude:
        errors.append(f"{label} readback.conditions.ref_name.exclude must be empty")
    return include_list, errors


def _collect_rules(
    readback: dict[str, Any], label: str
) -> tuple[list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    rules = readback.get("rules", [])
    if not isinstance(rules, list):
        errors.append(f"{label} readback.rules must be an array")
        return [], errors
    normalized: list[dict[str, Any]] = []
    for rule in rules:
        if not isinstance(rule, dict):
            errors.append(f"{label} readback rules must be objects")
            continue
        normalized.append(rule)
    return normalized, errors


def _rule_type_counts(rules: list[dict[str, Any]]) -> Counter[str]:
    return Counter(
        str(rule.get("type"))
        for rule in rules
        if isinstance(rule.get("type"), str) and rule.get("type")
    )


def _tag_target_errors(readback: dict[str, Any], label: str) -> list[str]:
    errors: list[str] = []
    if "target" not in readback:
        errors.append(f"{label} readback.target is missing")
    elif readback.get("target") != "tag":
        errors.append(f"{label} readback.target must be 'tag'")
    if readback.get("enforcement") != "active":
        errors.append(f"{label} readback.enforcement must be 'active'")
    return errors


def _policy_bypass_identities(
    policy: dict[str, Any],
) -> list[tuple[int, str, str]]:
    return [
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


def _tag_bypass_errors(
    policy: dict[str, Any], readback: dict[str, Any], label: str
) -> list[str]:
    errors: list[str] = []
    bypass = readback.get("bypass_actors", [])
    if not isinstance(bypass, list):
        return [f"{label} readback.bypass_actors must be an array"]
    actual_actors: list[tuple[int, str, str]] = []
    for actor in bypass:
        if not isinstance(actor, dict):
            errors.append(f"{label} readback bypass actors must be objects")
            continue
        mode = actor.get("bypass_mode")
        if not isinstance(mode, str):
            errors.append(f"{label} readback bypass actors require bypass_mode")
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
                f"{label} readback bypass actors require actor_id and actor_type"
            )
            continue
        actual_actors.append((actor_id, actor_type, mode))
        if mode == "always":
            errors.append(f"{label} readback contains a blanket always bypass actor")
        elif mode != "pull_request":
            errors.append(f"{label} readback bypass actor mode {mode!r} is not allowed")
    if actual_actors and Counter(actual_actors) != Counter(
        _policy_bypass_identities(policy)
    ):
        errors.append(f"{label} readback bypass actors do not match policy")
    return errors


def compare_tag_readback(policy: dict[str, Any], readback: dict[str, Any]) -> list[str]:
    """Compare the immutable semantic-version tag ruleset with API readback."""

    label = "immutable tag"
    errors = validate_policy(policy)
    errors.extend(_tag_target_errors(readback, label))
    include, include_errors = _ref_include_list(readback, label)
    errors.extend(include_errors)
    expected_include = list(_GITHUB_IMMUTABLE_TAG_INCLUDE)
    if include is not None and include != expected_include:
        errors.append(
            f"{label} readback must include {expected_include!r} "
            "(fnmatch translation of tags.immutable_pattern); "
            f"got {include!r}"
        )
    rules, rule_errors = _collect_rules(readback, label)
    errors.extend(rule_errors)
    types = _rule_type_counts(rules)
    if types["deletion"] < 1:
        errors.append(f"{label} readback allows deletion")
    if types["update"] < 1:
        errors.append(f"{label} readback allows replacement")
    if types["required_signatures"] < 1:
        errors.append(f"{label} readback allows unsigned replacement")
    errors.extend(_tag_bypass_errors(policy, readback, label))
    return errors


def compare_channel_readback(
    policy: dict[str, Any],
    readback: dict[str, Any],
    *,
    channel_tag: str | None = None,
) -> list[str]:
    """Compare a movable workflow-channel tag ruleset with API readback.

    Deletion must be restricted.  The ``update`` rule must be absent so
    authorized operators can move the channel tag.  Unsigned replacement is
    still forbidden via ``required_signatures``.  Blanket ``always`` bypass is
    not an accepted way to make the channel movable.
    """

    errors = validate_policy(policy)
    include, include_errors = _ref_include_list(readback, "channel")
    errors.extend(include_errors)
    resolved_channel = channel_tag or _channel_tag_from_include(policy, include)
    if resolved_channel is None:
        errors.append(
            "channel readback does not match any movable workflow channel tag"
        )
        return errors
    label = f"{resolved_channel} channel"
    errors.extend(_tag_target_errors(readback, label))
    expected_include = list(_channel_ref_include(resolved_channel))
    if include is not None and include != expected_include:
        errors.append(
            f"{label} readback must include {expected_include!r}; got {include!r}"
        )
    rules, rule_errors = _collect_rules(readback, label)
    errors.extend(rule_errors)
    types = _rule_type_counts(rules)
    if types["deletion"] < 1:
        errors.append(f"{label} readback allows deletion")
    if types["update"] > 0:
        errors.append(
            f"{label} readback must remain movable: the update rule must be absent"
        )
    if types["required_signatures"] < 1:
        errors.append(f"{label} readback allows unsigned replacement")
    errors.extend(_tag_bypass_errors(policy, readback, label))
    return errors


def load_readback_dir(directory: Path) -> list[tuple[Path, dict[str, Any]]]:
    if not directory.is_dir():
        raise ValueError(f"readback directory {directory} is not a directory")
    files = sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix == ".json"
    )
    if not files:
        raise ValueError("readback directory contains no JSON ruleset files")
    if len(files) > MAX_READBACK_FILES:
        raise ValueError(
            f"readback directory contains more than {MAX_READBACK_FILES} JSON files"
        )
    loaded: list[tuple[Path, dict[str, Any]]] = []
    for path in files:
        loaded.append((path, load_object(path)))
    return loaded


def compare_readback_dir(policy: dict[str, Any], directory: Path) -> list[str]:
    """Classify captured ruleset JSON files and compare each required target."""

    errors = validate_policy(policy)
    try:
        captures = load_readback_dir(directory)
    except ValueError as exc:
        return [*errors, str(exc), "missing tag protection"]

    branch: list[dict[str, Any]] = []
    immutable: list[dict[str, Any]] = []
    channels: dict[str, dict[str, Any]] = {}
    expected_immutable = list(_GITHUB_IMMUTABLE_TAG_INCLUDE)
    movable_channels = _movable_channels(policy)
    for path, payload in captures:
        target = payload.get("target")
        if target == "branch":
            branch.append(payload)
            continue
        if target != "tag":
            errors.append(f"ruleset {path.name} has unsupported target {target!r}")
            continue
        include, include_errors = _ref_include_list(payload, f"tag ruleset {path.name}")
        if include == expected_immutable:
            immutable.append(payload)
            continue
        channel_tag = _channel_tag_from_include(policy, include)
        if channel_tag is not None:
            if channel_tag in channels:
                errors.append(
                    f"readback directory contains multiple {channel_tag} channel rulesets"
                )
            else:
                channels[channel_tag] = payload
            continue
        errors.extend(include_errors)
        errors.append(
            f"tag ruleset {path.name} does not match the immutable "
            "fnmatch translation or a movable workflow-channel pattern"
        )

    if len(branch) > 1:
        errors.append("readback directory contains multiple branch rulesets")
    elif len(branch) == 1:
        errors.extend(compare_readback(policy, branch[0]))

    if not immutable:
        errors.append(
            "missing tag protection: immutable semantic-version ruleset was not captured"
        )
    elif len(immutable) > 1:
        errors.append("readback directory contains multiple immutable tag rulesets")
    else:
        errors.extend(compare_tag_readback(policy, immutable[0]))

    for channel_tag in movable_channels:
        if channel_tag not in channels:
            errors.append(
                f"missing tag protection: {channel_tag} channel ruleset was not captured"
            )
        else:
            errors.extend(
                compare_channel_readback(
                    policy, channels[channel_tag], channel_tag=channel_tag
                )
            )
    return errors


def validate_v4_promotion_entry(entry: Any) -> list[str]:
    """Validate a v4 promotion audit record (previous/new SHA, operator, time, reason)."""

    if not isinstance(entry, dict):
        return ["v4 promotion entry must be an object"]
    errors: list[str] = []
    extra = set(entry) - _V4_PROMOTION_KEYS
    if extra:
        errors.append(
            "v4 promotion entry has unknown fields: " + ", ".join(sorted(extra))
        )
    if entry.get("record_type") != _V4_PROMOTION_RECORD_TYPE:
        errors.append("v4 promotion entry record_type must be 'v4_promotion'")
    if entry.get("tag") != "v4":
        errors.append("v4 promotion entry tag must be 'v4'")

    previous = entry.get("previous_sha")
    previous_ok = False
    if "previous_sha" not in entry or previous in (None, ""):
        errors.append("v4 promotion entry is missing previous_sha")
    elif not isinstance(previous, str) or _GIT_SHA_RE.fullmatch(previous) is None:
        errors.append(
            "v4 promotion entry previous_sha must be a 40-character lowercase git SHA"
        )
    else:
        previous_ok = True

    new = entry.get("new_sha")
    new_ok = False
    if "new_sha" not in entry or new in (None, ""):
        errors.append("v4 promotion entry is missing new_sha")
    elif not isinstance(new, str) or _GIT_SHA_RE.fullmatch(new) is None:
        errors.append(
            "v4 promotion entry new_sha must be a 40-character lowercase git SHA"
        )
    else:
        new_ok = True

    if previous_ok and new_ok and previous == new:
        errors.append("v4 promotion entry previous_sha and new_sha must differ")

    operator = entry.get("operator")
    if (
        not isinstance(operator, str)
        or _OPERATOR_RE.fullmatch(operator) is None
        or _SECRET_OPERATOR_RE.match(operator)
    ):
        errors.append("v4 promotion entry operator must be a non-secret identity")

    timestamp = entry.get("timestamp")
    if not isinstance(timestamp, str) or _TIMESTAMP_RE.fullmatch(timestamp) is None:
        errors.append(
            "v4 promotion entry timestamp must be RFC 3339 UTC or offset datetime"
        )

    reason = entry.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        errors.append("v4 promotion entry is missing reason")
    elif len(reason) > MAX_REASON_CHARS:
        errors.append("v4 promotion entry reason exceeds the length limit")
    elif any(ord(character) < 32 for character in reason):
        errors.append("v4 promotion entry reason must not contain control characters")
    return errors


def _is_publication_ledger_entry(entry: dict[str, Any]) -> bool:
    return (
        isinstance(entry.get("source_sha"), str)
        and isinstance(entry.get("public_sha"), str)
        and entry.get("record_type") != _V4_PROMOTION_RECORD_TYPE
    )


def validate_promotion_ledger(path: Path) -> list[str]:
    """Validate mixed publication / v4-promotion JSONL. Malformed lines fail closed."""

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return [f"unable to read promotion ledger {path}: {exc}"]
    lines = text.splitlines()
    if len(lines) > MAX_LEDGER_LINES:
        return [f"promotion ledger {path} exceeds the line limit"]
    errors: list[str] = []
    for index, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        if len(line) > MAX_LEDGER_LINE_CHARS:
            errors.append(f"promotion ledger line {index} exceeds the length limit")
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"promotion ledger line {index} is malformed JSON: {exc}")
            continue
        if not isinstance(value, dict):
            errors.append(f"promotion ledger line {index} must be a JSON object")
            continue
        if value.get("record_type") == _V4_PROMOTION_RECORD_TYPE:
            for item in validate_v4_promotion_entry(value):
                errors.append(f"promotion ledger line {index}: {item}")
        elif _is_publication_ledger_entry(value):
            continue
        else:
            errors.append(f"promotion ledger line {index} has an unknown record type")
    return errors


def build_v4_promotion_entry(
    *,
    previous_sha: str,
    new_sha: str,
    operator: str,
    timestamp: str,
    reason: str,
) -> dict[str, str]:
    """Return a validated v4 promotion record for the shared publication ledger."""

    entry = {
        "record_type": _V4_PROMOTION_RECORD_TYPE,
        "tag": "v4",
        "previous_sha": previous_sha,
        "new_sha": new_sha,
        "operator": operator,
        "timestamp": timestamp,
        "reason": reason,
    }
    errors = validate_v4_promotion_entry(entry)
    if errors:
        raise ValueError("; ".join(errors))
    return entry


def append_v4_promotion_entry(path: Path, entry: dict[str, Any]) -> None:
    """Append a validated v4 promotion record to the shared JSONL ledger."""

    errors = validate_v4_promotion_entry(entry)
    if errors:
        raise ValueError("; ".join(errors))
    if path.is_file():
        existing = validate_promotion_ledger(path)
        if existing:
            raise ValueError("; ".join(existing))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument(
        "--readback",
        type=Path,
        help="Captured branch ruleset JSON from the GitHub ruleset API",
    )
    parser.add_argument(
        "--tag-readback",
        type=Path,
        help="Captured immutable tag ruleset JSON from the GitHub ruleset API",
    )
    parser.add_argument(
        "--channel-readback",
        type=Path,
        help="Captured v4 channel tag ruleset JSON from the GitHub ruleset API",
    )
    parser.add_argument(
        "--readback-dir",
        type=Path,
        help="Directory of captured ruleset JSON files (branch, immutable tags, v4)",
    )
    parser.add_argument(
        "--promotion-ledger",
        type=Path,
        help="JSONL ledger mixing publication provenance and v4 promotion records",
    )
    args = parser.parse_args(argv)
    try:
        policy = load_object(args.policy)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    errors = validate_policy(policy)
    file_flags = (args.readback, args.tag_readback, args.channel_readback)
    if args.readback_dir is not None and any(flag is not None for flag in file_flags):
        errors.append(
            "--readback-dir cannot be combined with --readback, --tag-readback, or --channel-readback"
        )
    elif args.readback_dir is not None:
        errors.extend(compare_readback_dir(policy, args.readback_dir))
    else:
        if args.readback is not None:
            try:
                errors.extend(compare_readback(policy, load_object(args.readback)))
            except ValueError as exc:
                errors.append(str(exc))
        if args.tag_readback is not None or args.channel_readback is not None:
            if args.tag_readback is None:
                errors.append("missing tag protection: --tag-readback was not provided")
            else:
                try:
                    errors.extend(
                        compare_tag_readback(policy, load_object(args.tag_readback))
                    )
                except ValueError as exc:
                    errors.append(str(exc))
            if args.channel_readback is None:
                errors.append(
                    "missing tag protection: --channel-readback was not provided"
                )
            else:
                try:
                    errors.extend(
                        compare_channel_readback(
                            policy, load_object(args.channel_readback)
                        )
                    )
                except ValueError as exc:
                    errors.append(str(exc))
    if args.promotion_ledger is not None:
        errors.extend(validate_promotion_ledger(args.promotion_ledger))
    if errors:
        print("\n".join(dict.fromkeys(errors)), file=sys.stderr)
        return 1
    if args.readback or args.tag_readback or args.channel_readback or args.readback_dir:
        suffix = " against readback"
    elif args.promotion_ledger is not None:
        suffix = " against promotion ledger"
    else:
        suffix = " (document only)"
    print(f"Protection policy check passed{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

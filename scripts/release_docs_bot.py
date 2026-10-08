"""Read-only checks for the explicitly approved, repository-only docs App."""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any

import release_docs as docs

APP_PERMISSIONS = {"contents": "write", "actions": "read", "metadata": "read"}
CORE_RULES = {"deletion", "non_fast_forward", "required_linear_history"}
ADMIN_BYPASS = {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}
PR_PARAMETERS = {
    "required_approving_review_count": 1,
    "dismiss_stale_reviews_on_push": True,
    "required_reviewers": [],
    "require_code_owner_review": True,
    "require_last_push_approval": True,
    "required_review_thread_resolution": False,
    "require_extra_approval_for_unattributed_changes": True,
    "allowed_merge_methods": ["squash"],
}
PR_RULE = {"type": "pull_request", "parameters": PR_PARAMETERS}
MAIN_CONDITIONS = {"ref_name": {"exclude": [], "include": ["~DEFAULT_BRANCH"]}}
PR_RULESET_NAME = "main-reviewed-source"


def api(path: str) -> Any:
    result = subprocess.run(
        ["gh", "api", path], check=True, capture_output=True, text=True
    )
    return json.loads(result.stdout)


def app_bypass(app_id: int) -> dict[str, Any]:
    return {"actor_id": app_id, "actor_type": "Integration", "bypass_mode": "always"}


def validate_app_id(value: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise ValueError("RELEASE_DOCS_APP_ID must be the dedicated App's numeric ID")
    return int(value)


def check_app(app_id: int, slug: str) -> dict[str, str]:
    # The slug comes from the pinned token Action, which authenticates with this
    # numeric App ID and the environment key. Never guess identity from a tag.
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,99}", slug):
        raise ValueError("dedicated App slug is missing or invalid")
    app = api(f"apps/{slug}")
    if (
        app.get("id") != app_id
        or app.get("slug") != slug
        or app.get("owner", {}).get("login") != docs.REPOSITORY.split("/")[0]
        or app.get("permissions") != APP_PERMISSIONS
    ):
        # These fields are public App metadata. Never dump the response: other
        # App endpoints and future fields can include credentials.
        expected = {
            "id": app_id,
            "slug": slug,
            "owner_login": docs.REPOSITORY.split("/")[0],
            "permissions": APP_PERMISSIONS,
        }
        observed = {
            "id": app.get("id"),
            "slug": app.get("slug"),
            "owner_login": app.get("owner", {}).get("login"),
            "permissions": app.get("permissions"),
        }
        differences = {
            field: {"expected": expected[field], "observed": observed[field]}
            for field in expected
            if expected[field] != observed[field]
        }
        raise ValueError(
            "dedicated docs App identity/owner/permissions do not match: "
            + json.dumps(differences, sort_keys=True)
        )
    repositories = api("installation/repositories?per_page=100")
    entries = repositories.get("repositories", [])
    if (
        repositories.get("total_count") != 1
        or len(entries) != 1
        or entries[0].get("full_name") != docs.REPOSITORY
    ):
        raise ValueError("docs token must access exactly the release repository")
    user = api(f"users/{slug}%5Bbot%5D")
    if (
        user.get("type") != "Bot"
        or user.get("login") != slug + "[bot]"
        or type(user.get("id")) is not int
        or user["id"] < 1
    ):
        raise ValueError("dedicated docs App bot identity is invalid")
    name = user["login"]
    return {
        "name": name,
        "email": f"{user['id']}+{name}@users.noreply.github.com",
    }


def check_rules(app_id: int) -> None:
    rules = docs.api_pages("rules/branches/main")
    if not CORE_RULES.issubset({rule.get("type") for rule in rules}):
        raise ValueError(
            "main must retain deletion, non-force and linear-history rules"
        )
    if not any(rule.get("type") == "pull_request" for rule in rules):
        raise ValueError("ordinary source changes must still require a reviewed PR")
    ids = set()
    for rule in rules:
        if (
            rule.get("ruleset_source_type") != "Repository"
            or rule.get("ruleset_source") != docs.REPOSITORY
            or type(rule.get("ruleset_id")) is not int
            or rule["ruleset_id"] < 1
        ):
            raise ValueError("active main rule has unverifiable repository provenance")
        ids.add(rule["ruleset_id"])
    for ruleset_id in sorted(ids):
        ruleset = docs.api(f"rulesets/{ruleset_id}")
        types = {rule.get("type") for rule in ruleset.get("rules", [])}
        # Metadata-only ruleset readers may not receive bypass_actors. The
        # server's effective decision for this authenticated token is required;
        # bootstrap must prove GitHub exposes it without Administration access.
        bypass = ruleset.get("bypass_actors")
        grants = [
            actor
            for actor in (bypass or [])
            if actor.get("actor_type") == "Integration"
            and actor.get("actor_id") == app_id
        ]
        if (
            ruleset.get("id") != ruleset_id
            or ruleset.get("enforcement") != "active"
            or ruleset.get("target") != "branch"
            or ruleset.get("source_type") != "Repository"
            or ruleset.get("source") != docs.REPOSITORY
        ):
            raise ValueError("main ruleset identity/enforcement changed")
        if "pull_request" in types:
            if (
                ruleset.get("conditions") != MAIN_CONDITIONS
                or ruleset.get("rules") != [PR_RULE]
                or (
                    bypass is not None
                    and (
                        grants != [app_bypass(app_id)]
                        or len(bypass) != 2
                        or ADMIN_BYPASS not in bypass
                    )
                )
                or ruleset.get("current_user_can_bypass") != "always"
            ):
                raise ValueError(
                    "main requires a pull request: the dedicated App exception must "
                    "cover only the unchanged main PR rule and this authenticated App"
                )
        elif grants or ruleset.get("current_user_can_bypass") != "never":
            raise ValueError("docs App must not bypass main's non-PR protections")


def check(app_id_value: str, slug: str) -> dict[str, str]:
    app_id = validate_app_id(app_id_value)
    committer = check_app(app_id, slug)
    check_rules(app_id)
    return committer

"""Plan/apply the owner-approved docs App exception; never handle private keys."""

from __future__ import annotations

import argparse
import copy
import json
import re
import subprocess
import sys
from typing import Any

import release_docs as docs
import release_docs_bot as bot

MAIN_RULESET_ID = 21068957
FIELDS = ("name", "target", "enforcement", "conditions", "rules", "bypass_actors")


def payload(ruleset: dict[str, Any]) -> dict[str, Any]:
    return {key: copy.deepcopy(ruleset[key]) for key in FIELDS}


def plan(current: dict[str, Any], app_id: int) -> dict[str, Any]:
    if (
        current.get("id") != MAIN_RULESET_ID
        or current.get("name") != "main"
        or current.get("source_type") != "Repository"
        or current.get("source") != docs.REPOSITORY
        or current.get("target") != "branch"
        or current.get("enforcement") != "active"
        or current.get("conditions") != bot.MAIN_CONDITIONS
        or current.get("bypass_actors") != [bot.ADMIN_BYPASS]
    ):
        raise ValueError(
            "main policy changed; refuse to rewrite unfamiliar protections"
        )
    rules = current.get("rules", [])
    core = [{"type": kind} for kind in sorted(bot.CORE_RULES)]
    if sorted(rules, key=lambda rule: rule["type"]) not in (
        core,
        sorted([*core, bot.PR_RULE], key=lambda rule: rule["type"]),
    ):
        raise ValueError(
            "main rules/review parameters changed; owner review is required"
        )
    protected = payload(current)
    protected["rules"] = [rule for rule in rules if rule["type"] != "pull_request"]
    reviewed = {
        "name": bot.PR_RULESET_NAME,
        "target": "branch",
        "enforcement": "active",
        "conditions": copy.deepcopy(bot.MAIN_CONDITIONS),
        "rules": [copy.deepcopy(bot.PR_RULE)],
        "bypass_actors": [copy.deepcopy(bot.ADMIN_BYPASS), bot.app_bypass(app_id)],
    }
    return {"protect_main": protected, "review_source": reviewed}


def write(path: str, method: str, data: dict[str, Any]) -> Any:
    result = subprocess.run(
        [
            "gh",
            "api",
            "--method",
            method,
            f"repos/{docs.REPOSITORY}/{path}",
            "--input",
            "-",
        ],
        input=json.dumps(data),
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def check_other_grants(
    entries: list[dict[str, Any]], app_id: int, reviewed_id: int | None
) -> None:
    for entry in entries:
        if entry.get("enforcement") != "active" or entry["id"] == reviewed_id:
            continue
        value = docs.api(f"rulesets/{entry['id']}")
        if "bypass_actors" not in value:
            raise ValueError("owner setup must read full ruleset bypass actors")
        if any(
            actor.get("actor_type") == "Integration" and actor.get("actor_id") == app_id
            for actor in value["bypass_actors"]
        ):
            raise ValueError("dedicated App already bypasses another active ruleset")


def configure(app_id: int, slug: str, apply: bool) -> dict[str, Any]:
    # App registration/install/key upload are the owner's secure UI handoff.
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,99}", slug):
        raise ValueError("dedicated App slug is missing or invalid")
    app = bot.api(f"apps/{slug}")
    if (
        app.get("id") != app_id
        or app.get("permissions") != bot.APP_PERMISSIONS
        or app.get("owner", {}).get("login") != docs.REPOSITORY.split("/")[0]
    ):
        raise ValueError(
            "register the dedicated owner App with exactly the documented permissions"
        )
    if docs.api("").get("default_branch") != "main":
        raise ValueError("main must remain the default branch")
    original = docs.api(f"rulesets/{MAIN_RULESET_ID}")
    change = plan(original, app_id)
    entries = docs.api_pages("rulesets")
    existing = [entry for entry in entries if entry.get("name") == bot.PR_RULESET_NAME]
    if len(existing) > 1:
        raise ValueError("ambiguous existing source-review rulesets")
    reviewed_id = None
    if existing:
        reviewed_id = existing[0]["id"]
        if payload(docs.api(f"rulesets/{reviewed_id}")) != change["review_source"]:
            raise ValueError(
                "existing source-review ruleset differs; refuse to overwrite it"
            )
    elif not any(rule["type"] == "pull_request" for rule in original["rules"]):
        raise ValueError("main is already missing source-review protection")
    check_other_grants(entries, app_id, reviewed_id)
    if not apply:
        return change
    # Create and read back the replacement FIRST. The original PR rule still
    # prevents direct writes until the second operation, so no unprotected gap.
    if reviewed_id is None:
        reviewed_id = write("rulesets", "POST", change["review_source"])["id"]
    if payload(docs.api(f"rulesets/{reviewed_id}")) != change["review_source"]:
        raise ValueError(
            "new review protection readback differs; keep original rules intact"
        )
    # Do not clobber a concurrent policy edit. An extra stricter ruleset is safe
    # if this fails; no automatic rollback removes either source review rule.
    if payload(docs.api(f"rulesets/{MAIN_RULESET_ID}")) != payload(original):
        raise ValueError("main policy advanced; original protections were not changed")
    if payload(original) != change["protect_main"]:
        write(f"rulesets/{MAIN_RULESET_ID}", "PUT", change["protect_main"])
    if (
        payload(docs.api(f"rulesets/{MAIN_RULESET_ID}")) != change["protect_main"]
        or payload(docs.api(f"rulesets/{reviewed_id}")) != change["review_source"]
    ):
        raise ValueError("final protection readback failed; keep the release tag held")
    check_other_grants(docs.api_pages("rulesets"), app_id, reviewed_id)
    return {**change, "source_review_ruleset_id": reviewed_id}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--app-slug", required=True)
    parser.add_argument(
        "--apply", action="store_true", help="apply the explicitly approved exception"
    )
    args = parser.parse_args()
    try:
        result = configure(bot.validate_app_id(args.app_id), args.app_slug, args.apply)
        print(json.dumps(result, indent=2))
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as exc:
        print(f"docs bot configuration failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

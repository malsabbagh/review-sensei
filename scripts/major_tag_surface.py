"""Read reusable-workflow contracts as bounded data, without executing tag code."""

from __future__ import annotations

import copy
import re
from pathlib import Path

import yaml
from check_action_pins import check_workflow_text
from python_release_sources import registry_core_contract


class WorkflowLoader(yaml.SafeLoader):
    # YAML 1.1 treats the Actions key `on` as a boolean. Keep scalar booleans
    # otherwise, but make this workflow loader use YAML 1.2 boolean spellings.
    yaml_implicit_resolvers = copy.deepcopy(yaml.SafeLoader.yaml_implicit_resolvers)
    for _key, _values in yaml_implicit_resolvers.items():
        yaml_implicit_resolvers[_key] = [
            v for v in _values if v[0] != "tag:yaml.org,2002:bool"
        ]
    yaml_implicit_resolvers.setdefault("t", []).append(
        ("tag:yaml.org,2002:bool", re.compile(r"^(?:true|false)$"))
    )
    yaml_implicit_resolvers.setdefault("f", []).append(
        ("tag:yaml.org,2002:bool", re.compile(r"^(?:true|false)$"))
    )

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise ValueError("workflow aliases are unsupported")
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        mapping = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in mapping:
                raise ValueError("workflow contains a non-string or duplicate key")
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def workflow_data(text):
    if len(text.encode()) > 512 * 1024:
        raise ValueError("workflow exceeds the bounded data reader")
    value = yaml.load(text, Loader=WorkflowLoader)
    if not isinstance(value, dict):
        raise ValueError("workflow is not a mapping")
    return value


def validate_surface(root: Path):
    paths = [
        ".github/workflows/review-sensei-run.yml",
        ".github/workflows/review-sensei-review.yml",
    ]
    raw_workflows = [root.joinpath(path).read_bytes() for path in paths]
    texts = [raw.decode("utf-8", errors="strict") for raw in raw_workflows]
    runner, caller = map(workflow_data, texts)
    for text, path in zip(texts, paths):
        violations = check_workflow_text(text, source=path)
        if violations:
            raise ValueError(
                "promoted surface contains unpinned/unsupported Action references"
            )
    call = runner.get("on", {}).get("workflow_call")
    if not isinstance(call, dict):
        raise ValueError("promoted runner is not a reusable workflow")
    inputs = call.get("inputs", {})
    required = {"mode", "repository", "repository_id", "base_ref", "head_ref"}
    if any(
        inputs.get(name, {}).get("required") is not True
        or inputs[name].get("type") != "string"
        for name in required
    ):
        raise ValueError("reusable invocation identity contract is incompatible")
    secrets = call.get("secrets", {})
    if set(secrets) != {
        "OLLAMA_API_KEY",
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
    } or any(value.get("required") is not False for value in secrets.values()):
        raise ValueError("provider secrets must remain explicitly optional")
    calls = []
    for job in caller.get("jobs", {}).values():
        if "/.github/workflows/review-sensei-run.yml@" not in job.get("uses", ""):
            continue
        supplied = job.get("with", {})
        if (
            not set(supplied) <= set(inputs)
            or not required <= set(supplied)
            or job.get("secrets") == "inherit"
        ):
            raise ValueError("caller/reusable invocation or secret contract differs")
        if not set(job.get("secrets", {})) <= set(secrets):
            raise ValueError("caller exposes an undeclared secret")
        calls.append(job["uses"])
    if not calls:
        raise ValueError("promoted caller does not invoke the reusable runner")
    jobs = runner.get("jobs", {})
    bootstrap = jobs.get("bootstrap", {})
    if bootstrap.get("permissions") != {"contents": "read"}:
        raise ValueError("bootstrap must retain read-only permission")
    for job in jobs.values():
        permissions = job.get("permissions", runner.get("permissions", {}))
        if not isinstance(permissions, dict) or any(
            v == "write" and k != "id-token" for k, v in permissions.items()
        ):
            raise ValueError("reusable runner expands repository write permissions")
    steps = [step for job in jobs.values() for step in job.get("steps", [])]
    installs = [
        step
        for step in steps
        if step.get("name")
        == "Install ReviewSensei package (PyPI first, GitHub fallback)"
    ]
    if len(installs) != 4:
        raise ValueError("expected exact installation in all four runtime lanes")
    for step in installs:
        script = step.get("run", "")
        required_fragments = [
            '"review-sensei==$expected_version"',
            '"$installed_version" != "$expected_version"',
        ]
        if "reviewsensei-core-installation:" not in texts[0]:
            required_fragments += [
                '"git+https://github.com/malsabbagh/review-sensei.git@$REVIEW_SENSEI_WORKFLOW_COMMIT"',
                "Could not find a version that satisfies the requirement review-sensei==",
            ]
        if any(
            fragment not in script for fragment in required_fragments
        ) or not step.get("env", {}).get("REVIEW_SENSEI_VERSION"):
            raise ValueError(
                "runtime installation must use the exact source manifest version"
            )
    publication = []
    lifecycle_command = (
        '"$RUNNER_TEMP/review-sensei-venv/bin/review-sensei-github" review-run'
    )
    for name in ("hosted", "local"):
        runtime_steps = jobs.get(name, {}).get("steps", [])
        owners = [step for step in runtime_steps if step.get("id") == "provider-review"]
        if len(owners) != 1:
            raise ValueError("expected one review lifecycle owner in each runtime lane")
        owner = owners[0]
        script = owner.get("run", "")
        if (
            owner.get("if") != "inputs.operation == 'review'"
            or owner.get("env", {}).get("GITHUB_TOKEN") != "${{ github.token }}"
            or owner.get("env", {}).get("POLICY_COMMIT")
            != "${{ needs.bootstrap.outputs.policy_commit }}"
            or script.count(lifecycle_command) != 1
            or '--policy-commit "$POLICY_COMMIT"' not in script
        ):
            raise ValueError("review lifecycle metadata or frozen policy binding is missing")
        if any(
            step.get("id") == "publish-admission"
            or step.get("name", "").startswith("Publish or promote validated")
            or re.search(
                r'review-sensei-github"\s+(?:publish|release-reservation)\b',
                step.get("run", ""),
            )
            for step in runtime_steps
        ):
            raise ValueError("review lifecycle has a separate publication or cleanup owner")
        publication.append(owner)
    identity = next(
        (
            step
            for step in bootstrap.get("steps", [])
            if step.get("id") == "release-identity"
        ),
        {},
    )
    if (
        identity.get("env", {}).get("REVIEW_SENSEI_WORKFLOW_REF")
        != "${{ job.workflow_ref }}"
        or identity.get("env", {}).get("REVIEW_SENSEI_WORKFLOW_SHA")
        != "${{ job.workflow_sha }}"
    ):
        raise ValueError(
            "callee identity contract changed; caller github.workflow_* is not an equivalent binding"
        )
    if 'show "$workflow_commit:pyproject.toml"' not in identity.get("run", ""):
        raise ValueError(
            "runtime version is not bound to the executing workflow commit"
        )
    installation = registry_core_contract(raw_workflows[0])
    return {
        "status": "PASS",
        "data_only": True,
        "caller_references": calls,
        "installation_lanes": len(installs),
        "publication_metadata_lanes": len(publication),
        "core_installation": installation,
        "diagnostics": [
            "actionlint/current context documentation omit job.workflow_*; observed downstream bootstrap resolved the correct callee tag object/source. This static mismatch is not a proven runtime defect; full live job/command execution remains separate."
        ],
        "limitations": [
            "No live reusable invocation, OIDC broker exchange or provider credentials",
            "Structural checks do not prove all GitHub expression/runtime semantics",
        ],
    }

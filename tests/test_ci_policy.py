import importlib.util
import json
import os
import re
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from review_sensei.errors import ReviewInputError

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_action_pins.py"
_SPEC = importlib.util.spec_from_file_location("check_action_pins", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
check_action_pins = _MODULE.check_action_pins
check_workflow_text = _MODULE.check_workflow_text


def _run_blocks(text: str) -> list[str]:
    """Return the full contents of every `run: |` block in workflow text."""

    blocks: list[str] = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^(\s*)run:\s*\|(.*)$", line)
        if match is None:
            continue
        indent = len(match.group(1))
        content: list[str] = []
        for candidate in lines[index + 1 :]:
            if not candidate.strip():
                content.append("")
                continue
            if len(candidate) - len(candidate.lstrip()) <= indent:
                break
            content.append(candidate[indent:])
        blocks.append("\n".join(content))
    return blocks


def _run_block_containing(text: str, marker: str) -> str:
    matches = [block for block in _run_blocks(text) if marker in block]
    if len(matches) != 1:
        raise AssertionError(f"expected one run block containing {marker!r}")
    return matches[0]


def _reusable_workflow_text() -> str:
    return (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "review-sensei-run.yml"
    ).read_text(encoding="utf-8")


def _job_section(text: str, job_id: str) -> str:
    pattern = rf"^  {re.escape(job_id)}:\n"
    match = re.search(pattern, text, flags=re.M)
    if match is None:
        raise AssertionError(f"job {job_id!r} was not found")
    start = match.start()
    next_job = re.search(r"^  [A-Za-z0-9_-]+:\n", text[match.end() :], flags=re.M)
    end = match.end() + next_job.start() if next_job is not None else len(text)
    return text[start:end]


def _named_steps(job_text: str) -> list[str]:
    return [
        match.group(1)
        for match in re.finditer(r"^      - name: (.+)$", job_text, flags=re.M)
    ]


def _step_block(job_text: str, name: str) -> str:
    pattern = rf"^      - name: {re.escape(name)}\n"
    match = re.search(pattern, job_text, flags=re.M)
    if match is None:
        raise AssertionError(f"step {name!r} was not found")
    start = match.start()
    next_step = re.search(r"^      - name: ", job_text[match.end() :], flags=re.M)
    end = match.end() + next_step.start() if next_step is not None else len(job_text)
    return job_text[start:end]


def _reusable_group_templates() -> tuple[str, str]:
    groups = [
        line.split("group:", 1)[1].strip()
        for line in _reusable_workflow_text().splitlines()
        if line.strip().startswith("group:")
    ]
    if len(groups) != 4:
        raise AssertionError(
            f"expected 4 concurrency group templates, found {len(groups)}"
        )
    workflow_group, cloud_provider, openrouter_provider, local_provider = groups
    if cloud_provider != openrouter_provider or cloud_provider != local_provider:
        raise AssertionError("provider group templates diverged across jobs")
    return workflow_group, cloud_provider


def _gha_truthy(value: object) -> bool:
    return value not in (None, False, "")


class _GhaExprParser:
    def __init__(self, expr: str, values: dict[str, object]) -> None:
        self.tokens = re.findall(
            r"'[^']*'|==|&&|\|\||[()]|[A-Za-z][A-Za-z0-9._-]*", expr
        )
        self.index = 0
        self.values = values

    def peek(self) -> str | None:
        if self.index >= len(self.tokens):
            return None
        return self.tokens[self.index]

    def take(self, expected: str | None = None) -> str:
        token = self.peek()
        if token is None:
            raise AssertionError("unexpected end of GitHub expression")
        if expected is not None and token != expected:
            raise AssertionError(f"expected {expected!r}, found {token!r}")
        self.index += 1
        return token

    def parse(self) -> object:
        value = self.parse_or()
        if self.peek() is not None:
            raise AssertionError(
                f"unparsed GitHub expression tokens: {self.tokens[self.index :]}"
            )
        return value

    def parse_or(self) -> object:
        left = self.parse_and()
        while self.peek() == "||":
            self.take("||")
            right = self.parse_and()
            left = left if _gha_truthy(left) else right
        return left

    def parse_and(self) -> object:
        left = self.parse_eq()
        while self.peek() == "&&":
            self.take("&&")
            right = self.parse_eq()
            left = right if _gha_truthy(left) else left
        return left

    def parse_eq(self) -> object:
        left = self.parse_primary()
        if self.peek() == "==":
            self.take("==")
            return left == self.parse_primary()
        return left

    def parse_primary(self) -> object:
        token = self.peek()
        if token == "(":
            self.take("(")
            value = self.parse_or()
            self.take(")")
            return value
        token = self.take()
        if token.startswith("'") and token.endswith("'"):
            return token[1:-1]
        if token not in self.values:
            raise AssertionError(f"unknown GitHub expression identifier {token!r}")
        return self.values[token]


def _eval_gha_group(template: str, values: dict[str, object]) -> str:
    def repl(match: re.Match[str]) -> str:
        value = _GhaExprParser(match.group(1).strip(), values).parse()
        if value is None or value is False:
            return ""
        return str(value)

    return re.sub(r"\$\{\{\s*(.+?)\s*\}\}", repl, template)


def _group_context(
    *,
    operation: str,
    repository: str,
    event_pull_request: int | str | None,
    run_id: str,
    preflight_pull_request: int | str | None = None,
) -> dict[str, object]:
    return {
        "inputs.operation": operation,
        "github.repository": repository,
        "github.run_id": run_id,
        "github.event.pull_request.number": event_pull_request,
        "needs.authoritative-preflight.outputs.pull_request_number": (
            preflight_pull_request
        ),
    }


def _hosted_workflow_group(
    *,
    operation: str,
    repository: str,
    event_pull_request: int | str | None,
    run_id: str,
) -> str:
    """Evaluate the reusable workflow-level group expression for a trigger."""

    template, _ = _reusable_group_templates()
    return _eval_gha_group(
        template,
        _group_context(
            operation=operation,
            repository=repository,
            event_pull_request=event_pull_request,
            run_id=run_id,
        ),
    )


def _hosted_provider_group(
    *,
    operation: str,
    repository: str,
    preflight_pull_request: int | str | None,
    event_pull_request: int | str | None,
    run_id: str,
) -> str:
    """Evaluate the reusable provider-job group expression for a trigger."""

    _, template = _reusable_group_templates()
    return _eval_gha_group(
        template,
        _group_context(
            operation=operation,
            repository=repository,
            event_pull_request=event_pull_request,
            run_id=run_id,
            preflight_pull_request=preflight_pull_request,
        ),
    )


class ActionPinPolicyTests(unittest.TestCase):
    def test_pinned_actions_with_release_comments_pass(self):
        text = """
        steps:
          - uses: actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803 # v6
          - uses: ./local-action
          - uses: docker://alpine:3.20
        """
        self.assertEqual(check_workflow_text(text), [])

    def test_public_reusable_workflow_uses_the_managed_v4_tag(self):
        self.assertEqual(
            check_workflow_text(
                "jobs:\n  call:\n    uses: "
                "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@"
                + "v4\n"
            ),
            [],
        )
        violations = check_workflow_text(
            "jobs:\n  call:\n    uses: "
            "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@v5\n"
        )
        self.assertEqual(len(violations), 1)
        self.assertIn("40-character commit SHA", violations[0])

    def test_mutable_or_undocumented_actions_are_rejected(self):
        text = """
        steps:
          - uses: actions/checkout@v4
          - uses: actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1
        """
        violations = check_workflow_text(text, source="fixture.yml")
        self.assertEqual(len(violations), 2)
        self.assertIn("40-character commit SHA", violations[0])
        self.assertIn("release tag", violations[1])

    def test_repository_scan_is_recursive_and_stable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflow = root / ".github" / "workflows" / "nested" / "ci.yml"
            workflow.parent.mkdir(parents=True)
            workflow.write_text(
                "jobs:\n  test:\n    steps:\n      - uses: actions/checkout@v4\n",
                encoding="utf-8",
            )
            self.assertTrue(check_action_pins(root))

    def test_example_workflow_does_not_interpolate_inputs_inside_run_blocks(self):
        workflow = (
            Path(__file__).resolve().parents[1]
            / "examples"
            / "github-actions"
            / "review-sensei-review.yml"
        )
        text = workflow.read_text(encoding="utf-8")
        run_blocks = _run_blocks(text)
        self.assertTrue(run_blocks)
        self.assertIn("# ReviewSensei setup version: 4", text)
        self.assertIn(
            "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@" + "v4",
            text,
        )
        self.assertIn("OLLAMA_API_KEY: ${{ secrets.OLLAMA_API_KEY }}", text)
        self.assertIn("OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}", text)
        self.assertIn("model: ${{ vars.REVIEWSENSEI_MODEL || '' }}", text)
        self.assertNotIn(
            "provider_profile: ${{ vars.REVIEWSENSEI_PROVIDER_PROFILE || '' }}", text
        )
        self.assertIn("id-token: write", text)
        self.assertIn("github.event.pull_request.draft != true", text)
        for block in run_blocks:
            self.assertNotIn("${{ inputs.", block)
        for input_name in (
            "review_sensei_version",
            "base_ref",
            "head_ref",
            "head_repository",
            "pull_request_number",
            "head_sha",
        ):
            with self.subTest(input_name=input_name):
                self.assertIn(f"inputs.{input_name}", text)
        self.assertIn("REVIEWSENSEI_PROVIDER_MODE", text)
        self.assertIn("REVIEWSENSEI_AUTO_APPROVE", text)
        self.assertEqual(text.count("review-sensei-run.yml@" + "v4"), 1)
        self.assertIn(
            "provider_mode: ${{ vars.REVIEWSENSEI_PROVIDER_MODE || 'local' }}", text
        )
        self.assertIn("resolve-trigger:", text)
        self.assertIn(
            "enable_review: ${{ needs.resolve-trigger.outputs.enable_review == 'true' && 'true' || 'false' }}",
            text,
        )
        self.assertIn('rescan = re.compile(r"\\bre[\\s-]?scan\\b"', text)
        self.assertNotIn("vars.REVIEWSENSEI_PROVIDER_MODE != 'cloud'", text)
        self.assertNotIn("vars.REVIEWSENSEI_PROVIDER_MODE == 'cloud'", text)

    def test_generated_review_workflow_can_authorize_opt_in_replies(self):
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "review-sensei-review.yml"
        )
        text = workflow.read_text(encoding="utf-8")
        self.assertIn("pull-requests: write", text)
        self.assertIn("issues: write", text)
        self.assertIn("github.event.issue.pull_request", text)
        self.assertIn("github.event.comment.author_association == 'OWNER'", text)
        self.assertIn("github.event.comment.author_association == 'MEMBER'", text)
        self.assertIn("github.event.comment.author_association == 'COLLABORATOR'", text)
        self.assertIn("github.event.comment.user.type != 'Bot'", text)
        self.assertIn(
            "github.event_name == 'issue_comment' && github.event.issue.number",
            text,
        )
        self.assertIn(
            "github.event_name == 'workflow_dispatch' && inputs.pull_request_number",
            text,
        )
        self.assertIn("github.event.pull_request.draft != true", text)
        self.assertEqual(text.count("github.event.pull_request.draft != true"), 2)
        self.assertIn("persist-credentials: false", text)
        self.assertIn(
            "ref: ${{ github.event.repository.default_branch }}",
            text,
        )
        self.assertIn(
            "pull_request_number: ${{ needs.resolve-trigger.outputs.pull_request_number }}",
            text,
        )
        self.assertIn("github.event.comment.pull_request_url", text)
        repository_gate = text.find(
            '[[ ! "${REPOSITORY}" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]]'
        )
        pull_fetch = text.find(
            'gh api --method GET "repos/${REPOSITORY}/pulls/${PULL_REQUEST}"'
        )
        self.assertNotEqual(repository_gate, -1)
        self.assertLess(repository_gate, pull_fetch)
        self.assertIn("::error::repository identity is invalid", text)
        self.assertIn(
            "github.event_name == 'workflow_dispatch' ||",
            text,
        )
        self.assertNotIn("PYTHONPATH=src python src/", text)
        self.assertIn(
            "enable_review: ${{ needs.resolve-trigger.outputs.enable_review == 'true' && 'true' || 'false' }}",
            text,
        )
        self.assertIn("contains(github.event.comment.body, '@sensei')", text)
        self.assertIn("while delimiter in title:", text)
        self.assertIn(
            'python - "$pull_json" "$AUTO_REVIEW" "$EVENT_NAME" "$COMMENT_BODY"',
            text,
        )
        self.assertIn('event_name == "pull_request_review_comment"', text)
        self.assertIn("PULL_REQUEST_JSON:", text)
        self.assertIn("printf '%s' \"$PULL_REQUEST_JSON\"", text)
        self.assertNotIn(
            "printf '%s' '${{ toJson(github.event.pull_request) }}'",
            text,
        )
        self.assertNotIn(
            "trusted trigger resolver is missing from the default branch", text
        )

    def test_protection_policy_readback_workflow_is_manual_and_read_only(self):
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "protection-policy-readback.yml"
        )
        text = workflow.read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", text)
        self.assertIn("ruleset_id:", text)
        self.assertIn("tag_ruleset_id:", text)
        self.assertIn("channel_ruleset_id:", text)
        self.assertIn("REVIEWSENSEI_RULESET_READ_TOKEN", text)
        self.assertIn("Validate ruleset read credential scope", text)
        self.assertIn("x-oauth-scopes", text)
        self.assertIn("delete_repo", text)
        self.assertIn("gh api --fail-with-body", text)
        self.assertIn("--readback", text)
        self.assertIn("--tag-readback", text)
        self.assertIn("--channel-readback", text)
        self.assertNotIn("gh api -X", text)
        self.assertNotIn("PATCH", text)
        self.assertNotIn("administration:", text)

    def test_codeql_matrix_is_aggregated_into_required_checks(self):
        workflow = (
            Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
        )
        text = workflow.read_text(encoding="utf-8")
        self.assertIn("language: [python, javascript-typescript]", text)
        self.assertIn(
            "needs: [compatibility, quality, schemas, package, npm, workers, codeql]",
            text,
        )
        self.assertIn('require_success "${{ needs.codeql.result }}"', text)

    def test_codeql_job_is_read_only_and_not_pull_request_target(self):
        workflow = (
            Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
        )
        text = workflow.read_text(encoding="utf-8")
        self.assertIn("pull_request:", text)
        self.assertNotIn("pull_request_target", text)
        self.assertRegex(text, r"(?m)^permissions:\n  contents: read\n")
        codeql_job = text.split("  codeql:\n", 1)[1].split("\n  required-checks:", 1)[0]
        self.assertNotIn("secrets.", codeql_job)
        self.assertNotIn("security-events:", codeql_job)
        self.assertIn("actions: read", codeql_job)
        self.assertIn("contents: read", codeql_job)
        self.assertIn("upload: never", codeql_job)
        codeql_config = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "codeql"
            / "codeql-config.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("deploy/cloudflare", codeql_config)
        self.assertIn("packages/npm/cli", codeql_config)

    def test_ci_proves_findings_gate_on_committed_fixtures(self):
        workflow = (
            Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
        )
        text = workflow.read_text(encoding="utf-8")
        self.assertIn("Prove CodeQL findings gate on committed fixtures", text)
        proof = _run_block_containing(text, "tests/fixtures/codeql/seeded-warning")
        self.assertLess(
            proof.index("set +e"),
            proof.index("tests/fixtures/codeql/clean"),
        )
        self.assertLess(
            proof.rindex("set -e"),
            proof.index("failed=0"),
        )
        self.assertIn("clean_status=$?", proof)
        self.assertIn("seeded_status=$?", proof)
        self.assertIn("malformed_status=$?", proof)
        self.assertIn("tests/fixtures/codeql/clean", proof)
        self.assertIn("tests/fixtures/codeql/malformed", proof)
        self.assertIn("clean fixture must pass the findings gate", proof)
        self.assertIn("seeded warning fixture must fail the findings gate", proof)
        self.assertIn("malformed SARIF fixture must fail the findings gate", proof)
        self.assertEqual(proof.count("failed=1"), 3)

    def test_reusable_workflow_supports_review_and_reply_in_both_provider_modes(self):
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "review-sensei-run.yml"
        )
        text = workflow.read_text(encoding="utf-8")
        self.assertIn("provider_mode:", text)
        self.assertIn("enable_auto_approve:", text)
        self.assertIn(
            "enable_auto_approve:\n        required: false\n        default: 'true'",
            text,
        )
        self.assertIn("AUTO_APPROVE", text)
        self.assertIn("--enable-auto-approve", text)
        self.assertIn("--no-auto-approve", text)
        self.assertIn("inputs.provider_mode == 'cloud'", text)
        self.assertIn("inputs.provider_mode == 'cloud-ollama'", text)
        self.assertIn("inputs.provider_mode == 'local'", text)
        self.assertIn("inputs.provider_mode == 'local-ollama'", text)
        self.assertIn("inputs.provider_mode == 'openrouter'", text)
        self.assertIn("OPENROUTER_API_KEY is required for OpenRouter mode", text)
        self.assertIn("--provider openrouter --model", text)
        self.assertIn("provider_profile is unused", text)
        self.assertIn("allow_unqualified_profile is unused", text)
        self.assertIn("validate-provider-mode:", text)
        self.assertIn(
            "if: github.event_name != 'pull_request' || github.event.pull_request.draft != true",
            text,
        )
        self.assertEqual(text.count("needs: validate-provider-mode"), 2)
        self.assertIn('case "$PROVIDER_MODE" in', text)
        self.assertIn(
            '""|local|local-ollama|cloud|cloud-ollama|openrouter) ;;',
            text,
        )
        self.assertEqual(text.count("github reply \\\n"), 3)
        self.assertEqual(text.count("github review \\\n"), 3)
        self.assertEqual(text.count("--outcome outcome.json"), 3)
        self.assertEqual(text.count("--outcome publication-outcome.json"), 3)
        self.assertEqual(text.count("recovery-artifact.json"), 6)
        self.assertEqual(text.count("Publish or promote"), 3)
        self.assertNotIn("after AI resolution", text)
        self.assertIn("auto_approve_args", text)
        self.assertEqual(text.count("reply_exit=$?"), 3)
        self.assertEqual(text.count("grep -E '^(replied_and_resolved|"), 3)
        self.assertEqual(
            text.count("mention reply command failed (exit $reply_exit)"), 3
        )
        self.assertEqual(text.count('if [[ "$reply_exit" -ne 0 ]]; then'), 3)
        self.assertEqual(text.count("Verify mention reply completed"), 3)
        self.assertEqual(text.count("always() && inputs.operation == 'reply' &&"), 3)
        self.assertEqual(text.count("steps.reply.outcome != 'success'"), 3)
        self.assertIn("runs-on: ubuntu-latest", text)
        self.assertIn("runs-on: [self-hosted, linux, x64, ollama]", text)

    def test_generated_caller_and_reusable_workflow_openrouter_routing_contract(
        self,
    ):
        from review_sensei.hosting.github.setup import SETUP_VARIABLES, SetupPlanBuilder

        repo_root = Path(__file__).resolve().parents[1]
        reusable = (
            repo_root / ".github" / "workflows" / "review-sensei-run.yml"
        ).read_text(encoding="utf-8")
        caller_template = (
            repo_root / "examples" / "github-actions" / "review-sensei-review.yml"
        ).read_text(encoding="utf-8")
        setup_plan = SetupPlanBuilder().build("owner/repo")
        generated_caller = next(
            file.content
            for file in setup_plan.files
            if file.path == ".github/workflows/review-sensei-review.yml"
        )
        self.assertEqual(generated_caller, caller_template)
        self.assertIn(
            "provider_mode: ${{ vars.REVIEWSENSEI_PROVIDER_MODE || 'local' }}",
            generated_caller,
        )
        self.assertIn("model: ${{ vars.REVIEWSENSEI_MODEL || '' }}", generated_caller)
        self.assertNotIn("provider_profile:", generated_caller)
        self.assertIn(("REVIEWSENSEI_MODEL", ""), SETUP_VARIABLES)
        self.assertNotIn(("REVIEWSENSEI_PROVIDER_PROFILE", ""), SETUP_VARIABLES)
        self.assertIn("provider_profile:", reusable)
        self.assertIn("provider_profile is unused", reusable)

        openrouter_gate = "inputs.provider_mode == 'openrouter'"
        cloud_fallback_gate = (
            "inputs.provider_mode == 'cloud' || inputs.provider_mode == 'cloud-ollama' || "
            "(inputs.provider_mode == '' && inputs.mode == 'automatic')"
        )
        local_fallback_gate = (
            "inputs.provider_mode == 'local' || inputs.provider_mode == 'local-ollama' || "
            "(inputs.provider_mode == '' && inputs.mode == 'manual')"
        )
        self.assertIn(openrouter_gate, reusable)
        self.assertIn(cloud_fallback_gate, reusable)
        self.assertIn(local_fallback_gate, reusable)
        self.assertIn("exempt from the repository ENABLE_UBICLOUD_HOSTED", reusable)

        self.assertIn("repository id must be a positive decimal integer", reusable)
        openrouter_job = _job_section(reusable, "openrouter")
        self.assertRegex(
            openrouter_job,
            r"- name: Prepare bounded trusted-base diff\n        if: inputs\.operation == 'review'",
        )
        self.assertIn(
            "ref: ${{ needs.authoritative-preflight.outputs.base_sha || inputs.base_sha || inputs.base_ref }}",
            openrouter_job,
        )
        review_step = _step_block(openrouter_job, "Run OpenRouter-provider review")
        self.assertIn("--provider openrouter --model", review_step)
        self.assertIn("hosted_openrouter_upstream", review_step)
        self.assertNotIn("openrouter model vendor is not allowlisted", review_step)
        reply_step = _step_block(
            openrouter_job, "Generate and publish OpenRouter mention reply"
        )
        self.assertIn("--provider openrouter --model", reply_step)
        self.assertIn("hosted_openrouter_upstream", reply_step)
        publish_step = _step_block(
            openrouter_job, "Publish or promote validated review through the broker"
        )
        self.assertIn(
            "BASE_SHA: ${{ inputs.base_sha || steps.trusted-base.outputs.sha }}",
            publish_step,
        )
        self.assertIn('--repository-id "$REPOSITORY_ID"', publish_step)
        self.assertIn('--base-branch "$BASE_REF"', publish_step)

    def test_reusable_workflow_reply_status_parsing_is_identical_across_provider_jobs(
        self,
    ):
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "review-sensei-run.yml"
        ).read_text(encoding="utf-8")
        reply_blocks = [
            block.strip() for block in workflow.split('reply_status="$(grep -E')[1:]
        ]
        self.assertEqual(len(reply_blocks), 3)
        first = reply_blocks[0].split('" | tail -n 1 || true)"')[0]
        for block in reply_blocks[1:]:
            other = block.split('" | tail -n 1 || true)"')[0]
            self.assertEqual(other, first)
        pull_request_env = (
            "PULL_REQUEST: ${{ inputs.pull_request_number || "
            "github.event.pull_request.number || github.event.issue.number }}"
        )
        expected_permissions = (
            "    permissions:\n"
            "      contents: read\n"
            "      pull-requests: read\n"
            "      issues: read\n"
            "      id-token: write\n"
        )
        for job_id in ("cloud", "openrouter", "local"):
            job = _job_section(workflow, job_id)
            self.assertEqual(job.count(pull_request_env), 4)
        for job_id in ("cloud", "openrouter", "local"):
            job = _job_section(workflow, job_id)
            self.assertIn(expected_permissions, job)
            reply_name = next(
                name
                for name in _named_steps(job)
                if name.startswith("Generate and publish") and "mention reply" in name
            )
            reply = _step_block(job, reply_name)
            self.assertIn("GITHUB_TOKEN: ${{ github.token }}", reply)
            self.assertIn("AUTO_APPROVE: ${{ inputs.enable_auto_approve }}", reply)
            self.assertIn("auto_approve_args=()", reply)
            self.assertIn("auto_approve_args+=(--enable-auto-approve)", reply)
            self.assertIn('"${auto_approve_args[@]}"', reply)
            self.assertIn("github reply \\", reply)
            self.assertIn("reply_exit=$?", reply)
            self.assertIn("grep -E '^(replied_and_resolved|", reply)
            self.assertIn("Verify mention reply completed", job)
            self.assertNotIn("- name: Finalize", reply)
            self.assertNotIn("review-sensei github review", reply)
        self.assertNotIn("- name: Finalize", workflow)
        self.assertNotIn("- name: Promote", workflow)
        self.assertEqual(
            workflow.count("Publish or promote validated review through the broker"),
            2,
        )

    def test_reusable_prepare_diff_binds_immutable_heads_and_reply_groups(self):
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "review-sensei-run.yml"
        )
        text = workflow.read_text(encoding="utf-8")
        self.assertEqual(
            text.count("BASE_REF: ${{ steps.trusted-base.outputs.sha }}"),
            3,
        )
        self.assertEqual(text.count("HEAD_REF: ${{ inputs.head_sha }}"), 3)
        self.assertIn(
            "github.event.pull_request.number || github.run_id",
            text,
        )
        # Concurrency keys are evaluated before validation jobs run, so they
        # must not interpolate the caller-controlled PR number input.
        group_lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip().startswith("group:")
        ]
        self.assertTrue(group_lines)
        for line in group_lines:
            self.assertNotIn("inputs.pull_request_number", line)
        self.assertNotIn(
            '--base-ref "$BASE_REF" --head-ref "$HEAD_REF"'
            "\n            --head-repository",
            text,
        )

    def test_reusable_concurrency_is_pr_scoped_for_reviews_and_run_scoped_for_replies(
        self,
    ):
        """Review cancellation and reply isolation must be explicit in YAML.

        Workflow-level concurrency cannot consume the validated PR output, so
        pull-request events use their host PR number there. The provider jobs
        additionally join manual reviews by the authoritative preflight
        number. Both provider jobs must use the same expression: provider mode
        is an execution detail, not a concurrency partition.
        """

        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "review-sensei-run.yml"
        )
        text = workflow.read_text(encoding="utf-8")
        group_lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip().startswith("group:")
        ]
        self.assertEqual(len(group_lines), 4)

        top_level, cloud, openrouter, local = group_lines
        review_selector = "inputs.operation == 'review'"
        reply_selector = "inputs.operation == 'review' && ("
        self.assertIn(review_selector, top_level)
        self.assertIn(reply_selector, top_level)
        self.assertIn("github.event.pull_request.number", top_level)
        # A pull_request_review_comment reply must not fall back to the PR
        # number; doing so would replace an earlier reply for that PR.
        self.assertTrue(
            top_level.endswith(
                "${{ inputs.operation == 'review' && (github.event.pull_request.number || github.run_id) || github.run_id }}"
            )
        )

        expected_provider_group = (
            "group: reviewsensei-provider-${{ inputs.operation == 'review' && "
            "'review' || 'reply' }}-${{ github.repository }}-${{ inputs.operation == "
            "'review' && (needs.authoritative-preflight.outputs.pull_request_number || "
            "github.event.pull_request.number || github.run_id) || github.run_id }}"
        )
        # OpenRouter intentionally shares the provider review slot with cloud/local.
        self.assertEqual(cloud, expected_provider_group)
        self.assertEqual(openrouter, expected_provider_group)
        self.assertEqual(local, expected_provider_group)
        cloud_job = _job_section(text, "cloud")
        openrouter_job = _job_section(text, "openrouter")
        local_job = _job_section(text, "local")
        cancel_expr = "cancel-in-progress: ${{ inputs.operation == 'review' }}"
        self.assertEqual(
            [
                line.strip()
                for line in cloud_job.splitlines()
                if "cancel-in-progress:" in line
            ],
            [cancel_expr],
        )
        self.assertEqual(
            [
                line.strip()
                for line in openrouter_job.splitlines()
                if "cancel-in-progress:" in line
            ],
            [cancel_expr],
        )
        self.assertEqual(
            [
                line.strip()
                for line in local_job.splitlines()
                if "cancel-in-progress:" in line
            ],
            [cancel_expr],
        )
        self.assertNotIn("-cloud-", cloud)
        self.assertNotIn("-openrouter-", openrouter)
        self.assertNotIn("-local-", local)
        self.assertIn(
            "OpenRouter shares the provider review group with cloud/local",
            text,
        )

        cancel_lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip().startswith("cancel-in-progress:")
        ]
        # Exactly one cancellation policy per block: latest-wins applies to
        # reviews, while replies always retain their unique run-id group.
        self.assertEqual(
            cancel_lines,
            [
                "cancel-in-progress: ${{ inputs.operation == 'review' && github.event.pull_request.number != null }}",
                "cancel-in-progress: ${{ inputs.operation == 'review' }}",
                "cancel-in-progress: ${{ inputs.operation == 'review' }}",
                "cancel-in-progress: ${{ inputs.operation == 'review' }}",
            ],
        )

    def test_reusable_workflow_preflights_authoritative_identity_and_gates_operations(
        self,
    ):
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "review-sensei-run.yml"
        )
        text = workflow.read_text(encoding="utf-8")
        self.assertIn("if: needs.validate-provider-mode.result == 'success'", text)
        self.assertIn(
            "base_sha: ${{ steps.fetch-pr.outputs.base_sha }}",
            text,
        )
        self.assertIn(
            "repository id must be a positive decimal integer",
            text,
        )
        self.assertIn(
            "authoritative GitHub pull-request preflight was unavailable; refusing provider execution",
            text,
        )
        self.assertIn(
            "encoded pull request title exceeds the workflow output limit",
            text,
        )
        self.assertIn(
            "authoritative pull-request title output could not be decoded",
            text,
        )
        # Provider jobs are selected only for an enabled operation. A reply
        # event cannot accidentally run the review CLI or consume a provider
        # runner when mention replies are disabled.
        expected_gate = (
            "(inputs.operation == 'review' && inputs.enable_review == 'true') || "
            "(inputs.operation == 'reply' && inputs.enable_github_writes == 'true' "
            "&& inputs.enable_mention_replies == 'true')"
        )
        self.assertEqual(text.count(expected_gate), 3)
        self.assertEqual(
            text.count(
                "if: inputs.operation == 'review' && inputs.enable_review == 'true'"
            ),
            3,
        )
        self.assertEqual(
            text.count(
                "if: inputs.operation == 'reply' && inputs.enable_github_writes == 'true' && inputs.enable_mention_replies == 'true'"
            ),
            3,
        )

    def test_ci_restores_strict_branch_coverage_and_bounds_workflow_identity(self):
        root = Path(__file__).resolve().parents[1]
        ci_text = (root / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        workflow_text = (
            root / ".github" / "workflows" / "review-sensei-run.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("--fail-under=80", ci_text)
        self.assertNotIn("--fail-under=75", ci_text)
        self.assertIn('len(title.encode("utf-8")) > 256', workflow_text)
        self.assertIn('"$BASE_REF" == *--*', workflow_text)
        self.assertIn('"$BASE_REF" == *-', workflow_text)
        self.assertIn('"$BASE_REF" != *--*', workflow_text)
        self.assertIn('"$BASE_REF" != *-', workflow_text)
        self.assertIn('"--" not in value', workflow_text)
        self.assertIn('not value.endswith("-")', workflow_text)

    def test_validate_provider_mode_rejects_cross_backend_model_slugs(self):
        from review_sensei.provider_config import validate_hosted_workflow_model

        with self.assertRaisesRegex(
            ReviewInputError, "must not use vendor/model openrouter slug"
        ):
            validate_hosted_workflow_model(
                provider_mode="cloud-ollama",
                workflow_mode="automatic",
                model="deepseek/deepseek-v4.1-flash",
            )

    def test_validate_provider_mode_job_validates_model_input(self):
        workflow_text = _reusable_workflow_text()
        job = _job_section(workflow_text, "validate-provider-mode")
        self.assertNotIn("actions/checkout@", job)
        self.assertIn("permissions: {}", job)
        self.assertIn("MODEL: ${{ inputs.model }}", job)
        self.assertIn("Install ReviewSensei package (PyPI first, GitHub fallback)", job)
        model_block = _run_block_containing(
            workflow_text, "validate_hosted_workflow_model"
        )
        self.assertIn('"$RUNNER_TEMP/review-sensei-venv/bin/python"', model_block)
        self.assertNotIn("PYTHONPATH=src", job)

    def test_validate_provider_mode_rejects_consecutive_and_trailing_hyphens(self):
        root = Path(__file__).resolve().parents[1]
        workflow_text = (
            root / ".github" / "workflows" / "review-sensei-run.yml"
        ).read_text(encoding="utf-8")
        block = _run_block_containing(
            workflow_text, "ReviewSensei provider mode is unsupported"
        )
        self.assertIn('"$BASE_REF" == *--*', block)
        self.assertIn('"$BASE_REF" == *-', block)
        self.assertIn('"$HEAD_REF" == *--*', block)
        self.assertIn('"$HEAD_REF" == *-', block)

    def test_local_provider_validation_uses_the_same_safe_ref_rules(self):
        root = Path(__file__).resolve().parents[1]
        workflow_text = (
            root / ".github" / "workflows" / "review-sensei-run.yml"
        ).read_text(encoding="utf-8")
        block = _run_block_containing(
            workflow_text, "local job received an invalid provider mode"
        )
        block = block.split("python - <<'PY'\n", 1)[1]
        block = textwrap.dedent(re.split(r"\n\s*PY\s*\n?$", block, maxsplit=1)[0])
        environment = {
            "MODE": "manual",
            "OPERATION": "reply",
            "PROVIDER_MODE": "local",
            "REPOSITORY": "owner/repo",
            "HEAD_REF": "",
            "HEAD_REPOSITORY": "",
            "REVIEW_SENSEI_VERSION": "0.1.1",
            "BASE_SHA": "",
            "HEAD_SHA": "",
        }
        for value in ("main--branch", "main-"):
            with self.subTest(value=value):
                result = subprocess.run(
                    ["python3", "-c", block],
                    env={**os.environ, **environment, "BASE_REF": value},
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
        valid = subprocess.run(
            ["python3", "-c", block],
            env={**os.environ, **environment, "BASE_REF": "main-feature"},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(valid.returncode, 0, valid.stderr)

    def test_authoritative_preflight_caps_pull_request_title_bytes(self):
        root = Path(__file__).resolve().parents[1]
        workflow_text = (
            root / ".github" / "workflows" / "review-sensei-run.yml"
        ).read_text(encoding="utf-8")
        block = _run_block_containing(
            workflow_text, "pull request title exceeds the 256-byte limit"
        )
        script = block.split("python - \"$payload\" <<'PY'\n", 1)[1]
        script = textwrap.dedent(re.split(r"\n\s*PY\s*\n?$", script, maxsplit=1)[0])
        metadata = {
            "state": "open",
            "draft": False,
            "number": 1,
            "title": "x" * 257,
            "head": {
                "sha": "b" * 40,
                "repo": {"full_name": "owner/repo", "id": 42, "fork": False},
            },
            "base": {
                "ref": "main",
                "sha": "a" * 40,
                "repo": {"full_name": "owner/repo", "id": 42},
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            payload = Path(temporary) / "pull-request.json"
            output = Path(temporary) / "github-output"
            payload.write_text(json.dumps(metadata), encoding="utf-8")
            environment = {
                "PULL_REQUEST": "1",
                "REPOSITORY": "owner/repo",
                "REPOSITORY_ID": "42",
                "HEAD_SHA": "b" * 40,
                "BASE_REF": "main",
                "BASE_SHA": "",
                "HEAD_REPOSITORY": "owner/repo",
                "GITHUB_OUTPUT": str(output),
            }
            rejected = subprocess.run(
                ["python3", "-c", script, str(payload)],
                env={**os.environ, **environment},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("256-byte limit", rejected.stderr)

            metadata["title"] = "x" * 256
            payload.write_text(json.dumps(metadata), encoding="utf-8")
            accepted = subprocess.run(
                ["python3", "-c", script, str(payload)],
                env={**os.environ, **environment},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(accepted.returncode, 0, accepted.stderr)

    def test_reusable_workflow_prefers_pypi_with_sha_verified_github_fallback(self):
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "review-sensei-run.yml"
        )
        text = workflow.read_text(encoding="utf-8")
        self.assertEqual(
            text.count("Install ReviewSensei package (PyPI first, GitHub fallback)"),
            4,
        )
        self.assertEqual(
            text.count("REVIEW_SENSEI_WORKFLOW_REF: ${{ job.workflow_ref }}"),
            4,
        )
        self.assertEqual(
            text.count(
                '"git+https://github.com/malsabbagh/review-sensei.git@$REVIEW_SENSEI_WORKFLOW_SHA"'
            ),
            4,
        )
        self.assertIn(
            '"review-sensei==$expected_version"',
            text,
        )
        self.assertIn("No matching distribution found for review-sensei==", text)
        self.assertIn(
            "Could not find a version that satisfies the requirement review-sensei==",
            text,
        )
        self.assertIn("refusing the GitHub fallback", text)
        self.assertIn("must run from a public git tag", text)
        self.assertIn("@refs/tags/[A-Za-z0-9]", text)
        self.assertNotIn("dogfood_ref", text)
        self.assertNotIn("refs/pull/", text)
        self.assertIn("installing the verified ReviewSensei workflow commit", text)
        self.assertNotIn(
            "git ls-remote https://github.com/malsabbagh/review-sensei.git", text
        )
        self.assertIn('importlib.metadata.version("review-sensei")', text)
        self.assertIn('"$python_bin" -m pip check', text)
        self.assertNotIn("review-sensei.git@main", text)
        install_blocks = [
            block
            for block in _run_blocks(text)
            if "REVIEW_SENSEI_WORKFLOW_REF" in block
        ]
        self.assertEqual(len(install_blocks), 4)
        for block in install_blocks:
            with self.subTest(block=block[:40]):
                self.assertLess(
                    block.index('"review-sensei==$expected_version"'),
                    block.index(
                        '"git+https://github.com/malsabbagh/review-sensei.git@$REVIEW_SENSEI_WORKFLOW_SHA"'
                    ),
                )

    def test_setup_v4_run_name_matches_worker_template(self):
        from review_sensei.hosting.github.setup import _resolve_trigger_workflow

        root = Path(__file__).resolve().parents[1]
        python_line = next(
            line
            for line in _resolve_trigger_workflow("v4").splitlines()
            if line.startswith("run-name:")
        )
        worker_source = (
            root / "deploy" / "cloudflare" / "src" / "setup-content.ts"
        ).read_text(encoding="utf-8")
        marker = 'run-name: "ReviewSensei @@{{ github.event.pull_request && format('
        start = worker_source.index(marker)
        end = worker_source.index('"', start + len('run-name: "'))
        worker_line = worker_source[start : end + 1].replace("@@{{", "${{")
        self.assertEqual(python_line, worker_line)

    def test_run_name_quotes_hash_so_yaml_does_not_comment_it_out(self):
        quoted = (
            'run-name: "ReviewSensei ${{ github.event.pull_request && '
            "format('PR #{0}', github.event.pull_request.number) || 'manual' }}\""
        )
        root = Path(__file__).resolve().parents[1]
        for relative in (
            ".github/workflows/review-sensei-review.yml",
            "examples/github-actions/review-sensei-review.yml",
        ):
            with self.subTest(relative=relative):
                text = (root / relative).read_text(encoding="utf-8")
                self.assertIn(quoted, text)
                run_name_lines = [
                    line for line in text.splitlines() if line.startswith("run-name:")
                ]
                self.assertEqual(run_name_lines, [quoted])

    def test_generated_dispatch_is_review_only(self):
        text = (
            Path(__file__).resolve().parents[1]
            / "examples"
            / "github-actions"
            / "review-sensei-review.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("options: [review]", text)
        self.assertNotIn("options: [review, reply]", text)

    def test_ci_runs_fixture_evaluation_without_live_or_secret_flags(self):
        workflow = (
            Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
        )
        text = workflow.read_text(encoding="utf-8")
        self.assertIn("scripts/validate_evaluation_corpus.py", text)
        self.assertIn("--mode fixture", text)
        self.assertIn("evaluation/v1/corpus.json", text)
        self.assertNotIn("--allow-live-model", text)
        self.assertNotIn("--allow-data-egress", text)
        self.assertNotIn("OLLAMA_API_KEY", text)

    def test_ci_npm_pack_disables_lifecycle_scripts(self):
        workflow = (
            Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
        )
        self.assertIn(
            "npm pack --ignore-scripts --dry-run --json packages/npm/cli",
            workflow.read_text(encoding="utf-8"),
        )

    def test_public_npm_restores_posix_artifact_modes(self):
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "publish-npm.yml"
        )
        text = workflow.read_text(encoding="utf-8")
        self.assertIn("Restore POSIX executable modes", text)
        self.assertIn(
            'mapfile -t payloads < <(find "$root" -type f -name review-sensei -print)',
            text,
        )
        self.assertIn('chmod u=rwx,go=rx "${payloads[0]}"', text)

    def test_ci_omits_non_ubicloud_matrix_lanes_when_enabled(self):
        workflow = (
            Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
        )
        text = workflow.read_text(encoding="utf-8")
        match = re.search(
            r"matrix: \$\{\{ fromJSON\(vars\.ENABLE_UBICLOUD_HOSTED == 'true'"
            r" && '(?P<enabled>\{.*?\})' \|\| '(?P<fallback>\{.*\})'\) \}\}",
            text,
        )
        self.assertIsNotNone(match)
        assert match is not None
        enabled = json.loads(match.group("enabled"))
        fallback = json.loads(match.group("fallback"))
        self.assertEqual(
            enabled,
            {
                "include": [
                    {"os": "ubuntu-latest", "python-version": "3.11"},
                    {"os": "ubuntu-latest", "python-version": "3.14"},
                ]
            },
        )
        self.assertEqual(
            fallback,
            {
                "include": [
                    {"os": "ubuntu-latest", "python-version": "3.11"},
                    {"os": "windows-latest", "python-version": "3.12"},
                    {"os": "macos-latest", "python-version": "3.13"},
                    {"os": "ubuntu-latest", "python-version": "3.14"},
                ]
            },
        )

    def test_every_active_workflow_job_uses_the_ubicloud_switch(self):
        workflow_root = Path(__file__).resolve().parents[1] / ".github" / "workflows"
        workflow_paths = sorted(workflow_root.glob("*.yml"))
        self.assertTrue(workflow_paths)
        for workflow in workflow_paths:
            with self.subTest(workflow=workflow.name):
                if workflow.name == "review-sensei-run.yml":
                    # This public reusable workflow intentionally runs
                    # on GitHub-hosted compute (automatic cloud) or the explicit
                    # trusted Ollama self-hosted label; the repository's private
                    # CI runner policy does not apply to this public contract.
                    continue
                lines = workflow.read_text(encoding="utf-8").splitlines()
                runs_on_lines = [
                    line.strip()
                    for line in lines
                    if line.strip().startswith("runs-on:")
                ]
                if not runs_on_lines:
                    # A caller of a reusable workflow has no runner of its
                    # own. Its called workflow owns runner selection instead.
                    self.assertTrue(
                        [line for line in lines if line.strip().startswith("uses:")]
                    )
                    continue
                self.assertTrue(runs_on_lines)
                for line in runs_on_lines:
                    # npm Trusted Publishing must use an explicitly eligible
                    # GitHub-hosted runner, independent of the private
                    # Ubicloud switch used by repository CI.
                    if workflow.name in {"release.yml", "publish-npm.yml"} and line == (
                        "runs-on: ubuntu-latest"
                    ):
                        continue
                    self.assertIn("vars.ENABLE_UBICLOUD_HOSTED", line)
                    self.assertIn("ubicloud-standard-2", line)

    def test_package_job_records_digests_and_runs_packaged_dist_safe_tests(self):
        workflow = (
            Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
        )
        text = workflow.read_text(encoding="utf-8")
        self.assertIn("sha256sum dist/*.tar.gz dist/*.whl", text)
        self.assertIn("REVIEWSENSEI_DIST_SAFE_LANE=1", text)
        self.assertIn('REVIEWSENSEI_CHECKOUT_ROOT="$GITHUB_WORKSPACE"', text)
        self.assertIn("pip install dist/*.whl", text)
        self.assertIn('unittest discover -s "$suite/dist_safe" -v', text)
        self.assertIn('unittest discover -s "$suite/downstream" -v', text)
        self.assertNotIn(
            'unittest discover -s "$GITHUB_WORKSPACE/tests" -v',
            text,
        )
        block = _run_block_containing(text, "REVIEWSENSEI_DIST_SAFE_LANE")
        self.assertNotIn("requirements/ci.txt", block)
        self.assertIn('test ! -e "$suite/test_ci_policy.py"', block)

    def test_downstream_canary_is_operator_gated_and_excluded_from_required_checks(
        self,
    ):
        root = Path(__file__).resolve().parents[1]
        canary = (root / ".github" / "workflows" / "downstream-canary.yml").read_text(
            encoding="utf-8"
        )
        ci = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", canary)
        self.assertNotIn("pull_request:", canary)
        self.assertIn("environment: downstream-canary", canary)
        self.assertIn("contents: read", canary)
        self.assertNotIn("OLLAMA_API_KEY", canary)
        self.assertNotIn("secrets:", canary)
        self.assertIn("Required checks", ci)
        self.assertIn(
            "needs: [compatibility, quality, schemas, package, npm, workers, codeql]",
            ci,
        )
        self.assertNotIn("downstream-canary", ci)
        self.assertNotIn("Downstream canary", ci)


class ReusablePublishGuardTests(unittest.TestCase):
    def test_cloud_and_local_publish_require_success_and_reject_cancelled_stale_runs(
        self,
    ):
        text = _reusable_workflow_text()
        admission_if = (
            "if: success() && !cancelled() && inputs.operation == 'review' "
            "&& inputs.enable_github_writes == 'true'"
        )
        publish_if = (
            admission_if + " && steps.publish-admission.outputs.status == 'current'"
        )
        confirm_name = "Confirm live pull-request head is still current"
        self.assertEqual(text.count(confirm_name), 3)
        self.assertEqual(text.count(publish_if), 3)
        self.assertEqual(text.count("id: publish-admission"), 3)
        self.assertNotIn(
            "if: inputs.operation == 'review' && inputs.enable_github_writes == 'true'\n",
            text,
        )

        for job_id, publish_name in (
            ("cloud", "Publish or promote validated review through the broker"),
            ("openrouter", "Publish or promote validated review through the broker"),
            ("local", "Publish or promote trusted local review and learnings"),
        ):
            with self.subTest(job=job_id):
                job = _job_section(text, job_id)
                names = _named_steps(job)
                publish_index = names.index(publish_name)
                self.assertEqual(names[publish_index - 1], confirm_name)
                revalidate = _step_block(job, confirm_name)
                publish = _step_block(job, publish_name)
                self.assertIn(admission_if, revalidate)
                self.assertNotIn("steps.publish-admission.outputs.status", revalidate)
                self.assertIn(publish_if, publish)
                self.assertIn("GH_TOKEN: ${{ github.token }}", revalidate)
                self.assertIn(
                    'gh api --method GET "repos/${REPOSITORY}/pulls/${PULL_REQUEST}"',
                    revalidate,
                )
                self.assertIn("--jq '.head.sha'", revalidate)
                self.assertIn("for attempt in 1 2 3", revalidate)
                self.assertIn("HTTP\\ (401|403|404)", revalidate)
                self.assertIn("jitter_hundredths", revalidate)
                self.assertIn('sleep "${delay}.${jitter_hundredths}"', revalidate)
                self.assertNotIn("2>/dev/null", revalidate)
                self.assertNotIn('sleep "$attempt"', revalidate)
                self.assertEqual(revalidate.count("echo 'skipped_stale'"), 1)
                self.assertIn("status=skipped_stale", revalidate)
                self.assertIn("status=current", revalidate)
                self.assertIn("refusing publish", revalidate)
                self.assertNotIn("--allow-write", revalidate)
                self.assertNotIn("github review", revalidate)
                self.assertNotIn("/github/token", revalidate)
                self.assertNotIn("id-token", revalidate)
                self.assertNotIn("OLLAMA_API_KEY", revalidate)
                self.assertIn("github review", publish)

        cloud_confirm = _step_block(_job_section(text, "cloud"), confirm_name)
        local_confirm = _step_block(_job_section(text, "local"), confirm_name)
        self.assertEqual(cloud_confirm, local_confirm)


class PythonWorkflowConcurrencyParityTests(unittest.TestCase):
    def test_python_and_workflow_review_groups_are_pr_scoped_sha_free_and_latest_wins(
        self,
    ):
        from review_sensei.concurrency import ReviewConcurrencyPlan

        text = _reusable_workflow_text()
        plan = ReviewConcurrencyPlan.for_pull_request("acme/api", 7)
        self.assertEqual(plan.workflow_key, "review-sensei:review:8:acme/api:1:7")
        self.assertTrue(plan.workflow.cancel_in_progress)
        self.assertNotIn("sha", plan.workflow_key.lower())
        self.assertNotIn("head", plan.workflow_key)

        first_head = "a" * 40
        second_head = "b" * 40
        automatic_a = _hosted_workflow_group(
            operation="review",
            repository="acme/api",
            event_pull_request=7,
            run_id=f"run-{first_head}",
        )
        automatic_b = _hosted_workflow_group(
            operation="review",
            repository="acme/api",
            event_pull_request=7,
            run_id=f"run-{second_head}",
        )
        self.assertEqual(automatic_a, "reviewsensei-review-acme/api-7")
        self.assertEqual(automatic_a, automatic_b)
        self.assertNotIn(first_head, automatic_a)
        self.assertNotIn(second_head, automatic_b)

        group_lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip().startswith("group:")
        ]
        self.assertEqual(len(group_lines), 4)
        for line in group_lines:
            self.assertIn("github.repository", line)
            self.assertIn("inputs.operation == 'review'", line)
            self.assertNotIn("head_sha", line)
            self.assertNotIn("inputs.head_sha", line)
            self.assertNotIn("github.sha", line)
            self.assertNotIn("github.event.pull_request.head.sha", line)

        top_level, cloud, openrouter, local = group_lines
        self.assertIn("github.event.pull_request.number || github.run_id", top_level)
        self.assertIn(
            "needs.authoritative-preflight.outputs.pull_request_number", cloud
        )
        self.assertEqual(cloud, openrouter)
        self.assertEqual(cloud, local)

    def test_manual_and_automatic_reviews_share_the_provider_latest_wins_group(self):
        automatic = _hosted_provider_group(
            operation="review",
            repository="acme/api",
            preflight_pull_request=7,
            event_pull_request=7,
            run_id="auto-111",
        )
        manual = _hosted_provider_group(
            operation="review",
            repository="acme/api",
            preflight_pull_request=7,
            event_pull_request=None,
            run_id="manual-222",
        )
        self.assertEqual(automatic, "reviewsensei-provider-review-acme/api-7")
        self.assertEqual(automatic, manual)

        workflow_automatic = _hosted_workflow_group(
            operation="review",
            repository="acme/api",
            event_pull_request=7,
            run_id="auto-111",
        )
        workflow_manual = _hosted_workflow_group(
            operation="review",
            repository="acme/api",
            event_pull_request=None,
            run_id="manual-222",
        )
        self.assertEqual(workflow_automatic, "reviewsensei-review-acme/api-7")
        self.assertEqual(workflow_manual, "reviewsensei-review-acme/api-manual-222")

    def test_different_pull_requests_are_isolated(self):
        from review_sensei.concurrency import ReviewConcurrencyPlan

        first = ReviewConcurrencyPlan.for_pull_request("acme/api", 7)
        second = ReviewConcurrencyPlan.for_pull_request("acme/api", 8)
        other_repo = ReviewConcurrencyPlan.for_pull_request("other/api", 7)
        self.assertNotEqual(first.workflow_key, second.workflow_key)
        self.assertNotEqual(first.workflow_key, other_repo.workflow_key)

        hosted_first = _hosted_provider_group(
            operation="review",
            repository="acme/api",
            preflight_pull_request=7,
            event_pull_request=7,
            run_id="shared",
        )
        hosted_second = _hosted_provider_group(
            operation="review",
            repository="acme/api",
            preflight_pull_request=8,
            event_pull_request=8,
            run_id="shared",
        )
        hosted_other = _hosted_provider_group(
            operation="review",
            repository="other/api",
            preflight_pull_request=7,
            event_pull_request=7,
            run_id="shared",
        )
        self.assertNotEqual(hosted_first, hosted_second)
        self.assertNotEqual(hosted_first, hosted_other)

    def test_reply_groups_use_run_id_and_do_not_share_the_review_cancel_group(self):
        from review_sensei.concurrency import ReviewConcurrencyPlan

        review = ReviewConcurrencyPlan.for_pull_request("acme/api", 7)
        reply = ReviewConcurrencyPlan.for_non_review_trigger("acme/api", "run-99")
        self.assertNotEqual(review.workflow_key, reply.workflow_key)
        self.assertIsNone(reply.provider)
        self.assertTrue(reply.workflow_key.startswith("review-sensei:trigger:"))

        hosted_review = _hosted_workflow_group(
            operation="review",
            repository="acme/api",
            event_pull_request=7,
            run_id="run-99",
        )
        hosted_reply = _hosted_workflow_group(
            operation="reply",
            repository="acme/api",
            event_pull_request=7,
            run_id="run-99",
        )
        hosted_reply_provider = _hosted_provider_group(
            operation="reply",
            repository="acme/api",
            preflight_pull_request=7,
            event_pull_request=7,
            run_id="run-99",
        )
        self.assertEqual(hosted_review, "reviewsensei-review-acme/api-7")
        self.assertEqual(hosted_reply, "reviewsensei-reply-acme/api-run-99")
        self.assertEqual(
            hosted_reply_provider, "reviewsensei-provider-reply-acme/api-run-99"
        )
        self.assertNotEqual(hosted_review, hosted_reply)
        self.assertNotEqual(hosted_review, hosted_reply_provider)

        text = _reusable_workflow_text()
        cancel_lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip().startswith("cancel-in-progress:")
        ]
        self.assertEqual(
            cancel_lines,
            [
                "cancel-in-progress: ${{ inputs.operation == 'review' && github.event.pull_request.number != null }}",
                "cancel-in-progress: ${{ inputs.operation == 'review' }}",
                "cancel-in-progress: ${{ inputs.operation == 'review' }}",
                "cancel-in-progress: ${{ inputs.operation == 'review' }}",
            ],
        )


if __name__ == "__main__":
    unittest.main()

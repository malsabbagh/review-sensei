import importlib.util
import json
import os
import re
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from review_sensei.configuration import parse_configuration_text
from review_sensei.errors import ReviewInputError
from review_sensei.hosted import hosted_plan_outputs, plan_hosted_execution
from review_sensei.hosting.github.setup import _tagged_workflow

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


def _install_fallback_region(block: str) -> str:
    """Return the tagged-release fallback body of one PyPI-fallback install step."""

    start = block.index('expected_version="${REVIEW_SENSEI_VERSION#v}"')
    end = block.index('"$python_bin" -m pip install', start)
    return block[start:end]


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


def _job_ids(text: str) -> list[str]:
    """Return every job id defined under the workflow's `jobs:` mapping."""

    match = re.search(r"^jobs:\n", text, flags=re.M)
    if match is None:
        raise AssertionError("the workflow defines no jobs")
    ids: list[str] = []
    for candidate in text[match.end() :].splitlines():
        if candidate and not candidate.startswith(" "):
            break
        key = re.match(r"^  ([A-Za-z0-9_-]+):$", candidate)
        if key is not None:
            ids.append(key.group(1))
    return ids


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
    workflow_group, command_group, hosted_provider, local_provider = groups
    if command_group != "reviewsensei-command-${{ github.run_id }}":
        raise AssertionError("command group must be isolated by host run identity")
    if hosted_provider != local_provider:
        raise AssertionError("provider group templates diverged across jobs")
    return workflow_group, hosted_provider


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

    def test_public_reusable_workflow_uses_the_managed_v5_tag(self):
        self.assertEqual(
            check_workflow_text(
                "jobs:\n  call:\n    uses: "
                "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@"
                + "v5\n"
            ),
            [],
        )
        violations = check_workflow_text(
            "jobs:\n  call:\n    uses: "
            "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@v4\n"
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

    def test_one_action_pinned_to_two_commits_in_a_workflow_is_rejected(self):
        text = """
        steps:
          - uses: github/codeql-action/init@1c5b675653bb5c22dbe9b12b556ec555138e09fd # v4.38.1
          - uses: github/codeql-action/analyze@b96794f015dfd88f77b49b1c93e0fa7110f94c63 # v4.38.0
        """
        violations = check_workflow_text(text, source="ci.yml")
        self.assertEqual(len(violations), 1)
        self.assertIn("one commit pin per workflow", violations[0])
        self.assertIn("1c5b675653bb5c22dbe9b12b556ec555138e09fd", violations[0])
        self.assertIn("b96794f015dfd88f77b49b1c93e0fa7110f94c63", violations[0])

    def test_matching_subpath_pins_are_accepted(self):
        text = """
        steps:
          - uses: github/codeql-action/init@1c5b675653bb5c22dbe9b12b556ec555138e09fd # v4.38.1
          - uses: github/codeql-action/analyze@1c5b675653bb5c22dbe9b12b556ec555138e09fd # v4.38.1
        """
        self.assertEqual(check_workflow_text(text), [])

    def test_one_commit_documented_with_two_tags_is_rejected(self):
        text = """
        steps:
          - uses: github/codeql-action/init@1c5b675653bb5c22dbe9b12b556ec555138e09fd # v4.38.1
          - uses: github/codeql-action/analyze@1c5b675653bb5c22dbe9b12b556ec555138e09fd # v4.37.0
        """
        violations = check_workflow_text(text, source="ci.yml")
        self.assertEqual(len(violations), 1)
        self.assertIn("several release tags", violations[0])

    def test_version_skew_is_scoped_to_a_single_workflow_document(self):
        # The shipped examples deliberately use different checkout versions.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflows = root / ".github" / "workflows"
            workflows.mkdir(parents=True)
            (workflows / "one.yml").write_text(
                "steps:\n  - uses: actions/checkout@"
                "d23441a48e516b6c34aea4fa41551a30e30af803 # v6\n",
                encoding="utf-8",
            )
            (workflows / "two.yml").write_text(
                "steps:\n  - uses: actions/checkout@"
                "3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1\n",
                encoding="utf-8",
            )
            self.assertEqual(check_action_pins(root), [])

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
        self.assertIn("# ReviewSensei setup version: 5", text)
        self.assertIn(
            "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@" + "v5",
            text,
        )
        self.assertIn("OLLAMA_API_KEY: ${{ secrets.OLLAMA_API_KEY }}", text)
        self.assertIn("OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}", text)
        self.assertIn("OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}", text)
        self.assertIn("id-token: write", text)
        # The caller is an invocation-only bridge: every policy decision
        # (drafts, writes, automatic reviews, mentions) belongs to the reusable
        # workflow, so the caller reads no repository variables at all.
        self.assertNotIn("vars.", text)
        self.assertNotIn("github.event.pull_request.draft", text)
        self.assertIn("types: [opened, reopened, synchronize, ready_for_review]", text)
        for block in run_blocks:
            self.assertNotIn("${{ inputs.", block)
        # The dispatch "operation" choice is declared for manual-dispatch
        # parity with the released caller; the resolver output is what the
        # reusable workflow consumes.
        self.assertIn("      operation:\n", text)
        self.assertNotIn("inputs.operation", text)
        for input_name in (
            "pull_request_number",
            "head_repository",
            "source_kind",
            "source_comment_id",
            "source_updated_at",
            "root_comment_id",
        ):
            with self.subTest(input_name=input_name):
                self.assertIn(f"inputs.{input_name}", text)
        self.assertEqual(text.count("review-sensei-run.yml@" + "v5"), 1)
        self.assertIn("resolve-trigger:", text)
        self.assertIn("operation: ${{ needs.resolve-trigger.outputs.operation }}", text)
        self.assertIn('rescan = re.compile(r"\\bre[\\s-]?scan\\b"', text)

    def test_generated_review_workflow_routes_comments_read_only(self):
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "review-sensei-review.yml"
        )
        text = workflow.read_text(encoding="utf-8")
        # The caller grants no write scope: the broker authorizes every
        # publication, so a widened caller permission can never publish.
        self.assertIn("pull-requests: read", text)
        self.assertIn("issues: read", text)
        self.assertNotIn("pull-requests: write", text)
        self.assertNotIn("issues: write", text)
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
        self.assertIn("persist-credentials: false", text)
        self.assertIn(
            "ref: ${{ github.event.repository.default_branch }}",
            text,
        )
        self.assertIn(
            "pull_request_number: ${{ needs.resolve-trigger.outputs.pull_request_number || inputs.pull_request_number }}",
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
        self.assertIn("contains(github.event.comment.body, '@sensei')", text)
        self.assertIn(
            'python - "$pull_json" "$EVENT_NAME" "$COMMENT_BODY"',
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

    def test_comment_arms_recheck_the_author_without_policy_branches(self):
        """Comment events must pass the mention, association, and bot checks.

        The resolver job condition is the caller's only policy expression: both
        comment arms require the @sensei mention, an OWNER/MEMBER/COLLABORATOR
        association, and a non-bot author, and the invocation job re-applies
        none of them because it has no condition of its own beyond its
        dependency. A future edit cannot widen the caller without either
        widening the resolver's event-shape guard or adding a policy branch
        that this contract forbids.
        """

        root = Path(__file__).resolve().parents[1]
        arm = (
            "      (github.event_name == 'issue_comment' &&\n"
            "      github.event.action == 'created' &&\n"
            "      github.event.issue.pull_request &&\n"
            "      contains(github.event.comment.body, '@sensei') &&\n"
            "      (github.event.comment.author_association == 'OWNER' ||\n"
            "      github.event.comment.author_association == 'MEMBER' ||\n"
            "      github.event.comment.author_association == 'COLLABORATOR') &&\n"
            "      github.event.comment.user.type != 'Bot') ||\n"
        )
        invocation = (
            "  review-or-reply:\n"
            "    # A skipped dependency skips this job, so the event guard above is the\n"
            "    # only guard: the reusable workflow decides eligibility, authorization,\n"
            "    # and whether anything is published.\n"
            "    needs: resolve-trigger\n"
        )
        for relative in (
            ".github/workflows/review-sensei-review.yml",
            "examples/github-actions/review-sensei-review.yml",
        ):
            with self.subTest(relative=relative):
                text = (root / relative).read_text(encoding="utf-8")
                self.assertIn(arm, text)
                self.assertEqual(text.count(arm), 1)
                self.assertIn(invocation, text)
                self.assertNotIn("vars.", text)

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
            "needs: [compatibility, quality, schemas, package, npm, linux-standalone, workers, codeql]",
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

    def test_reusable_workflow_runs_the_plan_it_resolves(self):
        text = _reusable_workflow_text()
        # Exactly two optional Actions overrides, mapped into the package
        # resolver once, in the read-only bootstrap plan step.
        self.assertEqual(
            text.count("REVIEWSENSEI_PROVIDER: ${{ vars.REVIEWSENSEI_PROVIDER }}"), 1
        )
        self.assertEqual(
            text.count("REVIEWSENSEI_MODEL: ${{ vars.REVIEWSENSEI_MODEL }}"), 1
        )
        self.assertEqual(text.count("host-plan"), 1)
        self.assertIn(
            '"$RUNNER_TEMP/review-sensei-venv/bin/review-sensei" host-plan \\',
            text,
        )
        self.assertIn('--github-output "$GITHUB_OUTPUT"', text)

        # Backend, endpoint, model, credential, and policy are plan outputs.
        # None of them is an input, a variable chain, or an Actions default.
        self.assertIn("needs.bootstrap.outputs.runner_kind == 'hosted'", text)
        self.assertIn("needs.bootstrap.outputs.runner_kind == 'local'", text)
        self.assertEqual(text.count("needs.bootstrap.outputs.writes == 'true'"), 4)
        self.assertIn(
            "inputs.mode == 'manual' || needs.bootstrap.outputs.automatic_reviews == 'true'",
            text,
        )
        self.assertIn(
            "(inputs.operation == 'reply' && needs.bootstrap.outputs.mentions == 'true')",
            text,
        )
        self.assertEqual(
            text.count('--provider "$BACKEND" --base-url "$BASE_URL" --model "$MODEL"'),
            2,
        )
        self.assertEqual(text.count("--format json --exit-semantics operational"), 2)
        self.assertNotIn("inputs.provider", text)
        self.assertNotIn("inputs.model", text)
        for retired in (
            "provider_mode",
            "provider_profile",
            "allow_unqualified_profile",
            "review_sensei_version",
            "stages_dir",
            "categories_dir",
            "upload_artifacts",
            "enable_review",
            "enable_auto_approve",
            "enable_github_writes",
            "enable_mention_replies",
            "enable_learning_proposals",
            "enable_learning_prs",
            "secrets: inherit",
        ):
            with self.subTest(retired=retired):
                self.assertNotIn(retired, text)

        # github.reviews and github.learning travel to the package as explicit
        # invocation flags instead of being re-derived from a variable.
        self.assertIn('if [[ "$REVIEWS" == "auto-approve" ]]; then', text)
        self.assertIn("auto_approve_args+=(--enable-auto-approve)", text)
        self.assertIn("auto_approve_args+=(--no-auto-approve)", text)
        self.assertIn("publish_args+=(--enable-auto-approve)", text)
        self.assertIn("publish_args+=(--no-auto-approve)", text)
        self.assertIn('if [[ "$LEARNING" == "disabled" ]]; then', text)
        self.assertIn("review_args+=(--no-learning-proposals)", text)
        self.assertIn('if [[ "$LEARNING" == "pull-requests" ]]; then', text)
        self.assertIn("publish_args+=(--enable-learning-prs)", text)
        self.assertIn('if [[ "$ARTIFACTS" == "diagnostics" ]]; then', text)
        self.assertIn("review_args+=(--recovery-artifact recovery-artifact.json)", text)

        self.assertIn(
            "if: github.event_name != 'pull_request' || github.event.pull_request.draft != true",
            text,
        )
        self.assertEqual(text.count("github reply \\\n"), 2)
        self.assertEqual(text.count("github review \\\n"), 2)
        self.assertEqual(text.count("--outcome outcome.json"), 2)
        self.assertEqual(text.count("--outcome publication-outcome.json"), 2)
        self.assertEqual(text.count("recovery-artifact.json"), 4)
        self.assertEqual(text.count("Publish or promote"), 2)
        self.assertEqual(text.count("reply_exit=$?"), 2)
        self.assertEqual(text.count("grep -E '^(replied_and_resolved|"), 2)
        self.assertEqual(
            text.count("mention reply command failed (exit $reply_exit)"), 2
        )
        self.assertEqual(text.count('if [[ "$reply_exit" -ne 0 ]]; then'), 2)
        self.assertEqual(text.count("Verify mention reply completed"), 2)
        self.assertEqual(text.count("always() && inputs.operation == 'reply' &&"), 2)
        self.assertEqual(text.count("steps.reply.outcome != 'success'"), 2)
        self.assertEqual(text.count("runs-on: ubuntu-latest"), 4)
        self.assertEqual(text.count("runs-on: [self-hosted, linux, x64, ollama]"), 1)

    def test_reusable_workflow_has_no_profile_or_custom_url_surface(self):
        text = _reusable_workflow_text()
        # A caller cannot name a provider profile, a custom endpoint, or an
        # upstream provider: the trusted policy commit and the packaged
        # defaults are the only sources for routing.
        self.assertNotIn("provider_profile", text)
        self.assertNotIn("allow_unqualified_profile", text)
        self.assertNotIn("allow_custom_endpoint", text)
        self.assertNotIn("OPENROUTER_UPSTREAM_PROVIDER", text)
        self.assertNotIn("resolve-hosted-openrouter", text)
        self.assertNotIn("validate_resolved_hosted_job_model", text)
        self.assertNotIn("--base-url http", text)
        self.assertNotIn("--model qwen", text)
        self.assertNotIn("--model deepseek", text)
        # OpenRouter keeps its credential where the adapter needs it, and no
        # provider credential is ever forwarded to the Worker.
        self.assertIn("OPENROUTER_API_KEY:", text)
        self.assertIn("OPENAI_API_KEY:", text)

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
        self.assertEqual(len(reply_blocks), 2)
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
        for job_id in ("hosted", "local"):
            job = _job_section(workflow, job_id)
            self.assertEqual(job.count(pull_request_env), 4)
        for job_id in ("hosted", "local"):
            job = _job_section(workflow, job_id)
            self.assertIn(expected_permissions, job)
            reply_name = next(
                name
                for name in _named_steps(job)
                if name.startswith("Generate and publish") and "mention reply" in name
            )
            reply = _step_block(job, reply_name)
            self.assertIn("GITHUB_TOKEN: ${{ github.token }}", reply)
            self.assertIn("REVIEWS: ${{ needs.bootstrap.outputs.reviews }}", reply)
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
        # Each provider job publishes through exactly one promotion step.
        self.assertEqual(
            workflow.count("Publish or promote validated review through the broker"),
            1,
        )
        self.assertEqual(
            workflow.count("Publish or promote validated local review and learnings"),
            1,
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
            2,
        )
        self.assertEqual(text.count("HEAD_REF: ${{ inputs.head_sha }}"), 2)
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
        number. Both provider jobs must use the same expression: the planned
        runner kind is an execution detail, not a concurrency partition.
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

        top_level, command, hosted, local = group_lines
        self.assertEqual(command, "group: reviewsensei-command-${{ github.run_id }}")
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
        # The hosted and local jobs intentionally share the provider review slot.
        self.assertEqual(hosted, expected_provider_group)
        self.assertEqual(local, expected_provider_group)
        hosted_job = _job_section(text, "hosted")
        local_job = _job_section(text, "local")
        cancel_expr = "cancel-in-progress: ${{ inputs.operation == 'review' }}"
        for job in (hosted_job, local_job):
            self.assertEqual(
                [
                    line.strip()
                    for line in job.splitlines()
                    if "cancel-in-progress:" in line
                ],
                [cancel_expr],
            )
        self.assertNotIn("runner_kind", hosted)
        self.assertNotIn("runner_kind", local)
        self.assertNotIn("bootstrap", hosted)
        self.assertNotIn("bootstrap", local)
        self.assertIn(
            "The hosted and local jobs share this review group",
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
                "cancel-in-progress: false",
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
        self.assertIn("if: needs.bootstrap.outputs.writes == 'true'", text)
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
        # A policy that does not authorize writes performs no host operation at
        # all: the preflight is skipped before any API request. A review or
        # reply then runs only when its operation is still selected by the
        # plan's github policy.
        gate = (
            "((inputs.operation == 'review' && (inputs.mode == 'manual' || "
            "needs.bootstrap.outputs.automatic_reviews == 'true')) ||\n"
            "      (inputs.operation == 'reply' && "
            "needs.bootstrap.outputs.mentions == 'true'))"
        )
        self.assertEqual(text.count(gate), 2)
        # Each provider job gates its review, reply, and diff-preparation steps
        # on the operation the caller requested.
        self.assertEqual(text.count("if: inputs.operation == 'review'\n"), 3)
        self.assertEqual(text.count("if: inputs.operation == 'reply'\n"), 2)
        # Command operations are never selected by the plan's review policy:
        # maintainer commands keep their own authorization.
        command = _job_section(text, "command")
        self.assertIn("inputs.operation == 'command'", command)
        self.assertIn("needs.bootstrap.outputs.writes == 'true'", command)
        self.assertNotIn("automatic_reviews", command)
        self.assertIn("reviewsensei-command-${{ github.run_id }}", command)
        self.assertIn(
            '--github-session-ledger --oidc-token "$oidc_token" --allow-write',
            command,
        )
        self.assertIn("PULL_REQUEST: ${{ inputs.pull_request_number }}", command)
        self.assertIn("job_workflow_ref", command)
        self.assertIn("source_comment_id", command)
        # The session-comment mutation is broker-authorized; GITHUB_TOKEN must
        # keep no write scope for the command job.
        self.assertNotIn("issues: write", command)
        self.assertNotIn("pull-requests: write", command)
        self.assertIn("id-token: write", command)
        validator = _job_section(text, "bootstrap")
        # The command branch is reached for every command operation, whether or
        # not the selected policy authorizes writes, so a caller cannot route
        # command payloads through the workflow while the consuming job is
        # skipped.
        self.assertNotIn("ENABLE_GITHUB_WRITES", validator)
        self.assertIn("command)", validator)
        self.assertIn(
            "command operations require an authorized human maintainer", validator
        )
        self.assertNotIn("command operations require enable_github_writes", validator)
        self.assertIn(
            'comment_body_bytes="$(printf \'%s\' "$COMMENT_BODY" | LC_ALL=C wc -c)"',
            validator,
        )
        self.assertNotIn("${#COMMENT_BODY}", validator)
        self.assertIn(
            "command operations require an explicit pull_request_number input",
            validator,
        )
        # An omitted actor type must not satisfy the maintainer gate, so the
        # input carries no default.
        self.assertIn("comment_actor_type:\n", text)
        self.assertNotIn("default: User\n", text)
        # A stale or fabricated head SHA must fail the live-PR preflight before
        # the mutation is attempted.
        self.assertIn("needs: [bootstrap, authoritative-preflight]", command)
        preflight = _job_section(text, "authoritative-preflight")
        self.assertIn('"$OPERATION" != "command"', preflight)
        self.assertIn("needs: bootstrap", preflight)

    def test_hosted_review_invocations_supply_the_broker_attested_session_ledger(
        self,
    ):
        # A default setup-v5 installation selects merge-focused, which needs a
        # trusted session ledger for admission. Every hosted `github review`
        # invocation supplies the broker-attested ledger, so a fresh
        # installation completes reviews instead of skipping for a missing
        # ledger. The local-CLI path without a ledger is the operator
        # diagnostics case documented in docs/diagnostics.md and ADR 0055.
        text = _reusable_workflow_text()
        sites: set[tuple[str, str]] = set()
        for job_id in _job_ids(text):
            job = _job_section(text, job_id)
            for name in _named_steps(job):
                for run in _run_blocks(_step_block(job, name)):
                    if "github review \\" not in run:
                        continue
                    sites.add((job_id, name))
                    self.assertIn("--github-session-ledger", run)
        # Pin the exact (job, step) invocation sites instead of a bare count,
        # so a new or split job fails here by name while a future job added
        # elsewhere is still swept by the loop above.
        self.assertEqual(
            sites,
            {
                ("hosted", "Publish or promote validated review through the broker"),
                ("local", "Publish or promote validated local review and learnings"),
            },
        )

    def test_hosted_analysis_emits_the_transaction_artifacts_publication_reads(
        self,
    ):
        # Operator modes need the identity-bound transaction ledger and its
        # trusted artifacts, so each analysis lane reserves the transaction
        # through the broker-attested comment ledger and writes the
        # configuration and admission documents its own publication re-reads.
        # Without them the lane hands off with `identity-bound transaction
        # required` and exits 1 instead of reviewing.
        text = _reusable_workflow_text()
        sites: set[tuple[str, str]] = set()
        for job_id in _job_ids(text):
            job = _job_section(text, job_id)
            for name in _named_steps(job):
                step = _step_block(job, name)
                invocations = [
                    run
                    for run in _run_blocks(step)
                    if "--transaction --github-session-ledger" in run
                ]
                if not invocations:
                    continue
                sites.add((job_id, name))
                # The hosted ledger scopes its comment marker to the numeric
                # repository id, so the step must map the input it consumes.
                self.assertIn("REPOSITORY_ID: ${{ inputs.repository_id }}", step)
                for run in invocations:
                    self.assertIn('--repository-id "$REPOSITORY_ID"', run)
                    self.assertIn(
                        "--configuration-context-output configuration-context.json",
                        run,
                    )
                    self.assertIn(
                        "--admission-context-output admission-context.json", run
                    )
        self.assertEqual(
            sites,
            {
                ("hosted", "Run hosted review"),
                ("local", "Run local review"),
            },
        )

    def test_hosted_publication_consumes_the_analysis_artifacts(self):
        # The publication boundary must read the same transaction context the
        # analysis lane admitted with. A publish step that omits either
        # artifact falls back to a durable-baseline handoff on every later
        # round of a pull request.
        text = _reusable_workflow_text()
        sites: set[tuple[str, str]] = set()
        for job_id in _job_ids(text):
            job = _job_section(text, job_id)
            for name in _named_steps(job):
                for run in _run_blocks(_step_block(job, name)):
                    if "--outcome publication-outcome.json" not in run:
                        continue
                    sites.add((job_id, name))
                    self.assertIn("--github-session-ledger", run)
                    self.assertIn(
                        "--configuration-context configuration-context.json", run
                    )
                    self.assertIn("--admission-context admission-context.json", run)
        self.assertEqual(
            sites,
            {
                ("hosted", "Publish or promote validated review through the broker"),
                ("local", "Publish or promote validated local review and learnings"),
            },
        )

    def test_reusable_workflow_owns_no_policy_markdown_or_budget_logic(self):
        # The workflow owns events, runners, jobs, concurrency, permissions,
        # secret injection, and installation. The engine owns model defaults,
        # review-mode resolution, budget arithmetic, blocker classification,
        # Markdown assembly, and approval eligibility — so no CLI invocation
        # passes a review-mode, stage, or category override and no budget job
        # or ceiling is admitted. The engine's `reviews` decision is still
        # mapped to the flag that states it; that translation is not a policy
        # decision the workflow makes.
        text = _reusable_workflow_text()
        for flag in ("--review-mode", "--stages-dir", "--categories-dir"):
            self.assertNotIn(flag, text)
        for token in (
            "inputs.review_mode",
            "budget-admission",
            "budget_admission",
            "max_rounds",
            "remaining_rounds",
            "--enable-replenish",
        ):
            self.assertNotIn(token, text)
        # The retired-variable report names the old path settings; nothing
        # executable may still read them.
        for run in _run_blocks(text):
            self.assertNotIn("STAGES_DIR", run)
            self.assertNotIn("CATEGORIES_DIR", run)
        for token in ("STAGES_DIR", "CATEGORIES_DIR"):
            for line in text.splitlines():
                if token in line:
                    self.assertIn(
                        f"REVIEWSENSEI_{token}: ${{{{ vars.REVIEWSENSEI_{token} }}}}",
                        line,
                    )
        self.assertIn("--format json --exit-semantics operational", text)
        # The one remaining behavior translation reads the plan's resolved
        # `reviews` value; nothing here re-derives a default from the event.
        self.assertNotIn("merge-focused", text)
        self.assertNotIn("advisory", text)

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

    def test_provider_jobs_run_the_planned_runner_kind(self):
        # The plan's runner requirement selects the job, and only one provider
        # job can run for one plan. No input and no repository variable picks
        # the runner: a repository cannot promote its own pull request onto the
        # hosted path or pin an unapproved backend.
        workflow_text = _reusable_workflow_text()
        hosted = _job_section(workflow_text, "hosted")
        local = _job_section(workflow_text, "local")
        self.assertIn("needs.bootstrap.outputs.runner_kind == 'hosted'", hosted)
        self.assertIn("needs.bootstrap.outputs.runner_kind == 'local'", local)
        self.assertIn("runs-on: ubuntu-latest", hosted)
        self.assertIn("runs-on: [self-hosted, linux, x64, ollama]", local)
        self.assertNotIn("inputs.provider_mode", workflow_text)
        self.assertNotIn("inputs.provider", workflow_text)
        # Each job re-checks the plan it was selected by, so a mismatch fails
        # closed instead of running a backend on the wrong runner.
        self.assertIn("hosted job received a non-hosted plan", hosted)
        self.assertIn("local job received a non-local plan", local)

    def test_workflow_hard_codes_no_model_endpoint_or_provider_defaults(self):
        # Model defaults, endpoints, and upstream routing are package
        # decisions. The workflow carries no fallback chain, no backend
        # default, and no provider-specific endpoint; it passes the exact plan
        # through and nothing else.
        workflow_text = _reusable_workflow_text()
        for token in (
            "OLLAMA_MODEL",
            "OLLAMA_BASE_URL",
            "HOSTED_REVIEWSENSEI_MODEL",
            "BACKEND_DEFAULT",
            "REVIEWSENSEI_UPSTREAM_PROVIDER",
            "deepseek",
            "qwen3.5",
            "ollama.com",
            "11434",
            "openrouter.ai",
            "localhost",
        ):
            self.assertNotIn(token, workflow_text)
        self.assertIn(
            '--provider "$BACKEND" --base-url "$BASE_URL" --model "$MODEL"',
            workflow_text,
        )
        # The retired model/backend variables may only be named by the
        # retired-variable report; nothing executable reads them.
        for token in ("REVIEWSENSEI_PROVIDER_MODE", "REVIEWSENSEI_CLOUD_MODEL"):
            for line in workflow_text.splitlines():
                if token in line:
                    self.assertIn(
                        f"{token}: ${{{{ vars.{token} }}}}",
                        line,
                    )

    def test_provider_jobs_scope_exactly_the_named_credential(self):
        # Only the three allowlisted key variables are declared, all optional,
        # and exactly the plan's named credential is materialized — and only
        # when the plan says the selected backend requires one. A backend that
        # requires no credential receives nothing, and a credential's presence
        # never authorizes writes or inference.
        workflow_text = _reusable_workflow_text()
        declaration = workflow_text.split("secrets:", 1)[1].split("concurrency:", 1)[0]
        self.assertNotIn("inherit:", declaration)
        self.assertEqual(
            re.findall(r"^      ([A-Z][A-Z0-9_]*):$", declaration, re.M),
            ["OLLAMA_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY"],
        )
        for name in ("OLLAMA_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY"):
            self.assertIn(f"{name}:\n", workflow_text)
            # Declared once and scoped by each provider job's one credential
            # step; no other path can read a secret.
            self.assertEqual(workflow_text.count(f"secrets.{name}"), 2)
        declaration = _reusable_workflow_text().split("secrets:", 1)[1]
        self.assertEqual(declaration.count("required: false"), 3)
        self.assertEqual(
            workflow_text.count("Scope the selected backend credential"), 2
        )
        self.assertEqual(workflow_text.count('--api-key-env "$CREDENTIAL_ENV"'), 4)
        self.assertIn("no credential never receives one implicitly", workflow_text)

    def test_bootstrap_job_is_read_only_and_resolves_one_plan(self):
        # The bootstrap phase resolves hosted policy and the runner requirement
        # from a trusted release of the package over a read-only checkout of
        # the trusted policy commit. It installs nothing from the pull request
        # and executes no repository script; its single plan is the one every
        # provider job consumes.
        workflow_text = _reusable_workflow_text()
        job = _job_section(workflow_text, "bootstrap")
        self.assertIn("permissions:\n      contents: read", job)
        self.assertNotIn("secrets.", job)
        self.assertNotIn("id-token", job)
        self.assertNotIn("actions/upload-artifact@", job)
        self.assertIn("path: trusted-policy", job)
        self.assertIn("persist-credentials: false", job)
        self.assertIn("ref: ${{ github.event.repository.default_branch }}", job)
        self.assertNotIn("github.event.pull_request.head", job)
        # Exactly the two optional Actions overrides are mapped, once, and only
        # in the plan step. The retired-variable report names old variables but
        # reads none of them as configuration.
        plan_step = _step_block(job, "Resolve the hosted plan")
        self.assertEqual(plan_step.count("${{ vars.REVIEWSENSEI_"), 2)
        self.assertIn(
            "REVIEWSENSEI_PROVIDER: ${{ vars.REVIEWSENSEI_PROVIDER }}", plan_step
        )
        self.assertIn("REVIEWSENSEI_MODEL: ${{ vars.REVIEWSENSEI_MODEL }}", plan_step)
        self.assertIn("working-directory: trusted-policy", plan_step)
        self.assertIn("id: plan", plan_step)
        self.assertIn("host-plan", plan_step)
        self.assertIn('--github-output "$GITHUB_OUTPUT"', plan_step)
        report_step = _step_block(job, "Report retired repository variables")
        self.assertNotIn(">>", report_step)
        self.assertNotIn("GITHUB_OUTPUT", report_step)
        self.assertIn("retired_environment_remedies", report_step)
        # The bootstrap plan channel mirrors the package's plan contract
        # exactly, so a new plan output cannot appear here without the package
        # changing too.

        plan_outputs = {
            name
            for name, _ in hosted_plan_outputs(
                plan_hosted_execution(
                    parse_configuration_text("schema: 1\n", root=Path("/tmp")),
                    environ={},
                )
            )
        }
        declared = set(re.findall(r"^      ([a-z_]+): \$\{\{ steps\.plan", job, re.M))
        self.assertEqual(declared, plan_outputs)
        identity = set(
            re.findall(
                r"^      ([a-z_]+): \$\{\{ steps\.(?:release-identity|policy)",
                job,
                re.M,
            )
        )
        self.assertEqual(
            identity, {"package_version", "workflow_commit", "policy_commit"}
        )
        # Every output a provider job consumes is one bootstrap declared.
        consumed = {
            match
            for match in re.findall(
                r"needs\.bootstrap\.outputs\.([a-z_]+)", workflow_text
            )
        }
        self.assertLessEqual(consumed, declared | identity)
        self.assertIn("git init --bare", job)
        self.assertIn("importlib.metadata.version", job)
        self.assertIn("pip check", job)

    def test_dogfood_caller_matches_generated_setup_template(self):
        repo_root = Path(__file__).resolve().parents[1]
        dogfood = (
            repo_root / ".github" / "workflows" / "review-sensei-review.yml"
        ).read_text(encoding="utf-8")
        example = (
            repo_root / "examples" / "github-actions" / "review-sensei-review.yml"
        ).read_text(encoding="utf-8")
        self.assertEqual(example, dogfood)
        self.assertEqual(dogfood, _tagged_workflow("v5"))
        self.assertIn(
            "uses: malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@v5",
            dogfood,
        )

    def test_bootstrap_validator_refuses_unknown_operations_and_unsafe_refs(self):
        root = Path(__file__).resolve().parents[1]
        workflow_text = (
            root / ".github" / "workflows" / "review-sensei-run.yml"
        ).read_text(encoding="utf-8")
        block = _run_block_containing(
            workflow_text, "operation must be review, reply, or command"
        )
        self.assertIn('case "$OPERATION" in', block)
        self.assertIn("review|reply|command", block)
        self.assertIn("automatic|manual", block)
        self.assertIn('"$BASE_REF" == *--*', block)
        self.assertIn('"$BASE_REF" == *-', block)
        self.assertIn('"$HEAD_REF" == *--*', block)
        self.assertIn('"$HEAD_REF" == *-', block)
        self.assertIn('"$HEAD_REF" == -*', block)
        # The provider-mode input no longer exists: the validator rejects
        # unknown operations instead of normalizing a mode.
        self.assertNotIn("provider_mode", workflow_text)
        self.assertNotIn("PROVIDER_MODE", block)

    def test_local_provider_validation_uses_the_same_safe_ref_rules(self):
        root = Path(__file__).resolve().parents[1]
        workflow_text = (
            root / ".github" / "workflows" / "review-sensei-run.yml"
        ).read_text(encoding="utf-8")
        block = _run_block_containing(
            workflow_text, "local job received a non-local plan"
        )
        block = block.split("python - <<'PY'\n", 1)[1]
        block = textwrap.dedent(re.split(r"\n\s*PY\s*\n?$", block, maxsplit=1)[0])
        environment = {
            "MODE": "manual",
            "OPERATION": "reply",
            "RUNNER_KIND": "local",
            "BACKEND": "local-ollama",
            "MODEL": "qwen3.5:4b",
            "BASE_URL": "http://127.0.0.1:11434",
            "CREDENTIAL_ENV": "OLLAMA_API_KEY",
            "CREDENTIAL_REQUIRED": "false",
            "REPOSITORY": "owner/repo",
            "HEAD_REF": "",
            "HEAD_REPOSITORY": "",
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
        for mismatch in ("hosted", "", "local "):
            with self.subTest(runner_kind=mismatch):
                result = subprocess.run(
                    ["python3", "-c", block],
                    env={
                        **os.environ,
                        **environment,
                        "BASE_REF": "main",
                        "RUNNER_KIND": mismatch,
                    },
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
        # Both provider jobs share one validator body: only the plan check may
        # differ, so a ref rule tightened for one runner cannot drift.
        hosted = _run_block_containing(
            workflow_text, "hosted job received a non-hosted plan"
        )
        hosted = hosted.split("python - <<'PY'\n", 1)[1]
        hosted = textwrap.dedent(re.split(r"\n\s*PY\s*\n?$", hosted, maxsplit=1)[0])
        self.assertEqual(hosted.replace("hosted", "local"), block)

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
        self.assertNotIn("Install ReviewSensei and validate hosted model", text)
        # The release identity is resolved once, from the workflow commit the
        # run actually executes, and every install step consumes that one
        # identity instead of re-deriving it.
        self.assertEqual(
            text.count("REVIEW_SENSEI_WORKFLOW_REF: ${{ job.workflow_ref }}"), 1
        )
        self.assertEqual(
            text.count("REVIEW_SENSEI_WORKFLOW_SHA: ${{ job.workflow_sha }}"), 1
        )
        release = _run_block_containing(text, "must run from a public git tag")
        self.assertIn("@refs/tags/[A-Za-z0-9]", release)
        self.assertIn("git init --bare", release)
        self.assertIn("fetch --depth=1", release)
        self.assertIn('cat-file -t "$REVIEW_SENSEI_WORKFLOW_SHA"', release)
        self.assertIn('if [[ "$object_type" == "tag" ]]; then', release)
        self.assertIn('rev-parse "${REVIEW_SENSEI_WORKFLOW_SHA}^{commit}"', release)
        self.assertIn('elif [[ "$object_type" == "commit" ]]; then', release)
        self.assertIn(
            "The ReviewSensei workflow SHA is not a commit or an annotated tag.",
            release,
        )
        self.assertIn('show "$workflow_commit:pyproject.toml"', release)
        self.assertIn("does not declare an exact released version", release)
        self.assertIn('echo "package_version=$package_version"', release)
        self.assertIn("refusing the GitHub fallback", text)
        self.assertIn("No matching distribution found for review-sensei==", text)
        self.assertIn(
            "Could not find a version that satisfies the requirement review-sensei==",
            text,
        )
        self.assertIn(
            '"review-sensei==$expected_version"',
            text,
        )
        self.assertNotIn("dogfood_ref", text)
        self.assertNotIn("refs/pull/", text)
        self.assertIn("installing the verified ReviewSensei workflow commit", text)
        self.assertNotIn(
            "git ls-remote https://github.com/malsabbagh/review-sensei.git", text
        )
        self.assertIn('importlib.metadata.version("review-sensei")', text)
        self.assertIn('"$python_bin" -m pip check', text)
        # No install path may fall back to a branch, a pull-request ref, or
        # `latest`: the exact released version is the only pin.
        self.assertNotIn("review-sensei.git@main", text)
        self.assertNotIn("pip install --upgrade review-sensei", text)
        install_blocks = [
            block
            for block in _run_blocks(text)
            if "REVIEW_SENSEI_WORKFLOW_COMMIT" in block
        ]
        self.assertEqual(len(install_blocks), 4)
        for block in install_blocks:
            with self.subTest(block=block[:40]):
                self.assertLess(
                    block.index('"review-sensei==$expected_version"'),
                    block.index(
                        '"git+https://github.com/malsabbagh/review-sensei.git@$REVIEW_SENSEI_WORKFLOW_COMMIT"'
                    ),
                )
        # The tagged-commit fallback is one behavior shared by four install
        # steps, so assert its shape once and assert the four copies stay
        # identical instead of counting marker strings anywhere in the file.
        regions = [_install_fallback_region(block) for block in install_blocks]
        self.assertEqual(len(set(regions)), 1)
        region = regions[0]
        # Both identity inputs are re-validated by every install step, so a
        # malformed plan output can never reach pip.
        self.assertIn('expected_version="${REVIEW_SENSEI_VERSION#v}"', region)
        self.assertIn(
            'if [[ ! "$expected_version" =~ ^[0-9]+\\.[0-9]+\\.[0-9]+$ ]]; then',
            region,
        )
        self.assertIn(
            'if [[ ! "$REVIEW_SENSEI_WORKFLOW_COMMIT" =~ ^[a-f0-9]{40}$ ]]; then',
            region,
        )

    def test_setup_v4_run_name_matches_worker_template(self):
        from review_sensei.hosting.github.setup import _resolve_trigger_workflow

        root = Path(__file__).resolve().parents[1]
        python_line = next(
            line
            for line in _resolve_trigger_workflow("v5").splitlines()
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

    def test_active_workflow_jobs_use_recognized_github_hosted_runners(self):
        workflow_root = Path(__file__).resolve().parents[1] / ".github" / "workflows"
        workflow_paths = sorted(workflow_root.glob("*.yml"))
        github_hosted_runs_on = (
            re.compile(r"^runs-on: ubuntu-latest$"),
            re.compile(r"^runs-on: \$\{\{ matrix\.os \}\}$"),
        )
        ollama_self_hosted = re.compile(
            r"^runs-on: \[self-hosted, linux, x64, ollama\]$"
        )
        self.assertTrue(workflow_paths)
        for workflow in workflow_paths:
            with self.subTest(workflow=workflow.name):
                lines = workflow.read_text(encoding="utf-8").splitlines()
                runs_on_lines = [
                    line.strip()
                    for line in lines
                    if line.strip().startswith("runs-on:")
                ]
                if not runs_on_lines:
                    # Caller workflows delegate runner selection to reusable workflows.
                    self.assertTrue(
                        [line for line in lines if line.strip().startswith("uses:")]
                    )
                    continue
                self.assertTrue(runs_on_lines)
                for line in runs_on_lines:
                    self.assertNotIn("ENABLE_UBICLOUD_HOSTED", line)
                    self.assertNotIn("ubicloud-standard-2", line)
                    if workflow.name == "review-sensei-run.yml":
                        self.assertTrue(
                            line == "runs-on: ubuntu-latest"
                            or ollama_self_hosted.match(line),
                            msg=f"{workflow.name} has unrecognized runner: {line}",
                        )
                        continue
                    self.assertTrue(
                        any(pattern.match(line) for pattern in github_hosted_runs_on),
                        msg=f"{workflow.name} has unrecognized runner: {line}",
                    )

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
            "needs: [compatibility, quality, schemas, package, npm, linux-standalone, workers, codeql]",
            ci,
        )
        self.assertNotIn("downstream-canary", ci)
        self.assertNotIn("Downstream canary", ci)


class ReusablePublishGuardTests(unittest.TestCase):
    def test_provider_jobs_publish_only_a_current_head_after_a_successful_review(
        self,
    ):
        text = _reusable_workflow_text()
        admission_if = "if: success() && !cancelled() && inputs.operation == 'review'"
        publish_if = (
            admission_if + " && steps.publish-admission.outputs.status == 'current'"
        )
        upload_if = (
            "if: inputs.operation == 'review' && needs.bootstrap.outputs.artifacts"
            " == 'diagnostics' && steps.provider-review.outcome == 'success'"
        )
        confirm_name = "Confirm live pull-request head is still current"
        upload_name = "Upload diagnostics artifact"
        self.assertEqual(text.count(confirm_name), 2)
        self.assertEqual(text.count(publish_if), 2)
        self.assertEqual(text.count(upload_if), 2)
        self.assertEqual(text.count("id: publish-admission"), 2)
        # No budget admission and no handoff-status plumbing exists: the engine
        # exits non-zero on a non-publishable outcome, so the publish and
        # artifact steps are skipped by their own success guards.
        for token in (
            "budget-admission",
            "outcome_status",
            "inputs.enable_github_writes",
            "inputs.enable_review",
            "inputs.enable_mention_replies",
            "inputs.upload_artifacts",
        ):
            self.assertNotIn(token, text)

        for job_id, publish_name in (
            ("hosted", "Publish or promote validated review through the broker"),
            ("local", "Publish or promote validated local review and learnings"),
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
                upload = _step_block(job, upload_name)
                self.assertIn(upload_if, upload)
                self.assertIn("if-no-files-found: error", upload)
                self.assertIn("retention-days: 7", upload)

        hosted_confirm = _step_block(_job_section(text, "hosted"), confirm_name)
        local_confirm = _step_block(_job_section(text, "local"), confirm_name)
        self.assertEqual(hosted_confirm, local_confirm)

    def test_provider_jobs_hold_no_credential_and_no_write_scope_by_default(self):
        # Publication is a broker exchange: provider jobs read the repository
        # and mint an identity token, but never receive a write-scoped token.
        # The credential they can materialize is the plan's single named one.
        text = _reusable_workflow_text()
        for job_id in ("hosted", "local"):
            with self.subTest(job=job_id):
                job = _job_section(text, job_id)
                self.assertNotIn("contents: write", job)
                self.assertNotIn("pull-requests: write", job)
                self.assertNotIn("issues: write", job)
                self.assertIn("id-token: write", job)
                self.assertIn("contents: read", job)
                credential = _step_block(job, "Scope the selected backend credential")
                self.assertIn("no credential never receives one implicitly", credential)
                self.assertIn('"$CREDENTIAL_ENV" "$credential_value"', credential)
                self.assertNotIn("--allow-write", credential)


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
        command_group = group_lines.pop(1)
        self.assertEqual(
            command_group, "group: reviewsensei-command-${{ github.run_id }}"
        )
        provider_lines = [line for line in group_lines if "provider" in line]
        self.assertEqual(len(provider_lines), 2)
        for line in group_lines:
            self.assertIn("github.repository", line)
            self.assertIn("inputs.operation == 'review'", line)
            self.assertNotIn("head_sha", line)
            self.assertNotIn("inputs.head_sha", line)
            self.assertNotIn("github.sha", line)
            self.assertNotIn("github.event.pull_request.head.sha", line)
        # The provider group is not partitioned by runner kind or backend: a
        # newer review cancels an older one for the same pull request wherever
        # it ran.
        for line in provider_lines:
            self.assertNotIn("runner_kind", line)
            self.assertNotIn("backend", line)
            self.assertNotIn("needs.bootstrap", line)

        top_level, hosted, local = group_lines
        self.assertIn("github.event.pull_request.number || github.run_id", top_level)
        self.assertIn(
            "needs.authoritative-preflight.outputs.pull_request_number", hosted
        )
        self.assertEqual(hosted, local)

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
                "cancel-in-progress: false",
                "cancel-in-progress: ${{ inputs.operation == 'review' }}",
                "cancel-in-progress: ${{ inputs.operation == 'review' }}",
            ],
        )


if __name__ == "__main__":
    unittest.main()

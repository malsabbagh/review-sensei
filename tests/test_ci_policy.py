import importlib.util
import json
import re
import tempfile
import unittest
from pathlib import Path

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
        self.assertFalse(run_blocks)
        self.assertIn("# ReviewSensei setup version: 4", text)
        self.assertIn(
            "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@" + "v4",
            text,
        )
        self.assertIn("OLLAMA_API_KEY: ${{ secrets.OLLAMA_API_KEY }}", text)
        self.assertIn("id-token: write", text)
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
        self.assertIn(
            "enable_review: ${{ github.event_name == 'workflow_dispatch' && 'true' "
            "|| vars.REVIEWSENSEI_AUTO_REVIEW || 'false' }}",
            text,
        )
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
        self.assertIn("REVIEWSENSEI_RULESET_READ_TOKEN", text)
        self.assertIn("Validate ruleset read credential scope", text)
        self.assertIn("x-oauth-scopes", text)
        self.assertIn("delete_repo", text)
        self.assertIn("gh api --fail-with-body", text)
        self.assertIn("--readback", text)
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
        self.assertIn("AUTO_APPROVE", text)
        self.assertIn("--enable-auto-approve", text)
        self.assertIn("inputs.provider_mode == 'cloud'", text)
        self.assertIn("inputs.provider_mode == 'local'", text)
        self.assertIn("validate-provider-mode:", text)
        self.assertEqual(text.count("needs: validate-provider-mode"), 2)
        self.assertIn('case "$PROVIDER_MODE" in', text)
        self.assertEqual(text.count("github reply \\\n"), 2)
        self.assertEqual(text.count("github review \\\n"), 2)
        self.assertEqual(text.count("Publish or promote"), 2)
        self.assertIn("runs-on: ubuntu-latest", text)
        self.assertIn("runs-on: [self-hosted, linux, x64, ollama]", text)

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
        self.assertEqual(len(group_lines), 3)

        top_level, cloud, local = group_lines
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
        self.assertEqual(cloud, expected_provider_group)
        self.assertEqual(local, expected_provider_group)
        self.assertNotIn("-cloud-", cloud)
        self.assertNotIn("-local-", local)

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
        self.assertEqual(text.count(expected_gate), 2)
        self.assertEqual(
            text.count(
                "if: inputs.operation == 'review' && inputs.enable_review == 'true'"
            ),
            2,
        )
        self.assertEqual(
            text.count(
                "if: inputs.operation == 'reply' && inputs.enable_github_writes == 'true' && inputs.enable_mention_replies == 'true'"
            ),
            2,
        )

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
            2,
        )
        self.assertEqual(
            text.count("REVIEW_SENSEI_WORKFLOW_REF: ${{ job.workflow_ref }}"),
            2,
        )
        self.assertEqual(
            text.count(
                '"git+https://github.com/malsabbagh/review-sensei.git@$REVIEW_SENSEI_WORKFLOW_SHA"'
            ),
            2,
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
        self.assertEqual(len(install_blocks), 2)
        for block in install_blocks:
            with self.subTest(block=block[:40]):
                self.assertLess(
                    block.index('"review-sensei==$expected_version"'),
                    block.index(
                        '"git+https://github.com/malsabbagh/review-sensei.git@$REVIEW_SENSEI_WORKFLOW_SHA"'
                    ),
                )

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


if __name__ == "__main__":
    unittest.main()

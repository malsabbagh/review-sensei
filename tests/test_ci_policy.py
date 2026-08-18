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
        for block in run_blocks:
            self.assertNotIn("${{ inputs.", block)
        for input_name, env_name in (
            ("review_sensei_version", "REVIEW_SENSEI_VERSION"),
            ("base_ref", "BASE_REF"),
            ("head_ref", "HEAD_REF"),
            ("head_repository", "HEAD_REPOSITORY"),
        ):
            with self.subTest(input_name=input_name):
                self.assertIn(f"${{{{ inputs.{input_name} }}}}", text)
                self.assertIn(f"{env_name}: ${{{{ inputs.{input_name} }}}}", text)
        self.assertIn('"$BASE_REF"', text)
        self.assertIn('"$HEAD_REF"', text)
        self.assertIn('"$HEAD_REPOSITORY"', text)
        self.assertIn('"review-sensei==${REVIEW_SENSEI_VERSION}"', text)
        self.assertIn('"$OLLAMA_MODEL"', text)
        self.assertIn("REVIEWSENSEI_PROVIDER_MODE", text)
        self.assertIn("REVIEWSENSEI_LOCAL_MODEL", text)
        self.assertIn("REVIEWSENSEI_CLOUD_MODEL", text)
        self.assertIn("qwen3.5:4b", text)
        self.assertIn("deepseek-v4-flash:cloud", text)
        self.assertIn("https://ollama.com/api", text)
        self.assertIn("http://127.0.0.1:11434/api", text)
        self.assertIn("runs-on: [self-hosted, linux, x64, ollama]", text)
        self.assertIn(
            "DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}", text
        )
        self.assertIn("base_ref must match the repository default branch", text)
        self.assertIn("ref: ${{ github.event.repository.default_branch }}", text)
        self.assertIn("Verify local Ollama service", text)
        self.assertIn("--version", text)
        self.assertIn("prepare-diff", text)

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
                runs_on_lines = [
                    line.strip()
                    for line in workflow.read_text(encoding="utf-8").splitlines()
                    if line.strip().startswith("runs-on:")
                ]
                self.assertTrue(runs_on_lines)
                for line in runs_on_lines:
                    self.assertIn("vars.ENABLE_UBICLOUD_HOSTED", line)
                    self.assertIn("ubicloud-standard-2", line)


if __name__ == "__main__":
    unittest.main()

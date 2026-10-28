"""Local workflow/registration source contracts; never dispatch Actions."""

import hashlib
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def script():
    spec = importlib.util.spec_from_file_location(
        "quarantine_registry_gate", ROOT / "scripts/check_quarantine_registry.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class QuarantineWorkflowTests(unittest.TestCase):
    def test_disabled_caller_retains_read_scopes_shared_lane_and_four_installer_contracts(
        self,
    ):
        text = (ROOT / ".github/workflows/review-sensei-quarantine.yml").read_text()
        self.assertIn("if: ${{ false }}", text)
        self.assertIn("      issues: read", text)
        self.assertNotIn("contents: write", text)
        self.assertNotIn("actions: write", text)
        self.assertIn(
            "quarantine_predecessor_comment_id: ${{ inputs.predecessor_comment_id }}",
            text,
        )
        runner = (ROOT / ".github/workflows/review-sensei-run.yml").read_text()
        self.assertEqual(
            runner.count("# reviewsensei-core-installation: pypi-exact-v1"), 1
        )
        self.assertEqual(
            runner.count("Install ReviewSensei package (PyPI first, GitHub fallback)"),
            4,
        )
        self.assertEqual(
            runner.count(
                'lifecycle_args+=(--quarantine-predecessor-comment-id "$QUARANTINE_PREDECESSOR_COMMENT_ID")'
            ),
            2,
        )
        from tests.test_ci_policy import _job_section

        for job in ("command", "hosted", "local"):
            self.assertIn("concurrency:", _job_section(runner, job))
        policy = (ROOT / "scripts/python_release_sources.py").read_text()
        self.assertIn(hashlib.sha256(runner.encode()).hexdigest(), policy)
        registry = json.loads((ROOT / script().REGISTRY).read_text())
        self.assertEqual(registry["registrations"], [])

    def test_real_git_registry_gate_preserves_prefix_and_rejects_noncommit_baselines(
        self,
    ):
        gate = script()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*args):
                return (
                    subprocess.check_output(
                        [
                            "git",
                            "-C",
                            str(root),
                            "-c",
                            "core.hooksPath=",
                            "-c",
                            "commit.gpgsign=false",
                            "-c",
                            "user.name=Fixture",
                            "-c",
                            "user.email=fixture@example.invalid",
                            *args,
                        ],
                        stderr=subprocess.DEVNULL,
                    )
                    .decode()
                    .strip()
                )

            git("init")
            target = root / gate.REGISTRY
            target.parent.mkdir(parents=True)
            initial = {
                "schema_version": "quarantine-activation-registry-v1",
                "registrations": [{"retained": 1}],
            }
            target.write_text(json.dumps(initial))
            git("add", "--", gate.REGISTRY)
            git("commit", "-m", "registered original slot")
            base = git("rev-parse", "HEAD")
            self.assertEqual(gate.check(root, base), initial["registrations"])
            target.write_text(
                json.dumps({**initial, "registrations": [{"retained": 1}, {"new": 2}]})
            )
            self.assertEqual(len(gate.check(root, base)), 2)
            for rows in ([], [{"retained": 2}], [{"new": 2}, {"retained": 1}]):
                target.write_text(json.dumps({**initial, "registrations": rows}))
                with self.assertRaises(gate.RegistryCheckError):
                    gate.check(root, base)
            with self.assertRaises(gate.RegistryCheckError):
                gate.check(root, "HEAD")
            with self.assertRaises(gate.RegistryCheckError):
                gate.check(root, git("rev-parse", "HEAD:" + gate.REGISTRY))

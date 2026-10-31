"""Validate the complete public caller, not a last-key-wins YAML projection."""

import io
import json
import shutil
import subprocess
import tempfile
import unittest
from collections import Counter
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import yaml
from review_sensei_github import cli
from review_sensei_github.setup import _tagged_workflow
from strict_workflow_yaml import load_workflow


def actionlint_signatures(text, path):
    path.write_text(text, encoding="utf-8")
    result = subprocess.run(
        ["actionlint", "-format", "{{json .}}", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    if result.returncode not in {0, 1}:
        raise AssertionError(result.stderr)
    diagnostics = json.loads(result.stdout or "[]")
    if bool(diagnostics) != bool(result.returncode):
        raise AssertionError("actionlint diagnostics and exit status conflict")
    signatures = []
    for item in diagnostics:
        message = item["message"]
        # The base caller already refers to this retired, undeclared input.
        # Its available-input type expands when new fields are added. Compare
        # this exact property diagnostic to the actual base, not a blanket
        # expression/syntax ignore or a forced successful lint result.
        prefix = 'property "recover_publication" is not defined in object type {'
        if item["kind"] == "expression" and message.startswith(prefix):
            message = prefix
        signatures.append((item["kind"], message))
    return Counter(signatures)


class CallerOutputTests(unittest.TestCase):
    def public_caller(self):
        output = io.StringIO()
        with (
            patch(
                "socket.socket.connect",
                side_effect=AssertionError("live HTTP forbidden"),
            ),
            redirect_stdout(output),
        ):
            self.assertEqual(cli.main(["restart-caller"]), 0)
        return output.getvalue()

    def test_public_command_preserves_one_complete_read_only_resolver_mapping(self):
        normal = _tagged_workflow()
        baseline = load_workflow(normal)
        prepared = load_workflow(self.public_caller())
        permissions = prepared["jobs"]["resolve-trigger"]["permissions"]
        self.assertEqual(
            permissions, {"contents": "read", "pull-requests": "read", "issues": "read"}
        )
        self.assertEqual(
            permissions, baseline["jobs"]["resolve-trigger"]["permissions"]
        )
        self.assertEqual(_tagged_workflow(), normal)

    def test_duplicate_key_oracle_rejects_every_mapping_depth(self):
        for text in (
            "jobs: {}\njobs: {}\n",
            "on: {}\non: {}\n",
            "jobs:\n  resolve-trigger:\n    permissions: {}\n    permissions: {}\n",
            "jobs:\n  resolve-trigger:\n    steps:\n      - env:\n          TOKEN: one\n          TOKEN: two\n",
        ):
            with self.subTest(text=text), self.assertRaises(yaml.YAMLError):
                load_workflow(text)

    def test_forwarded_restart_fields_are_declared_and_normal_runner_is_unchanged(self):
        root = Path(__file__).resolve().parents[1]
        declared = load_workflow(
            (root / ".github/workflows/review-sensei-run.yml").read_text()
        )[True]["workflow_call"]["inputs"]
        prepared = load_workflow(self.public_caller())
        normal = load_workflow(_tagged_workflow())
        forwarded = prepared["jobs"]["review-or-reply"]["with"]
        self.assertLessEqual(set(forwarded), set(declared))
        # The supported ordinary caller exposes no hosted_runner dispatch
        # input and omits the reusable field; its declared default is retained.
        self.assertNotIn("hosted_runner", normal[True]["workflow_dispatch"]["inputs"])
        self.assertNotIn("hosted_runner", normal["jobs"]["review-or-reply"]["with"])
        self.assertEqual(declared["hosted_runner"]["default"], "ubuntu-latest")
        self.assertNotIn("controlled_restart_runner", forwarded)
        self.assertIn("inputs.controlled_restart_runner", forwarded["hosted_runner"])

    def test_original_permissions_insertion_is_rejected_in_public_output(self):
        bad = self.public_caller().replace(
            "  resolve-trigger:\n", "  resolve-trigger:\n    permissions: {}\n", 1
        )
        with self.assertRaisesRegex(
            yaml.YAMLError, "duplicate mapping key: permissions"
        ):
            load_workflow(bad)

    @unittest.skipUnless(
        shutil.which("actionlint"), "actionlint is not installed on this test platform"
    )
    def test_actionlint_public_output_introduces_no_diagnostic_over_actual_base(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            baseline = actionlint_signatures(_tagged_workflow(), directory / "base.yml")
            current = actionlint_signatures(
                self.public_caller(), directory / "caller.yml"
            )
            self.assertEqual(current, baseline)


if __name__ == "__main__":
    unittest.main()

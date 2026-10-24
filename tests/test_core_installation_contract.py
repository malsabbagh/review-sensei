"""Execute the actual runtime installers without network or credentials."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from major_tag_surface import workflow_data  # noqa: E402


class RegistryOnlyInstallerTests(unittest.TestCase):
    def run_installers(self, failure):
        workflow = workflow_data(
            (ROOT / ".github/workflows/review-sensei-run.yml").read_text()
        )
        installers = [
            (name, step["run"])
            for name, job in workflow["jobs"].items()
            for step in job.get("steps", [])
            if step.get("name")
            == "Install ReviewSensei package (PyPI first, GitHub fallback)"
        ]
        self.assertEqual(len(installers), 4)
        for name, script in installers:
            with (
                self.subTest(lane=name, failure=failure),
                tempfile.TemporaryDirectory() as directory,
            ):
                temporary = Path(directory)
                calls = temporary / "calls.jsonl"
                fake = temporary / "python"
                fake.write_text(
                    f"#!{sys.executable}\n"
                    "import json,os,sys\n"
                    "args=sys.argv[1:]\n"
                    "with open(os.environ['INSTALL_CALLS'],'a') as f: f.write(json.dumps(args)+'\\n')\n"
                    "mode=os.environ['INSTALL_FAILURE']\n"
                    "if 'review-sensei==0.8.12' in args and mode in ('missing','network','auth'):\n"
                    " print('No matching distribution found for review-sensei==0.8.12' if mode=='missing' else 'synthetic '+mode+' refusal',file=sys.stderr);sys.exit(1)\n"
                    "if 'review-sensei-github==0.3.13' in args and mode=='hosted-missing':\n"
                    " print('No matching distribution found for review-sensei-github==0.3.13',file=sys.stderr);sys.exit(1)\n"
                    "if '-c' in args and 'metadata.version' in args[-1]:\n"
                    " print('0.3.13' if 'review-sensei-github' in args[-1] else ('0.8.99' if mode=='wrong-version' else '0.8.12'))\n"
                )
                fake.chmod(0o755)
                env = {
                    "PATH": os.environ["PATH"],
                    "RUNNER_TEMP": directory,
                    "GITHUB_PATH": str(temporary / "path"),
                    "GITHUB_OUTPUT": str(temporary / "output"),
                    "REVIEW_SENSEI_VERSION": "0.8.12",
                    "REVIEW_SENSEI_GITHUB_VERSION": "0.3.13",
                    "REVIEW_SENSEI_WORKFLOW_COMMIT": "a" * 40,
                    "FAKE_PYTHON": str(fake),
                    "INSTALL_CALLS": str(calls),
                    "INSTALL_FAILURE": failure,
                }
                tail = "expected_version=" + script.split("expected_version=", 1)[1]
                process = subprocess.run(
                    [
                        "bash",
                        "-c",
                        'set -euo pipefail\npython_bin="$FAKE_PYTHON"\n' + tail,
                    ],
                    cwd=temporary,
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=20,
                )
                arguments = [
                    json.loads(line) for line in calls.read_text().splitlines()
                ]
                core_git = [
                    arg
                    for args in arguments
                    for arg in args
                    if arg.startswith(
                        "git+https://github.com/malsabbagh/review-sensei.git@"
                    )
                    and "#subdirectory=" not in arg
                ]
                self.assertEqual(core_git, [], process.stdout + process.stderr)
                self.assertEqual(
                    process.returncode == 0,
                    failure in {"none", "hosted-missing"},
                    process.stdout + process.stderr,
                )
                if failure == "hosted-missing":
                    hosted_git = [
                        args
                        for args in arguments
                        if any(
                            "#subdirectory=packages/python/review-sensei-github" in arg
                            for arg in args
                        )
                    ]
                    self.assertEqual(len(hosted_git), 1)
                    self.assertIn("--no-deps", hosted_git[0])

    def test_unavailable_exact_core_fails_closed_in_every_lane(self):
        self.run_installers("missing")

    def test_network_and_auth_failures_do_not_fall_back(self):
        for failure in ("network", "auth"):
            self.run_installers(failure)

    def test_installed_version_mismatch_fails_closed(self):
        self.run_installers("wrong-version")

    def test_hosted_fallback_preserves_registry_core(self):
        self.run_installers("hosted-missing")

    def test_exact_registry_pair_succeeds(self):
        self.run_installers("none")

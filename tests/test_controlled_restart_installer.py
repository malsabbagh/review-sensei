"""Frozen-source installer validates signed artifacts before any later code runs."""

import hashlib
import io
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from test_controlled_restart import document, registration

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    import controlled_restart_launch as launch
finally:
    sys.path.pop(0)


class InstallerTests(unittest.TestCase):
    def test_release_verifier_cannot_read_pr_history_or_refill_its_budget(self):
        with launch.ReleaseReads() as reader:
            with patch.object(
                reader.http, "request", return_value=(200, {})
            ) as request:
                for path in (
                    "issues/395/comments",
                    "pulls/395",
                    "actions/runs?per_page=100",
                    "actions/runs/41105198130",
                    "git/tags/" + "a" * 8192,
                ):
                    with self.subTest(path=path[:80]), self.assertRaises(ValueError):
                        reader.read(path)
                self.assertEqual(request.call_count, 0)
                for _ in range(48):
                    reader.read("git/tags/" + "a" * 40)
                with self.assertRaises(ValueError):
                    reader.read("git/tags/" + "a" * 40)
                self.assertEqual(request.call_count, 48)
                self.assertTrue(
                    all(
                        c.args[0] == "GET"
                        and c.args[1].startswith("/repos/malsabbagh/review-sensei/")
                        for c in request.call_args_list
                    )
                )

    def test_verified_wheel_registry_is_selected_before_install_and_rechecked(self):
        row = registration()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            retained = root / "retained"
            retained.mkdir(mode=0o700)
            (retained / row["request_id"]).mkdir(mode=0o700)
            for name in ("temp", "workspace"):
                (root / name).mkdir()
            env = dict(
                GITHUB_EVENT_NAME="workflow_dispatch",
                GITHUB_RUN_ID=row["run_id"],
                GITHUB_RUN_ATTEMPT="1",
                RUNNER_ENVIRONMENT="self-hosted",
                RUNNER_TEMP=str(root / "temp"),
                GITHUB_WORKSPACE=str(root / "workspace"),
            )
            versions = ("0.8.99", "0.3.99")
            calls = []

            def qualify(source, core, hosted, output):
                calls.append("verify")
                output.mkdir()
                records = {}
                for project, version in zip(
                    ("review-sensei", "review-sensei-github"), versions
                ):
                    name = (
                        project.replace("-", "_") + "-" + version + "-py3-none-any.whl"
                    )
                    with zipfile.ZipFile(output / name, "w") as z:
                        if project.endswith("github"):
                            z.writestr(
                                "review_sensei_github/controlled_restarts.json",
                                document([row]),
                            )
                        else:
                            z.writestr("review_sensei/__init__.py", "")
                    raw = (output / name).read_bytes()
                    records[project] = {
                        "version": version,
                        "source": source,
                        "artifacts": {
                            name: {
                                "sha256": hashlib.sha256(raw).hexdigest(),
                                "size": len(raw),
                            }
                        },
                    }
                return {"source": source, "projects": records}

            scope = {
                k: row[k]
                for k in (
                    "repository",
                    "repository_id",
                    "pull_request",
                    "base_sha",
                    "head_sha",
                    "policy_commit",
                    "workflow_sha",
                    "job",
                )
            }
            with (
                patch.dict(os.environ, env, clear=True),
                patch.object(
                    launch.publication,
                    "signed_release",
                    return_value=("e" * 40, "f" * 40),
                ),
                patch.object(launch.publication, "qualify", side_effect=qualify),
            ):
                prepared = launch.prepare(
                    core_version=versions[0],
                    hosted_version=versions[1],
                    parent=retained,
                    output=root / "verified",
                    **scope,
                )
                self.assertEqual(calls, ["verify"])
                self.assertEqual(prepared.request_id, row["request_id"])
                self.assertEqual(prepared.directory, retained / row["request_id"])
                self.assertEqual(prepared.source, "f" * 40)
                # A changed local wheel cannot be installed from a formerly
                # successful signature result; every use rechecks exact bytes.
                prepared.wheels[0].write_bytes(b"changed after verification")
                with self.assertRaises(ValueError):
                    prepared.require_unchanged()

    def test_unverified_and_duplicate_registry_entries_refuse(self):
        for names in ([], ["review_sensei_github/controlled_restarts.json"] * 2):
            with self.subTest(names=names):
                stream = io.BytesIO()
                with zipfile.ZipFile(stream, "w") as z:
                    for name in names:
                        z.writestr(name, document([]))
                with self.assertRaises(ValueError):
                    launch.registry_bytes(stream.getvalue())


if __name__ == "__main__":
    unittest.main()

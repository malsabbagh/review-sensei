"""Offline composition of the public caller, planning and both late installers.

GitHub metadata and verified publication bytes are boundary fixtures. Emitted
Bash, workflow expressions and registration selection execute without network.
"""

import ast
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from review_sensei_github import cli
from strict_workflow_yaml import load_workflow
from test_controlled_restart import document, registration
from test_controlled_restart_installer import launch

from review_sensei.errors import ReviewInputError

ROOT = Path(__file__).resolve().parents[1]


def expression(value, context):
    """Evaluate only the small expression subset used by the tested seams."""
    if not isinstance(value, str) or not value.startswith("${{"):
        return value
    tree = ast.parse(
        value[3:-2]
        .strip()
        .replace("!cancelled()", "not cancelled()")
        .replace("&&", " and ")
        .replace("||", " or ")
        .replace("resolve-trigger", "resolve_trigger")
        .replace("authoritative-preflight", "authoritative_preflight"),
        mode="eval",
    )

    def visit(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return {"true": True, "false": False, "null": None}.get(
                node.id, context.get(node.id, {})
            )
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not visit(node.operand)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and not node.args
            and not node.keywords
        ):
            if node.func.id in {"always", "cancelled"}:
                return node.func.id == "always"
        if isinstance(node, ast.Attribute):
            parent = visit(node.value)
            return parent.get(node.attr, "") if isinstance(parent, dict) else ""
        if isinstance(node, ast.BoolOp):
            result = ""
            for child in node.values:
                result = visit(child)
                if isinstance(node.op, ast.Or) and result:
                    break
                if isinstance(node.op, ast.And) and not result:
                    break
            return result
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left, right = visit(node.left), visit(node.comparators[0])
            if isinstance(node.ops[0], ast.Eq):
                return left == right
            if isinstance(node.ops[0], ast.NotEq):
                return left != right
        raise AssertionError("unsupported workflow expression: " + ast.dump(node))

    return visit(tree.body)


@unittest.skipIf(os.name == "nt", "Actions launcher uses POSIX Bash and retained modes")
class CallerJourneyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.temp = self.root / "temp"
        self.temp.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.bin / "python").symlink_to(sys.executable)
        self.row = registration()
        self.pull = {
            "number": 7,
            "base": {"ref": "main", "sha": self.row["base_sha"]},
            "head": {"ref": "topic", "sha": self.row["head_sha"]},
        }
        self.fixture = self.root / "pull.json"
        self.requests = self.root / "requests.jsonl"
        self.executable(
            "gh",
            "import json,os,sys\nfrom pathlib import Path\n"
            "with open(os.environ['REQUESTS'],'a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\n"
            "print(Path(os.environ['PULL_FIXTURE']).read_text())\n",
        )
        self.env = dict(
            PATH=str(self.bin) + os.pathsep + os.defpath,
            RUNNER_TEMP=str(self.temp),
            GITHUB_WORKSPACE=str(self.workspace),
            GITHUB_EVENT_NAME="workflow_dispatch",
            GITHUB_RUN_ID="456",
            GITHUB_RUN_ATTEMPT="1",
            RUNNER_ENVIRONMENT="self-hosted",
            PULL_FIXTURE=str(self.fixture),
            REQUESTS=str(self.requests),
        )
        self.inputs = dict(
            pull_request_number="7",
            controlled_restart_hosted_version="0.3.99",
            controlled_restart_core_version="0.8.99",
            controlled_restart_parent_directory=str(self.root / "retained"),
            controlled_restart_runner="approved-runner",
        )
        self.github = dict(
            event_name="workflow_dispatch",
            repository="owner/repo",
            repository_id="123",
            event={"repository": {"default_branch": "main"}},
        )
        output = io.StringIO()
        with (
            redirect_stdout(output),
            patch(
                "socket.socket.connect", side_effect=AssertionError("network forbidden")
            ),
        ):
            self.assertEqual(cli.main(["restart-caller"]), 0)
        self.caller = load_workflow(output.getvalue())["jobs"]
        self.jobs = load_workflow(
            (ROOT / ".github/workflows/review-sensei-run.yml").read_text()
        )["jobs"]

    def executable(self, name, source):
        path = self.bin / name
        path.write_text("#!" + sys.executable + "\n" + source, encoding="utf-8")
        path.chmod(0o700)

    def run_step(self, step, context, name):
        output = self.root / (name + ".out")
        output.unlink(missing_ok=True)
        env = dict(self.env, GITHUB_OUTPUT=str(output))
        for key, value in step.get("env", {}).items():
            rendered = expression(value, context)
            env[key] = (
                str(rendered).lower() if isinstance(rendered, bool) else str(rendered)
            )
        result = subprocess.run(
            ["bash", "-e", "-c", step["run"]],
            env=env,
            cwd=self.workspace,
            capture_output=True,
            text=True,
            timeout=15,
        )
        values = (
            dict(line.split("=", 1) for line in output.read_text().splitlines())
            if output.exists()
            else {}
        )
        return result, values

    def caller_steps(self):
        context = dict(inputs=self.inputs, github=self.github)
        for index, step in enumerate(self.caller["resolve-trigger"]["steps"]):
            if "run" in step:
                self.assertNotIn("if", step)
                self.assertNotIn("continue-on-error", step)
                result, values = self.run_step(step, context, "caller-" + str(index))
                if result.returncode or step.get("id") == "resolve":
                    return result, values
        raise AssertionError("caller has no resolver step")

    def planning(self, *, trusted=True):
        self.fixture.write_text(json.dumps(self.pull), encoding="utf-8")
        if trusted:
            target = self.workspace / "src/review_sensei/hosting/github/trigger.py"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(
                ROOT / "src/review_sensei/hosting/github/trigger.py", target
            )
        context = dict(inputs=self.inputs, github=self.github)
        result, resolved = self.caller_steps()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        context["needs"] = {"resolve_trigger": {"outputs": resolved}}
        forwarded = {
            key: expression(value, context)
            for key, value in self.caller["review-or-reply"]["with"].items()
        }
        return dict(
            inputs=forwarded,
            github=self.github,
            job={"workflow_sha": self.row["workflow_sha"]},
            steps={
                "policy": {"outputs": {"commit": self.row["policy_commit"]}},
                "plan": {"outputs": {"runner_kind": "hosted"}},
            },
        ), resolved

    def hold_and_install(self, context, lane):
        context["steps"]["plan"]["outputs"]["runner_kind"] = lane
        report = next(
            s
            for s in self.jobs["bootstrap"]["steps"]
            if s["name"] == "Report frozen restart scope"
        )
        binaries = self.temp / "review-sensei-venv/bin"
        binaries.mkdir(parents=True, exist_ok=True)
        python = binaries / "python"
        if not python.exists():
            python.symlink_to(sys.executable)
        result, _ = self.run_step(report, context, "report-" + lane)
        self.assertEqual(result.returncode, 0, result.stderr)
        scope = json.loads(result.stdout.removeprefix("Held restart scope: "))
        self.assertEqual(scope["base_sha"], self.pull["base"]["sha"])
        self.assertEqual(scope["head_sha"], self.pull["head"]["sha"])
        preflight = self.jobs["authoritative-preflight"]["steps"][0]
        result, routed = self.run_step(preflight, context, "preflight-" + lane)
        self.assertEqual(result.returncode, 0, result.stderr)
        context["needs"] = {
            "authoritative_preflight": {"result": "success", "outputs": routed},
            "bootstrap": {
                "result": "success",
                "outputs": {
                    "workflow_commit": "e" * 40,
                    "policy_commit": self.row["policy_commit"],
                },
            },
        }
        hold = self.jobs["restart_hold"]["steps"][0]
        result, _ = self.run_step(hold, context, "hold-" + lane)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # The real Bash installer obtains frozen scripts and calls the CLI.
        # Stop at the external Git/pip boundary; retain its exact emitted argv,
        # then run the actual prepare/registry selector with verified fixtures.
        self.executable("git", "pass\n")
        self.executable("tar", "pass\n")
        python.unlink()
        capture = self.root / ("installer-" + lane + ".json")
        python.write_text(
            "#!" + sys.executable + "\nimport json,sys\nfrom pathlib import Path\n"
            "if 'controlled_restart_launch.py' in ' '.join(sys.argv):\n"
            f"    Path({str(capture)!r}).write_text(json.dumps(sys.argv[1:]))\n",
            encoding="utf-8",
        )
        python.chmod(0o700)
        step = next(
            s
            for s in self.jobs[lane]["steps"]
            if s["name"] == "Install designated restart package"
        )
        result, _ = self.run_step(step, context, "install-" + lane)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        args = json.loads(capture.read_text())
        start = args.index("--core-version")
        options = dict(zip(args[start::2], args[start + 1 :: 2], strict=True))
        for key in (
            "repository",
            "repository_id",
            "pull_request",
            "base_sha",
            "head_sha",
            "policy_commit",
            "workflow_sha",
        ):
            expected = scope[key]
            self.assertEqual(options["--" + key.replace("_", "-")], str(expected))
        return options

    def test_installer_outputs_refuse_missing_or_foreign_designation_in_both_lanes(
        self,
    ):
        request = self.row["request_id"]
        parent = self.inputs["controlled_restart_parent_directory"]
        for lane in ("hosted", "local"):
            steps = self.jobs[lane]["steps"]
            guard = next(
                step
                for step in steps
                if step["name"] == "Validate designated restart installation outputs"
            )
            self.assertNotIn("continue-on-error", guard)
            self.assertEqual(
                guard["env"],
                {
                    "REQUEST_ID": "${{ steps.restart-install.outputs.request_id }}",
                    "DIRECTORY": "${{ steps.restart-install.outputs.directory }}",
                    "PARENT": "${{ inputs.controlled_restart_parent_directory }}",
                },
            )
            for identity, directory, allowed in (
                (request, parent + "/" + request, True),
                ("", "", False),
                (request, "", False),
                ("", parent + "/" + request, False),
                ("invalid", parent + "/invalid", False),
                (request, "/foreign/" + request, False),
                (request, parent + "/other", False),
            ):
                with self.subTest(lane=lane, identity=identity, directory=directory):
                    # Execute the actual installed-output gate with the outputs
                    # of a successful, silent or malformed external installer.
                    env = {
                        **self.env,
                        "REQUEST_ID": identity,
                        "DIRECTORY": directory,
                        "PARENT": parent,
                    }
                    result = subprocess.run(
                        ["bash", "-e", "-c", guard["run"]],
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    self.assertEqual(result.returncode == 0, allowed, result.stderr)
                    if not allowed:
                        self.assertIn("no review entry authorized", result.stderr)

    def select(self, options):
        parent = Path(options["--parent"])
        parent.mkdir(mode=0o700, exist_ok=True)
        child = parent / self.row["request_id"]
        child.mkdir(mode=0o700, exist_ok=True)
        output = self.root / ("verified-" + options["--job"])

        def publication(source, core, hosted, directory):
            directory.mkdir()
            records = {}
            for project, version in (
                ("review-sensei", core),
                ("review-sensei-github", hosted),
            ):
                name = project.replace("-", "_") + "-" + version + "-py3-none-any.whl"
                path = directory / name
                with zipfile.ZipFile(path, "w") as wheel:
                    if project.endswith("github"):
                        wheel.writestr(
                            "review_sensei_github/controlled_restarts.json",
                            document([self.row]),
                        )
                    else:
                        wheel.writestr("review_sensei/__init__.py", "")
                raw = path.read_bytes()
                records[project] = dict(
                    version=version,
                    source=source,
                    artifacts={
                        name: dict(
                            size=len(raw), sha256=hashlib.sha256(raw).hexdigest()
                        )
                    },
                )
            return dict(source=source, projects=records)

        scope = {
            key: options["--" + key.replace("_", "-")]
            for key in (
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
        for key in ("repository_id", "pull_request"):
            scope[key] = int(scope[key])
        with (
            patch.dict(os.environ, self.env, clear=True),
            patch.object(
                launch.publication, "signed_release", return_value=("e" * 40, "f" * 40)
            ),
            patch.object(launch.publication, "qualify", side_effect=publication),
        ):
            return launch.prepare(
                core_version=options["--core-version"],
                hosted_version=options["--hosted-version"],
                parent=parent,
                output=output,
                **scope,
            )

    def test_generated_manual_caller_reaches_matching_held_registration_in_both_lanes(
        self,
    ):
        context, resolved = self.planning()
        self.assertEqual(
            self.requests.read_text().splitlines(),
            ['["api", "--method", "GET", "repos/owner/repo/pulls/7"]'],
        )
        self.assertEqual(resolved["base_sha"], "a" * 40)
        self.assertEqual(resolved["head_sha"], "b" * 40)
        for lane in ("hosted", "local"):
            with self.subTest(lane=lane):
                # Each lane's signed row names that lane, never a wildcard.
                self.row = registration(job=lane)
                options = self.hold_and_install(context, lane)
                prepared = self.select(options)
                self.assertEqual(prepared.request_id, self.row["request_id"])
                self.assertFalse(list(prepared.directory.iterdir()))
                shutil.rmtree(self.temp / "review-sensei-restart-source")
                python = self.temp / "review-sensei-venv/bin/python"
                python.unlink()
                python.symlink_to(sys.executable)

    def test_fallback_resolver_also_supplies_frozen_scope(self):
        context, _ = self.planning(trusted=False)
        prepared = self.select(self.hold_and_install(context, "hosted"))
        self.assertEqual(prepared.request_id, self.row["request_id"])

    def test_changed_base_never_matches_the_held_registration(self):
        self.pull["base"]["sha"] = "9" * 40
        context, _ = self.planning()
        with self.assertRaisesRegex(ReviewInputError, "no unique execution binding"):
            self.select(self.hold_and_install(context, "hosted"))

    def test_changed_head_never_matches_the_held_registration(self):
        self.pull["head"]["sha"] = "9" * 40
        context, _ = self.planning()
        with self.assertRaisesRegex(ReviewInputError, "no unique execution binding"):
            self.select(self.hold_and_install(context, "hosted"))

    def test_missing_sha_metadata_refuses_before_hold(self):
        for key in ("base", "head"):
            for value in ("", "invalid"):
                with self.subTest(key=key, value=value):
                    original = self.pull[key]["sha"]
                    self.pull[key]["sha"] = value
                    with self.assertRaisesRegex(
                        AssertionError, "identity metadata is invalid"
                    ):
                        self.planning()
                    self.pull[key]["sha"] = original

    def test_different_actual_run_refuses_after_correct_scope_planning(self):
        context, _ = self.planning()
        options = self.hold_and_install(context, "hosted")
        self.env["GITHUB_RUN_ID"] = "999"
        with self.assertRaisesRegex(ReviewInputError, "no unique execution binding"):
            self.select(options)

    def test_all_sixteen_restart_input_combinations_route_fail_closed(self):
        original = dict(self.inputs)
        fields = [key for key in original if key.startswith("controlled_restart_")]
        self.assertEqual(len(fields), 4)
        reusable = self.caller["review-or-reply"]
        # GitHub skips a dependent job after failed resolve-trigger. Neither
        # an always condition nor continue-on-error may erase this boundary.
        self.assertEqual(reusable["needs"], "resolve-trigger")
        self.assertNotIn("if", reusable)
        self.assertNotIn("continue-on-error", self.caller["resolve-trigger"])
        self.assertEqual(self.caller["resolve-trigger"]["runs-on"], "ubuntu-latest")
        self.assertEqual(
            self.caller["resolve-trigger"]["steps"][0]["name"],
            "Validate controlled restart inputs",
        )
        for mask in range(16):
            with self.subTest(mask=mask):
                self.inputs = dict(original)
                self.inputs.update(
                    {
                        key: original[key] if mask & (1 << i) else ""
                        for i, key in enumerate(fields)
                    }
                )
                self.requests.unlink(missing_ok=True)
                self.fixture.write_text(json.dumps(self.pull), encoding="utf-8")
                result, resolved = self.caller_steps()
                if mask not in (0, 15):
                    self.assertNotEqual(
                        result.returncode, 0, "partial restart reached routing"
                    )
                    self.assertIn("restart inputs", result.stdout + result.stderr)
                    self.assertEqual(resolved, {})
                    self.assertFalse(self.requests.exists())
                    continue
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.requests.exists(), mask == 15)
                context = dict(
                    inputs=self.inputs,
                    github=self.github,
                    needs={"resolve_trigger": {"outputs": resolved}},
                )
                forwarded = {
                    key: expression(value, context)
                    for key, value in reusable["with"].items()
                }
                context["inputs"] = forwarded
                context["needs"] = dict(
                    bootstrap=dict(
                        result="success",
                        outputs=dict(
                            runner_kind="hosted",
                            writes="true",
                            automatic_reviews="true",
                        ),
                    ),
                    authoritative_preflight=dict(result="success"),
                    restart_hold=dict(result="waiting" if mask else "skipped"),
                )

                def condition(text):
                    return expression(
                        "${{ " + " ".join(text.strip().splitlines()) + " }}", context
                    )

                self.assertEqual(
                    bool(condition(self.jobs["restart_hold"]["if"])), mask == 15
                )
                self.assertEqual(bool(condition(self.jobs["hosted"]["if"])), mask == 0)
                installer = next(
                    step
                    for step in self.jobs["hosted"]["steps"]
                    if step["name"] == "Install designated restart package"
                )
                self.assertEqual(bool(condition(installer["if"])), mask == 15)
                if mask:
                    self.assertEqual(
                        forwarded["hosted_runner"],
                        original["controlled_restart_runner"],
                    )
                    self.assertEqual(forwarded["base_sha"], self.pull["base"]["sha"])
                    self.assertEqual(forwarded["head_sha"], self.pull["head"]["sha"])
                    context["needs"]["restart_hold"]["result"] = "success"
                    self.assertTrue(condition(self.jobs["hosted"]["if"]))
        self.inputs = original

    def test_malformed_and_blank_restart_values_refuse_before_metadata(self):
        original = dict(self.inputs)
        bad_values = {
            "hosted_version": [" ", "\t", "latest", "v0.3.99", "0.3", "0.3.99\n"],
            "core_version": [" ", "\n", "latest", "v0.8.99", "0.8", "0.8.99;echo bad"],
            "parent_directory": [
                " ",
                "\t",
                "relative",
                "/retained\nother",
                "/retained/../other",
                "/retained//other",
                "/retained/./other",
            ],
            "runner": [
                " ",
                "\t",
                "-runner",
                "runner group",
                "[self-hosted]",
                "a" * 129,
                "runner\nother",
            ],
        }
        for field, values in bad_values.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    self.inputs = dict(
                        original, **{"controlled_restart_" + field: value}
                    )
                    self.requests.unlink(missing_ok=True)
                    result, resolved = self.caller_steps()
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("restart inputs", result.stdout + result.stderr)
                    self.assertFalse(self.requests.exists())
                    self.assertEqual(resolved, {})
        self.inputs = original

    def test_complete_restart_validation_preserves_existing_label_and_path_forms(self):
        for runner in ("r", "a" * 128, "approved.runner_1-linux"):
            with self.subTest(runner=runner):
                self.inputs["controlled_restart_runner"] = runner
                self.inputs["controlled_restart_parent_directory"] = (
                    "/approved/retained archive"
                )
                validation = self.caller["resolve-trigger"]["steps"][0]
                self.assertEqual(
                    validation.get("name"), "Validate controlled restart inputs"
                )
                result, values = self.run_step(
                    validation, dict(inputs=self.inputs), "valid-inputs"
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(values, {})

    def test_restart_validation_refuses_replayed_and_nonmanual_invocation(self):
        validation = self.caller["resolve-trigger"]["steps"][0]
        self.assertEqual(validation.get("name"), "Validate controlled restart inputs")
        for changes in (
            {"GITHUB_RUN_ATTEMPT": "2"},
            {"GITHUB_EVENT_NAME": "pull_request"},
            {"GITHUB_EVENT_NAME": "issue_comment"},
        ):
            with self.subTest(changes=changes):
                previous = dict(self.env)
                self.env.update(changes)
                result, _ = self.run_step(
                    validation, dict(inputs=self.inputs), "invalid-invocation"
                )
                self.assertNotEqual(result.returncode, 0)
                # The all-absent case never acquires a new invocation restriction.
                result, _ = self.run_step(
                    validation, dict(inputs={}), "ordinary-invocation"
                )
                self.assertEqual(result.returncode, 0)
                self.env = previous

    def test_ordinary_manual_dispatch_remains_routing_only(self):
        self.inputs.update(
            {key: "" for key in self.inputs if key.startswith("controlled_restart_")}
        )
        _, resolved = self.planning()
        self.assertNotIn("base_sha", resolved)
        self.assertNotIn("head_sha", resolved)
        self.assertFalse(self.requests.exists())


if __name__ == "__main__":
    unittest.main()

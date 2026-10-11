"""Writer jobs stay in one pull-request concurrency group.

Review, reply, and command write the operation record. Each of those jobs
must use ``reviewsensei-provider-review-<repository>-<pull request>``.
The command path reads the review result from GitHub and takes no
caller-supplied result file. GITHUB_TOKEN on the caller stays read-only.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from review_sensei.hosting.github.setup import (
    WORKFLOW_PATH,
    _classify_setup_files,
    _historical_v5_workflow,
    _merge_focused_v4_workflow,
    _resolve_trigger_v5_workflow,
    _tagged_workflow,
)

ROOT = Path(__file__).resolve().parents[1]
RUN_WORKFLOW = ROOT / ".github" / "workflows" / "review-sensei-run.yml"
CALLERS = (
    ROOT / ".github" / "workflows" / "review-sensei-review.yml",
    ROOT / "examples" / "github-actions" / "review-sensei-review.yml",
)
GROUP_PREFIX = "reviewsensei-provider-review-"
PROVIDER_GROUP = (
    "reviewsensei-provider-review-${{ github.repository }}-${{ "
    "needs.resolve-trigger.outputs.pull_request_number || "
    "github.event.pull_request.number || github.run_id }}"
)
RUN_PROVIDER_GROUP = (
    "reviewsensei-provider-review-${{ github.repository }}-${{ "
    "needs.authoritative-preflight.outputs.pull_request_number || "
    "github.event.pull_request.number || github.run_id }}"
)


def _job_bodies(text: str) -> dict[str, str]:
    match = re.search(r"^jobs:\n", text, flags=re.M)
    if match is None:
        raise AssertionError("workflow defines no jobs")
    bodies: dict[str, str] = {}
    current: str | None = None
    chunks: list[str] = []
    for line in text[match.end() :].splitlines(keepends=True):
        if line.strip() and not line.startswith(" "):
            break
        key = re.match(r"^  ([A-Za-z0-9_-]+):\n$", line)
        if key is not None:
            if current is not None:
                bodies[current] = "".join(chunks)
            current = key.group(1)
            chunks = [line]
            continue
        if current is None:
            raise AssertionError("workflow jobs are not a mapping")
        chunks.append(line)
    if current is not None:
        bodies[current] = "".join(chunks)
    return bodies


def _is_writer(job_id: str, body: str) -> bool:
    if job_id in {"review", "reply", "command"}:
        return True
    if re.search(r"\bgithub (?:review|reply|command)\b", body):
        return True
    return "needs.resolve-trigger.outputs.operation" in body


def _concurrency_group(body: str) -> str | None:
    match = re.search(r"(?m)^[ ]+group: (.+)$", body)
    if match is None:
        return None
    return match.group(1).strip()


class WriterJobConcurrencyTests(unittest.TestCase):
    def test_writer_jobs_stay_in_the_provider_review_group(self):
        run = RUN_WORKFLOW.read_text(encoding="utf-8")
        writers = {
            job_id: body
            for job_id, body in _job_bodies(run).items()
            if _is_writer(job_id, body)
        }
        self.assertEqual(set(writers), {"command", "hosted", "local"})
        for job_id, body in writers.items():
            with self.subTest(job=job_id):
                self.assertEqual(_concurrency_group(body), RUN_PROVIDER_GROUP)
                self.assertIn("cancel-in-progress: false", body)

        generated = _tagged_workflow("v5")
        copies = {
            "generated": generated,
            "repository": CALLERS[0].read_text(encoding="utf-8"),
            "example": CALLERS[1].read_text(encoding="utf-8"),
        }
        for name, text in copies.items():
            with self.subTest(caller=name):
                self.assertEqual(text, generated)
                found = {
                    job_id: body
                    for job_id, body in _job_bodies(text).items()
                    if _is_writer(job_id, body)
                }
                self.assertEqual(set(found), {"review-or-reply"})
                self.assertEqual(
                    _concurrency_group(found["review-or-reply"]), PROVIDER_GROUP
                )
                self.assertIn("cancel-in-progress: false", found["review-or-reply"])

    def test_command_path_takes_no_caller_supplied_result_file(self):
        command = _job_bodies(RUN_WORKFLOW.read_text(encoding="utf-8"))["command"]
        self.assertNotIn("--result", command)
        self.assertNotIn("result_file", command)
        self.assertNotIn("pull-requests: write", command)
        self.assertNotIn("issues: write", command)
        self.assertNotIn("contents: write", command)
        self.assertIn("contents: read", command)
        self.assertIn("pull-requests: read", command)
        self.assertIn("review_session", command)

        for path in CALLERS:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertNotIn("--result", text)
                self.assertNotRegex(text, r"(?m)^[ ]+result:")
                self.assertIn("contents: read", text)
                self.assertIn("pull-requests: read", text)
                self.assertIn("issues: read", text)
                self.assertNotIn("pull-requests: write", text)
                self.assertNotIn("issues: write", text)
                self.assertNotIn("contents: write", text)

    def test_frozen_v4_and_v5_callers_are_not_the_live_group(self):
        for name, content in (
            ("v4", _merge_focused_v4_workflow("v5")),
            ("historical-v5", _historical_v5_workflow("v5")),
            ("resolve-trigger-v5", _resolve_trigger_v5_workflow("v5")),
        ):
            with self.subTest(name=name):
                self.assertNotIn(GROUP_PREFIX, content)
                self.assertNotIn("# ReviewSensei setup version: 6", content)

    def test_installed_resolve_trigger_v5_caller_migrates(self):
        from review_sensei.hosting.github.setup import SetupPlanBuilder

        plan = SetupPlanBuilder().build("owner/repo")
        files = {item.path: item.content for item in plan.files}
        files[WORKFLOW_PATH] = _resolve_trigger_v5_workflow("v5")
        self.assertEqual(_classify_setup_files(files), "migration")
        self.assertIn("# ReviewSensei setup version: 5", files[WORKFLOW_PATH])
        self.assertIn("# ReviewSensei setup version: 6", plan.files[0].content)


if __name__ == "__main__":
    unittest.main()

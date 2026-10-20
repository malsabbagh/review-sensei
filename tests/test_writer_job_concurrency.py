"""Writer jobs stay in one pull-request concurrency group.

Review, reply, and command write the operation record. Each of those jobs
in the reusable workflow must use
``reviewsensei-provider-review-<repository>-<pull request>``. The caller job
that invokes the reusable workflow must not hold that group: it stays held
for the whole called run, so the nested writer job would wait on its own
parent and GitHub cancels the run as a deadlock.
The command path reads the review result from GitHub and takes no
caller-supplied result file. GITHUB_TOKEN on the caller stays read-only.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from review_sensei_github.setup import _prepared_recovery_workflow, _tagged_workflow

ROOT = Path(__file__).resolve().parents[1]
RUN_WORKFLOW = ROOT / ".github" / "workflows" / "review-sensei-run.yml"
CALLERS = (
    ROOT / ".github" / "workflows" / "review-sensei-review.yml",
    ROOT / "examples" / "github-actions" / "review-sensei-review.yml",
)
GROUP_PREFIX = "reviewsensei-provider-review-"
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
    if re.search(r'review-sensei-github" (?:review|publish|reply|command)\b', body):
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
        prepared = _prepared_recovery_workflow("v5")
        copies = {
            "generated": generated,
            "prepared": prepared,
            "repository": CALLERS[0].read_text(encoding="utf-8"),
            "example": CALLERS[1].read_text(encoding="utf-8"),
        }
        for name, text in copies.items():
            with self.subTest(caller=name):
                self.assertEqual(
                    text, prepared if name in {"prepared", "example"} else generated
                )
                found = {
                    job_id: body
                    for job_id, body in _job_bodies(text).items()
                    if _is_writer(job_id, body)
                }
                self.assertEqual(set(found), {"review-or-reply"})
                self.assertNotIn(GROUP_PREFIX, text)
                self.assertIsNone(_concurrency_group(found["review-or-reply"]))

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

        copies = {str(path): path.read_text(encoding="utf-8") for path in CALLERS}
        copies["generated"] = _tagged_workflow("v5")
        copies["prepared"] = _prepared_recovery_workflow("v5")
        for name, text in copies.items():
            with self.subTest(caller=name):
                self.assertNotIn("--result", text)
                self.assertNotRegex(text, r"(?m)^[ ]+result:")
                self.assertIn("contents: read", text)
                self.assertIn("pull-requests: read", text)
                self.assertIn("issues: read", text)
                self.assertNotIn("pull-requests: write", text)
                self.assertNotIn("issues: write", text)
                self.assertNotIn("contents: write", text)


if __name__ == "__main__":
    unittest.main()

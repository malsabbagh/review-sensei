"""Two real host clients interleave through a bounded synthetic API over pipes.

This proves stale-writer refusal and serialized replay, not cross-process locking
or atomic GitHub compare-and-publish. The provider barrier precedes publication.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from urllib.parse import urlparse

from review_sensei.hosting.github import GitHubHttp
from review_sensei.hosting.github.human_assessment import HumanAssessmentPublisher
from review_sensei.hosting.github.publication import approval_eligibility_from_body
from review_sensei.human_assessment import HumanAssessmentService
from review_sensei.models import ProviderResponse
from tests import test_human_assessment as host
from tests.fake_github_http import json_response
from tests.test_epic238_process_faults import ROOT, write_json


class TwoSourceHost(host.State):
    def __init__(self):
        super().__init__(host.prior(extra_human=True))
        self.second_source = {
            **self.source,
            "id": 11,
            "user": {"login": "bob", "type": "User"},
        }

    def open(self, request, timeout):
        path = urlparse(request.full_url).path
        if request.method == "GET" and path.endswith("/issues/comments/11"):
            self.calls.append((request.method, path, None))
            return json_response(self.second_source)
        if request.method == "GET" and path.endswith("/issues/1/comments"):
            self.calls.append((request.method, path, None))
            return json_response([self.source, self.second_source, *self.replies])
        if request.method == "POST" and path.endswith("/issues/comments/11/reactions"):
            return json_response({"id": 99}, 201)
        return super().open(request, timeout)


def wait_file(path, child=None):
    deadline = time.monotonic() + 20
    while not path.exists():
        if child is not None and child.poll() is not None:
            raise AssertionError("child exited before its barrier")
        if time.monotonic() >= deadline:
            raise AssertionError("interleaving barrier timed out")
        time.sleep(0.01)


def assessment_child(directory, source_id, target, paused):
    def opener(request, timeout):
        print(
            json.dumps(
                {
                    "method": request.method,
                    "url": request.full_url,
                    "body": json.loads(request.data) if request.data else None,
                }
            ),
            flush=True,
        )
        response = json.loads(sys.stdin.readline())
        return json_response(response["body"], response["status"])

    class SelectiveProvider:
        name = "fixture"
        model = "fixture"

        def complete(self, request):
            if paused:
                write_json(directory / f"ready-{source_id}.json", {"target": target})
                wait_file(directory / f"release-{source_id}")
            eligibility = host.prior(extra_human=True)
            fingerprint = eligibility.human_review.findings[target].fingerprint
            return ProviderResponse(
                json.dumps(
                    {
                        "body": "Supported by the current diff.",
                        "assessments": [
                            {
                                "fingerprint": fingerprint,
                                "decision": "dismissed",
                                "rationale": "The current diff rejects remote requests before sending.",
                                "human_evidence": "This path is intentionally local-only",
                                "diff_evidence": "reject_remote_requests(data)",
                            }
                        ],
                    }
                ),
                self.name,
                self.model,
            )

    http = GitHubHttp(api_url="https://api.github.test", opener=opener)
    publisher = HumanAssessmentPublisher(http=http)
    conversation = replace(
        host.State().prepared(), source_comment_id=source_id, root_comment_id=source_id
    )
    prepared = publisher.prepare(
        token="read",
        repository="owner/repo",
        pull_request=1,
        prepared=conversation,
        app_slug=host.APP,
    )
    assert prepared is not None
    reply = HumanAssessmentService(SelectiveProvider()).reply(
        context=prepared.conversation.context,
        pending=prepared.eligibility.human_review,
        source_body=prepared.source_body,
    )
    result = publisher.publish(
        token="issue",
        review_token="review",
        repository="owner/repo",
        pull_request=1,
        prepared=prepared,
        reply=reply,
        app_slug=host.APP,
    )
    write_json(directory / f"outcome-{source_id}.json", asdict(result))


class PipeClient:
    def __init__(self, state, lock, directory, source_id, target, paused):
        self.errors = []
        self.directory = directory
        self.source_id = source_id
        code = (
            "import sys; from pathlib import Path; "
            "from tests.test_epic238_process_interleaving import assessment_child; "
            "assessment_child(Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), "
            "sys.argv[4] == 'True')"
        )
        self.child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                code,
                str(directory),
                str(source_id),
                str(target),
                str(paused),
            ],
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        def serve():
            from urllib.request import Request

            try:
                for line in self.child.stdout:
                    message = json.loads(line)
                    body = message["body"]
                    request = Request(
                        message["url"],
                        data=json.dumps(body).encode() if body is not None else None,
                        method=message["method"],
                    )
                    with lock:
                        response = state.open(request, 20)
                    self.child.stdin.write(
                        json.dumps(
                            {
                                "status": response.status,
                                "body": json.loads(response.body),
                            }
                        )
                        + "\n"
                    )
                    self.child.stdin.flush()
            except Exception as error:
                self.errors.append(error)

        self.server = threading.Thread(target=serve, daemon=True)
        self.server.start()

    def finish(self):
        self.child.wait(timeout=25)
        self.server.join(timeout=5)
        errors = self.child.stderr.read()
        assert self.child.returncode == 0, errors
        assert not self.server.is_alive()
        assert not self.errors, self.errors
        return json.loads(
            (self.directory / f"outcome-{self.source_id}.json").read_text()
        )

    def close(self):
        if self.child.poll() is None:
            self.child.kill()
        self.child.wait(timeout=5)
        self.server.join(timeout=5)
        for stream in (self.child.stdin, self.child.stdout, self.child.stderr):
            stream.close()


class ProcessInterleavingTests(unittest.TestCase):
    def test_disjoint_replies_reject_stale_writer_then_preserve_both_resolutions(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            state = TwoSourceHost()
            lock = threading.Lock()
            clients = []
            try:
                first = PipeClient(state, lock, directory, 10, 0, True)
                clients.append(first)
                second = PipeClient(state, lock, directory, 11, 1, True)
                clients.append(second)
                wait_file(directory / "ready-10.json", first.child)
                wait_file(directory / "ready-11.json", second.child)
                (directory / "release-10").touch()
                self.assertEqual(first.finish()["status"], "replied")
                intermediate = approval_eligibility_from_body(state.reviews[-1]["body"])
                self.assertEqual(len(intermediate.human_review.resolved), 1)
                self.assertEqual(len(intermediate.human_review.pending), 1)
                (directory / "release-11").touch()
                self.assertEqual(second.finish()["status"], "skipped_stale_head")
                self.assertEqual(state.events(), ["COMMENT"])
                self.assertEqual(state.reply_count, 1)
                self.assertEqual(
                    approval_eligibility_from_body(state.reviews[-1]["body"]),
                    intermediate,
                )
                retry = PipeClient(state, lock, directory, 11, 1, False)
                clients.append(retry)
                self.assertEqual(retry.finish()["approval_status"], "approved")
                final = approval_eligibility_from_body(state.reviews[-2]["body"])
                self.assertEqual(
                    final.human_review.findings, state.eligibility.human_review.findings
                )
                self.assertEqual(len(final.human_review.resolved), 2)
                self.assertFalse(final.human_review.pending)
                self.assertEqual(state.events(), ["COMMENT", "COMMENT", "APPROVE"])
                self.assertEqual(state.reply_count, 2)
            finally:
                for client in clients:
                    client.close()


if __name__ == "__main__":
    unittest.main()

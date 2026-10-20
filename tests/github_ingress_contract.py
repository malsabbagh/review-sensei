"""Captured external ports for real installed GitHub CLI reply journeys.

The application, publishers, operation comment store, registry and adapters run
unchanged. Only broker authorization and external HTTP are synthetic.
"""

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch
from urllib.parse import urlparse

from fake_github_http import json_response
from review_sensei_github import GitHubHttp
from review_sensei_github.cli import main as github_main

HEAD, BASE = "b" * 40, "a" * 40
APP = {"id": 42, "login": "reviewsensei[bot]", "type": "Bot"}
ANSWER = "Synthetic bounded explanation."


class CapturedGitHub:
    def __init__(self, source_kind="issue"):
        self.source_kind = source_kind
        self.head = HEAD
        self.draft = False
        self.closed = False
        self.calls = []
        self.exchanges = []
        self.source = {
            "id": 10,
            "body": "@reviewsensei explain this local-only change",
            "user": {"id": 7, "login": "alice", "type": "User"},
            "author_association": "MEMBER",
            "created_at": "2026-10-18T00:00:00Z",
            "updated_at": "2026-10-18T00:00:00Z",
            "issue_url": "https://api.github.test/repos/owner/repo/issues/1",
            "pull_request_url": "https://api.github.test/repos/owner/repo/pulls/1",
            "path": "app.py",
            "diff_hunk": "@@ -1 +1 @@\n-return send(data)\n+return reject_remote_requests(data)",
        }
        self.comments = {10: self.source} if source_kind == "issue" else {}
        self.inline = {10: self.source} if source_kind == "inline" else {}
        self.http = GitHubHttp(api_url="https://api.github.test", opener=self.open)

    def request_oidc_token(self):
        return "synthetic-oidc"

    def exchange(self, token, *, capability=None):
        self.exchanges.append((token, capability))
        return "synthetic-" + str(capability)

    def open(self, request, timeout):
        path = urlparse(request.full_url).path
        data = json.loads(request.data) if request.data else None
        self.calls.append((request.method, path, data))
        if request.method == "GET":
            if path == "/user":
                return json_response(APP)
            if path.endswith("/pulls/1"):
                return json_response(
                    {
                        "state": "closed" if self.closed else "open",
                        "draft": self.draft,
                        "changed_files": 1,
                        "user": {"login": "alice", "type": "User"},
                        "head": {
                            "sha": self.head,
                            "ref": "feature",
                            "repo": {"full_name": "owner/repo", "fork": False},
                        },
                        "base": {
                            "sha": BASE,
                            "ref": "main",
                            "repo": {"id": 1, "full_name": "owner/repo", "fork": False},
                        },
                    }
                )
            if path.endswith("/pulls/1/reviews"):
                return json_response([])
            if path.endswith("/pulls/1/files"):
                return json_response(
                    [
                        {
                            "filename": "app.py",
                            "patch": self.source["diff_hunk"],
                            "additions": 1,
                            "deletions": 1,
                        }
                    ]
                )
            if path.endswith("/issues/1/comments"):
                return json_response(list(self.comments.values()))
            if path.endswith("/pulls/1/comments"):
                return json_response(list(self.inline.values()))
            if "/issues/comments/" in path or "/pulls/comments/" in path:
                identifier = int(path.rsplit("/", 1)[-1])
                value = (self.comments if "/issues/" in path else self.inline).get(
                    identifier
                )
                return json_response(value or {}, 200 if value else 404)
            if "/contents/" in path:
                return json_response({}, 404)
        if request.method == "POST" and path.endswith("/reactions"):
            return json_response({"id": 99}, 201)
        if request.method == "DELETE" and path.endswith("/reactions/99"):
            return json_response({}, 204)
        if request.method == "PATCH" and "/issues/comments/" in path:
            identifier = int(path.rsplit("/", 1)[-1])
            self.comments[identifier]["body"] = data["body"]
            return json_response(self.comments[identifier])
        if request.method == "POST" and (
            path.endswith("/issues/1/comments") or path.endswith("/comments/10/replies")
        ):
            table = (
                self.comments if path.endswith("/issues/1/comments") else self.inline
            )
            identifier = 100 + len(self.comments) + len(self.inline)
            value = {
                "id": identifier,
                "body": data["body"],
                "user": APP,
                "created_at": self.source["created_at"],
                "updated_at": self.source["updated_at"],
                "issue_url": self.source["issue_url"],
                "pull_request_url": self.source["pull_request_url"],
            }
            if table is self.inline:
                value["in_reply_to_id"] = 10
            table[identifier] = value
            return json_response(value, 201)
        raise AssertionError((request.method, path, data))

    def invoke(self, case, transport, config, environment, *, enabled=True):
        import review_sensei_github as github_module

        stdout, stderr = io.StringIO(), io.StringIO()
        argv = [
            "reply",
            "--generate",
            "--config",
            str(config),
            "--repository",
            "owner/repo",
            "--repository-id",
            "1",
            "--pull-request",
            "1",
            "--source-comment-id",
            "10",
            "--source-updated-at",
            "2026-10-18T00:00:00Z",
            "--source-kind",
            self.source_kind,
            "--head-sha",
            HEAD,
            "--root-comment-id",
            "10",
            "--allow-write",
        ]
        if enabled:
            argv.append("--enable-reply")
        with (
            case.transport_scope(transport),
            patch.dict("os.environ", environment, clear=True),
            patch.multiple(
                github_module, BrokerClient=lambda: self, GitHubHttp=lambda: self.http
            ),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            code = github_main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

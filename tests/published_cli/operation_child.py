"""Installed CLI operation acceptance process, all HTTP via in-memory opener."""

import hashlib
import io
import json
import socket
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import review_sensei_github as github
from review_sensei_github import cli
from review_sensei_github.http import GitHubHttp
from review_sensei_github.operation_host import OPERATION_COMMENT_MARKER
from review_sensei_github.publication import PublicationResult

import review_sensei
from review_sensei.models import ReviewResult

assert "site-packages" in review_sensei.__file__
assert "site-packages" in cli.__file__
spec = json.loads(sys.argv[1])
result = ReviewResult(
    summary="Synthetic partial context" if spec["partial"] else "Synthetic complete",
    comments=(),
    provider="fixture",
    review_status="partial" if spec["partial"] else "complete",
)
Path("review.json").write_text(json.dumps(result.to_dict()))
Path("pr.patch").write_text(
    "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-x=0\n+x=1\n",
    encoding="utf-8",
    newline="\n",
)
Path(".reviewsensei.yml").write_text(
    "schema: 1\ngithub:\n  operation_entry: " + spec.get("entry", "enabled") + "\n"
)
digest = hashlib.sha256(
    json.dumps(result.to_dict(), sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
requests = []
publication = []
clockstate = [0.0]


class Response(io.BytesIO):
    def __init__(self, data, status=200):
        super().__init__(json.dumps(data).encode())
        self.status = status


def comment(d):
    return {
        "id": 1,
        "body": OPERATION_COMMENT_MARKER + "\n" + d,
        "user": {
            "id": 42,
            "type": "Bot",
            "login": spec.get("login", "reviewsensei[bot]"),
        },
    }


def opener(request, timeout):
    path = urlsplit(request.full_url).path
    q = parse_qs(urlsplit(request.full_url).query)
    requests.append({"method": request.method, "path": path, "query": q})
    assert request.method == "GET", "publication writes must not reach HTTP"
    mode = spec["listing"]
    if mode == "timeout":
        raise TimeoutError("synthetic read timeout")
    if mode == "second-page-timeout" and q.get("page") == ["2"]:
        raise TimeoutError("synthetic later read timeout")
    if mode == "shared-deadline":
        clockstate[0] = 61.0
    if mode == "403":
        return Response({"message": "synthetic denied"}, 403)
    if mode == "wrong-type":
        return Response({"message": "synthetic wrong shape"})
    if mode == "matching":
        return Response([comment(digest)])
    if mode == "mismatch":
        return Response([comment("0" * 64)])
    if mode.startswith("second-page-") or mode in [
        "scan-limit",
        "oversized-page",
        "shared-deadline",
    ]:
        page = int(q.get("page", ["1"])[0])
        filler = [
            {
                "id": i,
                "body": "synthetic unrelated",
                "user": {"id": 2, "type": "User", "login": "other"},
            }
            for i in range(101 if mode == "oversized-page" else 100)
        ]
        if page == 1 or mode == "scan-limit" or mode == "oversized-page":
            return Response(filler)
        if mode == "second-page-mismatch":
            return Response([comment("0" * 64)])
        if mode == "second-page-matching":
            return Response([comment(digest)])
        if mode == "second-page-403":
            return Response({"message": "synthetic denied"}, 403)
        if mode == "second-page-malformed":
            return Response({"message": "synthetic wrong shape"})
        return Response([])
    return Response([])


class ClockedHttp(GitHubHttp):
    def paginate(self, **kwargs):
        if spec["listing"] == "shared-deadline":
            kwargs["monotonic"] = lambda: clockstate[0]
        return super().paginate(**kwargs)


class InjectedHttp(ClockedHttp):
    """Keep the transport a real class for installed adapter subclasses."""

    def __init__(self, *args, **kwargs):
        kwargs.update(api_url="https://github.synthetic.invalid", opener=opener)
        super().__init__(*args, **kwargs)


http = ClockedHttp(api_url="https://github.synthetic.invalid", opener=opener)


def publish(self, **kwargs):
    publication.append(
        {
            "review_status": kwargs["result"].review_status,
            "repository": kwargs["repository"],
            "pull_request": kwargs["pull_request"],
            "head_sha": kwargs["head_sha"],
            "has_configuration_context": kwargs["configuration_context"] is not None,
        }
    )
    return PublicationResult("published", review_id=1)


def deny(*args, **kwargs):
    raise AssertionError("live network prohibited")


stdout = io.StringIO()
stderr = io.StringIO()
# In process: the engine's `github review` forwards to a review-sensei-github
# subprocess, where these HTTP and publication patches would not apply.
argv = [
    "publish",
    "--result",
    "review.json",
    "--diff",
    "pr.patch",
    "--repository",
    "owner/repo",
    "--repository-id",
    "7",
    "--pull-request",
    "11",
    "--head-sha",
    "a" * 40,
    "--base-sha",
    "b" * 40,
    "--base-branch",
    "main",
    "--outcome",
    "publication-outcome.json",
    "--github-session-ledger",
    "--allow-write",
    "--enable-review",
    "--enable-auto-approve",
]
with (
    patch.object(github, "GitHubHttp", lambda: http),
    patch.object(github.GitHubApplication, "publish_review", publish),
    patch("review_sensei_github.http.GitHubHttp", InjectedHttp),
    patch.object(socket.socket, "connect", deny),
    redirect_stdout(stdout),
    redirect_stderr(stderr),
):
    status = cli.main(argv)
print(
    json.dumps(
        {
            "exit": status,
            "stdout": stdout.getvalue(),
            "stderr": stderr.getvalue(),
            "http_requests": requests,
            "publication_continuations": publication,
            "result_digest": digest,
            "outcome": json.loads(Path("publication-outcome.json").read_text())
            if Path("publication-outcome.json").exists()
            else None,
            "live_network": "blocked",
            "level": "installed-CLI-mocked-GitHub-operation-gate",
        }
    )
)
sys.exit(status)

#!/usr/bin/env python3
"""Installed trusted operation comment/state integration with a fake HTTP opener."""

import argparse
import hashlib
import importlib.metadata
import io
import json
import os
import socket
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

from review_sensei.hosting.github.http import GitHubHttp
from review_sensei.hosting.github.operation_comments import (
    GitHubIssueCommentOperationPort,
)
from review_sensei.hosting.github.operation_entry import accepted_payload_matches
from review_sensei.hosting.github.operation_host import (
    OPERATION_COMMENT_MARKER,
    render_marked_comment,
)

from review_sensei import cli
from review_sensei.models import ReviewRequest, ReviewResult
from review_sensei.outcomes import RunOutcome
from review_sensei.service import ReviewRun, ReviewService


class Response(io.BytesIO):
    def __init__(self, data, status=200):
        super().__init__(json.dumps(data).encode())
        self.status = status


class FakeAPI:
    def __init__(self, mode="echo", user_type="Bot"):
        self.mode = mode
        self.user_type = user_type
        self.comments = {}
        self.requests = []
        self.next_id = 1

    def opener(self, request, timeout):
        method = request.method
        path = urlsplit(request.full_url).path
        self.requests.append({"method": method, "path": path})
        if method == "GET" and path == "/user":
            return Response(
                {"id": 42, "type": self.user_type, "login": "reviewsensei[bot]"}
            )
        if method == "GET" and "/issues/comments/" not in path:
            return Response(list(self.comments.values()))
        if method in ["POST", "PATCH"]:
            if self.mode == "lost":
                return Response(None, 500)
            id = self.next_id if method == "POST" else int(path.rsplit("/", 1)[1])
            self.next_id += method == "POST"
            data = {
                "id": id,
                "body": json.loads(request.data)["body"],
                "user": {"id": 42, "type": "Bot", "login": "reviewsensei[bot]"},
            }
            self.comments[id] = data
            if self.mode == "id-only":
                return Response({"id": id}, 201 if method == "POST" else 200)
            if self.mode in ["echo-mismatch", "both-mismatch"]:
                data = {
                    **data,
                    "body": render_marked_comment(
                        OPERATION_COMMENT_MARKER, {"synthetic": "mismatched"}
                    ),
                }
            if self.mode in ["echo-author", "both-author"]:
                data = {
                    **data,
                    "user": {"id": 44, "type": "Bot", "login": "other[bot]"},
                }
            if self.mode == "both-user":
                data = {**data, "user": {"id": 42, "type": "User", "login": "other"}}
            return Response(data, 201 if method == "POST" else 200)
        if method == "GET":
            data = self.comments[int(path.rsplit("/", 1)[1])]
            if self.mode == "both-mismatch":
                data = {
                    **data,
                    "body": render_marked_comment(
                        OPERATION_COMMENT_MARKER, {"synthetic": "mismatched"}
                    ),
                }
            if self.mode == "both-author":
                data = {
                    **data,
                    "user": {"id": 44, "type": "Bot", "login": "other[bot]"},
                }
            if self.mode == "both-user":
                data = {**data, "user": {"id": 42, "type": "User", "login": "other"}}
            return Response(data)
        raise AssertionError("unexpected fake route")

    def http(self):
        return GitHubHttp(
            api_url="https://github.synthetic.invalid", opener=self.opener
        )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--expected-version", required=True)
    p.add_argument("--report", required=True, type=Path)
    a = p.parse_args()
    assert importlib.metadata.version("review-sensei") == a.expected_version
    assert "site-packages" in cli.__file__
    a.report.mkdir(parents=True, exist_ok=True)
    rows = []

    def record(id, checks, detail):
        rows.append(
            {
                "id": id,
                "status": "PASS" if all(checks.values()) else "FAIL",
                "checks": checks,
                "receipt": detail,
                "level": "installed-operation-state/HTTP-opener-seam",
            }
        )

    def deny(*args, **kwargs):
        raise AssertionError("live socket connection prohibited")

    with patch.object(socket.socket, "connect", deny):
        for mode, accepted, requests in [
            ("echo", True, 1),
            ("id-only", True, 2),
            ("echo-mismatch", True, 2),
            ("echo-author", True, 2),
            ("both-mismatch", False, 2),
            ("both-author", False, 2),
            ("both-user", False, 2),
            ("lost", False, 1),
        ]:
            fake = FakeAPI(mode)
            port = GitHubIssueCommentOperationPort(
                fake.http(),
                token="synthetic-only",
                repository="owner/repo",
                pull_request=11,
                app_user_id=42,
            )
            error = None
            actual = False
            try:
                port.create(
                    render_marked_comment(
                        OPERATION_COMMENT_MARKER, {"synthetic": "accepted"}
                    )
                )
                actual = True
            except Exception as e:
                error = type(e).__name__ + ": " + str(e)
            record(
                "port-create-" + mode,
                {
                    "acceptance": actual == accepted,
                    "bounded_requests": len(fake.requests) == requests,
                    "no_post_retry": sum(x["method"] == "POST" for x in fake.requests)
                    == 1,
                },
                {"error": error, "requests": fake.requests},
            )
        for partial in [False, True]:
            for appid in ["", "99"]:
                fake = FakeAPI()
                ledger = SimpleNamespace(
                    http=fake.http(), token="synthetic-broker-token"
                )
                calls = []
                result = ReviewResult(
                    summary="Synthetic review",
                    comments=(),
                    provider="fixture",
                    review_status="partial" if partial else "complete",
                )

                class Service:
                    _run_outcome_for_result = staticmethod(
                        ReviewService._run_outcome_for_result
                    )

                    def run(self, request, **kwargs):
                        calls.append("provider")
                        status, diag = self._run_outcome_for_result(result)
                        return ReviewRun(
                            RunOutcome(status, provider_calls=1, diagnostic=diag),
                            result,
                        )

                env = {
                    "GITHUB_RUN_ID": "100",
                    "GITHUB_RUN_ATTEMPT": "1",
                    "REVIEWSENSEI_APP_ID": appid,
                }
                error = None
                first = second = None
                captured_out = io.StringIO()
                captured_err = io.StringIO()
                with (
                    patch.dict(os.environ, env, clear=True),
                    redirect_stdout(captured_out),
                    redirect_stderr(captured_err),
                ):
                    try:
                        kwargs = {
                            "hosted_session_ledger": True,
                            "operation_entry": "enabled",
                            "ledger": ledger,
                            "repository": "owner/repo",
                            "repository_id": 7,
                            "pull_request": 11,
                            "trusted_head_sha": "a" * 40,
                        }
                        first = cli._run_review_provider(
                            Service(), ReviewRequest(diff="synthetic"), **kwargs
                        )
                        second = cli._run_review_provider(
                            Service(), ReviewRequest(diff="synthetic"), **kwargs
                        )
                    except Exception as e:
                        error = type(e).__name__ + ": " + str(e)
                bodies = tuple(c["body"] for c in fake.comments.values())
                digest = hashlib.sha256(
                    json.dumps(
                        result.to_dict(), sort_keys=True, separators=(",", ":")
                    ).encode()
                ).hexdigest()
                accepted = accepted_payload_matches(bodies, digest)
                expected_calls = 1 if appid else 2
                checks = {
                    "no_error": error is None,
                    "provider_count": len(calls) == expected_calls,
                    "operation_presence": bool(fake.comments) == bool(appid),
                    "accepted_digest": accepted is True if appid else accepted is None,
                    "same_result_bytes": first is not None
                    and second is not None
                    and first.result.to_dict() == second.result.to_dict(),
                    "partial_preserved": second is not None
                    and second.result.review_status == result.review_status,
                }
                record(
                    "analysis-"
                    + ("partial" if partial else "complete")
                    + "-appid-"
                    + (appid or "empty"),
                    checks,
                    {
                        "error": error,
                        "provider_calls": len(calls),
                        "operation_comment_count": len(fake.comments),
                        "requests": fake.requests,
                        "accepted_payload_match": accepted,
                        "result_digest": digest,
                        "replayed_status": second.outcome.status if second else None,
                    },
                )
        for field, new_value in [
            ("trusted_head_sha", "c" * 40),
            ("pull_request", 12),
            ("repository_id", 8),
            ("repository", "owner/other"),
        ]:
            fake = FakeAPI()
            ledger = SimpleNamespace(http=fake.http(), token="synthetic-broker-token")
            calls = []

            class IdentityService:
                _run_outcome_for_result = staticmethod(
                    ReviewService._run_outcome_for_result
                )

                def run(self, request, **kwargs):
                    calls.append(kwargs["trusted_head_sha"])
                    result = ReviewResult(
                        summary="Synthetic " + kwargs["trusted_head_sha"],
                        comments=(),
                        provider="fixture",
                        review_status="partial",
                    )
                    return ReviewRun(
                        RunOutcome(
                            "partial", provider_calls=1, diagnostic="partial_coverage"
                        ),
                        result,
                    )

            error = None
            with patch.dict(
                os.environ,
                {
                    "GITHUB_RUN_ID": "100",
                    "GITHUB_RUN_ATTEMPT": "1",
                    "REVIEWSENSEI_APP_ID": "99",
                },
                clear=True,
            ):
                try:
                    kwargs = {
                        "hosted_session_ledger": True,
                        "operation_entry": "enabled",
                        "ledger": ledger,
                        "repository": "owner/repo",
                        "repository_id": 7,
                        "pull_request": 11,
                        "trusted_head_sha": "a" * 40,
                    }
                    first = cli._run_review_provider(
                        IdentityService(), ReviewRequest(diff="synthetic"), **kwargs
                    )
                    changed = {**kwargs, field: new_value}
                    second = cli._run_review_provider(
                        IdentityService(), ReviewRequest(diff="synthetic"), **changed
                    )
                except Exception as e:
                    error = type(e).__name__ + ": " + str(e)
            record(
                "analysis-identity-change-" + field,
                {
                    "no_cross_identity_replay": len(calls) == 1,
                    "identity_change_refused": error is not None
                    and ("binding_conflict" in error or "grant_invalid" in error),
                },
                {
                    "error": error,
                    "provider_calls": len(calls),
                    "changed_field": field,
                    "requests": fake.requests,
                },
            )
        # Existing metadata token may read PR comments, but bot writes must retain
        # App-authored identity. A User token cannot become an operation store owner.
        fake = FakeAPI(user_type="User")
        from review_sensei.hosting.github.operation_entry import (
            analysis_payload,
            run_actions_analysis,
        )

        called = []
        admitted = run_actions_analysis(
            http=fake.http(),
            token="synthetic-metadata-token",
            repository="owner/repo",
            repository_id=7,
            pull_request=11,
            run_id="100",
            run_attempt="1",
            app_id=99,
            reservation_id="reservation",
            attestation_digest="a" * 64,
            execute=lambda: called.append(True),
            encode=analysis_payload,
        )
        record(
            "metadata-user-cannot-author-operation",
            {
                "refused": getattr(admitted, "reason", None) == "unavailable",
                "no_provider": not called,
                "no_write": all(x["method"] == "GET" for x in fake.requests),
            },
            {"reason": getattr(admitted, "reason", None), "requests": fake.requests},
        )
    data = {
        "version": a.expected_version,
        "total": len(rows),
        "pass": sum(r["status"] == "PASS" for r in rows),
        "fail": sum(r["status"] == "FAIL" for r in rows),
        "rows": rows,
        "scope": "actual installed operation port and state host, in-memory HTTP opener, no live writes or inference",
    }
    (a.report / "results.json").write_text(json.dumps(data, indent=2))
    print(json.dumps({k: v for k, v in data.items() if k != "rows"}, indent=2))
    for row in rows:
        if row["status"] != "PASS":
            print(json.dumps(row))
    return bool(data["fail"])


if __name__ == "__main__":
    sys.exit(main())

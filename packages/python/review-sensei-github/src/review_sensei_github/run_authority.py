"""Current purpose-scoped broker documents; parsing alone confers no authority.

The Worker authenticates OIDC, the live source and actor. The owning process
then compares the returned grant with its exact request before native effects.
Neither this document nor a retained historical grant restores request capacity.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Any, Mapping, cast

from .errors import GitHubBrokerClientError

AUDIENCE = "reviewsensei-run-action"
REQUEST_KEYS = frozenset(
    {
        "version",
        "purpose",
        "repository",
        "repository_id",
        "pull_request",
        "head_sha",
        "run_id",
        "run_attempt",
        "job",
        "job_workflow_ref",
        "job_workflow_sha",
        "issued_at",
        "expires_at",
        "audience",
        "source",
    }
)
SOURCE_KEYS = frozenset({"kind", "id", "updated_at", "body_sha256"})
CAPABILITIES = {
    "review": frozenset(
        {
            "review_session",
            "review_publish",
            "check_publish",
            "thread_resolve",
            "review_status",
            "review_actions",
            "learning_write",
        }
    ),
    "command": frozenset(
        {
            "review_session",
            "review_publish",
            "check_publish",
            "thread_resolve",
            "review_status",
            "review_actions",
            "issue_reply",
        }
    ),
    "reply": frozenset(
        {
            "review_session",
            "inline_reply",
            "issue_reply",
            "review_status",
            "review_actions",
            "review_publish",
            "check_publish",
            "thread_resolve",
        }
    ),
}


def _positive(value: object) -> bool:
    return type(value) is int and 0 < value <= 9_007_199_254_740_991


def parse_authority(value: object, *, grant: bool = False) -> dict[str, Any]:
    """Closed current schema, deliberately rejecting obsolete v1/v2 values."""

    def invalid() -> None:
        raise GitHubBrokerClientError("Broker current run authority is invalid")

    if not isinstance(value, Mapping) or set(value) != (
        REQUEST_KEYS | {"actor"} if grant else REQUEST_KEYS
    ):
        invalid()
    try:
        result = cast(
            dict[str, Any], json.loads(json.dumps(dict(cast(Mapping, value))))
        )
    except (TypeError, ValueError, OverflowError, RecursionError) as error:
        raise GitHubBrokerClientError(
            "Broker current run authority is invalid"
        ) from error
    purpose, source = result["purpose"], result["source"]
    if (
        type(result["version"]) is not int
        or result["version"] != 3
        or not isinstance(purpose, str)
        or purpose not in CAPABILITIES
        or not isinstance(result["repository"], str)
        or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", result["repository"])
        is None
        or not _positive(result["repository_id"])
        or not _positive(result["pull_request"])
        or not isinstance(result["head_sha"], str)
        or re.fullmatch(r"[a-f0-9]{40}", result["head_sha"]) is None
        or not isinstance(result["run_id"], str)
        or re.fullmatch(r"[1-9][0-9]{0,18}", result["run_id"]) is None
        or type(result["run_attempt"]) is not int
        or result["run_attempt"] not in {1, 2}
        or not isinstance(result["job"], str)
        or result["job"]
        not in ({"command"} if purpose == "command" else {"hosted", "local"})
        or not isinstance(result["job_workflow_ref"], str)
        or re.fullmatch(
            r"malsabbagh/review-sensei/\.github/workflows/review-sensei-run\.yml@refs/tags/[A-Za-z0-9][A-Za-z0-9._-]*",
            result["job_workflow_ref"],
        )
        is None
        or not isinstance(result["job_workflow_sha"], str)
        or re.fullmatch(r"[a-f0-9]{40}", result["job_workflow_sha"]) is None
        or not _positive(result["issued_at"])
        or not _positive(result["expires_at"])
        or not 0 < result["expires_at"] - result["issued_at"] <= 600
        or result["audience"] != AUDIENCE
        or not isinstance(source, dict)
        or set(source) != SOURCE_KEYS
        or not _positive(source["id"])
    ):
        invalid()
    if purpose == "review":
        if source != {
            "kind": "pull_request",
            "id": result["pull_request"],
            "updated_at": None,
            "body_sha256": None,
        }:
            invalid()
    elif (
        not isinstance(source["kind"], str)
        or source["kind"]
        not in (
            {"issue_comment"}
            if purpose == "command"
            else {"issue_comment", "review_comment"}
        )
        or not isinstance(source["updated_at"], str)
        or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", source["updated_at"])
        is None
        or not isinstance(source["body_sha256"], str)
        or re.fullmatch(r"[a-f0-9]{64}", source["body_sha256"]) is None
    ):
        invalid()
    if grant:
        actor = result["actor"]
        if purpose == "review":
            if actor is not None:
                invalid()
        elif (
            not isinstance(actor, dict)
            or set(actor) != {"login", "type", "association"}
            or not isinstance(actor["login"], str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", actor["login"]) is None
            or actor["type"] != "User"
            or not isinstance(actor["association"], str)
            or actor["association"] not in {"OWNER", "MEMBER", "COLLABORATOR"}
        ):
            invalid()
    return result


def request_authority(
    *,
    purpose: str,
    repository: str,
    repository_id: int,
    pull_request: int,
    head_sha: str,
    job: str,
    source: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    now = int(time.time())
    return parse_authority(
        {
            "version": 3,
            "purpose": purpose,
            "repository": repository,
            "repository_id": repository_id,
            "pull_request": pull_request,
            "head_sha": head_sha,
            "run_id": os.environ.get("GITHUB_RUN_ID", ""),
            "run_attempt": int(os.environ.get("GITHUB_RUN_ATTEMPT", "0")),
            "job": job,
            "job_workflow_ref": os.environ.get("REVIEW_SENSEI_WORKFLOW_REF", ""),
            "job_workflow_sha": os.environ.get("REVIEW_SENSEI_WORKFLOW_SHA", ""),
            "issued_at": now,
            "expires_at": now + 600,
            "audience": AUDIENCE,
            "source": dict(source)
            if source is not None
            else {
                "kind": "pull_request",
                "id": pull_request,
                "updated_at": None,
                "body_sha256": None,
            },
        }
    )


class VerifiedRunAction:
    """Process-owned current grant bound to one native metered action ledger."""

    def __init__(self, run: Any):
        from .api_run_requests import ApiRunSessionLedger

        if not isinstance(run.ledger, ApiRunSessionLedger) or run.session_grant is None:
            raise GitHubBrokerClientError("Current action ledger is unavailable")
        document = parse_authority(run.session_authority, grant=True)
        if document["purpose"] not in {"command", "reply"}:
            raise GitHubBrokerClientError("Current grant has the wrong action purpose")
        verified = run.broker.verify_session_grant(run.session_grant, document)
        if verified != document or (
            document["run_id"] != str(run.caller.run.run_id)
            or document["run_attempt"] != run.caller.accounting.owner.run_attempt
            or document["job"] != run.caller.accounting.owner.job
            or document["repository_id"] != run.identity.repository_id
            or document["repository"] != run.identity.repository
            or document["pull_request"] != run.identity.pull_request
            or document["head_sha"] != run.head_sha
        ):
            raise GitHubBrokerClientError("Current action grant binding conflicts")
        self.run, self.document = run, document
        self.owner = run.caller.accounting.owner

    @property
    def token(self) -> str:
        return cast(str, self.run.ledger.token)

    def require(
        self,
        ledger: Any,
        *,
        purpose: str,
        repository: str,
        repository_id: int,
        pull_request: int,
        head_sha: str,
        source_comment_id: int,
        body: str,
    ) -> Mapping[str, Any]:
        document = self.document
        if (
            ledger is not self.run.ledger
            or self.run.caller.accounting.owner != self.owner
            or (
                document["purpose"],
                document["repository"],
                document["repository_id"],
                document["pull_request"],
                document["head_sha"],
                document["source"]["id"],
            )
            != (
                purpose,
                repository,
                repository_id,
                pull_request,
                head_sha,
                source_comment_id,
            )
            or document["source"]["body_sha256"]
            != hashlib.sha256(body.encode()).hexdigest()
        ):
            raise GitHubBrokerClientError("Current action scope or source conflicts")
        self.fence()
        return cast(Mapping[str, Any], document["actor"])

    def fence(self) -> None:
        """Fresh source/head read before an action's native business mutation."""
        run, document = self.run, self.document
        if run.caller.accounting.owner != self.owner:
            raise GitHubBrokerClientError("Current action owner changed")
        source = document["source"]
        suffix = (
            "/issues/comments/"
            if source["kind"] == "issue_comment"
            else "/pulls/comments/"
        ) + str(source["id"])
        with run.caller.read_operation("action-source-fence"):
            status, current = run.http.request(
                "GET",
                run.http.repository_path(document["repository"], suffix),
                token=self.token,
            )
            if (
                status != 200
                or not isinstance(current, dict)
                or current.get("id") != source["id"]
                or current.get("updated_at") != source["updated_at"]
                or not isinstance(current.get("body"), str)
                or hashlib.sha256(current["body"].encode()).hexdigest()
                != source["body_sha256"]
                or not isinstance(current.get("user"), dict)
                or current["user"].get("login") != document["actor"]["login"]
                or current["user"].get("type") != "User"
                or current.get("author_association") != document["actor"]["association"]
            ):
                raise GitHubBrokerClientError("Current action source changed")
            status, pull = run.http.request(
                "GET",
                run.http.repository_path(
                    document["repository"], "/pulls/" + str(document["pull_request"])
                ),
                token=self.token,
            )
            if (
                status != 200
                or not isinstance(pull, dict)
                or not isinstance(pull.get("head"), dict)
                or pull["head"].get("sha") != document["head_sha"]
                or pull.get("state") != "open"
            ):
                raise GitHubBrokerClientError("Current action head changed")

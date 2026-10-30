"""Controlled serialized restart, independent of predecessor execution formats.

The packaged empty registry is an activation gate, never a CLI grant. Each row
names one already-designated execution. A crash consumes that execution; a new
run or an absent POST acknowledgement does not authorize another initializer.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import weakref
from importlib.resources import files
from pathlib import Path
from typing import Any, cast

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.session import MAX_SESSION_COMMENT_BYTES

MAX_ARCHIVE_BYTES = 262_144
MAX_OLD_OBJECTS = 8
_ISSUED: weakref.WeakSet[RestartAdmission] = weakref.WeakSet()
_KEYS = {
    "request_id",
    "repository",
    "repository_id",
    "pull_request",
    "base_sha",
    "head_sha",
    "policy_commit",
    "workflow_sha",
    "run_id",
    "job",
    "issuance_disabled_at",
    "retire_after",
    "old_runs",
    "old_comments",
}


def read_registry(raw: bytes) -> list[dict[str, Any]]:
    if not isinstance(raw, bytes) or len(raw) > 32_768:
        raise ReviewInputError("controlled restart registry exceeds its bound")
    value = json.loads(raw)
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "registrations"}
        or value["schema_version"] != "controlled-restart-registry-v1"
        or not isinstance(value["registrations"], list)
        or len(value["registrations"]) > 32
    ):
        raise ReviewInputError("controlled restart registry is unsupported")
    rows = value["registrations"]
    scopes, executions = set(), set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != _KEYS:
            raise ReviewInputError("controlled restart registration is unsupported")
        if (
            not isinstance(row["repository"], str)
            or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", row["repository"])
            is None
            or any(
                type(row[k]) is not int or not 0 < row[k] <= 9_007_199_254_740_991
                for k in (
                    "repository_id",
                    "pull_request",
                    "issuance_disabled_at",
                    "retire_after",
                )
            )
            or any(
                not isinstance(row[k], str)
                or re.fullmatch(r"[a-f0-9]{40}", row[k]) is None
                for k in ("base_sha", "head_sha", "policy_commit", "workflow_sha")
            )
            or not isinstance(row["run_id"], str)
            or re.fullmatch(r"[1-9][0-9]{0,18}", row["run_id"]) is None
            or not isinstance(row["job"], str)
            or row["job"] not in {"hosted", "local"}
            or row["retire_after"] < row["issuance_disabled_at"] + 3600
            or not isinstance(row["old_runs"], list)
            or not 1 <= len(row["old_runs"]) <= MAX_OLD_OBJECTS
            or any(
                type(v) is not int or not 0 < v <= 9_007_199_254_740_991
                for v in row["old_runs"]
            )
            or len(set(row["old_runs"])) != len(row["old_runs"])
            or int(row["run_id"]) in row["old_runs"]
            or not isinstance(row["old_comments"], list)
            or not 1 <= len(row["old_comments"]) <= MAX_OLD_OBJECTS
        ):
            raise ReviewInputError("controlled restart registration is invalid")
        ids = set()
        for ref in row["old_comments"]:
            if (
                not isinstance(ref, dict)
                or set(ref) != {"id", "sha256", "bytes"}
                or type(ref["id"]) is not int
                or not 0 < ref["id"] <= 9_007_199_254_740_991
                or type(ref["bytes"]) is not int
                or not 0 < ref["bytes"] <= 65_536
                or not isinstance(ref["sha256"], str)
                or re.fullmatch(r"[a-f0-9]{64}", ref["sha256"]) is None
                or ref["id"] in ids
            ):
                raise ReviewInputError(
                    "controlled restart archive reference is invalid"
                )
            ids.add(ref["id"])
        digest = hashlib.sha256(
            canonical_bytes({k: v for k, v in row.items() if k != "request_id"})
        ).hexdigest()
        scope = row["repository_id"], row["pull_request"]
        execution = row["repository_id"], row["run_id"]
        if row["request_id"] != digest or scope in scopes or execution in executions:
            raise ReviewInputError("controlled restart slot conflicts")
        scopes.add(scope)
        executions.add(execution)
    return rows


def registrations() -> list[dict[str, Any]]:
    return read_registry(
        files("review_sensei_github").joinpath("controlled_restarts.json").read_bytes()
    )


def entry_registration(
    *,
    repository: str,
    repository_id: int,
    pull_request: int,
    request_id: str | None,
    purpose: str,
    policy_commit: str,
    base_sha: str,
    head_sha: str,
    job: str | None = None,
) -> dict[str, Any] | None:
    rows = [
        row
        for row in registrations()
        if (row["repository_id"], row["pull_request"]) == (repository_id, pull_request)
    ]
    if not rows and request_id is None:
        return None
    if len(rows) != 1:
        raise ReviewInputError("controlled restart is not registered")
    row = rows[0]
    if (
        purpose != "review"
        or request_id != row["request_id"]
        or row["repository"] != repository
        or row["policy_commit"] != policy_commit
        or row["base_sha"] != base_sha
        or row["head_sha"] != head_sha
        or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
        or os.environ.get("GITHUB_RUN_ID") != row["run_id"]
        or os.environ.get("GITHUB_RUN_ATTEMPT") != "1"
        or os.environ.get("REVIEW_SENSEI_WORKFLOW_SHA") != row["workflow_sha"]
        or job is not None
        and job != row["job"]
    ):
        raise ReviewInputError(
            "ordinary entry is held; one designated controlled restart is required"
        )
    if time.time() < row["retire_after"]:
        raise ReviewInputError("old capabilities have not reached documented expiry")
    return row


class RestartAdmission:
    """Process-issued selection proof; archive data never grants execution."""

    def __init__(
        self, ledger: Any, identity: Any, registration: dict[str, Any], archive: bytes
    ):
        self._ledger, self._identity = ledger, identity
        self._registration = canonical_bytes(registration)
        self._archive = hashlib.sha256(archive).hexdigest()
        self._comment_id: int | None = None
        self._created = False
        self._runtime: Any = None

    @property
    def marker(self) -> str:
        row = json.loads(self._registration)
        return f"<!-- reviewsensei:controlled-restart:v1 request={row['request_id']} archive={self._archive} -->"

    def require(self, ledger: Any, identity: Any) -> dict[str, Any]:
        if (
            self not in _ISSUED
            or ledger is not self._ledger
            and ledger is not self._runtime
            or identity != self._identity
        ):
            raise ReviewInputError("controlled restart admission is foreign")
        return json.loads(self._registration)

    def prewrite_fence_requests(self, caller: Any) -> int:
        row = self.require(self._ledger, self._identity)
        expected = getattr(self._ledger, "_startup_caller", None)
        if expected is None or caller is not expected:
            raise ReviewInputError("controlled restart fence allocation is foreign")
        owner = cast(Any, caller)
        if (
            str(owner.run.run_id) != row["run_id"]
            or owner.run.repository_id != row["repository_id"]
        ):
            raise ReviewInputError("controlled restart fence allocation is foreign")
        return 1

    def bind_runtime(self, ledger: Any) -> None:
        # The owning adapter copies native state into its funded runtime ledger.
        # Only that explicit, independently bound transport may share selection.
        if self not in _ISSUED or self._runtime is not None:
            raise ReviewInputError("controlled restart runtime is already bound")
        caller: Any = getattr(self._ledger, "_startup_caller", None)
        if (
            caller is None
            or getattr(ledger, "caller", None) is not caller
            or ledger.http is not self._ledger.http
        ):
            raise ReviewInputError("controlled restart runtime transport is foreign")
        # Identity comparisons on dynamic adapter attributes can widen Any to
        # Any | None in type analysis; the preceding guard rejected None.
        caller = cast(Any, caller)
        caller._require_ledger(ledger, self._identity)
        row = json.loads(self._registration)
        if str(caller.accounting.identity.run_id) != row["run_id"]:
            raise ReviewInputError("controlled restart runtime owner changed")
        self._runtime = ledger

    def select(self, ledger: Any, identity: Any, items: list[Any]) -> list[Any]:
        row = self.require(ledger, identity)
        selected = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("body"), str):
                continue
            if item["body"].startswith(self.marker + "\n"):
                author = item.get("user")
                if (
                    not isinstance(author, dict)
                    or author.get("type") != "Bot"
                    or not isinstance(author.get("login"), str)
                    or author["login"].casefold() != ledger.app_slug.casefold()
                ):
                    continue
                if type(item.get("id")) is not int or item["id"] in {
                    v["id"] for v in row["old_comments"]
                }:
                    raise ReviewInputError("controlled restart target is not fresh")
                if self._comment_id is not None and item["id"] != self._comment_id:
                    raise ReviewInputError("controlled restart successor changed")
                selected.append(item)
        if len(selected) > 1:
            raise ReviewInputError("competing controlled restart successors")
        if selected:
            self._comment_id = selected[0]["id"]
        elif self._comment_id is not None or self._created:
            raise ReviewInputError(
                "controlled restart successor is absent or creation is uncertain"
            )
        return selected

    def render(self, ledger: Any, record: Any, body: str) -> str:
        row = self.require(ledger, record.identity())
        accounting = record.api_run_accounting
        if (
            not isinstance(accounting, dict)
            or str(accounting["identity"]["run_id"]) != row["run_id"]
        ):
            raise ReviewInputError("controlled restart accounting is not independent")
        result = self.marker + "\n" + body
        if len(result.encode("utf-8")) > MAX_SESSION_COMMENT_BYTES:
            raise ReviewInputError("controlled restart comment exceeds its byte bound")
        return result


def prepare_admission(
    *,
    ledger: Any,
    identity: Any,
    registration: dict[str, Any],
    actions_token: str,
    directory: Path,
) -> RestartAdmission:
    """Archive opaque bytes after independently confirming identified cessation.

    No cancellation privilege is requested. If an execution is still running,
    the operator must stop it through existing controls and use a new slot.
    """
    from .review_run import _get

    expected = entry_registration(
        repository=identity.repository,
        repository_id=identity.repository_id,
        pull_request=identity.pull_request,
        request_id=registration["request_id"],
        purpose="review",
        policy_commit=registration["policy_commit"],
        base_sha=registration["base_sha"],
        head_sha=registration["head_sha"],
        job=registration["job"],
    )
    if expected != registration:
        raise ReviewInputError("controlled restart is not an installed designation")
    http = ledger.http
    archive: dict[str, Any] = {
        "schema_version": "controlled-restart-archive-v1",
        "status": "abandoned",
        "historical_cost": "unknown",
        "historical_effects": "unsettled",
        "registration": registration,
        "comments": [],
        "runs": [],
    }
    ledger.evidence_budget.preflight(
        2 * len(registration["old_runs"]) + len(registration["old_comments"]) + 16
    )
    for run_id in registration["old_runs"]:
        path = http.repository_path(identity.repository, f"/actions/runs/{run_id}")
        run = _get(http, path, actions_token)
        repository = run.get("repository")
        if (
            run.get("id") != run_id
            or not isinstance(repository, dict)
            or repository.get("id") != identity.repository_id
            or repository.get("full_name") != identity.repository
            or run.get("status") != "completed"
            or type(run.get("run_attempt")) is not int
        ):
            raise ReviewInputError("identified old execution has not stopped")
        jobs = _get(
            http,
            path + f"/attempts/{run['run_attempt']}/jobs?per_page=100&page=1",
            actions_token,
        )
        values = jobs.get("jobs")
        if (
            not isinstance(values, list)
            or not values
            or jobs.get("total_count") != len(values)
            or len(values) > 100
            or any(
                not isinstance(v, dict)
                or v.get("run_id") != run_id
                or v.get("status") != "completed"
                for v in values
            )
        ):
            raise ReviewInputError(
                "identified old execution inventory is incomplete or active"
            )
        archive["runs"].append(
            {"run_id": run_id, "run_attempt": run["run_attempt"], "status": "completed"}
        )
    for ref in registration["old_comments"]:
        item = _get(
            http,
            http.repository_path(identity.repository, f"/issues/comments/{ref['id']}"),
            ledger.token,
        )
        user, body = item.get("user"), item.get("body")
        if (
            item.get("id") != ref["id"]
            or not isinstance(user, dict)
            or user.get("type") != "Bot"
            or not isinstance(user.get("login"), str)
            or user["login"].casefold() != ledger.app_slug.casefold()
            or not isinstance(body, str)
            or len(body.encode("utf-8")) != ref["bytes"]
            or hashlib.sha256(body.encode("utf-8")).hexdigest() != ref["sha256"]
            or item.get("issue_url")
            != http.api_url
            + http.repository_path(
                identity.repository, f"/issues/{identity.pull_request}"
            )
        ):
            raise ReviewInputError("opaque predecessor archive binding changed")
        archive["comments"].append({"id": ref["id"], "body": body, "user": user})
    raw = canonical_bytes(archive)
    if len(raw) > MAX_ARCHIVE_BYTES:
        raise ReviewInputError("controlled restart archive exceeds its byte bound")
    path = directory / "abandoned-history.json"
    try:
        with path.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as error:
        raise ReviewInputError(
            "controlled restart archive slot is consumed or unavailable"
        ) from error
    if path.read_bytes() != raw:
        raise ReviewInputError("controlled restart archive readback failed")
    proof = RestartAdmission(ledger, identity, registration, raw)
    _ISSUED.add(proof)
    return proof


class RestartEffects:
    """Fresh-target fence; complete reads remain unfiltered and charged.

    Historical pending creations have no reliable occupancy bound in an opaque
    archive. This first controlled route therefore publishes COMMENT summaries
    only when there are no new inline findings. It never transfers a decision,
    reuses old mutable objects, resolves threads or submits native approval.
    """

    def __init__(self, admission: RestartAdmission):
        self.admission = admission
        self.owned: set[str] = set()
        self.attempted: set[bytes] = set()
        self._review_id: int | None = None
        self._review_dispatches: dict[bytes, tuple[Any, int]] = {}

    def confirm_review(self, path: str, review: Any) -> bool:
        """Select only an exact positive echo of this process's scoped POST.

        A semantic finding/result marker alone is repeatable across sessions.
        The independent restart marker and full attempted payload prevent an
        older same-head review from becoming successor write authority.
        """
        row = self.admission.require(self.admission._ledger, self.admission._identity)
        ledger = self.admission._ledger
        expected = ledger.http.repository_path(
            row["repository"], f"/pulls/{row['pull_request']}/reviews"
        )
        if path != expected or not isinstance(review, dict):
            return False
        user = review.get("user")
        body = review.get("body")
        identifier = review.get("id")
        if (
            type(identifier) is not int
            or identifier <= 0
            or not isinstance(user, dict)
            or user.get("type") != "Bot"
            or str(user.get("login", "")).casefold() != ledger.app_slug.casefold()
            or review.get("commit_id") != row["head_sha"]
            or review.get("state") != "COMMENTED"
            or review.get("pull_request_url") != ledger.http.api_url + path[:-8]
            or not isinstance(body, str)
            or not body.endswith("\n" + self.admission.marker)
        ):
            return False
        intent = canonical_bytes(
            {
                "path": path,
                "body": {
                    "body": body,
                    "event": "COMMENT",
                    "commit_id": row["head_sha"],
                    "comments": [],
                },
            }
        )
        if intent not in self.attempted:
            return False
        if self._review_id is not None and self._review_id != identifier:
            raise ReviewInputError("competing controlled restart review echoes")
        retained = self._review_dispatches.get(intent)
        if retained is not None:
            from .caller_operation import caller_for_http

            caller, start = retained
            if caller_for_http(ledger.http) is not caller:
                raise ReviewInputError("controlled restart review owner changed")
            writes = [
                item for item in caller.observations[start:] if item.get("mutation")
            ]
            if len(writes) != 1:
                raise ReviewInputError(
                    "controlled restart review dispatch is ambiguous"
                )
            # Positive exact echo settles this effect only. The original
            # unknown observation and its charge remain, without a refill.
            if writes[0]["outcome"] in {"unknown", "expired"} or writes[0].get(
                "uncertain_effect"
            ):
                caller._reconciled_mutations.add(str(writes[0]["request_id"]))
            self._review_dispatches.pop(intent)
        self._review_id = identifier
        self.owned.add(path + f"/{identifier}")
        return True

    def before_request(self, method: str, path: str, body: Any) -> None:
        if (
            method == "GET"
            or path == "/graphql"
            and isinstance(body, dict)
            and "mutation" not in str(body.get("query", "")).lower()
        ):
            return
        ledger = self.admission._ledger
        row = self.admission.require(ledger, self.admission._identity)
        if method not in {"POST", "PATCH"} or path == "/graphql":
            raise ReviewInputError(
                "controlled restart cannot mutate historical targets"
            )
        root = ledger.http.repository_path(row["repository"])
        if method == "POST" and path not in {
            root + f"/issues/{row['pull_request']}/comments",
            root + f"/pulls/{row['pull_request']}/reviews",
            root + "/check-runs",
        }:
            raise ReviewInputError(
                "controlled restart POST target is outside its scope"
            )
        if method == "PATCH" and path not in self.owned:
            raise ReviewInputError(
                "controlled restart target is not independently fresh"
            )
        if path.endswith("/reviews"):
            if (
                not isinstance(body, dict)
                or body.get("event") != "COMMENT"
                or body.get("comments")
            ):
                raise ReviewInputError(
                    "historical occupancy is uncertain; finding publication and approval remain held"
                )
        from .review_run import _get

        current = _get(
            ledger.http,
            ledger.http.repository_path(
                row["repository"], f"/pulls/{row['pull_request']}"
            ),
            ledger.token,
        )
        if (
            current.get("state") != "open"
            or current.get("base", {}).get("sha") != row["base_sha"]
            or current.get("head", {}).get("sha") != row["head_sha"]
        ):
            raise ReviewInputError("controlled restart current base or head changed")
        if method == "POST":
            key = canonical_bytes({"path": path, "body": body})
            if key in self.attempted:
                raise ReviewInputError(
                    "controlled restart POST was already attempted; positive reconciliation is required"
                )
            self.attempted.add(key)
            if path.endswith("/reviews"):
                from .caller_operation import caller_for_http

                caller = caller_for_http(ledger.http)
                if caller is not None:
                    self._review_dispatches[key] = (caller, len(caller.observations))
            if (
                path.endswith(f"/issues/{row['pull_request']}/comments")
                and isinstance(body, dict)
                and str(body.get("body", "")).startswith(self.admission.marker + "\n")
            ):
                self.admission._created = True

    def after_response(self, method: str, path: str, status: int, value: Any) -> None:
        if (
            method != "POST"
            or status not in {200, 201}
            or not isinstance(value, dict)
            or type(value.get("id")) is not int
        ):
            return
        row = json.loads(self.admission._registration)
        root = self.admission._ledger.http.repository_path(row["repository"])
        identifier = value["id"]
        if identifier in {ref["id"] for ref in row["old_comments"]}:
            raise ReviewInputError(
                "controlled restart write returned a historical identity"
            )
        if path.endswith(f"/issues/{row['pull_request']}/comments"):
            self.owned.add(root + f"/issues/comments/{identifier}")
        elif path.endswith("/check-runs"):
            self.owned.add(root + f"/check-runs/{identifier}")
        elif path.endswith(f"/pulls/{row['pull_request']}/reviews"):
            self.owned.add(path + f"/{identifier}")


def effect_guard(http: Any) -> RestartEffects | None:
    for _ in range(16):
        guard = getattr(http, "_controlled_restart_guard", None)
        if guard is not None:
            return guard
        transport = getattr(http, "transport", None)
        if transport is None:
            return None
        http = transport
    raise ReviewInputError("controlled restart transport chain exceeds its bound")


def owns_target(http: Any, path: str) -> bool:
    guard = effect_guard(http)
    return guard is None or path in guard.owned


def install_admission(ledger: Any, proof: RestartAdmission) -> None:
    proof.require(ledger, proof._identity)
    ledger._controlled_restart = proof
    http = ledger.http
    while getattr(http, "transport", None) is not None:
        http = http.transport
    if getattr(http, "_controlled_restart_guard", None) is not None:
        raise ReviewInputError("controlled restart transport is already bound")
    http._controlled_restart_guard = RestartEffects(proof)
    caller = getattr(ledger, "_startup_caller", None)
    if caller is not None:
        proof.prewrite_fence_requests(caller)
        caller._controlled_restart_admission = proof


def owns_review(http: Any, path: str, review: Any) -> bool:
    guard = effect_guard(http)
    if guard is None:
        return True
    return path + f"/{review.get('id')}" in guard.owned or guard.confirm_review(
        path, review
    )

"""GitHub issue-comment adapter for the C3 session ledger.

Stores one bounded session record as an App-authored issue comment on the
source pull request. Discovery is fail-closed: multiple markers, truncated
pagination, or a marker/body digest mismatch do not initialize a second
session. This adapter never migrates legacy documents and never rehashes.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from ...errors import ReviewInputError
from ...session import (
    MAX_SESSION_COMMENT_BYTES,
    SessionIdentity,
    SessionLoadError,
    SessionLoadReason,
    SessionLoadResult,
    SessionRecord,
    load_session_status,
    mutate_abort,
    mutate_commit,
    mutate_reserved,
)
from .errors import (
    GitHubHTTPError,
    GitHubHTTPPaginationLimitError,
    GitHubHTTPTransientError,
    GitHubPublicationError,
    GitHubPublicationTransientError,
)
from .http import GitHubHttp

SESSION_MARKER_PREFIX = "<!-- reviewsensei:session:v1"
SESSION_MARKER_RE = re.compile(
    r"<!-- reviewsensei:session:v1 repo=(?P<repository_id>[1-9][0-9]*) "
    r"pr=(?P<pull_request>[1-9][0-9]*) gen=(?P<generation>0|[1-9][0-9]*) "
    r"digest=(?P<digest>[a-f0-9]{64}) -->"
)
_JSON_FENCE_RE = re.compile(
    r"```json\n(?P<body>\{.*?\})\n```",
    re.DOTALL,
)
_SESSION_INTRO = "ReviewSensei session ledger (round counters only; no source)."


def _within_session_comment_limit(body: str) -> bool:
    try:
        return len(body.encode("utf-8")) <= MAX_SESSION_COMMENT_BYTES
    except UnicodeError:
        return False


def session_marker(
    *, repository_id: int, pull_request: int, record: SessionRecord
) -> str:
    return (
        f"{SESSION_MARKER_PREFIX} repo={repository_id} pr={pull_request} "
        f"gen={record.generation} digest={record.record_sha256} -->"
    )


def render_session_comment(
    *, repository_id: int, pull_request: int, record: SessionRecord
) -> str:
    document = json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":"))
    body = (
        f"{_SESSION_INTRO}\n\n```json\n{document}\n```\n\n"
        f"{session_marker(repository_id=repository_id, pull_request=pull_request, record=record)}"
    )
    if len(body.encode("utf-8")) > MAX_SESSION_COMMENT_BYTES:
        raise ReviewInputError("session comment exceeds the configured size limit")
    return body


def parse_session_comment(
    body: object,
    *,
    identity: SessionIdentity,
) -> SessionRecord | None:
    if not isinstance(body, str) or not _within_session_comment_limit(body):
        return None
    matches = list(SESSION_MARKER_RE.finditer(body))
    if not matches:
        return None
    if len(matches) != 1:
        raise SessionLoadError(
            SessionLoadReason.CONFLICT, "session comment marker is ambiguous"
        )
    marker = matches[0]
    if identity.repository_id is None:
        raise ReviewInputError("GitHub session identity requires repository_id")
    if int(marker["repository_id"]) != identity.repository_id:
        raise SessionLoadError(
            SessionLoadReason.CONFLICT,
            "session comment repository_id does not match",
        )
    if int(marker["pull_request"]) != identity.pull_request:
        raise SessionLoadError(
            SessionLoadReason.CONFLICT,
            "session comment pull_request does not match",
        )
    fenced = _JSON_FENCE_RE.search(body)
    if fenced is None:
        raise SessionLoadError(
            SessionLoadReason.INTEGRITY_FAILED, "session comment JSON is missing"
        )
    try:
        payload = json.loads(fenced["body"])
    except json.JSONDecodeError as exc:
        raise SessionLoadError(
            SessionLoadReason.INTEGRITY_FAILED, "session comment JSON is invalid"
        ) from exc
    if not isinstance(payload, dict):
        raise SessionLoadError(
            SessionLoadReason.INTEGRITY_FAILED, "session comment JSON is invalid"
        )
    try:
        record = SessionRecord.from_dict(payload)
    except ReviewInputError as exc:
        raise SessionLoadError(
            SessionLoadReason.INTEGRITY_FAILED, "session comment record is invalid"
        ) from exc
    if record.record_sha256 != marker["digest"]:
        raise SessionLoadError(
            SessionLoadReason.INTEGRITY_FAILED,
            "session comment digest does not match",
        )
    if record.generation != int(marker["generation"]):
        raise SessionLoadError(
            SessionLoadReason.INTEGRITY_FAILED,
            "session comment generation does not match",
        )
    if record.repository != identity.repository:
        raise SessionLoadError(
            SessionLoadReason.INTEGRITY_FAILED,
            "session comment repository does not match",
        )
    return record


class GitHubIssueCommentSessionLedger:
    """GitHub-backed session ledger using one issue comment per pull request."""

    def __init__(self, http: GitHubHttp, *, token: str) -> None:
        if not isinstance(http, GitHubHttp):
            raise ReviewInputError("GitHub session ledger requires GitHubHttp")
        if not isinstance(token, str) or not token.strip():
            raise ReviewInputError("GitHub session ledger token is empty")
        self.http = http
        self.token = token

    def _require_identity(self, identity: SessionIdentity) -> int:
        if identity.repository_id is None:
            raise ReviewInputError("GitHub session identity requires repository_id")
        return identity.repository_id

    def _comments_path(self, identity: SessionIdentity) -> str:
        return self.http.repository_path(
            identity.repository, f"/issues/{identity.pull_request}/comments"
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
    ) -> tuple[int, dict[str, Any] | list[Any] | None]:
        try:
            return self.http.request(method, path, token=self.token, body=body)
        except GitHubHTTPTransientError as exc:
            raise GitHubPublicationTransientError(
                "session ledger request failed"
            ) from exc
        except GitHubHTTPError as exc:
            raise GitHubPublicationError("session ledger request failed") from exc

    def _discover(
        self, identity: SessionIdentity
    ) -> tuple[int | None, SessionRecord | None]:
        repository_id = self._require_identity(identity)
        try:
            items = self.http.paginate(
                path=self._comments_path(identity), token=self.token
            )
        except GitHubHTTPTransientError as exc:
            raise GitHubPublicationTransientError(
                "session ledger discovery failed"
            ) from exc
        except GitHubHTTPPaginationLimitError as exc:
            raise ReviewInputError("session comment discovery exceeded bound") from exc
        except GitHubHTTPError as exc:
            raise GitHubPublicationError("session ledger discovery failed") from exc
        found: list[tuple[int, SessionRecord]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            body = item.get("body")
            if (
                not isinstance(body, str)
                or not _within_session_comment_limit(body)
                or SESSION_MARKER_PREFIX not in body
            ):
                continue
            comment_id = item.get("id")
            if (
                isinstance(comment_id, bool)
                or not isinstance(comment_id, int)
                or comment_id < 1
            ):
                raise SessionLoadError(
                    SessionLoadReason.INTEGRITY_FAILED,
                    "session comment id is invalid",
                )
            record = parse_session_comment(body, identity=identity)
            if record is None:
                continue
            if record.repository_id not in {None, repository_id}:
                raise SessionLoadError(
                    SessionLoadReason.CONFLICT,
                    "session comment repository_id does not match",
                )
            found.append((comment_id, record))
        if len(found) > 1:
            raise SessionLoadError(
                SessionLoadReason.CONFLICT, "multiple session comments are present"
            )
        if not found:
            return None, None
        return found[0]

    def load(
        self, identity: SessionIdentity, *, now: datetime | None = None
    ) -> SessionLoadResult:
        try:
            _comment_id, record = self._discover(identity)
        except SessionLoadError as exc:
            return SessionLoadResult(status=exc.reason.value)
        if record is None:
            return SessionLoadResult(status="missing")
        return load_session_status(record, now=now)

    def initialize(
        self,
        identity: SessionIdentity,
        *,
        now: datetime | None = None,
        expires_at: datetime | str | None = None,
    ) -> SessionRecord:
        loaded = self.load(identity, now=now)
        if loaded.status in {"ok", "migrated"}:
            raise ReviewInputError("session already exists")
        if loaded.status in {"integrity-failed", "conflict"}:
            raise ReviewInputError(f"session ledger load failed: {loaded.status}")
        repository_id = self._require_identity(identity)
        record = SessionRecord.create(identity, now=now, expires_at=expires_at)
        status, payload = self._request(
            "POST",
            self._comments_path(identity),
            body={
                "body": render_session_comment(
                    repository_id=repository_id,
                    pull_request=identity.pull_request,
                    record=record,
                )
            },
        )
        if status not in {200, 201} or not isinstance(payload, dict):
            if status in {409, 422, 429} or status >= 500:
                raise GitHubPublicationTransientError(
                    "session comment create was ambiguous"
                )
            raise GitHubPublicationError("session comment create failed")
        verified_id, verified_record = self._discover(identity)
        if verified_id is None or verified_record is None:
            raise GitHubPublicationTransientError(
                "session comment create could not be verified"
            )
        created_id = payload.get("id")
        if (
            isinstance(created_id, bool)
            or not isinstance(created_id, int)
            or created_id != verified_id
            or verified_record.record_sha256 != record.record_sha256
        ):
            raise SessionLoadError(
                SessionLoadReason.CONFLICT,
                "session comment create raced with another initializer",
            )
        return verified_record

    def _replace(
        self,
        identity: SessionIdentity,
        mutate,
        *,
        now: datetime | None = None,
    ) -> SessionRecord:
        comment_id, record = self._discover(identity)
        if comment_id is None or record is None:
            raise ReviewInputError("session record is missing")
        if record.expired(now=now):
            raise ReviewInputError("session record is missing")
        updated = mutate(record)
        repository_id = self._require_identity(identity)
        path = self.http.repository_path(
            identity.repository, f"/issues/comments/{comment_id}"
        )
        status, payload = self._request(
            "PATCH",
            path,
            body={
                "body": render_session_comment(
                    repository_id=repository_id,
                    pull_request=identity.pull_request,
                    record=updated,
                )
            },
        )
        if status != 200 or not isinstance(payload, dict):
            if status in {409, 422, 429} or status >= 500:
                raise GitHubPublicationTransientError(
                    "session comment update was ambiguous"
                )
            raise GitHubPublicationError("session comment update failed")
        verified = parse_session_comment(payload.get("body"), identity=identity)
        if verified is None or verified.record_sha256 != updated.record_sha256:
            raise ReviewInputError("session comment update lost")
        return verified

    def reserve(
        self,
        identity: SessionIdentity,
        *,
        slot: str,
        reservation_id: str,
        expected_generation: int,
        now: datetime | None = None,
    ) -> SessionRecord:
        return self._replace(
            identity,
            lambda record: mutate_reserved(
                record,
                slot=slot,
                reservation_id=reservation_id,
                expected_generation=expected_generation,
                now=now,
            ),
            now=now,
        )

    def commit(
        self,
        identity: SessionIdentity,
        *,
        reservation_id: str,
        expected_generation: int,
        now: datetime | None = None,
    ) -> SessionRecord:
        return self._replace(
            identity,
            lambda record: mutate_commit(
                record,
                reservation_id=reservation_id,
                expected_generation=expected_generation,
                now=now,
            ),
            now=now,
        )

    def abort(
        self,
        identity: SessionIdentity,
        *,
        reservation_id: str,
        expected_generation: int,
        now: datetime | None = None,
    ) -> SessionRecord:
        return self._replace(
            identity,
            lambda record: mutate_abort(
                record,
                reservation_id=reservation_id,
                expected_generation=expected_generation,
                now=now,
            ),
            now=now,
        )

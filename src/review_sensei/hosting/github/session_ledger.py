"""GitHub issue-comment adapter for the C3 session ledger.

Stores one bounded session record as an App-authored issue comment on the
source pull request. Discovery is fail-closed: multiple markers, truncated
pagination, or a marker/body digest mismatch do not initialize a second
session. This adapter never migrates legacy documents and never rehashes.

GitHub issue-comment updates do not expose a conditional generation or ETag
precondition through this adapter. Mutations therefore use a bounded
pre-discovery check plus a post-update readback: they are best-effort against
cross-process writers, not a strict distributed lock. Callers that require a
no-lost-update guarantee must serialize writers for an identity. A successful
broker verification is consumed before the first remote mutation; an ambiguous
or failed GitHub write therefore consumes that one-attempt grant and must be
retried with a newly issued grant rather than replaying the old one.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Callable, Mapping

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
    GitHubBrokerClientError,
    GitHubHTTPError,
    GitHubHTTPPaginationLimitError,
    GitHubHTTPTransientError,
    GitHubPublicationError,
    GitHubPublicationTransientError,
)
from .http import GitHubHttp

SESSION_MARKER_PREFIX = "<!-- reviewsensei:session:v1"
SESSION_MARKER_RE = re.compile(
    r"(?m)^<!-- reviewsensei:session:v1 repo=(?P<repository_id>[1-9][0-9]*) "
    r"pr=(?P<pull_request>[1-9][0-9]*) gen=(?P<generation>0|[1-9][0-9]*) "
    r"digest=(?P<digest>[a-f0-9]{64}) -->$"
)
# Any marker version, used only to decide whether a body is a session marker
# document at all. Version-agnostic on purpose: a marker written by a newer
# schema must still fail closed rather than be ignored and silently duplicated.
SESSION_MARKER_ANY_VERSION = "<!-- reviewsensei:session:"
_JSON_FENCE_RE = re.compile(
    r"```json\n(?P<body>\{.*\})\n```$",
    re.DOTALL,
)
_SESSION_INTRO = "ReviewSensei session ledger (round counters only; no source)."


def _within_session_comment_limit(body: str) -> bool:
    try:
        return len(body.encode("utf-8")) <= MAX_SESSION_COMMENT_BYTES
    except UnicodeError:
        return False


def _marker_shaped(body: str) -> bool:
    """Return True when a body is a session marker document, not a quotation.

    Only a body that carries a terminal marker line may become established
    unreadable state for the pull request. A body that merely mentions the
    prefix while discussing something else -- an App-posted maintainer quote,
    or prose that references the marker mid-comment -- is not marker-shaped and
    is ignored, so it can never wedge the ledger. A terminal marker line is
    still treated as a marker document even when it fails validation: the
    version-agnostic prefix keeps a newer schema failing closed rather than
    being silently duplicated, and the documented re-enrollment path recovers
    the pull request instead of leaving it permanently stuck.
    """

    terminal_line = body.rstrip().rsplit("\n", 1)[-1].strip()
    return terminal_line.startswith(
        SESSION_MARKER_ANY_VERSION
    ) and terminal_line.endswith("-->")


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
    if identity.repository_id is None:
        raise ReviewInputError("GitHub session identity requires repository_id")
    terminal_end = len(body.rstrip())
    matches = [
        match
        for match in SESSION_MARKER_RE.finditer(body)
        if match.end() == terminal_end
    ]
    matches = [
        match
        for match in matches
        if int(match["repository_id"]) == identity.repository_id
        and int(match["pull_request"]) == identity.pull_request
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise SessionLoadError(
            SessionLoadReason.CONFLICT, "session comment marker is ambiguous"
        )
    marker = matches[0]
    prefix = body[: marker.start()].rstrip()
    fence_start = prefix.rfind("```json\n")
    fenced = (
        _JSON_FENCE_RE.fullmatch(prefix[fence_start:]) if fence_start >= 0 else None
    )
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
    """GitHub-backed session ledger using one issue comment per pull request.

    The GitHub REST API path used here has no conditional PATCH primitive, so
    generation checks are advisory across independent writers. A deployment
    that needs strict compare-and-swap semantics must serialize mutations
    outside this adapter. The caller supplies the short-lived capability token
    exchanged for the review-publication capability; this adapter deliberately
    uses only the issue-comment endpoints on the shared bounded HTTP client.
    """

    def __init__(
        self,
        http: GitHubHttp,
        *,
        token: str,
        app_slug: str | None = None,
        broker: Any | None = None,
        session_grant: str | None = None,
        session_attestation: Mapping[str, object] | None = None,
        head_sha: str | None = None,
    ) -> None:
        if not isinstance(http, GitHubHttp):
            raise ReviewInputError("GitHub session ledger requires GitHubHttp")
        if not isinstance(token, str) or not token.strip():
            raise ReviewInputError("GitHub session ledger token is empty")
        if app_slug is not None and (
            not isinstance(app_slug, str) or not app_slug.strip()
        ):
            raise ReviewInputError("GitHub session ledger app slug is invalid")
        grant_arguments = (broker, session_grant, session_attestation, head_sha)
        if any(argument is not None for argument in grant_arguments) and not all(
            argument is not None for argument in grant_arguments
        ):
            raise ReviewInputError(
                "GitHub session ledger grant configuration is incomplete"
            )
        if broker is not None:
            if not callable(getattr(broker, "verify_session_grant", None)):
                raise ReviewInputError(
                    "GitHub session ledger grant verifier is invalid"
                )
            if not isinstance(session_grant, str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{43}", session_grant
            ):
                raise ReviewInputError("GitHub session ledger grant is invalid")
            if not isinstance(session_attestation, Mapping):
                raise ReviewInputError("GitHub session ledger attestation is invalid")
            if (
                not isinstance(head_sha, str)
                or re.fullmatch(r"[a-f0-9]{40}", head_sha) is None
            ):
                raise ReviewInputError("GitHub session ledger head SHA is invalid")
        self.http = http
        self.token = token
        self.app_slug = app_slug
        self._broker = broker
        self._session_grant = session_grant
        self._session_attestation = (
            dict(session_attestation) if session_attestation is not None else None
        )
        self._head_sha = head_sha

    def _require_identity(self, identity: SessionIdentity) -> int:
        if identity.repository_id is None:
            raise ReviewInputError("GitHub session identity requires repository_id")
        return identity.repository_id

    def _comments_path(self, identity: SessionIdentity) -> str:
        return self.http.repository_path(
            identity.repository, f"/issues/{identity.pull_request}/comments"
        )

    def _verify_mutation_grant(self, identity: SessionIdentity) -> None:
        """Verify one hosted mutation grant before the first GitHub request.

        Read-only status calls deliberately do not consume this authority.  A
        broker-bound instance is created only for an attested maintainer
        command, so identity and head checks here prevent its token from being
        replayed against a different pull request before the broker is asked.
        """

        if self._broker is None:
            return
        assert self._session_grant is not None
        assert self._session_attestation is not None
        assert self._head_sha is not None
        attestation = self._session_attestation
        if (
            attestation.get("repository") != identity.repository
            or attestation.get("repository_id") != identity.repository_id
            or attestation.get("pull_request") != identity.pull_request
            or attestation.get("head_sha") != self._head_sha
        ):
            raise ReviewInputError("session grant scope does not match the mutation")
        try:
            verified = self._broker.verify_session_grant(
                self._session_grant, attestation
            )
        except GitHubBrokerClientError as exc:
            raise ReviewInputError("session grant verification failed") from exc
        if not isinstance(verified, Mapping) or dict(verified) != attestation:
            raise ReviewInputError("session grant verification failed")

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
        self, identity: SessionIdentity, *, now: datetime | None = None
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
            raise SessionLoadError(
                SessionLoadReason.INTEGRITY_FAILED,
                "session comment discovery exceeded bound",
            ) from exc
        except GitHubHTTPError as exc:
            raise GitHubPublicationError("session ledger discovery failed") from exc
        found: list[tuple[int, SessionRecord]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            author = item.get("user")
            trusted_session_author = False
            if author is None and self.app_slug is None:
                # Older test transports omitted the always-present GitHub
                # ``user`` object. Treat it as trusted only for that legacy
                # transport compatibility path. Production callers pass
                # ``app_slug`` and require the explicit bot author below.
                trusted_session_author = True
            else:
                if not isinstance(author, dict) or author.get("type") != "Bot":
                    continue
                author_login = author.get("login")
                if not isinstance(author_login, str):
                    continue
                if self.app_slug is not None and (
                    author_login.casefold() != self.app_slug.casefold()
                ):
                    continue
                trusted_session_author = True
            body = item.get("body")
            if (
                not isinstance(body, str)
                or not _within_session_comment_limit(body)
                or not _marker_shaped(body)
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
                # Only an author that passed the trusted-app check above can
                # turn a malformed marker into established unreadable state.
                # Human quotes were already ignored, so they cannot block a
                # later session initialization. The check is explicit rather
                # than an assertion so the invariant holds under -O and reads
                # as part of the control flow.
                if not trusted_session_author:
                    continue
                raise SessionLoadError(
                    SessionLoadReason.INTEGRITY_FAILED,
                    "session comment marker is malformed",
                )
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
            _comment_id, record = self._discover(identity, now=now)
        except SessionLoadError as exc:
            return SessionLoadResult(status=exc.reason.value, detail=str(exc))
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
        self._verify_mutation_grant(identity)
        loaded = self.load(identity, now=now)
        if loaded.status in {"ok", "migrated"}:
            # Initialization is an idempotent ensure operation. A caller can
            # observe ``missing`` and lose the race before its second load;
            # returning the already validated marker lets that loser continue
            # with the same durable identity instead of failing spuriously.
            if loaded.record is None:
                raise ReviewInputError("session load returned no record")
            return loaded.record
        if loaded.status in {"integrity-failed", "conflict", "expired"}:
            raise ReviewInputError(f"session ledger load failed: {loaded.status}")
        record = SessionRecord.create(identity, now=now, expires_at=expires_at)
        return self._create_initial_record(identity, record, now=now)

    def initialize_with_mutation(
        self,
        identity: SessionIdentity,
        mutate: Callable[[SessionRecord], SessionRecord],
        *,
        now: datetime | None = None,
        expires_at: datetime | str | None = None,
    ) -> SessionRecord:
        """Create a missing marker with its first command mutation applied.

        A hosted command may need to enroll a missing marker and persist its
        requested disposition. Keeping that in one POST means the one-use
        broker grant authorizes one logical command, not two independent
        remote writes.
        """

        self._verify_mutation_grant(identity)
        loaded = self.load(identity, now=now)
        if loaded.status in {"ok", "migrated"}:
            raise ReviewInputError("session already exists")
        if loaded.status in {"integrity-failed", "conflict", "expired"}:
            raise ReviewInputError(f"session ledger load failed: {loaded.status}")
        record = mutate(SessionRecord.create(identity, now=now, expires_at=expires_at))
        return self._create_initial_record(identity, record, now=now)

    def _create_initial_record(
        self,
        identity: SessionIdentity,
        record: SessionRecord,
        *,
        now: datetime | None,
    ) -> SessionRecord:
        repository_id = self._require_identity(identity)
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
        ambiguous = status in {202, 409, 422, 429} or status >= 500
        if not (200 <= status < 300 or ambiguous):
            raise GitHubPublicationError("session comment create failed")
        verified_id, verified_record = self._discover(identity, now=now)
        if verified_id is None or verified_record is None:
            if ambiguous:
                raise GitHubPublicationTransientError(
                    "session comment create could not be verified"
                )
            raise GitHubPublicationTransientError(
                "session comment create could not be verified"
            )
        if verified_record.record_sha256 != record.record_sha256:
            raise SessionLoadError(
                SessionLoadReason.CONFLICT,
                "session comment create raced with another initializer",
            )
        created_id = payload.get("id") if isinstance(payload, dict) else None
        if status in {200, 201} and (
            isinstance(created_id, bool)
            or not isinstance(created_id, int)
            or created_id != verified_id
        ):
            raise SessionLoadError(
                SessionLoadReason.CONFLICT,
                "session comment create raced with another initializer",
            )
        return verified_record

    def replace(
        self,
        identity: SessionIdentity,
        mutate: Callable[[SessionRecord], SessionRecord],
        *,
        now: datetime | None = None,
    ) -> SessionRecord:
        self._verify_mutation_grant(identity)
        comment_id, record = self._discover(identity, now=now)
        if comment_id is None or record is None:
            raise ReviewInputError("session record is missing")
        if record.expired(now=now):
            raise ReviewInputError("session record is missing")
        latest_comment_id, latest_record = self._discover(identity, now=now)
        if (
            latest_comment_id != comment_id
            or latest_record is None
            or latest_record.record_sha256 != record.record_sha256
            or latest_record.generation != record.generation
        ):
            raise ReviewInputError("session generation conflict")
        record = latest_record
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
        payload_id = payload.get("id")
        if (
            isinstance(payload_id, bool)
            or not isinstance(payload_id, int)
            or payload_id != comment_id
            or verified is None
            or verified.record_sha256 != updated.record_sha256
        ):
            raise ReviewInputError("session comment update lost")
        # A successful PATCH response proves only what GitHub returned for
        # that request. Re-read the marker so a concurrent writer that landed
        # immediately after the PATCH is surfaced instead of being silently
        # treated as our committed generation.
        readback_id, readback = self._discover(identity, now=now)
        if (
            readback_id != comment_id
            or readback is None
            or readback.record_sha256 != updated.record_sha256
            or readback.generation != updated.generation
        ):
            raise ReviewInputError("session comment update lost")
        return readback

    def reenroll(
        self,
        identity: SessionIdentity,
        *,
        now: datetime | None = None,
    ) -> SessionRecord:
        """Replace an expired session marker with a freshly enrolled session.

        Recovery for a hosted session is operator-driven and marker-level: the
        App rewrites its own expired comment, so the durable artifact the
        broker witnessed is restored rather than deleted, and the enrollment
        witness and the marker agree again. That is why nothing has to be
        purged in the broker alongside it -- including when the marker was
        deleted, where recovery establishes a fresh comment rather than leaving
        a witness that demands attention until the retention window elapses.
        Only an expired marker or an absent marker is recovered; a live,
        unreadable, or ambiguous marker needs investigation rather than a reset.
        """

        loaded = self.load(identity, now=now)
        if loaded.status == "missing":
            return self.initialize_with_mutation(
                identity, lambda record: record, now=now
            )
        if loaded.status != "expired":
            raise ReviewInputError(
                "only an expired or witness-only session can be re-enrolled"
            )
        self._verify_mutation_grant(identity)
        comment_id, record = self._discover(identity, now=now)
        if comment_id is None or record is None or not record.expired(now=now):
            raise ReviewInputError(
                "only an expired or witness-only session can be re-enrolled"
            )
        repository_id = self._require_identity(identity)
        replacement = SessionRecord.create(identity, now=now)
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
                    record=replacement,
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
        payload_id = payload.get("id")
        if (
            isinstance(payload_id, bool)
            or not isinstance(payload_id, int)
            or payload_id != comment_id
            or verified is None
            or verified.record_sha256 != replacement.record_sha256
        ):
            raise ReviewInputError("session comment update lost")
        # As with a reservation CAS, the PATCH response only proves what
        # GitHub returned for that request. Re-read the marker so a concurrent
        # writer is surfaced rather than treated as this enrollment.
        readback_id, readback = self._discover(identity, now=now)
        if (
            readback_id != comment_id
            or readback is None
            or readback.record_sha256 != replacement.record_sha256
        ):
            raise ReviewInputError("session comment update lost")
        return readback

    # Kept as a compatibility shim for older in-process callers. New code
    # must use the public CAS seam above.
    def _replace(
        self,
        identity: SessionIdentity,
        mutate: Callable[[SessionRecord], SessionRecord],
        *,
        now: datetime | None = None,
    ) -> SessionRecord:
        return self.replace(identity, mutate, now=now)

    def reserve(
        self,
        identity: SessionIdentity,
        *,
        slot: str,
        reservation_id: str,
        expected_generation: int,
        now: datetime | None = None,
    ) -> SessionRecord:
        return self.replace(
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
        return self.replace(
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
        return self.replace(
            identity,
            lambda record: mutate_abort(
                record,
                reservation_id=reservation_id,
                expected_generation=expected_generation,
                now=now,
            ),
            now=now,
        )

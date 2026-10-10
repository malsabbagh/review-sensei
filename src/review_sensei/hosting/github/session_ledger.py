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

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Callable, Mapping, cast

from ...bounded_evidence import (
    MAX_PART_BYTES,
    MAX_STORED_PART_BYTES,
    MAX_STORED_PARTS,
    PARTITION_ENCODING,
    ActivationTailPlan,
    AuthenticatedPart,
    EvidenceReadBudget,
    EvidenceTailTicket,
    NonResumableActivationError,
    TailDispatch,
    canonical_bytes,
    partition_evidence,
    read_partitioned_evidence,
    stage_partitioned_evidence,
)
from ...errors import ReviewInputError
from ...history_association import (
    HistoryAssociation,
    associate_history,
    read_associated_history,
)
from ...session import (
    MAX_SESSION_COMMENT_BYTES,
    SessionIdentity,
    SessionLoadError,
    SessionLoadReason,
    SessionLoadResult,
    SessionRecord,
    _history_extra_ids,
    _history_legacy_write_guard,
    _history_preserve_sources,
    _seal_tail_accounting,
    _tail_attempt_scope,
    _tail_operation_binding,
    _tail_part_ids,
    _tail_plan_scope,
    _validate_queue_retention,
    _validate_tail_draft,
    load_session_status,
    mutate_abort,
    mutate_commit,
    mutate_reserved,
    read_session_assessment_queue,
    read_session_baseline,
)
from .broker_client import BrokerClient, BrokerSessionGrant
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
        reservation_owner: Mapping[str, object] | None = None,
        actions_read_token: str | None = None,
        actions_token_provider: Callable[[], str] | None = None,
        evidence_budget: EvidenceReadBudget | None = None,
        enable_partition_writes: bool = False,
        enable_history_graph: bool = False,
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
        self._reservation_owner = reservation_owner
        self._actions_read_token = actions_read_token
        self._actions_token_provider = actions_token_provider
        self.evidence_budget = evidence_budget
        self.enable_partition_writes = enable_partition_writes
        self.enable_history_graph = enable_history_graph
        self._history_documents: dict[str, bytes] = {}
        self._history_associations: dict[object, Any] = {}
        self._history_reading = False
        self._evidence_author_id: int | None = None
        self._mutation_active = False
        self._tail_authorizing = False
        self._tail_preparing = False
        self._tail_scan_pages: int | None = None
        self._activation_ticket: EvidenceTailTicket | None = None
        self._activation_identity: SessionIdentity | None = None
        self._activation_patch_bytes: bytes | None = None
        self._tail_phase = ""
        self._bound_tail_grants: set[str] = set()
        if session_grant is not None:
            self._bound_tail_grants.add(
                hashlib.sha256(session_grant.encode()).hexdigest()
            )

    def evidence_producer(self) -> str:
        if self.app_slug is None or self._evidence_author_id is None:
            raise ReviewInputError(
                "partition authority requires exact configured Bot numeric ownership"
            )
        return f"github-bot:{self._evidence_author_id}"

    def _history_load(
        self,
        identity: SessionIdentity,
        *,
        max_scan_pages: int | None,
        now: datetime | None,
    ) -> tuple[SessionRecord | None, object]:
        if max_scan_pages is None:
            raise ReviewInputError("history reader requires sealed finite scan scope")
        self._validate_tail_scan_pages(max_scan_pages)
        if self._activation_ticket is not None or self._history_reading:
            raise ReviewInputError("history association cannot consume activation work")
        previous = self._tail_scan_pages
        self._history_reading, self._tail_scan_pages = True, max_scan_pages
        try:
            _, record = self._discover(identity, now=now)
            if record is None:
                raise ReviewInputError("history current root is missing")
            # _discover already authenticated and decoded the complete envelope.
            from ...session import _history_cached

            payload = _history_cached(self, record)
            if payload is None:
                raise ReviewInputError("history current envelope is not indexed")
            return record, payload["envelope"]
        finally:
            self._history_reading, self._tail_scan_pages = False, previous

    def _history_head(
        self, identity: SessionIdentity, binding: Mapping[str, object]
    ) -> None:
        # This read fence is required even without a mutation broker/grant.
        path = self.http.repository_path(
            identity.repository, f"/pulls/{identity.pull_request}"
        )
        status, payload = self._request("GET", path)
        if (
            status != 200
            or not isinstance(payload, dict)
            or not isinstance(payload.get("head"), dict)
            or payload["head"].get("sha") != binding["head_sha"]
        ):
            raise ReviewInputError("history trusted current PR head differs")

    def associate_assessment_history(
        self,
        identity: SessionIdentity,
        *,
        expected_binding: Mapping[str, object],
        expected_root_sha256: str,
        expected_generation: int,
        max_scan_pages: int,
        now: datetime | None = None,
    ) -> HistoryAssociation:
        return associate_history(
            self,
            identity,
            expected_binding=expected_binding,
            expected_root_sha256=expected_root_sha256,
            expected_generation=expected_generation,
            max_scan_pages=max_scan_pages,
            now=now,
        )

    def read_associated_history(
        self,
        proof: HistoryAssociation,
        *,
        source_digest: str,
        operation_id: str,
        now: datetime | None = None,
    ) -> object:
        return read_associated_history(
            self, proof, source_digest=source_digest, operation_id=operation_id, now=now
        )

    def _part_owner(self, item: object, identity: SessionIdentity) -> dict[str, object]:
        if not isinstance(item, dict):
            raise ReviewInputError("GitHub partition object is invalid")
        author = item.get("user")
        expected_issue = f"{self.http.api_url}/repos/{identity.repository}/issues/{identity.pull_request}"
        if (
            not isinstance(author, dict)
            or author.get("type") != "Bot"
            or self.app_slug is None
            or not isinstance(author.get("login"), str)
            or author["login"].casefold() != self.app_slug.casefold()
            or isinstance(author.get("id"), bool)
            or not isinstance(author.get("id"), int)
            or author.get("id") != self._evidence_author_id
            or item.get("issue_url") != expected_issue
        ):
            raise ReviewInputError(
                "GitHub partition owner or repository/PR association does not match"
            )
        return item

    def _read_part(
        self, identity: SessionIdentity, storage_id: str
    ) -> AuthenticatedPart:
        if (
            self._activation_ticket is not None
            and identity != self._activation_identity
        ):
            raise ReviewInputError("activation part identity changed")
        if not re.fullmatch(r"[1-9][0-9]{0,18}", storage_id):
            raise ReviewInputError("GitHub partition identity is invalid")
        path = self.http.repository_path(
            identity.repository, f"/issues/comments/{storage_id}"
        )
        status, payload = self._request("GET", path)
        if status != 200:
            raise ReviewInputError("GitHub partition is missing")
        item = self._part_owner(payload, identity)
        if type(item.get("id")) is not int or item.get("id") != int(storage_id):
            raise ReviewInputError("GitHub partition identity does not match")
        body = item.get("body")
        if (
            not isinstance(body, str)
            or len(body.encode()) > MAX_PART_BYTES
            or not body.startswith("ReviewSensei immutable evidence part v1\n```json\n")
            or not body.endswith("\n```\n<!-- reviewsensei:evidence-part:v1 -->")
        ):
            raise ReviewInputError("GitHub partition framing is invalid")
        raw = body.split("\n```json\n", 1)[1].rsplit("\n```\n", 1)[0]
        try:
            document = json.loads(raw)
            if canonical_bytes(document).decode() != raw:
                raise ValueError("noncanonical part")
        except (ValueError, UnicodeError) as exc:
            raise ReviewInputError("GitHub partition JSON is invalid") from exc
        return AuthenticatedPart(document, self.evidence_producer())

    def read_evidence(
        self,
        identity: SessionIdentity,
        manifest: dict[str, object],
        *,
        expected_binding: Mapping[str, object],
    ) -> object:
        if (
            expected_binding.get("repository") != identity.repository
            or expected_binding.get("pull_request") != identity.pull_request
            or expected_binding.get("repository_id") != identity.repository_id
            or expected_binding.get("producer") != self.evidence_producer()
        ):
            raise ReviewInputError("GitHub partition binding does not match")
        if self.evidence_budget is None:
            raise ReviewInputError(
                "partition reader requires shared metadata/read budget"
            )
        return read_partitioned_evidence(
            manifest,
            reader=lambda storage_id: self._read_part(identity, storage_id),
            expected_binding=expected_binding,
            budget=self.evidence_budget,
        )

    def stage_evidence(
        self,
        identity: SessionIdentity,
        *,
        binding: dict[str, object],
        document: object,
        item_count: int,
        max_manifest_bytes: int,
    ) -> dict[str, object]:
        if (
            not self.enable_partition_writes
            or not self._mutation_active
            or self.evidence_budget is None
        ):
            raise ReviewInputError(
                "GitHub partition staging requires enabled conditional mutation and shared budget"
            )
        if (
            binding.get("repository") != identity.repository
            or binding.get("pull_request") != identity.pull_request
            or binding.get("repository_id") != identity.repository_id
            or binding.get("producer") != self.evidence_producer()
        ):
            raise ReviewInputError("GitHub partition binding does not match")
        items = self._bounded_comments(identity)
        existing: dict[str, str] = {}
        stored_bytes = 0
        stored_count = 0
        for item in items:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("body"), str)
                or not item["body"].endswith("<!-- reviewsensei:evidence-part:v1 -->")
            ):
                continue
            author = item.get("user")
            if (
                not isinstance(author, dict)
                or author.get("id") != self._evidence_author_id
                or author.get("type") != "Bot"
            ):
                continue
            self._part_owner(item, identity)
            body = item["body"]
            stored_bytes += len(body.encode())
            stored_count += 1
            existing[body] = str(item["id"])
        if stored_count > MAX_STORED_PARTS or stored_bytes > MAX_STORED_PART_BYTES:
            raise ReviewInputError("GitHub partition retention capacity exceeded")
        prospective_parts = partition_evidence(
            document, binding=binding, item_count=item_count
        )
        prospective_bodies = [
            "ReviewSensei immutable evidence part v1\n```json\n"
            + canonical_bytes(part).decode()
            + "\n```\n<!-- reviewsensei:evidence-part:v1 -->"
            for part in prospective_parts
        ]
        missing_bodies = [body for body in prospective_bodies if body not in existing]
        if (
            self._tail_scan_pages is not None
            and len(items) + len(missing_bodies) >= 5 * self._tail_scan_pages
        ):
            raise ReviewInputError(
                "activation future metadata exceeds sealed page bound"
            )
        if (
            stored_count + len(missing_bodies) > MAX_STORED_PARTS
            or stored_bytes + sum(len(body.encode()) for body in missing_bodies)
            > MAX_STORED_PART_BYTES
        ):
            raise ReviewInputError(
                "GitHub whole retention capacity exceeded before staging"
            )

        def write(part: dict[str, object]) -> str:
            body = (
                "ReviewSensei immutable evidence part v1\n```json\n"
                + canonical_bytes(part).decode()
                + "\n```\n<!-- reviewsensei:evidence-part:v1 -->"
            )
            if body in existing:
                return existing[body]
            if (
                len(existing) >= MAX_STORED_PARTS
                or sum(len(old.encode()) for old in existing) + len(body.encode())
                > MAX_STORED_PART_BYTES
                or len(body.encode()) > MAX_PART_BYTES
            ):
                raise ReviewInputError("GitHub partition retention capacity exceeded")
            self._verify_live_head(identity)
            status, payload = self._request(
                "POST", self._comments_path(identity), body={"body": body}
            )
            if (
                status != 201
                or not isinstance(payload, dict)
                or isinstance(payload.get("id"), bool)
                or not isinstance(payload.get("id"), int)
                or payload["id"] < 1
            ):
                raise GitHubPublicationTransientError(
                    "GitHub partition create not confirmed; resume must rediscover"
                )
            self._part_owner(payload, identity)
            storage_id = str(payload["id"])
            existing[body] = storage_id
            return storage_id

        return stage_partitioned_evidence(
            document,
            binding=binding,
            item_count=item_count,
            max_manifest_bytes=max_manifest_bytes,
            writer=write,
            reader=lambda storage_id: self._read_part(identity, storage_id),
            budget=self.evidence_budget,
            preflight_calls=(
                len(missing_bodies) * (2 if self._broker is not None else 1)
                + len(prospective_parts)
            )
            if self._tail_preparing
            else None,
        )

    def _bounded_comments(self, identity: SessionIdentity) -> list[Any]:
        assert self.evidence_budget is not None
        items: list[Any] = []
        # Five maximum-size comments fit the retained 512 KiB transport bound.
        page_limit = self._tail_scan_pages if self._tail_scan_pages is not None else 200
        for page in range(1, page_limit + 1):
            status, payload = self._request(
                "GET", f"{self._comments_path(identity)}?per_page=5&page={page}"
            )
            if status != 200 or not isinstance(payload, list) or len(payload) > 5:
                raise ReviewInputError("partition metadata enumeration is unavailable")
            items.extend(payload)
            if len(payload) < 5:
                return items
        raise ReviewInputError("partition metadata enumeration exceeds item bound")

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

        if self._tail_authorizing:
            if (
                not isinstance(self._broker, BrokerClient)
                or self.evidence_budget is None
            ):
                raise NonResumableActivationError(
                    "prepaid hosted activation requires the consuming broker client"
                )
            callback = self._broker.before_request
            if callback is not None:
                if (
                    getattr(callback, "__self__", None) is not self.evidence_budget
                    or getattr(callback, "__func__", None)
                    is not EvidenceReadBudget.consume
                ):
                    raise ReviewInputError(
                        "broker accounting is not the original budget"
                    )
                # The actual client's callback charges its physical dispatch
                # and clamps timeout. Do not charge that request twice here.
                assert self.evidence_budget is not None
                self.evidence_budget.check()
            else:
                remaining = self.evidence_budget._remaining_seconds()
                if self._broker.timeout > remaining:
                    raise ReviewInputError(
                        "broker grant transport exceeds original remaining deadline"
                    )
                self.evidence_budget.consume()
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
        if self._tail_authorizing and self.evidence_budget is not None:
            self.evidence_budget.check()

    def _verify_live_head(self, identity: SessionIdentity) -> None:
        """Re-read the PR head immediately before a grant-authorized write.

        The broker binds a grant to the head observed at issuance, but the
        short-lived grant can outlive that commit if the PR advances. GitHub
        has no conditional comment-write primitive, so this bounded preflight
        closes the stale-grant window as tightly as the REST API permits.
        """

        if self._broker is None:
            return
        assert self._head_sha is not None
        path = self.http.repository_path(
            identity.repository, f"/pulls/{identity.pull_request}"
        )
        status, payload = self._request("GET", path)
        if status == 429 or status >= 500:
            raise GitHubPublicationTransientError(
                "session head preflight failed temporarily"
            )
        if status < 200 or status >= 300 or not isinstance(payload, dict):
            raise GitHubPublicationError("session head preflight failed")
        head = payload.get("head")
        if not isinstance(head, dict) or not isinstance(head.get("sha"), str):
            raise GitHubPublicationError("session head preflight was invalid")
        if head["sha"] != self._head_sha:
            raise ReviewInputError("session grant head is stale")

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
    ) -> tuple[int, dict[str, Any] | list[Any] | None]:
        try:
            if self.evidence_budget is not None:
                if self._activation_ticket is not None:
                    if self._activation_identity is None:
                        raise ReviewInputError(
                            "activation root identity is unavailable"
                        )
                    if body is not None and (
                        method != "PATCH"
                        or canonical_bytes(body) != self._activation_patch_bytes
                    ):
                        raise ReviewInputError(
                            "activation PATCH differs from sealed root"
                        )
                    fence = method == "PATCH" or (
                        self._tail_phase in {"reload", "readback"}
                        and path
                        == f"{self._comments_path(self._activation_identity)}?per_page=5&page=1"
                    )
                    remaining = self._activation_ticket.consume(
                        f"{method} {path}", fence=fence
                    )
                else:
                    remaining = self.evidence_budget.consume()
                response = self.http.request(
                    method, path, token=self.token, body=body, timeout_seconds=remaining
                )
                self.evidence_budget.check()
                return response
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
            items = (
                self._bounded_comments(identity)
                if self.evidence_budget is not None
                else self.http.paginate(
                    path=self._comments_path(identity), token=self.token
                )
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
            if not isinstance(body, str) or not _marker_shaped(body):
                continue
            if not _within_session_comment_limit(body):
                # An unreadable newer writer's authority is established state,
                # not a missing ledger. Never initialize a second session.
                raise SessionLoadError(
                    SessionLoadReason.INTEGRITY_FAILED,
                    "session comment exceeds the supported reader bound",
                )
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
            author_id = author.get("id") if isinstance(author, dict) else None
            if (
                self._tail_authorizing
                or self._activation_ticket is not None
                or self._history_reading
            ):
                if (
                    isinstance(author_id, bool)
                    or not isinstance(author_id, int)
                    or author_id <= 0
                    or self._evidence_author_id is not None
                    and author_id != self._evidence_author_id
                ):
                    raise ReviewInputError("activation root Bot numeric owner changed")
                self._evidence_author_id = author_id
                self._part_owner(item, identity)
            if (
                not isinstance(author_id, bool)
                and isinstance(author_id, int)
                and author_id > 0
            ):
                self._evidence_author_id = author_id
        if len(found) > 1:
            raise SessionLoadError(
                SessionLoadReason.CONFLICT, "multiple session comments are present"
            )
        if not found:
            return None, None
        record = found[0][1]
        stored_baseline = (
            record.convergence_history.get("baseline")
            if isinstance(record.convergence_history, Mapping)
            else None
        )
        if (
            record.assessment_queue is not None
            or isinstance(stored_baseline, dict)
            and stored_baseline.get("encoding") == PARTITION_ENCODING
        ):
            if self.evidence_budget is None:
                raise SessionLoadError(
                    SessionLoadReason.INTEGRITY_FAILED,
                    "partition session requires shared read budget",
                )
            try:
                self._part_owner(
                    next(
                        item
                        for item in items
                        if isinstance(item, dict) and item.get("id") == found[0][0]
                    ),
                    identity,
                )
                read_session_baseline(self, record)
                read_session_assessment_queue(self, record)
            except ReviewInputError as exc:
                raise SessionLoadError(
                    SessionLoadReason.INTEGRITY_FAILED,
                    "partition authority is unreadable",
                ) from exc
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
        self._verify_live_head(identity)
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
        _history_legacy_write_guard(self, record)
        self._mutation_active = True
        try:
            updated = mutate(record)
            _validate_queue_retention(record, updated)
            read_session_baseline(self, updated)
            read_session_assessment_queue(self, updated)
            _history_legacy_write_guard(self, updated)
        finally:
            self._mutation_active = False
        if self.evidence_budget is not None:
            preactivation_id, preactivation = self._discover(identity, now=now)
            if (
                preactivation_id != comment_id
                or preactivation is None
                or preactivation.record_sha256 != record.record_sha256
                or preactivation.generation != record.generation
            ):
                raise ReviewInputError("session generation conflict before activation")
        self._verify_live_head(identity)
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

    def reserve_for_tail(
        self,
        identity: SessionIdentity,
        *,
        operation_binding: Mapping[str, object],
        slot: str,
        reservation_id: str,
        expected_generation: int,
        max_scan_pages: int,
        head_sha: str | None = None,
        now: datetime | None = None,
    ) -> SessionRecord:
        if not self.enable_partition_writes or self.evidence_budget is None:
            raise ReviewInputError(
                "prepaid activation writers or original budget are unavailable"
            )
        self._validate_tail_scan_pages(max_scan_pages)
        budget = self.evidence_budget
        scope = _tail_attempt_scope(identity, reservation_id, operation_binding)
        if (
            budget.restored
            or scope in budget._live_attempts
            or scope in budget._failed_attempts
        ):
            raise NonResumableActivationError(
                "original activation attempt cannot be restarted"
            )

        def admit(record: SessionRecord) -> SessionRecord:
            if (
                record.reservation_id is not None
                or record.last_committed_reservation_id == reservation_id
            ):
                raise NonResumableActivationError(
                    "loaded reservation is not a live original attempt proof"
                )
            if self._session_attestation is not None:
                self._validate_feedback_tail_attestation(
                    self._session_attestation,
                    record,
                    operation=_tail_operation_binding(operation_binding),
                    reservation_id=reservation_id,
                )
            return self._own_reservation(
                record,
                mutate_reserved(
                    record,
                    slot=slot,
                    reservation_id=reservation_id,
                    expected_generation=expected_generation,
                    head_sha=head_sha,
                    now=now,
                ),
            )

        self._tail_authorizing, self._tail_scan_pages = True, max_scan_pages
        try:
            reserved = self.replace(identity, admit, now=now)
            budget._remember_live_attempt(scope, reserved.record_sha256, owner=self)
            return reserved
        except BaseException:
            budget._burn_live_attempt(scope)
            raise
        finally:
            self._tail_authorizing, self._tail_scan_pages = False, None

    @staticmethod
    def _validate_tail_scan_pages(value: int) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= 60
        ):
            raise ReviewInputError("activation scan page bound is invalid")

    def _validate_feedback_tail_attestation(
        self,
        attestation: Mapping[str, object],
        record: SessionRecord,
        *,
        operation: Mapping[str, object],
        reservation_id: str,
    ) -> None:
        """Bind parsed v2 references to retained live authority, never mint it."""
        if attestation.get("version") != 2:
            return
        assert self.evidence_budget is not None
        assert isinstance(self._broker, BrokerClient)
        try:
            parsed = self._broker._validated_attestation_grant(attestation)
            original = self._broker._validated_attestation_grant(
                self._session_attestation
            )
        except GitHubBrokerClientError as exc:
            raise ReviewInputError(
                "feedback activation attestation is invalid"
            ) from exc
        if original.get("version") != 2:
            raise ReviewInputError("activation attestation version changed")
        mutation = cast(Mapping[str, Any], parsed["mutation"])
        previous = cast(Mapping[str, Any], original["mutation"])
        accounting = mutation["read_accounting"]
        active = (
            record.assessment_queue["active_operation"]
            if record.assessment_queue is not None
            else None
        )
        retained_calls = (
            cast(Mapping[str, Any], active)["read_accounting"]["calls"]
            if active is not None
            else 0
        )
        if (
            {key: mutation[key] for key in operation} != operation
            or mutation["reservation_id"] != reservation_id
            or mutation["root_digest"] != record.record_sha256
            or mutation["root_generation"] != record.generation
            or accounting["deadline_unix_ms"] != self.evidence_budget.wall_deadline_ms
            or not max(previous["read_accounting"]["calls"], retained_calls)
            <= accounting["calls"]
            <= self.evidence_budget.calls
        ):
            raise ReviewInputError(
                "feedback grant original operation, root or accounting differs"
            )

    def bind_tail_grant(
        self,
        identity: SessionIdentity,
        grant: BrokerSessionGrant,
        *,
        operation_binding: Mapping[str, object],
        attempt_reservation_id: str,
        max_scan_pages: int,
        now: datetime | None = None,
    ) -> None:
        """Install a fresh one-attempt grant on the original live ledger only.

        This does not issue authority or verify/consume the grant. The next
        mutation verifies it through the actual consuming BrokerClient. Current
        v1 grant claims bind hosted command scope; they do not prove durable
        original operation accounting or permit crash/restart continuation.
        """
        budget = self.evidence_budget
        if not self.enable_partition_writes or budget is None:
            raise ReviewInputError(
                "prepaid activation writers or budget are unavailable"
            )
        self._validate_tail_scan_pages(max_scan_pages)
        operation = _tail_operation_binding(operation_binding)
        scope = _tail_attempt_scope(identity, attempt_reservation_id, operation)
        if (
            budget.restored
            or budget._live_attempt_owners.get(scope) is not self
            or scope in budget._failed_attempts
        ):
            raise NonResumableActivationError(
                "original live ledger proof is unavailable"
            )
        if self._activation_ticket is not None or budget._tail_ticket is not None:
            raise ReviewInputError("cannot rebind a pending activation tail")
        try:
            if not isinstance(self._broker, BrokerClient) or not isinstance(
                grant, BrokerSessionGrant
            ):
                raise ReviewInputError(
                    "activation requires a broker-parsed session grant"
                )
            attestation = self._broker._validated_attestation_grant(grant.attestation)
            original = self._broker._validated_attestation_grant(
                self._session_attestation
            )
            # Feedback mutation fields change between checkpoints; complete
            # source/actor/workflow identity remains immutable. V1 preserves
            # its original same-command claim equality.
            excluded = {"issued_at"}
            if attestation.get("version") == original.get("version") == 2:
                excluded.add("mutation")
            if (
                {
                    key: value
                    for key, value in attestation.items()
                    if key not in excluded
                }
                != {
                    key: value for key, value in original.items() if key not in excluded
                }
                or attestation["repository"] != identity.repository
                or attestation["repository_id"] != identity.repository_id
                or attestation["pull_request"] != identity.pull_request
                or attestation["head_sha"] != self._head_sha
                or grant.state not in {"enrolled", "known"}
                or not isinstance(grant.token, str)
                or not grant.token.strip()
                or not isinstance(grant.grant, str)
                or re.fullmatch(r"[A-Za-z0-9_-]{43}", grant.grant) is None
            ):
                raise ReviewInputError("fresh activation grant scope differs")
            digest = hashlib.sha256(grant.grant.encode()).hexdigest()
            if digest in self._bound_tail_grants or len(self._bound_tail_grants) >= 64:
                raise ReviewInputError(
                    "activation grant was reused or registry exhausted"
                )
            self._tail_authorizing, self._tail_scan_pages = True, max_scan_pages
            comment_id, current = self._discover(identity, now=now)
            if (
                comment_id is None
                or current is None
                or current.expired(now=now)
                or current.reservation_id != attempt_reservation_id
            ):
                raise ReviewInputError("current activation reservation is unavailable")
            budget._require_live_attempt(scope, current.record_sha256, owner=self)
            self._validate_feedback_tail_attestation(
                attestation,
                current,
                operation=operation,
                reservation_id=attempt_reservation_id,
            )
            if current.assessment_queue is not None:
                _validate_tail_draft(
                    current,
                    current.evolve(generation=current.generation + 1, now=now),
                    operation=operation,
                    reservation_id=attempt_reservation_id,
                    budget=budget,
                )
            self._verify_live_head(identity)
            budget.check()
            self.token = grant.token
            self._session_grant = grant.grant
            self._session_attestation = attestation
            self._bound_tail_grants.add(digest)
        except GitHubBrokerClientError as exc:
            budget._burn_live_attempt(scope)
            raise ReviewInputError(
                "fresh activation grant attestation is invalid"
            ) from exc
        except BaseException:
            budget._burn_live_attempt(scope)
            raise
        finally:
            self._tail_authorizing, self._tail_scan_pages = False, None

    def replace_with_tail(
        self,
        identity: SessionIdentity,
        prepare: Callable[[SessionRecord], SessionRecord],
        *,
        operation_binding: Mapping[str, object],
        attempt_reservation_id: str,
        seal_accounting: Callable[[SessionRecord, Mapping[str, int]], SessionRecord],
        max_scan_pages: int,
        now: datetime | None = None,
    ) -> SessionRecord:
        """Each checkpoint consumes a fresh grant; prepay all sealed root work."""
        if not self.enable_partition_writes or self.evidence_budget is None:
            raise ReviewInputError(
                "prepaid activation writers or original budget are unavailable"
            )
        self._validate_tail_scan_pages(max_scan_pages)
        budget = self.evidence_budget
        operation = _tail_operation_binding(operation_binding)
        scope = _tail_attempt_scope(identity, attempt_reservation_id, operation)
        if (
            budget.restored
            or scope not in budget._live_attempts
            or budget._live_attempt_owners.get(scope) is not self
            or scope in budget._failed_attempts
        ):
            raise NonResumableActivationError(
                "original activation attempt witness is unavailable"
            )
        ticket: EvidenceTailTicket | None = None
        self._tail_authorizing, self._tail_scan_pages = True, max_scan_pages
        try:
            self._verify_mutation_grant(identity)
            comment_id, before = self._discover(identity, now=now)
            if comment_id is None or before is None or before.expired(now=now):
                raise ReviewInputError(
                    "prepaid activation current authority is unavailable"
                )
            budget._require_live_attempt(scope, before.record_sha256, owner=self)
            if self._session_attestation is not None:
                self._validate_feedback_tail_attestation(
                    self._session_attestation,
                    before,
                    operation=operation,
                    reservation_id=attempt_reservation_id,
                )
            latest_id, latest = self._discover(identity, now=now)
            if (
                latest_id != comment_id
                or latest is None
                or latest.record_sha256 != before.record_sha256
            ):
                raise ReviewInputError("session generation conflict before preparation")
            self._mutation_active = self._tail_preparing = True
            draft = prepare(before)
            self._mutation_active = self._tail_preparing = False
            _validate_tail_draft(
                before,
                draft,
                operation=operation,
                reservation_id=attempt_reservation_id,
                budget=budget,
            )
            if self.enable_history_graph:
                read_session_assessment_queue(self, draft)
                _history_preserve_sources(self, before, draft)
            old_ids = _history_extra_ids(self, before)
            new_ids = _history_extra_ids(self, draft)
            if self._session_grant is None:
                raise ReviewInputError("activation grant is unavailable")
            grant_digest = hashlib.sha256(self._session_grant.encode()).hexdigest()
            attested_head = self._head_sha

            def plan_scope(record: SessionRecord) -> str:
                if (
                    self._session_grant is None
                    or self._head_sha != attested_head
                    or hashlib.sha256(self._session_grant.encode()).hexdigest()
                    != grant_digest
                ):
                    raise ReviewInputError("activation grant or head scope changed")
                return _tail_plan_scope(
                    "github",
                    before,
                    record,
                    operation=operation,
                    budget=budget,
                    scan_pages=max_scan_pages,
                    head_sha=self._head_sha,
                    root_id=comment_id,
                    grant_sha256=grant_digest,
                    old_history_ids=old_ids,
                    new_history_ids=new_ids,
                )

            def part_steps(record: SessionRecord) -> tuple[TailDispatch, ...]:
                return tuple(
                    TailDispatch(
                        f"GET {self.http.repository_path(identity.repository, f'/issues/comments/{part_id}')}"
                    )
                    for part_id in _tail_part_ids(
                        record,
                        history_ids=new_ids
                        if record is draft
                        or record.record_sha256 != before.record_sha256
                        else old_ids,
                    )
                )

            def scan_steps() -> tuple[TailDispatch, ...]:
                return tuple(
                    TailDispatch(
                        f"GET {self._comments_path(identity)}?per_page=5&page={page}",
                        fence=page == 1,
                        optional=page > 1,
                    )
                    for page in range(1, max_scan_pages + 1)
                )

            path = self.http.repository_path(
                identity.repository, f"/issues/comments/{comment_id}"
            )
            head_steps = (
                TailDispatch(
                    f"GET {self.http.repository_path(identity.repository, f'/pulls/{identity.pull_request}')}"
                ),
            )
            steps = (
                part_steps(draft)
                + scan_steps()
                + part_steps(before)
                + head_steps
                + (TailDispatch(f"PATCH {path}", fence=True),)
                + scan_steps()
                + part_steps(draft)
            )
            plan = ActivationTailPlan("github", plan_scope(draft), steps)
            ticket = budget.reserve_tail(plan)
            sealed = _seal_tail_accounting(draft, seal_accounting, budget)
            _validate_queue_retention(before, sealed)
            patch_body: dict[str, object] = {
                "body": render_session_comment(
                    repository_id=self._require_identity(identity),
                    pull_request=identity.pull_request,
                    record=sealed,
                )
            }
            self._activation_patch_bytes = canonical_bytes(patch_body)
            ticket.seal(sealed.record_sha256)
            ticket.start(
                scope_sha256=plan_scope(sealed), root_sha256=sealed.record_sha256
            )
            self._activation_ticket, self._activation_identity = ticket, identity
            self._tail_phase = "validate"
            read_session_baseline(self, sealed)
            read_session_assessment_queue(self, sealed)
            self._tail_phase = "reload"
            current_id, current = self._discover(identity, now=now)
            if (
                current_id != comment_id
                or current is None
                or current.record_sha256 != before.record_sha256
            ):
                raise ReviewInputError(
                    "session generation conflict before prepaid activation"
                )
            self._tail_phase = "activate"
            self._verify_live_head(identity)
            status, payload = self._request("PATCH", path, body=patch_body)
            if status != 200 or not isinstance(payload, dict):
                raise GitHubPublicationTransientError(
                    "prepaid root update is ambiguous"
                )
            self._part_owner(payload, identity)
            verified = parse_session_comment(payload.get("body"), identity=identity)
            if (
                payload.get("id") != comment_id
                or verified is None
                or verified.record_sha256 != sealed.record_sha256
            ):
                raise ReviewInputError("prepaid root update lost")
            self._tail_phase = "readback"
            readback_id, readback = self._discover(identity, now=now)
            if (
                readback_id != comment_id
                or readback is None
                or readback.record_sha256 != sealed.record_sha256
            ):
                raise ReviewInputError(
                    "prepaid activation readback is ambiguous or conflicting"
                )
            ticket.finish()
            budget._advance_live_attempt(scope, sealed.record_sha256)
            return readback
        except BaseException:
            budget._burn_live_attempt(scope)
            raise
        finally:
            if ticket is not None:
                ticket.abort()
            self._activation_ticket = self._activation_identity = None
            self._activation_patch_bytes = None
            self._tail_authorizing = self._tail_preparing = self._mutation_active = (
                False
            )
            self._tail_scan_pages, self._tail_phase = None, ""

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
        if record.assessment_queue is not None:
            raise ReviewInputError(
                "assessment queue requires retained tombstone authority before re-enrollment"
            )
        repository_id = self._require_identity(identity)
        replacement = SessionRecord.create(identity, now=now)
        self._verify_live_head(identity)
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

    def _own_reservation(
        self, previous: SessionRecord, reserved: SessionRecord
    ) -> SessionRecord:
        if (
            reserved.reservation_id is None
            or previous.reservation_id is not None
            or self._reservation_owner is None
        ):
            return reserved
        if reserved.failed_attempts_head_sha != self._reservation_owner.get("head_sha"):
            raise ReviewInputError(
                "broker reservation owner head does not match admission"
            )
        return reserved.evolve(
            reservation_owner=self._reservation_owner,
            now=datetime.fromisoformat(reserved.updated_at.replace("Z", "+00:00")),
        )

    def continue_after_abandoned_analysis(
        self, identity: SessionIdentity, *, now: datetime | None = None
    ) -> SessionRecord:
        """One broker grant, one CAS replacement, live Actions proof twice."""
        import hashlib

        from ...session import MAX_FAILED_ATTEMPTS, _failed_attempt_scope
        from .reservation_recovery import ActionsReservationEvidence

        if (
            self._broker is None
            or self._session_attestation is None
            or (
                self._actions_read_token is None
                and self._actions_token_provider is None
            )
        ):
            raise ReviewInputError(
                "reservation recovery requires an authenticated maintainer grant and Actions reads"
            )
        if self._session_attestation.get("operation") != "command":
            raise ReviewInputError("reservation recovery requires a maintainer command")
        loaded = self.load(identity, now=now)
        if loaded.status != "ok" or loaded.record is None:
            raise ReviewInputError(f"session ledger load failed: {loaded.status}")
        expected = loaded.record
        if expected.reservation_id is None:
            return expected
        actions_token = self._actions_read_token
        if actions_token is None and self._actions_token_provider is not None:
            actions_token = self._actions_token_provider()
        if not isinstance(actions_token, str) or not actions_token.strip():
            raise ReviewInputError("reservation recovery Actions token is unavailable")
        evidence = ActionsReservationEvidence(
            self.http,
            token=actions_token,
            command_run_id=str(self._session_attestation["run_id"]),
        )
        proof = evidence.verify(identity, expected)

        def recover(current: SessionRecord) -> SessionRecord:
            if (
                current.record_sha256 != expected.record_sha256
                or current.generation != expected.generation
                or current.reservation_id != expected.reservation_id
            ):
                raise ReviewInputError(
                    "abandoned reservation ownership or generation mismatch"
                )
            if evidence.verify(identity, current) != proof:
                raise ReviewInputError("reservation recovery run proof is stale")
            _, latest = self._discover(identity, now=now)
            if latest is None or latest.record_sha256 != expected.record_sha256:
                raise ReviewInputError(
                    "abandoned reservation ownership or generation mismatch"
                )
            # Charge the abandoned head, even when the command addresses a
            # newer live head. Recovery must not spend that new head's budget.
            owner_head = (
                str(current.reservation_owner["head_sha"])
                if current.reservation_owner is not None
                else current.transaction.head_sha
                if current.transaction is not None
                else None
            )
            scope = _failed_attempt_scope(current, owner_head)
            attempts = int(scope.get("failed_attempts", current.failed_attempts))
            scope["failed_attempts"] = min(attempts + 1, MAX_FAILED_ATTEMPTS)
            return current.evolve(
                now=now,
                generation=current.generation + 1,
                reservation_id=None,
                reserved_slot=None,
                transaction=None,
                last_committed_reservation_id=hashlib.sha256(
                    f"{current.reservation_id}|failed-attempt".encode()
                ).hexdigest(),
                operator_paused=False,
                **scope,
            )

        return self.replace(identity, recover, now=now)

    def reserve(
        self,
        identity: SessionIdentity,
        *,
        slot: str,
        reservation_id: str,
        expected_generation: int,
        head_sha: str | None = None,
        now: datetime | None = None,
    ) -> SessionRecord:
        return self.replace(
            identity,
            lambda record: self._own_reservation(
                record,
                mutate_reserved(
                    record,
                    slot=slot,
                    reservation_id=reservation_id,
                    expected_generation=expected_generation,
                    head_sha=head_sha,
                    now=now,
                ),
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

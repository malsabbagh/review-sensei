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

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Sequence, cast
from urllib.parse import quote

from review_sensei.bounded_evidence import (
    INLINE_ENCODING,
    MAX_MANIFEST_BYTES,
    MAX_PART_BYTES,
    MAX_PART_READS,
    MAX_PARTITION_BYTES,
    MAX_PARTITION_DECODED_BYTES,
    MAX_PARTS,
    MAX_STORED_PART_BYTES,
    MAX_STORED_PARTS,
    PACKED_CHUNK_BYTES,
    PART_ENCODING,
    PART_FRAMING_RESERVE,
    PARTITION_ENCODING,
    ActivationTailPlan,
    AuthenticatedPart,
    EvidenceReadBudget,
    EvidenceTailTicket,
    NonResumableActivationError,
    TailDispatch,
    _operation_witness_matches,
    canonical_bytes,
    evidence_manifest,
    inline_manifest,
    partition_evidence,
    read_inline_evidence,
    read_partitioned_evidence,
    stage_partitioned_evidence,
)
from review_sensei.errors import ReviewInputError
from review_sensei.history_association import (
    HistoryAssociation,
    associate_history,
    read_associated_history,
)
from review_sensei.operation_witness import OperationHandle
from review_sensei.rescan import AuthorizedRescan
from review_sensei.session import (
    MAX_SESSION_COMMENT_BYTES,
    SessionIdentity,
    SessionLoadError,
    SessionLoadReason,
    SessionLoadResult,
    SessionRecord,
    _disposition_page_ids,
    _history_extra_ids,
    _history_legacy_write_guard,
    _history_preserve_sources,
    _remember_disposition_pages,
    _seal_tail_accounting,
    _tail_attempt_scope,
    _tail_operation_binding,
    _tail_part_ids,
    _tail_plan_scope,
    _validate_queue_retention,
    _validate_tail_draft,
    disposition_snapshot_capacity,
    load_session_status,
    mutate_abort,
    mutate_commit,
    mutate_reserved,
    read_session_assessment_queue,
    read_session_baseline,
    read_session_dispositions,
)

from .broker_client import BrokerClient, BrokerSessionGrant
from .errors import (
    GitHubBrokerClientError,
    GitHubHTTPError,
    GitHubHTTPPaginationLimitError,
    GitHubHTTPResponseTooLargeError,
    GitHubHTTPTransientError,
    GitHubPublicationError,
    GitHubPublicationTransientError,
)
from .http import MAX_PAGINATION_ITEMS, GitHubHttp

_COMMENT_PAGE_SIZES = (100, 20, 10, 5, 1)
_FENCE_PAGE_SIZE = 100
_SINCE_MARGIN = timedelta(seconds=2)
_UPDATED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_PART_PREFIX = "ReviewSensei immutable evidence part v1\n"


def retained_carrier_body(body: object) -> bool:
    """Count native-union manifests in the same immutable storage ceiling."""
    return isinstance(body, str) and body.endswith(
        (
            "<!-- reviewsensei:evidence-part:v1 -->",
            "<!-- reviewsensei:native-manifest:v1 -->",
        )
    )


def _since_margin(value: str) -> str | None:
    """Server timestamp of a read, moved back so a same-second write is included."""

    if _UPDATED_AT.fullmatch(value) is None:
        return None
    parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    return (parsed - _SINCE_MARGIN).strftime("%Y-%m-%dT%H:%M:%SZ")


SESSION_MARKER_PREFIX = "<!-- reviewsensei:session:v1"
SESSION_MARKER_RE = re.compile(
    r"(?m)^<!-- reviewsensei:session:v(?P<marker_version>[12]) repo=(?P<repository_id>[1-9][0-9]*) "
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
_CONSUMED_GRANT_LINE = re.compile(
    r"(?m)^<!-- reviewsensei:consumed-grant:v1 sha256=([a-f0-9]{64}) -->$"
)


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
    prefix = (
        SESSION_MARKER_PREFIX
        if record.quarantine_lineage is None
        else "<!-- reviewsensei:session:v2"
    )
    return (
        f"{prefix} repo={repository_id} pr={pull_request} "
        f"gen={record.generation} digest={record.record_sha256} -->"
    )


def render_session_comment(
    *,
    repository_id: int,
    pull_request: int,
    record: SessionRecord,
    consumed_grants: tuple[str, ...] = (),
) -> str:
    document = json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":"))
    grant_lines = "".join(
        f"<!-- reviewsensei:consumed-grant:v1 sha256={digest} -->\n"
        for digest in consumed_grants
    )
    body = (
        f"{grant_lines}{_SESSION_INTRO}\n\n```json\n{document}\n```\n\n"
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
            SessionLoadReason.INTEGRITY_FAILED,
            f"session comment record is invalid: {exc}",
        ) from exc
    if (marker["marker_version"] == "2") != (record.quarantine_lineage is not None):
        return None
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


def _immutable_part_body(part: Mapping[str, object]) -> str:
    return (
        "ReviewSensei immutable evidence part v1\n```json\n"
        + canonical_bytes(part).decode()
        + "\n```\n<!-- reviewsensei:evidence-part:v1 -->"
    )


@dataclass(frozen=True)
class _DispositionIndexBounds:
    inline: bool
    decoded_bytes: int
    part_bytes: tuple[int, ...]
    body_bytes: tuple[int, ...]
    wire_bytes: tuple[int, ...]
    manifest_bytes: int


def _disposition_index_bounds(
    document: dict[str, Any],
    *,
    binding: dict[str, object],
    item_count: int,
    max_manifest_bytes: int,
) -> _DispositionIndexBounds:
    """Size only: input unknown IDs use their full nineteen-character width.

    No prospective identity or compressed placeholder document becomes evidence.
    zlib's general deflateBound covers non-default compiled memory settings:
    https://github.com/madler/zlib/blob/v1.2.12/deflate.c#L627-L697
    Its normal-wrapper bound is L + ceil(L/8) + ceil(L/64) + 11. The current
    partition codec uses no dictionary and its fixed 23,500-byte packed chunks.
    """
    decoded = len(canonical_bytes(document))
    if not 0 < decoded <= MAX_PARTITION_DECODED_BYTES:
        raise ReviewInputError("disposition index exceeds decoded capacity")
    inline = inline_manifest(document, binding=binding, item_count=item_count)
    if inline is not None:
        inline_size = len(canonical_bytes(inline))
        if inline_size <= max_manifest_bytes:
            return _DispositionIndexBounds(True, decoded, (), (), (), inline_size)
    packed = decoded + (decoded + 7) // 8 + (decoded + 63) // 64 + 11
    count = (packed + PACKED_CHUNK_BYTES - 1) // PACKED_CHUNK_BYTES
    if count > MAX_PARTS:
        raise ReviewInputError("disposition index exceeds bounded part count")
    raw_sizes, bodies, wires = [], [], []
    for index in range(count):
        chunk = min(PACKED_CHUNK_BYTES, packed - index * PACKED_CHUNK_BYTES)
        # All non-data fields have fixed values/widths. Base64 adds no JSON
        # escaping. These skeletons measure size only and are never published.
        skeleton = {
            "encoding": PART_ENCODING,
            "binding_sha256": "0" * 64,
            "inventory_sha256": "0" * 64,
            "index": index,
            "count": count,
            "data": "",
        }
        payload_size = 4 * ((chunk + 2) // 3)
        raw_sizes.append(len(canonical_bytes(skeleton)) + payload_size)
        bodies.append(len(_immutable_part_body(skeleton).encode()) + payload_size)
        wires.append(
            len(canonical_bytes({"body": _immutable_part_body(skeleton)}))
            + payload_size
        )
    encoded = sum(raw_sizes) + count * PART_FRAMING_RESERVE
    if (
        any(size + PART_FRAMING_RESERVE > MAX_PART_BYTES for size in raw_sizes)
        or any(size > MAX_PART_BYTES for size in bodies)
        or encoded > MAX_PARTITION_BYTES
    ):
        raise ReviewInputError("disposition index exceeds framed capacity")
    # Literal x IDs are sizing tokens, never allocated or stored identities.
    manifest_size = len(
        canonical_bytes(
            {
                "encoding": PARTITION_ENCODING,
                "binding": binding,
                "item_count": item_count,
                "decoded_bytes": decoded,
                "sha256": "0" * 64,
                "encoded_bytes": encoded,
                "parts": [
                    {"storage_id": "x" * 19, "bytes": size, "sha256": "0" * 64}
                    for size in raw_sizes
                ],
            }
        )
    )
    if manifest_size > min(MAX_MANIFEST_BYTES, max_manifest_bytes):
        raise ReviewInputError("disposition index exceeds actual root allocation")
    return _DispositionIndexBounds(
        False, decoded, tuple(raw_sizes), tuple(bodies), tuple(wires), manifest_size
    )


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
        enable_disposition_snapshot_writes: bool = False,
    ) -> None:
        if not isinstance(http, GitHubHttp):
            raise ReviewInputError("GitHub session ledger requires GitHubHttp")
        if not isinstance(token, str) or not token.strip():
            raise ReviewInputError("GitHub session ledger token is empty")
        if app_slug is not None and (
            not isinstance(app_slug, str) or not app_slug.strip()
        ):
            raise ReviewInputError("GitHub session ledger app slug is invalid")
        if any(
            value is not None
            for value in (broker, session_grant, session_attestation, head_sha)
        ):
            raise ReviewInputError(
                "broker-bound phase ledgers are retired; use the current run-owned ledger"
            )
        self.http = http
        self.token = token
        # Populated only by the current authenticated broker startup. The
        # Actions event/env is never the operation journal's App authority.
        self.authenticated_app_id: int | None = None
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
        self._request_started = False
        self._analysis_phases: Any | None = None
        self._metadata_phase_closed = False
        self.evidence_budget = evidence_budget
        self.enable_partition_writes = enable_partition_writes
        self.enable_history_graph = enable_history_graph
        self.enable_disposition_snapshot_writes = enable_disposition_snapshot_writes
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
        self._observed_consumed_grants: set[str] = set()
        self._accepted_consumed_grant: str | None = None
        self._comments_by_id: dict[int, dict[str, Any]] = {}
        self._comment_order: list[int] = []
        self._expected_repository_id: int | None = None
        self._inventory_complete = False
        self._list_refresh = True
        self._list_anchor: str | None = None
        self._fence_anchor: str | None = None
        self._fence_page_size = _FENCE_PAGE_SIZE
        self._verified_parts: dict[str, AuthenticatedPart] = {}
        self._part_digests: dict[str, str] = {}
        self._reuse_listed_parts = False
        self._quarantine_origin_reader: Any | None = None
        self._quarantine_check_token_provider: Callable[[], str] | None = None
        self._quarantine_rollover_snapshot: Any | None = None
        self._quarantine_creation_slot: Any | None = None
        self._quarantine_policy_branch: str | None = None
        self._quarantine_full_discovery = False
        self._quarantine_required = False
        self._quarantine_capacity_cursor = False
        self._quarantine_native_tail_parts = 2
        self._quarantine_command_receipt_marker: str | None = None
        self._quarantine_native_comment_bounds: tuple[int, ...] | None = None
        self._quarantine_checkpoint_snapshot: Any = None
        self._quarantine_complete_read = False
        self._quarantine_authenticated: Any | None = None
        self._quarantine_decision_permit: Any | None = None
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
            from review_sensei.session import _history_cached

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

    def _owned_carrier_items(self, items, identity):
        """The shared exact carrier selector for planning and staging."""
        for item in items:
            if not isinstance(item, dict) or not retained_carrier_body(
                item.get("body")
            ):
                continue
            author = item.get("user")
            if (
                not isinstance(author, dict)
                or author.get("id") != self._evidence_author_id
                or author.get("type") != "Bot"
            ):
                continue
            if type(item.get("id")) is not int or not 1 <= item["id"] < 10**19:
                raise ReviewInputError("GitHub partition storage identity is invalid")
            yield self._part_owner(item, identity)

    def _part_is_listed(self, storage_id: str) -> bool:
        if not storage_id.isdigit():
            return False
        item = self._comments_by_id.get(int(storage_id))
        body = item.get("body") if isinstance(item, dict) else None
        return isinstance(body, str) and body.startswith(_PART_PREFIX)

    def _remember_part(
        self, storage_id: str, part: AuthenticatedPart
    ) -> AuthenticatedPart:
        digest = hashlib.sha256(canonical_bytes(part.document)).hexdigest()
        previous = self._part_digests.get(storage_id)
        if previous is not None and previous != digest:
            raise ReviewInputError("GitHub partition changed")
        self._part_digests[storage_id] = digest
        self._verified_parts[storage_id] = part
        return part

    def _part_from_item(
        self, item: dict[str, Any], identity: SessionIdentity, storage_id: str
    ) -> AuthenticatedPart:
        owned = self._part_owner(item, identity)
        if type(owned.get("id")) is not int or owned.get("id") != int(storage_id):
            raise ReviewInputError("GitHub partition identity does not match")
        body = owned.get("body")
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
        return self._remember_part(
            storage_id, AuthenticatedPart(document, self.evidence_producer())
        )

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
        from .quarantine_rollover import read_startup_part

        startup_part = read_startup_part(self, identity, storage_id)
        if startup_part is not None:
            return startup_part
        snapshot = getattr(self, "_quarantine_checkpoint_snapshot", None)
        if snapshot is not None and snapshot.ledger is self:
            snapshot.require_part(storage_id)
        # One activation verifies a part once. Later reads in that activation
        # reuse the verified body. A later call fetches again, so a part that
        # disappeared or changed owner is still refused.
        if self._reuse_listed_parts or self._activation_ticket is not None:
            cached = self._verified_parts.get(storage_id)
            if cached is not None:
                return cached
            if self._part_is_listed(storage_id):
                listed = self._comments_by_id[int(storage_id)]
                return self._part_from_item(listed, identity, storage_id)
        if getattr(self, "_quarantine_complete_read", False):
            raise ReviewInputError(
                "quarantine complete listing is missing a required carrier"
            )
        path = self.http.repository_path(
            identity.repository, f"/issues/comments/{storage_id}"
        )
        status, payload = self._request("GET", path)
        if status != 200 or not isinstance(payload, dict):
            raise ReviewInputError("GitHub partition is missing")
        return self._part_from_item(payload, identity, storage_id)

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
        if isinstance(manifest, dict) and manifest.get("encoding") == INLINE_ENCODING:
            return read_inline_evidence(manifest, expected_binding=expected_binding)
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

    @property
    def supports_paged_disposition_snapshot_writes(self) -> bool:
        # Reader-first: the candidate batch does not enable production v2 writes.
        return False

    def _preflight_disposition_snapshot_batch(
        self, identity, record, *, missing_parts, read_parts, retained_parts
    ) -> None:
        # Plain adaptive replace has no sealed future scan-call allocation. Its
        # existing v1 path remains available; a candidate batch requires the
        # existing bounded tail or the run-owned exact-checkpoint override.
        if not self._tail_preparing or self._tail_scan_pages is None:
            raise ReviewInputError(
                "hosted disposition batch requires bounded activation scope"
            )
        self._validate_tail_scan_pages(self._tail_scan_pages)
        assert self.evidence_budget is not None
        calls = (
            2 * missing_parts
            + read_parts
            + 3 * retained_parts
            + 2 * read_parts
            + 2 * self._tail_scan_pages
            + 3
        )
        self.evidence_budget.preflight(calls)

    def stage_disposition_snapshot_batch(
        self,
        identity: SessionIdentity,
        *,
        record: SessionRecord,
        binding: dict[str, object],
        pages: Sequence[Mapping[str, Any]],
        values: tuple[Mapping[str, object], ...],
        result_digest: str,
        max_manifest_bytes: int,
    ) -> dict[str, object]:
        """Prepare one assigned-ID batch under existing discovery/allocation.

        Numeric IDs arrive after POST. Index compression is bounded from its
        maximum raw size, not favorable placeholder compression. Every physical
        old carrier and late-growth promise remains charged; uncertain effects
        stop the batch and are rediscovered by the existing recovery protocol.
        """
        from review_sensei.disposition_snapshot import (
            DISPOSITION_PAGE_ITEMS,
            _prepared_page_reference,
            read_paged_snapshot,
            validate_page_index,
        )
        from review_sensei.session import _stored_dispositions

        if (
            not self.enable_disposition_snapshot_writes
            or not self._mutation_active
            or self.evidence_budget is None
        ):
            raise ReviewInputError(
                "hosted disposition batch requires enabled conditional mutation"
            )
        if (
            type(self).stage_evidence
            is not GitHubIssueCommentSessionLedger.stage_evidence
            and getattr(self, "_part_staging_identity", None) != identity
        ):
            raise ReviewInputError(
                "derived disposition batch requires its owned staging scope"
            )
        if (
            binding.get("repository") != identity.repository
            or binding.get("pull_request") != identity.pull_request
            or binding.get("repository_id") != identity.repository_id
            or binding.get("producer") != self.evidence_producer()
            or binding.get("purpose") != "dispositions"
        ):
            raise ReviewInputError("hosted disposition batch binding does not match")
        values = _stored_dispositions(list(values), snapshot=True)
        root_shell = {
            "schema_version": "2.0",
            "result_digest": result_digest,
            "state_manifest": {},
        }
        max_manifest_bytes = min(
            max_manifest_bytes,
            disposition_snapshot_capacity(record)
            - len(canonical_bytes(root_shell))
            + 2,
        )
        items = self._bounded_comments(identity)
        if not self._inventory_complete:
            raise ReviewInputError(
                "hosted disposition batch needs complete retained census"
            )
        existing: dict[str, str] = {}
        stored_count = stored_bytes = 0
        for item in self._owned_carrier_items(items, identity):
            stored_count += 1
            stored_bytes += len(item["body"].encode())
            existing[item["body"]] = str(item["id"])
        references: list[dict[str, Any]] = []
        page_specs: list[tuple[object, tuple[dict[str, object], ...]] | None] = []
        missing: dict[str, dict[str, object]] = {}
        for index, supplied in enumerate(pages):
            page = json.loads(canonical_bytes(supplied))
            if page.get("schema_version") != "disposition-page-v1":
                references.append(
                    _prepared_page_reference(
                        page,
                        binding=binding,
                        values=values,
                        result_digest=result_digest,
                        offset=index * DISPOSITION_PAGE_ITEMS,
                    )
                )
                page_specs.append(None)
                continue
            offset = index * DISPOSITION_PAGE_ITEMS
            if (
                set(page)
                != {"schema_version", "result_digest", "offset", "dispositions"}
                or type(page["offset"]) is not int
                or page["offset"] != offset
                or page["result_digest"] != result_digest
                or page["dispositions"]
                != list(values[offset : offset + DISPOSITION_PAGE_ITEMS])
            ):
                raise ReviewInputError("prepared hosted disposition page differs")
            inline = inline_manifest(
                page, binding=binding, item_count=DISPOSITION_PAGE_ITEMS
            )
            if inline is not None and len(canonical_bytes(inline)) <= 1024:
                references.append(inline)
                page_specs.append(None)
                continue
            parts = partition_evidence(
                page, binding=binding, item_count=DISPOSITION_PAGE_ITEMS
            )
            ids = []
            for number, part in enumerate(parts):
                body = _immutable_part_body(part)
                if len(body.encode()) > MAX_PART_BYTES:
                    raise ReviewInputError(
                        "hosted disposition page body exceeds capacity"
                    )
                if body not in existing:
                    missing[body] = part
                ids.append(existing.get(body, "x" + str(number).zfill(18)))
            prospective = evidence_manifest(
                page,
                binding=binding,
                item_count=DISPOSITION_PAGE_ITEMS,
                parts=parts,
                storage_ids=tuple(ids),
            )
            if len(canonical_bytes(prospective)) > 1024:
                raise ReviewInputError(
                    "hosted disposition page manifest exceeds allocation"
                )
            references.append(prospective)
            page_specs.append((page, parts))
        index_document = {
            "schema_version": "disposition-pages-v1",
            "result_digest": result_digest,
            "pages": references,
            "tail": list(values[len(pages) * DISPOSITION_PAGE_ITEMS :]),
        }
        # Validate the closed page structure/count/scope before any POST. The
        # private nineteen-character sizing IDs never leave this preparation.
        validate_page_index(
            index_document,
            root={
                "schema_version": "2.0",
                "result_digest": result_digest,
                "state_manifest": {"binding": binding, "item_count": len(values)},
            },
        )
        bounds = _disposition_index_bounds(
            index_document,
            binding=binding,
            item_count=len(values),
            max_manifest_bytes=max_manifest_bytes,
        )
        from .quarantine_published_tail import retained_storage_reserve

        reserve_parts, reserve_bytes = retained_storage_reserve(self)
        future_parts = len(missing) + len(bounds.part_bytes)
        future_bytes = sum(len(body.encode()) for body in missing) + sum(
            bounds.body_bytes
        )
        if (
            stored_count + future_parts + reserve_parts > MAX_STORED_PARTS
            or stored_bytes + future_bytes + reserve_bytes > MAX_STORED_PART_BYTES
        ):
            raise ReviewInputError(
                "hosted disposition whole retention capacity exceeded before staging"
            )
        from .publication_recovery import _publication_transport

        guard = getattr(
            _publication_transport(self.http), "_quarantine_capacity_guard", None
        )
        if guard is not None and future_parts:
            available = getattr(guard, "comment_bounds", None)
            if not isinstance(available, list):
                raise ReviewInputError(
                    "hosted disposition batch has no exact comment promises"
                )
            available = list(available)
            # The command receipt is a distinct mandatory effect, not spare
            # evidence capacity. A previously selected receipt-update target
            # already has its own update-byte reserve and no creation promise.
            if self._quarantine_command_receipt_marker is None:
                raise ReviewInputError(
                    "hosted disposition batch needs distinct evidence promises"
                )
            if getattr(guard, "command_receipt_update_id", None) is None:
                receipt_choices = [
                    bound
                    for bound in available
                    if type(bound) is int and bound >= 65_536
                ]
                if not receipt_choices:
                    raise ReviewInputError(
                        "hosted disposition batch must retain its command receipt"
                    )
                available.remove(min(receipt_choices))
            wire_bounds = [
                len(canonical_bytes({"body": body})) for body in missing
            ] + list(bounds.wire_bytes)
            for size in sorted(wire_bounds, reverse=True):
                choices = [
                    bound for bound in available if type(bound) is int and bound >= size
                ]
                if not choices:
                    raise ReviewInputError(
                        "hosted disposition batch exceeds existing comment promises"
                    )
                available.remove(min(choices))
        old_pages = _disposition_page_ids(self, record)
        old_manifest = (
            record.disposition_snapshot["state_manifest"]
            if record.disposition_snapshot
            else None
        )
        old_snapshot_parts = len(old_pages) + (
            len(old_manifest["parts"])
            if isinstance(old_manifest, dict)
            and old_manifest["encoding"] == PARTITION_ENCODING
            else 0
        )
        retained_parts = (
            len(
                _tail_part_ids(
                    record,
                    history_ids=_history_extra_ids(self, record),
                    disposition_page_ids=old_pages,
                )
            )
            - old_snapshot_parts
        )
        read_parts = sum(
            len(ref["parts"])
            for ref in references
            if ref["encoding"] == PARTITION_ENCODING
        ) + len(bounds.part_bytes)
        self._preflight_disposition_snapshot_batch(
            identity,
            record,
            missing_parts=future_parts,
            read_parts=read_parts,
            retained_parts=retained_parts,
        )
        cache = getattr(self, "_disposition_page_cache", {})
        if not isinstance(cache, dict) or len(cache) >= MAX_PART_READS:
            raise ReviewInputError("disposition page proof cache exceeds finite bound")

        def write(part: dict[str, object]) -> str:
            body = _immutable_part_body(part)
            if body in existing:
                return existing[body]
            self._verify_live_head(identity)
            status, payload = self._request(
                "POST", self._comments_path(identity), body={"body": body}
            )
            if (
                status != 201
                or not isinstance(payload, dict)
                or type(payload.get("id")) is not int
                or not 1 <= payload["id"] < 10**19
                or payload.get("body") != body
            ):
                raise GitHubPublicationTransientError(
                    "hosted disposition create unconfirmed; rediscovery required"
                )
            self._part_owner(payload, identity)
            storage_id = str(payload["id"])
            if storage_id in existing.values():
                raise ReviewInputError("hosted disposition assigned identity conflicts")
            existing[body] = storage_id
            return storage_id

        actual_references = []
        for reference, spec in zip(references, page_specs):
            if spec is None:
                actual_references.append(reference)
                continue
            page, parts = spec
            manifest = evidence_manifest(
                page,
                binding=binding,
                item_count=DISPOSITION_PAGE_ITEMS,
                parts=parts,
                storage_ids=tuple(write(part) for part in parts),
            )
            if len(canonical_bytes(manifest)) > len(canonical_bytes(reference)):
                raise ReviewInputError(
                    "hosted disposition page exceeded prepared bound"
                )
            actual_references.append(manifest)
        actual_document = {**index_document, "pages": actual_references}
        if len(canonical_bytes(actual_document)) > bounds.decoded_bytes:
            raise ReviewInputError(
                "hosted disposition index exceeded prepared raw bound"
            )
        if bounds.inline:
            inlined_index = inline_manifest(
                actual_document, binding=binding, item_count=len(values)
            )
            if (
                inlined_index is None
                or len(canonical_bytes(inlined_index)) > bounds.manifest_bytes
            ):
                raise ReviewInputError(
                    "hosted disposition inline index exceeded prepared bound"
                )
            manifest = inlined_index
        else:
            parts = partition_evidence(
                actual_document, binding=binding, item_count=len(values)
            )
            if len(parts) > len(bounds.part_bytes) or any(
                len(canonical_bytes(part)) > bound
                for part, bound in zip(parts, bounds.part_bytes)
            ):
                raise ReviewInputError(
                    "hosted disposition compressed index exceeded prepared bound"
                )
            manifest = evidence_manifest(
                actual_document,
                binding=binding,
                item_count=len(values),
                parts=parts,
                storage_ids=tuple(write(part) for part in parts),
            )
            if len(canonical_bytes(manifest)) > bounds.manifest_bytes:
                raise ReviewInputError(
                    "hosted disposition index manifest exceeded prepared bound"
                )
        document = self.read_evidence(identity, manifest, expected_binding=binding)
        root = {
            "schema_version": "2.0",
            "result_digest": result_digest,
            "state_manifest": manifest,
        }
        restored = read_paged_snapshot(
            document, root=root, reader=self.read_evidence, identity=identity
        )
        if restored != values:
            raise ReviewInputError("hosted disposition batch readback differs")
        _remember_disposition_pages(self, root, document)
        return manifest

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
            not (
                self.enable_partition_writes
                or (
                    self.enable_disposition_snapshot_writes
                    and binding.get("purpose") == "dispositions"
                )
            )
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
        inlined = inline_manifest(document, binding=binding, item_count=item_count)
        if (
            inlined is not None
            and isinstance(max_manifest_bytes, int)
            and not isinstance(max_manifest_bytes, bool)
            and len(canonical_bytes(inlined)) <= max_manifest_bytes
        ):
            return inlined
        items = self._bounded_comments(identity)
        existing: dict[str, str] = {}
        stored_bytes = 0
        stored_count = 0
        for item in self._owned_carrier_items(items, identity):
            body = item["body"]
            stored_bytes += len(body.encode())
            stored_count += 1
            existing[body] = str(item["id"])
        from .quarantine_published_tail import retained_storage_reserve

        reserve_parts, reserve_bytes = retained_storage_reserve(self)
        if (
            stored_count + reserve_parts > MAX_STORED_PARTS
            or stored_bytes + reserve_bytes > MAX_STORED_PART_BYTES
        ):
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
        from .publication_recovery import _publication_transport

        capacity = getattr(
            _publication_transport(self.http), "_quarantine_capacity_guard", None
        )
        if capacity is not None:
            capacity.admit_comment_batch(missing_bodies)
        if self._tail_scan_pages is not None and not self._inventory_complete:
            raise ReviewInputError(
                "activation future metadata exceeds sealed page bound"
            )
        if (
            stored_count + len(missing_bodies) + reserve_parts > MAX_STORED_PARTS
            or stored_bytes
            + sum(len(body.encode()) for body in missing_bodies)
            + reserve_bytes
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
                len(existing) + reserve_parts >= MAX_STORED_PARTS
                or sum(len(old.encode()) for old in existing)
                + len(body.encode())
                + reserve_bytes
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

    def _cached_comments(self) -> list[Any]:
        return [self._comments_by_id[comment_id] for comment_id in self._comment_order]

    def _snapshot_comment(self, item: object) -> dict[str, Any] | None:
        if (
            not isinstance(item, dict)
            or isinstance(item.get("id"), bool)
            or not isinstance(item.get("id"), int)
        ):
            return None
        return copy.deepcopy(item)

    def _store_comment(self, item: dict[str, Any]) -> None:
        comment_id = item["id"]
        previous = self._comments_by_id.get(comment_id)
        if (
            previous is not None
            and previous.get("body") != item.get("body")
            and str(comment_id) in self._verified_parts
        ):
            raise ReviewInputError("GitHub partition changed")
        self._comments_by_id[comment_id] = item
        if comment_id not in self._comment_order:
            self._comment_order.append(comment_id)

    def _replace_inventory(self, items: list[Any]) -> None:
        self._comments_by_id = {}
        self._comment_order = []
        self._merge_comments(items)

    def _merge_comments(self, items: list[Any]) -> None:
        for item in items:
            copied = self._snapshot_comment(item)
            if copied is not None:
                self._store_comment(copied)

    def _note_anchor(self, items: list[Any]) -> None:
        stamps = [
            stamp
            for item in items
            if isinstance(item, dict)
            for stamp in (item.get("updated_at"),)
            if isinstance(stamp, str)
        ]
        if not stamps:
            return
        earlier = _since_margin(max(stamps))
        if earlier is not None and (
            self._list_anchor is None or earlier > self._list_anchor
        ):
            self._list_anchor = earlier

    def _take_primed_comments(self, identity: SessionIdentity) -> list[Any] | None:
        primed = getattr(self.http, "primed_issue_comments", None)
        if not isinstance(primed, dict):
            return None
        shared = primed.pop(self._comments_path(identity), None)
        if not isinstance(shared, list):
            return None
        return shared

    def _fence_path(self, identity: SessionIdentity, page: int) -> str:
        path = f"{self._comments_path(identity)}?per_page={self._fence_page_size}&page={page}"
        anchor = self._fence_anchor
        if isinstance(anchor, str) and anchor:
            path += f"&since={quote(anchor, safe='')}"
        return path

    def _fencing(self) -> bool:
        return self._activation_ticket is not None and self._tail_phase in {
            "reload",
            "readback",
        }

    def _fetch_pages(
        self, identity: SessionIdentity, *, since: str | None
    ) -> list[Any]:
        """Read a comment list with falling page sizes that step back up."""
        if self._quarantine_required or self._quarantine_full_discovery:
            from .caller_operation import caller_for_http
            from .quarantine_census import fetch_comments

            caller = caller_for_http(self.http)
            if caller is None:
                raise ReviewInputError(
                    "registered quarantine census requires its physical caller"
                )
            caller._require_ledger(self, identity)
            caller._quarantine_census_api_url = self.http.api_url
            return fetch_comments(self, identity)

        collected: list[Any] = []
        page_size_index = 0
        while len(collected) <= MAX_PAGINATION_ITEMS:
            hint_path = self._comments_path(identity)
            if since is not None:
                hint_path += "?since=" + quote(since, safe="")
            hint = self.http.pagination_page_size(hint_path, len(collected))
            page_size_index = next(
                (
                    index
                    for index in range(page_size_index, len(_COMMENT_PAGE_SIZES))
                    if _COMMENT_PAGE_SIZES[index] <= hint
                    and len(collected) % _COMMENT_PAGE_SIZES[index] == 0
                ),
                page_size_index,
            )
            page_size = _COMMENT_PAGE_SIZES[page_size_index]
            if len(collected) % page_size:
                raise ReviewInputError("partition metadata enumeration is unavailable")
            page = len(collected) // page_size + 1
            path = f"{self._comments_path(identity)}?per_page={page_size}&page={page}"
            if since is not None:
                path += f"&since={quote(since, safe='')}"
            try:
                status, payload = self._request("GET", path)
            except GitHubHTTPResponseTooLargeError:
                if page_size_index + 1 >= len(_COMMENT_PAGE_SIZES):
                    raise ReviewInputError(
                        "partition metadata enumeration is unavailable"
                    ) from None
                page_size_index += 1
                continue
            if (
                status != 200
                or not isinstance(payload, list)
                or len(payload) > page_size
            ):
                raise ReviewInputError("partition metadata enumeration is unavailable")
            # Select a safe size before sealing the tail's exact request URLs.
            # A sealed fence never changes size or adds replacement requests.
            if self._activation_ticket is None:
                self._fence_page_size = min(self._fence_page_size, page_size)
            collected.extend(payload)
            if len(payload) < page_size:
                return collected
            if len(collected) >= MAX_PAGINATION_ITEMS:
                raise ReviewInputError(
                    "partition metadata enumeration exceeds item bound"
                )
            page_size_index = next(
                index
                for index, size in enumerate(_COMMENT_PAGE_SIZES)
                if len(collected) % size == 0
            )
        raise ReviewInputError("partition metadata enumeration exceeds item bound")

    def _fence_scan(self, identity: SessionIdentity) -> list[Any]:
        pages = self._tail_scan_pages if self._tail_scan_pages is not None else 1
        collected: list[Any] = []
        for page in range(1, pages + 1):
            status, payload = self._request("GET", self._fence_path(identity, page))
            if (
                status != 200
                or not isinstance(payload, list)
                or len(payload) > self._fence_page_size
            ):
                raise ReviewInputError("partition metadata enumeration is unavailable")
            collected.extend(payload)
            if len(payload) < self._fence_page_size:
                self._merge_comments(collected)
                self._note_anchor(collected)
                return self._cached_comments()
        raise ReviewInputError("partition metadata enumeration exceeds item bound")

    def _history_pages(self, identity: SessionIdentity) -> list[Any]:
        """Read only as far as the declared history scan, and do not cache it.

        A history lookup repeats this read so a root edited while a child was
        fetched is still visible. A full last page means the list continues
        past the declared bound, which refuses rather than scanning further.
        """

        limit = self._tail_scan_pages if self._tail_scan_pages is not None else 1
        if self._quarantine_required or self._quarantine_full_discovery:
            from .caller_operation import caller_for_http
            from .quarantine_census import fetch_comments

            caller = caller_for_http(self.http)
            if caller is None:
                raise ReviewInputError(
                    "registered quarantine census requires its physical caller"
                )
            caller._require_ledger(self, identity)
            caller._quarantine_census_api_url = self.http.api_url
            return fetch_comments(self, identity)
        collected: list[Any] = []
        page_size_index = 0
        used = 0
        while used < limit and len(collected) < MAX_PAGINATION_ITEMS:
            hint = self.http.pagination_page_size(
                self._comments_path(identity), len(collected)
            )
            page_size_index = next(
                (
                    index
                    for index in range(page_size_index, len(_COMMENT_PAGE_SIZES))
                    if _COMMENT_PAGE_SIZES[index] <= hint
                    and len(collected) % _COMMENT_PAGE_SIZES[index] == 0
                ),
                page_size_index,
            )
            page_size = _COMMENT_PAGE_SIZES[page_size_index]
            if len(collected) % page_size:
                raise ReviewInputError("partition metadata enumeration is unavailable")
            page = len(collected) // page_size + 1
            path = f"{self._comments_path(identity)}?per_page={page_size}&page={page}"
            used += 1
            try:
                status, payload = self._request("GET", path)
            except GitHubHTTPResponseTooLargeError:
                if page_size_index + 1 >= len(_COMMENT_PAGE_SIZES):
                    raise ReviewInputError(
                        "partition metadata enumeration is unavailable"
                    ) from None
                page_size_index += 1
                continue
            if (
                status != 200
                or not isinstance(payload, list)
                or len(payload) > page_size
            ):
                raise ReviewInputError("partition metadata enumeration is unavailable")
            collected.extend(payload)
            if len(payload) < page_size:
                return collected
            if used >= limit or len(collected) >= MAX_PAGINATION_ITEMS:
                raise ReviewInputError(
                    "partition metadata enumeration exceeds item bound"
                )
            page_size_index = next(
                index
                for index, size in enumerate(_COMMENT_PAGE_SIZES)
                if len(collected) % size == 0
            )
        raise ReviewInputError("partition metadata enumeration exceeds item bound")

    def _bounded_comments(self, identity: SessionIdentity) -> list[Any]:
        assert self.evidence_budget is not None
        if self._history_reading:
            return self._history_pages(identity)
        if self._fencing():
            return self._fence_scan(identity)
        if self._inventory_complete and not self._list_refresh:
            return self._cached_comments()
        if self._list_refresh and self._inventory_complete and self._list_anchor:
            self._list_refresh = False
            changed = self._fetch_pages(identity, since=self._list_anchor)
            self._merge_comments(changed)
            self._note_anchor(changed)
            return self._cached_comments()
        self._list_refresh = False
        primed = self._take_primed_comments(identity)
        if primed is not None:
            self._replace_inventory(primed)
            self._inventory_complete = True
            self._note_anchor(primed)
            return self._cached_comments()
        items = self._fetch_pages(identity, since=None)
        self._replace_inventory(items)
        self._inventory_complete = True
        self._note_anchor(items)
        return self._cached_comments()

    def _require_identity(self, identity: SessionIdentity) -> int:
        if identity.repository_id is None:
            raise ReviewInputError("GitHub session identity requires repository_id")
        self._expected_repository_id = identity.repository_id
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
        remember = getattr(self, "_remember_consumed_grant", None)
        if callable(remember) and remember(self._session_grant) is not True:
            raise ReviewInputError("session grant was already used")
        self._accepted_consumed_grant = hashlib.sha256(
            self._session_grant.encode()
        ).hexdigest()
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
        if self._metadata_phase_closed:
            raise ReviewInputError("analysis metadata phase is closed")
        self._request_started = True
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
                        and path == self._fence_path(self._activation_identity, 1)
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
        except GitHubHTTPResponseTooLargeError:
            raise
        except GitHubHTTPError as exc:
            raise GitHubPublicationError("session ledger request failed") from exc

    def _render_comment(
        self, *, repository_id: int, pull_request: int, record: SessionRecord
    ) -> str:
        accepted = self._accepted_consumed_grant
        if accepted is not None and accepted in self._observed_consumed_grants:
            raise ReviewInputError("session grant was already used")
        digests = set(self._observed_consumed_grants)
        if accepted is not None:
            digests.add(accepted)
        body = render_session_comment(
            repository_id=repository_id,
            pull_request=pull_request,
            record=record,
            consumed_grants=tuple(sorted(digests)),
        )
        restart = getattr(self, "_controlled_restart", None)
        if restart is not None:
            body = restart.render(self, record, body)
        # Validate the exact comment before any GitHub write. Writers used to
        # PATCH first and parse the response afterwards, so a candidate the
        # public schema rejects became the stored ledger and the next command
        # only reported "session comment record is invalid".
        self._require_readable_candidate(body, record)
        return body

    def _require_readable_candidate(self, body: str, record: SessionRecord) -> None:
        try:
            preview = parse_session_comment(body, identity=record.identity())
        except (SessionLoadError, ReviewInputError) as exc:
            cause = exc.__cause__
            if isinstance(exc, ReviewInputError):
                detail = str(exc)
            else:
                detail = str(cause) if isinstance(cause, ReviewInputError) else str(exc)
            raise ReviewInputError(
                f"session comment was not written: {detail}"
            ) from exc
        if (
            preview is None
            or preview.record_sha256 != record.record_sha256
            or preview.generation != record.generation
        ):
            raise ReviewInputError(
                "session comment was not written: candidate is unreadable"
            )

    def _discover(
        self, identity: SessionIdentity, *, now: datetime | None = None
    ) -> tuple[int | None, SessionRecord | None]:
        self._observed_consumed_grants = set()
        if getattr(self, "_quarantine_full_discovery", False) or getattr(
            self, "_quarantine_required", False
        ):
            self._inventory_complete = False
            self._list_refresh = True
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
        restart = getattr(self, "_controlled_restart", None)
        if restart is not None:
            items = restart.select(self, identity, items)
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
            self._observed_consumed_grants = set(_CONSUMED_GRANT_LINE.findall(body))
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
            if (
                record.quarantine_lineage is not None
                and getattr(self, "_quarantine_origin_reader", None) is None
            ):
                raise SessionLoadError(
                    SessionLoadReason.INTEGRITY_FAILED,
                    "quarantine lineage requires authenticated provenance",
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
        from .quarantine_selection import select_quarantine

        try:
            selected = select_quarantine(self, identity, items, found)
        except ReviewInputError as error:
            raise SessionLoadError(
                SessionLoadReason.INTEGRITY_FAILED, str(error)
            ) from error
        if selected is not None:
            found = [selected]
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
            or record.disposition_snapshot is not None
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
                read_session_dispositions(self, record)
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
        self._list_refresh = True
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
                "body": self._render_comment(
                    repository_id=repository_id,
                    pull_request=identity.pull_request,
                    record=record,
                )
            },
        )
        ambiguous = status in {202, 409, 422, 429} or status >= 500
        if not (200 <= status < 300 or ambiguous):
            raise GitHubPublicationError("session comment create failed")
        self._list_refresh = True
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
        self._list_refresh = True
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
            if record.quarantine_lineage is not None:
                from .quarantine_action import require_native_decision

                require_native_decision(self, record, updated)
            _validate_queue_retention(record, updated)
            read_session_baseline(self, updated)
            read_session_dispositions(self, updated)
            read_session_assessment_queue(self, updated)
            _history_legacy_write_guard(self, updated)
        finally:
            self._mutation_active = False
        if self.evidence_budget is not None:
            self._list_refresh = True
            preactivation_id, preactivation = self._discover(identity, now=now)
            if (
                preactivation_id != comment_id
                or preactivation is None
                or preactivation.record_sha256 != record.record_sha256
                or preactivation.generation != record.generation
            ):
                raise ReviewInputError("session generation conflict before activation")
        if not self._is_own_release(record, updated):
            self._verify_live_head(identity)
        repository_id = self._require_identity(identity)
        path = self.http.repository_path(
            identity.repository, f"/issues/comments/{comment_id}"
        )
        status, payload = self._request(
            "PATCH",
            path,
            body={
                "body": self._render_comment(
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
        self._list_refresh = True
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
        witness: object = None,
    ) -> SessionRecord:
        if not self.enable_partition_writes or self.evidence_budget is None:
            raise ReviewInputError(
                "prepaid activation writers or original budget are unavailable"
            )
        self._validate_tail_scan_pages(max_scan_pages)
        budget = self.evidence_budget
        scope = _tail_attempt_scope(identity, reservation_id, operation_binding)
        witnessed = _operation_witness_matches(witness, reservation_id)
        if (
            scope in budget._live_attempts
            or scope in budget._failed_attempts
            or (budget.restored and not witnessed)
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
            budget._remember_live_attempt(
                scope,
                reserved.record_sha256,
                owner=self,
                witness=witness,
                reservation_id=reservation_id,
            )
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
        witness: object = None,
    ) -> None:
        raise ReviewInputError(
            "phase activation grants are retired; current actions use run-owned accounting"
        )

    def admit_with_tail(
        self,
        identity: SessionIdentity,
        prepare: Callable[[SessionRecord], SessionRecord],
        *,
        slot: str,
        expected_generation: int,
        operation_binding: Mapping[str, object],
        attempt_reservation_id: str,
        seal_accounting: Callable[[SessionRecord, Mapping[str, int]], SessionRecord],
        max_scan_pages: int,
        now: datetime | None = None,
        witness: object = None,
    ) -> SessionRecord:
        """Write reservation plus complete admission in one owned mutation.

        ``prepare`` receives an uncommitted reservation candidate. It adds the
        complete checkpoint without another generation increment. The caller
        must source/PR/base/head/latest-inventory fence this preparation and
        cannot dispatch inference until this method's owned readback succeeds.
        """
        budget = self.evidence_budget
        if (
            not isinstance(witness, OperationHandle)
            or not isinstance(operation_binding, Mapping)
            or not _operation_witness_matches(witness, attempt_reservation_id)
            or budget is None
            or witness.scope_digest
            != hashlib.sha256(
                f"{identity.repository_id}:{identity.pull_request}".encode()
            ).hexdigest()
            or witness.operation_id != operation_binding.get("operation_id")
            or witness.control_deadline_ms != budget.wall_deadline_ms
            or witness.created_at_ms + 60_000 != witness.control_deadline_ms
            or type(witness.calls) is not int
            or not budget.calls <= witness.calls <= 64
            or type(expected_generation) is not int
            or not 0 <= expected_generation < 2_147_483_647
            or slot != "verification"
        ):
            raise NonResumableActivationError(
                "original admitted control witness differs"
            )
        attestation = self._session_attestation
        mutation = (
            attestation.get("mutation") if isinstance(attestation, Mapping) else None
        )
        if (
            not isinstance(attestation, Mapping)
            or attestation.get("version") != 2
            or not isinstance(mutation, Mapping)
            or mutation.get("reason") != "admission-dispatch"
        ):
            raise ReviewInputError(
                "initial activation requires exact admission-dispatch grant"
            )
        scope = _tail_attempt_scope(identity, attempt_reservation_id, operation_binding)
        if scope in budget._live_attempts or scope in budget._failed_attempts:
            raise NonResumableActivationError(
                "original initial admission is already owned; inspect its retained checkpoint"
            )
        return self._replace_with_tail(
            identity,
            prepare,
            operation_binding=operation_binding,
            attempt_reservation_id=attempt_reservation_id,
            seal_accounting=seal_accounting,
            max_scan_pages=max_scan_pages,
            now=now,
            witness=witness,
            initial_slot=slot,
            initial_generation=expected_generation,
        )

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
        witness: object = None,
    ) -> SessionRecord:
        """Each checkpoint consumes a fresh grant; prepay all sealed root work."""
        return self._replace_with_tail(
            identity,
            prepare,
            operation_binding=operation_binding,
            attempt_reservation_id=attempt_reservation_id,
            seal_accounting=seal_accounting,
            max_scan_pages=max_scan_pages,
            now=now,
            witness=witness,
        )

    def _replace_with_tail(
        self,
        identity: SessionIdentity,
        prepare: Callable[[SessionRecord], SessionRecord],
        *,
        operation_binding: Mapping[str, object],
        attempt_reservation_id: str,
        seal_accounting: Callable[[SessionRecord, Mapping[str, int]], SessionRecord],
        max_scan_pages: int,
        now: datetime | None = None,
        witness: object = None,
        initial_slot: str | None = None,
        initial_generation: int | None = None,
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
        witnessed = _operation_witness_matches(witness, attempt_reservation_id)
        if scope in budget._failed_attempts or (
            not witnessed
            and (
                budget.restored
                or scope not in budget._live_attempts
                or budget._live_attempt_owners.get(scope) is not self
            )
        ):
            raise NonResumableActivationError(
                "original activation attempt witness is unavailable"
            )
        ticket: EvidenceTailTicket | None = None
        self._tail_authorizing, self._tail_scan_pages = True, max_scan_pages
        self._list_refresh = True
        self._reuse_listed_parts = True
        self._verified_parts = {}
        self._part_digests = {}
        try:
            self._verify_mutation_grant(identity)
            comment_id, before = self._discover(identity, now=now)
            if comment_id is None or before is None or before.expired(now=now):
                raise ReviewInputError(
                    "prepaid activation current authority is unavailable"
                )
            reserved = None
            if initial_slot is not None:
                if (
                    before.reservation_id is not None
                    or before.generation != initial_generation
                ):
                    raise ReviewInputError(
                        "initial activation reservation or generation differs"
                    )
                reserved = mutate_reserved(
                    before,
                    slot=initial_slot,
                    reservation_id=attempt_reservation_id,
                    expected_generation=before.generation,
                    head_sha=self._head_sha,
                    now=now,
                )
                budget._remember_live_attempt(
                    scope,
                    before.record_sha256,
                    owner=self,
                    witness=witness,
                    reservation_id=attempt_reservation_id,
                )
            budget._require_live_attempt(
                scope,
                before.record_sha256,
                owner=self,
                witness=witness,
                reservation_id=attempt_reservation_id,
            )
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
            draft = prepare(reserved if reserved is not None else before)
            self._mutation_active = self._tail_preparing = False
            _validate_tail_draft(
                before,
                draft,
                operation=operation,
                reservation_id=attempt_reservation_id,
                budget=budget,
                initial_slot=initial_slot,
            )
            if self.enable_history_graph:
                read_session_dispositions(self, draft)
                read_session_assessment_queue(self, draft)
                _history_preserve_sources(self, before, draft)
            old_ids = _history_extra_ids(self, before)
            new_ids = _history_extra_ids(self, draft)
            old_disposition_ids = _disposition_page_ids(self, before)
            new_disposition_ids = _disposition_page_ids(self, draft)
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
                    old_disposition_page_ids=old_disposition_ids,
                    new_disposition_page_ids=new_disposition_ids,
                )

            def part_steps(record: SessionRecord) -> tuple[TailDispatch, ...]:
                # Parts already verified, or present on the comment page just
                # read, are not fetched again. The plan charges only the reads
                # this activation will still make.
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
                        disposition_page_ids=new_disposition_ids
                        if record is draft
                        or record.record_sha256 != before.record_sha256
                        else old_disposition_ids,
                    )
                    if part_id not in self._verified_parts
                    and not self._part_is_listed(part_id)
                )

            self._fence_anchor = self._list_anchor

            def scan_steps() -> tuple[TailDispatch, ...]:
                return tuple(
                    TailDispatch(
                        f"GET {self._fence_path(identity, page)}",
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
            ticket = (
                budget.reserve_tail(plan)
                if witness is None
                else budget.reserve_tail(
                    plan, witness=witness, reservation_id=attempt_reservation_id
                )
            )
            if initial_slot is not None and (
                not isinstance(witness, OperationHandle) or budget.calls > witness.calls
            ):
                raise ReviewInputError("activation exceeds original prepaid liability")
            sealed = _seal_tail_accounting(draft, seal_accounting, budget)
            _validate_queue_retention(before, sealed)
            patch_body: dict[str, object] = {
                "body": self._render_comment(
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
            read_session_dispositions(self, sealed)
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
            try:
                status, payload = self._request("PATCH", path, body=patch_body)
            except GitHubPublicationError:
                if initial_slot is None:
                    raise
                # The pre-admitted mandatory owned scan resolves uncertainty;
                # never repeat PATCH or consume another grant after an effect.
                status, payload = 0, None
            if initial_slot is None:
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
                or (
                    initial_slot is not None
                    and grant_digest not in self._observed_consumed_grants
                )
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
            self._reuse_listed_parts = False

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
        if record.quarantine_lineage is not None:
            raise ReviewInputError(
                "quarantine lineage requires retained authority and cannot be re-enrolled"
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
                "body": self._render_comment(
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
        self._list_refresh = True
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
        return self._recover_abandoned_analysis(
            identity, now=now, operation="command", unpause=True
        )

    def recover_abandoned_analysis(
        self,
        identity: SessionIdentity,
        *,
        now: datetime | None = None,
        wait_attempts: int = 3,
        wait_seconds: float = 5.0,
        sleeper=None,
    ) -> SessionRecord:
        """Admission self-heal for a leaked reservation (ADR 0079).

        Same evidence and CAS seam as maintainer continuation, but driven
        by the review's own grant and never unpausing: the caller checks
        the operator pause first. A still-running owner is waited on
        briefly (bounded) before being reported as blocking this review.
        """

        return self._recover_abandoned_analysis(
            identity,
            now=now,
            operation="review",
            unpause=False,
            wait_attempts=wait_attempts,
            wait_seconds=wait_seconds,
            sleeper=sleeper,
        )

    def _recover_abandoned_analysis(
        self,
        identity: SessionIdentity,
        *,
        now: datetime | None = None,
        operation: str,
        unpause: bool,
        wait_attempts: int = 1,
        wait_seconds: float = 0.0,
        sleeper=None,
    ) -> SessionRecord:
        import hashlib
        import time

        from review_sensei.session import MAX_FAILED_ATTEMPTS, _failed_attempt_scope

        from .reservation_recovery import (
            ActionsReservationEvidence,
            ReservationOwnerActiveError,
        )

        if (
            self._broker is None
            or self._session_attestation is None
            or (
                self._actions_read_token is None
                and self._actions_token_provider is None
            )
        ):
            if operation == "command":
                raise ReviewInputError(
                    "reservation recovery requires an authenticated maintainer grant and Actions reads"
                )
            raise ReviewInputError(
                "reservation recovery requires an authenticated review grant and Actions reads"
            )
        if self._session_attestation.get("operation") != operation:
            if operation == "command":
                raise ReviewInputError(
                    "reservation recovery requires a maintainer command"
                )
            raise ReviewInputError("reservation recovery requires a review grant")
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
        sleep = sleeper if sleeper is not None else time.sleep
        attempts = max(1, wait_attempts)
        proof = None
        for attempt in range(attempts):
            try:
                proof = evidence.verify(identity, expected)
                break
            except ReviewInputError as exc:
                owner_active = isinstance(exc, ReservationOwnerActiveError)
                if not owner_active or attempt == attempts - 1:
                    if owner_active and attempts > 1:
                        owner_run = (
                            str(expected.reservation_owner["run_id"])
                            if expected.reservation_owner is not None
                            else "unknown"
                        )
                        raise ReviewInputError(
                            f"session reservation is held by run {owner_run} (still running)"
                        ) from exc
                    raise
                sleep(wait_seconds)

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
                operator_paused=False if unpause else current.operator_paused,
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
        rescan_request: AuthorizedRescan | None = None,
        now: datetime | None = None,
    ) -> SessionRecord:
        if rescan_request is not None and not isinstance(
            rescan_request, AuthorizedRescan
        ):
            raise ReviewInputError("explicit rescan authority is invalid")
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
                    rescan_request=rescan_request,
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
        # Releasing a reservation publishes nothing, so the owning run may
        # release its own hold even after the PR head has moved. replace()
        # scopes the live-head exception to exactly this mutation.
        previous = getattr(self, "_owner_release", False)
        self._owner_release = True
        try:
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
        finally:
            self._owner_release = previous

    def _is_own_release(self, record: SessionRecord, updated: SessionRecord) -> bool:
        """True only when this run releases its own held reservation.

        Ownership comes from the broker-recorded reservation owner matching
        the run bound into this adapter's verified session attestation.
        Any other mutation, owner, or outcome keeps the live-head check.
        """

        if not getattr(self, "_owner_release", False):
            return False
        if record.reservation_id is None or updated.reservation_id is not None:
            return False
        owner = record.reservation_owner
        if owner is None:
            return False
        attestation = self._session_attestation or {}
        return str(owner.get("run_id")) == str(attestation.get("run_id")) and str(
            owner.get("head_sha")
        ) == str(attestation.get("head_sha"))

    def load_retained_checkpoint_result(
        self,
        identity: SessionIdentity,
        *,
        transaction: object | None = None,
        head_sha: str | None = None,
    ) -> object | None:
        from dataclasses import replace

        from review_sensei.models import ReviewResult, ReviewTransaction

        from .publication_recovery import _event_key, _store

        target_tx = transaction if isinstance(transaction, ReviewTransaction) else None
        if target_tx is None:
            loaded = self.load(identity)
            if loaded.status in {"ok", "migrated"} and loaded.record is not None:
                target_tx = loaded.record.transaction
        if target_tx is None or target_tx.result_sha256 is None:
            return None
        if head_sha is not None and target_tx.head_sha != head_sha:
            return None
        effective_identity = (
            identity
            if identity.repository_id is not None
            else replace(identity, repository_id=self._expected_repository_id)
        )
        from .publication_recovery import publication_http

        store = _store(
            publication_http(self.http, self.evidence_budget)
            if self.evidence_budget is not None
            else self.http,
            token=self.token,
            identity=effective_identity,
            app_slug=self.app_slug or "reviewsensei[bot]",
            base_sha=target_tx.base_sha,
            head_sha=target_tx.head_sha,
        )
        available = store.read_available(
            event_key=_event_key(effective_identity, target_tx)
        )
        if available is None:
            return None
        stored_digest, payload = available
        # The retained-parts digest covers the full serialized envelope; the
        # transaction digest covers only ReviewResult's content projection.
        if hashlib.sha256(payload).hexdigest() != stored_digest:
            raise ReviewInputError("retained result payload digest does not match")
        document = json.loads(payload)
        if not isinstance(document, dict):
            raise ReviewInputError("retained result is invalid")
        result = ReviewResult.from_dict(document)
        if result.content_digest() != target_tx.result_sha256:
            raise ReviewInputError("retained result digest does not match transaction")
        return replace(result, transaction=target_tx)

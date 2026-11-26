"""ReviewSensei v6 stability-verified pagination and persistent no-progress guard.

Scans PR surfaces exclusively on read-only workflow ``GITHUB_TOKEN`` with no
artificial 75-call or 512-call read ceilings, enforces deterministic ascending
traversal (forbidding ``since=``), verifies prefix stability before resuming a
saved cursor, falls back to rescan-and-deduplicate on earlier deletions, and
stops early on persistent lack of measurable progress across restarts.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from review_sensei.errors import ReviewInputError

from .v6_credential_matrix import (
    CredentialKind,
    GitHubEventKind,
    RepositoryVisibility,
)
from .v6_recovery_ledger import (
    AdvisoryProgressHint,
    canonical_sha256,
)
from .v6_transport import DualCredentialGitHubClient, DualCredentialResponse

_SORTABLE_ASC_ENDPOINTS = frozenset({"pr_issue_comments", "pr_review_comments"})


class RecoveryStalledError(ReviewInputError):
    """Raised when recovery makes zero measurable progress across allowed steps."""

    error_category = "v6_recovery_stalled"

    def __init__(
        self,
        message: str,
        *,
        stall_count: int,
        transient_error_count: int,
        recovery_phase: str,
        last_progress_digest: str,
        diagnostic: str = "v6_recovery_stalled",
    ) -> None:
        super().__init__(message, diagnostic=diagnostic)
        self.stall_count = stall_count
        self.transient_error_count = transient_error_count
        self.recovery_phase = recovery_phase
        self.last_progress_digest = last_progress_digest


@dataclass(frozen=True)
class PaginatedRecord:
    """Canonical record produced by stability-verified pagination."""

    record_id: str | int
    content_sha256: str
    payload: Any

    def __post_init__(self) -> None:
        if type(self.record_id) is int:
            if self.record_id <= 0:
                raise ReviewInputError(
                    f"record_id integer must be > 0, got {self.record_id!r}",
                    diagnostic="v6_invalid_paginated_record",
                )
        elif (
            type(self.record_id) is not str
            or not self.record_id
            or self.record_id.strip() != self.record_id
        ):
            raise ReviewInputError(
                f"record_id must be a positive int or non-empty trimmed str, got {self.record_id!r}",
                diagnostic="v6_invalid_paginated_record",
            )
        if (
            type(self.content_sha256) is not str
            or len(self.content_sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in self.content_sha256)
        ):
            raise ReviewInputError(
                f"content_sha256 must be 64-char lowercase hex, got {self.content_sha256!r}",
                diagnostic="v6_invalid_paginated_record",
            )
        if isinstance(self.payload, Mapping) and not isinstance(
            self.payload, MappingProxyType
        ):
            object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    @classmethod
    def from_payload(cls, record_id: str | int, payload: Any) -> PaginatedRecord:
        """Build a ``PaginatedRecord`` computing ``canonical_sha256(payload)``."""

        return cls(
            record_id=record_id,
            content_sha256=canonical_sha256(payload),
            payload=payload,
        )

    @property
    def dedup_key(self) -> tuple[str, str]:
        """Return the stable deduplication key ``(record_id, content_sha256)``."""

        return (str(self.record_id), self.content_sha256)


def deduplicate_records(
    records: Iterable[PaginatedRecord],
) -> tuple[PaginatedRecord, ...]:
    """Deduplicate ``records`` deterministically by ``(record_id, content_sha256)``."""

    seen: set[tuple[str, str]] = set()
    unique: list[PaginatedRecord] = []
    for record in records:
        if not isinstance(record, PaginatedRecord):
            raise TypeError("records must contain PaginatedRecord instances")
        key = record.dedup_key
        if key not in seen:
            seen.add(key)
            unique.append(record)
    return tuple(unique)


def compute_records_prefix_digest(records: Sequence[PaginatedRecord]) -> str:
    """Compute a canonical SHA-256 digest over an ordered sequence of records."""

    return canonical_sha256(
        [[str(record.record_id), record.content_sha256] for record in records]
    )


class NoProgressGuard:
    """Tracks measurable recovery progress and persists stall counters across restarts."""

    def __init__(
        self,
        *,
        operation_id: str,
        recovery_phase: str = "scan",
        max_stall_steps: int = 2,
        max_transient_retries: int = 3,
        stall_count: int = 0,
        last_progress_digest: str = "",
        transient_error_count: int = 0,
        cursor_hint: str | None = None,
        verified_prefix_count: int = 0,
        verified_prefix_digest: str = "",
        last_verified_id: str | int | None = None,
    ) -> None:
        if type(max_stall_steps) is not int or max_stall_steps <= 0:
            raise ValueError("max_stall_steps must be a positive integer")
        if type(max_transient_retries) is not int or max_transient_retries < 0:
            raise ValueError("max_transient_retries must be a non-negative integer")
        if type(stall_count) is not int or stall_count < 0:
            raise ValueError("stall_count must be a non-negative integer")
        if type(transient_error_count) is not int or transient_error_count < 0:
            raise ValueError("transient_error_count must be a non-negative integer")
        if type(verified_prefix_count) is not int or verified_prefix_count < 0:
            raise ValueError("verified_prefix_count must be a non-negative integer")

        self.operation_id = operation_id
        self.recovery_phase = recovery_phase
        self.max_stall_steps = max_stall_steps
        self.max_transient_retries = max_transient_retries
        self.stall_count = stall_count
        self.last_progress_digest = last_progress_digest
        self.transient_error_count = transient_error_count
        self.cursor_hint = cursor_hint
        self.verified_prefix_count = verified_prefix_count
        self.verified_prefix_digest = verified_prefix_digest
        self.last_verified_id = last_verified_id

    @classmethod
    def from_advisory_hint(
        cls,
        hint: AdvisoryProgressHint,
        *,
        max_stall_steps: int = 2,
        max_transient_retries: int = 3,
    ) -> NoProgressGuard:
        """Restore persistent guard counters from a PR-resident ``AdvisoryProgressHint``."""

        if not isinstance(hint, AdvisoryProgressHint):
            raise TypeError("hint must be an AdvisoryProgressHint")
        return cls(
            operation_id=hint.operation_id,
            recovery_phase=hint.recovery_phase,
            max_stall_steps=max_stall_steps,
            max_transient_retries=max_transient_retries,
            stall_count=hint.stall_count,
            last_progress_digest=hint.last_progress_digest,
            transient_error_count=hint.transient_error_count,
            cursor_hint=hint.cursor_hint,
            verified_prefix_count=hint.verified_prefix_count,
            verified_prefix_digest=hint.verified_prefix_digest,
            last_verified_id=hint.last_verified_id,
        )

    def to_advisory_hint(
        self,
        *,
        cursor_hint: str | None = None,
        verified_prefix_count: int | None = None,
        verified_prefix_digest: str | None = None,
        last_verified_id: str | int | None = None,
    ) -> AdvisoryProgressHint:
        """Serialize current guard counters into a read-phase ``AdvisoryProgressHint``."""

        effective_count = (
            self.verified_prefix_count
            if verified_prefix_count is None
            else verified_prefix_count
        )
        effective_digest = (
            self.verified_prefix_digest
            if verified_prefix_digest is None
            else verified_prefix_digest
        )
        return AdvisoryProgressHint(
            operation_id=self.operation_id,
            recovery_phase=self.recovery_phase,
            cursor_hint=self.cursor_hint if cursor_hint is None else cursor_hint,
            verified_prefix_count=effective_count,
            verified_prefix_digest=effective_digest,
            last_verified_id=(
                self.last_verified_id if last_verified_id is None else last_verified_id
            ),
            stall_count=self.stall_count,
            last_progress_digest=self.last_progress_digest,
            transient_error_count=self.transient_error_count,
        )

    def ensure_not_stalled(self) -> None:
        """Fail early if persisted counters already exceed allowed stall/error bounds."""

        if self.stall_count >= self.max_stall_steps:
            raise RecoveryStalledError(
                f"recovery stalled for operation {self.operation_id!r} in phase "
                f"{self.recovery_phase!r}: stall_count={self.stall_count} >= "
                f"max_stall_steps={self.max_stall_steps}",
                stall_count=self.stall_count,
                transient_error_count=self.transient_error_count,
                recovery_phase=self.recovery_phase,
                last_progress_digest=self.last_progress_digest,
            )
        if self.transient_error_count > self.max_transient_retries:
            raise RecoveryStalledError(
                f"transient error retry limit exceeded for operation "
                f"{self.operation_id!r}: transient_error_count="
                f"{self.transient_error_count} > max_transient_retries="
                f"{self.max_transient_retries}",
                stall_count=self.stall_count,
                transient_error_count=self.transient_error_count,
                recovery_phase=self.recovery_phase,
                last_progress_digest=self.last_progress_digest,
                diagnostic="v6_transient_retries_exhausted",
            )

    def record_step(
        self,
        *,
        new_records_count: int,
        reconciled_effect: bool,
        new_phase: str,
        state_digest: str,
        transient_error: bool = False,
    ) -> None:
        """Record one recovery step and raise ``RecoveryStalledError`` on stall."""

        if type(new_records_count) is not int or new_records_count < 0:
            raise ValueError("new_records_count must be a non-negative integer")
        if type(reconciled_effect) is not bool:
            raise ValueError("reconciled_effect must be a strict bool")
        if (
            type(new_phase) is not str
            or not new_phase
            or new_phase.strip() != new_phase
        ):
            raise ValueError("new_phase must be a non-empty trimmed string")
        if (
            type(state_digest) is not str
            or "\n" in state_digest
            or "\r" in state_digest
        ):
            raise ValueError("state_digest must be a single-line string")
        if type(transient_error) is not bool:
            raise ValueError("transient_error must be a strict bool")

        if transient_error:
            self.transient_error_count += 1
            self.last_progress_digest = state_digest
            if self.transient_error_count > self.max_transient_retries:
                raise RecoveryStalledError(
                    f"transient error retry limit exceeded for operation "
                    f"{self.operation_id!r} in phase {self.recovery_phase!r}: "
                    f"{self.transient_error_count} > {self.max_transient_retries}",
                    stall_count=self.stall_count,
                    transient_error_count=self.transient_error_count,
                    recovery_phase=self.recovery_phase,
                    last_progress_digest=self.last_progress_digest,
                    diagnostic="v6_transient_retries_exhausted",
                )
            return

        phase_advanced = new_phase != self.recovery_phase
        made_progress = new_records_count > 0 or reconciled_effect or phase_advanced
        self.recovery_phase = new_phase
        self.last_progress_digest = state_digest
        if made_progress:
            self.stall_count = 0
            return

        self.stall_count += 1
        if self.stall_count >= self.max_stall_steps:
            raise RecoveryStalledError(
                f"recovery stalled for operation {self.operation_id!r} in phase "
                f"{self.recovery_phase!r}: {self.stall_count} consecutive steps "
                "without new verified records, reconciled effects, or phase advance",
                stall_count=self.stall_count,
                transient_error_count=self.transient_error_count,
                recovery_phase=self.recovery_phase,
                last_progress_digest=self.last_progress_digest,
            )


@dataclass(frozen=True)
class PaginationResult:
    """Result of a stability-verified, progress-guarded pagination scan."""

    records: tuple[PaginatedRecord, ...]
    pages_fetched: int
    resumed_from_cursor: bool
    rescanned: bool
    verified_prefix_count: int
    verified_prefix_digest: str
    last_verified_id: str | int | None
    updated_hint: AdvisoryProgressHint


def _normalize_and_validate_rest_path(raw_path_or_url: str, *, endpoint_id: str) -> str:
    if not isinstance(raw_path_or_url, str) or not raw_path_or_url.strip():
        raise ReviewInputError(
            "REST pagination path must be a non-empty string",
            diagnostic="v6_invalid_pagination_path",
        )
    parts = urlsplit(raw_path_or_url.strip())
    path = parts.path
    if not path.startswith("/"):
        raise ReviewInputError(
            f"REST pagination path must start with '/': {raw_path_or_url!r}",
            diagnostic="v6_invalid_pagination_path",
        )
    query_pairs = parse_qsl(parts.query, keep_blank_values=True)
    query_map: dict[str, str] = {}
    for key, value in query_pairs:
        lower_key = key.lower()
        if lower_key == "since":
            raise ReviewInputError(
                "REST pagination forbids 'since=' parameter because comment edits "
                "reorder results non-deterministically",
                diagnostic="v6_since_parameter_forbidden",
            )
        if lower_key == "sort" and value.lower() != "created":
            raise ReviewInputError(
                f"REST pagination requires sort=created, got sort={value!r}",
                diagnostic="v6_non_ascending_pagination",
            )
        if lower_key == "direction" and value.lower() != "asc":
            raise ReviewInputError(
                f"REST pagination requires direction=asc, got direction={value!r}",
                diagnostic="v6_non_ascending_pagination",
            )
        query_map[key] = value

    if endpoint_id in _SORTABLE_ASC_ENDPOINTS:
        if not any(k.lower() == "sort" for k in query_map):
            query_pairs.append(("sort", "created"))
        if not any(k.lower() == "direction" for k in query_map):
            query_pairs.append(("direction", "asc"))

    rebuilt_query = urlencode(query_pairs)
    return urlunsplit(("", "", path, rebuilt_query, ""))


def _default_rest_record_extractor(
    body: Any,
) -> tuple[PaginatedRecord, ...]:
    if not isinstance(body, Sequence) or isinstance(body, (str, bytes, bytearray)):
        raise ReviewInputError(
            "expected JSON array body for REST pagination page",
            diagnostic="v6_invalid_pagination_body",
        )
    records: list[PaginatedRecord] = []
    for item in body:
        if not isinstance(item, Mapping):
            raise ReviewInputError(
                "expected mapping entries in REST pagination page",
                diagnostic="v6_invalid_pagination_body",
            )
        record_id = item.get("id")
        if record_id is None:
            record_id = item.get("node_id")
        records.append(PaginatedRecord.from_payload(record_id, item))  # type: ignore[arg-type]
    return tuple(records)


def _default_graphql_page_extractor(
    response: DualCredentialResponse,
) -> tuple[tuple[PaginatedRecord, ...], str | None, bool]:
    body = response.body
    if not isinstance(body, Mapping):
        raise ReviewInputError(
            "expected mapping body for GraphQL pagination page",
            diagnostic="v6_invalid_pagination_body",
        )
    data = body.get("data")
    if not isinstance(data, Mapping):
        raise ReviewInputError(
            "GraphQL pagination response missing 'data' mapping",
            diagnostic="v6_invalid_pagination_body",
        )
    connection = data.get("connection")
    if not isinstance(connection, Mapping):
        for key, value in data.items():
            if key != "rateLimit" and isinstance(value, Mapping):
                connection = value
                break
    if not isinstance(connection, Mapping):
        raise ReviewInputError(
            "GraphQL pagination response missing connection mapping",
            diagnostic="v6_invalid_pagination_body",
        )
    nodes = connection.get("nodes")
    if not isinstance(nodes, Sequence) or isinstance(nodes, (str, bytes, bytearray)):
        raise ReviewInputError(
            "GraphQL pagination connection missing 'nodes' list",
            diagnostic="v6_invalid_pagination_body",
        )
    records: list[PaginatedRecord] = []
    for item in nodes:
        if not isinstance(item, Mapping):
            raise ReviewInputError(
                "GraphQL connection node must be a mapping",
                diagnostic="v6_invalid_pagination_body",
            )
        record_id = item.get("id") or item.get("databaseId")
        records.append(PaginatedRecord.from_payload(record_id, item))  # type: ignore[arg-type]

    page_info = connection.get("pageInfo")
    if not isinstance(page_info, Mapping):
        raise ReviewInputError(
            "GraphQL pagination connection missing 'pageInfo'",
            diagnostic="v6_invalid_pagination_body",
        )
    has_next = bool(page_info.get("hasNextPage", False))
    end_cursor = page_info.get("endCursor")
    if end_cursor is not None and not isinstance(end_cursor, str):
        raise ReviewInputError(
            "GraphQL pageInfo.endCursor must be str or None",
            diagnostic="v6_invalid_pagination_body",
        )
    return tuple(records), end_cursor, has_next


class StabilityVerifiedPaginator:
    """Pagination engine over ``WORKFLOW_GITHUB_TOKEN`` with stability verification."""

    def __init__(self, client: DualCredentialGitHubClient) -> None:
        if not isinstance(client, DualCredentialGitHubClient):
            raise TypeError("client must be a DualCredentialGitHubClient")
        self.client = client

    def verify_resume_stability(
        self,
        *,
        mode: str,
        cursor_hint: str | None,
        verified_prefix_count: int,
        verified_prefix_digest: str,
        current_total_count: int | None = None,
        current_prefix_digest: str | None = None,
        anchor_id: str | int | None = None,
        anchor_resolved_id: str | int | None = None,
        anchor_content_sha256: str | None = None,
        expected_anchor_sha256: str | None = None,
    ) -> bool:
        """Return ``True`` only when resuming past ``cursor_hint`` is provably safe."""

        if mode not in ("graphql", "rest"):
            raise ValueError(f"unsupported pagination mode: {mode!r}")
        if (
            cursor_hint is None
            or not isinstance(cursor_hint, str)
            or not cursor_hint
            or verified_prefix_count <= 0
            or not verified_prefix_digest
        ):
            return False

        # REST offset pagination (page=N) shifts left if any earlier record is deleted.
        if mode == "rest":
            return False

        if (
            type(current_total_count) is not int
            or current_total_count < verified_prefix_count
        ):
            return False
        if (
            not isinstance(current_prefix_digest, str)
            or not current_prefix_digest
            or not hmac.compare_digest(current_prefix_digest, verified_prefix_digest)
        ):
            return False
        if anchor_id is not None:
            if anchor_resolved_id is None or str(anchor_resolved_id) != str(anchor_id):
                return False
            if (
                expected_anchor_sha256 is not None
                and anchor_content_sha256 != expected_anchor_sha256
            ):
                return False
        return True

    def scan_rest(
        self,
        *,
        endpoint_id: str,
        path: str,
        event: GitHubEventKind,
        visibility: RepositoryVisibility,
        granted_scopes: Collection[str],
        progress_guard: NoProgressGuard,
        advisory_hint: AdvisoryProgressHint | None = None,
        known_records: Iterable[PaginatedRecord] = (),
        record_extractor: Callable[[Any], tuple[PaginatedRecord, ...]] | None = None,
        phase_name: str = "rest_scan",
        now_epoch: float | None = None,
        execute_fn: Callable[..., DualCredentialResponse] | None = None,
    ) -> PaginationResult:
        """Paginate a REST collection in deterministic ascending order (uncapped)."""

        progress_guard.ensure_not_stalled()
        extractor = (
            _default_rest_record_extractor
            if record_extractor is None
            else record_extractor
        )

        # REST offset pagination can never prove prefix stability across earlier deletions.
        rescanned = False
        resumed = False
        if advisory_hint is not None and advisory_hint.cursor_hint is not None:
            stable = self.verify_resume_stability(
                mode="rest",
                cursor_hint=advisory_hint.cursor_hint,
                verified_prefix_count=advisory_hint.verified_prefix_count,
                verified_prefix_digest=advisory_hint.verified_prefix_digest,
            )
            rescanned = not stable
            resumed = stable

        seen_keys: set[tuple[str, str]] = set()
        ordered_records: list[PaginatedRecord] = []
        if resumed:
            for existing in deduplicate_records(known_records):
                seen_keys.add(existing.dedup_key)
                ordered_records.append(existing)

        next_path: str | None = _normalize_and_validate_rest_path(
            path, endpoint_id=endpoint_id
        )
        visited_paths: set[str] = set()
        pages_fetched = 0

        while next_path is not None:
            if next_path in visited_paths:
                progress_guard.record_step(
                    new_records_count=0,
                    reconciled_effect=False,
                    new_phase=phase_name,
                    state_digest=canonical_sha256(
                        {"repeated_url": next_path, "count": len(ordered_records)}
                    ),
                )
                raise RecoveryStalledError(
                    f"Repeated pagination URL detected without progress: {next_path!r}",
                    stall_count=progress_guard.stall_count,
                    transient_error_count=progress_guard.transient_error_count,
                    recovery_phase=progress_guard.recovery_phase,
                    last_progress_digest=progress_guard.last_progress_digest,
                )
            visited_paths.add(next_path)

            caller = self.client.execute if execute_fn is None else execute_fn
            response = caller(
                endpoint_id,
                method="GET",
                path=next_path,
                event=event,
                visibility=visibility,
                granted_scopes=granted_scopes,
                credential_kind=CredentialKind.WORKFLOW_GITHUB_TOKEN,
                now_epoch=now_epoch,
            )
            pages_fetched += 1
            page_records = extractor(response.body)
            new_count = 0
            for record in page_records:
                if not isinstance(record, PaginatedRecord):
                    raise TypeError(
                        "record_extractor must yield PaginatedRecord instances"
                    )
                if record.dedup_key not in seen_keys:
                    seen_keys.add(record.dedup_key)
                    ordered_records.append(record)
                    new_count += 1

            link_next = response.link_next_url
            normalized_next = (
                _normalize_and_validate_rest_path(link_next, endpoint_id=endpoint_id)
                if link_next is not None
                else None
            )
            prefix_digest = compute_records_prefix_digest(ordered_records)
            progress_guard.verified_prefix_count = len(ordered_records)
            progress_guard.verified_prefix_digest = prefix_digest
            progress_guard.last_verified_id = (
                ordered_records[-1].record_id if ordered_records else None
            )
            progress_guard.cursor_hint = normalized_next

            # Only enforce no-progress stall when pagination continues (or page is empty on loop)
            if normalized_next is not None:
                progress_guard.record_step(
                    new_records_count=new_count,
                    reconciled_effect=False,
                    new_phase=phase_name,
                    state_digest=prefix_digest,
                )
            elif new_count > 0 or phase_name != progress_guard.recovery_phase:
                progress_guard.record_step(
                    new_records_count=new_count,
                    reconciled_effect=False,
                    new_phase=phase_name,
                    state_digest=prefix_digest,
                )

            next_path = normalized_next

        final_records = tuple(ordered_records)
        final_digest = compute_records_prefix_digest(final_records)
        last_id = final_records[-1].record_id if final_records else None
        progress_guard.verified_prefix_count = len(final_records)
        progress_guard.verified_prefix_digest = final_digest
        progress_guard.last_verified_id = last_id
        progress_guard.cursor_hint = None

        return PaginationResult(
            records=final_records,
            pages_fetched=pages_fetched,
            resumed_from_cursor=resumed,
            rescanned=rescanned,
            verified_prefix_count=len(final_records),
            verified_prefix_digest=final_digest,
            last_verified_id=last_id,
            updated_hint=progress_guard.to_advisory_hint(),
        )

    def scan_graphql(
        self,
        *,
        endpoint_id: str = "graphql_review_threads",
        query: str,
        variables: Mapping[str, Any] | None = None,
        event: GitHubEventKind,
        visibility: RepositoryVisibility,
        granted_scopes: Collection[str],
        progress_guard: NoProgressGuard,
        advisory_hint: AdvisoryProgressHint | None = None,
        known_records: Iterable[PaginatedRecord] = (),
        current_total_count: int | None = None,
        current_prefix_digest: str | None = None,
        anchor_resolved_id: str | int | None = None,
        anchor_content_sha256: str | None = None,
        expected_anchor_sha256: str | None = None,
        page_extractor: (
            Callable[
                [DualCredentialResponse],
                tuple[tuple[PaginatedRecord, ...], str | None, bool],
            ]
            | None
        ) = None,
        phase_name: str = "graphql_scan",
        now_epoch: float | None = None,
        execute_fn: Callable[..., DualCredentialResponse] | None = None,
    ) -> PaginationResult:
        """Paginate a GraphQL connection forward on ``WORKFLOW_GITHUB_TOKEN``."""

        progress_guard.ensure_not_stalled()
        if not isinstance(query, str) or not query.strip():
            raise ReviewInputError(
                "GraphQL query must be a non-empty string",
                diagnostic="v6_invalid_pagination_query",
            )
        extractor = (
            _default_graphql_page_extractor
            if page_extractor is None
            else page_extractor
        )

        resumed = False
        rescanned = False
        cursor: str | None = None
        seen_keys: set[tuple[str, str]] = set()
        ordered_records: list[PaginatedRecord] = []

        if advisory_hint is not None and advisory_hint.cursor_hint is not None:
            stable = self.verify_resume_stability(
                mode="graphql",
                cursor_hint=advisory_hint.cursor_hint,
                verified_prefix_count=advisory_hint.verified_prefix_count,
                verified_prefix_digest=advisory_hint.verified_prefix_digest,
                current_total_count=current_total_count,
                current_prefix_digest=current_prefix_digest,
                anchor_id=advisory_hint.last_verified_id,
                anchor_resolved_id=anchor_resolved_id,
                anchor_content_sha256=anchor_content_sha256,
                expected_anchor_sha256=expected_anchor_sha256,
            )
            if stable:
                resumed = True
                cursor = advisory_hint.cursor_hint
                for existing in deduplicate_records(known_records):
                    seen_keys.add(existing.dedup_key)
                    ordered_records.append(existing)
            else:
                rescanned = True

        base_variables = dict(variables or {})
        pages_fetched = 0
        seen_cursors: set[str] = set()

        while True:
            page_variables = dict(base_variables)
            page_variables["after"] = cursor
            caller = self.client.execute if execute_fn is None else execute_fn
            response = caller(
                endpoint_id,
                method="POST",
                path="/graphql",
                event=event,
                visibility=visibility,
                granted_scopes=granted_scopes,
                credential_kind=CredentialKind.WORKFLOW_GITHUB_TOKEN,
                body={"query": query, "variables": page_variables},
                is_graphql=True,
                now_epoch=now_epoch,
            )
            pages_fetched += 1
            page_records, end_cursor, has_next = extractor(response)
            new_count = 0
            for record in page_records:
                if not isinstance(record, PaginatedRecord):
                    raise TypeError(
                        "page_extractor must yield PaginatedRecord instances"
                    )
                if record.dedup_key not in seen_keys:
                    seen_keys.add(record.dedup_key)
                    ordered_records.append(record)
                    new_count += 1

            prefix_digest = compute_records_prefix_digest(ordered_records)
            progress_guard.verified_prefix_count = len(ordered_records)
            progress_guard.verified_prefix_digest = prefix_digest
            progress_guard.last_verified_id = (
                ordered_records[-1].record_id if ordered_records else None
            )
            progress_guard.cursor_hint = end_cursor if has_next else None

            repeated_cursor = end_cursor is not None and (
                end_cursor == cursor or end_cursor in seen_cursors
            )
            if end_cursor is not None:
                seen_cursors.add(end_cursor)

            effective_new = 0 if repeated_cursor else new_count
            if has_next:
                progress_guard.record_step(
                    new_records_count=effective_new,
                    reconciled_effect=False,
                    new_phase=phase_name,
                    state_digest=prefix_digest,
                )
            elif effective_new > 0 or phase_name != progress_guard.recovery_phase:
                progress_guard.record_step(
                    new_records_count=effective_new,
                    reconciled_effect=False,
                    new_phase=phase_name,
                    state_digest=prefix_digest,
                )

            if not has_next or end_cursor is None:
                break
            cursor = end_cursor

        final_records = tuple(ordered_records)
        final_digest = compute_records_prefix_digest(final_records)
        last_id = final_records[-1].record_id if final_records else None
        progress_guard.verified_prefix_count = len(final_records)
        progress_guard.verified_prefix_digest = final_digest
        progress_guard.last_verified_id = last_id
        progress_guard.cursor_hint = None

        return PaginationResult(
            records=final_records,
            pages_fetched=pages_fetched,
            resumed_from_cursor=resumed,
            rescanned=rescanned,
            verified_prefix_count=len(final_records),
            verified_prefix_digest=final_digest,
            last_verified_id=last_id,
            updated_hint=progress_guard.to_advisory_hint(),
        )

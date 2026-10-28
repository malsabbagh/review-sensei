"""Bounded GitHub REST HTTP client for publisher adapters."""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from review_sensei.errors import ReviewInputError
from review_sensei.evidence import EvidenceBundle, EvidenceRecord, EvidenceSnapshot
from review_sensei.validation import validate_repository_path

from .errors import (
    GitHubHTTPError,
    GitHubHTTPPaginationLimitError,
    GitHubHTTPResponseTooLargeError,
    GitHubHTTPTransientError,
)

MAX_GITHUB_RESPONSE_BYTES = 512 * 1024
MAX_PAGINATION_PAGES = 10
MAX_PAGINATION_ITEMS = MAX_PAGINATION_PAGES * 100
# Ten-item pages avoid repeated five-item rescans for histories clustered near
# GitHub's individual comment/review body limit. Fifty still admits long lists.
HISTORY_PAGE_SIZES = (100, 50, 25, 10, 5, 1)
_SAFE_SEGMENT = re.compile(r"[A-Za-z0-9._-]+")

Opener = Callable[..., Any]


def _completed_json_objects(raw: bytes | bytearray) -> int:
    """Count closed object tokens in an already bounded prefix, ignoring strings.

    This is a performance hint, never a partial JSON parser or source of data.
    """
    quoted = escaped = False
    count = 0
    for value in raw:
        if quoted:
            if escaped:
                escaped = False
            elif value == 92:
                escaped = True
            elif value == 34:
                quoted = False
        elif value == 34:
            quoted = True
        elif value == 125:
            count += 1
    return count


class GitHubHttpTransport(Protocol):
    """Protocol for the bounded GitHub REST transport."""

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str,
        body: dict[str, object] | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[int, dict[str, Any] | list[Any] | None]:
        """Return (status, parsed JSON body) for one bounded request."""


class BoundedGraphQLPages:
    """Retry one read cursor at smaller sizes within the original walk budget."""

    def __init__(
        self, http: GitHubHttpTransport, *, max_requests: int = MAX_PAGINATION_PAGES
    ) -> None:
        self.http = http
        self.max_requests = max_requests
        self.requests = 0
        self.size_index = 0

    @property
    def page_size(self) -> int:
        return (100, 50, 25, 5, 1)[self.size_index]

    def request(
        self, method: str, path: str, *, token: str, body: dict[str, Any]
    ) -> tuple[int, dict[str, Any] | list[Any] | None]:
        if (
            method != "POST"
            or path != "/graphql"
            or not isinstance(body.get("query"), str)
            or not body["query"].lstrip().startswith("query ")
            or not isinstance(body.get("variables"), dict)
        ):
            raise GitHubHTTPError("GitHub paginated query is invalid")
        while self.requests < self.max_requests:
            self.requests += 1
            submitted = {
                **body,
                "variables": {
                    **body["variables"],
                    "first": self.page_size,
                },
            }
            try:
                return self.http.request(method, path, token=token, body=submitted)
            except GitHubHTTPResponseTooLargeError:
                if self.page_size == 1:
                    raise
                self.size_index += 1
        raise GitHubHTTPPaginationLimitError("GitHub query exhausted its read budget")


def _validated_owner(owner: str) -> str:
    if not isinstance(owner, str) or _SAFE_SEGMENT.fullmatch(owner) is None:
        raise GitHubHTTPError("GitHub owner is invalid")
    return owner


def _validated_repo(repo: str) -> str:
    if not isinstance(repo, str) or _SAFE_SEGMENT.fullmatch(repo) is None:
        raise GitHubHTTPError("GitHub repository is invalid")
    return repo


def _read_allowance(before_read: Callable[[], float]) -> float:
    """Charge the host-owned shared envelope before each physical GET attempt."""
    remaining = before_read()
    if (
        isinstance(remaining, bool)
        or not isinstance(remaining, (int, float))
        or not math.isfinite(remaining)
        or not 0 < remaining <= 60
    ):
        raise GitHubHTTPPaginationLimitError(
            "GitHub shared read allowance is invalid or exhausted"
        )
    return remaining


class GitHubHttp:
    """Strict UTF-8/JSON GitHub REST client with capped pagination."""

    def __init__(
        self,
        *,
        api_url: str = "https://api.github.com",
        opener: Opener = urlopen,
        timeout: int = 30,
    ) -> None:
        if not isinstance(api_url, str) or not api_url.strip():
            raise GitHubHTTPError("GitHub API URL must be non-empty")
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
            raise GitHubHTTPError("GitHub HTTP timeout must be positive")
        self.api_url = api_url.rstrip("/")
        self.opener = opener
        self.timeout = timeout
        self.primed_issue_comments: dict[str, list[Any]] = {}

    def repository_path(self, repository: str, suffix: str = "") -> str:
        owner, _, name = repository.partition("/")
        return (
            f"/repos/{quote(_validated_owner(owner), safe='')}/"
            f"{quote(_validated_repo(name), safe='')}{suffix}"
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str,
        body: dict[str, object] | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[int, dict[str, Any] | list[Any] | None]:
        if not isinstance(path, str) or not path.startswith("/") or "\n" in path:
            raise GitHubHTTPError("GitHub request path is invalid")
        if not isinstance(token, str) or not token.strip():
            raise GitHubHTTPError("GitHub installation token is empty")
        if body is not None and not isinstance(body, dict):
            raise GitHubHTTPError("GitHub request body must be an object")
        payload = None
        if body is not None:
            payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "ReviewSensei-GitHub-App/1.0 (+https://reviewsensei.dev)",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.api_url}{path}",
            data=payload,
            headers=headers,
            method=method,
        )
        timeout: float = self.timeout
        if timeout_seconds is not None:
            if (
                isinstance(timeout_seconds, bool)
                or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds)
                or timeout_seconds <= 0
            ):
                raise GitHubHTTPError("GitHub request timeout is invalid")
            timeout = min(timeout, timeout_seconds)
        try:
            with self.opener(request, timeout=timeout) as response:
                raw = response.read(MAX_GITHUB_RESPONSE_BYTES + 1)
                if not isinstance(raw, (bytes, bytearray)):
                    raise GitHubHTTPError("GitHub response was invalid")
                if len(raw) > MAX_GITHUB_RESPONSE_BYTES:
                    raise GitHubHTTPResponseTooLargeError(
                        "GitHub response exceeded the configured size limit",
                        completed_json_objects=_completed_json_objects(raw),
                    )
                status = int(getattr(response, "status", 200))
                return status, self._parse_body(bytes(raw))
        except HTTPError as exc:
            self._read_bounded_error_body(exc)
            # Preserve the status as data so callers can reconcile ambiguous
            # write responses (409/422/429/5xx) without exposing the bounded
            # error body.  Network failures remain typed exceptions below.
            return exc.code, None
        except (URLError, TimeoutError) as exc:
            raise GitHubHTTPTransientError("GitHub request failed") from exc
        except OSError as exc:
            if "timed out" in str(exc).lower():
                raise GitHubHTTPTransientError("GitHub request timed out") from exc
            raise GitHubHTTPTransientError("GitHub request failed") from exc

    @staticmethod
    def _parse_body(raw: bytes) -> dict[str, Any] | list[Any] | None:
        if not raw:
            return None
        try:
            parsed = json.loads(raw.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubHTTPError("GitHub response was not valid UTF-8 JSON") from exc
        if not isinstance(parsed, (dict, list)):
            raise GitHubHTTPError("GitHub response must be a JSON object or array")
        return parsed

    @staticmethod
    def _read_bounded_error_body(exc: HTTPError) -> str:
        try:
            raw = exc.read(MAX_GITHUB_RESPONSE_BYTES + 1)
            if isinstance(raw, (bytes, bytearray)):
                return bytes(raw)[:MAX_GITHUB_RESPONSE_BYTES].decode(
                    "utf-8", errors="replace"
                )
        except OSError:
            pass
        return ""

    def pagination_page_size(self, path: str, offset: int = 0) -> int:
        """Propagate an operation-local size hint through audited wrappers.

        This carries no response bytes or completeness/authority decision.
        Every scan still dispatches and proves its own bounded final page.
        """
        transport = self.__dict__.get("transport")
        reader = getattr(transport, "pagination_page_size", None)
        hint = reader(path, offset) if callable(reader) else 100
        return hint if type(hint) is int and 1 <= hint <= 100 else 100

    def paginate(
        self,
        *,
        path: str,
        token: str,
        page_sizes: tuple[int, ...] | None = None,
        max_requests: int | None = None,
        timeout_seconds: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        expected_items: int | None = None,
        before_read: Callable[[], float] | None = None,
    ) -> list[Any]:
        from .caller_operation import http_read_operation

        with http_read_operation(self, "pagination:" + path):
            return self._paginate(
                path=path,
                token=token,
                page_sizes=page_sizes,
                max_requests=max_requests,
                timeout_seconds=timeout_seconds,
                monotonic=monotonic,
                expected_items=expected_items,
                before_read=before_read,
            )

    def _paginate(
        self,
        *,
        path: str,
        token: str,
        page_sizes: tuple[int, ...] | None = None,
        max_requests: int | None = None,
        timeout_seconds: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        expected_items: int | None = None,
        before_read: Callable[[], float] | None = None,
    ) -> list[Any]:
        """Return at most ``MAX_PAGINATION_ITEMS`` list entries.

        Callers that handle large, untrusted list entries may provide strictly
        descending page sizes. If a page exceeds the response budget, the same
        item offset is retried with the next smaller size that aligns with it.
        After a smaller page succeeds, the next aligned offset uses the largest page size that divides it, so one
        oversized page does not pin the rest of the list at the smaller size.
        """

        # The ordinary list reader previously admitted at most ten 100-item
        # pages. Keep that physical allowance when recovering oversized pages.
        # Explicit adaptive callers retain their separately owned envelopes.
        if page_sizes is None:
            page_sizes = HISTORY_PAGE_SIZES
            if max_requests is None:
                max_requests = MAX_PAGINATION_PAGES
        if not page_sizes or any(
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or page_size < 1
            or page_size > 100
            for page_size in page_sizes
        ):
            raise GitHubHTTPError(
                "GitHub pagination page sizes must be integers from 1 through 100"
            )
        if any(
            current <= following
            for current, following in zip(page_sizes, page_sizes[1:])
        ):
            raise GitHubHTTPError("GitHub pagination page sizes must descend strictly")

        if max_requests is not None and (
            isinstance(max_requests, bool)
            or not isinstance(max_requests, int)
            or not 1 <= max_requests <= MAX_PAGINATION_ITEMS + len(page_sizes)
        ):
            raise GitHubHTTPError("GitHub pagination request budget is invalid")
        if expected_items is not None and (
            isinstance(expected_items, bool)
            or not isinstance(expected_items, int)
            or not 0 <= expected_items <= MAX_PAGINATION_ITEMS
        ):
            raise GitHubHTTPPaginationLimitError(
                "GitHub expected inventory exceeds the item budget"
            )
        if timeout_seconds is not None and (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise GitHubHTTPError("GitHub pagination timeout is invalid")
        deadline = (
            monotonic() + timeout_seconds if timeout_seconds is not None else math.inf
        )
        requests = 0
        collected: list[Any] = []
        separator = "&" if "?" in path else "?"
        page_size_index = 0
        while len(collected) < MAX_PAGINATION_ITEMS or (
            expected_items == MAX_PAGINATION_ITEMS
            and len(collected) == MAX_PAGINATION_ITEMS
        ):
            hint = self.pagination_page_size(path, len(collected))
            page_size_index = next(
                (
                    index
                    for index in range(page_size_index, len(page_sizes))
                    if page_sizes[index] <= hint
                    and len(collected) % page_sizes[index] == 0
                ),
                page_size_index,
            )
            page_size = page_sizes[page_size_index]
            if len(collected) % page_size:
                raise GitHubHTTPError("GitHub pagination offset was invalid")
            page = len(collected) // page_size + 1
            current = f"{path}{separator}per_page={page_size}&page={page}"
            remaining = deadline - monotonic() if math.isfinite(deadline) else None
            if (max_requests is not None and requests >= max_requests) or (
                remaining is not None and remaining <= 0
            ):
                raise GitHubHTTPPaginationLimitError(
                    "GitHub pagination exhausted its request or time budget"
                )
            requests += 1
            if before_read is not None:
                shared_remaining = _read_allowance(before_read)
                deadline = min(deadline, monotonic() + shared_remaining)
                remaining = (
                    min(remaining, shared_remaining)
                    if remaining is not None
                    else shared_remaining
                )
            try:
                if remaining is None:
                    status, body = self.request("GET", current, token=token)
                else:
                    status, body = self.request(
                        "GET", current, token=token, timeout_seconds=remaining
                    )
            except GitHubHTTPResponseTooLargeError:
                if page_size_index + 1 >= len(page_sizes):
                    raise
                next_index = next(
                    (
                        index
                        for index in range(page_size_index + 1, len(page_sizes))
                        if len(collected) % page_sizes[index] == 0
                    ),
                    None,
                )
                if next_index is None:
                    raise GitHubHTTPError("GitHub pagination offset was invalid")
                page_size_index = next_index
                continue
            if monotonic() >= deadline:
                raise GitHubHTTPPaginationLimitError(
                    "GitHub pagination exhausted its time budget"
                )
            if status == 404:
                raise GitHubHTTPError("GitHub pagination target was not found")
            if status < 200 or status >= 300:
                raise GitHubHTTPError("GitHub pagination request was rejected")
            if not isinstance(body, list):
                raise GitHubHTTPError("GitHub pagination response was invalid")
            if len(body) > page_size:
                raise GitHubHTTPError(
                    "GitHub pagination page exceeded its requested size"
                )
            if len(collected) + len(body) > MAX_PAGINATION_ITEMS or (
                expected_items is not None
                and len(collected) + len(body) > expected_items
            ):
                raise GitHubHTTPPaginationLimitError(
                    "GitHub pagination exceeded its expected inventory"
                )
            collected.extend(body)
            if len(body) < page_size:
                if expected_items is not None and len(collected) != expected_items:
                    raise GitHubHTTPError("GitHub pagination inventory count changed")
                return collected
            page_size_index = next(
                index
                for index, size in enumerate(page_sizes)
                if len(collected) % size == 0
            )
        raise GitHubHTTPPaginationLimitError(
            "GitHub pagination exceeded configured page limit"
        )

    def load_review_evidence(
        self,
        *,
        token: str,
        snapshot: EvidenceSnapshot,
        required_paths: tuple[str, ...],
        include_all_changed: bool = False,
        timeout_seconds: float = 60,
        max_requests: int = 64,
        monotonic: Callable[[], float] = time.monotonic,
        before_read: Callable[[], float] | None = None,
    ) -> EvidenceBundle:
        """Read one exhaustive file inventory between exact snapshot/count fences.

        Both fences, oversized-page retries and the optional item-limit EOF probe
        share the original 64-read/60-second envelope. Failure raises a sanitized
        error; callers must retain pending obligations. No partial bundle escapes.
        The returned bundle may contain explicitly incomplete/binary patches.
        """
        if (
            not isinstance(snapshot, EvidenceSnapshot)
            or snapshot.repository is None
            or snapshot.pull_request is None
            or snapshot.base_sha is None
            or snapshot.head_sha is None
            or not isinstance(required_paths, tuple)
            or len(required_paths) > 64
            or any(not isinstance(path, str) for path in required_paths)
            or not isinstance(include_all_changed, bool)
        ):
            raise GitHubHTTPError("GitHub evidence acquisition scope is invalid")
        if len(set(required_paths)) != len(required_paths):
            raise GitHubHTTPError("GitHub evidence acquisition scope is duplicated")
        for path in required_paths:
            validate_repository_path(
                path, label="review evidence path", allow_glob_chars=True
            )
        if (
            isinstance(max_requests, bool)
            or not isinstance(max_requests, int)
            or not 3 <= max_requests <= 64
            or isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 60
        ):
            raise GitHubHTTPError("GitHub evidence acquisition budget is invalid")
        deadline = monotonic() + timeout_seconds
        reads = 0

        def remaining() -> float:
            value = deadline - monotonic()
            if value <= 0:
                raise GitHubHTTPPaginationLimitError(
                    "GitHub evidence acquisition exhausted its time budget"
                )
            # Float addition/subtraction at high uptime can round above the
            # original timeout. Bound this one positive sample, not the deadline.
            return min(value, timeout_seconds)

        def charge() -> float:
            nonlocal reads, deadline
            remaining()
            if reads >= max_requests:
                raise GitHubHTTPPaginationLimitError(
                    "GitHub evidence acquisition exhausted its read budget"
                )
            reads += 1
            if before_read is not None:
                shared = _read_allowance(before_read)
                deadline = min(deadline, monotonic() + shared)
                return min(remaining(), shared)
            return remaining()

        pr_path = self.repository_path(
            snapshot.repository, f"/pulls/{snapshot.pull_request}"
        )

        def fence() -> int:
            allowance = charge()
            status, pr = self.request(
                "GET", pr_path, token=token, timeout_seconds=min(remaining(), allowance)
            )
            remaining()
            if (
                status != 200
                or not isinstance(pr, dict)
                or not isinstance(pr.get("base"), dict)
                or not isinstance(pr.get("head"), dict)
                or pr["base"].get("sha") != snapshot.base_sha
                or pr["head"].get("sha") != snapshot.head_sha
            ):
                raise GitHubHTTPError(
                    "GitHub evidence snapshot changed or is unavailable"
                )
            count = pr.get("changed_files")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise GitHubHTTPError("GitHub evidence inventory count is invalid")
            if count > MAX_PAGINATION_ITEMS:
                raise GitHubHTTPPaginationLimitError(
                    "GitHub evidence inventory exceeds the item budget"
                )
            return count

        expected_count = fence()
        files = self.paginate(
            path=pr_path + "/files",
            token=token,
            page_sizes=(100, 50, 25, 5, 1),
            max_requests=max_requests - 2,
            timeout_seconds=remaining(),
            monotonic=monotonic,
            expected_items=expected_count,
            before_read=charge,
        )
        if fence() != expected_count:
            raise GitHubHTTPError("GitHub evidence inventory count changed")
        records = []
        seen: set[str] = set()
        try:
            for item in files:
                if not isinstance(item, dict) or not isinstance(
                    item.get("filename"), str
                ):
                    raise GitHubHTTPError("GitHub evidence file inventory is invalid")
                path = item["filename"]
                validate_repository_path(
                    path, label="review evidence path", allow_glob_chars=True
                )
                if path in seen:
                    raise GitHubHTTPError(
                        "GitHub evidence file inventory is duplicated"
                    )
                seen.add(path)
                # Validate all entries before selecting required paths. Enumeration
                # cannot be certified by ignoring malformed unrelated entries.
                patch = item.get("patch")
                additions = item.get("additions")
                deletions = item.get("deletions")
                record = EvidenceRecord(
                    path,
                    patch if isinstance(patch, str) else "",
                    snapshot,
                    supplied_complete=isinstance(patch, str)
                    and bool(patch.strip())
                    and isinstance(additions, int)
                    and not isinstance(additions, bool)
                    and isinstance(deletions, int)
                    and not isinstance(deletions, bool),
                    old_path=item.get("previous_filename"),
                    expected_additions=additions,
                    expected_deletions=deletions,
                )
                if include_all_changed or path in required_paths:
                    records.append(record)
            bundle = EvidenceBundle(snapshot, tuple(records))
        except ReviewInputError as exc:
            raise GitHubHTTPError(
                "GitHub evidence file patches conflict or exceed bounds"
            ) from exc
        remaining()
        return bundle

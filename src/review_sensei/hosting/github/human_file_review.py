"""Opt-in, exact-snapshot human binary-file review and metadata-only audit.

No existing eligibility marker is rewritten and this adapter never APPROVEs.
An explicit host may evaluate combined coverage after fresh live authentication.
Default workflows and the legacy approval finalizer remain unchanged.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, cast
from urllib.parse import quote

from ...errors import ReviewInputError
from ...human_file_review import (
    MAX_DOCUMENT_BYTES,
    MAX_FILES,
    MAX_SOURCE_BYTES,
    FileReviewReceipt,
    FileReviewRequest,
    UnsupportedFile,
    canonical,
    display_path,
    is_blanket_media_approval,
    parse_confirmation,
    positive,
    sha,
    validate_inventory,
)
from ...mixed_coverage import BinaryConfirmation, evaluate_mixed_coverage
from ...models import ReviewResult
from .approval import (
    AutoApprovalDecision,
    ReviewApprovalEligibility,
    approval_facts_from_result,
    evaluate_approval_facts,
)
from .http import GitHubHttpTransport
from .publication import (
    APPROVAL_ELIGIBILITY_MARKER_PREFIX,
    approval_eligibility_from_body,
    review_result_digest,
)

REQUEST_PREFIX = "<!-- reviewsensei:human-files:request:v1 "
RECEIPT_PREFIX = "<!-- reviewsensei:human-files:receipt:v1 "
MAX_BODY_BYTES = 65536
MAX_PAGES = 10


@dataclass(frozen=True)
class HumanFileReviewPolicy:
    """Trusted host configuration; neither repository prose nor a model sets it."""

    allow_confirmations: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.allow_confirmations, bool):
            raise ReviewInputError("human file review policy is invalid")


@dataclass(frozen=True)
class HumanFileReviewAssessment:
    """Freshly evaluated status, not a durable approval grant or AI result."""

    request: FileReviewRequest
    receipt_ids: tuple[int, ...]
    human_reviewed_ids: tuple[str, ...]
    pending_ids: tuple[str, ...]
    ai_reviewed_paths: tuple[str, ...]
    invalidated_receipt_ids: tuple[int, ...]
    approval: AutoApprovalDecision

    def render(self) -> str:
        lines = [
            f"Coverage: {len(self.ai_reviewed_paths)} AI-reviewed path(s), "
            f"{len(self.human_reviewed_ids)} human-reviewed binary change(s), "
            f"{len(self.pending_ids)} unsupported binary change(s) pending."
        ]
        if self.invalidated_receipt_ids:
            lines.append(
                f"{len(self.invalidated_receipt_ids)} prior receipt(s) lost current source/permission evidence; retained for audit only."
            )
        reviewed = set(self.human_reviewed_ids)
        for item in self.request.files:
            status = (
                "human-reviewed"
                if item.file_id in reviewed
                else "unsupported; pending human review"
            )
            lines.append(f"- {display_path(item.path)}: {status}; not AI-reviewed.")
        return "\n".join(lines)


def _marker(prefix: str, value: object) -> str:
    raw = canonical(value).encode("utf-8")
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise ReviewInputError("human file audit document exceeds its bound")
    return prefix + base64.urlsafe_b64encode(raw).decode("ascii") + " -->"


def _body_size(body: str) -> int:
    try:
        return len(body.encode("utf-8", errors="strict"))
    except UnicodeError:
        raise ReviewInputError("human file body is not strict UTF-8") from None


def _document(body: object, prefix: str) -> object:
    if (
        not isinstance(body, str)
        or _body_size(body) > MAX_BODY_BYTES
        or body.count(prefix) != 1
    ):
        raise ReviewInputError("human file audit marker is invalid")
    start = body.index(prefix) + len(prefix)
    end = body.find(" -->", start)
    if end < start:
        raise ReviewInputError("human file audit marker is invalid")
    try:
        raw = base64.b64decode(body[start:end], altchars=b"-_", validate=True)
        if len(raw) > MAX_DOCUMENT_BYTES:
            raise ValueError
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (ValueError, UnicodeError, binascii.Error):
        raise ReviewInputError("human file audit marker is invalid") from None
    if canonical(value).encode("utf-8") != raw:
        raise ReviewInputError("human file audit encoding is noncanonical")
    return value


class HumanFileReviewPublisher:
    """Functional library seam; callers supply authentication and ONE IO budget.

    Every GET/POST is charged before dispatch. HTTP failures and unknown POST
    outcomes are not retried. A later explicit read reconciles owned receipts.
    Broker-internal transports are outside this adapter and must be included by
    any future public factory; this is not whole-operation 64/60 qualification.
    """

    def __init__(
        self,
        *,
        http: GitHubHttpTransport,
        api_url: str,
        repository: str,
        repository_id: int,
        pull_request: int,
        app_login: str,
        app_user_id: int,
        token: str,
        before_request: Callable[[], float],
        policy: HumanFileReviewPolicy = HumanFileReviewPolicy(),
    ) -> None:
        # Validate context through the same domain constructor without giving
        # placeholder values any authorizing role.
        FileReviewRequest(
            repository,
            repository_id,
            pull_request,
            "a" * 40,
            "b" * 40,
            1,
            "c" * 64,
            (UnsupportedFile("probe", "added", None, "d" * 40),),
        )
        if (
            not positive(app_user_id)
            or not isinstance(app_login, str)
            or not app_login
            or not isinstance(token, str)
            or not token
            or not callable(before_request)
            or not isinstance(policy, HumanFileReviewPolicy)
            or not isinstance(api_url, str)
            or not api_url.startswith("https://")
        ):
            raise ReviewInputError("human file publisher configuration is invalid")
        self.http, self.api_url = http, api_url.rstrip("/")
        self.repository, self.repository_id, self.pull_request = (
            repository,
            repository_id,
            pull_request,
        )
        self.app_login, self.app_user_id = app_login, app_user_id
        self.token, self.before_request, self.policy = token, before_request, policy
        self.prefix = f"/repos/{repository}"

    def _io(self, method: str, path: str, body: dict[str, object] | None = None) -> Any:
        remaining = self.before_request()
        if (
            isinstance(remaining, bool)
            or not isinstance(remaining, (int, float))
            or not math.isfinite(remaining)
            or not 0 < remaining <= 60
        ):
            raise ReviewInputError("human file original IO budget is exhausted")
        status, value = self.http.request(
            method,
            self.prefix + path,
            token=self.token,
            body=body,
            timeout_seconds=remaining,
        )
        if status not in ({200} if method == "GET" else {201}) or value is None:
            raise ReviewInputError("human file GitHub operation could not be verified")
        if _body_size(canonical(value)) > 512 * 1024:
            raise ReviewInputError("human file GitHub response exceeds its bound")
        return value

    def _object(self, path: str) -> Mapping[str, Any]:
        value = self._io("GET", path)
        if not isinstance(value, Mapping):
            raise ReviewInputError("human file GitHub object is invalid")
        return value

    def _list(self, path: str) -> list[Mapping[str, Any]]:
        values: list[Mapping[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            response = self._io("GET", f"{path}?per_page=100&page={page}")
            if (
                not isinstance(response, list)
                or len(response) > 100
                or any(not isinstance(x, Mapping) for x in response)
            ):
                raise ReviewInputError("human file GitHub enumeration is invalid")
            values.extend(response)
            if len(response) < 100:
                return values
        raise ReviewInputError("human file GitHub enumeration is incomplete")

    def _owned(self, value: Mapping[str, Any]) -> bool:
        user = value.get("user")
        return (
            isinstance(user, Mapping)
            and type(user.get("id")) is int
            and user.get("id") == self.app_user_id
            and user.get("login") == self.app_login
            and user.get("type") == "Bot"
        )

    def _comment(self, identifier: int, *, owned: bool) -> Mapping[str, Any]:
        if not positive(identifier):
            raise ReviewInputError("human file comment ID is invalid")
        x = self._object(f"/issues/comments/{identifier}")
        if (
            type(x.get("id")) is not int
            or x.get("id") != identifier
            or x.get("issue_url")
            != f"{self.api_url}{self.prefix}/issues/{self.pull_request}"
            or not isinstance(x.get("body"), str)
            or (owned and not self._owned(x))
        ):
            raise ReviewInputError(
                "human file comment ownership or location is invalid"
            )
        return x

    def _snapshot(self) -> tuple[str, str]:
        repo = self._object("")
        pr = self._object(f"/pulls/{self.pull_request}")
        base, head = pr.get("base"), pr.get("head")
        if (
            type(repo.get("id")) is not int
            or repo.get("id") != self.repository_id
            or repo.get("full_name") != self.repository
            or pr.get("state") != "open"
            or type(pr.get("number")) is not int
            or pr.get("number") != self.pull_request
            or not isinstance(base, Mapping)
            or not isinstance(head, Mapping)
            or not sha(base.get("sha"))
            or not sha(head.get("sha"))
        ):
            raise ReviewInputError("human file live snapshot is invalid")
        return cast(str, base["sha"]), cast(str, head["sha"])

    def _root(
        self, review_id: int, result: ReviewResult, head: str
    ) -> ReviewApprovalEligibility:
        if not positive(review_id):
            raise ReviewInputError("human file review ID is invalid")
        root = self._object(f"/pulls/{self.pull_request}/reviews/{review_id}")
        if (
            not self._owned(root)
            or type(root.get("id")) is not int
            or root.get("id") != review_id
            or root.get("commit_id") != head
            or root.get("state") != "COMMENTED"
        ):
            raise ReviewInputError("human file review root is not owned or current")
        eligibility = approval_eligibility_from_body(root.get("body"))
        if (
            eligibility is None
            or eligibility.head_sha != head
            or eligibility.result_digest != review_result_digest(result)
            or eligibility.partitioned_root is not None
        ):
            raise ReviewInputError(
                "human file result lacks an authenticated review root"
            )
        candidates = [
            x
            for x in self._list(f"/pulls/{self.pull_request}/reviews")
            if self._owned(x)
            and x.get("commit_id") == head
            and isinstance(x.get("body"), str)
            and APPROVAL_ELIGIBILITY_MARKER_PREFIX in x["body"]
        ]
        if (
            not candidates
            or any(not positive(x.get("id")) for x in candidates)
            or len({x["id"] for x in candidates}) != len(candidates)
            or max(candidates, key=lambda x: x["id"]) != root
        ):
            raise ReviewInputError("human file review root is superseded or ambiguous")
        return eligibility

    def _blobs(
        self, commit: str, paths: tuple[str, ...]
    ) -> dict[str, tuple[str, str] | None]:
        node = self._object(f"/git/commits/{commit}")
        tree = node.get("tree")
        if (
            node.get("sha") != commit
            or not isinstance(tree, Mapping)
            or not sha(tree.get("sha"))
        ):
            raise ReviewInputError("human file immutable commit evidence is missing")
        trees: dict[str, list[Mapping[str, Any]]] = {}

        def entries(identifier: str) -> list[Mapping[str, Any]]:
            if identifier not in trees:
                data = self._object(f"/git/trees/{identifier}")
                items = data.get("tree")
                if (
                    data.get("sha") != identifier
                    or data.get("truncated") is not False
                    or not isinstance(items, list)
                    or any(not isinstance(x, Mapping) for x in items)
                    or any(
                        not isinstance(x.get("path"), str) or not sha(x.get("sha"))
                        for x in items
                    )
                    or len({x["path"] for x in items}) != len(items)
                ):
                    raise ReviewInputError(
                        "human file immutable tree evidence is incomplete"
                    )
                trees[identifier] = items
            return trees[identifier]

        result: dict[str, tuple[str, str] | None] = {}
        for path in paths:
            identifier = tree["sha"]
            parts = path.split("/")
            found: Mapping[str, Any] | None = None
            for i, part in enumerate(parts):
                found = next(
                    (x for x in entries(identifier) if x["path"] == part), None
                )
                if found is None:
                    break
                if i < len(parts) - 1:
                    if found.get("type") != "tree" or found.get("mode") != "040000":
                        raise ReviewInputError(
                            "human file path is not a regular tree path"
                        )
                    identifier = found["sha"]
                elif (
                    found.get("type") != "blob"
                    or not isinstance(found.get("mode"), str)
                    or found.get("mode") not in {"100644", "100755"}
                ):
                    raise ReviewInputError("human file evidence is not a regular blob")
            result[path] = None if found is None else (found["sha"], found["mode"])
        return result

    def prepare(self, *, review_id: int, result: ReviewResult) -> FileReviewRequest:
        if not isinstance(result, ReviewResult) or result.coverage is None:
            raise ReviewInputError(
                "human file request requires complete coverage inventory"
            )
        paths = {
            x.path
            for x in result.coverage.files
            if x.outcome == "unsupported" and x.reason == "binary"
        }
        if not paths or not result.coverage.enumeration_complete:
            raise ReviewInputError(
                "human file unsupported inventory is missing or incomplete"
            )
        base, head = self._snapshot()
        self._root(review_id, result, head)
        records = self._list(f"/pulls/{self.pull_request}/files")
        if any(
            not isinstance(x.get("filename"), str)
            or not isinstance(x.get("status"), str)
            or (
                x.get("previous_filename") is not None
                and not isinstance(x.get("previous_filename"), str)
            )
            for x in records
        ) or len({x["filename"] for x in records}) != len(records):
            raise ReviewInputError(
                "human file change inventory is incomplete or duplicated"
            )
        changed_paths = {x["filename"] for x in records}
        for x in records:
            if x.get("status") == "renamed":
                if not isinstance(x.get("previous_filename"), str):
                    raise ReviewInputError("human file rename evidence is missing")
                changed_paths.add(x["previous_filename"])
        if changed_paths != set(result.coverage.enumerated_paths):
            raise ReviewInputError("human file result omits or adds changed paths")
        selected = [
            x
            for x in records
            if x.get("filename") in paths or x.get("previous_filename") in paths
        ]
        if not 1 <= len(selected) <= MAX_FILES:
            raise ReviewInputError("human file inventory exceeds its bound")
        all_paths = tuple(sorted(paths))
        old, new = self._blobs(base, all_paths), self._blobs(head, all_paths)
        files: list[UnsupportedFile] = []
        for x in selected:
            path, change, previous = (
                x.get("filename"),
                x.get("status"),
                x.get("previous_filename"),
            )
            if not isinstance(path, str) or not isinstance(change, str):
                raise ReviewInputError("human file change inventory is invalid")
            old_path = previous if change == "renamed" else path
            if not isinstance(old_path, str) or old_path not in old or path not in new:
                raise ReviewInputError(
                    "human file rename or change evidence is missing"
                )
            before, after = old[old_path], new[path]
            item = UnsupportedFile(
                path,
                change,
                before[0] if before else None,
                after[0] if after else None,
                previous if change == "renamed" else None,
                before[1] if before else None,
                after[1] if after else None,
            )
            if change == "renamed" and (
                new[old_path] is not None or old[path] is not None
            ):
                raise ReviewInputError("human file rename inventory conflicts")
            files.append(item)
        request = FileReviewRequest(
            self.repository,
            self.repository_id,
            self.pull_request,
            base,
            head,
            review_id,
            review_result_digest(result),
            tuple(sorted(files, key=lambda x: x.path)),
        )
        validate_inventory(request, result.coverage)
        if self._snapshot() != (base, head):
            raise ReviewInputError("human file snapshot changed during preparation")
        return request

    def _request_body(self, request: FileReviewRequest) -> str:
        note = (
            ""
            if self.policy.allow_confirmations
            else "\n\nConfirmations are disabled by trusted host policy."
        )
        return (
            request.render()
            + note
            + "\n\n"
            + _marker(REQUEST_PREFIX, request.to_dict())
        )

    def _receipt_body(self, receipt: FileReviewReceipt) -> str:
        return (
            f"Human file review recorded for {len(receipt.selected_ids)} selected binary change(s); "
            "not AI-reviewed. Other findings and approval checks remain in force.\n"
            f"Reviewer: {receipt.actor} (numeric user {receipt.actor_id}); source comment {receipt.source_comment_id}.\n"
            + "\n".join(f"- `{key}`: human-reviewed." for key in receipt.selected_ids)
            + "\n\n"
            + _marker(RECEIPT_PREFIX, receipt.to_dict())
        )

    def _publish(self, body: str) -> int:
        if _body_size(body) > MAX_BODY_BYTES:
            raise ReviewInputError("human file publication exceeds its bound")
        response = self._io(
            "POST", f"/issues/{self.pull_request}/comments", {"body": body}
        )
        if not isinstance(response, Mapping) or not positive(response.get("id")):
            raise ReviewInputError("human file publication outcome is unknown")
        identifier = response["id"]
        if self._comment(identifier, owned=True).get("body") != body:
            raise ReviewInputError("human file publication readback disagrees")
        return cast(int, identifier)

    def request_review(
        self, *, review_id: int, result: ReviewResult
    ) -> tuple[int, FileReviewRequest]:
        request = self.prepare(review_id=review_id, result=result)
        body = self._request_body(request)
        for x in self._list(f"/issues/{self.pull_request}/comments"):
            if self._owned(x) and x.get("body") == body:
                identifier = x.get("id")
                if (
                    not positive(identifier)
                    or self._comment(cast(int, identifier), owned=True).get("body")
                    != body
                ):
                    raise ReviewInputError("human file request readback disagrees")
                return cast(int, identifier), request
        if self._snapshot() != (request.base_sha, request.head_sha):
            raise ReviewInputError("human file request snapshot is stale")
        identifier = self._publish(body)
        if self._snapshot() != (request.base_sha, request.head_sha):
            raise ReviewInputError(
                "human file request snapshot changed after publication"
            )
        return identifier, request

    def _request(self, identifier: int, result: ReviewResult) -> FileReviewRequest:
        comment = self._comment(identifier, owned=True)
        request = FileReviewRequest.from_dict(
            _document(comment["body"], REQUEST_PREFIX)
        )
        if (
            comment["body"] != self._request_body(request)
            or self.prepare(review_id=request.review_id, result=result) != request
        ):
            raise ReviewInputError("human file request is stale or altered")
        return request

    def _source(
        self, identifier: int, request_id: int, request: FileReviewRequest
    ) -> FileReviewReceipt:
        x = self._comment(identifier, owned=False)
        user = x.get("user")
        if (
            not isinstance(user, Mapping)
            or not positive(user.get("id"))
            or user.get("type") != "User"
            or user.get("login") == self.app_login
            or not isinstance(x.get("author_association"), str)
            or x.get("author_association") not in {"OWNER", "MEMBER", "COLLABORATOR"}
        ):
            raise ReviewInputError(
                "human file confirmation requires an authorized human"
            )
        blanket = is_blanket_media_approval(x["body"])
        selection = parse_confirmation(x["body"], request)
        if selection is None or len(x["body"].encode("utf-8")) > MAX_SOURCE_BYTES:
            raise ReviewInputError(
                "human file confirmation requires exact selected file IDs"
            )
        receipt = FileReviewReceipt(
            request.digest,
            request_id,
            identifier,
            cast(str, x.get("updated_at")),
            hashlib.sha256(x["body"].encode("utf-8")).hexdigest(),
            user["id"],
            cast(str, user.get("login")),
            selection,
        )
        permission = self._object(
            f"/collaborators/{quote(receipt.actor, safe='')}/permission"
        )
        permission_user = permission.get("user")
        permission_name = permission.get("permission")
        allowed = {"maintain", "admin"} if blanket else {"write", "maintain", "admin"}
        if blanket and permission_name not in {"maintain", "admin"}:
            raise ReviewInputError("media approval requires a maintainer or admin")
        if (
            not isinstance(permission_name, str)
            or permission_name not in allowed
            or not isinstance(permission_user, Mapping)
            or permission_user.get("id") != receipt.actor_id
            or type(permission_user.get("id")) is not int
            or permission_user.get("login") != receipt.actor
        ):
            raise ReviewInputError(
                "human file reviewer permission is missing or revoked"
            )
        return receipt

    def confirm(
        self, *, request_comment_id: int, source_comment_id: int, result: ReviewResult
    ) -> int:
        if not self.policy.allow_confirmations:
            raise ReviewInputError("human file confirmations are disabled")
        request = self._request(request_comment_id, result)
        receipt = self._source(source_comment_id, request_comment_id, request)
        for x in self._list(f"/issues/{self.pull_request}/comments"):
            if (
                not self._owned(x)
                or not isinstance(x.get("body"), str)
                or RECEIPT_PREFIX not in x["body"]
            ):
                continue
            prior = FileReviewReceipt.from_dict(_document(x["body"], RECEIPT_PREFIX))
            if prior.source_comment_id == source_comment_id:
                if prior != receipt or x["body"] != self._receipt_body(prior):
                    raise ReviewInputError(
                        "human file source event was already bound differently"
                    )
                if (
                    self._source(source_comment_id, request_comment_id, request)
                    != prior
                ):
                    raise ReviewInputError("human file source changed during replay")
                if self._comment(x["id"], owned=True).get("body") != x["body"]:
                    raise ReviewInputError("human file receipt readback disagrees")
                if self.prepare(review_id=request.review_id, result=result) != request:
                    raise ReviewInputError("human file replay snapshot is stale")
                return cast(int, x["id"])
        if (
            self.prepare(review_id=request.review_id, result=result) != request
            or self._source(source_comment_id, request_comment_id, request) != receipt
        ):
            raise ReviewInputError(
                "human file source or snapshot changed before publication"
            )
        identifier = self._publish(self._receipt_body(receipt))
        # Ambiguous changes after POST never acknowledge success or authority.
        if (
            self._source(source_comment_id, request_comment_id, request) != receipt
            or self.prepare(review_id=request.review_id, result=result) != request
        ):
            raise ReviewInputError(
                "human file source or snapshot changed after publication"
            )
        return identifier

    def evaluate(
        self,
        *,
        request_comment_id: int,
        result: ReviewResult,
        enabled: bool = True,
        app_authored: bool = False,
        has_open_review_threads: bool | None = None,
        qualification: str = "not-required",
    ) -> HumanFileReviewAssessment:
        if not self.policy.allow_confirmations:
            raise ReviewInputError("human file confirmations are disabled")
        request = self._request(request_comment_id, result)
        known = {item.file_id for item in request.files}
        confirmed: set[str] = set()
        receipt_ids: list[int] = []
        invalidated: list[int] = []
        valid_receipts: list[tuple[int, FileReviewReceipt]] = []
        seen_sources: set[int] = set()
        for x in self._list(f"/issues/{self.pull_request}/comments"):
            if (
                not self._owned(x)
                or not isinstance(x.get("body"), str)
                or RECEIPT_PREFIX not in x["body"]
            ):
                continue
            receipt = FileReviewReceipt.from_dict(_document(x["body"], RECEIPT_PREFIX))
            if (
                receipt.request_digest != request.digest
                or receipt.request_comment_id != request_comment_id
            ):
                continue
            if (
                receipt.source_comment_id in seen_sources
                or not set(receipt.selected_ids) <= known
                or x["body"] != self._receipt_body(receipt)
                or self._comment(x["id"], owned=True).get("body") != x["body"]
            ):
                raise ReviewInputError("human file audit evidence is conflicting")
            seen_sources.add(receipt.source_comment_id)
            receipt_ids.append(x["id"])
            try:
                valid = (
                    self._source(receipt.source_comment_id, request_comment_id, request)
                    == receipt
                )
            except ReviewInputError:
                # A deleted/edited/revoked source cannot count as reviewed. A
                # different fresh authorized comment may renew selected files.
                # Root, audit corruption and final IO/deadline fences still
                # propagate; this never erases the historical receipt.
                valid = False
            if not valid:
                invalidated.append(x["id"])
                continue
            valid_receipts.append((x["id"], receipt))
        if self.prepare(review_id=request.review_id, result=result) != request:
            raise ReviewInputError("human file evaluation snapshot is stale")
        # Source/permission changes during blob/root acquisition cannot use an
        # earlier successful read. The same original IO guard funds each fence.
        for identifier, receipt in valid_receipts:
            try:
                valid = (
                    self._source(receipt.source_comment_id, request_comment_id, request)
                    == receipt
                )
            except ReviewInputError:
                valid = False
            if valid:
                confirmed.update(receipt.selected_ids)
            else:
                invalidated.append(identifier)
        eligibility = self._root(request.review_id, result, request.head_sha)
        original = eligibility.facts
        current = approval_facts_from_result(
            result,
            enabled=enabled,
            app_authored=app_authored,
            has_open_review_threads=has_open_review_threads,
            qualification=qualification,
        )
        facts = replace(
            original,
            enabled=original.enabled and current.enabled,
            app_authored=original.app_authored or current.app_authored,
            has_blocking_findings=original.has_blocking_findings
            or current.has_blocking_findings,
            has_human_adjudication_findings=original.has_human_adjudication_findings
            or current.has_human_adjudication_findings
            or bool(eligibility.human_review and eligibility.human_review.pending),
            qualification=original.qualification
            if original.qualification != "not-required"
            else current.qualification,
            has_open_review_threads=has_open_review_threads,
        )
        # AI review_status stays partial. Mixed coverage may waive only the
        # binary coverage/status blockers. It must not rewrite those facts to
        # complete, which would hide any other partial cause from the old gate.
        standard = evaluate_approval_facts(facts)
        confirmations = tuple(
            BinaryConfirmation(file=item, event_id=str(request_comment_id))
            for item in request.files
            if item.file_id in confirmed
        )
        source = result.source_context_coverage
        source_incomplete = (
            source is not None
            and bool(getattr(source, "enabled", False))
            and not bool(getattr(source, "complete", False))
        )
        mixed = evaluate_mixed_coverage(
            result,
            request.files,
            confirmations,
            text_completed=result.coverage_only_partial,
            allow_confirmations=True,
            event_id=str(request_comment_id),
            source_context_incomplete=source_incomplete,
            unresolved_threads=has_open_review_threads,
            qualification=qualification,
        )
        waivable = {"review-partial", "coverage-partial"}
        other = tuple(item for item in standard.blockers if item not in waivable)
        if (
            mixed.approved
            and not other
            and facts.review_status == "partial"
            and mixed.ai_review_status == "partial"
        ):
            decision = AutoApprovalDecision(approved=True, blockers=())
        else:
            decision = AutoApprovalDecision(
                approved=False,
                blockers=tuple(dict.fromkeys((*standard.blockers, *mixed.blockers))),
            )
        if self._snapshot() != (request.base_sha, request.head_sha):
            raise ReviewInputError("human file evaluation snapshot is stale")
        return HumanFileReviewAssessment(
            request,
            tuple(receipt_ids),
            tuple(sorted(confirmed)),
            tuple(sorted(known - confirmed)),
            tuple(x.path for x in result.coverage.files if x.outcome == "reviewed")
            if result.coverage is not None
            else (),
            tuple(invalidated),
            decision,
        )

    def publish_mixed_approval(
        self,
        *,
        request_comment_id: int,
        result: ReviewResult,
        enabled: bool = True,
        app_authored: bool = False,
        has_open_review_threads: bool | None = None,
        qualification: str = "not-required",
    ) -> HumanFileReviewAssessment:
        """Post one exact-head APPROVE when mixed coverage is the only waiver.

        The stored AI result stays partial. A withheld decision does not post.
        """

        assessment = self.evaluate(
            request_comment_id=request_comment_id,
            result=result,
            enabled=enabled,
            app_authored=app_authored,
            has_open_review_threads=has_open_review_threads,
            qualification=qualification,
        )
        if not assessment.approval.approved:
            return assessment
        if result.review_status != "partial":
            raise ReviewInputError("mixed approval requires a partial AI result")
        head = assessment.request.head_sha
        if self._snapshot()[1] != head:
            raise ReviewInputError("human file approval head changed")
        body = (
            assessment.render()
            + "\n\nAI-reviewed text; human-reviewed selected media; "
            "mixed coverage sufficient. AI result status remains partial."
        )
        response = self._io(
            "POST",
            f"/pulls/{self.pull_request}/reviews",
            {"commit_id": head, "event": "APPROVE", "body": body},
        )
        if not isinstance(response, Mapping) or not positive(response.get("id")):
            raise ReviewInputError("mixed approval outcome is unknown")
        readback = self._object(f"/pulls/{self.pull_request}/reviews/{response['id']}")
        if (
            readback.get("commit_id") != head
            or readback.get("body") != body
            or readback.get("state") != "APPROVED"
        ):
            raise ReviewInputError("mixed approval readback disagrees")
        if result.review_status != "partial":
            raise ReviewInputError("mixed approval changed the AI result")
        return assessment

    def approve_reviewed_media(
        self,
        *,
        request_comment_id: int,
        source_comment_id: int,
        result: ReviewResult,
        has_open_review_threads: bool | None = None,
        qualification: str = "not-required",
    ) -> HumanFileReviewAssessment:
        """Confirm every file named by the source comment, then post one APPROVE.

        The blanket sentence selects the whole current request. The AI result
        stays partial. Unrelated blockers still withhold the review. Both
        calls share this publisher's original guard and refuse when that guard
        cannot hold them. A host can confirm, then call ``publish_mixed_approval``
        on a later invocation of the same comment.
        """

        self.confirm(
            request_comment_id=request_comment_id,
            source_comment_id=source_comment_id,
            result=result,
        )
        return self.publish_mixed_approval(
            request_comment_id=request_comment_id,
            result=result,
            has_open_review_threads=has_open_review_threads,
            qualification=qualification,
        )

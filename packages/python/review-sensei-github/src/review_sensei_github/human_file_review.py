"""Exact-snapshot human binary-file review, audit and mixed approval.

Human confirmations preserve the partial AI result and existing eligibility
markers. Trusted hosts provide current approval facts and one original IO guard.
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

from review_sensei.errors import ReviewInputError
from review_sensei.human_file_review import (
    MAX_DOCUMENT_BYTES,
    MAX_SOURCE_BYTES,
    FileReviewReceipt,
    FileReviewReceiptValue,
    FileReviewRequest,
    FileReviewRequestValue,
    UnsupportedFile,
    build_receipt,
    build_request_group,
    canonical,
    display_path,
    is_blanket_media_approval,
    is_human_confirmable_file,
    parse_confirmation,
    parse_receipt,
    parse_request,
    positive,
    sha,
    validate_inventory,
)
from review_sensei.mixed_coverage import (
    BinaryConfirmation,
    CurrentFindingFacts,
    evaluate_mixed_coverage,
)
from review_sensei.models import ReviewResult

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
REQUEST_GROUP_PREFIX = "<!-- reviewsensei:human-files:request-group:v1 "
RECEIPT_GROUP_PREFIX = "<!-- reviewsensei:human-files:receipt-group:v1 "
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

    request: FileReviewRequestValue
    receipt_ids: tuple[int, ...]
    human_reviewed_ids: tuple[str, ...]
    pending_ids: tuple[str, ...]
    ai_reviewed_paths: tuple[str, ...]
    invalidated_receipt_ids: tuple[int, ...]
    approval: AutoApprovalDecision
    current_findings: CurrentFindingFacts | None = None

    def render(self) -> str:
        lines = [
            f"Coverage: {len(self.ai_reviewed_paths)} AI-reviewed path(s), "
            f"{len(self.human_reviewed_ids)} human-reviewed file change(s), "
            f"{len(self.pending_ids)} file change(s) pending human review."
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
        approval_facts_provider: Callable[..., Mapping[str, object]] | None = None,
        eligibility_reader: Callable[..., ReviewApprovalEligibility] | None = None,
        snapshot_validator: Callable[[Mapping[str, Any]], None] | None = None,
        before_approval: Callable[[int], None] | None = None,
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
        self._last_reviews: list[Mapping[str, Any]] = []
        if approval_facts_provider is not None and not callable(
            approval_facts_provider
        ):
            raise ReviewInputError("human file approval facts provider is invalid")
        if eligibility_reader is not None and not callable(eligibility_reader):
            raise ReviewInputError("human file eligibility reader is invalid")
        if snapshot_validator is not None and not callable(snapshot_validator):
            raise ReviewInputError("human file snapshot validator is invalid")
        if before_approval is not None and not callable(before_approval):
            raise ReviewInputError("human file approval tail guard is invalid")
        self.before_approval = before_approval
        self.snapshot_validator = snapshot_validator
        self.eligibility_reader = eligibility_reader
        self.approval_facts_provider = approval_facts_provider
        # Only immutable, exact-commit inventory is reused within this invocation.
        # Mutable source, request, permission and latest-root fences never use it.
        self._prepared: dict[tuple[str, str, int, str], FileReviewRequestValue] = {}
        self._requested: dict[int, FileReviewRequestValue] = {}

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
        pr = self._object(f"/pulls/{self.pull_request}")
        base, head = pr.get("base"), pr.get("head")
        repo = base.get("repo") if isinstance(base, Mapping) else None
        # Older explicit library transports may not project base.repo; retain
        # the independently authenticated repository read for those callers.
        if not isinstance(repo, Mapping):
            repo = self._object("")
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
        if self.snapshot_validator is not None:
            self.snapshot_validator(pr)
        return cast(str, base["sha"]), cast(str, head["sha"])

    def _root(
        self, review_id: int, result: ReviewResult, head: str
    ) -> ReviewApprovalEligibility:
        if not positive(review_id):
            raise ReviewInputError("human file review ID is invalid")
        reviews = self._list(f"/pulls/{self.pull_request}/reviews")
        self._last_reviews = reviews
        roots = [
            x for x in reviews if type(x.get("id")) is int and x["id"] == review_id
        ]
        if len(roots) != 1:
            raise ReviewInputError("human file review root is missing or ambiguous")
        root = roots[0]
        if (
            not self._owned(root)
            or type(root.get("id")) is not int
            or root.get("id") != review_id
            or root.get("commit_id") != head
            or root.get("state") != "COMMENTED"
        ):
            raise ReviewInputError("human file review root is not owned or current")
        eligibility = (
            self.eligibility_reader(
                root=root, reviews=reviews, result=result, head_sha=head
            )
            if self.eligibility_reader is not None
            else approval_eligibility_from_body(root.get("body"))
        )
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
            for x in reviews
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

    def prepare(
        self, *, review_id: int, result: ReviewResult
    ) -> FileReviewRequestValue:
        if not isinstance(result, ReviewResult) or result.coverage is None:
            raise ReviewInputError(
                "human file request requires complete coverage inventory"
            )
        paths = {x.path for x in result.coverage.files if is_human_confirmable_file(x)}
        if not paths or not result.coverage.enumeration_complete:
            raise ReviewInputError(
                "human file unsupported inventory is missing or incomplete"
            )
        base, head = self._snapshot()
        self._root(review_id, result, head)
        key = (base, head, review_id, review_result_digest(result))
        if key in self._prepared:
            request = self._prepared[key]
            validate_inventory(request, result.coverage)
            if self._snapshot() != (base, head):
                raise ReviewInputError("human file snapshot changed during preparation")
            return request
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
        if not selected:
            raise ReviewInputError("human file inventory is empty")
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
        request = build_request_group(
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
        self._prepared[key] = request
        return request

    def _request_body(self, request: FileReviewRequestValue) -> str:
        note = (
            ""
            if self.policy.allow_confirmations
            else "\n\nConfirmations are disabled by trusted host policy."
        )
        return (
            request.render()
            + note
            + "\n\n"
            + _marker(
                REQUEST_PREFIX
                if isinstance(request, FileReviewRequest)
                else REQUEST_GROUP_PREFIX,
                request.to_dict(),
            )
        )

    def _receipt_body(self, receipt: FileReviewReceiptValue) -> str:
        return (
            f"Human file review recorded for {len(receipt.selected_ids)} selected file change(s); "
            "not AI-reviewed. Other findings and approval checks remain in force.\n"
            f"Reviewer: {receipt.actor} (numeric user {receipt.actor_id}); source comment {receipt.source_comment_id}.\n"
            + "\n".join(f"- `{key}`: human-reviewed." for key in receipt.selected_ids)
            + "\n\n"
            + _marker(
                RECEIPT_PREFIX
                if isinstance(receipt, FileReviewReceipt)
                else RECEIPT_GROUP_PREFIX,
                receipt.to_dict(),
            )
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
    ) -> tuple[int, FileReviewRequestValue]:
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
                self._requested[cast(int, identifier)] = request
                return cast(int, identifier), request
        if self._snapshot() != (request.base_sha, request.head_sha):
            raise ReviewInputError("human file request snapshot is stale")
        identifier = self._publish(body)
        if self._snapshot() != (request.base_sha, request.head_sha):
            raise ReviewInputError(
                "human file request snapshot changed after publication"
            )
        self._requested[identifier] = request
        return identifier, request

    def _request(self, identifier: int, result: ReviewResult) -> FileReviewRequestValue:
        comment = self._comment(identifier, owned=True)
        request = parse_request(
            _document(
                comment["body"],
                REQUEST_GROUP_PREFIX
                if REQUEST_GROUP_PREFIX in comment["body"]
                else REQUEST_PREFIX,
            )
        )
        if (
            comment["body"] != self._request_body(request)
            or self.prepare(review_id=request.review_id, result=result) != request
        ):
            raise ReviewInputError("human file request is stale or altered")
        return request

    def _source(
        self, identifier: int, request_id: int, request: FileReviewRequestValue
    ) -> FileReviewReceiptValue:
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
        receipt = build_receipt(
            request,
            request_comment_id=request_id,
            source_comment_id=identifier,
            source_updated_at=cast(str, x.get("updated_at")),
            source_sha256=hashlib.sha256(x["body"].encode("utf-8")).hexdigest(),
            actor_id=user["id"],
            actor=cast(str, user.get("login")),
            selected_ids=selection,
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
                or not any(
                    prefix in x["body"]
                    for prefix in (RECEIPT_PREFIX, RECEIPT_GROUP_PREFIX)
                )
            ):
                continue
            prior = parse_receipt(
                _document(
                    x["body"],
                    RECEIPT_GROUP_PREFIX
                    if RECEIPT_GROUP_PREFIX in x["body"]
                    else RECEIPT_PREFIX,
                )
            )
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
        valid_receipts: list[tuple[int, FileReviewReceiptValue]] = []
        seen_sources: set[int] = set()
        for x in self._list(f"/issues/{self.pull_request}/comments"):
            if (
                not self._owned(x)
                or not isinstance(x.get("body"), str)
                or not any(
                    prefix in x["body"]
                    for prefix in (RECEIPT_PREFIX, RECEIPT_GROUP_PREFIX)
                )
            ):
                continue
            receipt = parse_receipt(
                _document(
                    x["body"],
                    RECEIPT_GROUP_PREFIX
                    if RECEIPT_GROUP_PREFIX in x["body"]
                    else RECEIPT_PREFIX,
                )
            )
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
        facts = self._current_facts(
            request,
            result,
            eligibility,
            enabled=enabled,
            app_authored=app_authored,
            has_open_review_threads=has_open_review_threads,
            qualification=qualification,
        )
        eligibility = self._root(request.review_id, result, request.head_sha)
        if self._snapshot() != (request.base_sha, request.head_sha):
            raise ReviewInputError("human file evaluation snapshot is stale")
        return self._assess(
            request,
            result,
            eligibility,
            confirmed,
            receipt_ids,
            invalidated,
            request_comment_id=request_comment_id,
            enabled=cast(bool, facts["enabled"]) and enabled,
            app_authored=cast(bool, facts["app_authored"]) or app_authored,
            has_open_review_threads=cast(bool | None, facts["has_open_review_threads"]),
            qualification=cast(str, facts["qualification"]),
            current_findings=cast(
                CurrentFindingFacts | None, facts.get("current_findings")
            ),
        )

    def _current_facts(
        self,
        request: FileReviewRequestValue,
        result: ReviewResult,
        eligibility: ReviewApprovalEligibility,
        *,
        enabled: bool,
        app_authored: bool,
        has_open_review_threads: bool | None,
        qualification: str,
    ) -> Mapping[str, object]:
        facts: Mapping[str, object] = dict(
            enabled=enabled,
            app_authored=app_authored,
            has_open_review_threads=has_open_review_threads,
            qualification=qualification,
        )
        if self.approval_facts_provider is not None:
            facts = self.approval_facts_provider(
                request=request, result=result, eligibility=eligibility
            )
        if (
            not isinstance(facts, Mapping)
            or not {
                "enabled",
                "app_authored",
                "has_open_review_threads",
                "qualification",
            }
            <= set(facts)
            or not set(facts)
            <= {
                "enabled",
                "app_authored",
                "has_open_review_threads",
                "qualification",
                "current_findings",
            }
            or type(facts["enabled"]) is not bool
            or type(facts["app_authored"]) is not bool
            or (
                facts["has_open_review_threads"] is not None
                and type(facts["has_open_review_threads"]) is not bool
            )
            or facts["qualification"]
            not in {"not-required", "qualified", "missing", "unverified"}
        ):
            raise ReviewInputError("human file current approval facts are invalid")
        if "current_findings" in facts and (
            type(facts["current_findings"]) is not CurrentFindingFacts
            or cast(CurrentFindingFacts, facts["current_findings"]).result_digest
            != result.content_digest()
        ):
            raise ReviewInputError(
                "human file current finding facts are invalid or stale"
            )
        return facts

    def _assess(
        self,
        request: FileReviewRequestValue,
        result: ReviewResult,
        eligibility: ReviewApprovalEligibility,
        confirmed: set[str],
        receipt_ids: list[int],
        invalidated: list[int],
        *,
        request_comment_id: int,
        enabled: bool,
        app_authored: bool,
        has_open_review_threads: bool | None,
        qualification: str,
        current_findings: CurrentFindingFacts | None = None,
    ) -> HumanFileReviewAssessment:
        known = {item.file_id for item in request.files}
        original = eligibility.facts
        current = approval_facts_from_result(
            result,
            enabled=enabled,
            app_authored=app_authored,
            has_open_review_threads=has_open_review_threads,
            qualification=qualification,
        )
        if current_findings is not None:
            if (
                type(current_findings) is not CurrentFindingFacts
                or current_findings.result_digest != result.content_digest()
            ):
                raise ReviewInputError(
                    "human file current finding facts are invalid or stale"
                )
            current = replace(
                current,
                has_blocking_findings=current_findings.has_blocking_findings,
                has_human_adjudication_findings=current_findings.has_human_adjudication_findings,
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
            qualification=(
                "missing"
                if "missing" in {original.qualification, current.qualification}
                else "unverified"
                if "unverified" in {original.qualification, current.qualification}
                else "qualified"
                if "qualified" in {original.qualification, current.qualification}
                else "not-required"
            ),
            has_open_review_threads=(
                True
                if original.has_open_review_threads is True
                else has_open_review_threads
            ),
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
            unresolved_threads=facts.has_open_review_threads,
            qualification=facts.qualification,
            current_findings=current_findings,
        )
        waivable = {"review-partial", "coverage-partial"}
        other = tuple(item for item in standard.blockers if item not in waivable)
        if (
            mixed.approved
            and facts.review_status == "partial"
            and mixed.ai_review_status == "partial"
        ):
            if not other:
                decision = AutoApprovalDecision(approved=True, blockers=())
            else:
                decision = AutoApprovalDecision(approved=False, blockers=other)
        else:
            decision = AutoApprovalDecision(
                approved=False,
                blockers=tuple(dict.fromkeys((*standard.blockers, *mixed.blockers))),
            )
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
            current_findings,
        )

    def publish_mixed_approval(
        self,
        *,
        request_comment_id: int,
        result: ReviewResult,
        enabled: bool = True,
        app_authored: bool = False,
        has_open_review_threads: bool | None = False,
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
        return self._publish_approval(assessment, result)

    def _publish_approval(
        self, assessment: HumanFileReviewAssessment, result: ReviewResult
    ) -> HumanFileReviewAssessment:
        request = assessment.request
        if self._snapshot() != (request.base_sha, request.head_sha):
            raise ReviewInputError("human file approval snapshot changed")
        if not assessment.approval.approved:
            return assessment
        if result.review_status != "partial":
            raise ReviewInputError("mixed approval requires a partial AI result")
        body = (
            assessment.render()
            + "\n\nAI-reviewed text; human-reviewed selected media; "
            "mixed coverage sufficient. AI result status remains partial."
        )
        existing = [
            item
            for item in self._last_reviews
            if self._owned(item)
            and item.get("commit_id") == request.head_sha
            and item.get("state") == "APPROVED"
            and item.get("body") == body
            and positive(item.get("id"))
        ]
        if len(existing) > 1:
            raise ReviewInputError("mixed approval has duplicate owned reviews")
        # Admit the complete native write/readback/snapshot tail before its
        # first irreversible request. The host also retains the original
        # reserved check-finalization calls; this guard never refills or refunds.
        if self.before_approval is not None:
            self.before_approval(2 if existing else 3)
        if existing:
            identifier = existing[0]["id"]
        else:
            response = self._io(
                "POST",
                f"/pulls/{self.pull_request}/reviews",
                {"commit_id": request.head_sha, "event": "APPROVE", "body": body},
            )
            if not isinstance(response, Mapping) or not positive(response.get("id")):
                raise ReviewInputError("mixed approval outcome is unknown")
            identifier = response["id"]
        readback = self._object(f"/pulls/{self.pull_request}/reviews/{identifier}")
        if (
            not self._owned(readback)
            or type(readback.get("id")) is not int
            or readback.get("id") != identifier
            or readback.get("commit_id") != request.head_sha
            or readback.get("body") != body
            or readback.get("state") != "APPROVED"
        ):
            raise ReviewInputError("mixed approval readback disagrees")
        if self._snapshot() != (request.base_sha, request.head_sha):
            raise ReviewInputError(
                "human file approval snapshot changed after publication"
            )
        if result.review_status != "partial":
            raise ReviewInputError("mixed approval changed the AI result")
        return assessment

    def approve_reviewed_media(
        self,
        *,
        request_comment_id: int,
        source_comment_id: int,
        result: ReviewResult,
        enabled: bool = True,
        has_open_review_threads: bool | None = False,
        qualification: str = "not-required",
    ) -> HumanFileReviewAssessment:
        """Confirm every file named by the source comment, then post one APPROVE.

        The blanket sentence selects the whole authenticated request group.
        Immutable inventory is acquired once; fresh mutable root, request,
        source, permission and snapshot fences surround the audit and approval
        writes. Every physical request uses this publisher's original guard.
        The AI result stays partial and unrelated blockers still withhold.
        """

        if not self.policy.allow_confirmations:
            raise ReviewInputError("human file confirmations are disabled")
        # request_review may have just authenticated and read back this exact
        # request in the same invocation. It remains reference data: fresh root,
        # request, source/permission and full snapshot fences below still gate
        # every write. No cache crosses publishers or invocation budgets.
        request = self._requested.get(request_comment_id)
        if request is None:
            request = self._request(request_comment_id, result)
        if request.result_digest != review_result_digest(result):
            raise ReviewInputError("human file request result changed")
        receipt = self._source(source_comment_id, request_comment_id, request)
        prior_id = None
        for item in self._list(f"/issues/{self.pull_request}/comments"):
            if not self._owned(item) or not isinstance(item.get("body"), str):
                continue
            if not any(
                prefix in item["body"]
                for prefix in (RECEIPT_PREFIX, RECEIPT_GROUP_PREFIX)
            ):
                continue
            prior = self._parse_receipt_body(item["body"])
            if prior.source_comment_id == source_comment_id:
                if prior_id is not None:
                    raise ReviewInputError("human file audit evidence is conflicting")
                if prior != receipt or item["body"] != self._receipt_body(prior):
                    raise ReviewInputError(
                        "human file source event was already bound differently"
                    )
                if not positive(item.get("id")):
                    raise ReviewInputError("human file audit evidence is conflicting")
                prior_id = item["id"]
        eligibility = self._root(request.review_id, result, request.head_sha)
        if self._snapshot() != (request.base_sha, request.head_sha):
            raise ReviewInputError("human file confirmation snapshot is stale")
        if self._source(source_comment_id, request_comment_id, request) != receipt:
            raise ReviewInputError("human file source changed before publication")
        receipt_body = self._receipt_body(receipt)
        if prior_id is None:
            prior_id = self._publish(receipt_body)
        elif self._comment(prior_id, owned=True).get("body") != receipt_body:
            raise ReviewInputError("human file receipt readback disagrees")

        # Re-enumerate after the write/readback to detect duplicate or conflicting
        # audit records. Authenticate every source against this same frozen group.
        known = {item.file_id for item in request.files}
        receipt_ids: list[int] = []
        invalidated: list[int] = []
        candidates: list[tuple[int, FileReviewReceiptValue]] = []
        seen: set[int] = set()
        for item in self._list(f"/issues/{self.pull_request}/comments"):
            if not self._owned(item) or not isinstance(item.get("body"), str):
                continue
            if not any(
                prefix in item["body"]
                for prefix in (RECEIPT_PREFIX, RECEIPT_GROUP_PREFIX)
            ):
                continue
            audit = self._parse_receipt_body(item["body"])
            if (
                audit.request_digest != request.digest
                or audit.request_comment_id != request_comment_id
            ):
                continue
            identifier = item.get("id")
            if (
                not positive(identifier)
                or audit.source_comment_id in seen
                or not set(audit.selected_ids) <= known
                or item["body"] != self._receipt_body(audit)
            ):
                raise ReviewInputError("human file audit evidence is conflicting")
            identifier = cast(int, identifier)
            if identifier == prior_id:
                if audit != receipt or item["body"] != receipt_body:
                    raise ReviewInputError("human file receipt readback disagrees")
            elif self._comment(identifier, owned=True).get("body") != item["body"]:
                raise ReviewInputError("human file receipt readback disagrees")
            seen.add(audit.source_comment_id)
            receipt_ids.append(identifier)
            candidates.append((identifier, audit))
        if prior_id not in receipt_ids:
            raise ReviewInputError("human file receipt vanished during evaluation")
        confirmed: set[str] = set()
        for identifier, audit in candidates:
            try:
                current = (
                    self._source(audit.source_comment_id, request_comment_id, request)
                    == audit
                )
            except ReviewInputError:
                current = False
            if current:
                confirmed.update(audit.selected_ids)
            else:
                invalidated.append(identifier)
        if self._comment(request_comment_id, owned=True).get(
            "body"
        ) != self._request_body(request):
            raise ReviewInputError("human file request changed during evaluation")
        facts = self._current_facts(
            request,
            result,
            eligibility,
            enabled=enabled,
            app_authored=False,
            has_open_review_threads=has_open_review_threads,
            qualification=qualification,
        )
        # A callback may dispatch mutable reads. Re-authenticate the latest
        # owned root after it, then use the existing full PR snapshot as the
        # final native-publication fence. No earlier root observation is a grant.
        eligibility = self._root(request.review_id, result, request.head_sha)
        assessment = self._assess(
            request,
            result,
            eligibility,
            confirmed,
            receipt_ids,
            invalidated,
            request_comment_id=request_comment_id,
            enabled=cast(bool, facts["enabled"]) and enabled,
            app_authored=cast(bool, facts["app_authored"]),
            has_open_review_threads=cast(bool | None, facts["has_open_review_threads"]),
            qualification=cast(str, facts["qualification"]),
            current_findings=cast(
                CurrentFindingFacts | None, facts.get("current_findings")
            ),
        )
        return self._publish_approval(assessment, result)

    @staticmethod
    def _parse_receipt_body(body: str) -> FileReviewReceiptValue:
        return parse_receipt(
            _document(
                body,
                RECEIPT_GROUP_PREFIX
                if RECEIPT_GROUP_PREFIX in body
                else RECEIPT_PREFIX,
            )
        )

"""Explicit nonactivating COMMENT-review staging and complete readback.

The caller supplies its existing authorized publication token and shared A
budget. This prototype is not enabled by ReviewPublisher or approval callers.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Mapping

from ...bounded_evidence import EvidenceReadBudget
from ...errors import ReviewInputError
from ...validation import validate_repository_path
from .errors import GitHubHTTPError, GitHubPublicationError
from .http import GitHubHttp
from .publication_parts import (
    FindingProsePart,
    FindingProseReadback,
    _header,
    verify_finding_prose_readbacks,
)

PROSE_RECEIPT_INTERFACE_VERSION = "visible-prose-v1"
MAX_RETAINED_PROSE_REVIEWS = 256
MAX_RETAINED_PROSE_BYTES = 8 * 1024 * 1024


def _positive_id(value: object) -> bool:
    return type(value) is int and 0 < value <= 2**63 - 1


@dataclass(frozen=True)
class VisibleProseReceipt:
    """An immutable receipt, requiring authenticated root and fresh readback."""

    index: int
    review_id: int
    head_sha: str
    producer_id: int
    sha256: str
    bytes: int
    instances: tuple[str, ...]
    paths: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            type(self.index) is not int
            or not 0 <= self.index < 32
            or not _positive_id(self.review_id)
            or not _positive_id(self.producer_id)
            or type(self.bytes) is not int
            or not 1 <= self.bytes <= 65_536
            or not isinstance(self.instances, tuple)
            or not 1 <= len(self.instances) <= 250
            or len(set(self.instances)) != len(self.instances)
            or not isinstance(self.paths, tuple)
            or not 1 <= len(self.paths) <= 64
            or len(set(self.paths)) != len(self.paths)
        ):
            raise ReviewInputError("visible prose receipt metadata is invalid")
        for value, width in (
            (self.head_sha, 40),
            (self.sha256, 64),
            *((item, 64) for item in self.instances),
        ):
            if (
                not isinstance(value, str)
                or len(value) != width
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ReviewInputError("visible prose receipt identity is invalid")
        for path in self.paths:
            validate_repository_path(
                path, label="visible prose receipt path", allow_glob_chars=True
            )

    @classmethod
    def from_dict(cls, value: object) -> VisibleProseReceipt:
        fields = {
            "interface",
            "placement",
            "index",
            "review_id",
            "head_sha",
            "producer_id",
            "sha256",
            "bytes",
            "instances",
            "paths",
        }
        if (
            not isinstance(value, dict)
            or set(value) != fields
            or value["interface"] != PROSE_RECEIPT_INTERFACE_VERSION
            or value["placement"] != "review-body"
            or not isinstance(value["instances"], list)
            or not isinstance(value["paths"], list)
        ):
            raise ReviewInputError("visible prose receipt document is invalid")
        return cls(
            value["index"],
            value["review_id"],
            value["head_sha"],
            value["producer_id"],
            value["sha256"],
            value["bytes"],
            tuple(value["instances"]),
            tuple(value["paths"]),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "interface": PROSE_RECEIPT_INTERFACE_VERSION,
            "placement": "review-body",
            "index": self.index,
            "review_id": self.review_id,
            "head_sha": self.head_sha,
            "producer_id": self.producer_id,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "instances": list(self.instances),
            "paths": list(self.paths),
        }


class ProseStagingError(GitHubPublicationError):
    """Known staged IDs retained for reconciliation; no activation implied."""

    def __init__(self, staged_ids: tuple[int, ...]) -> None:
        self.staged_ids = staged_ids
        self.diagnostic = "visible_prose_staging_failed"
        super().__init__(
            "complete visible prose staging failed; retained state requires reconciliation"
        )


class CommentReviewProseStore:
    """Finite existing-review capability, with one charged dispatch per request."""

    def __init__(
        self,
        *,
        http: GitHubHttp,
        token: str,
        repository: str,
        pull_request: int,
        producer_id: int,
        app_slug: str,
        budget: EvidenceReadBudget,
    ) -> None:
        if (
            not _positive_id(pull_request)
            or not _positive_id(producer_id)
            or not isinstance(app_slug, str)
            or not app_slug
        ):
            raise ReviewInputError("visible prose trusted identity is invalid")
        self.http = http
        self.token = token
        self.path = http.repository_path(repository, f"/pulls/{pull_request}")
        self.pull_request_url = http.api_url + self.path
        self.producer_id = producer_id
        self.app_slug = app_slug
        self.budget = budget

    def _request(
        self, method: str, path: str, body: dict[str, object] | None = None
    ) -> object:
        status, result = self.http.request(
            method,
            path,
            token=self.token,
            body=body,
            timeout_seconds=self.budget.consume(),
        )
        self.budget.check()
        if not 200 <= status < 300:
            raise ReviewInputError("visible prose transport failed")
        return result

    def _owned(self, item: Mapping[str, object]) -> bool:
        user = item.get("user")
        return (
            isinstance(user, dict)
            and _positive_id(user.get("id"))
            and user.get("id") == self.producer_id
            and user.get("type") == "Bot"
            and user.get("login") == self.app_slug
        )

    def _read(self, identifier: int, head_sha: str) -> FindingProseReadback:
        if not _positive_id(identifier):
            raise ReviewInputError("visible prose numeric identity is invalid")
        item = self._request("GET", f"{self.path}/reviews/{identifier}")
        if (
            not isinstance(item, dict)
            or item.get("id") != identifier
            or not self._owned(item)
            or item.get("pull_request_url") != self.pull_request_url
            or item.get("state") != "COMMENTED"
            or item.get("commit_id") != head_sha
            or not isinstance(item.get("body"), str)
        ):
            raise ReviewInputError("visible prose placement or owner conflicts")
        return FindingProseReadback(
            identifier, self.producer_id, head_sha, item["body"]
        )

    def _retained(self) -> tuple[dict[str, object], ...]:
        # One review per page bounds JSON escaping of an entire 64KiB body
        # below the transport ceiling. A complete scan shares the strict budget;
        # an old busy PR can therefore refuse before any new mutation.
        items: list[dict[str, object]] = []
        seen: set[int] = set()
        for page in range(1, MAX_RETAINED_PROSE_REVIEWS + 2):
            result = self._request("GET", f"{self.path}/reviews?per_page=1&page={page}")
            if not isinstance(result, list) or len(result) > 1:
                raise ReviewInputError("visible prose metadata scan is incomplete")
            if not result:
                return tuple(items)
            item = result[0]
            if (
                not isinstance(item, dict)
                or not _positive_id(item.get("id"))
                or item["id"] in seen
            ):
                raise ReviewInputError("visible prose metadata identity conflicts")
            seen.add(item["id"])
            if len(seen) > MAX_RETAINED_PROSE_REVIEWS:
                raise ReviewInputError("visible prose retention scan exceeds capacity")
            body = item.get("body")
            if (
                self._owned(item)
                and isinstance(body, str)
                and body.startswith("## ReviewSensei finding explanations — part ")
            ):
                if item.get("pull_request_url") != self.pull_request_url:
                    raise ReviewInputError("visible prose retained placement conflicts")
                items.append(item)
        raise ReviewInputError("visible prose metadata scan is incomplete")

    def stage(
        self, parts: tuple[FindingProsePart, ...], *, head_sha: str
    ) -> tuple[VisibleProseReceipt, ...]:
        """Reuse or stage complete COMMENT bodies, then GET every exact object.

        No root/check/APPROVE mutation occurs. Failed or ambiguous writes remain
        nonauthoritative; a later complete scan reconciles content-identical
        objects. Nothing deletes retained reviews or prior obligations.
        """
        known: list[int] = []
        try:
            # Validate the entire plan before metadata or mutations.
            if any(
                not part.body.startswith(_header(index, len(parts), head_sha))
                for index, part in enumerate(parts)
            ):
                raise ReviewInputError("visible prose plan head or framing conflicts")
            verify_finding_prose_readbacks(
                parts,
                tuple(
                    FindingProseReadback(i + 1, self.producer_id, head_sha, part.body)
                    for i, part in enumerate(parts)
                ),
                producer_id=self.producer_id,
                head_sha=head_sha,
            )
            if not parts:
                return ()
            retained = self._retained()
            matches: dict[str, dict[str, object]] = {}
            for retained_item in retained:
                if (
                    retained_item.get("state") == "COMMENTED"
                    and retained_item.get("commit_id") == head_sha
                ):
                    matches.setdefault(str(retained_item["body"]), retained_item)
            new = tuple(part for part in parts if part.body not in matches)
            retained_bytes = sum(
                len(str(item["body"]).encode("utf-8")) for item in retained
            )
            if (
                len(retained) + len(new) > MAX_RETAINED_PROSE_REVIEWS
                or retained_bytes + sum(part.byte_length for part in new)
                > MAX_RETAINED_PROSE_BYTES
            ):
                raise ReviewInputError("visible prose retained capacity exceeded")
            self.budget.preflight(len(new) + 2 * len(parts) + 16)
            for part in parts:
                item = matches.get(part.body)
                if item is None:
                    created = self._request(
                        "POST",
                        self.path + "/reviews",
                        {"commit_id": head_sha, "event": "COMMENT", "body": part.body},
                    )
                    if not isinstance(created, dict) or not _positive_id(
                        created.get("id")
                    ):
                        raise ReviewInputError(
                            "visible prose write identity is ambiguous"
                        )
                    item = created
                assert type(item["id"]) is int
                known.append(item["id"])
            observed = tuple(self._read(identifier, head_sha) for identifier in known)
            verify_finding_prose_readbacks(
                parts, observed, producer_id=self.producer_id, head_sha=head_sha
            )
            return tuple(
                VisibleProseReceipt(
                    part.index,
                    readback.storage_id,
                    head_sha,
                    self.producer_id,
                    part.sha256,
                    part.byte_length,
                    part.instances,
                    part.paths,
                )
                for part, readback in zip(parts, observed, strict=True)
            )
        except (ReviewInputError, GitHubHTTPError, UnicodeError):
            raise ProseStagingError(tuple(known)) from None

    def revalidate(self, receipts: tuple[VisibleProseReceipt, ...]) -> None:
        """Fresh complete exact readback before activation or finalization."""
        if (
            not isinstance(receipts, tuple)
            or any(not isinstance(item, VisibleProseReceipt) for item in receipts)
            or len(receipts) > 32
            or len({item.review_id for item in receipts}) != len(receipts)
            or len({item.head_sha for item in receipts}) > 1
            or sum(item.bytes for item in receipts) > 1_048_576
            or len({instance for item in receipts for instance in item.instances})
            != sum(len(item.instances) for item in receipts)
            or sum(len(item.instances) for item in receipts) > 250
        ):
            raise ReviewInputError("visible prose receipt set is incomplete")
        self.budget.preflight(len(receipts))
        for index, receipt in enumerate(receipts):
            if receipt.index != index or receipt.producer_id != self.producer_id:
                raise ReviewInputError("visible prose receipt authority conflicts")
            observed = self._read(receipt.review_id, receipt.head_sha)
            raw = observed.body.encode("utf-8")
            if (
                len(raw) != receipt.bytes
                or hashlib.sha256(raw).hexdigest() != receipt.sha256
            ):
                raise ReviewInputError("visible prose receipt body changed")

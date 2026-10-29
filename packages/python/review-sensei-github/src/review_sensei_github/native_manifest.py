"""Immutable small native-union carrier; it contains no review prose or grant.

The original accepted result/event remains byte-identical. This carrier only
solves cold native selection; its source reference and union must be compared
with independently authenticated original admission before use as evidence.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.publication_staging import PublicationReceipt
from review_sensei.session import SessionIdentity

from .errors import GitHubPublicationTransientError
from .native_union import NativeUnionReceipt
from .operation_comments import GitHubIssueCommentOperationPort

MANIFEST_WIRE_BOUND = 8192
_PREFIX = "ReviewSensei immutable native publication manifest v1\n```json\n"
_SUFFIX = "\n```\n<!-- reviewsensei:native-manifest:v1 -->"
_SHA = re.compile(r"[a-f0-9]{64}")


def manifest_body(receipt: NativeUnionReceipt, source_sha256: str) -> str:
    if not isinstance(source_sha256, str) or _SHA.fullmatch(source_sha256) is None:
        raise ReviewInputError("native manifest original source digest is invalid")
    payload = canonical_bytes(
        {
            "schema_version": "native-union-manifest-v1",
            "source_sha256": source_sha256,
            "native_union": receipt.to_document(),
        }
    )
    body = (
        _PREFIX
        + canonical_bytes(
            {"data": base64.urlsafe_b64encode(payload).decode().rstrip("=")}
        ).decode()
        + _SUFFIX
    )
    if (
        len(json.dumps({"body": body}, separators=(",", ":")).encode())
        > MANIFEST_WIRE_BOUND
    ):
        raise ReviewInputError("native manifest exceeds its complete reserved carrier")
    return body


def parse_manifest_body(body: object) -> tuple[str, NativeUnionReceipt]:
    if (
        not isinstance(body, str)
        or not body.startswith(_PREFIX)
        or not body.endswith(_SUFFIX)
        or len(body.encode()) > MANIFEST_WIRE_BOUND
    ):
        raise ReviewInputError("native manifest body is unsupported")
    try:
        frame = json.loads(body[len(_PREFIX) : -len(_SUFFIX)])
        if (
            not isinstance(frame, dict)
            or set(frame) != {"data"}
            or not isinstance(frame["data"], str)
        ):
            raise ValueError("native manifest frame is invalid")
        encoded = frame["data"]
        payload = base64.b64decode(
            (encoded + "=" * (-len(encoded) % 4)).encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
        value = json.loads(payload)
        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "source_sha256", "native_union"}
            or value["schema_version"] != "native-union-manifest-v1"
            or not isinstance(value["source_sha256"], str)
            or _SHA.fullmatch(value["source_sha256"]) is None
            or canonical_bytes(value) != payload
        ):
            raise ValueError("native manifest original source conflicts")
        receipt = NativeUnionReceipt.from_document(value["native_union"])
        if body != manifest_body(receipt, value["source_sha256"]):
            raise ValueError("noncanonical native manifest")
        return value["source_sha256"], receipt
    except (ValueError, UnicodeError, TypeError, KeyError) as error:
        raise ReviewInputError("native manifest cannot be authenticated") from error


def read_manifest_body(body: object, *, source_sha256: str) -> NativeUnionReceipt:
    source, receipt = parse_manifest_body(body)
    if source != source_sha256:
        raise ReviewInputError("native manifest original source conflicts")
    return receipt


@dataclass(frozen=True)
class NativeManifestStore:
    port: GitHubIssueCommentOperationPort
    identity: SessionIdentity
    producer_id: int
    source_sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.port) is not GitHubIssueCommentOperationPort
            or (self.port._repository, self.port._list_path, self.port._app_user_id)
            != (
                self.identity.repository,
                self.port._http.repository_path(
                    self.identity.repository,
                    f"/issues/{self.identity.pull_request}/comments",
                ),
                self.producer_id,
            )
            or not self.port._strict_association
            or not isinstance(self.source_sha256, str)
            or _SHA.fullmatch(self.source_sha256) is None
        ):
            raise ReviewInputError("native manifest store source/scope is invalid")

    def _scope(self, receipt: NativeUnionReceipt) -> None:
        if (receipt.repository_id, receipt.pull_request, receipt.producer_id) != (
            self.identity.repository_id,
            self.identity.pull_request,
            self.producer_id,
        ):
            raise ReviewInputError("native manifest receipt scope conflicts")

    def create(self, receipt: NativeUnionReceipt) -> int | None:
        """Dispatch once under the caller's native-publication allocation.

        The core manifest claim already precedes this dispatch. This is not an
        operation-host event/result write with its separate two-read echo tail.
        Its complete confirmation census keeps the unchanged ten-read bound.
        """
        self._scope(receipt)
        body = manifest_body(receipt, self.source_sha256)
        status, value = self.port._http.request(
            "POST", self.port._list_path, token=self.port._token, body={"body": body}
        )
        if status not in {200, 201} or not isinstance(value, dict):
            return None
        try:
            echoed = self.port._view(value)
        except ValueError:
            return None
        if (
            echoed.body != body
            or echoed.user_id != self.producer_id
            or echoed.user_type != "Bot"
        ):
            return None
        return echoed.comment_id

    def find(
        self, receipt: NativeUnionReceipt, *, known_id: int | None = None
    ) -> PublicationReceipt:
        self._scope(receipt)
        expected = manifest_body(receipt, self.source_sha256)
        if self.port._primed is not None:
            raise ReviewInputError(
                "native manifest requires a fresh complete comment read"
            )
        matches = []
        for comment in self.port.list_comments():
            if (
                comment.user_id != self.producer_id
                or comment.user_type != "Bot"
                or not comment.body.startswith(_PREFIX)
            ):
                continue
            source, candidate = parse_manifest_body(comment.body)
            self._scope(candidate)
            same_subject = (
                candidate.transaction_id,
                candidate.repository_id,
                candidate.pull_request,
            ) == (receipt.transaction_id, receipt.repository_id, receipt.pull_request)
            if same_subject or source == self.source_sha256:
                if (
                    source != self.source_sha256
                    or candidate != receipt
                    or comment.body != expected
                ):
                    raise ReviewInputError(
                        "native manifest immutable settlement conflicts"
                    )
                matches.append(comment.comment_id)
        if len(matches) != 1 or known_id is not None and matches[0] != known_id:
            raise GitHubPublicationTransientError(
                "native manifest remains unconfirmed; no replacement was dispatched"
            )
        return PublicationReceipt(
            "issue:" + str(matches[0]), hashlib.sha256(expected.encode()).hexdigest()
        )

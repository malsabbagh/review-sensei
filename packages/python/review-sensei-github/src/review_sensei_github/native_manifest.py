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
from review_sensei.models import ReviewTransaction
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

    def select(
        self, transaction: ReviewTransaction, *, result_digest: str
    ) -> tuple[NativeUnionReceipt, PublicationReceipt] | None:
        """Discover a cold receipt from independently selected original state.

        The manifest never chooses a transaction or grants publication. Both
        logical subject and physical checkpoint source must match; a second
        owned version, including one with another source digest, withholds.
        """
        if self.port._primed is not None:
            raise ReviewInputError("native manifest original selection is invalid")
        return self._select_comments(
            transaction, result_digest=result_digest, comments=self.port.list_comments()
        )

    def select_after_admission(self, transaction, *, result_digest, store):
        from .default_human_inventory import HumanInventoryStore

        if type(store) is not HumanInventoryStore or store.port is not self.port:
            raise ReviewInputError("native manifest admission reader changed")
        comments = store.comments_after_read()
        if comments is None:
            return self.select(transaction, result_digest=result_digest)
        return self._select_comments(
            transaction, result_digest=result_digest, comments=comments
        )

    def _select_comments(self, transaction, *, result_digest, comments):
        if (
            type(transaction) is not ReviewTransaction
            or not isinstance(result_digest, str)
            or _SHA.fullmatch(result_digest) is None
            or self.port._primed is not None
        ):
            raise ReviewInputError("native manifest original selection is invalid")
        matches = []
        for comment in comments:
            if (
                comment.user_id != self.producer_id
                or comment.user_type != "Bot"
                or not comment.body.startswith(_PREFIX)
            ):
                continue
            source, candidate = parse_manifest_body(comment.body)
            self._scope(candidate)
            if (
                source != self.source_sha256
                and candidate.transaction_id != transaction.transaction_id
            ):
                continue
            if (
                source != self.source_sha256
                or candidate.transaction_id != transaction.transaction_id
                or candidate.result_sha256 != transaction.result_sha256
                or candidate.result_digest != result_digest
                or candidate.head_sha != transaction.head_sha
                or candidate.base_sha != transaction.base_sha
            ):
                raise ReviewInputError("native manifest original selection conflicts")
            matches.append(
                (
                    candidate,
                    PublicationReceipt(
                        "issue:" + str(comment.comment_id),
                        hashlib.sha256(comment.body.encode()).hexdigest(),
                    ),
                )
            )
        if len(matches) > 1:
            raise ReviewInputError("native manifest original selection is duplicated")
        return matches[0] if matches else None


@dataclass(frozen=True)
class NativePublicationReader:
    """Cold union read bound to independently authenticated original admission.

    The host selects the current review and qualifies historical thread targets
    before constructing this reader. Neither a manifest nor this read permits a
    write, narrows conversation/report-conflict readers, or settles old effects.
    Every invocation rereads source, admission, manifest and the complete union.
    """

    store: object
    review_bytes: bytes
    old_root_nodes: tuple[tuple[int, str], ...] = ()

    @classmethod
    def for_review(cls, store, review, *, old_root_nodes=None):
        from .default_human_inventory import HumanInventoryStore

        if type(store) is not HumanInventoryStore or not isinstance(review, dict):
            raise ReviewInputError("native publication reader source is invalid")
        targets = {} if old_root_nodes is None else dict(old_root_nodes)
        return cls(store, canonical_bytes(review), tuple(sorted(targets.items())))

    def __call__(self, receipt: NativeUnionReceipt):
        from review_sensei.models import ReviewResult

        from .checkpoint_result_store import uses_grouped_publication
        from .default_human_inventory import HumanInventoryStore
        from .default_human_reader import restore_admitted_document
        from .native_review_census import read_publication_union
        from .native_review_groups import NativeWalkBudget
        from .native_union import native_union_frame_document
        from .publication import parse_finding_index

        store = self.store
        if (
            type(store) is not HumanInventoryStore
            or type(receipt) is not NativeUnionReceipt
        ):
            raise ReviewInputError(
                "native publication reader original source is invalid"
            )
        context = store.context
        available = store.read_available()
        if available is None:
            raise ReviewInputError(
                "native publication original admission is unavailable"
            )
        # _checkpoint_source authenticates the physical checkpoint carriers and
        # their accepted-operation source against the independently selected
        # store transaction. The manifest cannot choose any of those inputs.
        source_reference, payload = store.checkpoint_source_after_read()
        original = ReviewResult.from_dict(json.loads(payload))
        transaction = original.transaction
        if (
            transaction is None
            or transaction.transaction_id != context.transaction_id
            or original.content_digest() != context.original_content_digest
            or not uses_grouped_publication(store.http, original)
            or (receipt.repository_id, receipt.pull_request, receipt.producer_id)
            != (context.repository_id, context.pull_request, context.producer_id)
            or receipt.transaction_id != transaction.transaction_id
            or receipt.result_sha256 != transaction.result_sha256
            or receipt.result_digest != context.publication_digest
            or (receipt.head_sha, receipt.base_sha)
            != (context.head_sha, context.base_sha)
        ):
            raise ReviewInputError("native publication original checkpoint conflicts")
        _, admitted = available
        restored = restore_admitted_document(original, store, admitted)
        index = parse_finding_index(
            admitted["finding_index"],
            repository_id=context.repository_id,
            pull_request=context.pull_request,
            head_sha=context.head_sha,
        )
        if index is None:
            raise ReviewInputError("native publication original index is unavailable")
        manifest_store = NativeManifestStore(
            store.port,
            SessionIdentity(
                context.repository, context.pull_request, context.repository_id
            ),
            context.producer_id,
            hashlib.sha256(canonical_bytes(source_reference)).hexdigest(),
        )
        selected = manifest_store.select_after_admission(
            transaction, result_digest=context.publication_digest, store=store
        )
        if selected is None or selected[0] != receipt:
            raise ReviewInputError("native publication original manifest conflicts")
        review = json.loads(self.review_bytes)
        from review_sensei.publication_staging import PublicationStaging

        record = store.transaction_after_read()
        frontier = (
            None
            if record is None or record.publication_staging is None
            else PublicationStaging.from_document(record.publication_staging)
        )
        if (
            record is None
            or record.transaction is None
            or not record.transaction.logical_identity_matches(transaction)
            or frontier is None
            or frontier.final_receipt is None
            or frontier.plan_sha256 != receipt.plan_sha256
            or frontier.final_receipt.native_id != str(review.get("id"))
        ):
            raise ReviewInputError(
                "native publication original final review identity changed"
            )
        user = review.get("user")
        if (
            type(review.get("id")) is not int
            or review["id"] <= 0
            or review.get("state") not in {"COMMENTED", "CHANGES_REQUESTED"}
            or review.get("commit_id") != context.head_sha
            or not isinstance(user, dict)
            or user.get("type") != "Bot"
            or type(user.get("id")) is not int
            or user["id"] != context.producer_id
            or not isinstance(user.get("login"), str)
            or user["login"].casefold() != context.app_slug.casefold()
            or review.get("pull_request_url")
            != store.http.api_url
            + store.http.repository_path(
                context.repository, f"/pulls/{context.pull_request}"
            )
        ):
            raise ReviewInputError("native publication selected review scope conflicts")
        # A later legitimate assessment may change the current authority frame.
        # Bind this fresh complete frame, while part receipts remain original.
        document = native_union_frame_document(review.get("body"), receipt)
        published_index = parse_finding_index(
            review["body"],
            repository_id=context.repository_id,
            pull_request=context.pull_request,
            head_sha=context.head_sha,
        )
        if published_index != index:
            raise ReviewInputError("native publication selected review index conflicts")
        plan = receipt.reconstruct_plan(restored, index, main_body=review["body"])
        evidence = read_publication_union(
            store.http,
            token=store.token,
            repository=context.repository,
            plan=plan,
            producer_id=context.producer_id,
            group_review_ids=tuple(int(part.native_id) for part in receipt.parts),
            main_review_id=review["id"],
            main_body=review["body"],
            main_state=review["state"],
            old_root_nodes=dict(self.old_root_nodes),
            budget=NativeWalkBudget(),
        )
        receipt.require_evidence(
            evidence,
            document_sha256=hashlib.sha256(canonical_bytes(document)).hexdigest(),
        )
        store.revalidate_native_snapshot()
        return evidence

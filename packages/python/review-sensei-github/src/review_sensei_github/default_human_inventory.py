"""Purpose-bound storage of complete admitted human inventory for default roots.

These parts are evidence, never activation authority. Callers independently select
an original durable transaction and budget; no field in a reference supplies them.
Only the original live publication owner may explicitly authorize starting writes.
An intent without complete parts is held, including after a crash or lost response.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from review_sensei.bounded_evidence import EvidenceReadBudget, canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.human_inventory import inventory_from_result_document
from review_sensei.models import ReviewTransaction
from review_sensei.session import SessionIdentity, SessionLedger

from .http import GitHubHttp
from .operation_comments import (
    GitHubIssueCommentOperationPort,
    ResultPartStore,
    ensure_result_part_capacity,
)
from .operation_host import (
    OperationComment,
    bounded_comment,
    comment_payload,
    render_marked_comment,
)
from .prose_publication import VisibleProseReceipt
from .publication import finding_index_marker, parse_finding_index
from .publication_recovery import publication_http, publication_session_ledger

PURPOSE = "default-human-inventory"
INTENT_MARKER = "<!-- reviewsensei:default-human-inventory-intent:v1 -->"


def _digest(value: object, width: int = 64) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(f"[a-f0-9]{{{width}}}", value) is not None
    )


@dataclass(frozen=True)
class HumanInventoryReference:
    """Small closed transport reference; hashes confer no publication authority."""

    event_key: str
    payload_digest: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.event_key, str)
            or re.fullmatch(r"[a-f0-9]{64}\t[a-f0-9]{64}", self.event_key) is None
            or not _digest(self.payload_digest)
        ):
            raise ReviewInputError("default human inventory reference is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "1.0",
            "purpose": PURPOSE,
            "event_key": self.event_key,
            "payload_digest": self.payload_digest,
        }

    @classmethod
    def from_dict(cls, value: object) -> HumanInventoryReference:
        if (
            not isinstance(value, dict)
            or set(value)
            != {"schema_version", "purpose", "event_key", "payload_digest"}
            or value["schema_version"] != "1.0"
            or value["purpose"] != PURPOSE
        ):
            raise ReviewInputError("default human inventory reference is invalid")
        return cls(value["event_key"], value["payload_digest"])


@dataclass(frozen=True)
class HumanInventoryContext:
    """Independently authenticated expected scope, never restored from a root."""

    repository: str
    repository_id: int
    pull_request: int
    base_sha: str
    head_sha: str
    app_slug: str
    producer_id: int
    transaction_id: str
    original_content_digest: str
    publication_digest: str
    policy_digest: str
    configuration_digest: str
    evidence_digest: str
    budget: EvidenceReadBudget

    def __post_init__(self) -> None:
        SessionIdentity(self.repository, self.pull_request, self.repository_id)
        if (
            type(self.repository_id) is not int
            or type(self.producer_id) is not int
            or self.producer_id <= 0
            or not isinstance(self.app_slug, str)
            or re.fullmatch(r"[A-Za-z0-9_-]+\[bot\]", self.app_slug, re.I | re.ASCII)
            is None
            or not isinstance(self.budget, EvidenceReadBudget)
            or not all(_digest(value, 40) for value in (self.base_sha, self.head_sha))
            or not all(
                _digest(getattr(self, field))
                for field in (
                    "transaction_id",
                    "original_content_digest",
                    "publication_digest",
                    "policy_digest",
                    "configuration_digest",
                    "evidence_digest",
                )
            )
        ):
            raise ReviewInputError("default human inventory trusted context is invalid")

    def binding(self) -> dict[str, object]:
        return {
            "purpose": PURPOSE,
            "schema_version": "1.0",
            **{
                field: getattr(self, field)
                for field in (
                    "repository",
                    "repository_id",
                    "pull_request",
                    "base_sha",
                    "head_sha",
                    "producer_id",
                    "transaction_id",
                    "original_content_digest",
                    "publication_digest",
                    "policy_digest",
                    "configuration_digest",
                    "evidence_digest",
                )
            },
        }

    @property
    def event_key(self) -> str:
        scope = hashlib.sha256(
            canonical_bytes(
                {
                    "purpose": PURPOSE,
                    "repository": self.repository,
                    "repository_id": self.repository_id,
                    "pull_request": self.pull_request,
                }
            )
        ).hexdigest()
        event = hashlib.sha256(canonical_bytes(self.binding())).hexdigest()
        return scope + "\t" + event


def _transaction(
    ledger: SessionLedger, context: HumanInventoryContext
) -> ReviewTransaction:
    context.budget.check()
    loaded = ledger.load(
        SessionIdentity(context.repository, context.pull_request, context.repository_id)
    )
    context.budget.check()
    record = loaded.record
    transaction = record.transaction if record is not None else None
    if (
        loaded.status not in {"ok", "migrated"}
        or record is None
        or record.repository_id != context.repository_id
        or transaction is None
        or transaction.phase
        not in {"publication_pending", "publication_failed", "publication_succeeded"}
        or transaction.transaction_id != context.transaction_id
        or transaction.result_sha256 != context.original_content_digest
        or not transaction.identity_matches(
            repository=context.repository,
            pull_request=context.pull_request,
            base_sha=context.base_sha,
            head_sha=context.head_sha,
            policy_digest=context.policy_digest,
            configuration_digest=context.configuration_digest,
            evidence_digest=context.evidence_digest,
        )
    ):
        raise ReviewInputError("default human inventory durable authority conflicts")
    return transaction


def _get(http: GitHubHttp, token: str, path: str) -> tuple[int, Any]:
    try:
        return http.request("GET", path, token=token)
    except ReviewInputError:
        raise
    except Exception:
        raise ReviewInputError(
            "default human inventory authority transport unavailable"
        ) from None


def _live(http: GitHubHttp, token: str, context: HumanInventoryContext) -> None:
    status, user = _get(http, token, "/users/" + quote(context.app_slug, safe=""))
    if (
        status != 200
        or not isinstance(user, dict)
        or type(user.get("id")) is not int
        or user["id"] != context.producer_id
        or user.get("type") != "Bot"
        or not isinstance(user.get("login"), str)
        or user["login"].casefold() != context.app_slug.casefold()
    ):
        raise ReviewInputError("default human inventory Bot identity conflicts")
    _live_pr(http, token, context)


def _live_pr(http: GitHubHttp, token: str, context: HumanInventoryContext) -> None:
    status, pr = _get(
        http,
        token,
        http.repository_path(context.repository, f"/pulls/{context.pull_request}"),
    )
    base = pr.get("base") if isinstance(pr, dict) else None
    head = pr.get("head") if isinstance(pr, dict) else None
    repo = base.get("repo") if isinstance(base, dict) else None
    if (
        status != 200
        or not isinstance(pr, dict)
        or pr.get("state") != "open"
        or not isinstance(base, dict)
        or base.get("sha") != context.base_sha
        or not isinstance(head, dict)
        or head.get("sha") != context.head_sha
        or not isinstance(repo, dict)
        or type(repo.get("id")) is not int
        or repo["id"] != context.repository_id
        or repo.get("full_name") != context.repository
    ):
        raise ReviewInputError("default human inventory current pull request conflicts")


def build_human_inventory_context(
    *,
    http: GitHubHttp,
    token: str,
    ledger: SessionLedger,
    identity: SessionIdentity,
    base_sha: str,
    head_sha: str,
    app_slug: str,
    publication_digest: str,
    policy_digest: str,
    configuration_digest: str,
    evidence_digest: str,
    budget: EvidenceReadBudget,
) -> HumanInventoryContext:
    """Select durable transaction independently, then authenticate live Bot and PR."""
    budget.check()
    if (
        not isinstance(app_slug, str)
        or re.fullmatch(r"[A-Za-z0-9_-]+\[bot\]", app_slug, re.I | re.ASCII) is None
    ):
        raise ReviewInputError(
            "default human inventory configured Bot login is invalid"
        )
    ledger = publication_session_ledger(ledger, budget)
    loaded = ledger.load(identity)
    budget.check()
    transaction = loaded.record.transaction if loaded.record is not None else None
    if (
        loaded.status not in {"ok", "migrated"}
        or transaction is None
        or transaction.result_sha256 is None
        or identity.repository_id is None
    ):
        raise ReviewInputError(
            "default human inventory requires an independent checkpoint"
        )
    transport = publication_http(http, budget)
    # Authenticate the producer before placing its numeric identity in context.
    status, user = _get(transport, token, "/users/" + quote(app_slug, safe=""))
    if (
        status != 200
        or not isinstance(user, dict)
        or type(user.get("id")) is not int
        or user["id"] <= 0
        or user.get("type") != "Bot"
        or not isinstance(user.get("login"), str)
        or user["login"].casefold() != app_slug.casefold()
    ):
        raise ReviewInputError("default human inventory Bot identity unavailable")
    context = HumanInventoryContext(
        identity.repository,
        identity.repository_id,
        identity.pull_request,
        base_sha,
        head_sha,
        app_slug,
        user["id"],
        transaction.transaction_id,
        transaction.result_sha256,
        publication_digest,
        policy_digest,
        configuration_digest,
        evidence_digest,
        budget,
    )
    _transaction(ledger, context)
    _live_pr(transport, token, context)
    return context


class _SnapshotPort:
    """One complete owned list already charged to this operation."""

    def __init__(
        self,
        port: GitHubIssueCommentOperationPort,
        comments: tuple[OperationComment, ...],
    ):
        self.port, self.comments = port, comments
        self.requests = 0

    def list_comments(self) -> tuple[OperationComment, ...]:
        return self.comments

    def create(self, body: str) -> OperationComment:
        before = self.port.requests
        try:
            return self.port.create(body)
        finally:
            self.requests += self.port.requests - before

    def update(self, comment_id: int, body: str) -> OperationComment:
        before = self.port.requests
        try:
            return self.port.update(comment_id, body)
        finally:
            self.requests += self.port.requests - before


class HumanInventoryStore:
    """Closed admitted-evidence envelope over the audited bounded part transport.

    ResultPartStore supplies opaque byte framing, not ReviewResult admission.
    This adapter validates its distinct purpose document before storing bytes.
    No root, check, approval, or session schema is changed by this store.
    """

    def __init__(
        self,
        http: GitHubHttp,
        token: str,
        ledger: SessionLedger,
        context: HumanInventoryContext,
    ) -> None:
        if not isinstance(context, HumanInventoryContext):
            raise ReviewInputError("default human inventory context is required")
        self.context = context
        self.http = publication_http(http, context.budget)
        self.token = token
        self.ledger = publication_session_ledger(ledger, context.budget)
        self._authenticated_snapshot: (
            tuple[HumanInventoryReference, bytes, bytes] | None
        ) = None
        self.port = GitHubIssueCommentOperationPort(
            self.http,
            token=token,
            repository=context.repository,
            pull_request=context.pull_request,
            app_user_id=context.producer_id,
            strict_association=True,
        )

    @property
    def authenticated_reference(self) -> HumanInventoryReference | None:
        """A prior complete read in this store, never caller-supplied authority."""
        snapshot = self._authenticated_snapshot
        return snapshot[0] if snapshot is not None else None

    def _remember_snapshot(
        self, reference: HumanInventoryReference, payload: bytes, intent: dict[str, Any]
    ) -> dict[str, object]:
        document = self._restore(payload)
        self._authenticated_snapshot = (reference, payload, canonical_bytes(intent))
        return document

    def read_authenticated_snapshot(
        self, reference: HumanInventoryReference
    ) -> dict[str, object]:
        """Reuse admitted bytes only after fresh complete mutable storage fences.

        Parts and intent can be edited or duplicated. A fresh full enumeration
        and ordinary read detect those changes; the prior payload supplies no
        authority. Reusing this store avoids reconstructing its independently
        selected context in the same finite operation.
        """
        snapshot = self._authenticated_snapshot
        if snapshot is None or snapshot[0] != reference:
            raise ReviewInputError(
                "default human inventory authenticated snapshot missing"
            )
        try:
            self.read(reference)
            if self._authenticated_snapshot != snapshot:
                raise ReviewInputError(
                    "default human inventory authenticated snapshot changed"
                )
        finally:
            # A failed handoff cannot replace the original authenticated intent
            # with the changed live bytes for a subsequent retry.
            self._authenticated_snapshot = snapshot
        return self._restore(snapshot[1])

    def _authority(self) -> None:
        _transaction(self.ledger, self.context)
        _live(self.http, self.token, self.context)

    def revalidate_authority(self) -> None:
        """Freshly fence mutable transaction, configured Bot and live PR scope."""
        self._authority()

    def revalidate_snapshot(self) -> None:
        """Fence mutable scope after this operation authenticated its producer.

        Readers compare every root/inline numeric producer against the original
        authenticated identity. The configured Bot lookup need not be repeated
        before every read-only scan within the same finite operation.
        """
        _transaction(self.ledger, self.context)
        _live_pr(self.http, self.token, self.context)

    def _document(self, document: object) -> dict[str, object]:
        context = self.context
        if not isinstance(document, dict) or set(document) != {
            "human_inventory",
            "finding_index",
            "visible_prose",
        }:
            raise ReviewInputError(
                "default human inventory document fields are invalid"
            )
        inventory = (
            inventory_from_result_document(
                document["human_inventory"],
                result_digest=context.publication_digest,
                base_sha=context.base_sha,
            )
            if document["human_inventory"] is not None
            else None
        )
        index = parse_finding_index(
            document["finding_index"],
            repository_id=context.repository_id,
            pull_request=context.pull_request,
            head_sha=context.head_sha,
        )
        if (
            index is None
            or index.base_sha != context.base_sha
            or index.result_digest != context.publication_digest
            or finding_index_marker(
                repository_id=context.repository_id,
                pull_request=context.pull_request,
                head_sha=context.head_sha,
                base_sha=context.base_sha,
                result_digest=context.publication_digest,
                entries=index.entries,
                complete_inventory=True,
            )
            != document["finding_index"]
        ):
            raise ReviewInputError("default human inventory finding index conflicts")
        human_ids = (
            {finding.fingerprint for finding in inventory.findings}
            if inventory is not None
            else set()
        )
        indexed_humans = {
            entry.instance for entry in index.entries if entry.needs_human
        }
        if human_ids != indexed_humans:
            raise ReviewInputError(
                "default human inventory admitted identities conflict"
            )
        raw_receipts = document["visible_prose"]
        if not isinstance(raw_receipts, list) or len(raw_receipts) > 32:
            raise ReviewInputError("default human inventory prose receipts are invalid")
        receipts = tuple(VisibleProseReceipt.from_dict(item) for item in raw_receipts)
        instances = [instance for receipt in receipts for instance in receipt.instances]
        if (
            any(
                receipt.index != index
                or receipt.head_sha != context.head_sha
                or receipt.producer_id != context.producer_id
                for index, receipt in enumerate(receipts)
            )
            or len({receipt.review_id for receipt in receipts}) != len(receipts)
            or len(set(instances)) != len(instances)
            or sum(receipt.bytes for receipt in receipts) > 1_048_576
            or not set(instances) <= {entry.instance for entry in index.entries}
        ):
            raise ReviewInputError("default human inventory prose receipts conflict")
        # Return a detached canonical snapshot, not the caller's mutable object.
        return json.loads(canonical_bytes(document))

    def _payload(self, document: object) -> bytes:
        envelope = {
            "schema_version": "1.0",
            "purpose": PURPOSE,
            "binding": self.context.binding(),
            "document": self._document(document),
        }
        payload = canonical_bytes(envelope)
        ensure_result_part_capacity(len(payload))
        return payload

    def _restore(self, payload: bytes) -> dict[str, object]:
        ensure_result_part_capacity(len(payload))
        envelope = json.loads(payload)
        if (
            not isinstance(envelope, dict)
            or set(envelope) != {"schema_version", "purpose", "binding", "document"}
            or envelope["schema_version"] != "1.0"
            or envelope["purpose"] != PURPOSE
            or envelope["binding"] != self.context.binding()
            or canonical_bytes(envelope) != payload
        ):
            raise ReviewInputError("default human inventory stored authority conflicts")
        return self._document(envelope["document"])

    def _intent(self, comments: tuple[OperationComment, ...]) -> dict[str, Any] | None:
        found = []
        for item in comments:
            if (
                INTENT_MARKER not in item.body
                or item.user_id != self.context.producer_id
                or item.user_type != "Bot"
            ):
                continue
            bounded_comment(item.body)
            payload = json.loads(comment_payload(item.body))
            if not isinstance(payload, dict):
                raise ReviewInputError("default human inventory intent is malformed")
            if payload.get("event_key") != self.context.event_key:
                continue
            if (
                set(payload)
                != {
                    "schema_version",
                    "purpose",
                    "event_key",
                    "binding",
                    "payload_digest",
                    "part_count",
                    "owner",
                }
                or payload["schema_version"] != "1.0"
                or payload["purpose"] != PURPOSE
                or payload["binding"] != self.context.binding()
                or not _digest(payload["payload_digest"])
                or not _digest(payload["owner"], 32)
                or type(payload["part_count"]) is not int
                or not 1 <= payload["part_count"] <= 32
                or item.body != render_marked_comment(INTENT_MARKER, payload)
            ):
                raise ReviewInputError("default human inventory intent conflicts")
            found.append(payload)
        if len(found) > 1:
            raise ReviewInputError(
                "default human inventory intent is duplicated; reconcile owned state"
            )
        return found[0] if found else None

    def _parts(self, comments: tuple[OperationComment, ...]) -> ResultPartStore:
        return ResultPartStore(
            _SnapshotPort(self.port, comments), app_user_id=self.context.producer_id
        )

    def stage(
        self, document: dict[str, object], *, allow_start: bool = False
    ) -> HumanInventoryReference:
        try:
            return self._stage(document, allow_start=allow_start)
        except ReviewInputError:
            raise
        except Exception:
            raise ReviewInputError(
                "default human inventory staging unavailable; retain original intent and parts"
            ) from None

    def _stage(
        self, document: dict[str, object], *, allow_start: bool = False
    ) -> HumanInventoryReference:
        """Reconcile complete parts; starting requires original live admission."""
        if not isinstance(allow_start, bool):
            raise ReviewInputError("default human inventory start authority is invalid")
        payload = self._payload(document)
        digest = hashlib.sha256(payload).hexdigest()
        reference = HumanInventoryReference(self.context.event_key, digest)
        self._authority()
        comments = self.port.list_comments()
        intent = self._intent(comments)
        available = self._parts(comments).read_available(event_key=reference.event_key)
        if intent is not None:
            if intent["payload_digest"] != digest or intent[
                "part_count"
            ] != ensure_result_part_capacity(len(payload)):
                raise ReviewInputError(
                    "default human inventory staging identity conflicts"
                )
            if available is None or available != (digest, payload):
                raise ReviewInputError(
                    "default human inventory write outcome unknown; retain original intent and parts"
                )
            self._remember_snapshot(reference, available[1], intent)
            return reference
        if available is not None:
            raise ReviewInputError(
                "default human inventory parts have no original staging intent"
            )
        if not allow_start:
            raise ReviewInputError(
                "default human inventory has no confirmed complete original staging; fresh recovery cannot start writes"
            )
        count = ensure_result_part_capacity(len(payload))
        pages = (len(comments) + count + 1) // 100 + 1
        self.context.budget.preflight(count + 3 * pages + 4)
        intent = {
            "schema_version": "1.0",
            "purpose": PURPOSE,
            "event_key": reference.event_key,
            "binding": self.context.binding(),
            "payload_digest": digest,
            "part_count": count,
            "owner": uuid.uuid4().hex,
        }
        try:
            self.port.create(render_marked_comment(INTENT_MARKER, intent))
        except Exception:
            # Even a persisted intent does not authorize a fresh writer to start.
            raise ReviewInputError(
                "default human inventory intent outcome unknown; reconcile without new writes"
            ) from None
        comments = self.port.list_comments()
        if self._intent(comments) != intent:
            raise ReviewInputError(
                "default human inventory intent changed before staging"
            )
        try:
            self._parts(comments).write(event_key=reference.event_key, result=payload)
        except Exception:
            # One read-only reconciliation; never retry a POST or release intent.
            comments = self.port.list_comments()
            if self._intent(comments) != intent or self._parts(comments).read_available(
                event_key=reference.event_key
            ) != (digest, payload):
                raise ReviewInputError(
                    "default human inventory write outcome unknown; retain original intent and parts"
                ) from None
        self.read(reference)
        return reference

    def read_available(
        self,
    ) -> tuple[HumanInventoryReference, dict[str, object]] | None:
        """Discover evidence only under independently authenticated transaction scope.

        Absence is not a clean admitted result. Recovery callers must explicitly
        hold when this returns None; an intent with incomplete parts raises.
        """
        try:
            return self._read_available()
        except ReviewInputError:
            raise
        except Exception:
            raise ReviewInputError(
                "default human inventory read unavailable; retain original intent and parts"
            ) from None

    def _read_available(
        self,
    ) -> tuple[HumanInventoryReference, dict[str, object]] | None:
        self._authority()
        comments = self.port.list_comments()
        intent = self._intent(comments)
        if intent is None:
            return None
        reference = HumanInventoryReference(
            self.context.event_key, str(intent["payload_digest"])
        )
        payload = self._parts(comments).read(
            event_key=reference.event_key, payload_digest=reference.payload_digest
        )
        if ensure_result_part_capacity(len(payload)) != intent["part_count"]:
            raise ReviewInputError("default human inventory part count conflicts")
        return reference, self._remember_snapshot(reference, payload, intent)

    def read(self, reference: HumanInventoryReference) -> dict[str, object]:
        try:
            return self._read(reference)
        except ReviewInputError:
            raise
        except Exception:
            raise ReviewInputError(
                "default human inventory read unavailable; retain original intent and parts"
            ) from None

    def _read(self, reference: HumanInventoryReference) -> dict[str, object]:
        if (
            not isinstance(reference, HumanInventoryReference)
            or reference.event_key != self.context.event_key
        ):
            raise ReviewInputError("default human inventory reference scope conflicts")
        self._authority()
        comments = self.port.list_comments()
        intent = self._intent(comments)
        if intent is None or intent["payload_digest"] != reference.payload_digest:
            raise ReviewInputError(
                "default human inventory original staging intent is missing or conflicting"
            )
        payload = self._parts(comments).read(
            event_key=reference.event_key, payload_digest=reference.payload_digest
        )
        if ensure_result_part_capacity(len(payload)) != intent["part_count"]:
            raise ReviewInputError("default human inventory part count conflicts")
        return self._remember_snapshot(reference, payload, intent)

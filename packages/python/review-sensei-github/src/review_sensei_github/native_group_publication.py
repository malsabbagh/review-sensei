"""One attempted native group at a time, durably bound to the original result.

This protocol is not an activation or a grant. The guarded application supplies
complete pre-effect admission and fresh installed-source/owner/head fences.
Parts remain nonauthoritative until a separately verified final union frame.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewTransaction
from review_sensei.publication_staging import (
    PublicationStaging,
    checkpoint_publication_staging,
)
from review_sensei.session import SessionIdentity, SessionLedger, SessionRecord

from .errors import GitHubHTTPTransientError, GitHubPublicationTransientError
from .http import GitHubHttp
from .native_manifest import NativeManifestStore
from .native_review_census import (
    NativePublicationEvidence,
    open_publication_census,
    read_publication_union,
)
from .native_review_groups import (
    NativeGroupEvidence,
    NativeReviewPlan,
    NativeWalkBudget,
    _native_number,
    read_complete_group,
)
from .native_union import NativeUnionReceipt, native_union_frame_document
from .operation_host import OperationWriteUnconfirmed


@dataclass
class NativeGroupPublication:
    http: GitHubHttp
    ledger: SessionLedger
    identity: SessionIdentity
    transaction: ReviewTransaction
    plan: NativeReviewPlan
    token: str
    producer_id: int
    admission: Callable[[NativeReviewPlan], None]
    authority_fence: Callable[[], bool]
    now: datetime | None = None
    manifest_store: NativeManifestStore | None = None
    _physical_attempts: dict[str, tuple[Any, int]] = field(
        default_factory=dict, init=False, repr=False
    )
    _frontier_record: SessionRecord | None = field(default=None, init=False, repr=False)
    _claim_receipt: tuple[Any, str, str, int, str] | None = field(
        default=None, init=False, repr=False
    )

    def _remember_dispatch(self, claim: str) -> None:
        from .caller_operation import caller_for_http

        caller = caller_for_http(self.http)
        if caller is not None:
            self._physical_attempts[claim] = (caller, len(caller.observations))

    def _settle_original_dispatch(self, claim: str) -> None:
        """Positive exact readback settles only this process's original effect.

        Native charges and physical read costs remain unchanged. No observation
        is invented and no cold/foreign actor can mark an earlier request done.
        """
        from .caller_operation import caller_for_http

        retained = self._physical_attempts.get(claim)
        if retained is None:
            return
        caller, start = retained
        if caller_for_http(self.http) is not caller:
            raise ReviewInputError("native dispatch reconciliation owner changed")
        writes = [item for item in caller.observations[start:] if item.get("mutation")]
        if len(writes) != 1:
            raise ReviewInputError(
                "native dispatch reconciliation request is ambiguous"
            )
        if writes[0]["outcome"] in {"unknown", "expired"} or writes[0].get(
            "uncertain_effect"
        ):
            caller._reconciled_mutations.add(str(writes[0]["request_id"]))
        self._physical_attempts.pop(claim)

    def fence(self) -> SessionRecord:
        current = self.ledger.load(self.identity, now=self.now)
        if (
            current.status != "ok"
            or current.record is None
            or current.record.transaction is None
            or not current.record.transaction.logical_identity_matches(self.transaction)
            or current.record.transaction.phase
            not in {"publication_pending", "publication_failed"}
            or self.identity.repository_id != self.plan.repository_id
            or self.identity.pull_request != self.plan.pull_request
            or self.transaction.transaction_id != self.plan.transaction_id
            or self.transaction.result_sha256 != self.plan.result_sha256
            or self.transaction.head_sha != self.plan.head_sha
            or self.transaction.base_sha != self.plan.base_sha
            or not self.authority_fence()
        ):
            raise ReviewInputError("native grouped publication authority changed")
        self._frontier_record = current.record
        return current.record

    def adopt_read_frontier(self, reader) -> None:
        """Use this operation's complete hold read for read-only staging choice.

        No mutation or claim is authorized by this handoff. Each actual staging
        CAS still discovers the whole ledger and confirms its own exact root.
        """
        from .api_run_requests import ApiRunSessionLedger
        from .quarantine_holds import QuarantineHoldReader

        if (
            type(reader) is not QuarantineHoldReader
            or not isinstance(self.ledger, ApiRunSessionLedger)
            or not isinstance(reader.ledger, ApiRunSessionLedger)
            or reader.ledger.caller is not self.ledger.caller
            or (reader.repository, reader.pull_request)
            != (self.identity.repository, self.identity.pull_request)
        ):
            raise ReviewInputError("native publication complete read owner changed")
        record = reader.record_after_read()
        if (
            record.transaction is None
            or not record.transaction.logical_identity_matches(self.transaction)
            or record.transaction.phase
            not in {"publication_pending", "publication_failed"}
        ):
            raise ReviewInputError(
                "native publication complete read transaction changed"
            )
        self._frontier_record = record

    def _current_frontier(self) -> SessionRecord:
        """Reuse an own process's confirmed frontier only for read-only work.

        Every new native dispatch uses the exact own-CAS receipt below. Every
        staging mutation uses the native ledger's complete CAS census, actor,
        live-head and protected confirmation checks. Cold readers have no cache.
        """
        from .api_run_requests import ApiRunSessionLedger

        record = self._frontier_record
        if (
            isinstance(self.ledger, ApiRunSessionLedger)
            and record is not None
            and self.ledger.caller._checkpoint is None
            and record.record_sha256 == self.ledger.caller._root
            and record.transaction is not None
            and record.transaction.logical_identity_matches(self.transaction)
            and record.transaction.phase
            in {"publication_pending", "publication_failed"}
        ):
            return record
        return self.fence()

    def state(self) -> PublicationStaging | None:
        record = self._current_frontier()
        value = record.publication_staging
        return None if value is None else PublicationStaging.from_document(value)

    def checkpoint(
        self, before: PublicationStaging | None, after: PublicationStaging
    ) -> PublicationStaging:
        from .api_run_requests import ApiRunSessionLedger

        native = (
            isinstance(self.ledger, ApiRunSessionLedger)
            and self.ledger._quarantine_required
        )
        if not native:
            self.fence()
        if (
            (
                after.transaction_id,
                after.result_sha256,
                after.plan_sha256,
                after.part_count,
            )
            != (
                self.plan.transaction_id,
                self.plan.result_sha256,
                self.plan.sha256,
                len(self.plan.groups),
            )
            or self.transaction.transaction_id != self.plan.transaction_id
            or self.transaction.result_sha256 != self.plan.result_sha256
            or (self.transaction.head_sha, self.transaction.base_sha)
            != (self.plan.head_sha, self.plan.base_sha)
            or (self.identity.repository_id, self.identity.pull_request)
            != (self.plan.repository_id, self.plan.pull_request)
        ):
            raise ReviewInputError("native publication checkpoint plan changed")
        confirmed = checkpoint_publication_staging(
            self.ledger,
            self.identity,
            self.transaction,
            expected=before,
            after=after,
            now=self.now,
        )
        if (
            isinstance(self.ledger, ApiRunSessionLedger)
            and self.ledger._quarantine_required
        ):
            from .quarantine_checkpoint import take_confirmed_checkpoint

            caller = self.ledger.caller
            receipt = take_confirmed_checkpoint(
                caller,
                self.ledger,
                self.identity,
                staging=confirmed.to_document(),
            )
            self._frontier_record = receipt.record
            self._claim_receipt = (
                caller,
                receipt.record.record_sha256,
                receipt.block,
                receipt.comment_id,
                receipt.body,
            )
        return confirmed

    def require_claim(self, state: PublicationStaging) -> None:
        """Only the caller with this positive own attempt CAS may dispatch it."""
        from .api_run_requests import ApiRunSessionLedger
        from .caller_operation import http_read_operation

        if (
            not isinstance(self.ledger, ApiRunSessionLedger)
            or not self.ledger._quarantine_required
        ):
            if self.state() != state:
                raise ReviewInputError("native publication attempt claim changed")
            return
        receipt, self._claim_receipt = self._claim_receipt, None
        caller = self.ledger.caller
        if (
            receipt is None
            or receipt[0] is not caller
            or caller._root != receipt[1]
            or caller._block != receipt[2]
            or caller._checkpoint is not None
            or self._frontier_record is None
            or self._frontier_record.publication_staging != state.to_document()
        ):
            raise ReviewInputError(
                "native publication attempt has no own checkpoint receipt"
            )
        with http_read_operation(self.http, "native-claim:" + receipt[1]):
            status, row = self.http.request(
                "GET",
                self.http.repository_path(
                    self.identity.repository, f"/issues/comments/{receipt[3]}"
                ),
                token=self.token,
            )
            if status != 200 or not isinstance(row, dict):
                raise ReviewInputError(
                    "native publication own attempt readback is unavailable"
                )
            self.ledger._part_owner(row, self.identity)
            if (
                row.get("id") != receipt[3]
                or row.get("body") != receipt[4]
                or not self.authority_fence()
            ):
                raise ReviewInputError("native publication attempt claim changed")

    def reconcile_part(
        self, ordinal: int, *, known_review_id: int | None = None
    ) -> NativeGroupEvidence:
        record = self._current_frontier()
        budget = NativeWalkBudget()
        opening = open_publication_census(
            self.http,
            token=self.token,
            repository=self.identity.repository,
            plan=self.plan,
            budget=budget,
        )
        candidates = []
        for identifier, row in opening.review_nodes.items():
            if known_review_id is not None and identifier != known_review_id:
                continue
            author, comments = row.get("a"), row.get("x")
            if (
                not isinstance(author, dict)
                or author.get("t") != "Bot"
                or _native_number(author.get("n")) != self.producer_id
                or row.get("c") != {"o": self.plan.head_sha}
                or row.get("s") != "COMMENTED"
                or not isinstance(comments, dict)
                or comments.get("totalCount") != len(self.plan.groups[ordinal])
            ):
                continue
            nodes = comments.get("nodes")
            if (
                not isinstance(nodes, list)
                or len(nodes) != 1
                or nodes[0].get("q") is not None
            ):
                continue
            candidates.append((identifier, row))
        # No body marker or ordinal from an incomplete prefix can select a
        # review. Known receipts exclude other previously confirmed parts.
        retained = (
            None
            if record.publication_staging is None
            else PublicationStaging.from_document(record.publication_staging)
        )
        confirmed_ids = (
            {int(receipt.native_id) for receipt in retained.receipts}
            if retained is not None
            else set()
        )
        candidates = [
            (identifier, row)
            for identifier, row in candidates
            if identifier not in confirmed_ids
        ]
        if len(candidates) != 1:
            raise GitHubPublicationTransientError(
                "native publication part remains unconfirmed; no replacement was dispatched"
            )
        identifier, row = candidates[0]
        evidence = read_complete_group(
            self.http,
            token=self.token,
            plan=self.plan,
            ordinal=ordinal,
            node_id=row["i"],
            expected_review_id=identifier,
            producer_id=self.producer_id,
            budget=budget,
        )
        closing = open_publication_census(
            self.http,
            token=self.token,
            repository=self.identity.repository,
            plan=self.plan,
            budget=budget,
        )
        if closing.review_nodes.get(identifier) != row:
            raise GitHubPublicationTransientError(
                "native publication part changed during reconciliation"
            )
        self._current_frontier()
        return evidence

    def publish_parts(self) -> PublicationStaging:
        return self._publish_parts(claim_manifest=False)[0]

    def _publish_parts(
        self, *, claim_manifest: bool
    ) -> tuple[PublicationStaging, bool]:
        # This callback must reserve all native/storage/caller effects before
        # the first staging CAS. It cannot add capacity during preparation.
        self.admission(self.plan)
        state = self.state()
        prepared = PublicationStaging.prepare(
            transaction_id=self.plan.transaction_id,
            result_sha256=self.plan.result_sha256,
            plan_sha256=self.plan.sha256,
            part_count=len(self.plan.groups),
            manifest_required=self.manifest_store is not None,
        )
        own_part_claim = False
        own_manifest_claim = False
        if (
            state is None
            or state.complete
            and state.transaction_id != prepared.transaction_id
        ):
            initial = prepared
            if prepared.part_count:
                initial = prepared.attempt_part(0)
                own_part_claim = True
            elif claim_manifest:
                initial = prepared.attempt_manifest()
                own_manifest_claim = True
            state = self.checkpoint(state, initial)
        if (
            state.transaction_id,
            state.result_sha256,
            state.plan_sha256,
            state.part_count,
            state.manifest_required,
        ) != (
            prepared.transaction_id,
            prepared.result_sha256,
            prepared.plan_sha256,
            prepared.part_count,
            prepared.manifest_required,
        ):
            raise ReviewInputError("native grouped publication original plan changed")
        for ordinal in range(len(state.receipts), state.part_count):
            known = None
            if state.outstanding_part is None:
                attempted = state.attempt_part(ordinal)
                state = self.checkpoint(state, attempted)
                own_part_claim = True
            if own_part_claim:
                own_part_claim = False
                self.require_claim(state)
                self._remember_dispatch(state.attempt_tokens[ordinal])
                try:
                    status, response = self.http.request(
                        "POST",
                        self.http.repository_path(
                            self.identity.repository,
                            f"/pulls/{self.identity.pull_request}/reviews",
                        ),
                        token=self.token,
                        body=self.plan.part_payload(ordinal),
                    )
                    if status in {200, 201} and isinstance(response, dict):
                        try:
                            known = _native_number(response.get("id"))
                        except ReviewInputError:
                            pass  # Malformed acknowledgements still require a fresh read.
                except GitHubHTTPTransientError:
                    # The only legal continuation is positive reconciliation of
                    # this exact original attempt. An absent read grants no POST.
                    pass
            evidence = self.reconcile_part(ordinal, known_review_id=known)
            self._settle_original_dispatch(state.attempt_tokens[ordinal])
            confirmed = state.confirm_part(
                ordinal,
                native_id=str(evidence.review_id),
                content_sha256=evidence.receipt_digest,
            )
            next_state = confirmed
            if ordinal + 1 < state.part_count:
                next_state = confirmed.attempt_part(ordinal + 1)
                own_part_claim = True
            elif claim_manifest:
                next_state = confirmed.attempt_manifest()
                own_manifest_claim = True
            state = self.checkpoint(state, next_state)
        return state, own_manifest_claim

    def publish_final(
        self,
        *,
        body_for_receipt: Callable[[NativeUnionReceipt], str],
        event: str,
        old_root_nodes: dict[int, str],
    ) -> tuple[PublicationStaging, NativePublicationEvidence]:
        """Confirm the entire selected union before acknowledging its main frame.

        An attempted final frame is never posted again. A cold invocation uses
        the same durable part receipts and exact original frame to reconcile it.
        The final native receipt does not complete caller accounting or cleanup.
        """
        if event not in {"COMMENT", "REQUEST_CHANGES"}:
            raise ReviewInputError("native final publication event is unsupported")
        store = self.manifest_store
        if (
            store is None
            or store.identity != self.identity
            or store.producer_id != self.producer_id
        ):
            raise ReviewInputError(
                "native final publication requires its original manifest store"
            )
        state, own_manifest_claim = self._publish_parts(claim_manifest=True)
        receipt = NativeUnionReceipt.from_staging(
            self.plan, state, producer_id=self.producer_id
        )
        known_manifest = None
        if not state.manifest_attempted:
            state = self.checkpoint(state, state.attempt_manifest())
            own_manifest_claim = True
        if own_manifest_claim:
            self.require_claim(state)
            assert state.manifest_token is not None
            self._remember_dispatch(state.manifest_token)
            try:
                known_manifest = store.create(receipt)
            except (GitHubHTTPTransientError, OperationWriteUnconfirmed):
                pass
        if state.manifest_receipt is not None:
            prefix, separator, manifest_identifier = (
                state.manifest_receipt.native_id.partition(":")
            )
            if prefix != "issue" or not separator:
                raise ReviewInputError(
                    "native manifest receipt changed its object domain"
                )
            known_manifest = _native_number(manifest_identifier)
        retained_manifest = store.find(receipt, known_id=known_manifest)
        self._current_frontier()
        assert state.manifest_token is not None
        self._settle_original_dispatch(state.manifest_token)
        confirmed_manifest = state.confirm_manifest(
            native_id=retained_manifest.native_id,
            content_sha256=retained_manifest.content_sha256,
        )
        # Keep the verified manifest receipt in the same CAS as the final
        # attempt claim. Both become durable before the main POST; a cold or
        # unknown-ACK invocation only reconciles that original claim.
        own_final_claim = not state.final_attempted
        if own_final_claim:
            state = self.checkpoint(state, confirmed_manifest.attempt_final())
        elif confirmed_manifest != state:
            state = self.checkpoint(state, confirmed_manifest)
        body = body_for_receipt(receipt)
        document = native_union_frame_document(body, receipt)
        if not isinstance(body, str) or not 0 < len(body.encode()) <= 65_536:
            raise ReviewInputError("native final frame exceeds its reserved bound")
        payload: dict[str, object] = {
            "body": body,
            "event": event,
            "commit_id": self.plan.head_sha,
            "comments": [],
        }
        digest = hashlib.sha256(canonical_bytes(payload)).hexdigest()
        known = None
        if own_final_claim:
            self.require_claim(state)
            assert state.final_token is not None
            self._remember_dispatch(state.final_token)
            try:
                status, response = self.http.request(
                    "POST",
                    self.http.repository_path(
                        self.identity.repository,
                        f"/pulls/{self.identity.pull_request}/reviews",
                    ),
                    token=self.token,
                    body=payload,
                )
                if status in {200, 201} and isinstance(response, dict):
                    try:
                        known = _native_number(response.get("id"))
                    except ReviewInputError:
                        pass
            except GitHubHTTPTransientError:
                pass
        if state.final_receipt is not None:
            if state.final_receipt.content_sha256 != digest:
                raise ReviewInputError("native original final frame changed")
            known = _native_number(state.final_receipt.native_id)
        budget = NativeWalkBudget()
        opening = open_publication_census(
            self.http,
            token=self.token,
            repository=self.identity.repository,
            plan=self.plan,
            budget=budget,
        )
        expected_state = "COMMENTED" if event == "COMMENT" else "CHANGES_REQUESTED"
        candidates = []
        for identifier, row in opening.review_nodes.items():
            if known is not None and identifier != known:
                continue
            author, comments = row.get("a"), row.get("x")
            if (
                identifier not in {int(item.native_id) for item in state.receipts}
                and isinstance(author, dict)
                and author.get("t") == "Bot"
                and _native_number(author.get("n")) == self.producer_id
                and row.get("c") == {"o": self.plan.head_sha}
                and row.get("s") == expected_state
                and isinstance(comments, dict)
                and comments.get("totalCount") == 0
                and comments.get("nodes") == []
            ):
                candidates.append(identifier)
        if len(candidates) != 1:
            raise GitHubPublicationTransientError(
                "native final publication remains unconfirmed; no replacement was dispatched"
            )
        # Reuse this complete opening within the same physical ten-call walk.
        evidence = read_publication_union(
            self.http,
            token=self.token,
            repository=self.identity.repository,
            plan=self.plan,
            producer_id=self.producer_id,
            group_review_ids=tuple(int(item.native_id) for item in state.receipts),
            main_review_id=candidates[0],
            main_body=body,
            main_state=expected_state,
            old_root_nodes=old_root_nodes,
            budget=budget,
            opening=opening,
        )
        receipt.require_evidence(
            evidence,
            document_sha256=hashlib.sha256(canonical_bytes(document)).hexdigest(),
        )
        self._current_frontier()
        assert state.final_token is not None
        self._settle_original_dispatch(state.final_token)
        confirmed = state.confirm_final(
            native_id=str(evidence.main_review_id), content_sha256=digest
        )
        state = self.checkpoint(state, confirmed)
        return state, evidence

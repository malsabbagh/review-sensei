"""Operation-local default inventory reader with fresh mutable publication fences."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Any

from review_sensei.bounded_evidence import EvidenceReadBudget, canonical_bytes
from review_sensei.context import finding_lifecycle_for_comment
from review_sensei.errors import ReviewInputError
from review_sensei.human_assessment import (
    PendingHumanReview,
    finding_instance_fingerprint,
)
from review_sensei.human_inventory import inventory_from_result_document
from review_sensei.models import ReviewResult
from review_sensei.presentation import (
    assign_finding_identifiers,
    build_finding_view,
    render_finding,
)
from review_sensei.session import SessionIdentity, SessionLedger

from .default_human_inventory import (
    HumanInventoryReference,
    HumanInventoryStore,
    build_human_inventory_context,
)
from .http import GitHubHttp
from .publication import (
    FindingIndex,
    approval_eligibility_from_body,
    finding_instance_from_body,
    parse_finding_index,
)
from .publication_recovery import publication_http, publication_session_ledger
from .retained_publication_source import RetainedPublishedInventorySource


def transaction_publication_digest(transaction: Any) -> str:
    """The existing publication identity, reconstructed from independent state."""
    return hashlib.sha256(
        canonical_bytes(
            {
                "domain": "reviewsensei:transaction-publication:v1",
                "transaction_id": transaction.transaction_id,
                "result_content_sha256": transaction.result_sha256,
            }
        )
    ).hexdigest()


def restore_admitted_result(
    result: ReviewResult, store: HumanInventoryStore
) -> ReviewResult:
    """Restore runtime flags only from complete independently bound admission."""
    available = store.read_available()
    if available is None:
        raise ReviewInputError(
            "complete publication admission evidence is missing; retain the original result and do not infer clean obligations"
        )
    _reference, document = available
    return restore_admitted_document(result, store, document)


def restore_admitted_document(result, store, document):
    """Pure restoration from this operation's already authenticated admission.

    This supplies no source or native authority; callers still authenticate the
    store read and the actual complete publication independently.
    """
    context = store.context
    index = parse_finding_index(
        document["finding_index"],
        repository_id=context.repository_id,
        pull_request=context.pull_request,
        head_sha=context.head_sha,
    )
    if index is None or context.original_content_digest != result.content_digest():
        raise ReviewInputError("complete publication admission content conflicts")
    entries = {entry.instance: entry for entry in index.entries}
    inactive = {
        item.fingerprint
        for item in result.finding_lifecycles
        if item.state in {"fixed", "outdated"}
    }
    active = {
        finding_instance_fingerprint(comment)
        for comment in result.comments
        if finding_lifecycle_for_comment(comment).fingerprint not in inactive
    }
    if set(entries) != active or any(
        entries[finding_instance_fingerprint(comment)].lifecycle
        != finding_lifecycle_for_comment(comment).fingerprint
        for comment in result.comments
        if finding_instance_fingerprint(comment) in entries
    ):
        raise ReviewInputError("complete publication admitted instances conflict")
    restored = replace(
        result,
        comments=tuple(
            replace(
                comment,
                effective_blocking=entries[instance].blocking
                if instance in entries
                else False,
                needs_human=entries[instance].needs_human
                if instance in entries
                else False,
            )
            for comment in result.comments
            for instance in (finding_instance_fingerprint(comment),)
        ),
    )
    expected_human = (
        inventory_from_result_document(
            document["human_inventory"],
            result_digest=context.publication_digest,
            base_sha=context.base_sha,
        )
        if document["human_inventory"] is not None
        else None
    )
    actual_human = PendingHumanReview.from_result(restored, context.base_sha)
    if (
        (expected_human is None) != (actual_human is None)
        or expected_human is not None
        and actual_human is not None
        and expected_human.inventory_document() != actual_human.inventory_document()
    ):
        raise ReviewInputError(
            "complete publication restored human admission conflicts"
        )
    return restored


class DefaultHumanInventoryReader:
    """Only immutable part bytes are cached; root/prose/authority are reread."""

    def __init__(
        self,
        *,
        http: GitHubHttp,
        ledger: SessionLedger,
        token: str,
        repository: str,
        pull_request: int,
        head_sha: str,
        app_slug: str,
        budget: EvidenceReadBudget,
        _retained_source: RetainedPublishedInventorySource | None = None,
    ) -> None:
        self.http = publication_http(http, budget)
        self.ledger = publication_session_ledger(ledger, budget)
        self.token = token
        self.repository = repository
        self.pull_request = pull_request
        self.head_sha = head_sha
        self.app_slug = app_slug
        self.budget = budget
        self._retained_source = _retained_source
        self._admitted_store: HumanInventoryStore | None = None
        self.store: HumanInventoryStore | None = None
        self.document: dict[str, object] | None = None
        self.reference: HumanInventoryReference | None = None
        self.inventory: PendingHumanReview | None = None
        self._native_root_read: tuple[bytes, Any] | None = None
        self._native_root_frontier: tuple[Any, ...] | None = None
        self._native_root_read_budget: EvidenceReadBudget | None = None

    def attach_authenticated_store(self, store: HumanInventoryStore) -> bool:
        """Accept only the original same-operation store's complete admission."""
        from .publication_recovery import _publication_transport

        if type(store) is not HumanInventoryStore:
            raise ReviewInputError("default human inventory handoff owner conflicts")
        context = store.context
        if (
            context.budget is not self.budget
            or context.retained_source is not self._retained_source
            or store.token != self.token
            or _publication_transport(store.http)
            is not _publication_transport(self.http)
            or (
                context.repository,
                context.pull_request,
                context.head_sha,
                context.app_slug.casefold(),
            )
            != (
                self.repository,
                self.pull_request,
                self.head_sha,
                self.app_slug.casefold(),
            )
        ):
            raise ReviewInputError("default human inventory handoff scope conflicts")
        if store.authenticated_reference is None:
            return False
        if self.reference is not None and (
            self.reference != store.authenticated_reference or self.store is not store
        ):
            raise ReviewInputError(
                "default human inventory handoff reference conflicts"
            )
        self._admitted_store = store
        return True

    def __call__(self, reference: HumanInventoryReference) -> PendingHumanReview:
        if self.reference is not None:
            if reference != self.reference or self.inventory is None:
                raise ReviewInputError("default human inventory reference conflicts")
            if self._retained_source is not None:
                assert self.store is not None
                self.store.revalidate_snapshot()
            return self.inventory
        admitted = self._admitted_store
        if admitted is not None:
            document = admitted.read_authenticated_snapshot(reference)
            inventory = inventory_from_result_document(
                document["human_inventory"],
                result_digest=admitted.context.publication_digest,
                base_sha=admitted.context.base_sha,
            )
            self.store, self.document = admitted, document
            self.reference, self.inventory = reference, inventory
            return inventory
        store = self._cold_store()
        context = store.context
        document = store.read(reference)
        inventory = inventory_from_result_document(
            document["human_inventory"],
            result_digest=context.publication_digest,
            base_sha=context.base_sha,
        )
        self.store, self.document = store, document
        self.reference, self.inventory = reference, inventory
        return inventory

    def attach_current_read(self, reader):
        from .api_run_requests import ApiRunSessionLedger
        from .quarantine_holds import QuarantineHoldReader

        if (
            type(reader) is QuarantineHoldReader
            and isinstance(self.ledger, ApiRunSessionLedger)
            and isinstance(reader.ledger, ApiRunSessionLedger)
            and reader.ledger.caller is self.ledger.caller
            and (reader.repository, reader.pull_request)
            == (self.repository, self.pull_request)
        ):
            self._current_read = reader

    def _cold_store(self) -> HumanInventoryStore:
        status, pr = self.http.request(
            "GET",
            self.http.repository_path(self.repository, f"/pulls/{self.pull_request}"),
            token=self.token,
        )
        if status != 200 or not isinstance(pr, dict):
            raise ReviewInputError("default human inventory current PR unavailable")
        base = pr.get("base")
        repository = base.get("repo") if isinstance(base, dict) else None
        repository_id = repository.get("id") if isinstance(repository, dict) else None
        if type(repository_id) is not int:
            raise ReviewInputError(
                "default human inventory repository identity unavailable"
            )
        identity = SessionIdentity(self.repository, self.pull_request, repository_id)
        loaded = None
        current_read = getattr(self, "_current_read", None)
        if current_read is not None:
            try:
                record = current_read.record_after_read()
            except ReviewInputError:
                pass  # A changed root must perform fresh context discovery.
            else:
                if record.identity() == identity:
                    from review_sensei.session import SessionLoadResult

                    loaded = SessionLoadResult(status="ok", record=record)
        if loaded is None:
            loaded = self.ledger.load(identity)
        transaction = (
            self._retained_source.select(loaded)
            if self._retained_source is not None
            else loaded.record.transaction
            if loaded.record is not None
            else None
        )
        if loaded.status not in {"ok", "migrated"} or transaction is None:
            raise ReviewInputError(
                "default human inventory independent checkpoint unavailable"
            )
        context = build_human_inventory_context(
            http=self.http,
            token=self.token,
            ledger=self.ledger,
            identity=identity,
            base_sha=transaction.base_sha,
            head_sha=self.head_sha,
            app_slug=self.app_slug,
            publication_digest=transaction_publication_digest(transaction),
            policy_digest=transaction.policy_digest,
            configuration_digest=transaction.configuration_digest,
            evidence_digest=transaction.evidence_digest,
            budget=self.budget,
            _loaded=(
                loaded if getattr(self.ledger, "_quarantine_required", False) else None
            ),
            _retained_source=self._retained_source,
        )
        return HumanInventoryStore(self.http, self.token, self.ledger, context)

    def native_union_reader(self, review):
        """Select the union reader only from an actual version-eight frame.

        Source/admission selection remains independent of that frame. Historical
        target projection must be supplied by the qualified recovery host; an
        unknown extra root causes the complete union read to refuse.
        """
        from .native_manifest import NativePublicationReader
        from .native_union import native_union_frame_document

        try:
            native_union_frame_document(review.get("body"))
        except ReviewInputError:
            return None

        def read(receipt):
            store = self.store or self._admitted_store or self._cold_store()
            original_budget = self.budget
            self._native_root_frontier = None
            self._native_root_read = None
            self._native_root_read_budget = None
            if store.context.budget is not original_budget:
                raise ReviewInputError("native root consumer original budget changed")
            from .publication_recovery import _publication_transport

            guard = getattr(
                _publication_transport(store.http), "_quarantine_capacity_guard", None
            )
            targets = getattr(guard, "native_old_root_nodes", None)
            evidence = NativePublicationReader.for_review(
                store, review, old_root_nodes=targets
            )(receipt)
            if (
                self.budget is not original_budget
                or store.context.budget is not original_budget
            ):
                raise ReviewInputError("native root consumer original budget changed")
            self.store = store
            self._native_root_read = (canonical_bytes(review), evidence)
            self._native_root_read_budget = original_budget
            from .caller_operation import caller_for_http

            caller = caller_for_http(store.http)
            self._native_root_frontier = (
                caller,
                None if caller is None else caller._block,
                None if caller is None else caller._root,
                None if caller is None else caller._active_operation,
                None if caller is None else len(caller.observations),
                evidence,
            )
            return evidence

        return read

    def native_thread_nodes(self):
        from .native_review_census import refresh_native_thread_nodes

        value = getattr(self, "_native_root_read", None)
        if value is None:
            return None
        if (
            self.store is None
            or self._native_root_read_budget is not self.budget
            or self.store.context.budget is not self._native_root_read_budget
        ):
            raise ReviewInputError("native root consumer original budget changed")
        self.budget.check()
        _, evidence = value
        return refresh_native_thread_nodes(
            self.store.http,
            token=self.store.token,
            repository=self.repository,
            evidence=evidence,
            app_slug=self.app_slug,
            producer_id=self.store.context.producer_id,
        )

    def confirmed_native_root(self, eligibility):
        """Expected main bytes from a complete proof, never a mutation grant."""
        import json

        value = getattr(self, "_native_root_read", None)
        if value is None or self.store is None or eligibility.native_union is None:
            return None
        if (
            self._native_root_read_budget is not self.budget
            or self.store.context.budget is not self._native_root_read_budget
        ):
            raise ReviewInputError("native root consumer original budget changed")
        self.budget.check()
        raw, evidence = value
        if (
            evidence.document_sha256
            != hashlib.sha256(canonical_bytes(eligibility.to_dict())).hexdigest()
        ):
            return None
        self.store.transaction_after_read()
        selected = json.loads(raw)
        if selected.get("id") != evidence.main_review_id:
            raise ReviewInputError("native publication confirmed root identity changed")
        return selected

    def _owned_review(self, review: object) -> bool:
        if self.store is None or not isinstance(review, dict):
            return False
        user = review.get("user")
        return bool(
            type(review.get("id")) is int
            and review["id"] > 0
            and review.get("commit_id") == self.head_sha
            and review.get("state") in {"APPROVED", "COMMENTED", "CHANGES_REQUESTED"}
            and isinstance(user, dict)
            and type(user.get("id")) is int
            and user["id"] == self.store.context.producer_id
            and user.get("type") == "Bot"
            and isinstance(user.get("login"), str)
            and user["login"].casefold() == self.app_slug.casefold()
            and isinstance(review.get("body"), str)
            and review.get("pull_request_url")
            == self.http.api_url
            + self.http.repository_path(self.repository, f"/pulls/{self.pull_request}")
        )

    def validate_metadata(self, review: object, reviews: list[Any]) -> None:
        """Fresh nonauthoritative receipt fence; this does not permit a decision."""
        self._original_review(review, reviews)

    def _original_review(
        self, review: object, reviews: list[Any]
    ) -> tuple[FindingIndex, dict[str, Any], Any]:
        # Every validation attempt consumes this one-use handoff, including
        # ownership, proof or root-fence failures and the ordinary fallback.
        frontier = self._native_root_frontier
        self._native_root_frontier = None
        if self.store is None or self.document is None or self.inventory is None:
            raise ReviewInputError("default human inventory was not authenticated")
        if not isinstance(review, dict) or not self._owned_review(review):
            raise ReviewInputError("default human inventory root ownership conflicts")
        context = self.store.context
        inventory = self.inventory
        native_reader = self.native_union_reader(review)
        cached_native = getattr(self, "_native_root_read", None)
        from .caller_operation import caller_for_http

        caller = caller_for_http(self.store.http)
        adjacent = (
            caller is not None
            and cached_native is not None
            and cached_native[0] == canonical_bytes(review)
            and self._native_root_read_budget is self.budget
            and context.budget is self._native_root_read_budget
            and frontier is not None
            and len(frontier) == 6
            and frontier[0] is caller
            and frontier[1] == caller._block
            and frontier[2] is caller._root
            and frontier[3] is caller._active_operation
            and frontier[4] == len(caller.observations)
            and frontier[5] is cached_native[1]
        )
        if adjacent:
            assert cached_native is not None
            from .native_review_census import require_native_root_evidence

            # The decoder just completed this exact source/admission/native
            # proof and its owned root/head closing. No HTTP call, root change
            # or operation switch intervened. Consume that positive frontier
            # once for this local envelope check; never restart its ten reads.
            require_native_root_evidence(
                self.store.http, token=self.store.token, evidence=cached_native[1]
            )
            self.store.transaction_after_read()

            def native_reader(_receipt):
                return cached_native[1]
        else:
            self.store.revalidate_snapshot()
        # The selected root must name this authenticated publication, not merely
        # carry a valid reference to its parts. Decode locally against the
        # already authenticated inventory; never recurse through the reader or
        # perform another remote scan while checking the envelope.
        eligibility = approval_eligibility_from_body(
            review["body"],
            human_inventory_reader=lambda _reference: inventory,
            native_union_reader=native_reader,
        )
        if (
            eligibility is None
            or eligibility.head_sha != context.head_sha
            or eligibility.result_digest != context.publication_digest
            or eligibility.human_inventory_reference != self.reference
        ):
            raise ReviewInputError("default human inventory root identity conflicts")
        expected = parse_finding_index(
            self.document["finding_index"],
            repository_id=context.repository_id,
            pull_request=context.pull_request,
            head_sha=context.head_sha,
        )
        if expected is None:
            raise ReviewInputError("default human inventory index unavailable")
        originals = []
        for item in reviews:
            if not self._owned_review(item):
                continue
            index = parse_finding_index(
                item["body"],
                repository_id=context.repository_id,
                pull_request=context.pull_request,
                head_sha=context.head_sha,
            )
            if index is not None and index.result_digest == context.publication_digest:
                if index != expected:
                    raise ReviewInputError(
                        "default human inventory published index conflicts"
                    )
                originals.append(item)
        if not originals:
            raise ReviewInputError("default human inventory original root unavailable")
        if self._retained_source is None:
            return expected, originals[0], eligibility
        original = next(
            (
                item
                for item in originals
                if item["id"] == self._retained_source.review_id
            ),
            None,
        )
        if original is None:
            raise ReviewInputError(
                "retained inventory published receipt root unavailable"
            )
        return expected, original, eligibility

    def validate_root(self, review: object, reviews: list[Any]) -> None:
        """A decision cannot replace its original complete visible explanation."""
        expected, original, eligibility = self._original_review(review, reviews)
        # _original_review just authenticated the complete original native
        # union, including all admitted inline/body explanations and their
        # physical parent IDs. A legacy main-review-only REST scan cannot
        # replace or strengthen that already completed proof.
        if eligibility.native_union is not None:
            return
        assert self.store is not None and self.inventory is not None
        context = self.store.context
        # Initial inference, decision persistence and final approval all use
        # this full fresh proof. Receipt-only pages may use metadata fences.
        inline = self.http.paginate(
            path=self.http.repository_path(
                self.repository,
                f"/pulls/{self.pull_request}/reviews/{original['id']}/comments",
            ),
            token=self.token,
            page_sizes=(100, 50, 25, 5, 1),
            max_requests=64,
            timeout_seconds=60,
        )
        entries = {entry.instance: entry for entry in expected.entries}
        identifiers = assign_finding_identifiers(entries)
        for finding in self.inventory.findings:
            entry = entries[finding.fingerprint]
            if finding.comment is None or entry.lifecycle is None:
                raise ReviewInputError(
                    "default human inventory instance metadata missing"
                )
            explanation = render_finding(
                build_finding_view(
                    finding.comment,
                    fingerprint=entry.lifecycle,
                    identifier=identifiers[finding.fingerprint],
                    placement="body" if entry.body_placed else "inline",
                )
            )
            if entry.body_placed:
                if explanation not in original["body"]:
                    raise ReviewInputError(
                        "default human inventory visible prose missing"
                    )
                continue
            matches = [
                item
                for item in inline
                if isinstance(item, dict)
                and item.get("pull_request_review_id") == original["id"]
                and isinstance(item.get("user"), dict)
                and item["user"].get("type") == "Bot"
                and type(item["user"].get("id")) is int
                and item["user"]["id"] == context.producer_id
                and item.get("commit_id") == context.head_sha
                and item.get("pull_request_url") == original["pull_request_url"]
                and finding.comment is not None
                and item.get("path") == finding.comment.path
                and (
                    item.get("line") is None or item.get("line") == finding.comment.line
                )
                and (
                    item.get("side") is None or item.get("side") == finding.comment.side
                )
                and finding_instance_from_body(item.get("body")) == finding.fingerprint
                and isinstance(item.get("body"), str)
                and explanation in item["body"]
            ]
            if len(matches) != 1:
                raise ReviewInputError(
                    "default human inventory visible prose conflicts"
                )
        if self._retained_source is not None:
            self.store.revalidate_snapshot()

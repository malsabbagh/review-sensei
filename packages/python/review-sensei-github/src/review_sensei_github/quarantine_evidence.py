"""Read-only reconstruction of complete failed and earlier published origins.

Current PR authority and historical source scope are separate. No historical
completion, admission grant, write, or new-root selection is issued here.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from review_sensei.api_work import ApiRunAccounting
from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.context import finding_lifecycle_for_comment
from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.errors import ReviewInputError
from review_sensei.human_assessment import finding_instance_fingerprint
from review_sensei.human_inventory import inventory_from_result_document
from review_sensei.models import ReviewResult
from review_sensei.quarantine_assessments import validate_inventory
from review_sensei.session import SessionLoadResult

from .api_run_history import read_previous_run
from .default_human_inventory import HumanInventoryContext, HumanInventoryStore
from .default_human_reader import transaction_publication_digest
from .operation_comments import GitHubIssueCommentOperationPort
from .publication import (
    approval_eligibility_from_body,
    parse_finding_index,
    prepare_publication_review,
)
from .publication_context_recovery import reconstruct_publication_contexts
from .publication_recovery import _event_key, _payload, _validate
from .quarantine_source import frozen_publication_source
from .session_ledger import GitHubIssueCommentSessionLedger


class _SnapshotLedger(GitHubIssueCommentSessionLedger):
    def __init__(self, native, record):
        self.__dict__.update(native.__dict__)
        self._snapshot_record = record

    def load(self, identity, **kwargs):
        if identity != self._snapshot_record.identity():
            raise ReviewInputError("quarantine historical snapshot identity changed")
        return SessionLoadResult(status="ok", record=self._snapshot_record)

    def replace(self, *args, **kwargs):
        raise ReviewInputError("quarantine historical snapshot permits reads only")

    def _request(self, method, path, *, body=None):
        if method != "GET" or body is not None:
            raise ReviewInputError("quarantine historical snapshot permits reads only")
        return super()._request(method, path, body=body)


@dataclass(frozen=True)
class CurrentReadScope:
    ledger: Any
    identity: Any
    base_sha: str
    head_sha: str
    metadata_token: str
    _read_window: bool = field(default=False, init=False, repr=False, compare=False)

    @contextmanager
    def read_only_window(self):
        """Bracket only immutable-source GETs, never an effect or authority grant."""
        if self._read_window:
            raise ReviewInputError("quarantine read-only scope cannot nest")
        self.require()
        object.__setattr__(self, "_read_window", True)
        try:
            yield
        finally:
            object.__setattr__(self, "_read_window", False)
        self.require()

    def _require_caller(self):
        ledger, identity = self.ledger, self.identity
        caller = getattr(ledger, "caller", None) or getattr(
            ledger, "_startup_caller", None
        )
        if caller is None:
            raise ReviewInputError(
                "quarantine evidence requires current metered lifecycle"
            )
        caller._require_ledger(ledger, identity)
        caller.budget_view.check()

    def require(self):
        self._require_caller()
        ledger, identity = self.ledger, self.identity
        if self._read_window:
            return
        status, pull = ledger.http.request(
            "GET",
            ledger.http.repository_path(
                identity.repository, f"/pulls/{identity.pull_request}"
            ),
            token=self.metadata_token,
        )
        if status != 200:
            raise ReviewInputError("quarantine current evidence scope changed")
        self.validate_pull(pull)

    def validate_pull(self, pull):
        """Pure additional facts on a fresh action-owned PR observation.

        This validates data; it issues no read, cache or authority. The action
        fence calls it synchronously after its own authenticated GET.
        """
        self._require_caller()
        identity = self.identity
        if (
            not isinstance(pull, dict)
            or pull.get("number") != identity.pull_request
            or pull.get("state") != "open"
            or pull.get("draft") is not False
            or not isinstance(pull.get("base"), dict)
            or pull["base"].get("sha") != self.base_sha
            or not isinstance(pull["base"].get("repo"), dict)
            or type(pull["base"]["repo"].get("id")) is not int
            or pull["base"]["repo"]["id"] != identity.repository_id
            or pull["base"]["repo"].get("full_name") != identity.repository
            or not isinstance(pull.get("head"), dict)
            or pull["head"].get("sha") != self.head_sha
            or not isinstance(pull["head"].get("repo"), dict)
            or type(pull["head"]["repo"].get("id")) is not int
            or pull["head"]["repo"]["id"] != identity.repository_id
            or pull["head"]["repo"].get("full_name") != identity.repository
            or pull["head"]["repo"].get("fork") is not False
        ):
            raise ReviewInputError("quarantine current evidence scope changed")


class _HistoricalInventoryStore(HumanInventoryStore):
    def __init__(self, *args, current_scope, **kwargs):
        super().__init__(*args, **kwargs)
        self._current_scope = current_scope

    def _authority(self):
        from .default_human_inventory import _transaction

        _transaction(self.ledger, self.context)
        self._current_scope.require()

    def revalidate_snapshot(self):
        self._authority()

    def stage(self, *args, **kwargs):
        raise ReviewInputError("quarantine historical inventory permits reads only")


def _snapshot(previous, ledger):
    if previous.identity().repository_id != ledger._expected_repository_id:
        raise ReviewInputError("quarantine historical numeric repository changed")
    snapshot = _SnapshotLedger(ledger, previous)
    if ledger._inventory_complete:
        # Reuse immutable bytes from the same complete native listing. Missing
        # carriers cannot fall back to stale cache entries or another GET.
        snapshot._reuse_listed_parts = True
        snapshot._quarantine_complete_read = True
        snapshot._verified_parts = {}
        snapshot._part_digests = {}
    return snapshot


def compact_admission_context(context, source):
    """Versioned exact-context representation, independently rebuilt on reads.

    Ordered document occurrences retain their selected digest and byte length.
    The complete discovered source inventory binds each occurrence to its path
    in the original policy tree. Selected/truncated text is recomputed, never
    admitted from these reference values or from current worktree contents.
    """
    if not isinstance(context, dict):
        raise ReviewInputError("quarantine original admission context is absent")
    document = json.loads(canonical_bytes(context))
    if document.get("schema_version") != "quarantine-original-admission-context-v1":
        raise ReviewInputError("quarantine context reconstruction contract changed")
    files = {path: (blob, text) for path, blob, text in source.files}
    for lens in document["lens_contexts"]:
        for selected in lens["documents"]:
            path, content = selected["path"], selected.pop("content")
            if (
                path not in files
                or hashlib.sha256(content.encode()).hexdigest() != selected["sha256"]
            ):
                raise ReviewInputError("quarantine selected document source conflicts")
            blob, original = files[path]
            selected.update(
                {
                    "policy_commit": source.policy_commit,
                    "git_blob": blob,
                    "source_sha256": hashlib.sha256(original.encode()).hexdigest(),
                    "source_bytes": len(original.encode()),
                    "selected_bytes": len(content.encode()),
                }
            )
    document["schema_version"] = "quarantine-original-admission-context-v2"
    document["reconstruction_contract"] = (
        "default-stages-86017489651cfc6ef7dd493bef255e1edebbe556-v1"
    )
    document["full_context_sha256"] = hashlib.sha256(
        canonical_bytes(context)
    ).hexdigest()
    return document


def reconstruct_failed_source(*, ledger, previous, current_scope, retained_result=None):
    """Recompute original admission, preserving all raw findings independently.

    The caller must already select an authenticated predecessor root. This
    reader verifies result carriers or verifies the supplied retained snapshot
    against that root and independently reconstructed immutable Git context.
    """
    current_scope.require()
    identity = previous.identity()
    old = ApiRunAccounting.from_document(previous.api_run_accounting)
    transaction = previous.transaction
    if (
        transaction is None
        or transaction.phase != "publication_failed"
        or transaction.result_sha256 is None
        or previous.dispositions
        or previous.disposition_snapshot is not None
    ):
        raise ReviewInputError("quarantine original admission context is unsupported")
    snapshot = _snapshot(previous, ledger)
    policy = ReviewConvergencePolicy(mode="merge-focused")
    with frozen_publication_source(
        Path.cwd(),
        old.configuration,
        base_sha=transaction.base_sha,
        head_sha=transaction.head_sha,
    ) as source:
        contexts = reconstruct_publication_contexts(
            source_root=source.root,
            diff=source.diff,
            ledger=snapshot,
            identity=identity,
            base_sha=transaction.base_sha,
            head_sha=transaction.head_sha,
            policy=policy,
        )
        if retained_result is None:
            producer = ledger.evidence_producer()
            if producer != f"github-bot:{ledger._evidence_author_id}":
                raise ReviewInputError(
                    "quarantine original result owner is unavailable"
                )
            port = GitHubIssueCommentOperationPort(
                ledger.http,
                token=ledger.token,
                repository=identity.repository,
                pull_request=identity.pull_request,
                app_user_id=ledger._evidence_author_id,
                strict_association=True,
            )
            if ledger._inventory_complete:
                port.prime(ledger._cached_comments())
            from .checkpoint_result_store import CheckpointResultStore

            available = CheckpointResultStore(
                port,
                app_user_id=ledger._evidence_author_id,
                http=ledger.http,
                identity=identity,
                transaction=transaction,
                app_id=getattr(ledger, "authenticated_app_id", None),
                compact_write=False,
            ).read_available(event_key=_event_key(identity, transaction))
            if available is None:
                raise ReviewInputError("quarantine original retained result is missing")
            retained_result = json.loads(available[1])
        result = ReviewResult.from_dict(retained_result)
        if _payload(result) != canonical_bytes(retained_result):
            raise ReviewInputError(
                "quarantine original retained result is not canonical"
            )
        result = _validate(
            result,
            ledger=snapshot,
            identity=identity,
            base_sha=transaction.base_sha,
            head_sha=transaction.head_sha,
            configuration_context=contexts.configuration,
            evidence_context=contexts.evidence,
            policy=policy,
        )
        admitted = prepare_publication_review(
            result=result,
            diff=source.diff,
            head_sha=transaction.head_sha,
            convergence_policy=policy,
            baseline=contexts.baseline,
            current_key=contexts.current_key,
        )
        if [finding_instance_fingerprint(c) for c in admitted.result.comments] != [
            finding_instance_fingerprint(c) for c in result.comments
        ]:
            raise ReviewInputError(
                "quarantine original admission changed raw finding inventory"
            )
        assessments = validate_inventory(
            {
                "schema_version": "quarantine-assessment-inventory-v1",
                "assessments": sorted(
                    (
                        {
                            "instance": finding_instance_fingerprint(c),
                            "concern": finding_lifecycle_for_comment(c).concern,
                        }
                        for c in admitted.result.comments
                        if c.needs_human
                    ),
                    key=lambda a: a["instance"],
                ),
            }
        )
        document = {
            "schema_version": "quarantine-failed-result-source-v1",
            "predecessor_record_sha256": previous.record_sha256,
            "retained_result": retained_result,
            "original_context_source": source.to_document(include_file_text=False),
            "original_admission_context": compact_admission_context(
                contexts.admission_context, source
            ),
        }
        from .quarantine_body_limit import reconstructed_source

        document = reconstructed_source(
            document,
            ledger=ledger,
            previous=previous,
            result=result,
            admitted=admitted,
            diff=source.diff,
            policy=policy,
        )
    current_scope.require()
    return document, assessments, result


def read_published_source(
    *, ledger, previous, current_scope, retained_source=None, complete_inventory=False
):
    """Authenticate the earlier published inventory without completing anything."""
    current_scope.require()
    identity = previous.identity()
    old = ApiRunAccounting.from_document(previous.api_run_accounting)
    reference = old.previous_record
    if reference is None:
        if retained_source is not None:
            raise ReviewInputError("quarantine earlier published source is foreign")
        return None
    if retained_source is None:
        original = read_previous_run(_snapshot(previous, ledger), identity, reference)
    else:
        from review_sensei.session import SessionRecord

        if (
            not isinstance(retained_source, dict)
            or set(retained_source)
            != {"schema_version", "record", "review_id", "review_body"}
            or retained_source["schema_version"]
            != "quarantine-published-assessment-source-v1"
        ):
            raise ReviewInputError("quarantine earlier published source is unsupported")
        original = SessionRecord.from_dict(retained_source["record"])
    state = ApiRunAccounting.from_document(original.api_run_accounting)
    if (
        original.record_sha256 != reference["root_sha256"]
        or original.identity() != identity
        or state.identity.run_id != reference["run_id"]
        or state.previous_record is not None
    ):
        raise ReviewInputError(
            "quarantine earlier published origin reference conflicts"
        )
    return _read_published_record(
        ledger=ledger,
        original=original,
        current_scope=current_scope,
        retained_source=retained_source,
        complete_inventory=complete_inventory,
    )


def _read_published_record(
    *,
    ledger,
    original,
    current_scope,
    retained_source=None,
    complete_inventory=False,
    unfinished=False,
):
    """Read an authenticated record's publication; this issues no write authority."""
    identity = original.identity()
    state = ApiRunAccounting.from_document(original.api_run_accounting)
    transaction = original.transaction
    if (
        transaction is None
        or transaction.phase != "publication_succeeded"
        or transaction.result_sha256 is None
        or (
            state.terminal_transaction is not None
            if unfinished
            else state.terminal_transaction != transaction.transaction_id
        )
        or original.dispositions
        or original.disposition_snapshot is not None
    ):
        raise ReviewInputError(
            "quarantine earlier published origin is unsupported or incomplete"
        )
    if retained_source is not None and (
        not isinstance(retained_source, dict)
        or set(retained_source)
        != {"schema_version", "record", "review_id", "review_body"}
        or retained_source["schema_version"]
        != "quarantine-published-assessment-source-v1"
        or retained_source["record"] != original.to_dict()
    ):
        raise ReviewInputError("quarantine published snapshot changed")
    publication_digest = transaction_publication_digest(transaction)

    def matches(review):
        user = review.get("user") if isinstance(review, dict) else None
        if (
            not isinstance(user, dict)
            or user.get("type") != "Bot"
            or type(user.get("id")) is not int
            or user["id"] != ledger._evidence_author_id
            or not isinstance(user.get("login"), str)
            or user["login"].casefold() != ledger.app_slug.casefold()
            or review.get("commit_id") != transaction.head_sha
            or review.get("state") not in {"COMMENTED", "CHANGES_REQUESTED", "APPROVED"}
            or type(review.get("id")) is not int
            or not 1 <= review["id"] <= 9_007_199_254_740_991
            or not isinstance(review.get("body"), str)
        ):
            return None
        index = parse_finding_index(
            review["body"],
            repository_id=identity.repository_id,
            pull_request=identity.pull_request,
            head_sha=transaction.head_sha,
        )
        return (
            index
            if index is not None
            and index.base_sha == transaction.base_sha
            and index.result_digest == publication_digest
            else None
        )

    if retained_source is None:
        reviews = ledger.http.paginate(
            path=ledger.http.repository_path(
                identity.repository, f"/pulls/{identity.pull_request}/reviews"
            ),
            token=current_scope.metadata_token,
        )
        # Same-process complete native census, before any business effect. A
        # capacity reader may reuse these exact bytes, never across transports.
        ledger._quarantine_native_reviews = (ledger.http, identity, reviews)
        found = [
            (review, index)
            for review in reviews
            if (index := matches(review)) is not None
        ]
        if len(found) != 1:
            raise ReviewInputError(
                "quarantine earlier publication is missing or duplicated"
            )
        review, index = found[0]
    else:
        if (
            type(retained_source["review_id"]) is not int
            or not 1 <= retained_source["review_id"] <= 9_007_199_254_740_991
        ):
            raise ReviewInputError("quarantine earlier publication identity is invalid")
        status, review = ledger.http.request(
            "GET",
            ledger.http.repository_path(
                identity.repository,
                f"/pulls/{identity.pull_request}/reviews/{retained_source['review_id']}",
            ),
            token=current_scope.metadata_token,
        )
        index = matches(review) if status == 200 else None
        if index is None or review.get("body") != retained_source["review_body"]:
            raise ReviewInputError("quarantine earlier publication changed")
    context = HumanInventoryContext(
        identity.repository,
        identity.repository_id,
        identity.pull_request,
        transaction.base_sha,
        transaction.head_sha,
        ledger.app_slug,
        ledger._evidence_author_id,
        transaction.transaction_id,
        transaction.result_sha256,
        publication_digest,
        transaction.policy_digest,
        transaction.configuration_digest,
        transaction.evidence_digest,
        ledger.evidence_budget,
    )
    store = _HistoricalInventoryStore(
        ledger.http,
        ledger.token,
        _snapshot(original, ledger),
        context,
        current_scope=current_scope,
    )

    def inventory_reader(reference):
        document = store.read(reference)
        return inventory_from_result_document(
            document["human_inventory"],
            result_digest=publication_digest,
            base_sha=transaction.base_sha,
        )

    eligibility = approval_eligibility_from_body(
        review["body"], human_inventory_reader=inventory_reader
    )
    if (
        eligibility is None
        or eligibility.head_sha != transaction.head_sha
        or eligibility.result_digest != publication_digest
    ):
        raise ReviewInputError(
            "quarantine earlier published assessment state is unreadable"
        )
    pending = (
        (
            eligibility.human_review.findings
            if complete_inventory
            else eligibility.human_review.pending
        )
        if eligibility.human_review
        else ()
    )
    indexed = {entry.instance: entry for entry in index.entries if entry.needs_human}
    complete_inventory = (
        {item.fingerprint for item in eligibility.human_review.findings}
        if eligibility.human_review
        else set()
    )
    if complete_inventory != set(indexed) or any(
        item.fingerprint not in indexed for item in pending
    ):
        raise ReviewInputError(
            "quarantine earlier pending inventory conflicts with publication"
        )
    assessments = validate_inventory(
        {
            "schema_version": "quarantine-assessment-inventory-v1",
            "assessments": sorted(
                (
                    {
                        "instance": item.fingerprint,
                        "concern": indexed[item.fingerprint].lifecycle,
                    }
                    for item in pending
                ),
                key=lambda a: a["instance"],
            ),
        }
    )
    current_scope.require()
    return {
        "schema_version": "quarantine-published-assessment-source-v1",
        "record": original.to_dict(),
        "review_id": review["id"],
        "review_body": review["body"],
    }, assessments


def read_published_tail_sources(
    *, ledger, previous, current_scope, retained_sources=None
):
    """Two distinct immutable published origins; no terminal flag is fabricated.

    The caller independently qualifies the closed execution/target contract.
    Complete historical membership ignores resolution receipts and thread flags.
    """
    retained_sources = retained_sources if retained_sources is not None else {}
    if set(retained_sources) - {"published-current", "published-earlier"}:
        raise ReviewInputError("published-tail sources contain a foreign origin")
    current = _read_published_record(
        ledger=ledger,
        original=previous,
        current_scope=current_scope,
        retained_source=retained_sources.get("published-current"),
        complete_inventory=True,
        unfinished=True,
    )
    earlier = read_published_source(
        ledger=ledger,
        previous=previous,
        current_scope=current_scope,
        retained_source=retained_sources.get("published-earlier"),
        complete_inventory=True,
    )
    if earlier is None:
        raise ReviewInputError("published-tail earlier publication is absent")
    return {"published-current": current, "published-earlier": earlier}

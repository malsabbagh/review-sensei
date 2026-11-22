"""Authenticated pre-external-write failure settlement and failover census.

When a review run fails either during pure pre-checkpoint validation or after
writing ``publication_pending`` (and transitioning to ``publication_failed``)
before starting any external GitHub mutation, a missing receipt or ``phase``
label alone never proves that no external effect started.

Safe rollover requires ALL of:
1. Durable positive proof (``pre-external-write-settlement-v1`` Bot comment)
   binding the exact settled root digest, generation, phase, boundary, and scope.
2. Independent Actions cessation proof (complete terminal run/job inventory).
3. Complete 3-surface negative native census (reviews, inline comments, and
   ``ReviewSensei`` check-runs on the settled head SHA), rejecting any
   unexplained native effect, tampered/foreign witness, or truncated census.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Literal
from weakref import WeakKeyDictionary

from review_sensei.api_work import ApiExecutionOwner, ApiRunAccounting
from review_sensei.bounded_evidence import EvidenceReadBudget, canonical_bytes
from review_sensei.errors import EvidenceBudgetExhausted, ReviewInputError
from review_sensei.session import SessionIdentity, SessionRecord
from review_sensei.validation import validate_repository_path

from .api_run_history import RETAINED_CONFIRMATION_TAIL, read_previous_run
from .checks import REVIEW_CHECK_EXTERNAL_ID, REVIEW_CHECK_NAME
from .errors import (
    GitHubHTTPError,
    GitHubPublicationError,
    GitHubPublicationTransientError,
)
from .http import MAX_PAGINATION_PAGES
from .pre_entry_abort_recovery import (
    classify_abort_inline_effect,
    classify_abort_review_effect,
)
from .session_ledger import GitHubIssueCommentSessionLedger

if TYPE_CHECKING:
    from .api_run_requests import ApiRunRequestTransport

SETTLEMENT_WITNESS_SCHEMA = "pre-external-write-settlement-v1"
SETTLEMENT_WITNESS_MARKER = "<!-- reviewsensei:pre-external-write-settlement:v1 -->"
SETTLEMENT_BOUNDARIES = frozenset(
    {"pre_checkpoint_validation", "post_checkpoint_pre_write"}
)

_PAGE_SIZE = 100
_CENSUS_WALL_SECONDS = 30
_HEX40 = re.compile(r"^[a-f0-9]{40}\Z")
_HEX64 = re.compile(r"^[a-f0-9]{64}\Z")
_PRODUCER_RE = re.compile(r"^github-bot:([1-9][0-9]*)\Z")
_JSON_FENCE_RE = re.compile(
    r"```json\n([^\n]+)\n```\n" + re.escape(SETTLEMENT_WITNESS_MARKER) + r"\s*\Z"
)
_REVIEW_STATES = {
    "COMMENT": "COMMENTED",
    "APPROVE": "APPROVED",
    "REQUEST_CHANGES": "CHANGES_REQUESTED",
}
_WITNESS_FIELDS = frozenset(
    {
        "schema_version",
        "repository",
        "repository_id",
        "pull_request",
        "run_id",
        "owner",
        "settled_root_sha256",
        "settled_generation",
        "settled_phase",
        "boundary",
        "transaction_id",
        "base_sha",
        "head_sha",
        "producer",
        "content_sha256",
    }
)


def _hold(reason: str, *, record: SessionRecord | None = None) -> ReviewInputError:
    phase = (
        record.transaction.phase
        if record is not None and record.transaction is not None
        else "no-transaction"
    )
    return ReviewInputError(
        f"previous caller run is not terminal (phase={phase}): " + reason
    )


def _positive_id(value: object) -> bool:
    return type(value) is int and 0 < value <= 9_223_372_036_854_775_807


def _protect_tail(
    budget: EvidenceReadBudget, reads: int, *, record: SessionRecord | None = None
) -> None:
    try:
        budget.preflight(RETAINED_CONFIRMATION_TAIL + reads)
    except EvidenceBudgetExhausted:
        raise _hold(
            "native effect census is unfunded in caller startup", record=record
        ) from None


def _producer_id(producer: str) -> int:
    match = _PRODUCER_RE.fullmatch(producer) if isinstance(producer, str) else None
    if match is None:
        raise ReviewInputError("settlement producer identity is invalid")
    return int(match.group(1))


@dataclass(frozen=True)
class PreExternalWriteSettlementWitness:
    """Data-only witness binding one pre-external-write failure to its settled root."""

    schema_version: str
    repository: str
    repository_id: int
    pull_request: int
    run_id: int
    owner: dict[str, object]
    settled_root_sha256: str
    settled_generation: int
    settled_phase: str
    boundary: str
    transaction_id: str | None
    base_sha: str
    head_sha: str
    producer: str
    content_sha256: str
    authority: Literal[False] = field(init=False, default=False)

    def __post_init__(self) -> None:
        if self.schema_version != SETTLEMENT_WITNESS_SCHEMA:
            raise ReviewInputError("settlement witness schema_version is invalid")
        if not isinstance(self.repository, str) or self.repository.count("/") != 1:
            raise ReviewInputError("settlement witness repository is invalid")
        validate_repository_path(self.repository, label="repository")
        if not _positive_id(self.repository_id) or not _positive_id(self.pull_request):
            raise ReviewInputError("settlement witness pull_request scope is invalid")
        if not _positive_id(self.run_id):
            raise ReviewInputError("settlement witness run_id is invalid")
        if not isinstance(self.owner, dict) or set(self.owner) != {
            "run_attempt",
            "job",
            "generation",
        }:
            raise ReviewInputError("settlement witness owner shape is invalid")
        run_attempt = self.owner["run_attempt"]
        job = self.owner["job"]
        generation = self.owner["generation"]
        if (
            type(run_attempt) is not int
            or type(job) is not str
            or type(generation) is not int
        ):
            raise ReviewInputError("settlement witness owner values are invalid")
        ApiExecutionOwner(run_attempt, job, generation)
        if not isinstance(self.settled_root_sha256, str) or not _HEX64.fullmatch(
            self.settled_root_sha256
        ):
            raise ReviewInputError("settlement witness settled_root_sha256 is invalid")
        if type(self.settled_generation) is not int or self.settled_generation < 1:
            raise ReviewInputError("settlement witness settled_generation is invalid")
        if self.boundary not in SETTLEMENT_BOUNDARIES:
            raise ReviewInputError("settlement witness boundary is invalid")
        if self.boundary == "pre_checkpoint_validation":
            if (
                self.settled_phase != "no-transaction"
                or self.transaction_id is not None
            ):
                raise ReviewInputError(
                    "pre-checkpoint settlement witness cannot carry a transaction"
                )
        else:
            if (
                self.settled_phase != "publication_failed"
                or not isinstance(self.transaction_id, str)
                or not _HEX64.fullmatch(self.transaction_id)
            ):
                raise ReviewInputError(
                    "post-checkpoint settlement witness requires publication_failed transaction_id"
                )
        if not isinstance(self.base_sha, str) or not _HEX40.fullmatch(self.base_sha):
            raise ReviewInputError("settlement witness base_sha is invalid")
        if not isinstance(self.head_sha, str) or not _HEX40.fullmatch(self.head_sha):
            raise ReviewInputError("settlement witness head_sha is invalid")
        _producer_id(self.producer)
        if not isinstance(self.content_sha256, str) or not _HEX64.fullmatch(
            self.content_sha256
        ):
            raise ReviewInputError("settlement witness content_sha256 is invalid")
        expected_digest = hashlib.sha256(
            canonical_bytes(self._unsigned_dict())
        ).hexdigest()
        if self.content_sha256 != expected_digest:
            raise ReviewInputError("settlement witness digest mismatch")
        object.__setattr__(
            self,
            "owner",
            {
                "run_attempt": run_attempt,
                "job": job,
                "generation": generation,
            },
        )

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "repository": self.repository,
            "repository_id": self.repository_id,
            "pull_request": self.pull_request,
            "run_id": self.run_id,
            "owner": {
                "run_attempt": self.owner["run_attempt"],
                "job": self.owner["job"],
                "generation": self.owner["generation"],
            },
            "settled_root_sha256": self.settled_root_sha256,
            "settled_generation": self.settled_generation,
            "settled_phase": self.settled_phase,
            "boundary": self.boundary,
            "transaction_id": self.transaction_id,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "producer": self.producer,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            **self._unsigned_dict(),
            "content_sha256": self.content_sha256,
        }

    @classmethod
    def create(
        cls,
        *,
        repository: str,
        repository_id: int,
        pull_request: int,
        run_id: int,
        owner: Mapping[str, object],
        settled_root_sha256: str,
        settled_generation: int,
        settled_phase: str,
        boundary: str,
        transaction_id: str | None,
        base_sha: str,
        head_sha: str,
        producer: str,
    ) -> PreExternalWriteSettlementWitness:
        unsigned = {
            "schema_version": SETTLEMENT_WITNESS_SCHEMA,
            "repository": repository,
            "repository_id": repository_id,
            "pull_request": pull_request,
            "run_id": run_id,
            "owner": dict(owner),
            "settled_root_sha256": settled_root_sha256,
            "settled_generation": settled_generation,
            "settled_phase": settled_phase,
            "boundary": boundary,
            "transaction_id": transaction_id,
            "base_sha": base_sha,
            "head_sha": head_sha,
            "producer": producer,
        }
        digest = hashlib.sha256(canonical_bytes(unsigned)).hexdigest()
        return cls(
            schema_version=SETTLEMENT_WITNESS_SCHEMA,
            repository=repository,
            repository_id=repository_id,
            pull_request=pull_request,
            run_id=run_id,
            owner=dict(owner),
            settled_root_sha256=settled_root_sha256,
            settled_generation=settled_generation,
            settled_phase=settled_phase,
            boundary=boundary,
            transaction_id=transaction_id,
            base_sha=base_sha,
            head_sha=head_sha,
            producer=producer,
            content_sha256=digest,
        )


def render_settlement_witness_comment(
    witness: PreExternalWriteSettlementWitness,
) -> str:
    """Render an immutable authenticated settlement witness comment body."""
    if not isinstance(witness, PreExternalWriteSettlementWitness):
        raise ReviewInputError("settlement witness instance is invalid")
    encoded = json.dumps(
        witness.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return (
        "ReviewSensei recorded a pre-external-write failure settlement witness "
        "(data only; requires independent Actions cessation and native negative census).\n\n"
        f"```json\n{encoded}\n```\n"
        f"{SETTLEMENT_WITNESS_MARKER}"
    )


def parse_settlement_witness_comment(
    body: object,
) -> PreExternalWriteSettlementWitness | None:
    """Strictly parse a marked settlement witness comment, failing closed on corruption."""
    if not isinstance(body, str) or SETTLEMENT_WITNESS_MARKER not in body:
        return None
    if body.count(SETTLEMENT_WITNESS_MARKER) != 1:
        raise ReviewInputError("settlement witness marker is duplicated")
    match = _JSON_FENCE_RE.search(body)
    if match is None:
        raise ReviewInputError("settlement witness comment body is malformed")
    try:
        parsed = json.loads(match.group(1))
    except (ValueError, TypeError) as exc:
        raise ReviewInputError("settlement witness JSON is malformed") from exc
    if not isinstance(parsed, dict) or set(parsed) != _WITNESS_FIELDS:
        raise ReviewInputError("settlement witness JSON keys are invalid")
    witness = PreExternalWriteSettlementWitness(
        schema_version=parsed["schema_version"],
        repository=parsed["repository"],
        repository_id=parsed["repository_id"],
        pull_request=parsed["pull_request"],
        run_id=parsed["run_id"],
        owner=parsed["owner"],
        settled_root_sha256=parsed["settled_root_sha256"],
        settled_generation=parsed["settled_generation"],
        settled_phase=parsed["settled_phase"],
        boundary=parsed["boundary"],
        transaction_id=parsed["transaction_id"],
        base_sha=parsed["base_sha"],
        head_sha=parsed["head_sha"],
        producer=parsed["producer"],
        content_sha256=parsed["content_sha256"],
    )
    if body != render_settlement_witness_comment(witness):
        raise ReviewInputError(
            "settlement witness body does not match canonical rendering"
        )
    return witness


def emit_pre_external_write_settlement(
    *,
    ledger: GitHubIssueCommentSessionLedger,
    caller: ApiRunRequestTransport,
    identity: SessionIdentity,
    settled_record: SessionRecord,
    boundary: str,
    base_sha: str,
    head_sha: str,
    transaction_id: str | None = None,
) -> PreExternalWriteSettlementWitness:
    """Publish one authenticated witness proving no external GitHub write began."""
    from .api_run_requests import ApiRunRequestTransport

    if not isinstance(caller, ApiRunRequestTransport) or not isinstance(
        ledger, GitHubIssueCommentSessionLedger
    ):
        raise ReviewInputError("settlement witness emitter requires caller transport")
    if boundary not in SETTLEMENT_BOUNDARIES:
        raise ReviewInputError("settlement boundary is unsupported")
    if caller._external_business_write_attempted:
        raise ReviewInputError(
            "cannot emit pre-write settlement after external business write attempt"
        )
    if caller._provider_effect_unresolved:
        raise ReviewInputError(
            "cannot emit pre-write settlement when provider effect is unresolved"
        )
    if caller._checkpoint is not None:
        raise ReviewInputError(
            "cannot emit pre-write settlement with an unconfirmed checkpoint"
        )
    unresolved = any(
        o.get("mutation")
        and (o["outcome"] in {"unknown", "expired"} or o.get("uncertain_effect"))
        and str(o["request_id"]) not in caller._reconciled_mutations
        for o in caller.observations
    )
    if unresolved:
        raise ReviewInputError(
            "cannot emit pre-write settlement with unresolved transport mutations"
        )
    if (
        settled_record.identity() != identity
        or identity.repository_id is None
        or settled_record.record_sha256 != caller._root
        or settled_record.reservation_id is not None
        or settled_record.reserved_slot is not None
        or settled_record.publication_staging is not None
    ):
        raise ReviewInputError(
            "settled session record does not match active caller root"
        )
    if boundary == "pre_checkpoint_validation":
        if settled_record.transaction is not None or transaction_id is not None:
            raise ReviewInputError(
                "pre-checkpoint settlement requires no active durable transaction"
            )
        settled_phase = "no-transaction"
    else:
        tx = settled_record.transaction
        if (
            tx is None
            or tx.phase != "publication_failed"
            or transaction_id != tx.transaction_id
            or base_sha != tx.base_sha
            or head_sha != tx.head_sha
        ):
            raise ReviewInputError(
                "post-checkpoint settlement requires a publication_failed transaction"
            )
        if (
            settled_record.finding_lifecycle is not None
            and settled_record.finding_lifecycle.get("operation_id")
            == tx.transaction_id
        ):
            raise ReviewInputError(
                "cannot emit pre-write settlement when finding lifecycle recorded transaction effects"
            )
        settled_phase = "publication_failed"
    producer = ledger.evidence_producer()
    producer_id = _producer_id(producer)
    app_slug = ledger.app_slug
    if not isinstance(app_slug, str) or not app_slug.strip():
        raise ReviewInputError("settlement witness requires authenticated app slug")
    witness = PreExternalWriteSettlementWitness.create(
        repository=identity.repository,
        repository_id=identity.repository_id,
        pull_request=identity.pull_request,
        run_id=caller.run.run_id,
        owner=asdict(caller.accounting.owner),
        settled_root_sha256=settled_record.record_sha256,
        settled_generation=settled_record.generation,
        settled_phase=settled_phase,
        boundary=boundary,
        transaction_id=transaction_id,
        base_sha=base_sha,
        head_sha=head_sha,
        producer=producer,
    )
    rendered = render_settlement_witness_comment(witness)
    path = ledger.http.repository_path(
        identity.repository, f"/issues/{identity.pull_request}/comments"
    )
    with caller.control_witness_write():
        status, payload = ledger.http.request(
            "POST",
            path,
            token=ledger.token,
            body={"body": rendered},
        )
    user = payload.get("user") if isinstance(payload, dict) else None
    if (
        status != 201
        or not isinstance(payload, dict)
        or payload.get("body") != rendered
        or not _positive_id(payload.get("id"))
        or not isinstance(user, dict)
        or user.get("type") != "Bot"
        or user.get("id") != producer_id
        or not isinstance(user.get("login"), str)
        or user["login"].casefold() != app_slug.casefold()
    ):
        raise GitHubPublicationTransientError(
            "pre-external-write settlement witness POST was not confirmed"
        )
    return witness


def _is_authenticated_bot_comment(
    row: Mapping[str, Any], *, producer_id: int, app_slug: str
) -> bool:
    user = row.get("user")
    return (
        isinstance(user, dict)
        and user.get("type") == "Bot"
        and type(user.get("id")) is int
        and user["id"] == producer_id
        and isinstance(user.get("login"), str)
        and user["login"].casefold() == app_slug.casefold()
    )


def find_authenticated_settlement_witness(
    ledger: GitHubIssueCommentSessionLedger,
    identity: SessionIdentity,
    record: SessionRecord,
    old: ApiRunAccounting,
) -> PreExternalWriteSettlementWitness:
    """Find and strictly authenticate the unique settlement witness for ``record``."""
    budget, app_slug = ledger.evidence_budget, ledger.app_slug
    if budget is None or not app_slug or not ledger._mutation_active:
        raise _hold("authenticated witness reader is unavailable", record=record)
    producer = ledger.evidence_producer()
    try:
        producer_id = _producer_id(producer)
    except ReviewInputError as exc:
        raise _hold(str(exc), record=record) from exc
    http = ledger.http
    deadline = http.evidence_monotonic() + _CENSUS_WALL_SECONDS
    path = http.repository_path(
        identity.repository, f"/issues/{identity.pull_request}/comments"
    )
    seen_ids: set[int] = set()
    matches: list[PreExternalWriteSettlementWitness] = []
    for page in range(1, MAX_PAGINATION_PAGES + 1):
        _protect_tail(budget, 1, record=record)
        remaining = deadline - http.evidence_monotonic()
        if remaining <= 0:
            raise _hold("settlement witness scan reached its deadline", record=record)
        try:
            status, rows = http.request(
                "GET",
                f"{path}?per_page={_PAGE_SIZE}&page={page}",
                token=ledger.token,
                timeout_seconds=remaining,
            )
        except GitHubHTTPError:
            raise _hold(
                "settlement witness scan is unavailable", record=record
            ) from None
        if status != 200 or not isinstance(rows, list) or len(rows) > _PAGE_SIZE:
            raise _hold("settlement witness scan is incomplete", record=record)
        if http.evidence_monotonic() >= deadline:
            raise _hold("settlement witness scan reached its deadline", record=record)
        for row in rows:
            if (
                not isinstance(row, dict)
                or not _positive_id(row.get("id"))
                or row["id"] in seen_ids
            ):
                raise _hold(
                    "settlement witness scan has an unreadable row", record=record
                )
            seen_ids.add(row["id"])
            if not _is_authenticated_bot_comment(
                row, producer_id=producer_id, app_slug=app_slug
            ):
                # Ignore non-Bot or foreign comments so they can neither forge nor DoS.
                continue
            body = row.get("body")
            if isinstance(body, str) and SETTLEMENT_WITNESS_MARKER in body:
                try:
                    parsed = parse_settlement_witness_comment(body)
                except ReviewInputError as exc:
                    raise _hold(
                        "tampered or corrupt bot settlement witness: " + str(exc),
                        record=record,
                    ) from exc
                if (
                    parsed is not None
                    and parsed.settled_root_sha256 == record.record_sha256
                ):
                    matches.append(parsed)
        if len(rows) < _PAGE_SIZE:
            if http.evidence_monotonic() >= deadline:
                raise _hold(
                    "settlement witness scan reached its deadline", record=record
                )
            break
    else:
        raise _hold(
            "settlement witness scan reached its pagination bound", record=record
        )

    if not matches:
        if record.transaction is None:
            raise ReviewInputError(
                "previous caller run is not terminal: "
                f"run={old.identity.run_id} root={record.record_sha256} "
                "phase=no-transaction; "
                "missing pre-external-write settlement witness"
            )
        raise _hold("missing pre-external-write settlement witness", record=record)
    if len(matches) > 1:
        raise _hold("ambiguous pre-external-write settlement witnesses", record=record)
    witness = matches[0]
    expected_phase = (
        record.transaction.phase if record.transaction is not None else "no-transaction"
    )
    expected_boundary = (
        "post_checkpoint_pre_write"
        if record.transaction is not None
        else "pre_checkpoint_validation"
    )
    expected_tx_id = (
        record.transaction.transaction_id if record.transaction is not None else None
    )
    if (
        witness.repository != identity.repository
        or witness.repository_id != identity.repository_id
        or witness.pull_request != identity.pull_request
        or witness.run_id != old.identity.run_id
        or witness.owner != asdict(old.owner)
        or witness.settled_root_sha256 != record.record_sha256
        or witness.settled_generation != record.generation
        or witness.settled_phase != expected_phase
        or witness.boundary != expected_boundary
        or witness.transaction_id != expected_tx_id
        or witness.producer != producer
    ):
        raise _hold(
            "settlement witness binding conflicts with settled predecessor",
            record=record,
        )
    if record.transaction is not None and (
        witness.base_sha != record.transaction.base_sha
        or witness.head_sha != record.transaction.head_sha
    ):
        raise _hold(
            "settlement witness commit scope conflicts with settled transaction",
            record=record,
        )
    elif (
        record.transaction is None
        and witness.head_sha != record.failed_attempts_head_sha
    ):
        raise _hold(
            "settlement witness commit scope conflicts with settled failed attempt",
            record=record,
        )
    return witness


@dataclass(frozen=True)
class _EarlierSettledEffects:
    reviews: frozenset[tuple[int, str, str, int]]
    inline: frozenset[tuple[int, int, str, str]]
    check_runs: frozenset[int]

    def explains(self, row: dict[str, Any], *, inline: bool) -> bool:
        try:
            raw = row["body"].encode("utf-8")
        except UnicodeEncodeError:
            return False
        digest = hashlib.sha256(raw).hexdigest()
        if inline:
            parent = row.get("pull_request_review_id")
            head = row.get("commit_id")
            return (
                _positive_id(parent)
                and isinstance(head, str)
                and head == row.get("original_commit_id")
                and row.get("in_reply_to_id") is None
                and (row["id"], parent, head, digest) in self.inline
            )
        state = row.get("state")
        return (
            isinstance(state, str)
            and (row["id"], state, digest, len(raw)) in self.reviews
        )

    def explains_check_run(self, row: dict[str, Any]) -> bool:
        cid = row.get("id")
        return _positive_id(cid) and cid in self.check_runs


_NO_EARLIER_EFFECTS = _EarlierSettledEffects(frozenset(), frozenset(), frozenset())
_ADMITTED_PRIOR_RECORDS: WeakKeyDictionary[Any, tuple[str, SessionRecord | None]] = (
    WeakKeyDictionary()
)
_ADMITTED_PRIOR_CHAINS: WeakKeyDictionary[
    Any, tuple[str, tuple[SessionRecord, ...]]
] = WeakKeyDictionary()
_MAX_PRIOR_ANCESTOR_HOPS = 8


def consume_admitted_prior_record(
    ledger: Any, before_record: SessionRecord
) -> SessionRecord | None:
    """Return and clear the prior record authenticated for this exact rollover root."""
    entry = _ADMITTED_PRIOR_RECORDS.pop(ledger, None)
    if entry is None:
        return None
    root_sha256, prior_record = entry
    if root_sha256 != before_record.record_sha256:
        return None
    return prior_record


def peek_admitted_prior_chain(
    ledger: Any, before_record: SessionRecord
) -> tuple[SessionRecord, ...]:
    """Return the authenticated skipped-failed ancestor chain without clearing it."""
    entry = _ADMITTED_PRIOR_CHAINS.get(ledger)
    if entry is None:
        return ()
    root_sha256, chain = entry
    if root_sha256 != before_record.record_sha256:
        return ()
    return chain


def consume_admitted_prior_chain(
    ledger: Any, before_record: SessionRecord
) -> tuple[SessionRecord, ...]:
    """Return and clear the skipped-failed ancestor chain for this rollover root."""
    entry = _ADMITTED_PRIOR_CHAINS.pop(ledger, None)
    if entry is None:
        return ()
    root_sha256, chain = entry
    if root_sha256 != before_record.record_sha256:
        return ()
    return chain


def clear_admitted_prior_record(ledger: Any) -> None:
    """Drop any unclaimed admitted prior record and chain on cleanup."""
    _ADMITTED_PRIOR_RECORDS.pop(ledger, None)
    _ADMITTED_PRIOR_CHAINS.pop(ledger, None)


def _archive_part_count(archive_ref: dict[str, Any]) -> int:
    manifest = archive_ref.get("manifest")
    parts = (
        manifest.get("parts", ())
        if isinstance(manifest, dict) and isinstance(manifest.get("parts"), list)
        else ()
    )
    return max(1, len(parts))


def _same_lifecycle_envelope(
    prior_ref: Mapping[str, object] | None, current_ref: Mapping[str, object]
) -> bool:
    if prior_ref == current_ref:
        return True
    if not isinstance(prior_ref, Mapping):
        return False
    left, right = dict(prior_ref), dict(current_ref)
    for item in (left, right):
        item.pop("schema_version", None)
        item.pop("accepted_semantic_history", None)
    return left == right


def _earlier_settled_effects(
    ledger: GitHubIssueCommentSessionLedger,
    identity: SessionIdentity,
    current: SessionRecord,
    old: ApiRunAccounting,
    head_sha: str,
    producer_id: int,
    budget: EvidenceReadBudget,
    *,
    prior_record: SessionRecord | None = None,
    prior_archive_ref: dict[str, Any] | None = None,
) -> _EarlierSettledEffects:
    """Explain only exact visible bytes/IDs proved by the settled ancestor run."""
    from review_sensei.bounded_evidence import read_inline_evidence
    from review_sensei.finding_lifecycle_store import _decode_selected_lifecycle

    from .finding_lifecycle import _unpack_journal, _validate
    from .finding_lifecycle_dictionary import (
        _manifest as _dict_manifest,
    )
    from .finding_lifecycle_dictionary import (
        is_dictionary,
    )
    from .finding_lifecycle_dictionary import (
        unpack as _unpack_dictionary,
    )

    archive, reference = old.previous_record, current.finding_lifecycle
    if archive is None or reference is None:
        return _NO_EARLIER_EFFECTS
    if (
        current.transaction is not None
        and reference.get("operation_id") == current.transaction.transaction_id
    ):
        return _NO_EARLIER_EFFECTS
    manifest = reference.get("manifest")
    binding = manifest.get("binding") if isinstance(manifest, dict) else None
    if (
        not isinstance(manifest, dict)
        or not isinstance(binding, dict)
        or binding.get("head_sha") != head_sha
    ):
        return _NO_EARLIER_EFFECTS
    expected_archive = prior_archive_ref if prior_archive_ref is not None else archive
    need_prior_read = prior_record is None
    _protect_tail(
        budget,
        int(need_prior_read) + len(manifest.get("parts", ())),
        record=current,
    )
    try:
        candidate = (
            prior_record
            if prior_record is not None
            else read_previous_run(ledger, identity, expected_archive)
        )
        candidate_caller = ApiRunAccounting.from_document(candidate.api_run_accounting)
        if (
            candidate.identity() != identity
            or candidate.record_sha256 != expected_archive.get("root_sha256")
            or candidate_caller.identity.run_id != expected_archive.get("run_id")
            or candidate_caller.identity.repository_id != identity.repository_id
            or candidate.generation >= current.generation
        ):
            return _NO_EARLIER_EFFECTS
        prior: SessionRecord | None = None
        prior_caller: ApiRunAccounting | None = None
        for _ in range(_MAX_PRIOR_ANCESTOR_HOPS + 1):
            tx_cand = candidate.transaction
            if (
                tx_cand is not None
                and tx_cand.phase == "publication_succeeded"
                and tx_cand.transaction_id == reference.get("operation_id")
            ):
                prior = candidate
                prior_caller = candidate_caller
                break
            next_ref = candidate_caller.previous_record
            if not isinstance(next_ref, dict):
                return _NO_EARLIER_EFFECTS
            _protect_tail(budget, _archive_part_count(next_ref), record=current)
            next_cand = read_previous_run(ledger, identity, next_ref)
            next_caller = ApiRunAccounting.from_document(next_cand.api_run_accounting)
            if (
                next_cand.identity() != identity
                or next_cand.record_sha256 != next_ref.get("root_sha256")
                or next_caller.identity.run_id != next_ref.get("run_id")
                or next_caller.identity.repository_id != identity.repository_id
                or next_cand.generation >= candidate.generation
            ):
                return _NO_EARLIER_EFFECTS
            candidate, candidate_caller = next_cand, next_caller
        if prior is None or prior_caller is None:
            return _NO_EARLIER_EFFECTS
        tx = prior.transaction
        assert tx is not None
        terminal_review = (
            prior_caller.action is None
            and prior_caller.terminal_transaction == tx.transaction_id
        )
        terminal_command = (
            prior_caller.action is not None
            and prior_caller.action.get("phase") == "terminal"
            and prior_caller.terminal_transaction is None
        )
        if (
            tx.phase != "publication_succeeded"
            or (not terminal_review and not terminal_command)
            or prior.reservation_id is not None
            or not _same_lifecycle_envelope(prior.finding_lifecycle, reference)
            or not tx.generation <= binding["generation"] <= prior.generation
            or prior.generation >= current.generation
            or reference["operation_id"] != tx.transaction_id
            or (
                binding["base_sha"],
                binding["head_sha"],
                binding["policy_digest"],
                binding["configuration_digest"],
            )
            != (tx.base_sha, tx.head_sha, tx.policy_digest, tx.configuration_digest)
        ):
            return _NO_EARLIER_EFFECTS
        stored = _decode_selected_lifecycle(ledger, current)
        if stored is None:
            return _NO_EARLIER_EFFECTS
        if is_dictionary(stored):
            dict_manifest = stored.get("dictionary_manifest")
            base_binding = _dict_manifest(dict_manifest, binding)
            if not isinstance(dict_manifest, dict):
                raise ReviewInputError(
                    "finding lifecycle dictionary manifest is invalid"
                )
            base_parts = (
                0
                if dict_manifest.get("encoding") == "inline-json-v1"
                else len(dict_manifest.get("parts", ()))
                if isinstance(dict_manifest.get("parts"), list)
                else 0
            )
            _protect_tail(budget, base_parts, record=current)
            base = (
                read_inline_evidence(dict_manifest, expected_binding=base_binding)
                if dict_manifest.get("encoding") == "inline-json-v1"
                else ledger.read_evidence(
                    identity, dict_manifest, expected_binding=base_binding
                )
            )
            journal = _unpack_dictionary(stored, base=base, current_binding=binding)
        else:
            journal = _validate(_unpack_journal(stored))
    except (GitHubHTTPError, GitHubPublicationError, ReviewInputError):
        raise _hold(
            "earlier native effect evidence is unavailable", record=current
        ) from None
    if journal["unknown_trust_effect"]:
        return _NO_EARLIER_EFFECTS

    reviews: dict[int, tuple[int, str, str, int]] = {}
    reviews_path = ledger.http.repository_path(
        identity.repository, f"/pulls/{identity.pull_request}/reviews"
    )
    check_post_path = ledger.http.repository_path(identity.repository, "/check-runs")
    check_runs: set[int] = set()
    app_slug = (ledger.app_slug or "").removesuffix("[bot]").casefold()

    for effect in journal["effects"]:
        if effect["status"] != "confirmed":
            continue
        method = effect["method"]
        eff_path = effect["path"]
        response_pair = effect.get("response")
        status, response = (
            response_pair
            if isinstance(response_pair, (list, tuple)) and len(response_pair) == 2
            else (None, None)
        )
        request = effect["request"]
        if method == "POST" and eff_path == reviews_path:
            event = request.get("event")
            body = request.get("body")
            if (
                status in {200, 201}
                and isinstance(response, dict)
                and _positive_id(response.get("id"))
                and response.get("producer_id") == producer_id
                and request.get("commit_id") == head_sha
                and isinstance(event, str)
                and event in _REVIEW_STATES
                and isinstance(body, dict)
                and set(body) == {"$body_sha256", "utf8_bytes"}
            ):
                rid = response["id"]
                if rid in reviews:
                    return _NO_EARLIER_EFFECTS
                reviews[rid] = (
                    rid,
                    _REVIEW_STATES[event],
                    body["$body_sha256"],
                    body["utf8_bytes"],
                )
        elif method == "PUT" and eff_path.startswith(reviews_path + "/"):
            suffix = eff_path.removeprefix(reviews_path + "/")
            body = request.get("body")
            if (
                suffix.isdigit()
                and int(suffix) in reviews
                and status in {200, 201}
                and isinstance(response, dict)
                and response.get("id") == int(suffix)
                and response.get("producer_id") == producer_id
                and isinstance(body, dict)
                and set(body) == {"$body_sha256", "utf8_bytes"}
            ):
                rid = int(suffix)
                old_row = reviews[rid]
                reviews[rid] = (
                    rid,
                    old_row[1],
                    body["$body_sha256"],
                    body["utf8_bytes"],
                )
        elif method == "POST" and eff_path == check_post_path:
            if (
                status in {200, 201}
                and isinstance(response, dict)
                and _positive_id(response.get("id"))
                and request.get("head_sha") == head_sha
                and request.get("name") == REVIEW_CHECK_NAME
            ):
                check_runs.add(response["id"])
        elif method == "PATCH" and eff_path.startswith(check_post_path + "/"):
            precond = effect.get("check_update_precondition")
            if (
                isinstance(precond, dict)
                and precond.get("head_sha") == head_sha
                and _positive_id(precond.get("check_run_id"))
                and (
                    not app_slug
                    or str(precond.get("app_slug", "")).casefold() == app_slug
                )
                and status in {200, 201}
                and isinstance(response, dict)
                and response.get("id") == precond["check_run_id"]
            ):
                check_runs.add(precond["check_run_id"])

    inline: dict[int, tuple[int, int, str, str]] = {}
    for r in journal["native_receipts"]:
        if (
            _positive_id(r["comment_id"])
            and r["review_id"] in reviews
            and r["producer_id"] == producer_id
            and (r["repository_id"], r["pull_request"], r["head_sha"], r["base_sha"])
            == (identity.repository_id, identity.pull_request, tx.head_sha, tx.base_sha)
        ):
            cid = r["comment_id"]
            if cid in inline:
                return _NO_EARLIER_EFFECTS
            inline[cid] = (cid, r["review_id"], r["native_head_sha"], r["body_sha256"])

    return _EarlierSettledEffects(
        frozenset(reviews.values()),
        frozenset(inline.values()),
        frozenset(check_runs),
    )


def admit_publication_failure_recovery(
    ledger: GitHubIssueCommentSessionLedger,
    identity: SessionIdentity,
    record: SessionRecord,
    old: ApiRunAccounting,
) -> tuple[PreExternalWriteSettlementWitness, SessionRecord | None]:
    """Verify witness, 3-surface negative census, and prior root inside rollover CAS."""
    if (
        old.action is not None
        or old.analysis_abort is not None
        or old.terminal_transaction is not None
        or record.reservation_id is not None
        or record.reserved_slot is not None
        or record.publication_staging is not None
        or record.identity() != identity
        or old.identity.repository_id != identity.repository_id
        or (
            record.transaction is not None
            and record.transaction.phase != "publication_failed"
        )
        or (
            record.transaction is None
            and (
                record.failed_attempts <= 0
                or not isinstance(record.failed_attempts_head_sha, str)
                or _HEX40.fullmatch(record.failed_attempts_head_sha) is None
            )
        )
    ):
        raise _hold(
            "predecessor state does not qualify for pre-write failure rollover",
            record=record,
        )
    if (
        record.transaction is not None
        and record.finding_lifecycle is not None
        and record.finding_lifecycle.get("operation_id")
        == record.transaction.transaction_id
    ):
        raise _hold(
            "finding lifecycle already recorded effects for the failed transaction",
            record=record,
        )
    budget, app_slug = ledger.evidence_budget, ledger.app_slug
    if budget is None or not app_slug or not ledger._mutation_active:
        raise _hold("authenticated native census is unavailable", record=record)

    witness = find_authenticated_settlement_witness(ledger, identity, record, old)
    producer_id = _producer_id(ledger.evidence_producer())
    http = ledger.http
    deadline = http.evidence_monotonic() + _CENSUS_WALL_SECONDS
    head_sha = witness.head_sha

    prior_record: SessionRecord | None = None
    prior_archive_ref: dict[str, Any] | None = None
    skipped_failed_ancestors: list[SessionRecord] = []
    if record.transaction is not None and old.previous_record is not None:
        cursor_ref: Any = old.previous_record
        child_record = record
        for _ in range(_MAX_PRIOR_ANCESTOR_HOPS):
            if not isinstance(cursor_ref, dict):
                raise _hold(
                    "prior settled session root conflicts with predecessor",
                    record=record,
                )
            _protect_tail(budget, _archive_part_count(cursor_ref), record=record)
            try:
                candidate = read_previous_run(ledger, identity, cursor_ref)
            except (GitHubHTTPError, GitHubPublicationError, ReviewInputError):
                raise _hold(
                    "prior settled session root is unavailable", record=record
                ) from None
            if (
                candidate.identity() != identity
                or candidate.record_sha256 != cursor_ref.get("root_sha256")
                or candidate.generation >= child_record.generation
                or candidate.api_run_accounting is None
            ):
                raise _hold(
                    "prior settled session root conflicts with predecessor",
                    record=record,
                )
            candidate_acc = ApiRunAccounting.from_document(candidate.api_run_accounting)
            if candidate_acc.identity.run_id != cursor_ref.get("run_id"):
                raise _hold(
                    "prior settled session root conflicts with predecessor",
                    record=record,
                )
            if (
                candidate.transaction is not None
                and candidate.transaction.phase == "publication_failed"
            ):
                skipped_failed_ancestors.append(candidate)
                child_record = candidate
                cursor_ref = candidate_acc.previous_record
                if cursor_ref is None:
                    break
                continue
            prior_record = candidate
            prior_archive_ref = cursor_ref
            break
        else:
            raise _hold(
                "prior settled session root exceeds archive walk bound",
                record=record,
            )
        if prior_record is not None and (
            prior_record.completed_initial_reviews > record.completed_initial_reviews
            or prior_record.completed_verification_rounds
            > record.completed_verification_rounds
        ):
            raise _hold(
                "prior settled session root conflicts with predecessor",
                record=record,
            )

    earlier: _EarlierSettledEffects | None = None
    earlier_review_ids: set[int] = set()

    def ensure_earlier() -> _EarlierSettledEffects:
        nonlocal earlier
        if earlier is None:
            earlier = _earlier_settled_effects(
                ledger,
                identity,
                record,
                old,
                head_sha,
                producer_id,
                budget,
                prior_record=prior_record,
                prior_archive_ref=prior_archive_ref,
            )
        return earlier

    def inspect_pr_surface(
        resource: str,
        classifier: Callable[[dict[str, Any]], bool | None],
        *,
        inline: bool = False,
    ) -> None:
        path = http.repository_path(
            identity.repository, f"/pulls/{identity.pull_request}/{resource}"
        )
        seen: set[int] = set()
        for page in range(1, MAX_PAGINATION_PAGES + 1):
            _protect_tail(budget, 1, record=record)
            remaining = deadline - http.evidence_monotonic()
            if remaining <= 0:
                raise _hold("native effect census reached its deadline", record=record)
            try:
                status, rows = http.request(
                    "GET",
                    f"{path}?per_page={_PAGE_SIZE}&page={page}",
                    token=ledger.token,
                    timeout_seconds=remaining,
                )
            except GitHubHTTPError:
                raise _hold(
                    "native effect census is unavailable", record=record
                ) from None
            if status != 200 or not isinstance(rows, list) or len(rows) > _PAGE_SIZE:
                raise _hold("native effect census is incomplete", record=record)
            if http.evidence_monotonic() >= deadline:
                raise _hold("native effect census reached its deadline", record=record)
            for row in rows:
                if (
                    not isinstance(row, dict)
                    or not _positive_id(row.get("id"))
                    or row["id"] in seen
                ):
                    raise _hold(
                        "native effect census has an unreadable row", record=record
                    )
                seen.add(row["id"])
                effect = classifier(row)
                # Classifiers return False for unrelated human/foreign/different-head rows
                # and None exclusively for unreadable or contradictory Bot-owned rows.
                if effect is None:
                    raise _hold("native effect scope is unconfirmed", record=record)
                if effect is True:
                    ancestor = ensure_earlier()
                    if inline:
                        parent = row.get("pull_request_review_id")
                        if not _positive_id(parent) or parent not in earlier_review_ids:
                            raise _hold(
                                "late native effect on the failed scope", record=record
                            )
                    if not ancestor.explains(row, inline=inline):
                        raise _hold(
                            "late native effect on the failed scope", record=record
                        )
                    if not inline:
                        earlier_review_ids.add(row["id"])
            if len(rows) < _PAGE_SIZE:
                if http.evidence_monotonic() >= deadline:
                    raise _hold(
                        "native effect census reached its deadline", record=record
                    )
                return
        raise _hold("native effect census reached its pagination bound", record=record)

    def classify_review(row: dict[str, Any]) -> bool | None:
        return classify_abort_review_effect(
            row,
            identity=identity,
            head_sha=head_sha,
            app_slug=app_slug,
            app_user_id=producer_id,
        )

    def classify_inline(row: dict[str, Any]) -> bool | None:
        return classify_abort_inline_effect(
            author=row.get("user"),
            native_head=row.get("original_commit_id"),
            current_head=row.get("commit_id"),
            body=row.get("body"),
            identity=identity,
            head_sha=head_sha,
            app_slug=app_slug,
            app_user_id=producer_id,
        )

    def inspect_check_runs() -> None:
        path = http.repository_path(
            identity.repository, f"/commits/{head_sha}/check-runs"
        )
        seen: set[int] = set()
        total: int | None = None
        expected_slug = app_slug.removesuffix("[bot]").casefold()
        for page in range(1, MAX_PAGINATION_PAGES + 1):
            _protect_tail(budget, 1, record=record)
            remaining = deadline - http.evidence_monotonic()
            if remaining <= 0:
                raise _hold(
                    "native check-run census reached its deadline", record=record
                )
            try:
                status, value = http.request(
                    "GET",
                    f"{path}?check_name={REVIEW_CHECK_NAME}&per_page={_PAGE_SIZE}&page={page}",
                    token=ledger.token,
                    timeout_seconds=remaining,
                )
            except GitHubHTTPError:
                raise _hold(
                    "native check-run census is unavailable", record=record
                ) from None
            if http.evidence_monotonic() >= deadline:
                raise _hold(
                    "native check-run census reached its deadline", record=record
                )
            if not isinstance(value, dict) or status != 200:
                raise _hold("native check-run census is incomplete", record=record)
            rows, count = value.get("check_runs"), value.get("total_count")
            if (
                not isinstance(rows, list)
                or len(rows) > _PAGE_SIZE
                or type(count) is not int
                or count < 0
                or (total is not None and total != count)
            ):
                raise _hold("native check-run census is incomplete", record=record)
            total = count
            for row in rows:
                if (
                    not isinstance(row, dict)
                    or not _positive_id(row.get("id"))
                    or row["id"] in seen
                ):
                    raise _hold(
                        "native check-run census has an unreadable row", record=record
                    )
                seen.add(row["id"])
                app = row.get("app")
                if row.get("name") == REVIEW_CHECK_NAME:
                    if (
                        not isinstance(app, dict)
                        or not isinstance(app.get("slug"), str)
                        or row.get("head_sha") not in {head_sha, None}
                    ):
                        raise _hold(
                            "native check-run scope is unconfirmed", record=record
                        )
                    if app["slug"].casefold() == expected_slug:
                        ext_id = row.get("external_id")
                        if ext_id not in {None, REVIEW_CHECK_EXTERNAL_ID}:
                            raise _hold(
                                "late native check-run effect on the failed scope",
                                record=record,
                            )
                        if row.get("head_sha") != head_sha:
                            raise _hold(
                                "native check-run scope is unconfirmed", record=record
                            )
                        ancestor = ensure_earlier()
                        if not ancestor.explains_check_run(row):
                            raise _hold(
                                "late native check-run effect on the failed scope",
                                record=record,
                            )
            if len(rows) < _PAGE_SIZE:
                if len(seen) != total:
                    raise _hold(
                        "native check-run pagination is incomplete", record=record
                    )
                if http.evidence_monotonic() >= deadline:
                    raise _hold(
                        "native check-run census reached its deadline", record=record
                    )
                return
        raise _hold(
            "native check-run census reached its pagination bound", record=record
        )

    inspect_pr_surface("reviews", classify_review)
    inspect_pr_surface("comments", classify_inline, inline=True)
    inspect_check_runs()
    if ledger._mutation_active:
        if prior_record is not None:
            _ADMITTED_PRIOR_RECORDS[ledger] = (record.record_sha256, prior_record)
        if skipped_failed_ancestors:
            _ADMITTED_PRIOR_CHAINS[ledger] = (
                record.record_sha256,
                tuple(skipped_failed_ancestors),
            )
    return witness, prior_record


def require_publication_failure_admission(
    ledger: GitHubIssueCommentSessionLedger,
    identity: SessionIdentity,
    record: SessionRecord,
    old: ApiRunAccounting,
) -> PreExternalWriteSettlementWitness:
    """Verify witness + 3-surface negative native census inside rollover CAS."""
    witness, _ = admit_publication_failure_recovery(ledger, identity, record, old)
    return witness


__all__ = [
    "PreExternalWriteSettlementWitness",
    "SETTLEMENT_BOUNDARIES",
    "SETTLEMENT_WITNESS_MARKER",
    "SETTLEMENT_WITNESS_SCHEMA",
    "admit_publication_failure_recovery",
    "clear_admitted_prior_record",
    "consume_admitted_prior_chain",
    "consume_admitted_prior_record",
    "emit_pre_external_write_settlement",
    "find_authenticated_settlement_witness",
    "parse_settlement_witness_comment",
    "peek_admitted_prior_chain",
    "render_settlement_witness_comment",
    "require_publication_failure_admission",
]

from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()

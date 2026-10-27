"""One bounded retained PR root for an authenticated different Actions run.

Uses the existing immutable evidence-part codec and Bot ownership boundary, not
an archival service or restored allowance. A lost create response permits exact
owned readback once, never another POST. History pointers never confer authority.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping, cast

from review_sensei.api_work import ApiRunAccounting
from review_sensei.bounded_evidence import (
    MAX_MANIFEST_BYTES,
    MAX_PART_BYTES,
    MAX_PARTITION_DECODED_BYTES,
    MAX_STORED_PART_BYTES,
    MAX_STORED_PARTS,
    canonical_bytes,
    decode_evidence,
    evidence_manifest,
    partition_evidence,
    read_inline_evidence,
    read_partitioned_evidence,
)
from review_sensei.errors import ReviewInputError
from review_sensei.session import SessionIdentity, SessionLoadResult, SessionRecord

from .errors import GitHubPublicationTransientError
from .http import MAX_PAGINATION_PAGES
from .session_ledger import GitHubIssueCommentSessionLedger

RETAINED_CONFIRMATION_TAIL = 4 + 3 * MAX_PAGINATION_PAGES


def action_result_binding(
    identity: SessionIdentity, state: ApiRunAccounting, producer: str
) -> dict[str, object]:
    """Stable binding for one action, including its authenticated antecedent."""
    action = state.action
    if action is None:
        raise ReviewInputError("caller action intent is unavailable")
    intent = action["intent"]
    return {
        "repository": identity.repository,
        "repository_id": identity.repository_id,
        "pull_request": identity.pull_request,
        # An action has no new reviewed base. P identifies its frozen policy;
        # this value does not construct or replace any review transaction.
        "base_sha": state.configuration.commit,
        "head_sha": intent["head_sha"],
        "policy_digest": hashlib.sha256(canonical_bytes(intent)).hexdigest(),
        "configuration_digest": state.configuration.digest,
        "generation": intent["owner"]["generation"],
        "producer": producer,
        "purpose": "evidence",
        "schema_version": "1.0",
    }


def decode_action_result_carrier(value: object) -> dict[str, Any]:
    """Bound lossless action-result expansion; this does not authenticate it."""
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "payload"}
        or value["schema_version"] != "caller-action-result-v1"
    ):
        raise ReviewInputError("caller action result carrier is unsupported")
    document = decode_evidence(
        value["payload"],
        max_encoded_bytes=MAX_MANIFEST_BYTES,
        max_decoded_bytes=MAX_PARTITION_DECODED_BYTES,
    )
    if (
        not isinstance(document, dict)
        or set(document) != {"schema_version", "intent_sha256", "outcome", "result"}
        or document["schema_version"] != "caller-action-result-v1"
        or not isinstance(document["intent_sha256"], str)
        or re.fullmatch(r"[a-f0-9]{64}", document["intent_sha256"]) is None
        or not isinstance(document["outcome"], dict)
        or not isinstance(document["result"], dict)
    ):
        raise ReviewInputError("caller action result envelope is unsupported")
    return document


def read_action_result(ledger, identity, record) -> dict[str, Any]:
    """Authenticate an exact retained action outcome from a native root."""
    state = ApiRunAccounting.from_document(record.api_run_accounting)
    action = state.action
    if record.identity() != identity or action is None or action["phase"] != "terminal":
        raise ReviewInputError("caller action has no terminal retained result")
    binding = action_result_binding(identity, state, ledger.evidence_producer())
    value = decode_action_result_carrier(
        read_inline_evidence(action["result"], expected_binding=binding)
    )
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "intent_sha256", "outcome", "result"}
        or value["schema_version"] != "caller-action-result-v1"
        or value["intent_sha256"] != binding["policy_digest"]
        or value["outcome"] != action["outcome"]
        or not isinstance(value["result"], dict)
    ):
        raise ReviewInputError("caller action retained result conflicts")
    return value


def retained_binding(record: SessionRecord, producer: str) -> dict[str, object]:
    state = ApiRunAccounting.from_document(record.api_run_accounting)
    if state.action is not None:
        if state.action["phase"] != "terminal":
            raise ReviewInputError("previous run has an unfinished action")
        binding = action_result_binding(record.identity(), state, producer)
        return {**binding, "generation": record.generation}
    transaction = record.transaction
    if transaction is None and state.analysis_abort is not None:
        from review_sensei.models import ReviewTransaction

        transaction = ReviewTransaction.from_dict(
            cast(Mapping[str, object], state.analysis_abort["transaction"])
        )
    if transaction is None:
        raise ReviewInputError("previous run has no terminal review transaction")
    return {
        "repository": record.repository,
        "repository_id": record.repository_id,
        "pull_request": record.pull_request,
        "base_sha": transaction.base_sha,
        "head_sha": transaction.head_sha,
        "policy_digest": transaction.policy_digest,
        "configuration_digest": state.configuration.digest,
        "generation": record.generation,
        "producer": producer,
        "purpose": "evidence",
        "schema_version": "1.0",
    }


def _prepared_archive(ledger, previous, completion):
    state = ApiRunAccounting.from_document(previous.api_run_accounting)
    producer = ledger.evidence_producer()
    binding = retained_binding(previous, producer)
    document = {"schema_version": "caller-run-root-v1", "record": previous.to_dict()}
    if completion is not None:
        from .action_completion import _issued
        from .historical_completion import HistoricalPublicationCompletion

        if type(completion) is not HistoricalPublicationCompletion or not _issued(
            completion
        ):
            raise ReviewInputError(
                "historical archive requires process-issued completion"
            )
        completion.selection.select(SessionLoadResult(status="ok", record=previous))
        document = {
            "schema_version": "caller-run-root-v2",
            "record": previous.to_dict(),
            "completion": completion.receipt,
        }
    parts = partition_evidence(document, binding=binding, item_count=1)
    # The root's independent byte bound does not guarantee a one-part archive.
    # Refuse before POST when actual prepared bytes need multiple carriers.
    if len(parts) != 1:
        raise ReviewInputError("retained run exceeds its one-part carrier")
    body = (
        "ReviewSensei immutable evidence part v1\n```json\n"
        + canonical_bytes(parts[0]).decode()
        + "\n```\n<!-- reviewsensei:evidence-part:v1 -->"
    )
    if len(body.encode()) > MAX_PART_BYTES:
        raise ReviewInputError("retained run exceeds its carrier bound")

    return state, binding, document, parts, body


def preview_retained_reference(ledger, previous, *, completion):
    """Worst-width value for root capacity only; never a stored archive proof."""
    if not ledger._mutation_active:
        raise ReviewInputError("retained capacity preview requires the native mutation")
    state, binding, document, parts, _body = _prepared_archive(
        ledger, previous, completion
    )
    manifest = evidence_manifest(
        document, binding=binding, item_count=1, parts=parts, storage_ids=("9" * 19,)
    )
    return {
        "schema_version": "caller-run-reference-v2",
        "run_id": state.identity.run_id,
        "root_sha256": previous.record_sha256,
        "manifest": manifest,
        "completion": completion.receipt,
    }


def retain_previous_run(
    ledger: GitHubIssueCommentSessionLedger,
    identity: SessionIdentity,
    previous: SessionRecord,
    *,
    completion=None,
) -> dict[str, object]:
    """Only inside the native serialized mutation after fresh root discovery."""
    if not ledger._mutation_active or ledger.evidence_budget is None:
        raise ReviewInputError("retained run requires the current native mutation")
    state, binding, document, parts, body = _prepared_archive(
        ledger, previous, completion
    )

    def owned(item: object) -> bool:
        return (
            isinstance(item, dict)
            and isinstance(item.get("user"), dict)
            and item["user"].get("id") == ledger._evidence_author_id
            and item["user"].get("type") == "Bot"
        )

    def matching(items: list[Any]) -> list[str]:
        found = []
        for item in items:
            if owned(item) and isinstance(item, dict) and item.get("body") == body:
                ledger._part_owner(item, identity)
                found.append(str(item["id"]))
        if len(found) > 1:
            raise ReviewInputError("retained run carrier is duplicated")
        return found

    items = ledger._cached_comments()
    existing = matching(items)
    if not existing:
        retained = [
            item
            for item in items
            if owned(item)
            and isinstance(item, dict)
            and isinstance(item.get("body"), str)
            and item["body"].endswith("<!-- reviewsensei:evidence-part:v1 -->")
        ]
        from .quarantine_published_tail import retained_storage_reserve

        reserve_parts, reserve_bytes = retained_storage_reserve(ledger)
        if (
            len(retained) + reserve_parts >= MAX_STORED_PARTS
            or sum(len(item["body"].encode()) for item in retained)
            + len(body.encode())
            + reserve_bytes
            > MAX_STORED_PART_BYTES
        ):
            raise ReviewInputError("retained run carrier capacity exceeded")
        # Reserve the bounded lost-response listing plus all remaining root
        # scans/head/PATCH/readback before the first immutable POST.
        requests = (
            1
            + MAX_PAGINATION_PAGES
            + 1
            + MAX_PAGINATION_PAGES
            + 1
            + 1
            + MAX_PAGINATION_PAGES
        )
        if previous.quarantine_lineage is not None:
            from .quarantine_rollover import retention_preflight

            exact = retention_preflight(ledger, previous)
            if exact is not None:
                requests = exact
        ledger.evidence_budget.preflight(requests)
        try:
            status, payload = ledger._request(
                "POST", ledger._comments_path(identity), body={"body": body}
            )
        except GitHubPublicationTransientError:
            status, payload = 0, None
        if status == 201 and isinstance(payload, dict):
            ledger._part_owner(payload, identity)
            if payload.get("body") != body:
                raise ReviewInputError("retained run create response conflicts")
            storage_id = str(payload["id"])
        elif status in {0, 202, 409, 422, 429} or status >= 500:
            # Absence is not permission to retry. Authentication and complete
            # enumeration are required, even if the response was merely lost.
            ledger._list_refresh = True
            existing = matching(ledger._bounded_comments(identity))
            if len(existing) != 1:
                raise ReviewInputError("retained run create remains uncertain")
            storage_id = existing[0]
        else:
            raise ReviewInputError("retained run create failed")
    else:
        storage_id = existing[0]
    if not re.fullmatch(r"[1-9][0-9]{0,18}", storage_id):
        raise ReviewInputError("retained run storage identity is invalid")
    manifest = evidence_manifest(
        document, binding=binding, item_count=1, parts=parts, storage_ids=(storage_id,)
    )
    restored = read_partitioned_evidence(
        manifest,
        reader=lambda value: ledger._read_part(identity, value),
        expected_binding=binding,
        budget=ledger.evidence_budget,
    )
    if canonical_bytes(restored) != canonical_bytes(document):
        raise ReviewInputError("retained run readback differs")
    reference = {
        "run_id": state.identity.run_id,
        "root_sha256": previous.record_sha256,
        "manifest": manifest,
    }
    if completion is not None:
        reference.update(
            schema_version="caller-run-reference-v2", completion=completion.receipt
        )
    return reference


def read_previous_run(ledger, identity, reference) -> SessionRecord:
    """Authenticate a retained root before using it for subsequent recovery."""
    if ledger.evidence_budget is None:
        raise ReviewInputError("retained run reader requires caller accounting")
    manifest = reference["manifest"]
    value = read_partitioned_evidence(
        manifest,
        reader=lambda key: ledger._read_part(identity, key),
        expected_binding=manifest["binding"],
        budget=ledger.evidence_budget,
    )
    historical = (
        isinstance(reference, dict)
        and reference.get("schema_version") == "caller-run-reference-v2"
    )
    if (
        not isinstance(value, dict)
        or set(value)
        != (
            {"schema_version", "record", "completion"}
            if historical
            else {"schema_version", "record"}
        )
        or value["schema_version"]
        != ("caller-run-root-v2" if historical else "caller-run-root-v1")
    ):
        raise ReviewInputError("retained run document is unsupported")
    record = SessionRecord.from_dict(value["record"])
    state = ApiRunAccounting.from_document(record.api_run_accounting)
    if historical:
        from review_sensei.historical_completion import (
            historical_receipt_matches,
            validate_receipt,
        )

        receipt = validate_receipt(value["completion"])
        if (
            receipt != reference["completion"]
            or not historical_receipt_matches(record, receipt)
            or receipt["historical_root_sha256"] != record.record_sha256
            or receipt["historical_run_id"] != state.identity.run_id
            or receipt["historical_accounting_sha256"]
            != hashlib.sha256(canonical_bytes(state.to_document())).hexdigest()
            or record.transaction is None
            or receipt["transaction_id"] != record.transaction.transaction_id
        ):
            raise ReviewInputError("historical archive completion receipt conflicts")
    if (
        record.identity() != identity
        or record.record_sha256 != reference["root_sha256"]
        or state.identity.run_id != reference["run_id"]
        or retained_binding(record, ledger.evidence_producer()) != manifest["binding"]
    ):
        raise ReviewInputError("retained run source binding conflicts")
    return record


def inherited_preprovider_reference(record, accounting):
    """Recognize only the v2 post-enrollment/pre-transaction boundary.

    This is evidence selection only. Unchanged bytes do not prove that an
    earlier root PATCH was never dispatched. Abrupt successors must refuse this
    state without positive settlement or a proven pre-operation abort.
    """
    reference = accounting.previous_record
    if (
        reference is None
        or reference.get("schema_version") != "caller-run-reference-v2"
    ):
        return None
    receipt = reference["completion"]
    transaction = record.transaction
    from review_sensei.api_work import ApiExecutionOwner

    if (
        transaction is None
        or transaction.phase != "publication_succeeded"
        or transaction.transaction_id != receipt["transaction_id"]
        or transaction.base_sha != receipt["historical_base_sha"]
        or transaction.head_sha != receipt["historical_head_sha"]
        or record.reservation_id is not None
        or record.reserved_slot is not None
        or accounting.terminal_transaction is not None
        or accounting.action is not None
        or accounting.analysis_abort is not None
        or receipt["current_run_id"] != accounting.identity.run_id
        or receipt["current_owner_sha256"]
        != hashlib.sha256(
            canonical_bytes(
                {"run_attempt": 1, "job": accounting.owner.job, "generation": 0}
            )
        ).hexdigest()
        or accounting.owner
        not in (
            ApiExecutionOwner(1, accounting.owner.job, 0),
            ApiExecutionOwner(2, accounting.owner.job, 1),
        )
    ):
        return None
    return reference

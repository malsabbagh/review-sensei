"""One bounded retained PR root for an authenticated different Actions run.

Uses the existing immutable evidence-part codec and Bot ownership boundary, not
an archival service or restored allowance. A lost create response permits exact
owned readback once, never another POST. History pointers never confer authority.
"""

from __future__ import annotations

import re
from typing import Any

from review_sensei.api_work import ApiRunAccounting
from review_sensei.bounded_evidence import (
    MAX_PART_BYTES,
    MAX_STORED_PART_BYTES,
    MAX_STORED_PARTS,
    canonical_bytes,
    evidence_manifest,
    partition_evidence,
    read_partitioned_evidence,
)
from review_sensei.errors import ReviewInputError
from review_sensei.session import SessionIdentity, SessionRecord

from .errors import GitHubPublicationTransientError
from .http import MAX_PAGINATION_PAGES
from .session_ledger import GitHubIssueCommentSessionLedger


def retained_binding(record: SessionRecord, producer: str) -> dict[str, object]:
    state = ApiRunAccounting.from_document(record.api_run_accounting)
    transaction = record.transaction
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


def retain_previous_run(
    ledger: GitHubIssueCommentSessionLedger,
    identity: SessionIdentity,
    previous: SessionRecord,
) -> dict[str, object]:
    """Only inside the native serialized mutation after fresh root discovery."""
    if not ledger._mutation_active or ledger.evidence_budget is None:
        raise ReviewInputError("retained run requires the current native mutation")
    state = ApiRunAccounting.from_document(previous.api_run_accounting)
    producer = ledger.evidence_producer()
    binding = retained_binding(previous, producer)
    document = {"schema_version": "caller-run-root-v1", "record": previous.to_dict()}
    parts = partition_evidence(document, binding=binding, item_count=1)
    # SessionRecord's existing byte bound fits one codec part. Do not grow this
    # into a general history staging protocol if that bound ever changes.
    if len(parts) != 1:
        raise ReviewInputError("retained run exceeds its one-part carrier")
    body = (
        "ReviewSensei immutable evidence part v1\n```json\n"
        + canonical_bytes(parts[0]).decode()
        + "\n```\n<!-- reviewsensei:evidence-part:v1 -->"
    )
    if len(body.encode()) > MAX_PART_BYTES:
        raise ReviewInputError("retained run exceeds its carrier bound")

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
        if (
            len(retained) >= MAX_STORED_PARTS
            or sum(len(item["body"].encode()) for item in retained) + len(body.encode())
            > MAX_STORED_PART_BYTES
        ):
            raise ReviewInputError("retained run carrier capacity exceeded")
        # Reserve the bounded lost-response listing plus all remaining root
        # scans/head/PATCH/readback before the first immutable POST.
        ledger.evidence_budget.preflight(
            1
            + MAX_PAGINATION_PAGES
            + 1
            + MAX_PAGINATION_PAGES
            + 1
            + 1
            + MAX_PAGINATION_PAGES
        )
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
    return {
        "run_id": state.identity.run_id,
        "root_sha256": previous.record_sha256,
        "manifest": manifest,
    }


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
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "record"}
        or value["schema_version"] != "caller-run-root-v1"
    ):
        raise ReviewInputError("retained run document is unsupported")
    record = SessionRecord.from_dict(value["record"])
    state = ApiRunAccounting.from_document(record.api_run_accounting)
    if (
        record.identity() != identity
        or record.record_sha256 != reference["root_sha256"]
        or state.identity.run_id != reference["run_id"]
        or retained_binding(record, ledger.evidence_producer()) != manifest["binding"]
    ):
        raise ReviewInputError("retained run source binding conflicts")
    return record

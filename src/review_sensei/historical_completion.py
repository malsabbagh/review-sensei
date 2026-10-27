"""Closed evidence for one historical-publication/current-run transition.

Values constrain archival and retention; they do not authenticate a writer or
authorize enrollment. The hosted adapter must supply fresh process-issued proof.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict

from .bounded_evidence import canonical_bytes
from .errors import ReviewInputError

REFERENCE_VERSION = "caller-run-reference-v2"
RECEIPT_VERSION = "historical-publication-completion-v1"

# Immutable executor qualification, not a mutable tag/version-name allowlist.
# The signed object/source/package chain and complete root-writer contract must
# be qualified before another executor can be admitted by this historical path.
QUALIFIED_EXECUTORS = {
    "86017489651cfc6ef7dd493bef255e1edebbe556": {
        "commit": "52b5ca47ba3508a526312ce8424ff0c7fa1bb347",
        "workflow_blob": "4f337242534a9246f7cdf5f70cd367b40ea5cafb",
        "workflow_path": "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@v5",
        "workflow_ref": "refs/tags/v5",
        "core": "0.8.14",
        "hosted": "0.3.20",
        "contract": "successful-review-no-unsettled-root-write-v1",
    },
}


def validate_receipt(value):
    from .schemas import validate_public_document

    validate_public_document(value, "historical-publication-completion")
    return value


def historical_receipt_matches(previous, receipt):
    validate_receipt(receipt)
    transaction = previous.transaction
    from .api_work import ApiRunAccounting

    old = ApiRunAccounting.from_document(previous.api_run_accounting)
    return (
        transaction is not None
        and transaction.phase == "publication_succeeded"
        and old.terminal_transaction is None
        and old.action is None
        and old.analysis_abort is None
        and not previous.operator_paused
        and previous.reservation_id is None
        and previous.reserved_slot is None
        and receipt["repository"] == previous.repository
        and receipt["repository_id"] == previous.repository_id
        and receipt["pull_request"] == previous.pull_request
        and receipt["historical_run_id"] == old.identity.run_id
        and receipt["historical_root_sha256"] == previous.record_sha256
        and receipt["historical_accounting_sha256"]
        == hashlib.sha256(canonical_bytes(old.to_document())).hexdigest()
        and receipt["transaction_id"] == transaction.transaction_id
        and receipt["historical_base_sha"] == transaction.base_sha
        and receipt["historical_head_sha"] == transaction.head_sha
        and receipt["historical_policy_sha256"] == transaction.policy_digest
        and receipt["historical_configuration_sha256"]
        == transaction.configuration_digest
        and receipt["historical_evidence_sha256"] == transaction.evidence_digest
        and receipt["historical_content_sha256"] == transaction.result_sha256
        and receipt["historical_executor_sha"] in QUALIFIED_EXECUTORS
        and receipt["historical_executor_commit"]
        == QUALIFIED_EXECUTORS[receipt["historical_executor_sha"]]["commit"]
        and receipt["historical_workflow_blob"]
        == QUALIFIED_EXECUTORS[receipt["historical_executor_sha"]]["workflow_blob"]
        and receipt["publication_result_sha256"]
        == hashlib.sha256(
            canonical_bytes(
                {
                    "domain": "reviewsensei:transaction-publication:v1",
                    "transaction_id": transaction.transaction_id,
                    "result_content_sha256": transaction.result_sha256,
                }
            )
        ).hexdigest()
        and receipt["historical_head_sha"] != receipt["current_head_sha"]
    )


def receipt_matches(previous, candidate, receipt):
    return (
        historical_receipt_matches(previous, receipt)
        and receipt["repository_id"] == candidate.identity.repository_id
        and receipt["current_run_id"] == candidate.identity.run_id
        and receipt["current_owner_sha256"]
        == hashlib.sha256(canonical_bytes(asdict(candidate.owner))).hexdigest()
        and receipt["current_policy_commit"] == candidate.configuration.commit
        and receipt["current_configuration_sha256"] == candidate.configuration.digest
    )


def require_receipt_matches(previous, candidate, receipt):
    if not receipt_matches(previous, candidate, receipt):
        raise ReviewInputError("historical completion receipt scope conflicts")

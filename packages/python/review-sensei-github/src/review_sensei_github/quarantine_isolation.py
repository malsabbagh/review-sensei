"""Source-bound finite target isolation, never failed-run completion.

Only the recognized default workflow's deterministic prepublication literal-path
failure is classified. This component reads Actions evidence and issues a local
proof; it neither creates a ledger nor admits original human assessments.
"""

from __future__ import annotations

import hashlib
import json
import re
import weakref

from review_sensei.api_work import ApiRunAccounting
from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewResult
from review_sensei.session import SessionRecord

from .api_run_owner import ApiRunActionsEvidence, _writer_name
from .reservation_recovery import _stamp

# Immutable source qualification, not a version/tag-name permission. The source
# constructs BlockerCandidate for every result comment before reviewer.publish;
# its literal-glob rejection is deterministic. The exception path can dispatch
# only old-root cleanup, old-head failed-check writes and old-transaction parts.
_EXECUTORS = {
    "86017489651cfc6ef7dd493bef255e1edebbe556": {
        "commit": "52b5ca47ba3508a526312ce8424ff0c7fa1bb347",
        "workflow_blob": "4f337242534a9246f7cdf5f70cd367b40ea5cafb",
        "workflow_path": "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@v5",
        "workflow_ref": "refs/tags/v5",
        "contract": "literal-blocker-path-prepublication-v1",
        "caller_path": ".github/workflows/review-sensei-review.yml",
        "caller_blob": "dbc745b3b3471cf2d9861e9a84774e34eb0d6e8b",
    },
    "c7f13b8cfe28e89ec82ad03bd2f34aed5b40cbb1": {
        "commit": "e006596160be74a9a72b9428c5675224afa347c6",
        "workflow_blob": "b23d80ebbc831f8baa6a38937c044dc3a13633f7",
        "workflow_path": "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@v5",
        "workflow_ref": "refs/tags/v5",
        "contract": "body-limit-prepublication-v1",
        "caller_path": ".github/workflows/review-sensei-review.yml",
        "caller_blob": "dbc745b3b3471cf2d9861e9a84774e34eb0d6e8b",
    },
}
_ISSUED: weakref.WeakSet[IsolatedPredecessor] = weakref.WeakSet()


class IsolatedPredecessor:
    """Read-only issued proof bound to exact source, transport and predecessor."""

    def __init__(
        self, *, http, identity, previous, predecessor_comment_id, current_head, receipt
    ):
        self._http, self._identity = http, identity
        self._root = previous.record_sha256
        self._comment_id, self._head = predecessor_comment_id, current_head
        self._receipt = canonical_bytes(receipt)

    @property
    def receipt(self):
        return json.loads(self._receipt)

    def require_scope(
        self, *, http, identity, previous, predecessor_comment_id, current_head
    ):
        if (
            self not in _ISSUED
            or self._http is not http
            or self._identity != identity
            or self._root != previous.record_sha256
            or self._comment_id != predecessor_comment_id
            or self._head != current_head
        ):
            raise ReviewInputError(
                "quarantine target-isolation proof is foreign or changed"
            )


def authenticate_isolated_predecessor(
    *,
    actions,
    metadata_token,
    identity,
    previous,
    retained_result,
    predecessor_comment_id,
    current_head,
    creation_run_id=None,
    failed_source=None,
):
    """Authenticate cessation plus deterministic isolation of one failed path.

    The retained result must separately come from authenticated immutable IO.
    Matching its digest here is necessary but grants no original-context or
    assessment authority. Revalidate this evidence and the current head before
    creation; previously dispatched old requests remain uncertain and charged.
    """
    if (
        isinstance(previous, SessionRecord)
        and previous.transaction is not None
        and previous.transaction.phase == "publication_succeeded"
    ):
        from .quarantine_activation import registered_activation
        from .quarantine_published_tail import authenticate_tail_contract

        if (
            not isinstance(actions, ApiRunActionsEvidence)
            or not isinstance(metadata_token, str)
            or not metadata_token.strip()
            or previous.identity() != identity
            or type(predecessor_comment_id) is not int
            or predecessor_comment_id < 1
            or not isinstance(current_head, str)
            or re.fullmatch(r"[a-f0-9]{40}", current_head) is None
        ):
            raise ReviewInputError("published-tail predecessor scope is unsupported")
        registration = registered_activation(identity, predecessor_comment_id)
        if (
            registration["schema_version"]
            not in {
                "quarantine-published-activation-v1",
                "quarantine-published-activation-v2",
            }
            or registration["predecessor_record_sha256"] != previous.record_sha256
            or previous.transaction.head_sha == current_head
            or previous.api_run_accounting is None
            or str(
                ApiRunAccounting.from_document(
                    previous.api_run_accounting
                ).identity.run_id
            )
            == actions.command_run_id
        ):
            raise ReviewInputError("published-tail installed registration conflicts")
        receipt = authenticate_tail_contract(
            actions=actions,
            metadata_token=metadata_token,
            identity=identity,
            previous=previous,
            registration=registration,
            creation_run_id=creation_run_id,
        )
        proof = IsolatedPredecessor(
            http=actions.http,
            identity=identity,
            previous=previous,
            predecessor_comment_id=predecessor_comment_id,
            current_head=current_head,
            receipt=receipt,
        )
        _ISSUED.add(proof)
        return proof
    if (
        not isinstance(metadata_token, str)
        or not metadata_token.strip()
        or not isinstance(actions, ApiRunActionsEvidence)
        or not isinstance(previous, SessionRecord)
        or previous.identity() != identity
        or type(predecessor_comment_id) is not int
        or not 1 <= predecessor_comment_id <= 9_007_199_254_740_991
        or not isinstance(current_head, str)
        or re.fullmatch(r"[a-f0-9]{40}", current_head) is None
        or not isinstance(retained_result, ReviewResult)
        or previous.api_run_accounting is None
    ):
        raise ReviewInputError("quarantine predecessor scope is unsupported")
    old = ApiRunAccounting.from_document(previous.api_run_accounting)
    transaction = previous.transaction
    if (
        old.identity.repository_id != identity.repository_id
        or str(old.identity.run_id) == actions.command_run_id
        or old.owner.run_attempt != 1
        or old.owner.generation != 0
        or old.owner.job not in {"hosted", "local"}
        or old.terminal_transaction is not None
        or old.action is not None
        or old.analysis_abort is not None
        or previous.operator_paused
        or previous.reservation_id is not None
        or previous.reserved_slot is not None
        or previous.rescan_state is not None
        or previous.quarantine_lineage is not None
        or transaction is None
        or transaction.phase != "publication_failed"
        or transaction.head_sha == current_head
        or transaction.policy_digest
        != ReviewConvergencePolicy(mode="merge-focused").digest()
        or retained_result.transaction is None
        or retained_result.transaction.transaction_id != transaction.transaction_id
        or retained_result.content_digest() != transaction.result_sha256
    ):
        raise ReviewInputError(
            "quarantine predecessor has no recognized isolated failure"
        )
    literal = (
        retained_result.review_status == "complete"
        and not retained_result.coverage_only_partial
        and any(
            any(c in comment.path for c in "*?[")
            for comment in retained_result.comments
        )
    )
    body_failure = None
    if not literal:
        from .quarantine_body_limit import ReconstructedFailedSource

        if type(
            failed_source
        ) is not ReconstructedFailedSource or retained_result.review_status not in {
            "complete",
            "partial",
        }:
            raise ReviewInputError(
                "quarantine predecessor has no recognized isolated failure"
            )
        body_failure = failed_source.require_body_failure(
            http=actions.http, previous=previous, result=retained_result
        )
    previous_actions = ApiRunActionsEvidence(
        actions.http,
        token=actions.token,
        command_run_id=str(old.identity.run_id),
        job=old.owner.job,
    )
    run = previous_actions._get(identity, f"/actions/runs/{old.identity.run_id}")
    repository, references = run.get("repository"), run.get("referenced_workflows")
    if (
        type(run.get("id")) is not int
        or run["id"] != old.identity.run_id
        or not isinstance(repository, dict)
        or type(repository.get("id")) is not int
        or repository["id"] != identity.repository_id
        or repository.get("full_name") != identity.repository
        or type(run.get("run_attempt")) is not int
        or run["run_attempt"] != 1
        or run.get("status") != "completed"
        or run.get("conclusion") != "failure"
        or run.get("event") != "pull_request"
        or run.get("head_sha") != transaction.head_sha
        or type(run.get("workflow_id")) is not int
        or not 1 <= run["workflow_id"] <= 9_007_199_254_740_991
        or not isinstance(references, list)
        or len(references) != 1
        or not isinstance(references[0], dict)
    ):
        raise ReviewInputError(
            "quarantine original Actions execution has not ceased or changed"
        )
    source = references[0]
    source_sha = source.get("sha")
    contract = _EXECUTORS.get(source_sha) if isinstance(source_sha, str) else None
    if (
        contract is None
        or contract["contract"]
        != (
            "literal-blocker-path-prepublication-v1"
            if literal
            else "body-limit-prepublication-v1"
        )
        or source.get("path") != contract["workflow_path"]
        or source.get("ref") != contract["workflow_ref"]
        or run.get("path") != contract["caller_path"]
    ):
        raise ReviewInputError("quarantine original executor source is unrecognized")
    # Both parents contain the exact audited caller. A pull-request merge
    # cannot introduce an extra caller job through a head/base source change.
    # The caller resolver has read-only GitHub permissions and no OIDC; the
    # pinned reusable bootstrap/preflight perform routing/planning, not writes.
    for commit in dict.fromkeys((transaction.base_sha, transaction.head_sha)):
        status, caller_source = actions.http.request(
            "GET",
            actions.http.repository_path(
                identity.repository, f"/contents/{contract['caller_path']}?ref={commit}"
            ),
            token=metadata_token,
        )
        if (
            status != 200
            or not isinstance(caller_source, dict)
            or caller_source.get("type") != "file"
            or caller_source.get("path") != contract["caller_path"]
            or caller_source.get("sha") != contract["caller_blob"]
        ):
            raise ReviewInputError("quarantine original caller source is unrecognized")
    jobs = previous_actions._jobs(identity, 1)
    nonwriters = {
        "resolve-trigger": "read-only-caller-routing",
        "review-or-reply / bootstrap": "read-only-policy-and-plan",
        "review-or-reply / authoritative-preflight": "read-only-routing-metadata",
    }
    writers = {"review-or-reply / " + name for name in ("hosted", "local", "command")}
    if {job.get("name") for job in jobs} != set(nonwriters) | writers or len(jobs) != 6:
        raise ReviewInputError(
            "quarantine original caller job inventory is unqualified"
        )
    for job in jobs:
        if job["name"] in nonwriters and (
            job.get("status") != "completed" or job.get("conclusion") != "success"
        ):
            raise ReviewInputError("quarantine original auxiliary job is unconfirmed")
    selected = None
    for kind in ("hosted", "local", "command"):
        matches = [job for job in jobs if _writer_name(job.get("name"), kind)]
        if (
            len(matches) != 1
            or matches[0].get("status") != "completed"
            or matches[0].get("conclusion")
            != ("failure" if kind == old.owner.job else "skipped")
        ):
            raise ReviewInputError(
                "quarantine original writer inventory is unconfirmed"
            )
        if kind == old.owner.job:
            selected = matches[0]
    if selected is None:
        raise ReviewInputError("quarantine original owning job is missing")
    steps = selected.get("steps")
    if (
        not isinstance(steps, list)
        or not 1 <= len(steps) <= 64
        or any(not isinstance(s, dict) for s in steps)
    ):
        raise ReviewInputError("quarantine original step inventory is incomplete")
    review = [s for s in steps if s.get("name") == f"Run {old.owner.job} review"]
    reply = [
        s
        for s in steps
        if s.get("name") == f"Generate and publish {old.owner.job} mention reply"
    ]
    if (
        len(review) != 1
        or len(reply) != 1
        or review[0].get("status") != "completed"
        or review[0].get("conclusion") != "failure"
        or reply[0].get("status") != "completed"
        or reply[0].get("conclusion") != "skipped"
        or type(review[0].get("number")) is not int
        or review[0]["number"] < 1
        or _stamp(review[0].get("completed_at")) > _stamp(selected.get("completed_at"))
    ):
        raise ReviewInputError("quarantine original failed review step is unconfirmed")
    # A later attempt or changed source during collection invalidates the proof.
    latest = previous_actions._get(identity, f"/actions/runs/{old.identity.run_id}")
    if latest != run:
        raise ReviewInputError(
            "quarantine original Actions execution changed during collection"
        )
    receipt = {
        "schema_version": "quarantine-target-isolation-v1",
        "disposition": "unresolved-isolated",
        "predecessor_record_sha256": previous.record_sha256,
        "run_id": old.identity.run_id,
        "run_attempt": 1,
        "executor_sha": source["sha"],
        **contract,
        "caller_workflow_id": run["workflow_id"],
        "nonwriter_classifications": nonwriters,
        "job_id": selected["id"],
        "step_number": review[0]["number"],
        "completed_at": selected["completed_at"],
        "writer_inventory_sha256": hashlib.sha256(canonical_bytes(jobs)).hexdigest(),
        "targets": {
            "root_comment_id": predecessor_comment_id,
            "check_head_sha": transaction.head_sha,
            "evidence_transaction_id": transaction.transaction_id,
        },
    }
    if body_failure is not None:
        from .quarantine_activation import registered_activation
        from .quarantine_published_tail import authenticate_tail_contract

        registration = registered_activation(identity, predecessor_comment_id)
        if (
            registration["schema_version"] != "quarantine-body-limit-activation-v2"
            or registration["predecessor_record_sha256"] != previous.record_sha256
            or registration["head_sha"] != current_head
        ):
            raise ReviewInputError(
                "body-limit installed execution registration conflicts"
            )
        receipt["execution_isolation"] = authenticate_tail_contract(
            actions=actions,
            metadata_token=metadata_token,
            identity=identity,
            previous=previous,
            registration=registration,
            creation_run_id=creation_run_id,
        )
        receipt["body_limit_failure"] = body_failure
    proof = IsolatedPredecessor(
        http=actions.http,
        identity=identity,
        previous=previous,
        predecessor_comment_id=predecessor_comment_id,
        current_head=current_head,
        receipt=receipt,
    )
    _ISSUED.add(proof)
    return proof

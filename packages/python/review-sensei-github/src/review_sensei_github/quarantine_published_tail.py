"""Closed published-tail/reply isolation. Cessation never means settlement.

The installed registration names the complete audited execution set. Positive
Actions reads authenticate it again; a bounded complete census detects omissions.
No CLI dictionary, native thread flag or absent request supplies this authority.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict
from urllib.parse import quote

from review_sensei.api_work import ApiRunAccounting, FrozenApiConfiguration
from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError

from .api_run_owner import ApiRunActionsEvidence
from .reservation_recovery import _stamp

CONTRACT = "published-tail-reply-isolation-v1"
BODY_CONTRACT = "body-limit-execution-isolation-v1"
EXECUTOR = "c7f13b8cfe28e89ec82ad03bd2f34aed5b40cbb1"
EXECUTOR_COMMIT = "e006596160be74a9a72b9428c5675224afa347c6"
WORKFLOW_BLOB = "b23d80ebbc831f8baa6a38937c044dc3a13633f7"
CALLER_BLOB = "e013534fcfc85238fbea598ebb96ca549dfcc600"
BODY_CALLER_BLOB = "dbc745b3b3471cf2d9861e9a84774e34eb0d6e8b"
CALLER_PATH = ".github/workflows/review-sensei-review.yml"
WORKFLOW_PATH = "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@v5"

# A future reviewed qualification adds the immutable deployment-receipt digest
# and its proved finite growth contract. Empty means no live published-tail
# activation is qualified. Registration alone cannot fill this catalog.
BROKER_QUALIFICATIONS: dict[str, dict] = {}
# No actual failed 899 execution is qualified by publishing this parser. Each
# entry requires independent exact-package failure-union/target qualification.
FAILED_PUBLISHED_EXECUTORS: dict[str, dict] = {}
FAILED_CONTRACT = "failed-published-tail-isolation-v1"


def failed_profile(contract):
    if (
        not isinstance(contract, dict)
        or contract.get("schema_version") != FAILED_CONTRACT
    ):
        return None
    digest = contract.get("profile_sha256")
    profile = (
        FAILED_PUBLISHED_EXECUTORS.get(digest) if isinstance(digest, str) else None
    )
    fields = {
        "executor_sha",
        "caller_blob",
        "roles",
        "same_head",
        "failure_union_sha256",
        "core_sha256",
        "hosted_sha256",
    }
    if (
        not isinstance(profile, dict)
        or set(profile) != fields
        or hashlib.sha256(canonical_bytes(profile)).hexdigest() != digest
        or any(
            not isinstance(profile[k], str)
            or re.fullmatch(r"[a-f0-9]{40}", profile[k]) is None
            for k in ("executor_sha", "caller_blob")
        )
        or any(
            not isinstance(profile[k], str)
            or re.fullmatch(r"[a-f0-9]{64}", profile[k]) is None
            for k in ("failure_union_sha256", "core_sha256", "hosted_sha256")
        )
        or type(profile["same_head"]) is not bool
        or not isinstance(profile["roles"], list)
        or not 1 <= len(profile["roles"]) <= 8
        or profile["roles"].count(["published-review", "failure"]) != 1
        or any(
            role
            not in [
                ["published-review", "failure"],
                ["discussion", "failure"],
                ["discussion", "cancelled"],
            ]
            for role in profile["roles"]
        )
    ):
        raise ReviewInputError(
            "failed published-tail source qualification is absent or invalid"
        )
    return profile


def execution_manifest_digest(contract):
    """Execution scope excludes its forward provenance reference; no hash cycle."""
    return hashlib.sha256(
        canonical_bytes(
            {
                key: value
                for key, value in contract.items()
                if key != "broker_qualification_sha256"
            }
        )
    ).hexdigest()


def late_growth(receipt):
    """A permanent reserve; absence and elapsed time release nothing."""
    receipt = receipt.get("execution_isolation", receipt)
    if receipt.get("schema_version") not in {
        "quarantine-published-tail-isolation-v1",
        "quarantine-body-limit-isolation-v1",
    }:
        return {
            "evidence_parts": 0,
            "evidence_bytes": 0,
            "native_objects": 0,
            "native_bytes": 0,
        }
    from .quarantine_tail_provenance import exposure_reserve

    return exposure_reserve(receipt["qualification"])


def retained_storage_reserve(ledger):
    authenticated = getattr(ledger, "_quarantine_authenticated", None)
    if authenticated is None:
        return 0, 0
    growth = late_growth(authenticated.archive["target_isolation"])
    return growth["evidence_parts"], growth["evidence_bytes"]


def normalize_tail_contract(value):
    profile = failed_profile(value)
    body_limit = (
        isinstance(value, dict) and value.get("schema_version") == BODY_CONTRACT
    )
    required = {
        "schema_version",
        "executor_sha",
        "caller_blob",
        "workflow_id",
        "census_started_at",
        "executions",
        "broker_qualification_sha256",
    }
    if profile is not None:
        required.add("profile_sha256")
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value["schema_version"] not in {CONTRACT, BODY_CONTRACT, FAILED_CONTRACT}
        or value["executor_sha"]
        != (profile["executor_sha"] if profile is not None else EXECUTOR)
        or value["caller_blob"]
        != (
            profile["caller_blob"]
            if profile is not None
            else BODY_CALLER_BLOB
            if body_limit
            else CALLER_BLOB
        )
        or type(value["workflow_id"]) is not int
        or value["workflow_id"] < 1
        or not isinstance(value["broker_qualification_sha256"], str)
        or re.fullmatch(r"[a-f0-9]{64}", value["broker_qualification_sha256"]) is None
        or not isinstance(value["executions"], list)
        or len(value["executions"])
        != (len(profile["roles"]) if profile is not None else 1 if body_limit else 5)
    ):
        raise ReviewInputError("published-tail execution contract is unsupported")
    _stamp(value["census_started_at"])
    ids, roles = [], []
    for item in value["executions"]:
        if not isinstance(item, dict) or set(item) != {
            "run_id",
            "run_attempt",
            "job_id",
            "route",
            "conclusion",
            "source",
            "configuration",
            "accounting_snapshot",
        }:
            raise ReviewInputError("published-tail execution instance is unsupported")
        if (
            any(
                type(item[k]) is not int or not 1 <= item[k] <= 9_007_199_254_740_991
                for k in ("run_id", "job_id")
            )
            or type(item["run_attempt"]) is not int
            or item["run_attempt"] != 1
            or item["route"]
            not in (
                {"failed-review"} if body_limit else {"published-review", "discussion"}
            )
            or item["conclusion"] not in {"success", "failure", "cancelled"}
        ):
            raise ReviewInputError("published-tail execution identity conflicts")
        config = item["configuration"]
        if not isinstance(config, dict) or set(config) != {"commit", "digest", "limit"}:
            raise ReviewInputError("published-tail original configuration is absent")
        FrozenApiConfiguration(**config)
        source = item["source"]
        if item["route"] in {"published-review", "failed-review"}:
            if source is not None or item["conclusion"] != (
                "failure" if body_limit or profile is not None else "success"
            ):
                raise ReviewInputError("published-tail original publication conflicts")
        else:
            if (
                not isinstance(source, dict)
                or set(source) != {"kind", "id", "root_id", "updated_at", "body_sha256"}
                or source["kind"] != "review_comment"
                or any(
                    type(source[k]) is not int or source[k] < 1
                    for k in ("id", "root_id")
                )
                or not isinstance(source["body_sha256"], str)
                or re.fullmatch(r"[a-f0-9]{64}", source["body_sha256"]) is None
            ):
                raise ReviewInputError("published-tail discussion source is absent")
            _stamp(source["updated_at"])
        # Available snapshots are retained verbatim, not converted to a zero or
        # a final measured charge. A missing snapshot remains explicitly unknown.
        if item["accounting_snapshot"] is not None:
            snapshot = ApiRunAccounting.from_document(item["accounting_snapshot"])
            if (
                snapshot.identity.run_id != item["run_id"]
                or asdict(snapshot.configuration) != config
            ):
                raise ReviewInputError("published-tail accounting snapshot conflicts")
        ids.append(item["run_id"])
        roles.append((item["route"], item["conclusion"]))
    if ids != sorted(set(ids)) or (
        roles != [tuple(role) for role in profile["roles"]]
        if profile is not None
        else (
            roles != [("failed-review", "failure")]
            if body_limit
            else (
                roles.count(("published-review", "success")) != 1
                or roles.count(("discussion", "failure")) != 1
                or roles.count(("discussion", "cancelled")) != 3
            )
        )
    ):
        raise ReviewInputError("published-tail execution union is incomplete")
    return value


def authenticate_tail_contract(
    *, actions, metadata_token, identity, previous, registration, creation_run_id=None
):
    """Return a complete immutable receipt only for a reviewed closed footprint."""
    body_limit = (
        registration.get("schema_version") == "quarantine-body-limit-activation-v2"
    )
    contract = normalize_tail_contract(
        registration.get("prepublication" if body_limit else "published_tail")
    )
    profile = failed_profile(contract)
    if (contract["schema_version"] == BODY_CONTRACT) != body_limit:
        raise ReviewInputError("quarantine execution class conflicts with registration")
    old = ApiRunAccounting.from_document(previous.api_run_accounting)
    tx = previous.transaction
    if (
        tx is None
        or tx.phase != ("publication_failed" if body_limit else "publication_succeeded")
        or old.terminal_transaction is not None
        or old.action is not None
        or old.analysis_abort is not None
        or old.owner.run_attempt != 1
        or old.owner.job != "hosted"
        or old.owner.generation != 0
        or previous.reservation_id is not None
        or previous.reserved_slot is not None
        or previous.operator_paused
        or previous.rescan_state is not None
        or previous.quarantine_lineage is not None
        or tx.head_sha == registration["head_sha"]
        and not (profile is not None and profile["same_head"])
    ):
        raise ReviewInputError("published-tail predecessor is unsupported")
    full = next(
        i
        for i in contract["executions"]
        if i["route"] in {"published-review", "failed-review"}
    )
    if (
        full["run_id"] != old.identity.run_id
        or full["configuration"] != asdict(old.configuration)
        or full["accounting_snapshot"] != previous.api_run_accounting
    ):
        raise ReviewInputError("published-tail original owner conflicts")
    qualification = BROKER_QUALIFICATIONS.get(contract["broker_qualification_sha256"])
    if qualification is None:
        raise ReviewInputError(
            "published-tail historical broker and late-growth qualification is absent"
        )
    if (
        hashlib.sha256(canonical_bytes(qualification)).hexdigest()
        != contract["broker_qualification_sha256"]
    ):
        raise ReviewInputError(
            "published-tail installed qualification digest conflicts"
        )
    from .quarantine_tail_provenance import validate_provenance

    validate_provenance(qualification, contract)
    # The catalog is source-owned qualification, not an operator-supplied bound.
    if (
        qualification.get("contract") != contract["schema_version"]
        or qualification.get("executor_sha") != contract["executor_sha"]
    ):
        raise ReviewInputError("published-tail broker source contract conflicts")
    creation_run_id = (
        int(actions.command_run_id) if creation_run_id is None else creation_run_id
    )
    creator = actions._get(identity, f"/actions/runs/{creation_run_id}")
    if creator.get("id") != creation_run_id or type(creator.get("id")) is not int:
        raise ReviewInputError("published-tail creation census fence is unavailable")
    cutoff = creator.get("created_at")
    if _stamp(cutoff) <= _stamp(contract["census_started_at"]):
        raise ReviewInputError("published-tail census interval conflicts")
    query = "created=" + quote(contract["census_started_at"] + ".." + cutoff, safe="")
    refusals = qualification.get("prewrite_refusals", [])
    if not isinstance(refusals, list):
        raise ReviewInputError(
            "published-tail pre-write refusal qualification is absent"
        )
    refusal_ids = {i["run_id"] for i in refusals}

    def projection(run):
        return {
            key: run.get(key)
            for key in (
                "id",
                "repository",
                "run_attempt",
                "status",
                "conclusion",
                "head_sha",
                "path",
                "workflow_id",
                "event",
                "referenced_workflows",
                "created_at",
                "updated_at",
            )
        }

    def census():
        runs = actions._runs(identity, query)
        relevant = []
        for run in runs:
            if run.get("path") != CALLER_PATH:
                continue
            pulls = run.get("pull_requests")
            if (
                isinstance(pulls, list)
                and pulls
                and all(
                    isinstance(p, dict)
                    and type(p.get("number")) is int
                    and p["number"] != identity.pull_request
                    for p in pulls
                )
            ):
                continue
            relevant.append(run)
        ids = [r.get("id") for r in relevant]
        if (
            len(ids) != len(set(ids))
            or set(ids) != {i["run_id"] for i in contract["executions"]} | refusal_ids
        ):
            raise ReviewInputError("published-tail complete execution census conflicts")
        return {r["id"]: projection(r) for r in relevant}

    observed = census()
    executions = []
    for item in contract["executions"]:
        prior = ApiRunActionsEvidence(
            actions.http, token=actions.token, command_run_id=str(item["run_id"])
        )
        # The complete authenticated opening census already contains this
        # positive run identity. Reuse its scoped fields, then take a fresh
        # closing detail read after the job census; no allowance is refreshed.
        run = observed[item["run_id"]]
        repo = run.get("repository")
        if (
            projection(run) != observed[item["run_id"]]
            or type(run.get("run_attempt")) is not int
            or run["run_attempt"] != 1
            or run.get("status") != "completed"
            or run.get("conclusion") != item["conclusion"]
            or run.get("head_sha") != tx.head_sha
            or run.get("path") != CALLER_PATH
            or run.get("workflow_id") != contract["workflow_id"]
            or type(run.get("id")) is not int
            or run["id"] != item["run_id"]
            or not isinstance(repo, dict)
            or type(repo.get("id")) is not int
            or repo.get("id") != identity.repository_id
            or repo.get("full_name") != identity.repository
            or run.get("event")
            != (
                "pull_request"
                if item["route"] in {"published-review", "failed-review"}
                else "pull_request_review_comment"
            )
            or run.get("referenced_workflows")
            != [
                {
                    "sha": contract["executor_sha"],
                    "path": WORKFLOW_PATH,
                    "ref": "refs/tags/v5",
                }
            ]
        ):
            raise ReviewInputError("published-tail execution has not ceased or changed")
        if _stamp(run.get("created_at")) < _stamp(contract["census_started_at"]):
            raise ReviewInputError("published-tail census omits the original start")
        jobs = prior._jobs(identity, 1)
        coverage = qualification["broker"]["routing_intervals"]
        if _stamp(run["created_at"]) < _stamp(coverage[0]["starts_at"]) or any(
            _stamp(j.get("completed_at")) > _stamp(coverage[-1]["ends_at"])
            for j in jobs
        ):
            raise ReviewInputError(
                "published-tail execution lies outside historical routing coverage"
            )
        expected = {
            "resolve-trigger",
            "review-or-reply / bootstrap",
            "review-or-reply / authoritative-preflight",
        }
        if len(jobs) != 6 or {j["name"] for j in jobs} != expected | {
            "review-or-reply / " + n for n in ("hosted", "local", "command")
        }:
            raise ReviewInputError("published-tail writer job census is incomplete")
        for job in jobs:
            conclusion = (
                item["conclusion"]
                if job["name"] == "review-or-reply / hosted"
                else "success"
                if job["name"] in expected
                else "skipped"
            )
            if job.get("status") != "completed" or job.get("conclusion") != conclusion:
                raise ReviewInputError("published-tail writer job is unconfirmed")
            if _stamp(job.get("completed_at")) > _stamp(cutoff):
                raise ReviewInputError(
                    "published-tail writer outlived the creation fence"
                )
            if (
                job["name"] == "review-or-reply / hosted"
                and job["id"] != item["job_id"]
            ):
                raise ReviewInputError("published-tail owning job conflicts")
        if projection(prior._get(identity, f"/actions/runs/{item['run_id']}")) != run:
            raise ReviewInputError("published-tail execution changed during collection")
        executions.append(
            {
                **item,
                "writer_inventory_sha256": hashlib.sha256(
                    canonical_bytes(jobs)
                ).hexdigest(),
            }
        )
    for refused in refusals:
        # These exact exceptions are independently source-qualified with their
        # authenticated pre-write failure evidence in the installed catalog.
        prior = ApiRunActionsEvidence(
            actions.http, token=actions.token, command_run_id=str(refused["run_id"])
        )
        run = prior._get(identity, f"/actions/runs/{refused['run_id']}")
        jobs = prior._jobs(identity, 1)
        if (
            projection(run) != observed[refused["run_id"]]
            or run.get("status") != "completed"
            or run.get("conclusion") != "failure"
            or run.get("run_attempt") != 1
            or run.get("head_sha") != refused["head_sha"]
            or hashlib.sha256(canonical_bytes(jobs)).hexdigest()
            != refused["writer_inventory_sha256"]
            or prior._get(identity, f"/actions/runs/{refused['run_id']}") != run
        ):
            raise ReviewInputError("published-tail pre-write refusal execution changed")
    for commit in dict.fromkeys((tx.base_sha, tx.head_sha)):
        status, source = actions.http.request(
            "GET",
            actions.http.repository_path(
                identity.repository, f"/contents/{CALLER_PATH}?ref={commit}"
            ),
            token=metadata_token,
        )
        if (
            status != 200
            or not isinstance(source, dict)
            or source.get("type") != "file"
            or source.get("sha") != contract["caller_blob"]
        ):
            raise ReviewInputError("published-tail caller source is unrecognized")
    if census() != observed:
        raise ReviewInputError("published-tail closing execution census changed")
    receipt = {
        "schema_version": "quarantine-body-limit-isolation-v1"
        if body_limit
        else "quarantine-published-tail-isolation-v1",
        "contract": contract["schema_version"],
        "disposition": "unresolved-isolated",
        "predecessor_record_sha256": previous.record_sha256,
        "executions": executions,
        "broker_qualification_sha256": contract["broker_qualification_sha256"],
        "qualification": qualification,
        "census_cutoff_at": cutoff,
        "census_sha256": hashlib.sha256(canonical_bytes(observed)).hexdigest(),
        "old_head_sha": tx.head_sha,
        "predecessor_comment_id": registration["predecessor_comment_id"],
        "liabilities": [
            {
                "run_id": item["run_id"],
                "run_attempt": item["run_attempt"],
                "usage_status": "last-authenticated-snapshot"
                if item["accounting_snapshot"] is not None
                else "unknown",
                "api_requests_upper_bound": item["configuration"]["limit"],
                "provider_calls_upper_bound": 32,
                "bounds_include_observed_usage": True,
                "settled": False,
                "allowance_transferred": 0,
            }
            for item in contract["executions"]
        ],
    }
    late_growth(receipt)
    return receipt

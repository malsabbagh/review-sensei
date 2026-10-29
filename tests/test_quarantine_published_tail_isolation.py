"""Five ceased executions remain unresolved under a closed isolation proof."""

import hashlib
import unittest
from copy import deepcopy
from dataclasses import replace
from unittest.mock import patch

from fake_github_http import json_response, make_http
from review_sensei_github.api_run_owner import ApiRunActionsEvidence
from review_sensei_github.quarantine_published_tail import (
    BROKER_QUALIFICATIONS,
    CALLER_BLOB,
    CONTRACT,
    EXECUTOR,
    authenticate_tail_contract,
    execution_manifest_digest,
    normalize_tail_contract,
)
from test_quarantine_activation import registration
from test_quarantine_isolation import IDENTITY, NEW_HEAD, jobs, predecessor, run

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.errors import ReviewInputError


def contract_fixture():
    previous, _ = predecessor()
    previous = previous.evolve(
        transaction=replace(previous.transaction, phase="publication_succeeded")
    )
    config = previous.api_run_accounting["configuration"]
    entries, runs, inventories = [], [], []
    for i in range(5):
        conclusion = "success" if i == 0 else "failure" if i == 4 else "cancelled"
        source = (
            None
            if i == 0
            else {
                "kind": "review_comment",
                "id": 100 + i,
                "root_id": 77,
                "updated_at": f"2026-10-28T00:00:0{i}Z",
                "body_sha256": str(i) * 64,
            }
        )
        entries.append(
            {
                "run_id": 800 + i,
                "run_attempt": 1,
                "job_id": 1 + i * 10,
                "route": "published-review" if i == 0 else "discussion",
                "conclusion": conclusion,
                "source": source,
                "configuration": config,
                "accounting_snapshot": previous.api_run_accounting if i == 0 else None,
            }
        )
        execution = run()
        execution.update(
            {
                "id": 800 + i,
                "conclusion": conclusion,
                "event": "pull_request" if i == 0 else "pull_request_review_comment",
                "created_at": "2026-10-28T00:00:00Z",
            }
        )
        execution["referenced_workflows"][0]["sha"] = EXECUTOR
        runs.append(execution)
        inventory = jobs()
        for job in inventory["jobs"]:
            job["run_id"] = 800 + i
            job["id"] += i * 10
            if job["name"] == "review-or-reply / hosted":
                job["conclusion"] = conclusion
        inventories.append(inventory)
    manifest = {
        "schema_version": CONTRACT,
        "executor_sha": EXECUTOR,
        "caller_blob": CALLER_BLOB,
        "workflow_id": 700,
        "census_started_at": "2026-10-28T00:00:00Z",
        "executions": entries,
        "broker_qualification_sha256": "a" * 64,
    }
    scope = registration()
    scope.update({"head_sha": NEW_HEAD, "published_tail": manifest})
    # This synthetic qualification replaces only the external historical
    # deployment/call-graph proof boundary; it makes no production-bound claim.
    from review_sensei_github.quarantine_tail_provenance import SURFACES

    qualified = {
        "schema_version": "quarantine-published-tail-provenance-v1",
        "contract": CONTRACT,
        "executor_sha": EXECUTOR,
        "execution_manifest_sha256": execution_manifest_digest(manifest),
        "broker": {
            "account": "synthetic",
            "service": "synthetic",
            "environment": "synthetic",
            "endpoints": ["/github/token", "/github/session-grant"],
            "native_contract": "no-independent-native-write-continuation-v1",
            "bindings_sha256": "b" * 64,
            "readback_sha256s": ["c" * 64],
            "routing_intervals": [
                {
                    "starts_at": "2026-10-27T00:00:00Z",
                    "ends_at": "2026-10-29T00:00:00Z",
                    "versions": [
                        {
                            "deployment_id": "synthetic",
                            "version_id": "synthetic",
                            "bundle_sha256": "d" * 64,
                            "source_commit": "e" * 40,
                            "build_sha256": "f" * 64,
                        }
                    ],
                }
            ],
        },
        "exposure": {
            "source_contract_sha256": "f" * 64,
            "surfaces": {
                surface: {
                    "new_objects": 9
                    if surface == "evidence_parts"
                    else 4
                    if surface == "issue_comments"
                    else 73
                    if surface == "inline_replies"
                    else 8
                    if surface == "reviews"
                    else 10
                    if surface == "checks"
                    else 4
                    if surface == "reactions"
                    else 0,
                    "new_wire_bytes": 9 * 32768
                    if surface == "evidence_parts"
                    else 4 * 60000
                    if surface == "issue_comments"
                    else 73 * 16384
                    if surface == "inline_replies"
                    else 8 * 65536
                    if surface == "reviews"
                    else 10 * 65536
                    if surface == "checks"
                    else 4096
                    if surface == "reactions"
                    else 0,
                    "positive_update_bytes": 65536 + 4 * 60000
                    if surface == "issue_comments"
                    else 4096
                    if surface == "threads"
                    else 0,
                    "max_object_wire_bytes": 32768
                    if surface == "evidence_parts"
                    else 65536
                    if surface in {"issue_comments", "reviews", "checks"}
                    else 16384
                    if surface == "inline_replies"
                    else 4096
                    if surface in {"threads", "reactions"}
                    else 0,
                }
                for surface in SURFACES
            },
        },
        "prewrite_refusals": [],
    }
    manifest["broker_qualification_sha256"] = hashlib.sha256(
        canonical_bytes(qualified)
    ).hexdigest()
    return previous, scope, runs, inventories, qualified


def responses(runs, inventories, *, closing=None):
    values = [
        json_response({"id": 900, "created_at": "2026-10-28T00:01:00Z"}),
        json_response({"workflow_runs": runs, "total_count": len(runs)}),
    ]
    for execution, inventory in zip(runs, inventories, strict=True):
        values.extend([json_response(inventory), json_response(execution)])
    values.extend(json_response({"type": "file", "sha": CALLER_BLOB}) for _ in range(2))
    values.append(
        json_response(
            {
                "workflow_runs": runs if closing is None else closing,
                "total_count": len(runs if closing is None else closing),
            }
        )
    )
    return values


class PublishedTailIsolationTests(unittest.TestCase):
    def authenticate(
        self, *, runs=None, inventories=None, qualification=True, closing=None
    ):
        previous, scope, defaults, job_defaults, qualified = contract_fixture()
        runs = defaults if runs is None else runs
        inventories = job_defaults if inventories is None else inventories
        http, calls = make_http(responses(runs, inventories, closing=closing))
        before = deepcopy(previous.to_dict())
        with patch.dict(
            BROKER_QUALIFICATIONS,
            {scope["published_tail"]["broker_qualification_sha256"]: qualified}
            if qualification
            else {},
            clear=True,
        ):
            receipt = authenticate_tail_contract(
                actions=ApiRunActionsEvidence(
                    http, token="synthetic-actions", command_run_id="900"
                ),
                metadata_token="synthetic-metadata",
                identity=IDENTITY,
                previous=previous,
                registration=scope,
            )
        self.assertEqual(previous.to_dict(), before)
        self.assertTrue(all(request[0] == "GET" for request in calls))
        return receipt

    def test_all_five_remain_uncertain_with_separate_inclusive_liability_bounds(self):
        receipt = self.authenticate()
        self.assertEqual(len(receipt["executions"]), 5)
        self.assertEqual(len(receipt["liabilities"]), 5)
        self.assertEqual(
            [i["usage_status"] for i in receipt["liabilities"]],
            ["last-authenticated-snapshot"] + ["unknown"] * 4,
        )
        self.assertTrue(
            all(
                not i["settled"] and i["allowance_transferred"] == 0
                for i in receipt["liabilities"]
            )
        )
        self.assertEqual(
            len({i["source"]["updated_at"] for i in receipt["executions"][1:]}), 4
        )

    def test_absent_historical_broker_proof_cannot_be_supplied_by_registration(self):
        with self.assertRaisesRegex(ReviewInputError, "historical broker"):
            self.authenticate(qualification=False)

    def test_missing_extra_rerun_and_closing_census_changes_refuse(self):
        _, scope, runs, inventories, _ = contract_fixture()
        for change in ("missing", "extra", "rerun", "active", "source", "job"):
            altered, job_values = deepcopy(runs), deepcopy(inventories)
            if change == "missing":
                altered.pop()
                job_values.pop()
            elif change == "extra":
                altered.append({**runs[0], "id": 899})
                job_values.append(inventories[0])
            elif change == "rerun":
                altered[2]["run_attempt"] = 2
            elif change == "active":
                altered[2]["status"] = "in_progress"
            elif change == "source":
                altered[2]["referenced_workflows"][0]["sha"] = "f" * 40
            else:
                job_values[2]["jobs"].pop()
                job_values[2]["total_count"] -= 1
            with self.subTest(change=change), self.assertRaises(ReviewInputError):
                self.authenticate(runs=altered, inventories=job_values)
        with self.assertRaises(ReviewInputError):
            self.authenticate(closing=runs[:-1])
        malformed = deepcopy(scope["published_tail"])
        malformed["executions"][1]["source"] = malformed["executions"][2]["source"]
        # Different executions may share a source, but none may omit its version.
        malformed["executions"][1]["source"].pop("updated_at")
        with self.assertRaises(ReviewInputError):
            normalize_tail_contract(malformed)

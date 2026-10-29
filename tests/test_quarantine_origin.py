"""Complete origin authentication through real codecs and synthetic external IO."""

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from fake_github_http import json_response
from review_sensei_github.api_run_host import review_startup_plan
from review_sensei_github.quarantine_evidence import (
    CurrentReadScope,
    reconstruct_failed_source,
)
from review_sensei_github.quarantine_isolation import authenticate_isolated_predecessor
from review_sensei_github.quarantine_origin import (
    QuarantineOriginReader,
    encode_predecessor,
    origin_binding,
    read_predecessor,
    source_binding,
)
from review_sensei_github.review_run import StartupRunLedger
from review_sensei_github.session_ledger import parse_session_comment
from test_quarantine_activation import registered_resource
from test_quarantine_evidence import FailedPublicationFixture, original_objects
from test_quarantine_isolation import jobs as qualified_jobs
from test_quarantine_isolation import run as qualified_run

from review_sensei.api_work import (
    ApiExecutionOwner,
    ApiRunIdentity,
    FrozenApiConfiguration,
    initial_accounting,
)
from review_sensei.bounded_evidence import (
    canonical_bytes,
    evidence_manifest,
    inline_manifest,
    partition_evidence,
)
from review_sensei.errors import ReviewInputError
from review_sensei.quarantine_assessments import origin_scope
from review_sensei.quarantine_lineage import lineage_scope
from review_sensei.quarantine_state import assessment_cursor


class OriginFixture(FailedPublicationFixture):
    source_previous = None

    def external(self, request, timeout):
        target = urlsplit(request.full_url)
        path = target.path
        old = self.source_previous
        if request.method == "GET" and path.startswith(
            "/repos/owner/repo/issues/comments/3"
        ):
            identifier = int(path.rsplit("/", 1)[-1])
            if identifier not in self.extra_comments:
                self.trace.append((self.attempt, request.method, path))
                return json_response({"message": "Not Found"}, 404)
        if old is not None and (
            "/actions/runs/" in path or "/contents/.github/workflows/" in path
        ):
            self.trace.append((self.attempt, request.method, path))
            if "/contents/" in path:
                sha = (
                    "f" * 40
                    if path.endswith("review-sensei-quarantine.yml")
                    else "dbc745b3b3471cf2d9861e9a84774e34eb0d6e8b"
                )
                return json_response(
                    {"type": "file", "sha": sha, "path": path.split("/contents/", 1)[1]}
                )
            if "/actions/runs/900/attempts/1" in path:
                return json_response(
                    {
                        "id": 900,
                        "repository": {"id": 123, "full_name": "owner/repo"},
                        "workflow_id": 700,
                        "run_number": 1,
                        "run_attempt": 1,
                        "event": "workflow_dispatch",
                        "path": ".github/workflows/review-sensei-quarantine.yml",
                        "head_sha": self.policy,
                        "status": "completed",
                        "actor": {"login": "alice", "type": "User", "id": 99},
                        "triggering_actor": {
                            "login": "alice",
                            "type": "User",
                            "id": 99,
                        },
                    }
                )
            if path.endswith("/jobs"):
                value = qualified_jobs()
                for job in value["jobs"]:
                    job["run_id"] = old.api_run_accounting["identity"]["run_id"]
                return json_response(value)
            value = qualified_run()
            value["id"] = old.api_run_accounting["identity"]["run_id"]
            value["head_sha"] = old.transaction.head_sha
            return json_response(value)
        return super().external(request, timeout)

    def seed(self, document, binding, count):
        parts = partition_evidence(document, binding=binding, item_count=count)
        ids = []
        for part in parts:
            identifier = 3_000_000 + len(self.extra_comments)
            self.extra_comments[identifier] = {
                "id": identifier,
                "body": "ReviewSensei immutable evidence part v1\n```json\n"
                + canonical_bytes(part).decode()
                + "\n```\n<!-- reviewsensei:evidence-part:v1 -->",
                "user": {"id": 999, "login": "reviewsensei[bot]", "type": "Bot"},
                "issue_url": "https://api.github.test/repos/owner/repo/issues/7",
            }
            ids.append(str(identifier))
        return evidence_manifest(
            document,
            binding=binding,
            item_count=count,
            parts=parts,
            storage_ids=tuple(ids),
        )


class QuarantineOriginTests(unittest.TestCase):
    def test_complete_registered_origin_is_authenticated_and_missing_or_corrupt_carriers_refuse(
        self,
    ):
        from review_sensei_github.api_run_owner import ApiRunActionsEvidence

        with tempfile.TemporaryDirectory() as directory:
            fixture = OriginFixture(directory)
            original_objects(fixture)
            fixture.provider_comments = [
                {
                    "path": "*",
                    "line": None,
                    "side": "FILE",
                    "body": "An original unknown-path assessment.",
                    "severity": "high",
                    "blocking": True,
                }
            ]
            self.assertEqual(fixture.invoke(), 1, fixture.error)
            old = parse_session_comment(
                fixture.comment["body"], identity=fixture.selected.identity
            )
            fixture.next_run()
            self.assertEqual(fixture.invoke(), 1, fixture.error)
            self.assertIn("not terminal", fixture.error)
            native = StartupRunLedger(
                fixture.http,
                caller=fixture.accounted,
                token="synthetic-bot",
                app_slug="reviewsensei[bot]",
                reservation_owner={
                    "run_id": fixture.run_id,
                    "head_sha": fixture.live_head,
                },
            )
            self.assertEqual(native.load(old.identity()).record, old)
            scope = CurrentReadScope(
                native,
                old.identity(),
                fixture.live_base,
                fixture.live_head,
                "synthetic-workflow-read",
            )
            fixture.source_previous = old
            current = Path.cwd()
            try:
                os.chdir(fixture.root)
                source, inventory, result = reconstruct_failed_source(
                    ledger=native, previous=old, current_scope=scope
                )
                actions = ApiRunActionsEvidence(
                    fixture.http,
                    token="synthetic-actions",
                    command_run_id=str(fixture.run_id),
                )
                isolated = authenticate_isolated_predecessor(
                    actions=actions,
                    metadata_token="synthetic-workflow-read",
                    identity=old.identity(),
                    previous=old,
                    retained_result=result,
                    predecessor_comment_id=1_000_000,
                    current_head=fixture.live_head,
                )
                registration = {
                    "schema_version": "quarantine-activation-v1",
                    "repository": "owner/repo",
                    "repository_id": 123,
                    "pull_request": 7,
                    "predecessor_comment_id": 1_000_000,
                    "predecessor_record_sha256": old.record_sha256,
                    "transaction_id": old.transaction.transaction_id,
                    "head_sha": fixture.live_head,
                    "base_sha": fixture.live_base,
                    "workflow_id": 700,
                    "workflow_path": ".github/workflows/review-sensei-quarantine.yml",
                    "workflow_blob": "f" * 40,
                    "run_number": 1,
                }
                source_manifest = fixture.seed(
                    source, source_binding(old, "github-bot:999"), 1
                )
                inventory_manifest = fixture.seed(
                    inventory,
                    source_binding(old, "github-bot:999"),
                    len(inventory["assessments"]),
                )
                origin = {
                    "schema_version": "quarantine-assessment-origin-v1",
                    "kind": "retained-result",
                    "source_manifest": source_manifest,
                    "inventory_manifest": inventory_manifest,
                }
                origin["scope_sha256"] = origin_scope(origin)
                archive = {
                    "schema_version": "quarantine-origin-v1",
                    "predecessor_comment_id": 1_000_000,
                    "predecessor_record": encode_predecessor(old),
                    "predecessor_record_representation": "zlib-json-v1",
                    "creation_run_id": 900,
                    "creation_policy_commit": fixture.policy,
                    "creation_configuration_digest": hashlib.sha256(
                        fixture.config_bytes
                    ).hexdigest(),
                    "registration_sha256": hashlib.sha256(
                        canonical_bytes(registration)
                    ).hexdigest(),
                    "target_isolation": isolated.receipt,
                    "assessment_origins": {
                        "schema_version": "quarantine-assessment-origins-v1",
                        "origins": [origin],
                    },
                }
                binding = origin_binding(
                    old.identity(),
                    registration,
                    old,
                    producer="github-bot:999",
                    configuration_digest=archive["creation_configuration_digest"],
                )
                lineage = {
                    "schema_version": "quarantine-lineage-v1",
                    "predecessor_comment_id": 1_000_000,
                    "predecessor_record_sha256": old.record_sha256,
                    "creation_run_id": 900,
                    "registration_sha256": archive["registration_sha256"],
                    "origin_count": 1,
                    "origin_manifest": fixture.seed(archive, binding, 1),
                }
                lineage["scope_sha256"] = lineage_scope(lineage)
                lineage["assessment_state_manifest"] = inline_manifest(
                    assessment_cursor(scope=lineage["scope_sha256"]),
                    binding={**binding, "policy_digest": lineage["scope_sha256"]},
                    item_count=0,
                )
                state = initial_accounting(
                    ApiRunIdentity(123, 900),
                    FrozenApiConfiguration(
                        fixture.policy, archive["creation_configuration_digest"], 512
                    ),
                    ApiExecutionOwner(1, "hosted", 0),
                    review_startup_plan("hosted"),
                    prior_nonstartup_liability_upper_bound=0,
                )
                record = old.quarantine_api_accounting(state.to_document(), lineage)
                reader = QuarantineOriginReader(
                    metadata_token="synthetic-workflow-read",
                    actions_token="synthetic-actions",
                    current_scope=scope,
                )
                before = len(fixture.trace)
                with registered_resource([registration]):
                    from contextlib import contextmanager
                    from unittest.mock import patch

                    original_window = CurrentReadScope.read_only_window
                    original_head = fixture.live_head

                    @contextmanager
                    def closing_head_change(current_scope):
                        try:
                            with original_window(current_scope):
                                yield
                                fixture.live_head = "e" * 40
                        finally:
                            fixture.live_head = original_head

                    with patch.object(
                        CurrentReadScope, "read_only_window", closing_head_change
                    ):
                        with self.assertRaisesRegex(
                            ReviewInputError, "current evidence scope changed"
                        ):
                            reader.read(native, old.identity(), record)
                    self.assertEqual(reader._authenticated_sources, {})
                    authenticated = reader.read(native, old.identity(), record)
                    self.assertEqual(
                        authenticated.pending,
                        (
                            (
                                origin["scope_sha256"],
                                inventory["assessments"][0]["instance"],
                            ),
                        ),
                    )
                    self.assertEqual(
                        read_predecessor(authenticated.archive).to_dict(), old.to_dict()
                    )
                    broken_id = int(source_manifest["parts"][0]["storage_id"])
                    original_carrier = fixture.extra_comments[broken_id]["body"]
                    fixture.extra_comments[broken_id]["body"] += "corrupt"
                    with self.assertRaises(ReviewInputError):
                        reader.read(native, old.identity(), record)
                    fixture.extra_comments[broken_id]["body"] = original_carrier
                    del fixture.extra_comments[broken_id]
                    with self.assertRaises(ReviewInputError):
                        reader.read(native, old.identity(), record)
                self.assertTrue(
                    all(method == "GET" for _, method, _ in fixture.trace[before:])
                )
                self.assertEqual(
                    parse_session_comment(
                        fixture.comment["body"], identity=old.identity()
                    ),
                    old,
                )
                self.assertIsNone(old.api_run_accounting["terminal_transaction"])
            finally:
                os.chdir(current)


class QuarantinePredecessorCodecTests(unittest.TestCase):
    def test_lossless_roundtrip_rejects_corrupt_truncated_and_expanding_snapshots(self):
        from copy import deepcopy

        from test_quarantine_isolation import predecessor

        from review_sensei.session import MAX_SESSION_COMMENT_BYTES

        old, _ = predecessor()
        archive = {
            "predecessor_record_representation": "zlib-json-v1",
            "predecessor_record": encode_predecessor(old),
        }
        self.assertEqual(read_predecessor(archive).to_dict(), old.to_dict())
        for case in (
            "representation",
            "truncated",
            "digest",
            "encoded",
            "expansion",
            "missing",
        ):
            with self.subTest(case=case):
                value = deepcopy(archive)
                if case == "representation":
                    value["predecessor_record_representation"] = "future"
                elif case == "truncated":
                    value["predecessor_record"]["data"] = value["predecessor_record"][
                        "data"
                    ][:-4]
                elif case == "digest":
                    value["predecessor_record"]["sha256"] = "f" * 64
                elif case == "encoded":
                    value["predecessor_record"]["data"] = "a" * (
                        MAX_SESSION_COMMENT_BYTES + 1
                    )
                elif case == "expansion":
                    value["predecessor_record"]["decoded_bytes"] = (
                        MAX_SESSION_COMMENT_BYTES + 1
                    )
                else:
                    del value["predecessor_record"]
                with self.assertRaises(ReviewInputError):
                    read_predecessor(value)

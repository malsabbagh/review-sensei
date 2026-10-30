"""Opaque archive and serialized fresh-target admission without old migration."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fake_github_http import json_response
from review_sensei_github import controlled_restart as restart
from review_sensei_github.http import GitHubHttp

from review_sensei.bounded_evidence import EvidenceReadBudget, canonical_bytes
from review_sensei.errors import ReviewInputError
from review_sensei.session import SessionIdentity

IDENTITY = SessionIdentity("owner/repo", 7, 123)
BODY = "unreadable old execution\n<!-- reviewsensei:session:v99 unknown -->"


def registration(**changes):
    row = dict(
        repository="owner/repo",
        repository_id=123,
        pull_request=7,
        base_sha="a" * 40,
        head_sha="b" * 40,
        policy_commit="c" * 40,
        workflow_sha="d" * 40,
        run_id="456",
        job="hosted",
        issuance_disabled_at=1000,
        retire_after=4600,
        old_runs=[123],
        old_comments=[
            dict(
                id=71,
                bytes=len(BODY.encode()),
                sha256=hashlib.sha256(BODY.encode()).hexdigest(),
            )
        ],
    )
    row.update(changes)
    row["request_id"] = hashlib.sha256(canonical_bytes(row)).hexdigest()
    return row


def document(rows):
    return canonical_bytes(
        dict(schema_version="controlled-restart-registry-v1", registrations=rows)
    )


class ArchiveFixture:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.trace = []
        self.active = False
        self.incomplete = False
        self.changed = False
        self.head_changed = False
        self.budget = EvidenceReadBudget()
        self.http = GitHubHttp(api_url="https://api.github.test", opener=self.open)
        self.ledger = SimpleNamespace(
            http=self.http,
            token="synthetic-read",
            app_slug="reviewsensei[bot]",
            evidence_budget=self.budget,
        )

    def open(self, request, timeout):
        from urllib.parse import urlsplit

        self.budget.consume()
        path = urlsplit(request.full_url).path
        self.trace.append((request.method, path))
        if path.endswith("/actions/runs/123"):
            return json_response(
                dict(
                    id=123,
                    repository=dict(id=123, full_name="owner/repo"),
                    status="in_progress" if self.active else "completed",
                    run_attempt=1,
                )
            )
        if path.endswith("/jobs"):
            return json_response(
                dict(
                    total_count=2 if self.incomplete else 1,
                    jobs=[dict(id=10, run_id=123, run_attempt=1, status="completed")],
                )
            )
        if path.endswith("/issues/comments/71"):
            return json_response(
                dict(
                    id=71,
                    body=BODY + ("changed" if self.changed else ""),
                    user=dict(type="Bot", login="reviewsensei[bot]", id=999),
                    issue_url="https://api.github.test/repos/owner/repo/issues/7",
                )
            )
        if path.endswith("/pulls/7"):
            return json_response(
                dict(
                    state="open",
                    base=dict(sha="a" * 40),
                    head=dict(sha=("e" if self.head_changed else "b") * 40),
                )
            )
        raise AssertionError("unexpected physical dispatch: " + path)

    def prepare(self):
        row = registration()
        env = dict(
            GITHUB_EVENT_NAME="workflow_dispatch",
            GITHUB_RUN_ID="456",
            GITHUB_RUN_ATTEMPT="1",
            REVIEW_SENSEI_WORKFLOW_SHA="d" * 40,
        )
        with (
            patch.object(restart, "registrations", return_value=[row]),
            patch.dict(os.environ, env),
        ):
            return restart.prepare_admission(
                ledger=self.ledger,
                identity=IDENTITY,
                registration=row,
                actions_token="synthetic-actions",
                directory=self.directory,
            )


class ControlledRestartTests(unittest.TestCase):
    def test_installed_registry_is_empty_and_cannot_activate(self):
        self.assertEqual(restart.registrations(), [])
        args = dict(
            repository="owner/repo",
            repository_id=123,
            pull_request=7,
            purpose="review",
            policy_commit="c" * 40,
            base_sha="a" * 40,
            head_sha="b" * 40,
        )
        self.assertIsNone(restart.entry_registration(**args, request_id=None))
        with self.assertRaisesRegex(ReviewInputError, "not registered"):
            restart.entry_registration(**args, request_id="f" * 64)

    def test_closed_registry_rejects_reassignment_unknown_fields_and_bounds(self):
        row = registration()
        self.assertEqual(restart.read_registry(document([row])), [row])
        malformed = [
            document([row, row]),
            document([{**row, "extra": True}]),
            document([registration(old_runs=[456])]),
            document([registration(retire_after=4599)]),
            document(
                [registration(old_comments=[{**row["old_comments"][0], "bytes": True}])]
            ),
            b"x" * 32769,
        ]
        for raw in malformed:
            with self.subTest(raw=raw[:80]), self.assertRaises(ReviewInputError):
                restart.read_registry(raw)

    def test_ordinary_entry_is_held_and_only_exact_designation_admits(self):
        row = registration()
        args = dict(
            repository="owner/repo",
            repository_id=123,
            pull_request=7,
            purpose="review",
            policy_commit="c" * 40,
            base_sha="a" * 40,
            head_sha="b" * 40,
            request_id=row["request_id"],
        )
        env = dict(
            GITHUB_EVENT_NAME="workflow_dispatch",
            GITHUB_RUN_ID="456",
            GITHUB_RUN_ATTEMPT="1",
            REVIEW_SENSEI_WORKFLOW_SHA="d" * 40,
        )
        with (
            patch.object(restart, "registrations", return_value=[row]),
            patch.dict(os.environ, env),
            patch.object(restart.time, "time", return_value=5000),
        ):
            self.assertEqual(restart.entry_registration(**args), row)
            for change in (
                {"request_id": None},
                {"purpose": "command"},
                {"head_sha": "e" * 40},
                {"base_sha": "e" * 40},
                {"policy_commit": "e" * 40},
                {"job": "local"},
            ):
                with self.subTest(change=change), self.assertRaises(ReviewInputError):
                    restart.entry_registration(**{**args, **change})
            for key, value in [
                ("GITHUB_RUN_ID", "789"),
                ("GITHUB_RUN_ATTEMPT", "2"),
                ("GITHUB_EVENT_NAME", "pull_request"),
            ]:
                with (
                    patch.dict(os.environ, {key: value}),
                    self.assertRaises(ReviewInputError),
                ):
                    restart.entry_registration(**args)
            with (
                patch.object(restart.time, "time", return_value=4599),
                self.assertRaisesRegex(ReviewInputError, "expiry"),
            ):
                restart.entry_registration(**args)

    def test_archive_preserves_opaque_bytes_without_loading_old_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ArchiveFixture(directory)
            with patch(
                "review_sensei.session.SessionRecord.from_dict",
                side_effect=AssertionError("no migration"),
            ):
                proof = fixture.prepare()
            archive = json.loads(
                (fixture.directory / "abandoned-history.json").read_bytes()
            )
            self.assertEqual(archive["comments"][0]["body"], BODY)
            self.assertEqual(archive["status"], "abandoned")
            self.assertEqual(archive["historical_cost"], "unknown")
            self.assertEqual(archive["historical_effects"], "unsettled")
            self.assertEqual(len(fixture.trace), 3)
            self.assertTrue(all(method == "GET" for method, _ in fixture.trace))
            self.assertIn(
                hashlib.sha256(
                    (fixture.directory / "abandoned-history.json").read_bytes()
                ).hexdigest(),
                proof.marker,
            )
            with self.assertRaisesRegex(ReviewInputError, "consumed"):
                fixture.prepare()

    def test_active_incomplete_or_changed_predecessor_refuses_before_archive(self):
        for fault in ("active", "incomplete", "changed"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                fixture = ArchiveFixture(directory)
                setattr(fixture, fault, True)
                with self.assertRaises(ReviewInputError):
                    fixture.prepare()
                self.assertFalse(
                    (fixture.directory / "abandoned-history.json").exists()
                )
                self.assertTrue(all(method == "GET" for method, _ in fixture.trace))

    def test_forged_selection_proof_never_confers_authority(self):
        forged = restart.RestartAdmission(
            object(), IDENTITY, registration(), b"archive"
        )
        with self.assertRaisesRegex(ReviewInputError, "foreign"):
            forged.select(object(), IDENTITY, [])

    def test_late_old_updates_do_not_select_successor_or_replace_pinned_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ArchiveFixture(directory)
            proof = fixture.prepare()
            old = dict(
                id=71, body=BODY, user=dict(type="Bot", login="reviewsensei[bot]")
            )
            fresh = dict(
                id=72,
                body=proof.marker + "\nnew root",
                user=dict(type="Bot", login="reviewsensei[bot]"),
            )
            self.assertEqual(proof.select(fixture.ledger, IDENTITY, [old]), [])
            self.assertEqual(
                proof.select(fixture.ledger, IDENTITY, [old, fresh]), [fresh]
            )
            old["body"] = "late old write"
            self.assertEqual(
                proof.select(fixture.ledger, IDENTITY, [old, fresh]), [fresh]
            )
            with self.assertRaisesRegex(ReviewInputError, "changed"):
                proof.select(fixture.ledger, IDENTITY, [{**fresh, "id": 73}])
            with self.assertRaisesRegex(ReviewInputError, "absent"):
                proof.select(fixture.ledger, IDENTITY, [old])

    def test_concurrent_successors_and_missing_post_ack_refuse(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ArchiveFixture(directory)
            proof = fixture.prepare()
            fresh = dict(
                id=72,
                body=proof.marker + "\nnew root",
                user=dict(type="Bot", login="reviewsensei[bot]"),
            )
            with self.assertRaisesRegex(ReviewInputError, "competing"):
                proof.select(fixture.ledger, IDENTITY, [fresh, {**fresh, "id": 73}])
            proof._created = True
            with self.assertRaisesRegex(ReviewInputError, "uncertain"):
                proof.select(fixture.ledger, IDENTITY, [])

    def test_effect_guard_denies_old_targets_native_approval_findings_and_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ArchiveFixture(directory)
            guard = restart.RestartEffects(fixture.prepare())
            root = "/repos/owner/repo"
            for method, path, body in [
                ("PATCH", root + "/issues/comments/71", {}),
                ("POST", root + "/pulls/7/reviews", {"event": "APPROVE"}),
                (
                    "POST",
                    root + "/pulls/7/reviews",
                    {"event": "COMMENT", "comments": [{}]},
                ),
                ("POST", "/graphql", {"query": "mutation { resolveReviewThread }"}),
                ("DELETE", root + "/issues/comments/71", None),
            ]:
                before = len(fixture.trace)
                with self.subTest(path=path), self.assertRaises(ReviewInputError):
                    guard.before_request(method, path, body)
                self.assertEqual(len(fixture.trace), before)
            path = root + "/issues/7/comments"
            body = {"body": "new immutable evidence"}
            guard.before_request("POST", path, body)
            with self.assertRaisesRegex(ReviewInputError, "already attempted"):
                guard.before_request("POST", path, body)
            guard.after_response("POST", path, 500, {"id": 72})
            self.assertNotIn(root + "/issues/comments/72", guard.owned)
            guard.after_response("POST", path, 201, {"id": 72})
            self.assertIn(root + "/issues/comments/72", guard.owned)

    def test_changed_current_head_stops_fresh_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ArchiveFixture(directory)
            guard = restart.RestartEffects(fixture.prepare())
            fixture.head_changed = True
            with self.assertRaisesRegex(ReviewInputError, "head changed"):
                guard.before_request(
                    "POST", "/repos/owner/repo/issues/7/comments", {"body": "new"}
                )
            self.assertTrue(all(method == "GET" for method, _ in fixture.trace))

    def test_runtime_binding_rejects_unrelated_transport_owner_and_repeat_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ArchiveFixture(directory)
            proof = fixture.prepare()
            bound = []

            class Caller:
                accounting = SimpleNamespace(identity=SimpleNamespace(run_id=456))

                def _require_ledger(self, ledger, identity):
                    bound.append((ledger, identity))

            caller = Caller()
            fixture.ledger._startup_caller = caller
            for runtime in (
                SimpleNamespace(caller=object(), http=fixture.http),
                SimpleNamespace(caller=caller, http=object()),
            ):
                with self.assertRaisesRegex(ReviewInputError, "foreign"):
                    proof.bind_runtime(runtime)
            self.assertEqual(bound, [])
            runtime = SimpleNamespace(caller=caller, http=fixture.http)
            caller.accounting = SimpleNamespace(identity=SimpleNamespace(run_id=789))
            with self.assertRaisesRegex(ReviewInputError, "owner changed"):
                proof.bind_runtime(runtime)
            caller.accounting = SimpleNamespace(identity=SimpleNamespace(run_id=456))
            proof.bind_runtime(runtime)
            self.assertEqual(proof.select(runtime, IDENTITY, []), [])
            self.assertEqual(proof.select(fixture.ledger, IDENTITY, []), [])
            with self.assertRaisesRegex(ReviewInputError, "already bound"):
                proof.bind_runtime(runtime)
            with self.assertRaisesRegex(ReviewInputError, "foreign"):
                proof.select(
                    SimpleNamespace(caller=caller, http=fixture.http), IDENTITY, []
                )

    def test_restart_fence_funding_preserves_confirmation_and_terminal_reserves(self):
        from urllib.parse import urlsplit

        from review_sensei_github.api_run_plan import (
            TERMINAL_REQUESTS,
            WRITE_CONFIRMATION_TAIL,
        )
        from test_api_work_lifecycle import LifecycleFixture

        from review_sensei.errors import EvidenceBudgetExhausted

        with tempfile.TemporaryDirectory() as directory:
            fixture = LifecycleFixture(directory, limit=512)
            native, configuration, _, owner = fixture.prelude()
            caller = fixture.accounted
            caller.initialize_and_activate(native, IDENTITY, configuration, owner)
            native._startup_caller = caller
            native.evidence_budget = caller.budget_view
            row = registration(policy_commit=configuration.commit)
            old = dict(
                id=71,
                body=BODY,
                user=dict(type="Bot", login="reviewsensei[bot]", id=999),
                issue_url="https://api.github.test/repos/owner/repo/issues/7",
            )
            original = caller._opener

            def external(request, timeout):
                path = urlsplit(request.full_url).path
                if path.endswith("/actions/runs/123"):
                    fixture.trace.append((fixture.attempt, request.method, path))
                    return json_response(
                        dict(
                            id=123,
                            repository=dict(id=123, full_name="owner/repo"),
                            status="completed",
                            run_attempt=1,
                        )
                    )
                if path.endswith("/actions/runs/123/attempts/1/jobs"):
                    fixture.trace.append((fixture.attempt, request.method, path))
                    return json_response(
                        dict(
                            total_count=1,
                            jobs=[
                                dict(
                                    id=10, run_id=123, run_attempt=1, status="completed"
                                )
                            ],
                        )
                    )
                if urlsplit(request.full_url).path.endswith("/issues/comments/71"):
                    fixture.trace.append(
                        (
                            fixture.attempt,
                            request.method,
                            "/repos/owner/repo/issues/comments/71",
                        )
                    )
                    return json_response(old)
                return original(request, timeout=timeout)

            caller._opener = external
            before = len(caller.observations)
            with caller.mutation_operation("ordinary-profile") as allocation:
                self.assertEqual(
                    (allocation.capacity, allocation.write_tail),
                    (WRITE_CONFIRMATION_TAIL, WRITE_CONFIRMATION_TAIL),
                )
            self.assertEqual(len(caller.observations), before)
            env = dict(
                GITHUB_EVENT_NAME="workflow_dispatch",
                GITHUB_RUN_ID="456",
                GITHUB_RUN_ATTEMPT="1",
                REVIEW_SENSEI_WORKFLOW_SHA="d" * 40,
            )
            with (
                patch.object(restart, "registrations", return_value=[row]),
                patch.dict(os.environ, env),
            ):
                proof = restart.prepare_admission(
                    ledger=native,
                    identity=IDENTITY,
                    registration=row,
                    actions_token="synthetic-actions",
                    directory=Path(directory),
                )
            restart.install_admission(native, proof)
            with caller.mutation_operation("funded-profile") as allocation:
                self.assertEqual(
                    (allocation.capacity, allocation.write_tail),
                    (WRITE_CONFIRMATION_TAIL + 1, WRITE_CONFIRMATION_TAIL),
                )
            while caller.usable > WRITE_CONFIRMATION_TAIL:
                fixture.http.request(
                    "GET", "/repos/owner/repo/pulls/7", token="synthetic-read"
                )
            before = len(caller.observations)
            terminal_before = next(
                block
                for block in caller.accounting.blocks
                if block.block_id == "hosted-terminal"
            )
            with self.assertRaisesRegex(EvidenceBudgetExhausted, "unfunded"):
                with caller.mutation_operation("insufficient-prewrite-room"):
                    self.fail("unfunded operation must not yield")
            self.assertEqual(len(caller.observations), before)
            self.assertEqual(caller.usable, WRITE_CONFIRMATION_TAIL)
            self.assertEqual(
                next(
                    block
                    for block in caller.accounting.blocks
                    if block.block_id == "hosted-terminal"
                ),
                terminal_before,
            )
            self.assertEqual(terminal_before.undispatched, TERMINAL_REQUESTS)
            self.assertEqual(caller.accounting.configuration.limit, 512)
            with self.assertRaisesRegex(ReviewInputError, "foreign"):
                proof.prewrite_fence_requests(SimpleNamespace(run=caller.run))

    def test_transport_binding_is_one_shot_and_retains_original_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ArchiveFixture(directory)
            proof = fixture.prepare()
            restart.install_admission(fixture.ledger, proof)
            self.assertIs(restart.effect_guard(fixture.http).admission, proof)
            self.assertIs(fixture.ledger.evidence_budget, fixture.budget)
            self.assertFalse(
                restart.owns_target(
                    fixture.http, "/repos/owner/repo/issues/comments/71"
                )
            )
            with self.assertRaisesRegex(ReviewInputError, "already bound"):
                restart.install_admission(fixture.ledger, proof)


if __name__ == "__main__":
    unittest.main()


class ControlledRestartPublicEntryTests(unittest.TestCase):
    def test_same_head_incompatible_archive_gets_fresh_analysis_and_targets(self):
        from urllib.parse import urlsplit

        from review_sensei_github import cli, review_run
        from test_review_run import EntryFixture
        from test_review_transaction import HostedAnalysisCliTests

        with tempfile.TemporaryDirectory() as directory:
            fixture = EntryFixture(directory)
            fixture.event_name = "workflow_dispatch"
            row = registration(policy_commit=fixture.policy, workflow_sha="a" * 40)
            old = dict(
                id=71,
                body=BODY,
                user=dict(type="Bot", login="reviewsensei[bot]", id=999),
                issue_url="https://api.github.test/repos/owner/repo/issues/7",
            )
            original_external = fixture.external
            original_main = cli.main

            def external(request, timeout):
                path = urlsplit(request.full_url).path
                if path.endswith("/issues/comments/71"):
                    fixture.trace.append((fixture.attempt, request.method, path))
                    return json_response(old)
                response = original_external(request, timeout)
                if path.endswith("/issues/7/comments") and request.method == "GET":
                    return json_response([old, *json.loads(response.body)])
                return response

            fixture.external = external

            def main(argv):
                argv = ["restart-run", "--restart-id", row["request_id"], *argv[1:]]
                if "--diff" in argv:
                    index = argv.index("--diff")
                    del argv[index : index + 2]
                return original_main(argv)

            prepared = []

            def prepare_diff(**kwargs):
                prepared.append(kwargs)
                kwargs["output"].write_text(
                    HostedAnalysisCliTests.ANALYSIS_DIFF, encoding="utf-8", newline="\n"
                )

            with (
                patch.object(restart, "registrations", return_value=[row]),
                patch.object(cli, "main", side_effect=main),
                patch.object(review_run, "prepare_diff", side_effect=prepare_diff),
            ):
                code = fixture.invoke()
            self.assertEqual(code, 0, fixture.error)
            self.assertEqual(fixture.provider_calls, 1)
            self.assertEqual(
                fixture.selected.caller.accounting.configuration.limit, 512
            )
            self.assertIsNotNone(
                fixture.selected.caller.accounting.terminal_transaction
            )
            self.assertEqual(
                len(fixture.selected.caller.observations), len(fixture.trace)
            )
            self.assertEqual(len(prepared), 1)
            self.assertEqual(prepared[0]["base_ref"], row["base_sha"])
            self.assertEqual(prepared[0]["head_ref"], row["head_sha"])
            self.assertTrue(
                fixture.comment["body"].startswith(
                    "<!-- reviewsensei:controlled-restart:v1"
                )
            )
            self.assertEqual(old["body"], BODY)
            archive = json.loads((fixture.root / "abandoned-history.json").read_bytes())
            self.assertEqual(archive["comments"][0]["body"], BODY)
            self.assertTrue(fixture.reviews)
            self.assertFalse(
                any(item["state"] == "APPROVED" for item in fixture.reviews)
            )
            self.assertTrue(
                json.loads((fixture.root / "publication.json").read_text())[
                    "accounting_terminal"
                ]
            )
            self.assertFalse(
                any(
                    method == "PATCH" and path.endswith("/issues/comments/71")
                    for _, method, path in fixture.trace
                )
            )

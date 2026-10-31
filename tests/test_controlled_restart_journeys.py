"""Public serialized restart journeys; external services are synthetic only."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import test_review_transaction as transaction
from fake_github_http import json_response
from review_sensei_github import cli, review_run
from review_sensei_github import controlled_restart as restart
from test_controlled_restart import BODY, registration
from test_review_run import EntryFixture

from review_sensei.errors import ReviewInputError


class InjectedCrash(BaseException):
    """Model process death outside normal exception recovery."""


class RestartJourney:
    def __init__(self, directory, *, runner="hosted", body=BODY):
        self.fixture = EntryFixture(directory, runner_kind=runner)
        self.fixture.event_name = "workflow_dispatch"
        self.row = registration(
            policy_commit=self.fixture.policy,
            workflow_sha="a" * 40,
            job=runner,
            old_comments=[
                {
                    "id": 71,
                    "bytes": len(body.encode()),
                    "sha256": hashlib.sha256(body.encode()).hexdigest(),
                }
            ],
        )
        self.old = dict(
            id=71,
            body=body,
            user=dict(type="Bot", login="reviewsensei[bot]", id=999),
            issue_url="https://api.github.test/repos/owner/repo/issues/7",
        )
        self.external_hook = None
        self.original_external = self.fixture.external
        self.fixture.external = self.external
        self.actual_diff = False
        self.pass_diff = False

    def external(self, request, timeout):
        path = urlsplit(request.full_url).path
        if path.endswith("/issues/comments/71"):
            self.fixture.trace.append((self.fixture.attempt, request.method, path))
            return json_response(self.old)
        if self.external_hook is not None:
            response = self.external_hook(request, timeout, self.original_external)
        else:
            response = self.original_external(request, timeout)
        if path.endswith("/issues/7/comments") and request.method == "GET":
            return json_response([self.old, *json.loads(response.body)])
        return response

    def invoke(self, *, human=False):
        original = cli.main

        def main(argv):
            argv = ["restart-run", "--restart-id", self.row["request_id"], *argv[1:]]
            if not self.pass_diff:
                index = argv.index("--diff")
                del argv[index : index + 2]
            return original(argv)

        def diff(**kwargs):
            kwargs["output"].write_text(
                transaction.HostedAnalysisCliTests.ANALYSIS_DIFF,
                encoding="utf-8",
                newline="\n",
            )

        with ExitStack() as scopes:
            scopes.enter_context(
                patch.object(restart, "registrations", return_value=[self.row])
            )
            scopes.enter_context(patch.object(cli, "main", side_effect=main))
            if not self.actual_diff:
                scopes.enter_context(
                    patch.object(review_run, "prepare_diff", side_effect=diff)
                )
            return self.fixture.invoke(human=human)

    def archive(self):
        return json.loads((self.fixture.root / "abandoned-history.json").read_bytes())


class ControlledRestartLifecycleTests(unittest.TestCase):
    def assert_accounting(self, journey):
        fixture = journey.fixture
        self.assertEqual(len(fixture.accounted.observations), len(fixture.trace))
        if fixture.selected is not None:
            self.assertEqual(
                fixture.selected.caller.accounting.configuration.limit, 512
            )

    def test_clean_analysis_cannot_certify_opaque_historical_obligations(self):
        with tempfile.TemporaryDirectory() as directory:
            journey = RestartJourney(directory)
            self.assertEqual(journey.invoke(), 0, journey.fixture.error)
            self.assertEqual(journey.fixture.provider_calls, 1)
            completed = [
                c for c in journey.fixture.check_runs if c.get("status") == "completed"
            ]
            self.assertTrue(completed)
            self.assertEqual(completed[-1]["conclusion"], "action_required")
            self.assertIn("historical", completed[-1]["output"]["summary"].lower())
            self.assertEqual(journey.archive()["historical_effects"], "unsettled")
            self.assert_accounting(journey)

    def test_supplied_diff_refuses_before_consuming_the_designated_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            journey = RestartJourney(directory)
            journey.pass_diff = True
            self.assertNotEqual(journey.invoke(), 0)
            self.assertIn("freshly prepared", journey.fixture.error)
            self.assertEqual(journey.fixture.trace, [])
            self.assertFalse((journey.fixture.root / "abandoned-history.json").exists())
            self.assertIsNone(journey.fixture.comment)

    def test_published_unfinished_and_failed_publication_history_stay_opaque(self):
        for runner in ("hosted", "local"):
            for status in ("publication_succeeded", "publication_failed"):
                with (
                    self.subTest(runner=runner, status=status),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    body = json.dumps(
                        {
                            "schema_version": "unknown-old-execution",
                            "transaction": {"status": status},
                            "terminal_transaction": None,
                            "charges": "unknown",
                            "text": "old\r\n雪",
                        },
                        ensure_ascii=False,
                    )
                    journey = RestartJourney(directory, runner=runner, body=body)
                    self.assertEqual(journey.invoke(), 0, journey.fixture.error)
                    self.assertEqual(journey.fixture.provider_calls, 1)
                    self.assertEqual(
                        journey.archive()["comments"][0]["body"].encode(), body.encode()
                    )
                    self.assertEqual(journey.old["body"], body)
                    self.assertIsNotNone(
                        journey.fixture.selected.caller.accounting.terminal_transaction
                    )
                    self.assertFalse(
                        any(r["state"] == "APPROVED" for r in journey.fixture.reviews)
                    )
                    self.assert_accounting(journey)

    def test_valid_published_nonterminal_root_is_archived_without_successful_exit(self):
        from review_sensei_github.publication import ReviewPublisher
        from review_sensei_github.session_ledger import parse_session_comment
        from test_api_work_persistence import IDENTITY

        for runner in ("hosted", "local"):
            with (
                self.subTest(runner=runner),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                for name in ("prior", "ordinary", "restart"):
                    (root / name).mkdir()
                prior = EntryFixture(root / "prior", runner_kind=runner)
                with patch.object(
                    ReviewPublisher,
                    "_finish_thread_cleanup",
                    lambda _, value: replace(value, thread_resolution="unconfirmed"),
                ):
                    self.assertEqual(prior.invoke(human=True), 1, prior.error)
                old = copy.deepcopy(prior.comment)
                record = parse_session_comment(old["body"], identity=IDENTITY)
                self.assertEqual(record.transaction.phase, "publication_succeeded")
                self.assertIsNone(record.api_run_accounting["terminal_transaction"])
                self.assertTrue(prior.reviews)
                parts = copy.deepcopy(prior.extra_comments)
                for item in parts.values():
                    item["issue_url"] = (
                        "https://api.github.test/repos/owner/repo/issues/7"
                    )

                def failed_owner(request, timeout, original):
                    response = original(request, timeout)
                    path = urlsplit(request.full_url).path
                    if path == f"/repos/owner/repo/actions/runs/{prior.run_id}":
                        value = json.loads(response.read(512 * 1024 + 1))
                        value.update(
                            conclusion="failure", head_sha=record.transaction.head_sha
                        )
                        return json_response(value)
                    if path == (
                        f"/repos/owner/repo/actions/runs/{prior.run_id}/attempts/1/jobs"
                    ):
                        value = json.loads(response.read(512 * 1024 + 1))
                        for job in value["jobs"]:
                            job["conclusion"] = "failure"
                        return json_response(value)
                    return response

                # A new ordinary execution refuses this authentic old root.
                probe = EntryFixture(root / "ordinary", runner_kind=runner)
                probe.run_id = prior.run_id + 1
                probe.live_head = probe.requested_head = "d" * 40
                probe.comment = copy.deepcopy(old)
                original = probe.external
                probe.external = lambda request, timeout: failed_owner(
                    request, timeout, original
                )
                self.assertEqual(probe.invoke(human=True), 1)
                self.assertIn("successful review exit", probe.error)
                self.assertEqual(probe.provider_calls, 0)
                self.assertEqual(probe.comment, old)

                # Only a separately designated synthetic restart can proceed.
                journey = RestartJourney(
                    root / "restart", runner=runner, body=old["body"]
                )
                fixture = journey.fixture
                fixture.run_id = prior.run_id + 2
                fixture.live_head = fixture.requested_head = "d" * 40
                fixture.extra_comments = copy.deepcopy(parts)
                fixture.reviews = copy.deepcopy(prior.reviews)
                fixture.inline = copy.deepcopy(prior.inline)
                old_reviews = copy.deepcopy(fixture.reviews)
                old_inline = copy.deepcopy(fixture.inline)
                archived = [journey.old, *parts.values()]
                journey.row = registration(
                    policy_commit=fixture.policy,
                    workflow_sha="a" * 40,
                    base_sha=fixture.live_base,
                    head_sha=fixture.live_head,
                    run_id=str(fixture.run_id),
                    job=runner,
                    old_runs=[prior.run_id],
                    old_comments=[
                        {
                            "id": item["id"],
                            "bytes": len(item["body"].encode()),
                            "sha256": hashlib.sha256(item["body"].encode()).hexdigest(),
                        }
                        for item in archived
                    ],
                )
                journey.external_hook = failed_owner
                self.assertEqual(journey.invoke(), 0, fixture.error)
                self.assertEqual(fixture.provider_calls, 1)
                self.assertEqual(journey.archive()["historical_cost"], "unknown")
                self.assertEqual(journey.archive()["historical_effects"], "unsettled")
                self.assertEqual(
                    [
                        (item["id"], item["body"])
                        for item in journey.archive()["comments"]
                    ],
                    [(item["id"], item["body"]) for item in archived],
                )
                self.assertEqual(fixture.reviews[: len(old_reviews)], old_reviews)
                self.assertEqual(fixture.inline, old_inline)
                for identifier, item in parts.items():
                    self.assertEqual(fixture.extra_comments[identifier], item)
                self.assertEqual(journey.old["body"], old["body"])
                self.assertEqual(
                    parse_session_comment(journey.old["body"], identity=IDENTITY),
                    record,
                )
                self.assertNotEqual(fixture.comment["id"], journey.old["id"])
                # Exercise the public, offline reviewer handoff against the
                # actual old and successor publication bodies. Prior decisions
                # remain evidence; no current disposition or approval is minted.
                from contextlib import redirect_stdout
                from io import StringIO

                from review_sensei_github.publication import parse_finding_index

                old_review = old_reviews[-1]
                new_review = fixture.reviews[-1]
                old_index = parse_finding_index(
                    old_review["body"],
                    repository_id=123,
                    pull_request=7,
                    head_sha=old_review["commit_id"],
                )
                self.assertIsNotNone(old_index)
                notes = [
                    dict(
                        instance=item.instance,
                        decision="advisory-deferred",
                        reference="https://github.com/owner/repo/pull/7#issuecomment-321",
                        rationale="Synthetic prior maintainer judgment; revalidate against this successor.",
                    )
                    for item in old_index.entries
                ]
                inputs = root / "reviewer-inputs"
                inputs.mkdir()
                (inputs / "old.txt").write_text(old_review["body"], encoding="utf-8")
                (inputs / "current.txt").write_text(
                    new_review["body"], encoding="utf-8"
                )
                (inputs / "notes.json").write_text(json.dumps(notes), encoding="utf-8")
                observed = len(fixture.trace)
                output = StringIO()
                with redirect_stdout(output):
                    result = cli.main(
                        [
                            "restart-handoff",
                            "--archive",
                            str(fixture.root / "abandoned-history.json"),
                            "--prior-body",
                            str(inputs / "old.txt"),
                            "--prior-head",
                            old_review["commit_id"],
                            "--current-body",
                            str(inputs / "current.txt"),
                            "--notes",
                            str(inputs / "notes.json"),
                        ]
                    )
                self.assertEqual(result, 0, output.getvalue())
                worksheet = json.loads(output.getvalue())
                self.assertFalse(worksheet["approval_allowed"])
                self.assertEqual(
                    worksheet["next_action"], "qualified-current-head-human-review"
                )
                self.assertEqual(len(worksheet["mappings"]), len(old_index.entries))
                self.assertTrue(
                    all(
                        m["successor_disposition"] is None
                        for m in worksheet["mappings"]
                    )
                )
                self.assertEqual(len(fixture.trace), observed)
                self.assertEqual(fixture.reviews[: len(old_reviews)], old_reviews)
                state = fixture.accounted.accounting
                self.assertEqual(state.identity.run_id, fixture.run_id)
                self.assertIsNotNone(state.terminal_transaction)
                self.assertFalse(any(block.pending for block in state.blocks))
                self.assertEqual(
                    fixture.check_runs[-1]["conclusion"], "action_required"
                )
                self.assertFalse(any(r["state"] == "APPROVED" for r in fixture.reviews))
                self.assert_accounting(journey)

    def test_archive_crash_consumes_slot_without_provider_or_native_write(self):
        for boundary in ("fsync", "after-archive"):
            with (
                self.subTest(boundary=boundary),
                tempfile.TemporaryDirectory() as directory,
            ):
                journey = RestartJourney(directory)
                original = restart.prepare_admission

                def prepare(**kwargs):
                    original(**kwargs)
                    raise InjectedCrash()

                with ExitStack() as scopes:
                    if boundary == "fsync":
                        fsync = restart.os.fsync

                        def fail_archive(fd):
                            if (
                                journey.fixture.root / "abandoned-history.json"
                            ).exists():
                                raise InjectedCrash()
                            return fsync(fd)

                        scopes.enter_context(
                            patch.object(restart.os, "fsync", side_effect=fail_archive)
                        )
                    else:
                        scopes.enter_context(
                            patch.object(
                                restart, "prepare_admission", side_effect=prepare
                            )
                        )
                    with self.assertRaises(InjectedCrash):
                        journey.invoke()
                self.assertTrue(
                    (journey.fixture.root / "abandoned-history.json").exists()
                )
                self.assertEqual(journey.fixture.provider_calls, 0)
                self.assertFalse(
                    any(
                        m in {"POST", "PATCH", "DELETE", "PUT"} and "broker" not in p
                        for _, m, p in journey.fixture.trace
                        if p.startswith("/repos/")
                    )
                )
                self.assertNotEqual(journey.invoke(), 0)
                self.assertEqual(journey.fixture.provider_calls, 0)
                self.assertIsNone(journey.fixture.comment)

    def test_changed_head_base_or_policy_after_analysis_stops_publication(self):
        for fault in ("head", "base", "policy"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                journey = RestartJourney(directory)
                fixture = journey.fixture

                def change():
                    if fault == "head":
                        fixture.live_head = "e" * 40
                    elif fault == "base":
                        fixture.live_base = "e" * 40
                    else:
                        (fixture.root / ".reviewsensei.yml").write_bytes(
                            fixture.config_bytes + b"\n# changed\n"
                        )

                fixture.after_engine = change
                self.assertNotEqual(journey.invoke(), 0)
                self.assertEqual(fixture.provider_calls, 1)
                self.assertEqual(fixture.reviews, [])
                self.assertIsNone(
                    fixture.selected.caller.accounting.terminal_transaction
                )
                self.assertEqual(journey.archive()["historical_cost"], "unknown")
                self.assert_accounting(journey)

    def test_revoked_authority_before_publication_cannot_publish_or_approve(self):
        with tempfile.TemporaryDirectory() as directory:
            journey = RestartJourney(directory)
            journey.fixture.before_publication = lambda: setattr(
                journey.fixture, "refuse_broker", True
            )
            self.assertNotEqual(journey.invoke(), 0)
            self.assertEqual(journey.fixture.provider_calls, 1)
            self.assertEqual(journey.fixture.reviews, [])
            self.assertIsNone(
                journey.fixture.selected.caller.accounting.terminal_transaction
            )
            self.assert_accounting(journey)

    def test_lost_successor_creation_ack_never_repeats_creation_or_analysis(self):
        for stored in (False, True):
            with (
                self.subTest(stored=stored),
                tempfile.TemporaryDirectory() as directory,
            ):
                journey = RestartJourney(directory)
                lost = []

                def fault(request, timeout, original):
                    payload = json.loads(request.data) if request.data else {}
                    path = urlsplit(request.full_url).path
                    if (
                        request.method == "POST"
                        and path.endswith("/issues/7/comments")
                        and str(payload.get("body", "")).startswith(
                            "<!-- reviewsensei:controlled-restart:"
                        )
                        and not lost
                    ):
                        lost.append(path)
                        if stored:
                            original(request, timeout)
                        else:
                            journey.fixture.trace.append(
                                (journey.fixture.attempt, request.method, path)
                            )
                        raise TimeoutError("synthetic lost creation acknowledgement")
                    return original(request, timeout)

                journey.external_hook = fault
                code = journey.invoke()
                self.assertEqual(len(lost), 1)
                created = [
                    p
                    for _, m, p in journey.fixture.trace
                    if m == "POST" and p.endswith("/issues/7/comments")
                ]
                self.assertEqual(len(created), 1)
                if code != 0:
                    calls = journey.fixture.provider_calls
                    self.assertNotEqual(journey.invoke(), 0)
                    self.assertEqual(journey.fixture.provider_calls, calls)
                self.assertEqual(journey.old["body"], BODY)

    def test_lost_publication_ack_reconciles_one_review_without_new_inference(self):
        with tempfile.TemporaryDirectory() as directory:
            journey = RestartJourney(directory)
            lost = []

            def fault(request, timeout, original):
                path = urlsplit(request.full_url).path
                if (
                    request.method == "POST"
                    and path.endswith("/pulls/7/reviews")
                    and not lost
                ):
                    lost.append(path)
                    original(request, timeout)
                    raise TimeoutError("synthetic lost publication acknowledgement")
                return original(request, timeout)

            journey.external_hook = fault
            self.assertEqual(journey.invoke(), 0, journey.fixture.error)
            self.assertEqual(journey.fixture.provider_calls, 1)
            self.assertEqual(len(journey.fixture.reviews), 1)
            posts = [
                p
                for _, m, p in journey.fixture.trace
                if m == "POST" and p.endswith("/pulls/7/reviews")
            ]
            self.assertEqual(len(posts), 1)
            self.assertIsNotNone(
                journey.fixture.selected.caller.accounting.terminal_transaction
            )
            unknown = [
                o
                for o in journey.fixture.accounted.observations
                if o.get("mutation") and o["outcome"] == "unknown"
            ]
            self.assertEqual(len(unknown), 1)
            self.assertIn(
                unknown[0]["request_id"],
                journey.fixture.accounted._reconciled_mutations,
            )
            self.assert_accounting(journey)

    def test_terminal_response_loss_does_not_fabricate_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            journey = RestartJourney(directory)

            def fail_close():
                original = journey.fixture.selected.caller.finish_review_run

                def close(*args, **kwargs):
                    journey.fixture.fail_terminal_patch = True
                    return original(*args, **kwargs)

                journey.fixture.selected.caller.finish_review_run = close

            journey.fixture.before_publication = fail_close
            self.assertEqual(journey.invoke(), 1, journey.fixture.error)
            outcome = json.loads(
                (journey.fixture.root / "publication.json").read_bytes()
            )
            self.assertEqual(outcome["status"], "published_terminal_pending")
            self.assertFalse(outcome["accounting_terminal"])
            from review_sensei_github.session_ledger import parse_session_comment

            remote = parse_session_comment(
                journey.fixture.comment["body"],
                identity=journey.fixture.selected.identity,
            )
            self.assertIsNone(remote.api_run_accounting["terminal_transaction"])
            self.assertEqual(len(journey.fixture.reviews), 1)
            self.assertEqual(journey.archive()["historical_effects"], "unsettled")
            self.assert_accounting(journey)

    def test_delayed_old_check_review_and_root_never_become_successor_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            journey = RestartJourney(directory)
            fixture = journey.fixture

            def late():
                journey.old["body"] = "delayed old root update"
                fixture.reviews.append(
                    dict(
                        id=900,
                        commit_id=fixture.live_head,
                        state="APPROVED",
                        body="late historical approval",
                        user=dict(id=999, login="reviewsensei[bot]", type="Bot"),
                        pull_request_url="https://api.github.test/repos/owner/repo/pulls/7",
                        submitted_at="2026-10-25T22:00:00Z",
                    )
                )
                fixture.check_runs.append(
                    dict(
                        id=900,
                        name="ReviewSensei",
                        head_sha=fixture.live_head,
                        status="completed",
                        conclusion="success",
                        app=dict(slug="reviewsensei"),
                    )
                )

            fixture.before_publication = late
            self.assertEqual(journey.invoke(), 0, fixture.error)
            self.assertEqual(journey.archive()["comments"][0]["body"], BODY)
            self.assertNotIn("late historical", fixture.comment["body"])
            self.assertEqual(fixture.check_runs[-1]["conclusion"], "action_required")
            self.assertFalse(
                any(
                    m in {"PATCH", "PUT", "DELETE"}
                    and p.rsplit("/", 1)[-1] in {"71", "900"}
                    for _, m, p in fixture.trace
                )
            )
            self.assert_accounting(journey)

    def test_fresh_findings_do_not_spend_uncertain_old_occupancy(self):
        with tempfile.TemporaryDirectory() as directory:
            journey = RestartJourney(directory)
            self.assertNotEqual(journey.invoke(human=True), 0)
            self.assertEqual(journey.fixture.provider_calls, 1)
            self.assertEqual(journey.fixture.inline, [])
            self.assertEqual(journey.old["body"], BODY)
            self.assertFalse(
                any(r["state"] == "APPROVED" for r in journey.fixture.reviews)
            )
            self.assertIsNone(
                journey.fixture.selected.caller.accounting.terminal_transaction
            )
            self.assertTrue((journey.fixture.root / "review.json").exists())

    def test_cross_process_archive_slot_has_one_winner(self):
        with tempfile.TemporaryDirectory() as directory:
            script = """import sys
from test_controlled_restart import ArchiveFixture
from review_sensei.errors import ReviewInputError
try:
 ArchiveFixture(sys.argv[1]).prepare()
except ReviewInputError:
 print('refused')
else:
 print('admitted')
"""
            env = {
                k: v
                for k, v in os.environ.items()
                if k
                in {
                    "PATH",
                    "SYSTEMROOT",
                    "COMSPEC",
                    "WINDIR",
                    "TMP",
                    "TEMP",
                    "TMPDIR",
                    "PYTHONPATH",
                }
            }
            import review_sensei_github

            import review_sensei

            core = str(Path(review_sensei.__file__).resolve())
            adapter = str(Path(review_sensei_github.__file__).resolve())
            helpers = (
                Path(sys.modules["test_controlled_restart"].__file__).resolve().parent
            )
            env["PYTHONPATH"] = os.pathsep.join(
                map(
                    str,
                    (helpers, Path(core).parent.parent, Path(adapter).parent.parent),
                )
            )
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            children = [
                subprocess.Popen(
                    [sys.executable, "-c", script, directory],
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for _ in range(2)
            ]
            try:
                responses = [
                    (child, *child.communicate(timeout=30)) for child in children
                ]
            finally:
                for child in children:
                    if child.poll() is None:
                        child.terminate()
                        child.communicate(timeout=10)
            answers = []
            for child, out, err in responses:
                self.assertEqual(child.returncode, 0, err)
                answers.append(out.strip())
            self.assertCountEqual(answers, ["admitted", "refused"])
            archive = json.loads(
                (Path(directory) / "abandoned-history.json").read_bytes()
            )
            self.assertEqual(archive["comments"][0]["body"], BODY)

    def test_misplaced_temporary_root_refuses_without_reusing_retained_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            journey = RestartJourney(directory)
            # Remove only the fixture-owned supplied diff; restart prepares its own.
            (journey.fixture.root / "review.patch").unlink()
            original = review_run.TemporaryDirectory

            def misplaced(**kwargs):
                return original(prefix=kwargs["prefix"], dir=journey.fixture.root)

            with patch.object(review_run, "TemporaryDirectory", side_effect=misplaced):
                self.assertEqual(journey.invoke(), 1)
            self.assertIn("diff cannot use retained history", journey.fixture.error)
            self.assertEqual(journey.fixture.provider_calls, 0)
            self.assertTrue((journey.fixture.root / "abandoned-history.json").exists())
            self.assertEqual(len(list(journey.fixture.root.glob("*.claim"))), 1)
            self.assertFalse((journey.fixture.root / "review.patch").exists())
            self.assertFalse(list(journey.fixture.root.glob("review-sensei-restart-*")))

    def test_actual_git_diff_is_prepared_at_designated_base_and_head(self):
        with tempfile.TemporaryDirectory() as directory:
            journey = RestartJourney(directory)
            fixture = journey.fixture
            # The fixture creates a supplied diff; this case exercises actual Git.
            (fixture.root / "review.patch").unlink()

            def git(*args):
                return subprocess.check_output(
                    [
                        "git",
                        "-C",
                        str(fixture.root),
                        "-c",
                        "user.name=Fixture",
                        "-c",
                        "user.email=fixture@example.invalid",
                        "-c",
                        "commit.gpgsign=false",
                        *args,
                    ],
                    stderr=subprocess.PIPE,
                    text=True,
                ).strip()

            source = fixture.root / "src/app.py"
            source.parent.mkdir()
            source.write_text("keep\n", encoding="utf-8", newline="\n")
            git("add", "src/app.py")
            git("commit", "-m", "base")
            base = git("rev-parse", "HEAD")
            source.write_text("keep\nchange\n", encoding="utf-8", newline="\n")
            git("add", "src/app.py")
            git("commit", "-m", "head")
            head = git("rev-parse", "HEAD")
            git("checkout", "--detach", fixture.policy)
            fixture.requested_base = fixture.live_base = base
            fixture.requested_head = fixture.live_head = head
            changes = {k: v for k, v in journey.row.items() if k != "request_id"}
            journey.row = registration(
                **{**changes, "base_sha": base, "head_sha": head}
            )
            journey.actual_diff = True
            prepared = []
            original_prepare = review_run.prepare_diff

            def observe_diff(**kwargs):
                original_prepare(**kwargs)
                prepared.append((kwargs["output"], kwargs["output"].read_text()))

            with patch.object(review_run, "prepare_diff", side_effect=observe_diff):
                self.assertEqual(journey.invoke(), 0, fixture.error)
            self.assertEqual(fixture.provider_calls, 1)
            self.assertEqual(len(prepared), 1)
            path, diff = prepared[0]
            self.assertIn("+change", diff)
            self.assertFalse(path.is_relative_to(fixture.root.resolve()))
            self.assertFalse(path.exists())
            self.assertFalse(path.parent.exists())
            self.assertFalse((fixture.root / "review.patch").exists())
            self.assertEqual(git("rev-parse", "HEAD"), fixture.policy)
            self.assert_accounting(journey)

    def test_crashes_after_activation_analysis_or_publication_do_not_replay(self):
        for boundary in ("activation", "analysis", "publication"):
            with (
                self.subTest(boundary=boundary),
                tempfile.TemporaryDirectory() as directory,
            ):
                journey = RestartJourney(directory)
                fixture = journey.fixture

                def crash():
                    raise InjectedCrash()

                if boundary == "activation":
                    fixture.before_entry = crash
                elif boundary == "analysis":
                    fixture.after_engine = crash
                else:

                    def fault(request, timeout, original):
                        response = original(request, timeout)
                        if request.method == "POST" and urlsplit(
                            request.full_url
                        ).path.endswith("/pulls/7/reviews"):
                            raise InjectedCrash()
                        return response

                    journey.external_hook = fault
                with self.assertRaises(InjectedCrash):
                    journey.invoke()
                self.assertEqual(
                    fixture.provider_calls, 0 if boundary == "activation" else 1
                )
                self.assertEqual(
                    len(fixture.reviews), 1 if boundary == "publication" else 0
                )
                archive = (fixture.root / "abandoned-history.json").read_bytes()
                calls, reviews = fixture.provider_calls, len(fixture.reviews)
                self.assertNotEqual(journey.invoke(), 0)
                self.assertEqual(
                    (fixture.provider_calls, len(fixture.reviews)), (calls, reviews)
                )
                self.assertEqual(
                    (fixture.root / "abandoned-history.json").read_bytes(), archive
                )
                self.assertEqual(journey.old["body"], BODY)

    def test_archive_overflow_refuses_without_truncating_or_creating_successor(self):
        from test_controlled_restart import ArchiveFixture

        with tempfile.TemporaryDirectory() as directory:
            fixture = ArchiveFixture(directory)
            body = "x" * 65_536
            refs = [
                dict(
                    id=i,
                    bytes=len(body),
                    sha256=hashlib.sha256(body.encode()).hexdigest(),
                )
                for i in range(71, 75)
            ]
            row = registration(old_comments=refs)
            original = fixture.http.opener

            def open_archive(request, timeout):
                path = urlsplit(request.full_url).path
                if "/issues/comments/" in path:
                    fixture.budget.consume()
                    fixture.trace.append((request.method, path))
                    return json_response(
                        dict(
                            id=int(path.rsplit("/", 1)[1]),
                            body=body,
                            user=dict(type="Bot", login="reviewsensei[bot]"),
                            issue_url="https://api.github.test/repos/owner/repo/issues/7",
                        )
                    )
                return original(request, timeout)

            fixture.http.opener = open_archive
            with (
                patch("test_controlled_restart.registration", return_value=row),
                self.assertRaisesRegex(ReviewInputError, "archive exceeds"),
            ):
                fixture.prepare()
            self.assertFalse((Path(directory) / "abandoned-history.json").exists())
            self.assertEqual(len(fixture.trace), 6)
            self.assertTrue(all(method == "GET" for method, _ in fixture.trace))

    def test_review_echo_requires_exact_attempted_bytes_scope_and_authenticated_author(
        self,
    ):
        from test_controlled_restart import ArchiveFixture

        with tempfile.TemporaryDirectory() as directory:
            fixture = ArchiveFixture(directory)
            proof = fixture.prepare()
            guard = restart.RestartEffects(proof)
            path = "/repos/owner/repo/pulls/7/reviews"
            body = "fresh summary\n" + proof.marker
            payload = dict(body=body, event="COMMENT", commit_id="b" * 40, comments=[])
            item = dict(
                id=72,
                body=body,
                state="COMMENTED",
                commit_id="b" * 40,
                user=dict(type="Bot", login="reviewsensei[bot]"),
                pull_request_url="https://api.github.test/repos/owner/repo/pulls/7",
            )
            self.assertFalse(guard.confirm_review(path, item))
            guard.before_request("POST", path, payload)
            for change in (
                dict(body="old review"),
                dict(commit_id="e" * 40),
                dict(state="APPROVED"),
                dict(user=dict(type="User", login="reviewsensei[bot]")),
                dict(
                    pull_request_url="https://api.github.test/repos/other/repo/pulls/7"
                ),
                dict(id=True),
            ):
                with self.subTest(change=change):
                    self.assertFalse(guard.confirm_review(path, {**item, **change}))
            self.assertTrue(guard.confirm_review(path, item))
            with self.assertRaisesRegex(ReviewInputError, "competing"):
                guard.confirm_review(path, {**item, "id": 73})

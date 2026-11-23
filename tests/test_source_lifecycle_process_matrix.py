"""G27 ordinary CLI checkpoints across real interpreter boundaries.

Only synthetic remote JSON and fixture checkout files cross processes. No caller,
result proof, cancellation witness or epoch capability is serialized. The
pending-receipt interruption is a landed business effect without terminal receipt;
the real classifier conservatively calls it started work. Terminal cold entry is
an ordinary bootstrap refusal, not a claimed successful terminal replay route.
A stopped Python process is not promoted to a production cessation/non-dispatch receipt.
"""

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from fake_github_http import json_response
from review_sensei_github import cli as package_cli
from review_sensei_github.session_ledger import (
    parse_session_comment,
    render_session_comment,
)

from review_sensei.api_work import ApiRunAccounting
from review_sensei.models import ReviewTransaction
from review_sensei.publication_staging import PublicationStaging
from tests import test_action_pre_effect_and_g32_cli as reply_fixtures
from tests import test_action_run as fixtures
from tests import test_finding_dictionary_restart as persisted
from tests import test_source_comment_lifecycle_g27 as g27_fixtures

LIMIT = 512


def record(fixture):
    return (
        parse_session_comment(fixture.comment["body"], identity=fixtures.IDENTITY)
        if fixture.comment
        else None
    )


def snapshot(fixture):
    selected = record(fixture)
    state = (
        ApiRunAccounting.from_document(selected.api_run_accounting)
        if selected
        else None
    )
    return {
        "root": selected.to_dict() if selected else None,
        "business_writes": fixture.business_writes,
        "provider_calls": fixture.provider_calls,
        "closure_writes": fixture.closure_writes,
        "trace": fixture.trace,
        "limit": state.configuration.limit if state else None,
        "remaining": state.remaining if state else None,
        "confirmed": state.confirmed if state else None,
        "uncertain": state.uncertain if state else None,
        "reserved": state.reserved_undispatched if state else None,
        "prior": state.prior_nonstartup_liability if state else None,
        "startup": state.startup.liability if state else None,
        "argv": fixture.observed_argv,
        "environment": fixture.observed_environment,
    }


def save(fixture, directory):
    persisted.save_remote(fixture, directory / "remote.json")
    (directory / "before.json").write_text(json.dumps(snapshot(fixture)))


class ProcessFixture(fixtures.CommandEntryFixture):
    def __init__(self, directory):
        super().__init__(directory, limit=LIMIT)
        self.crash_phase = None
        self.crash_directory = None
        self.deleted = False
        self.verify_before_intent = False
        self.observed_argv = None
        self.observed_environment = None

    def external(self, request, timeout):
        path, method = urlsplit(request.full_url).path, request.get_method()
        if (
            self.deleted
            and method == "GET"
            and path == f"/repos/owner/repo/issues/comments/{self.SOURCE_ID}"
        ):
            self._meter(request)
            return json_response({"message": "Not Found"}, 404)
        response = super().external(request, timeout)
        selected = record(self)
        action = selected.api_run_accounting["action"] if selected else None
        matched = False
        if action is not None and action["phase"] == "pending":
            if self.crash_phase == "unstarted":
                # This ordinary source read follows the completed admission CAS.
                matched = (
                    method == "GET"
                    and path.endswith(f"/issues/comments/{self.SOURCE_ID}")
                    and not action["execution_started"]
                )
            elif self.crash_phase == "started":
                matched = (
                    method == "GET"
                    and path.endswith("/issues/7/comments")
                    and action["execution_started"]
                    and not selected.operator_paused
                )
            elif self.crash_phase == "pending_receipt":
                # Native business PATCH has landed; no terminal result receipt yet.
                matched = method == "PATCH" and selected.operator_paused
        if matched:
            save(self, self.crash_directory)
            os._exit(77)
        return response

    def invoke_observed(self, *, event):
        original = package_cli.main

        def main(argv):
            if self.verify_before_intent:
                argv = [argv[0], "--verify-source-before-intent", *argv[1:]]
            self.observed_argv = list(argv)
            self.observed_environment = {
                key: os.environ.get(key)
                for key in (
                    "GITHUB_RUN_ID",
                    "GITHUB_RUN_ATTEMPT",
                    "GITHUB_EVENT_NAME",
                    "GITHUB_ACTOR",
                    "GITHUB_TRIGGERING_ACTOR",
                    "REVIEW_SENSEI_WORKFLOW_REF",
                    "REVIEW_SENSEI_WORKFLOW_SHA",
                )
            }
            self.observed_environment["event"] = json.loads(
                Path(os.environ["GITHUB_EVENT_PATH"]).read_text()
            )
            return original(argv)

        with patch.object(package_cli, "main", side_effect=main):
            return self.invoke_command(event_override=event)


def original_event(fixture):
    return {
        "action": "created",
        "issue": {"number": 7, "pull_request": {}},
        "comment": copy.deepcopy(fixture.source),
    }


def produce(directory, phase):
    root = directory / "checkout"
    root.mkdir()
    fixture = ProcessFixture(root)
    fixture.select_command("@reviewsensei review pause")
    event = original_event(fixture)
    (directory / "event.json").write_text(json.dumps(event))
    if phase.startswith("live_"):
        fixture.verify_before_intent = True
        if phase == "live_deleted":
            fixture.deleted = True
        else:
            fixture.source.update(
                body="@reviewsensei review status", updated_at="2026-10-26T01:00:00Z"
            )
    elif phase.startswith("before_"):
        event["action"] = phase.removeprefix("before_")
    elif phase not in {"terminal", "staging_pending", "queue_pending"}:
        fixture.crash_phase, fixture.crash_directory = phase, directory
    elif phase in {"staging_pending", "queue_pending"}:
        orig_external = fixture.external

        def pending_carrier_ext(req, timeout):
            resp = orig_external(req, timeout)
            p = urlsplit(req.full_url).path
            if req.get_method() == "PATCH" and p.endswith("/issues/comments/1000000"):
                cur = record(fixture)
                if cur is not None and cur.operator_paused:
                    action = (cur.api_run_accounting or {}).get("action")
                    if action is not None and action["phase"] == "pending":
                        if phase == "staging_pending":
                            from dataclasses import replace

                            tx = replace(
                                ReviewTransaction.create(
                                    repository=fixtures.IDENTITY.repository,
                                    pull_request=fixtures.IDENTITY.pull_request,
                                    base_sha=fixture.requested_base,
                                    head_sha=fixture.requested_head,
                                    policy_digest="e" * 64,
                                    configuration_digest="f" * 64,
                                    evidence_digest="a" * 64,
                                    reservation_id="b" * 64,
                                    generation=1,
                                ),
                                phase="publication_pending",
                                result_sha256="c" * 64,
                            )
                            staging = PublicationStaging(
                                transaction_id=tx.transaction_id,
                                result_sha256="c" * 64,
                                plan_sha256="9" * 64,
                                part_count=1,
                            )
                            evolved = cur.evolve(
                                transaction=tx,
                                last_committed_reservation_id="b" * 64,
                                publication_staging=staging.to_document(),
                            )
                        else:
                            qroot = g27_fixtures._queue_root_document(
                                item_count=0, with_active_operation=True
                            )
                            from review_sensei.bounded_evidence import inline_manifest

                            qmanifest = inline_manifest(
                                {"schema_version": "1.0", "pending": 0},
                                binding=qroot["state_manifest"]["binding"],
                                item_count=0,
                            )
                            assert qmanifest is not None
                            qroot["state_manifest"] = qmanifest
                            evolved = cur.evolve(assessment_queue=qroot)
                        fixture.comment["body"] = render_session_comment(
                            repository_id=fixtures.IDENTITY.repository_id or 123,
                            pull_request=fixtures.IDENTITY.pull_request,
                            record=evolved,
                        )
                        save(fixture, directory)
                        (directory / "producer.json").write_text(
                            json.dumps({"code": 0, "error": fixture.error})
                        )
                        os._exit(0)
            return resp

        fixture.external = pending_carrier_ext  # type: ignore[method-assign]
    code = fixture.invoke_observed(event=event)
    if phase in {"staging_pending", "queue_pending"}:
        current = record(fixture)
        assert current is not None
        if phase == "staging_pending":
            from dataclasses import replace

            tx = replace(
                ReviewTransaction.create(
                    repository=fixtures.IDENTITY.repository,
                    pull_request=fixtures.IDENTITY.pull_request,
                    base_sha=fixture.requested_base,
                    head_sha=fixture.requested_head,
                    policy_digest="e" * 64,
                    configuration_digest="f" * 64,
                    evidence_digest="a" * 64,
                    reservation_id="b" * 64,
                    generation=1,
                ),
                phase="publication_pending",
                result_sha256="c" * 64,
            )
            staging = PublicationStaging(
                transaction_id=tx.transaction_id,
                result_sha256="c" * 64,
                plan_sha256="9" * 64,
                part_count=1,
            )
            evolved = current.evolve(
                transaction=tx,
                last_committed_reservation_id="b" * 64,
                publication_staging=staging.to_document(),
            )
        else:
            evolved = current.evolve(
                assessment_queue=g27_fixtures._queue_root_document(
                    item_count=0, with_active_operation=True
                ),
            )
        fixture.comment["body"] = render_session_comment(
            repository_id=fixtures.IDENTITY.repository_id or 123,
            pull_request=fixtures.IDENTITY.pull_request,
            record=evolved,
        )
    save(fixture, directory)
    (directory / "producer.json").write_text(
        json.dumps({"code": code, "error": fixture.error})
    )
    if phase not in {
        "terminal",
        "before_edited",
        "before_deleted",
        "live_edited",
        "live_deleted",
        "staging_pending",
        "queue_pending",
    }:
        raise AssertionError(("required crash not reached", phase, code, fixture.error))


REPLY_FIELDS = (
    "comment",
    "extra_comments",
    "reviews",
    "inline",
    "threads",
    "check_runs",
    "past_jobs",
    "run_id",
    "owner_job",
    "event_name",
    "source",
    "source_deleted",
    "policy",
    "policy_ref",
    "live_head",
    "live_base",
    "requested_head",
    "requested_base",
    "provider_calls",
    "reply_posts",
)


def save_reply_remote(fixture, path):
    value = {name: getattr(fixture, name) for name in REPLY_FIELDS}
    value["resolved_threads"] = sorted(fixture.resolved_threads)
    value["fixture_root"] = str(fixture.root)
    path.write_text(json.dumps(value), encoding="utf-8")


class ReplyProcessFixture(reply_fixtures._PreEffectReplyFixture):
    def __init__(self, directory, *, kind="issue"):
        super().__init__(directory, limit=LIMIT)
        self.kind = kind
        if kind == "inline":
            self.prepare_inline_reply(source_id=41)
        else:
            self.prepare_reply(source_id=41)

    def external(self, request, timeout):
        path = urlsplit(request.full_url).path
        method = request.get_method()
        if (
            method == "POST"
            and path.startswith("/repos/owner/repo/pulls/7/comments/")
            and path.endswith("/replies")
        ):
            self._meter_inline_safe(request)
            payload = json.loads(request.data)
            self.reply_posts += 1
            created = {
                "id": 9000 + self.reply_posts,
                "body": payload["body"],
                "user": {"id": 999, "login": "reviewsensei[bot]", "type": "Bot"},
                "pull_request_url": "https://api.github.test/repos/owner/repo/pulls/7",
            }
            self.inline.append(copy.deepcopy(created))
            return json_response(created, 201)
        return super().external(request, timeout)


def produce_reply(directory, spec):
    kind, phase = spec.split(":", 1)
    root = directory / "checkout"
    root.mkdir()
    fixture = ReplyProcessFixture(root, kind=kind)
    (directory / "reply-source.json").write_text(json.dumps(fixture.source))
    orig_ext = fixture.external

    def crash_before_started(req, timeout):
        path = urlsplit(req.full_url).path
        method = req.get_method()
        if (
            phase == "unstarted"
            and method == "PATCH"
            and path.endswith("/issues/comments/1000000")
        ):
            cur = record(fixture)
            cur_action = (cur.api_run_accounting or {}).get("action") if cur else None
            cand = parse_session_comment(
                json.loads(req.data)["body"], identity=fixtures.IDENTITY
            )
            cand_action = (
                (cand.api_run_accounting or {}).get("action") if cand else None
            )
            if (
                cur_action is not None
                and cur_action["phase"] == "pending"
                and not cur_action["execution_started"]
                and cand_action is not None
                and cand_action["execution_started"]
            ):
                save_reply_remote(fixture, directory / "remote.json")
                os._exit(77)
        return orig_ext(req, timeout)

    if phase == "unstarted":
        fixture.external = crash_before_started  # type: ignore[method-assign]
    code = fixture.invoke_reply_cli()
    save_reply_remote(fixture, directory / "remote.json")
    (directory / "producer.json").write_text(
        json.dumps(
            {
                "code": code,
                "error": fixture.error,
                "reply_posts": fixture.reply_posts,
                "provider_calls": fixture.provider_calls,
                "root": record(fixture).to_dict() if record(fixture) else None,
            }
        )
    )
    if phase == "unstarted":
        raise AssertionError(("reply unstarted crash not reached", code, fixture.error))


def consume_reply(directory, spec):
    kind, mode_flag = spec.split(":", 1)
    saved = json.loads((directory / "remote.json").read_text())
    with tempfile.TemporaryDirectory() as fresh:
        fixture = ReplyProcessFixture(fresh, kind=kind)
        for name in REPLY_FIELDS:
            setattr(fixture, name, saved[name])
        fixture.root = fixture.directory = Path(saved["fixture_root"])
        fixture.extra_comments = {int(k): v for k, v in fixture.extra_comments.items()}
        fixture.past_jobs = {int(k): v for k, v in fixture.past_jobs.items()}
        fixture.resolved_threads = set(saved["resolved_threads"])
        orig_source = json.loads((directory / "reply-source.json").read_text())
        if mode_flag.endswith("_a1"):
            fixture.past_jobs[fixture.run_id] = fixture.owner_job
            fixture.run_id += 1
            fixture.attempt = 1
        else:
            fixture.attempt = 2
        if mode_flag in {"edited", "edited_a1"}:
            fixture.source = {
                **orig_source,
                "body": "@reviewsensei edited",
                "updated_at": "2026-10-26T01:00:00Z",
            }
            if kind == "inline":
                fixture.inline = [
                    c if c.get("id") != fixture.source["id"] else fixture.source
                    for c in fixture.inline
                ]
        elif mode_flag in {"deleted", "deleted_a1"}:
            fixture.source_deleted = True
        else:
            fixture.source = copy.deepcopy(orig_source)

        # invoke_reply_cli uses self.source for event payload; feed original event source!
        saved_live = copy.deepcopy(fixture.source)
        fixture.source = orig_source

        orig_ext = fixture.external

        def live_mutated_ext(req, timeout):
            p = urlsplit(req.full_url).path
            if req.get_method() == "GET" and p.endswith(
                f"/comments/{orig_source['id']}"
            ):
                if fixture.source_deleted:
                    fixture.trace.append((fixture.attempt, "GET", p))
                    return json_response({"message": "Not Found"}, 404)
                fixture.trace.append((fixture.attempt, "GET", p))
                return json_response(copy.deepcopy(saved_live))
            return orig_ext(req, timeout)

        fixture.external = live_mutated_ext  # type: ignore[method-assign]
        code = fixture.invoke_reply_cli()
        cur = record(fixture)
        result = {
            "code": code,
            "error": fixture.error,
            "reply_posts": fixture.reply_posts,
            "provider_calls": fixture.provider_calls,
            "root": cur.to_dict() if cur else None,
        }
        (directory / "after.json").write_text(json.dumps(result))


def consume(directory, mutation):
    saved = json.loads((directory / "remote.json").read_text())
    with tempfile.TemporaryDirectory() as fresh:
        fixture = ProcessFixture(fresh)
        for name in persisted.FIELDS:
            setattr(fixture, name, saved[name])
        fixture.root = fixture.directory = Path(saved["fixture_root"])
        fixture.extra_comments = {
            int(key): value for key, value in fixture.extra_comments.items()
        }
        fixture.past_jobs = {
            int(key): value for key, value in fixture.past_jobs.items()
        }
        fixture.resolved_threads = set(saved["resolved_threads"])
        event = json.loads((directory / "event.json").read_text())
        if mutation.endswith("_a1"):
            fixture.past_jobs[fixture.run_id] = fixture.owner_job
            fixture.run_id += 1
            fixture.attempt = 1
        else:
            fixture.attempt = 2
        if mutation in {"edited", "edited_a1"}:
            fixture.source.update(
                body="@reviewsensei review status", updated_at="2026-10-26T01:00:00Z"
            )
        elif mutation in {"deleted", "deleted_a1"}:
            fixture.deleted = True
        elif mutation == "edited_event_a1":
            event["action"] = "edited"
            fixture.source.update(
                body="@reviewsensei review status", updated_at="2026-10-26T01:00:00Z"
            )
        elif mutation == "deleted_event_a1":
            event["action"] = "deleted"
            fixture.deleted = True
        elif mutation == "restored":
            fixture.source["updated_at"] = "2026-10-26T01:00:00Z"
        elif mutation == "head":
            fixture.live_head = "9" * 40
        code = fixture.invoke_observed(event=event)
        result = snapshot(fixture)
        result.update(code=code, error=fixture.error)
        (directory / "after.json").write_text(json.dumps(result))


class SourceLifecycleProcessMatrixTests(unittest.TestCase):
    def worker(self, directory, mode, value):
        root = Path(__file__).resolve().parents[1]
        env = dict(
            os.environ,
            PYTHONDONTWRITEBYTECODE="1",
            PYTHONPATH=os.pathsep.join(
                str(p)
                for p in (
                    root,
                    root / "src",
                    root / "packages/python/review-sensei-github/src",
                    root / "tests",
                )
            ),
        )
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "tests.test_source_lifecycle_process_matrix",
                mode,
                str(directory),
                value,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
        )

    def assert_accounting(self, state):
        self.assertEqual(state["limit"], LIMIT)
        self.assertEqual(
            sum(
                state[key]
                for key in (
                    "startup",
                    "prior",
                    "confirmed",
                    "uncertain",
                    "reserved",
                    "remaining",
                )
            ),
            LIMIT,
        )

    def test_preintent_edited_and_deleted_events_refuse_before_remote_work(self):
        for kind in ("edited", "deleted"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                directory = Path(directory)
                result = self.worker(directory, "produce", "before_" + kind)
                self.assertEqual(result.returncode, 0, result.stderr)
                before = json.loads((directory / "before.json").read_text())
                outcome = json.loads((directory / "producer.json").read_text())
                self.assertEqual(outcome["code"], 1, outcome["error"])
                self.assertEqual(before["trace"], [])
                self.assertIsNone(before["root"])
                self.assertEqual(before["provider_calls"], 0)
                self.assertEqual(before["environment"]["event"]["action"], kind)

    def test_live_source_mutation_before_intent_uses_real_cli_option(self):
        for kind in ("edited", "deleted"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                directory = Path(directory)
                result = self.worker(directory, "produce", "live_" + kind)
                self.assertEqual(result.returncode, 0, result.stderr)
                before = json.loads((directory / "before.json").read_text())
                outcome = json.loads((directory / "producer.json").read_text())
                self.assertEqual(outcome["code"], 1, outcome["error"])
                self.assert_accounting(before)
                self.assertIsNone(before["root"]["api_run_accounting"]["action"])
                self.assertFalse(before["root"]["operator_paused"])
                self.assertEqual(before["business_writes"], 0)
                self.assertEqual(before["provider_calls"], 0)
                self.assertIn("--verify-source-before-intent", before["argv"])
                self.assertTrue(
                    any(
                        path.endswith("/issues/comments/41")
                        for _, method, path in before["trace"]
                        if method == "GET"
                    )
                )

    def test_cold_supported_and_held_routes_preserve_exact_source_and_funding(self):
        for phase in ("unstarted", "started", "pending_receipt", "terminal"):
            for mutation in ("unchanged", "edited", "deleted", "restored", "head"):
                with (
                    self.subTest(phase=phase, mutation=mutation),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    directory = Path(directory)
                    produced = self.worker(directory, "produce", phase)
                    self.assertEqual(
                        produced.returncode,
                        0 if phase == "terminal" else 77,
                        produced.stderr,
                    )
                    before = json.loads((directory / "before.json").read_text())
                    self.assert_accounting(before)
                    action = before["root"]["api_run_accounting"]["action"]
                    self.assertEqual(
                        action["phase"],
                        "terminal" if phase == "terminal" else "pending",
                    )
                    self.assertEqual(action["execution_started"], phase != "unstarted")
                    self.assertEqual(
                        before["root"]["operator_paused"],
                        phase in {"pending_receipt", "terminal"},
                    )
                    cold = self.worker(directory, "consume", mutation)
                    self.assertEqual(cold.returncode, 0, cold.stderr)
                    after = json.loads((directory / "after.json").read_text())
                    self.assert_accounting(after)
                    next_action = after["root"]["api_run_accounting"]["action"]
                    self.assertEqual(
                        next_action["intent"]["source"], action["intent"]["source"]
                    )
                    self.assertEqual(
                        next_action["intent"]["operation_key"],
                        action["intent"]["operation_key"],
                    )
                    self.assertGreaterEqual(
                        after["prior"] + after["uncertain"], before["uncertain"]
                    )
                    self.assertEqual(after["provider_calls"], 0)
                    self.assertLessEqual(after["remaining"], before["remaining"])
                    self.assertEqual(after["environment"]["GITHUB_RUN_ATTEMPT"], "2")
                    self.assertEqual(after["argv"][0], "command-run")
                    self.assertIn("--github-session-ledger", after["argv"])
                    if phase == "unstarted" and mutation == "unchanged":
                        self.assertEqual(after["code"], 0, after["error"])
                        self.assertTrue(after["root"]["operator_paused"])
                        self.assertEqual(after["business_writes"], 1)
                        self.assertEqual(next_action["phase"], "terminal")
                    else:
                        self.assertEqual(
                            after["business_writes"],
                            before["business_writes"],
                            after["error"],
                        )
                        self.assertEqual(
                            after["root"]["operator_paused"],
                            before["root"]["operator_paused"],
                        )
                        self.assertEqual(next_action["phase"], action["phase"])
                        self.assertEqual(after["code"], 1, after["error"])
                        self.assertNotIn("Traceback", after["error"])
                        if mutation != "head":
                            self.assertIn(
                                "caller run is already terminal"
                                if phase == "terminal"
                                else "hold fail-closed",
                                after["error"],
                            )

    def test_fresh_process_reply_run_issue_and_inline_routes(self):
        # 1. Unstarted reply-run crash -> attempt=2 unchanged completes; edit/delete holds!
        for kind in ("issue", "inline"):
            for mutation in ("unchanged", "edited", "deleted"):
                with (
                    self.subTest(kind=kind, mutation=mutation),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    d = Path(directory)
                    produced = self.worker(d, "produce_reply", f"{kind}:unstarted")
                    self.assertEqual(produced.returncode, 77, produced.stderr)
                    before = json.loads((d / "remote.json").read_text())
                    self.assertEqual(before["provider_calls"], 0)
                    cold = self.worker(d, "consume_reply", f"{kind}:{mutation}")
                    self.assertEqual(cold.returncode, 0, cold.stderr)
                    after = json.loads((d / "after.json").read_text())
                    if mutation == "unchanged":
                        self.assertEqual(after["code"], 0, after["error"])
                        self.assertEqual(after["reply_posts"], 1)
                        self.assertEqual(
                            after["root"]["api_run_accounting"]["action"]["phase"],
                            "terminal",
                        )
                    else:
                        self.assertEqual(after["code"], 1)
                        self.assertEqual(after["reply_posts"], 0)
                        self.assertEqual(after["provider_calls"], 0)
                        self.assertIn("hold fail-closed", after["error"])

        # 2. Attempt=1 cross-run duplicate/edited/deleted after terminal reply-run!
        for kind in ("issue", "inline"):
            for mutation in ("unchanged_a1", "edited_a1", "deleted_a1"):
                with (
                    self.subTest(kind=kind, cross_mutation=mutation),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    d = Path(directory)
                    produced = self.worker(d, "produce_reply", f"{kind}:terminal")
                    self.assertEqual(produced.returncode, 0, produced.stderr)
                    prod = json.loads((d / "producer.json").read_text())
                    self.assertEqual(prod["code"], 0, prod["error"])
                    self.assertEqual(prod["reply_posts"], 1)
                    cold = self.worker(d, "consume_reply", f"{kind}:{mutation}")
                    self.assertEqual(cold.returncode, 0, cold.stderr)
                    after = json.loads((d / "after.json").read_text())
                    self.assertEqual(after["reply_posts"], prod["reply_posts"])
                    self.assertEqual(after["provider_calls"], prod["provider_calls"])
                    self.assertEqual(
                        after["root"]["record_sha256"],
                        prod["root"]["record_sha256"],
                    )
                    self.assertEqual(after["code"], 0, after["error"])

    def test_real_publication_staging_and_assessment_queue_pending_receipts(self):
        for phase in ("staging_pending", "queue_pending"):
            with (
                self.subTest(phase=phase),
                tempfile.TemporaryDirectory() as directory,
            ):
                d = Path(directory)
                produced = self.worker(d, "produce", phase)
                self.assertEqual(produced.returncode, 0, produced.stderr)
                before = json.loads((d / "before.json").read_text())
                self.assert_accounting(before)
                cold = self.worker(d, "consume", "deleted")
                self.assertEqual(cold.returncode, 0, cold.stderr)
                after = json.loads((d / "after.json").read_text())
                self.assert_accounting(after)
                self.assertEqual(after["code"], 1)
                self.assertIn("hold pending receipt", after["error"])
                self.assertEqual(after["business_writes"], before["business_writes"])
                self.assertEqual(after["provider_calls"], 0)
                self.assertEqual(
                    after["root"]["operator_paused"],
                    before["root"]["operator_paused"],
                )
                self.assertEqual(
                    after["root"]["api_run_accounting"]["action"],
                    before["root"]["api_run_accounting"]["action"],
                )
                self.assertEqual(
                    after["root"].get("publication_staging"),
                    before["root"].get("publication_staging"),
                )
                self.assertEqual(
                    after["root"].get("assessment_queue"),
                    before["root"].get("assessment_queue"),
                )

    def test_attempt1_cross_run_duplicate_edited_and_deleted_after_terminal_command(
        self,
    ):
        for mutation in (
            "unchanged_a1",
            "edited_a1",
            "deleted_a1",
            "edited_event_a1",
            "deleted_event_a1",
        ):
            with (
                self.subTest(mutation=mutation),
                tempfile.TemporaryDirectory() as directory,
            ):
                d = Path(directory)
                produced = self.worker(d, "produce", "terminal")
                self.assertEqual(produced.returncode, 0, produced.stderr)
                before = json.loads((d / "before.json").read_text())
                cold = self.worker(d, "consume", mutation)
                self.assertEqual(cold.returncode, 0, cold.stderr)
                after = json.loads((d / "after.json").read_text())
                self.assertEqual(after["business_writes"], before["business_writes"])
                self.assertEqual(after["provider_calls"], before["provider_calls"])
                self.assertEqual(
                    after["root"]["record_sha256"], before["root"]["record_sha256"]
                )
                self.assertEqual(
                    after["root"]["api_run_accounting"]["previous_record"],
                    before["root"]["api_run_accounting"]["previous_record"],
                )
                if mutation in {"edited_event_a1", "deleted_event_a1"}:
                    self.assertEqual(after["code"], 1)
                    self.assertEqual(after["trace"], [])
                else:
                    self.assertEqual(after["code"], 0, after["error"])


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] in {
        "produce",
        "consume",
        "produce_reply",
        "consume_reply",
    }:
        with patch(
            "socket.socket.connect", side_effect=AssertionError("unexpected network")
        ):
            {
                "produce": produce,
                "consume": consume,
                "produce_reply": produce_reply,
                "consume_reply": consume_reply,
            }[sys.argv[1]](Path(sys.argv[2]), sys.argv[3])
    else:
        unittest.main()

"""Ordinary accepted semantics: actual CLI and cold native-service persistence."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from review_sensei_github.accepted_semantic_history import (
    append_history,
    capture_selected,
    semantic_advancement,
    validate_history,
)

from review_sensei.errors import ReviewInputError
from review_sensei.session import LocalSessionLedger
from tests import test_durable_finding_lifecycle_restart as restart

FIELDS = (
    "comment",
    "extra_comments",
    "reviews",
    "inline",
    "check_runs",
    "threads",
    "run_id",
    "owner_job",
    "past_jobs",
    "provider_calls",
)


def phase(root, number):
    from review_sensei_github.api_run_history import read_action_result
    from review_sensei_github.session_ledger import parse_session_comment
    from test_action_run import CommandEntryFixture
    from test_api_work_persistence import IDENTITY

    child = root / str(number)
    child.mkdir()

    class HistoryEntryFixture(CommandEntryFixture):
        def external(self, request, timeout):
            from urllib.parse import urlsplit

            from fake_github_http import json_response

            response = super().external(request, timeout)
            path = urlsplit(request.full_url).path
            if (
                not self.command_active
                and "/actions/runs/" in path
                and path.endswith("/jobs")
            ):
                run_id = int(path.split("/actions/runs/")[1].split("/")[0])
                if run_id in self.past_jobs:
                    value = json.loads(response.read(512 * 1024 + 1))
                    value["jobs"][0]["name"] = (
                        "ReviewSensei / " + self.past_jobs[run_id]
                    )
                    return json_response(value)
            return response

    f = HistoryEntryFixture(child, limit=512)
    native = root / "native.json"
    if number:
        saved = json.loads(native.read_text())
        for key, value in saved.items():
            setattr(f, key, value)
        f.past_jobs = {int(key): value for key, value in f.past_jobs.items()}
        f.extra_comments = {int(key): value for key, value in f.extra_comments.items()}
        if number == 2 and (root / "intervening-review").exists():
            f.past_jobs[f.run_id] = f.owner_job
            f.run_id += 1
            f.owner_job = "hosted"
            code = f.invoke()
        else:
            f.SOURCE_ID = 40 + number
            f.select_command("@reviewsensei review status", new_run=True)
            code = f.invoke_command()
    else:
        code = f.invoke()
    native.write_text(json.dumps({key: getattr(f, key) for key in FIELDS}))
    value = {
        "exit": code,
        "error": f.error,
        "reviews": len(f.reviews),
        "provider_calls": f.provider_calls,
    }
    record = parse_session_comment(f.comment["body"], identity=IDENTITY)
    value["root_sha256"] = record.record_sha256
    if (
        number
        and code == 0
        and not (number == 2 and (root / "intervening-review").exists())
    ):
        value["history"] = read_action_result(f.selected.ledger, IDENTITY, record)[
            "result"
        ]["accepted_semantic_history"]
    (root / (str(number) + ".json")).write_text(json.dumps(value))


class AcceptedSemanticHistoryTests(unittest.TestCase):
    def test_actual_fresh_process_commands_persist_two_attempt_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = []
            for number in range(4):
                env = dict(
                    os.environ,
                    PYTHONDONTWRITEBYTECODE="1",
                    PYTHONPATH=str(Path.cwd())
                    + os.pathsep
                    + os.environ.get("PYTHONPATH", ""),
                    GIT_AUTHOR_DATE="2026-11-13T00:00:00Z",
                    GIT_COMMITTER_DATE="2026-11-13T00:00:00Z",
                )
                call = subprocess.run(
                    [sys.executable, __file__, str(root), str(number)],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(call.returncode, 0, call.stderr)
                results.append(json.loads((root / (str(number) + ".json")).read_text()))
            self.assertEqual(
                [r["exit"] for r in results],
                [0, 0, 0, 1],
                [r["error"][-1000:] for r in results],
            )
            self.assertEqual(results[1]["history"]["streak"], 1)
            self.assertEqual(results[2]["history"]["streak"], 2)
            self.assertIn("finite_no_progress_stop", results[3]["error"])
            self.assertEqual(results[2]["root_sha256"], results[3]["root_sha256"])
            self.assertEqual(
                [r["reviews"] for r in results], [results[0]["reviews"]] * 4
            )
            self.assertEqual(
                [r["provider_calls"] for r in results],
                [results[0]["provider_calls"]] * 4,
            )
            # Neither a terminal command nor accounting churn certifies review.
            self.assertEqual(
                [r["purpose"] for r in results[2]["history"]["records"]],
                ["command", "command"],
            )
            self.assertTrue(
                all(not r["made_progress"] for r in results[2]["history"]["records"])
            )

    def test_intervening_review_carries_history_without_reset_or_ancestor_census(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "intervening-review").touch()
            results = []
            for number in range(5):
                env = dict(
                    os.environ,
                    PYTHONDONTWRITEBYTECODE="1",
                    PYTHONPATH=str(Path.cwd())
                    + os.pathsep
                    + os.environ.get("PYTHONPATH", ""),
                    GIT_AUTHOR_DATE="2026-11-13T00:00:00Z",
                    GIT_COMMITTER_DATE="2026-11-13T00:00:00Z",
                )
                call = subprocess.run(
                    [sys.executable, __file__, str(root), str(number)],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(call.returncode, 0, call.stderr)
                results.append(json.loads((root / (str(number) + ".json")).read_text()))
            self.assertEqual(
                [r["exit"] for r in results],
                [0, 0, 0, 0, 1],
                [r["error"][-2200:] for r in results],
            )
            self.assertEqual(results[1]["history"]["streak"], 1)
            self.assertEqual(results[3]["history"]["streak"], 2)
            self.assertEqual(results[3]["root_sha256"], results[4]["root_sha256"])

    def test_actual_owned_partition_checkpoint_issues_only_exact_semantic_observation(
        self,
    ):
        import copy
        from unittest.mock import patch

        import test_api_work_lifecycle as lifecycle
        from review_sensei_github import GitHubWriteOptions, ReviewPublisher
        from review_sensei_github.accepted_semantic_history import (
            _digest,
            record_checkpoint_semantics,
        )
        from review_sensei_github.api_run_requests import ApiRunSessionLedger
        from review_sensei_github.finding_lifecycle import (
            FindingLifecycleJournal,
            restore_finding_lifecycle,
        )
        from test_activation_tail import CurrentHostedFixture
        from test_api_work_persistence import IDENTITY

        with tempfile.TemporaryDirectory() as directory:
            fixture = CurrentHostedFixture(directory)

            def options(**kwargs):
                return GitHubWriteOptions(**kwargs, max_active_findings=1)

            with patch(
                "test_api_work_lifecycle.GitHubWriteOptions", side_effect=options
            ):
                service, _, _ = lifecycle.CallerLifecycleTests().journey(
                    directory, prepared=(fixture.service, fixture.native), human=False
                )
            native = service.recover_lifecycle()
            ledger = ApiRunSessionLedger(native, service.accounted, IDENTITY)
            ledger.enable_finding_lifecycle_writes = True
            publisher = ReviewPublisher(http=service.http)
            record, document = restore_finding_lifecycle(publisher, ledger, IDENTITY)
            reference = record.finding_lifecycle
            binding = reference["manifest"]["binding"]
            journal = FindingLifecycleJournal(
                publisher,
                ledger,
                IDENTITY,
                operation_id=reference["operation_id"],
                base_sha=binding["base_sha"],
                head_sha=binding["head_sha"],
                policy_digest=binding["policy_digest"],
                configuration_digest=binding["configuration_digest"],
                document=document,
                record=record,
            )
            with (
                patch(
                    "review_sensei.finding_lifecycle_store.inline_manifest",
                    return_value=None,
                ),
                patch(
                    "review_sensei_github.session_ledger.inline_manifest",
                    return_value=None,
                ),
            ):
                journal.checkpoint()
            current = service.accounted._effect_record
            self.assertEqual(
                current.finding_lifecycle["manifest"]["encoding"], "partitioned-json-v1"
            )
            observation = service.accounted._accepted_semantic_checkpoint
            self.assertEqual(
                observation.reference_sha256, _digest(current.finding_lifecycle)
            )
            calls = len(service.accounted.observations)
            self.assertTrue(
                record_checkpoint_semantics(
                    ledger, IDENTITY, current, journal._document()
                )
            )
            changed = copy.deepcopy(journal._document())
            changed["unknown_trust_effect"] = not changed["unknown_trust_effect"]
            self.assertFalse(
                record_checkpoint_semantics(ledger, IDENTITY, current, changed)
            )
            self.assertFalse(
                record_checkpoint_semantics(
                    ledger, IDENTITY, record, journal._document()
                )
            )
            self.assertEqual(len(service.accounted.observations), calls)
            self.assertEqual(
                service.accounted._accepted_semantic_checkpoint, observation
            )

    def test_unreviewed_discussion_keeps_explicit_unqualified_result(self):
        from review_sensei_github.api_run_history import read_action_result
        from review_sensei_github.session_ledger import parse_session_comment
        from test_action_reply_entry import ReplyEntryFixture
        from test_api_work_persistence import IDENTITY

        with tempfile.TemporaryDirectory() as directory:
            fixture = ReplyEntryFixture(directory)
            self.assertEqual(fixture.invoke_reply(), 0, fixture.error)
            record = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
            result = read_action_result(fixture.selected.ledger, IDENTITY, record)
            summary = result["result"]["accepted_semantic_history"]
            self.assertEqual(summary["qualification"], "unavailable")
            self.assertNotIn("streak", summary)
            self.assertNotIn("records", summary)
            self.assertEqual(fixture.reply_posts, 1)

    def test_reference_metadata_requires_literal_terminal_result(self):
        from copy import deepcopy
        from unittest.mock import patch

        from review_sensei_github.session_ledger import parse_session_comment
        from test_api_work_persistence import IDENTITY

        from review_sensei.finding_lifecycle_store import (
            inline_terminal_action_history,
            require_reference_successor,
            require_terminal_history_amendment,
        )

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ,
                {
                    "GIT_AUTHOR_DATE": "2026-11-13T00:00:00Z",
                    "GIT_COMMITTER_DATE": "2026-11-13T00:00:00Z",
                },
            ),
        ):
            root = Path(directory)
            phase(root, 0)
            phase(root, 1)
            native = json.loads((root / "native.json").read_text())
            record = parse_session_comment(native["comment"]["body"], identity=IDENTITY)
            reference = deepcopy(record.finding_lifecycle)
            with self.assertRaises(ReviewInputError):
                require_reference_successor(None, reference)
            history = reference.pop("accepted_semantic_history")
            reference["schema_version"] = "finding-lifecycle-reference-v1"
            from review_sensei.session import SessionRecord, _digest_payload

            raw_before = deepcopy(record.to_dict())
            raw_before["finding_lifecycle"] = reference
            raw_before["api_run_accounting"]["action"].update(
                phase="pending", result=None, outcome=None
            )
            raw_before.pop("record_sha256")
            raw_before["record_sha256"] = _digest_payload(raw_before)
            before = SessionRecord.from_dict(raw_before)
            require_terminal_history_amendment(before, record)
            missing = deepcopy(record.to_dict())
            missing["finding_lifecycle"] = reference
            missing.pop("record_sha256")
            missing["record_sha256"] = _digest_payload(missing)
            with self.assertRaisesRegex(ReviewInputError, "commitment"):
                require_terminal_history_amendment(
                    SessionRecord.from_dict(missing), record
                )
            self.assertEqual(inline_terminal_action_history(record), history)
            for field, value in (
                ("target_head_sha", "f" * 40),
                ("operation_key", "forged"),
            ):
                forged = deepcopy(record.finding_lifecycle)
                forged["accepted_semantic_history"]["records"][0][field] = value
                with self.assertRaises(ReviewInputError):
                    require_terminal_history_amendment(
                        before, record.evolve(finding_lifecycle=forged)
                    )
            with self.assertRaises(ReviewInputError):
                require_reference_successor(record.finding_lifecycle, reference)
            forged = deepcopy(record.finding_lifecycle)
            forged["operation_id"] = "other-operation"
            with self.assertRaises(ReviewInputError):
                require_terminal_history_amendment(
                    before, record.evolve(finding_lifecycle=forged)
                )
            # Both separate widths fit, but grown carrier + next summary does
            # not. The pure stage preparation must refuse before any part sink.
            import hashlib
            from types import SimpleNamespace

            from review_sensei.bounded_evidence import canonical_bytes
            from review_sensei.finding_lifecycle_store import (
                MAX_REFERENCE_BYTES,
                _prepare_lifecycle,
            )

            future_history = deepcopy(history)
            future_history["records"].append(
                {**deepcopy(history["records"][0]), "operation_key": "next"}
            )
            future_history["streak"] = 2
            action = record.api_run_accounting["action"]
            caller = SimpleNamespace(
                _root=record.record_sha256,
                _effect_record=record,
                _accepted_semantic_capacity=None,
            )
            binding = record.finding_lifecycle["manifest"]["binding"]
            sink = []
            preview_ledger = SimpleNamespace(
                caller=caller,
                evidence_producer=lambda: binding["producer"],
                stage_partitioned_evidence=lambda *a: sink.append(a),
            )
            args = dict(
                operation_id=record.finding_lifecycle["operation_id"],
                base_sha=binding["base_sha"],
                head_sha=binding["head_sha"],
                policy_digest=binding["policy_digest"],
                configuration_digest=binding["configuration_digest"],
                item_count=1,
            )
            witness = {
                "intent_sha256": hashlib.sha256(
                    canonical_bytes(action["intent"])
                ).hexdigest(),
                "initial_root": record.record_sha256,
                "operation_id": args["operation_id"],
                "history": future_history,
            }
            current_future = {
                **record.finding_lifecycle,
                "accepted_semantic_history": future_history,
            }
            self.assertLessEqual(
                len(canonical_bytes(current_future)), MAX_REFERENCE_BYTES
            )
            found = False
            for count in range(4000, 22000, 1000):
                document = {
                    "growth": "".join(
                        hashlib.sha256(str(n).encode()).hexdigest()
                        for n in range(count)
                    )
                }
                try:
                    _, _, _, _, candidate = _prepare_lifecycle(
                        preview_ledger, record, document=document, **args
                    )
                except ReviewInputError:
                    continue
                combined = {
                    **candidate.finding_lifecycle,
                    "accepted_semantic_history": future_history,
                }
                if len(canonical_bytes(combined)) <= MAX_REFERENCE_BYTES:
                    continue
                found = True
                caller._accepted_semantic_capacity = witness
                with self.assertRaises(ReviewInputError):
                    _prepare_lifecycle(
                        preview_ledger, record, document=document, **args
                    )
                self.assertEqual(sink, [])
                break
            self.assertTrue(
                found, "growth witness must cross the unchanged 8 KiB joint bound"
            )
            # The grown pending root fits, but reserving its eventual terminal
            # result at the SAME grown root must refuse before an effect sink.
            from review_sensei.finding_lifecycle_store import (
                require_prospective_terminal_history_capacity,
            )

            raw = deepcopy(record.to_dict())
            raw["api_run_accounting"]["action"]["phase"] = "pending"
            raw["api_run_accounting"]["action"]["result"] = None
            raw["api_run_accounting"]["action"]["outcome"] = None
            padding = (
                32768
                - len(canonical_bytes(raw))
                - len(canonical_bytes({"sizing_only": ""}))
            )
            raw["sizing_only"] = "x" * padding
            self.assertLessEqual(len(canonical_bytes(raw)), 32768)
            with self.assertRaisesRegex(
                ReviewInputError, "terminal history root capacity"
            ):
                require_prospective_terminal_history_capacity(raw)
            self.assertEqual(sink, [])

            accounting = deepcopy(record.api_run_accounting)
            accounting["action"]["intent"]["head_sha"] = "f" * 40
            with self.assertRaises(ReviewInputError):
                inline_terminal_action_history(
                    record.evolve(api_run_accounting=accounting)
                )

    def test_command_capacity_includes_metadata_inside_existing_result_envelope(self):
        import hashlib
        from datetime import datetime, timezone

        from test_api_work_persistence import IDENTITY
        from test_terminal_representability_g34 import TerminalRepresentabilityG34Tests

        from review_sensei.disposition import MaintainerCommand
        from review_sensei.session import SessionRecord
        from review_sensei.terminal_representability import (
            preview_session_command_representability,
        )

        # Reproducible high-entropy bounded metadata; exact same encoder as settlement.
        metadata = {
            "accepted_semantic_history": "".join(
                hashlib.sha256(str(n).encode()).hexdigest() for n in range(120)
            )
        }
        record = SessionRecord.create(
            IDENTITY, now=datetime(2026, 11, 13, tzinfo=timezone.utc)
        )
        options = dict(
            identity=IDENTITY,
            current_record=record,
            command=MaintainerCommand(action="status", actor="alice"),
            intent_sha256="1" * 64,
            binding=TerminalRepresentabilityG34Tests()._default_binding(),
        )
        _, _, plain = preview_session_command_representability(**options)
        _, _, augmented = preview_session_command_representability(
            **options, result_metadata=metadata
        )
        self.assertTrue(plain.representable)
        self.assertGreater(
            augmented.manifest_bytes or augmented.carrier_bytes,
            plain.manifest_bytes or plain.carrier_bytes,
        )
        # At the existing encoder's exact boundary the plain result fits and
        # the augmented result refuses; the preview never owns an effect sink.
        from unittest.mock import patch

        with patch(
            "review_sensei.terminal_representability.MAX_MANIFEST_BYTES",
            plain.manifest_bytes,
        ):
            _, _, refused = preview_session_command_representability(
                **options, result_metadata={"accepted_semantic_history": "x"}
            )
        self.assertFalse(refused.representable)
        self.assertEqual(record.to_dict(), options["current_record"].to_dict())

    def test_history_capacity_boundary_is_pure_and_precedes_effect_checkpoint(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from review_sensei_github.accepted_semantic_history import (
            SemanticContext,
            history_reference_reserve,
            preflight_history_capacity,
            prospective_history,
        )
        from review_sensei_github.api_run_requests import ApiRunRequestTransport

        from review_sensei.bounded_evidence import canonical_bytes
        from review_sensei.finding_lifecycle_store import normalize_reference

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            restart.DurableFindingRestartTests().run_phase(root, "window", "initial")
            ledger = LocalSessionLedger(
                root / "ledger", enable_finding_lifecycle_writes=True
            )
            record, snapshot = capture_selected(ledger, restart.IDENTITY)
            intent = {
                "operation_key": "capacity",
                "purpose": "command",
                "head_sha": "a" * 40,
                "source": {
                    "kind": "issue_comment",
                    "id": 1,
                    "updated_at": "now",
                    "body_sha256": "a" * 64,
                },
            }
            caller = SimpleNamespace(
                _effect_record=record,
                _accepted_semantic_context=SemanticContext(
                    snapshot, None, restart.IDENTITY, "a" * 64, "b" * 40
                ),
            )
            summary = prospective_history(caller, restart.IDENTITY, intent)
            candidate = {
                **record.finding_lifecycle,
                "schema_version": "finding-lifecycle-reference-v2",
                "accepted_semantic_history": summary,
            }
            width = len(canonical_bytes(candidate))
            with patch(
                "review_sensei.finding_lifecycle_store.MAX_REFERENCE_BYTES", width
            ):
                normalize_reference(candidate)
                preflight_history_capacity(caller, restart.IDENTITY, intent)
            with patch(
                "review_sensei.finding_lifecycle_store.MAX_REFERENCE_BYTES", width - 1
            ):
                with self.assertRaises(ReviewInputError):
                    preflight_history_capacity(caller, restart.IDENTITY, intent)
            reserve = history_reference_reserve(record, summary)
            self.assertGreater(reserve, 0)
            called = []
            caller.accounting = SimpleNamespace(
                action={
                    "intent": intent,
                    "phase": "pending",
                    "execution_started": False,
                }
            )
            caller._action_effect_boundary = None
            caller._action_unresolved = lambda: False
            caller._action_checkpoint = lambda *args: called.append(args)
            with patch(
                "review_sensei.finding_lifecycle_store.MAX_REFERENCE_BYTES", width - 1
            ):
                with self.assertRaises(ReviewInputError):
                    ApiRunRequestTransport.begin_action_effect(
                        caller, ledger, restart.IDENTITY, intent
                    )
            self.assertEqual(called, [])
            self.assertFalse(caller.accounting.action["execution_started"])

    def test_semantic_projection_ignores_accounting_rounds_and_requires_real_progress(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            restart.DurableFindingRestartTests().run_phase(root, "window", "initial")
            ledger = LocalSessionLedger(
                root / "ledger", enable_finding_lifecycle_writes=True
            )
            _, snapshot = capture_selected(ledger, restart.IDENTITY)
            self.assertFalse(semantic_advancement(snapshot, dict(snapshot)))
            changed = {**snapshot, "receipts_sha256": "f" * 64}
            self.assertFalse(semantic_advancement(snapshot, changed))
            improved = {
                **snapshot,
                "unresolved_count": snapshot["unresolved_count"] - 1,
                "unresolved_sha256": "f" * 64,
            }
            self.assertTrue(semantic_advancement(snapshot, improved))
            source = {
                "kind": "issue_comment",
                "id": 1,
                "updated_at": "2026-11-13T00:00:00Z",
                "body_sha256": "a" * 64,
            }
            history = None
            for n in range(2):
                history = append_history(
                    history,
                    restart.IDENTITY,
                    {"operation_key": str(n), "purpose": "command", "source": source},
                    snapshot,
                    snapshot,
                )
            self.assertEqual(history["streak"], 2)
            progressed = append_history(
                history,
                restart.IDENTITY,
                {"operation_key": "new", "purpose": "command", "source": source},
                snapshot,
                improved,
            )
            self.assertEqual(progressed["streak"], 0)
            with self.assertRaises(ReviewInputError):
                validate_history({**history, "streak": 0}, restart.IDENTITY)


if __name__ == "__main__" and len(sys.argv) == 3:
    phase(Path(sys.argv[1]), int(sys.argv[2]))

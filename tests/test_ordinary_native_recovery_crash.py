"""A process crash cannot turn ephemeral settlement into durable authority."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from review_sensei_github import ordinary_native_recovery as recovery

from tests import test_ordinary_native_recovery_cold as cold
from tests import test_ordinary_native_recovery_custody as custody


class SimulatedProcessCrash(BaseException):
    pass


class OrdinaryNativeRecoveryCrashTests(unittest.TestCase):
    def _crash_recovery(self, fault=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            fixture = custody.OrdinaryNativeRecoveryCustodyTests().fixture(str(source))
            key = root / "private.key"
            key.write_bytes(b"synthetic-only-recovery-key-32byt")
            key.chmod(0o600)
            fixture.recovery_context_dir = root / "handoff"
            fixture.recovery_context_key_file = key
            captured = []

            def crash(proof, checks, **kwargs):
                capture = recovery._CHECK_RESULTS[proof][0]
                captured.append((capture.journal._record, capture.caller))
                raise SimulatedProcessCrash()

            with patch.object(recovery, "consume_check", crash):
                with self.assertRaises(SimulatedProcessCrash):
                    fixture.invoke()
            self.assertEqual(len(captured), 1)
            self.assertIsNone(captured[0][1].accounting.terminal_transaction)
            packet = root / "remote.json"
            packet.write_text(
                json.dumps(
                    {
                        "resume_expected": fault is None,
                        "admission_fault": fault,
                        "root": str(fixture.root),
                        "policy": fixture.policy,
                        "config_hex": fixture.config_bytes.hex(),
                        "handoff": str(fixture.recovery_context_dir),
                        "key_file": str(key),
                        "remote": {
                            name: getattr(fixture, name)
                            for name in (
                                "comment",
                                "extra_comments",
                                "reviews",
                                "inline",
                                "check_runs",
                                "trace",
                                "threads",
                            )
                        },
                    }
                )
            )
            child = root / "child"
            child.mkdir()
            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    self._admission_child(),
                    str(packet),
                    str(child),
                ],
                capture_output=True,
                text=True,
                timeout=45,
                env=cold.subprocess_environment(),
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            return result, fixture.check_runs[0]["id"]

    @staticmethod
    def _admission_child():
        fault_observer = r"""
original_observe=observe
from review_sensei_github import checkpoint_result_store as checkpoint
from review_sensei_github import empty_original_admission as empty
wrapper_code=lifecycle.restore_finding_lifecycle.__code__
from review_sensei_github.application import GitHubApplication
publish_code=GitHubApplication.publish_review.__code__
decoder_code=inspect.unwrap(checkpoint.CheckpointResultStore._decode).__code__
last_reader=[]
omission_grants=[]
def observe(frame,event,result):
    fault=packet.get("admission_fault")
    if event == "return" and frame.f_code is decoder_code:
        last_reader[:]=[frame.f_locals["self"],frame.f_locals["payload"],frame.f_locals.get("store")]
    if event == "return" and frame.f_code is empty.consume.__code__:
        omission_grants.append(result)
    if event == "call" and frame.f_code is publish_code:
        kwargs=frame.f_locals["kwargs"]
        if fault == "copied_return": kwargs["result"]=copy.copy(kwargs["result"])
        elif fault == "direct_decode":
            reader,payload,store=last_reader
            decoded=copy.copy(reader)._decode(payload,store)
            from review_sensei.models import ReviewResult
            kwargs["result"]=ReviewResult.from_dict(json.loads(decoded))
        elif fault == "old_return":
            from review_sensei_github.errors import GitHubPublicationError
            try: frame.f_locals["app"].publish_review(**{**kwargs,"result":copy.copy(kwargs["result"])})
            except GitHubPublicationError: pass
    if event == "return" and frame.f_code is restore_code and isinstance(result,tuple) and result[1] is not None:
        document=result[1]
        if fault == "unknown": document["unknown_trust_effect"]=True
        elif fault == "review": document["effects"][0]["path"]="/repos/owner/repo/pulls/7/reviews"
        elif fault == "pending":
            document["effects"][0]["status"]="pending"
            document["effects"][0]["response"]=None
        elif fault == "foreign_app": document["effects"][0]["response"][1]["app_id"]+=1
        elif fault == "foreign_head": document["effects"][0]["request"]["head_sha"]="c"*40
        elif fault == "native": document["native_receipts"].append({"schema_version":"native-finding-receipt-v1","repository_id":123,"pull_request":7,"head_sha":"b"*40,"base_sha":"a"*40,"producer_id":1,"review_id":1234,"instance":"a"*64,"body_sha256":"a"*64,"thread_id":None,"comment_id":None,"native_head_sha":None})
        elif fault == "loader": checkpoint.__spec__.loader=object()
        elif fault == "reader_source": checkpoint.read_accepted_result=lambda *a,**k: b"{}"
        elif fault == "consumer_source": empty.consume=lambda *a,**k: True
        elif fault == "foreign_external": document["effects"][0]["request"]["external_id"]="reviewsensei-review-v1:quarantine:foreign"
        if fault in {"review","foreign_head","foreign_external"}:
            effect=document["effects"][0]
            effect["request_sha256"]=lifecycle._digest({key:effect[key] for key in ("method","path","request")})
    if event == "return" and frame.f_code is wrapper_code and isinstance(result,tuple) and result[1] is not None and fault == "changed_document":
        result[1]["effects"][0]["response"][1]["id"]+=1
    original_observe(frame,event,result)
"""
        return cold.OrdinaryNativeRecoveryColdTests.CHILD.replace(
            '"code":code,"error":f.error',
            '"code":code,"omission_grants":omission_grants,"error":f.error',
        ).replace(
            "previous_profile=sys.getprofile()",
            fault_observer + "\nprevious_profile=sys.getprofile()",
        )

    @unittest.skipUnless(os.name == "posix", "private recovery storage requires POSIX")
    def test_crash_after_confirmed_receipt_before_terminal_keeps_cold_liability(self):
        result, check_id = self._crash_recovery()
        self.assertEqual(result["provider_calls"], 0)
        self.assertGreater(result["restored_unknown_charges"], 0)
        self.assertTrue(result["states"], result)
        self.assertEqual(set(result["states"]), {"confirmed"})
        # The source-confirmed journal is revalidated on the fresh attempt;
        # the original physical unknown charge is never refunded.
        self.assertEqual(result["code"], 0, result)
        self.assertIsNotNone(result["terminal"])
        self.assertEqual(result["review_states"], ["COMMENTED", "APPROVED"])
        self.assertEqual(result["check_ids"], [check_id])
        self.assertEqual(result["check_statuses"], ["completed"])
        self.assertEqual(
            result["native_writes"],
            [
                [
                    "PATCH",
                    f"/repos/owner/repo/check-runs/{check_id}",
                ],
                ["POST", "/repos/owner/repo/pulls/7/reviews"],
                ["POST", "/repos/owner/repo/pulls/7/reviews"],
            ],
        )
        self.assertTrue(result["no_process_authority"])

    @unittest.skipUnless(os.name == "posix", "private recovery storage requires POSIX")
    def test_empty_original_omission_rejects_changed_scope_effects_and_source(self):
        for fault in (
            "unknown",
            "review",
            "pending",
            "foreign_app",
            "foreign_head",
            "native",
            "loader",
            "reader_source",
            "changed_document",
            "copied_return",
            "direct_decode",
            "old_return",
        ):
            with self.subTest(fault=fault):
                result, _ = self._crash_recovery(fault)
                self.assertEqual(result["code"], 1, result)
                self.assertNotIn(True, result["omission_grants"])
                self.assertEqual(result["provider_calls"], 0)
                self.assertEqual(result["native_writes"], [])
                self.assertIsNone(result["terminal"])
                self.assertGreater(result["restored_unknown_charges"], 0)

    @unittest.skipUnless(os.name == "posix", "private recovery storage requires POSIX")
    def test_empty_original_consumer_and_namespace_cannot_grant(self):
        for fault in ("consumer_source", "foreign_external"):
            with self.subTest(fault=fault):
                result, _ = self._crash_recovery(fault)
                self.assertEqual(result["code"], 1, result)
                self.assertNotIn(True, result["omission_grants"])
                self.assertEqual(result["native_writes"], [])
                self.assertEqual(result["provider_calls"], 0)
                self.assertIsNone(result["terminal"])
                self.assertGreater(result["restored_unknown_charges"], 0)

    def test_empty_original_installers_reject_nondefining_callers(self):
        from review_sensei_github import (
            checkpoint_result_store,
            empty_original_admission,
            publication_recovery,
        )

        from review_sensei.errors import ReviewInputError

        cls = checkpoint_result_store.CheckpointResultStore
        original = (
            cls._source,
            cls._decode,
            publication_recovery.recover_retained_review_result,
        )
        with self.assertRaisesRegex(ReviewInputError, "defining installer"):
            empty_original_admission.install_reader(cls)
        with self.assertRaisesRegex(ReviewInputError, "defining installer"):
            empty_original_admission.install_recovery(original[2])
        self.assertEqual(
            (
                cls._source,
                cls._decode,
                publication_recovery.recover_retained_review_result,
            ),
            original,
        )

    def test_projected_empty_cannot_replace_nonempty_original(self):
        from dataclasses import replace

        from review_sensei_github.retention_projection import checkpoint_projection

        from review_sensei.errors import ReviewInputError
        from tests.test_retention_projection import fixture

        original, bound, raw, *_ = fixture(1)
        self.assertTrue(original.comments)
        empty = replace(original, comments=())
        empty = replace(
            empty,
            transaction=replace(
                bound.transaction, result_sha256=empty.content_digest()
            ),
        )
        self.assertNotEqual(empty.content_digest(), original.content_digest())
        with self.assertRaisesRegex(
            ReviewInputError, "review transaction result digest does not match"
        ):
            checkpoint_projection(raw, empty, {"source": "original"})

    def test_zero_effect_original_still_requires_uncorrupted_exact_source(self):
        import copy
        import inspect
        from contextlib import nullcontext

        from review_sensei_github import empty_original_admission, finding_lifecycle
        from review_sensei_github.application import GitHubApplication
        from review_sensei_github.session_ledger import parse_session_comment

        from review_sensei.api_work import ApiRunAccounting
        from tests.publication_timing_observers import (
            at_empty_original_publication_fence,
        )
        from tests.test_force_push_ancestry_g06 import IDENTITY, LifecycleEntryFixture

        restore_code = inspect.unwrap(
            finding_lifecycle.restore_finding_lifecycle
        ).__code__
        publish_code = GitHubApplication.publish_review.__code__
        for fault in ("unknown", "copied_return", "consumer_source"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                fixture = LifecycleEntryFixture(Path(directory))
                with at_empty_original_publication_fence(
                    lambda: setattr(fixture, "base_ref", "release/1.0")
                ):
                    code, outcome = fixture.run_and_read(human=False)
                self.assertEqual(code, 1, fixture.error)
                self.assertEqual(outcome["status"], "skipped_stale_base")
                original = parse_session_comment(
                    fixture.comment["body"], identity=IDENTITY
                )
                before = ApiRunAccounting.from_document(original.api_run_accounting)
                fixture.attempt = 2
                fixture.trace.clear()
                fixture.transports.clear()
                fixture.worker_observations.clear()
                injected = []
                previous = sys.getprofile()

                def observe(frame, event, result):
                    if (
                        fault == "unknown"
                        and event == "return"
                        and frame.f_code is restore_code
                        and isinstance(result, tuple)
                        and result[1] is not None
                    ):
                        self.assertEqual(result[1]["effects"], [])
                        result[1]["unknown_trust_effect"] = True
                        injected.append(True)
                    if (
                        fault == "copied_return"
                        and event == "call"
                        and frame.f_code is publish_code
                    ):
                        frame.f_locals["kwargs"]["result"] = copy.copy(
                            frame.f_locals["kwargs"]["result"]
                        )
                        injected.append(True)
                    if previous is not None:
                        previous(frame, event, result)

                changed = (
                    patch.object(empty_original_admission, "consume", return_value=True)
                    if fault == "consumer_source"
                    else nullcontext()
                )
                sys.setprofile(observe)
                try:
                    with changed as consumer:
                        code, _ = fixture.run_and_read(human=False)
                        if fault == "consumer_source":
                            consumer.assert_not_called()
                finally:
                    sys.setprofile(previous)
                if fault != "consumer_source":
                    self.assertTrue(injected)
                self.assertEqual(code, 1, fixture.error)
                self.assertIn("no confirmed complete original staging", fixture.error)
                self.assertEqual(fixture.provider_calls, 1)
                self.assertEqual(fixture.check_runs, [])
                self.assertEqual(fixture.review_posts, [])
                current = parse_session_comment(
                    fixture.comment["body"], identity=IDENTITY
                )
                after = ApiRunAccounting.from_document(current.api_run_accounting)
                from review_sensei_github.finding_lifecycle import _unpack_journal

                from review_sensei.bounded_evidence import read_inline_evidence

                def journal(record):
                    manifest = record.finding_lifecycle["manifest"]
                    stored = read_inline_evidence(
                        manifest, expected_binding=manifest["binding"]
                    )
                    return _unpack_journal(stored["lifecycle"])

                original_journal, current_journal = journal(original), journal(current)
                self.assertEqual(
                    current_journal["effects"], original_journal["effects"]
                )
                self.assertEqual(
                    current_journal["native_receipts"],
                    original_journal["native_receipts"],
                )
                self.assertEqual(
                    current_journal["lifecycle"], original_journal["lifecycle"]
                )
                self.assertEqual(
                    current_journal.get("accepted_semantic_history"),
                    original_journal.get("accepted_semantic_history"),
                )
                self.assertGreaterEqual(
                    current_journal["unknown_trust_effect"],
                    original_journal["unknown_trust_effect"],
                )
                self.assertEqual(
                    current.quarantine_lineage, original.quarantine_lineage
                )
                self.assertEqual(after.configuration, before.configuration)
                self.assertEqual(after.identity, before.identity)
                self.assertGreaterEqual(after.uncertain, before.uncertain)
                self.assertIsNone(after.terminal_transaction)

    def test_receipt_checkpoint_crash_never_settles_before_durable_return(self):
        from urllib.parse import urlsplit

        from review_sensei_github.session_ledger import parse_session_comment

        from review_sensei.api_work import ApiRunAccounting
        from tests.test_api_work_persistence import IDENTITY

        for stored in (False, True):
            with (
                self.subTest(stored=stored),
                tempfile.TemporaryDirectory() as directory,
            ):
                fixture = custody.OrdinaryNativeRecoveryCustodyTests().fixture(
                    directory
                )
                original = fixture.external
                captured = []

                def crash_checkpoint(request, timeout):
                    captures = tuple(recovery._ACTIVE.values())
                    if (
                        captures
                        and recovery._CLOSED.get(captures[0])
                        and request.get_method() == "PATCH"
                        and "/issues/comments/" in urlsplit(request.full_url).path
                    ):
                        captured.append(captures[0])
                        if stored:
                            original(request, timeout)
                        raise SimulatedProcessCrash()
                    return original(request, timeout)

                fixture.external = crash_checkpoint
                with self.assertRaises(SimulatedProcessCrash):
                    fixture.invoke()
                self.assertEqual(len(captured), 1)
                capture = captured[0]
                self.assertNotIn(
                    capture.mutation["request_id"], capture.caller._reconciled_mutations
                )
                record = parse_session_comment(
                    fixture.comment["body"], identity=IDENTITY
                )
                state = ApiRunAccounting.from_document(record.api_run_accounting)
                self.assertIsNone(state.terminal_transaction)
                # Before receipt storage, the original persisted allocation
                # still reserves the not-yet-reported dispatch; it is not free.
                self.assertGreater(state.uncertain + state.reserved_undispatched, 0)
                self.assertGreater(capture.caller.accounting.uncertain, 0)
                if stored:
                    self.assertGreater(state.uncertain, 0)
                self.assertEqual(len(fixture.check_runs), 1)
                self.assertEqual(fixture.reviews, [])

"""Runtime wiring and cold admission tests for ADR 0142 prospective abort close."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

from fake_github_http import FakeHTTPResponse, json_response
from review_sensei_github.api_run_history import (
    RETAINED_CONFIRMATION_TAIL,
    action_result_binding,
)
from review_sensei_github.api_run_plan import WRITE_CONFIRMATION_TAIL
from review_sensei_github.finding_lifecycle import _pack_journal, _unpack_journal
from review_sensei_github.finding_lifecycle_dictionary import pack as pack_lifecycle_v4
from review_sensei_github.http import MAX_PAGINATION_PAGES, GitHubHttp
from review_sensei_github.pre_entry_abort_admission import (
    require_pre_entry_abort_admission,
)
from review_sensei_github.prospective_abort_close import (
    AbortCloseTarget,
    observe_exact_abort_close,
)
from review_sensei_github.publication import (
    FINDING_MARKER_PREFIX_V2,
    approval_marker,
)
from review_sensei_github.session_ledger import (
    GitHubIssueCommentSessionLedger,
    parse_session_comment,
    render_session_comment,
)
from test_api_work_persistence import BASE, HEAD, IDENTITY
from test_review_run import EntryFixture, fixture_process_environment

from review_sensei.api_work import (
    ApiExecutionOwner,
    ApiRunAccounting,
    PendingApiRequest,
)
from review_sensei.bounded_evidence import (
    MAX_PARTITION_DECODED_BYTES,
    EvidenceReadBudget,
    canonical_bytes,
    encode_evidence,
    evidence_manifest,
    inline_manifest,
)
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewTransaction
from review_sensei.session import SessionRecord


def _subproc_env() -> dict[str, str]:
    repo_root = Path(__file__).resolve().parents[1]
    pythonpath = os.pathsep.join(
        (
            str(repo_root / "src"),
            str(repo_root / "packages" / "python" / "review-sensei-github" / "src"),
            str(repo_root / "tests"),
        )
    )
    return {
        **fixture_process_environment(),
        "PYTHONPATH": pythonpath,
    }


def _install_wire_inventory(test_case: unittest.TestCase, f: EntryFixture, lookup):
    external = f.external
    calls = []

    def supply(request, timeout):
        path = urlsplit(request.full_url)
        if (
            request.get_method() != "GET"
            or f.accounted._state is not None
            or path.path
            not in {
                "/repos/owner/repo/pulls/7/reviews",
                "/repos/owner/repo/pulls/7/comments",
            }
        ):
            return external(request, timeout)
        page = parse_qs(path.query)
        test_case.assertEqual(set(page), {"page", "per_page"})
        target = path.path.rsplit("/", 1)[-1], int(page["page"][0])
        calls.append(target)
        data = lookup(*target)
        if isinstance(data, Exception):
            raise data
        if isinstance(data, bytes):
            return FakeHTTPResponse(data)
        if isinstance(data, tuple):
            return json_response(copy.deepcopy(data[0]), status=data[1])
        return json_response(copy.deepcopy(data))

    f.external = supply
    return calls


class ProspectiveAbortCloseRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(
            patch("socket.socket", side_effect=AssertionError("offline fixture"))
        )

    def _assert_held(self, f: EntryFixture, reason: str) -> None:
        previous = copy.deepcopy(f.comment)
        evidence = copy.deepcopy(f.extra_comments)
        native = copy.deepcopy((f.reviews, f.inline, f.threads, f.check_runs))
        providers = f.provider_calls
        self.assertEqual(f.invoke(), 1, f.error)
        self.assertIn(reason, f.error)
        self.assertEqual(f.comment, previous)
        self.assertEqual(f.extra_comments, evidence)
        self.assertEqual((f.reviews, f.inline, f.threads, f.check_runs), native)
        self.assertEqual(f.provider_calls, providers)
        self.assertIsNone(f.accounted._activation)
        self.assertIsNone(f.accounted._state)
        self.assertEqual(f.accounted.usable, 0)
        self.assertFalse(
            any(
                method in {"POST", "PUT", "PATCH", "DELETE"}
                and path.startswith("/repos/")
                for _, method, path in f.trace
            ),
            f.trace,
        )

    def _abort_direct(
        self,
        f: EntryFixture,
        *,
        fault: str | None = "closure-stored",
        error: BaseException | None = None,
    ) -> SessionRecord:
        raised = KeyboardInterrupt() if error is None else error

        def refuse() -> None:
            raise raised

        f.before_entry = refuse
        f.abort_fault = fault
        expected_code = (
            130 if isinstance(raised, (KeyboardInterrupt, SystemExit)) else 1
        )
        self.assertEqual(f.invoke(), expected_code, f.error)
        f.before_entry = None
        return parse_session_comment(f.comment["body"], identity=IDENTITY)

    def _assert_prospective_witness_present(
        self, record: SessionRecord, *, comment_row: dict[str, object]
    ) -> ApiRunAccounting:
        accounting = ApiRunAccounting.from_document(record.api_run_accounting)
        self.assertIsNotNone(accounting.analysis_abort)
        self.assertEqual(accounting.uncertain, 1)
        block = next(
            item for item in accounting.blocks if item.block_id == "hosted-terminal"
        )
        self.assertEqual(len(block.pending), 1)
        self.assertTrue(block.pending[0].request_id.startswith("abort-close-v1-"))
        target = AbortCloseTarget(
            IDENTITY,
            "https://api.github.test",
            int(comment_row["id"]),
            "reviewsensei[bot]",
            int(comment_row["user"]["id"]),
        )
        echo = observe_exact_abort_close(target, selected=record, row=comment_row)
        self.assertEqual(echo.root_sha256, record.record_sha256)
        self.assertEqual(echo.run_id, accounting.identity.run_id)
        return accounting

    def test_cold_subprocess_lost_close_response_stores_witness_and_approves(
        self,
    ) -> None:
        for fault_exc in ("TimeoutError", "ConnectionResetError"):
            with (
                self.subTest(fault_exc=fault_exc),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                child_dir = root / "repo"
                child_dir.mkdir()
                packet = root / "aborted.json"
                phase1_script = f"""
import copy, json, sys
from pathlib import Path
from unittest.mock import patch
from review_sensei_github.session_ledger import parse_session_comment
from test_api_work_persistence import IDENTITY
from test_review_run import EntryFixture

with patch("socket.socket", side_effect=AssertionError("offline fixture")):
    f = EntryFixture(sys.argv[1], limit=512)
    def interrupt():
        raise KeyboardInterrupt()
    f.before_entry = interrupt
    base_external = f.external
    fault_armed = True
    def external(request, timeout):
        global fault_armed
        if fault_armed and request.get_method() == "PATCH":
            payload = json.loads(request.data)
            candidate = parse_session_comment(payload["body"], identity=IDENTITY)
            if candidate.api_run_accounting["analysis_abort"] is not None:
                fault_armed = False
                f._external_open(request, timeout)
                raise {fault_exc}("synthetic lost close PATCH response")
        return base_external(request, timeout)
    f.external = external
    code = f.invoke()
    accounting = f.accounted.accounting
    Path(sys.argv[2]).write_text(json.dumps({{
        "code": code,
        "comment": f.comment,
        "extra_comments": f.extra_comments,
        "policy": f.policy,
        "reviews": f.reviews,
        "inline": f.inline,
        "threads": f.threads,
        "check_runs": f.check_runs,
        "trace": f.trace,
        "provider_calls": f.provider_calls,
        "accounting_uncertain": accounting.uncertain,
        "final_abort_dispatched": f.accounted._final_abort_dispatched,
        "run_id": f.run_id,
    }}))
"""
                proc1 = subprocess.run(
                    [sys.executable, "-c", phase1_script, str(child_dir), str(packet)],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    env=_subproc_env(),
                    check=False,
                )
                self.assertEqual(proc1.returncode, 0, proc1.stderr)
                saved = json.loads(packet.read_text())
                self.assertEqual(saved["code"], 130)
                self.assertEqual(saved["accounting_uncertain"], 1)
                self.assertTrue(saved["final_abort_dispatched"])
                self.assertEqual(saved["provider_calls"], 0)
                patches = [
                    path
                    for _, method, path in saved["trace"]
                    if method == "PATCH" and "/issues/comments/" in path
                ]
                # Reserve PATCH, cleanup PATCH, and one prospective close PATCH; never replayed.
                self.assertEqual(len(patches), 3, saved["trace"])
                self.assertEqual(
                    saved["trace"][-1][1:],
                    ["PATCH", "/repos/owner/repo/issues/comments/1000000"],
                )
                stored_record = parse_session_comment(
                    saved["comment"]["body"], identity=IDENTITY
                )
                self.assertEqual(stored_record.generation, 3)
                self._assert_prospective_witness_present(
                    stored_record, comment_row=saved["comment"]
                )

                child2_dir = root / "repo2"
                child2_dir.mkdir()
                result_packet = root / "recovered.json"
                phase2_script = """
import json, sys
from pathlib import Path
from unittest.mock import patch
from review_sensei_github.api_run_history import read_previous_run
from review_sensei_github.session_ledger import GitHubIssueCommentSessionLedger, parse_session_comment
from test_api_work_persistence import IDENTITY
from test_review_run import EntryFixture

saved = json.loads(Path(sys.argv[2]).read_text())
with patch("socket.socket", side_effect=AssertionError("offline fixture")):
    f = EntryFixture(sys.argv[4], limit=512)
    f.root = Path(sys.argv[1])
    f.policy = f.policy_ref = saved["policy"]
    f.comment = saved["comment"]
    f.extra_comments = {int(k): v for k, v in saved["extra_comments"].items()}
    f.reviews = saved["reviews"]
    f.inline = saved["inline"]
    f.threads = saved["threads"]
    f.check_runs = saved["check_runs"]
    f.run_id = saved["run_id"]
    f.next_run()
    code = f.invoke()
    archived_dict = None
    successor_uncertain = None
    previous_record = None
    if code == 0 and f.selected is not None:
        archived = read_previous_run(
            f.selected.ledger, IDENTITY, f.accounted.accounting.previous_record
        )
        archived_dict = archived.to_dict()
        successor_uncertain = f.accounted.accounting.uncertain
        previous_record = f.accounted.accounting.previous_record
    Path(sys.argv[3]).write_text(json.dumps({
        "code": code,
        "error": f.error,
        "provider_calls": f.provider_calls,
        "reviews": f.reviews,
        "archived": archived_dict,
        "current_previous_record": previous_record,
        "successor_uncertain": successor_uncertain,
    }))
"""
                proc2 = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        phase2_script,
                        str(child_dir),
                        str(packet),
                        str(result_packet),
                        str(child2_dir),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    env=_subproc_env(),
                    check=False,
                )
                self.assertEqual(proc2.returncode, 0, proc2.stderr)
                recovered = json.loads(result_packet.read_text())
                self.assertEqual(recovered["code"], 0, recovered["error"])
                self.assertEqual(recovered["provider_calls"], 1)
                self.assertEqual(
                    sum(r["state"] == "APPROVED" for r in recovered["reviews"]), 1
                )
                self.assertEqual(recovered["successor_uncertain"], 0)
                # Archived predecessor record is preserved byte-for-byte with uncertain == 1.
                self.assertEqual(recovered["archived"], stored_record.to_dict())
                archived_acc = ApiRunAccounting.from_document(
                    recovered["archived"]["api_run_accounting"]
                )
                self.assertEqual(archived_acc.uncertain, 1)

    def test_sticky_finality_lock_blocks_post_abort_mutations_and_reconciliation(
        self,
    ) -> None:
        for fault in (None, "closure-stored"):
            with (
                self.subTest(fault=fault),
                tempfile.TemporaryDirectory() as directory,
            ):
                f = EntryFixture(directory)
                stored = self._abort_direct(f, fault=fault)
                self._assert_prospective_witness_present(stored, comment_row=f.comment)
                caller = f.accounted
                self.assertTrue(caller._final_abort_dispatched)
                ledger = GitHubIssueCommentSessionLedger(
                    f.http,
                    token="synthetic-review_session",
                    app_slug="reviewsensei[bot]",
                )
                snapshot = copy.deepcopy(f.comment)
                trace_before = len(f.trace)
                tx = ReviewTransaction.create(
                    repository=IDENTITY.repository,
                    pull_request=IDENTITY.pull_request,
                    base_sha="b" * 40,
                    head_sha=HEAD,
                    policy_digest="d" * 64,
                    configuration_digest="e" * 64,
                    evidence_digest="f" * 64,
                    reservation_id="a" * 64,
                    generation=3,
                )
                with self.assertRaisesRegex(
                    ReviewInputError, "prospective final abort close has sealed"
                ):
                    caller.replace_checkpoint(ledger, IDENTITY)
                with self.assertRaisesRegex(
                    ReviewInputError, "prospective final abort close has sealed"
                ):
                    caller.reconcile_checkpoint(ledger, IDENTITY)
                with self.assertRaisesRegex(
                    ReviewInputError, "prospective final abort close has sealed"
                ):
                    caller.admit_action(
                        ledger,
                        IDENTITY,
                        purpose="reply",
                        source={
                            "kind": "review_comment",
                            "id": 1,
                            "updated_at": "2026-11-18T00:00:00Z",
                            "body_sha256": "0" * 64,
                        },
                        head_sha=HEAD,
                        operation_key="0" * 64,
                    )
                with self.assertRaisesRegex(
                    ReviewInputError, "prospective final abort close has sealed"
                ):
                    caller.finish_review_run(ledger, IDENTITY, tx)
                with self.assertRaisesRegex(
                    ReviewInputError, "prospective final abort close has sealed"
                ):
                    caller.close_analysis_abort(ledger, IDENTITY, stored)
                with self.assertRaisesRegex(
                    ReviewInputError, "prospective final abort close has sealed"
                ):
                    req = Request(
                        "https://api.github.test/repos/owner/repo/issues/comments/1000000",
                        data=b'{"body":"tampered"}',
                        method="PATCH",
                    )
                    caller.open(req, timeout=10)

                self.assertEqual(f.comment, snapshot)
                self.assertEqual(len(f.trace), trace_before)
                f.next_run()
                # Direct ledger mutation wrappers routed through replace_checkpoint also fail closed.
                caller._final_abort_dispatched = True
                api_ledger = GitHubIssueCommentSessionLedger(
                    f.http,
                    token="synthetic-review_session",
                    app_slug="reviewsensei[bot]",
                )
                from review_sensei_github.api_run_requests import ApiRunSessionLedger

                wrapped = ApiRunSessionLedger(api_ledger, caller, IDENTITY)
                for fn in (
                    lambda: wrapped.reserve(
                        IDENTITY,
                        slot="failed-attempt",
                        reservation_id="1" * 64,
                        expected_generation=stored.generation,
                    ),
                    lambda: wrapped.commit(
                        IDENTITY,
                        reservation_id="1" * 64,
                        expected_generation=stored.generation,
                    ),
                    lambda: wrapped.abort(
                        IDENTITY,
                        reservation_id="1" * 64,
                        expected_generation=stored.generation,
                    ),
                ):
                    with self.assertRaisesRegex(
                        ReviewInputError, "prospective final abort close has sealed"
                    ):
                        fn()

                self.assertEqual(f.comment, snapshot)
                self.assertEqual(len(f.trace), 0)

    def test_session_ledger_captures_exact_raw_row_and_clears_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            f = EntryFixture(directory)
            stored = self._abort_direct(f, fault=None)
            now = datetime.now(UTC)
            raw_row = copy.deepcopy(f.comment)
            seen_headers = []

            def opener(request, timeout):
                seen_headers.append(request.get_header("Authorization"))
                path = urlsplit(request.full_url).path
                if request.get_method() == "GET" and path.endswith(
                    "/issues/7/comments"
                ):
                    return json_response([copy.deepcopy(raw_row)])
                if request.get_method() == "GET" and path.endswith("/pulls/7"):
                    return json_response({"head": {"sha": f.live_head}})
                if request.get_method() == "PATCH" and path.endswith(
                    "/issues/comments/1000000"
                ):
                    payload = json.loads(request.data)
                    raw_row["body"] = payload["body"]
                    return json_response(copy.deepcopy(raw_row))
                raise AssertionError((request.get_method(), path))

            http = GitHubHttp(api_url="https://api.github.test", opener=opener)
            ledger = GitHubIssueCommentSessionLedger(
                http, token="secret-token-value", app_slug="reviewsensei[bot]"
            )
            loaded = ledger.load(IDENTITY, now=now)
            self.assertEqual(loaded.status, "ok")
            self.assertIsNotNone(ledger._last_selected_row)
            cid, digest, captured = ledger._last_selected_row
            self.assertEqual(cid, 1_000_000)
            self.assertEqual(digest, stored.record_sha256)
            self.assertEqual(captured, f.comment)
            self.assertNotIn("secret-token-value", json.dumps(captured))
            # Must not expose raw row outside active replace() CAS mutation.
            self.assertIsNone(ledger.selected_native_comment(stored))

            observed_inside_cas = {}

            def inspect_and_mutate(record):
                observed_inside_cas["match"] = ledger.selected_native_comment(record)
                observed_inside_cas["wrong_digest"] = ledger.selected_native_comment(
                    record.evolve(now=now, generation=record.generation + 9)
                )
                return record.evolve(now=now, generation=record.generation + 1)

            updated = ledger.replace(IDENTITY, inspect_and_mutate, now=now)
            self.assertEqual(observed_inside_cas["match"], (1_000_000, f.comment))
            self.assertIsNone(observed_inside_cas["wrong_digest"])
            self.assertIsNotNone(ledger._last_selected_row)
            self.assertEqual(ledger._last_selected_row[1], updated.record_sha256)
            self.assertIsNone(ledger.selected_native_comment(stored))

            # Stale row is cleared when _discover observes missing or malformed state.
            def empty_opener(request, timeout):
                return json_response([])

            ledger.http = GitHubHttp(
                api_url="https://api.github.test", opener=empty_opener
            )
            missing = ledger.load(IDENTITY, now=now)
            self.assertEqual(missing.status, "missing")
            self.assertIsNone(ledger._last_selected_row)
            self.assertTrue(all(h == "Bearer secret-token-value" for h in seen_headers))

    def test_charged_failure_local_prior_uncertain_and_unfunded_close(self) -> None:
        # 1. Charged failure (ReviewInputError + closure-stored): out of scope -> holds.
        with tempfile.TemporaryDirectory() as directory:
            f = EntryFixture(directory)
            record = self._abort_direct(
                f, fault="closure-stored", error=ReviewInputError("pre-entry refusal")
            )
            self.assertEqual(record.generation, 5)
            self.assertEqual(record.failed_attempts, 1)
            acc = ApiRunAccounting.from_document(record.api_run_accounting)
            self.assertEqual(acc.uncertain, 1)
            block = next(b for b in acc.blocks if b.block_id == "hosted-terminal")
            self.assertFalse(block.pending[0].request_id.startswith("abort-close-v1-"))
            f.next_run()
            self._assert_held(f, "predecessor caller dispatch remains uncertain")

        # 2. Non-hosted runner falls back cleanly without emitting abort-close-v1.
        with tempfile.TemporaryDirectory() as directory:
            f = EntryFixture(directory, runner_kind="local")
            record = self._abort_direct(f, fault=None)
            acc = ApiRunAccounting.from_document(record.api_run_accounting)
            self.assertEqual(acc.uncertain, 0)
            self.assertFalse(f.accounted._final_abort_dispatched)
            f.next_run()
            self.assertEqual(f.invoke(), 0, f.error)

        # 3. Prior uncertainty at cleanup (cleanup-stored) falls back and holds successor.
        with tempfile.TemporaryDirectory() as directory:
            f = EntryFixture(directory)
            record = self._abort_direct(f, fault="cleanup-stored")
            acc = ApiRunAccounting.from_document(record.api_run_accounting)
            self.assertEqual(acc.uncertain, 1)
            block = next(b for b in acc.blocks if b.block_id == "hosted-terminal")
            self.assertFalse(block.pending[0].request_id.startswith("abort-close-v1-"))
            f.next_run()
            self._assert_held(f, "predecessor caller dispatch remains uncertain")

        # 4. Unfunded WRITE_CONFIRMATION_TAIL on direct abort close fails closed.
        with tempfile.TemporaryDirectory() as directory:
            f = EntryFixture(directory)

            def drain_before_interrupt() -> None:
                step = 0
                while (
                    next(
                        b.undispatched
                        for b in f.accounted.accounting.blocks
                        if b.block_id == "hosted-terminal"
                    )
                    > WRITE_CONFIRMATION_TAIL + 4
                ):
                    step += 1
                    req = PendingApiRequest(f"c-0-d{step:x}", 1_795_000_000_000)
                    f.accounted._state = f.accounted._state.dispatch(
                        "hosted-terminal", req
                    ).confirm(req)
                # Leave 7 slots on hosted-terminal: 4 consumed by cleanup (gen=2)
                # leaves 3 == WRITE_CONFIRMATION_TAIL, so close_analysis_abort reads fail
                # EvidenceBudgetExhausted before sending any close PATCH.
                raise KeyboardInterrupt()

            f.before_entry = drain_before_interrupt
            self.assertEqual(f.invoke(), 130, f.error)
            stored = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(stored.generation, 2)
            self.assertIsNone(stored.api_run_accounting["analysis_abort"])
            self.assertIsNone(stored.transaction)

    def test_negative_controls_hold_fail_closed_before_archive_provider_or_writes(
        self,
    ) -> None:
        def make_stored_prospective(
            directory: str,
        ) -> tuple[EntryFixture, SessionRecord]:
            f = EntryFixture(directory)
            record = self._abort_direct(f, fault="closure-stored")
            self._assert_prospective_witness_present(record, comment_row=f.comment)
            f.next_run()
            return f, record

        # (a) Lost close PATCH where GitHub did NOT store gen=3 -> held (no-transaction).
        with tempfile.TemporaryDirectory() as directory:
            f = EntryFixture(directory)
            record = self._abort_direct(f, fault="closure-absent")
            self.assertIsNone(record.api_run_accounting["analysis_abort"])
            f.next_run()
            self._assert_held(f, "phase=no-transaction")

        # (b) Legacy opaque ID (c-0-1f) with uncertain == 1 -> held.
        with tempfile.TemporaryDirectory() as directory:
            f = EntryFixture(directory)
            captured_cleanup: list[str] = []
            base_external = f.external

            def capture_cleanup(request, timeout):
                if request.get_method() == "PATCH":
                    payload = json.loads(request.data)
                    cand = parse_session_comment(payload["body"], identity=IDENTITY)
                    if cand.generation == 2 and cand.transaction is None:
                        captured_cleanup.append(payload["body"])
                return base_external(request, timeout)

            f.external = capture_cleanup
            stored = self._abort_direct(f, fault=None)
            cleanup_rec = parse_session_comment(captured_cleanup[0], identity=IDENTITY)
            cleanup_acc = ApiRunAccounting.from_document(cleanup_rec.api_run_accounting)
            opaque_acc = replace(
                cleanup_acc,
                analysis_abort=stored.api_run_accounting["analysis_abort"],
            ).dispatch(
                "hosted-terminal", PendingApiRequest("c-0-1f", 1_795_000_000_000)
            )
            opaque_rec = cleanup_rec.evolve(
                generation=3, api_run_accounting=opaque_acc.to_document()
            )
            f.comment["body"] = render_session_comment(
                repository_id=123, pull_request=7, record=opaque_rec
            )
            f.next_run()
            self._assert_held(f, "predecessor caller dispatch remains uncertain")

        # (c) uncertain == 2 (extra pending write alongside abort-close-v1) -> held.
        with tempfile.TemporaryDirectory() as directory:
            f, _ = make_stored_prospective(directory)

            def extra_pending(rec):
                acc = ApiRunAccounting.from_document(rec.api_run_accounting).dispatch(
                    "hosted-review", PendingApiRequest("c-0-fe", 1_795_000_000_000)
                )
                return rec.evolve(
                    generation=rec.generation,
                    api_run_accounting=acc.to_document(),
                )

            f.rewrite_record(mutate=extra_pending)
            self._assert_held(f, "predecessor caller dispatch remains uncertain")

        # (d) Late native review on aborted scope -> held.
        with tempfile.TemporaryDirectory() as directory:
            f, _ = make_stored_prospective(directory)
            f.reviews.append(
                {
                    "id": 77,
                    "commit_id": HEAD,
                    "state": "APPROVED",
                    "body": approval_marker(
                        repository_id=123, pull_request=7, head_sha=HEAD, base_sha=BASE
                    ),
                    "user": {"type": "Bot", "login": "reviewsensei[bot]", "id": 999},
                }
            )
            self._assert_held(f, "late native effect on the aborted scope")

        # (e) Late native inline comment on aborted scope -> held.
        with tempfile.TemporaryDirectory() as directory:
            f, _ = make_stored_prospective(directory)
            f.inline.append(
                {
                    "id": 88,
                    "pull_request_review_id": 77,
                    "commit_id": HEAD,
                    "original_commit_id": HEAD,
                    "body": (
                        f"{FINDING_MARKER_PREFIX_V2} repo=123 pr=7 head={HEAD} "
                        f"base={BASE} fingerprint={'a' * 64} state=new blocking=false -->"
                    ),
                    "user": {"type": "Bot", "login": "reviewsensei[bot]", "id": 999},
                }
            )
            self._assert_held(f, "late native effect on the aborted scope")

        # (f) Active (unceased) predecessor Actions owner -> held.
        with tempfile.TemporaryDirectory() as directory:
            f, _ = make_stored_prospective(directory)
            f.previous_active = True
            self._assert_held(f, "previous Actions run has not ceased")

        # (g) Altered comment body (noncanonical prefix / reflowed JSON, still marker-shaped) -> held.
        for tamper in (
            lambda body: "noncanonical-prefix\n" + body,
            lambda body: body.replace("```json\n{", "```json\n{ ", 1),
        ):
            with tempfile.TemporaryDirectory() as directory:
                f, _ = make_stored_prospective(directory)
                f.comment["body"] = tamper(f.comment["body"])
                self._assert_held(f, "predecessor caller dispatch remains uncertain")

        # (h) Wrong producer ID on Bot author -> held.
        with tempfile.TemporaryDirectory() as directory:
            f, _ = make_stored_prospective(directory)
            f.comment["user"]["id"] = 998
            self._assert_held(f, "predecessor caller dispatch remains uncertain")

        # (i) Wrong comment ID (body copied to comment 2_000_000) -> held.
        with tempfile.TemporaryDirectory() as directory:
            f, _ = make_stored_prospective(directory)
            f.comment["id"] = 2_000_000
            self._assert_held(f, "predecessor caller dispatch remains uncertain")

        # (j) Wrong issue_url returned on native comment read -> held.
        with tempfile.TemporaryDirectory() as directory:
            f, _ = make_stored_prospective(directory)
            base_external = f.external

            def spoof_issue_url(request, timeout):
                response = base_external(request, timeout)
                if request.get_method() == "GET" and urlsplit(
                    request.full_url
                ).path.endswith("/issues/7/comments"):
                    rows = json.loads(response.read(524288))
                    for row in rows:
                        if row.get("id") == 1_000_000:
                            row["issue_url"] = (
                                "https://api.github.test/repos/owner/repo/issues/8"
                            )
                    return json_response(rows)
                return response

            f.external = spoof_issue_url
            self._assert_held(f, "predecessor caller dispatch remains uncertain")

        # (k) Wrong generation (gen=4 != tx.generation + 2) -> held.
        with tempfile.TemporaryDirectory() as directory:
            f, _ = make_stored_prospective(directory)
            f.rewrite_record(generation_delta=1)
            self._assert_held(f, "predecessor caller dispatch remains uncertain")

        # (l) Non-plain root (trust_enrollment attached alongside abort-close-v1) -> held.
        with tempfile.TemporaryDirectory() as directory:
            f, _ = make_stored_prospective(directory)
            auth_doc = {
                "version": 4,
                "purpose": "review",
                "repository": IDENTITY.repository,
                "repository_id": IDENTITY.repository_id,
                "pull_request": IDENTITY.pull_request,
                "head_sha": HEAD,
                "run_id": 456,
                "run_attempt": 1,
                "job": "hosted",
                "job_workflow_ref": "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@refs/tags/v5",
                "job_workflow_sha": "a" * 40,
                "issued_at": "2026-11-18T00:00:00Z",
                "expires_at": "2026-11-18T00:05:00Z",
                "audience": "https://broker.github.test",
                "source": {
                    "kind": "issue_comment",
                    "id": 1,
                    "updated_at": "2026-11-18T00:00:00Z",
                    "body_sha256": "0" * 64,
                },
                "actor": None,
                "current_observation": {
                    "schema_version": "current-observation-v1",
                    "repository": {
                        "id": 123,
                        "full_name": IDENTITY.repository,
                        "owner_id": 1,
                        "owner_login": "owner",
                        "fork": False,
                    },
                    "installation": {
                        "id": 1,
                        "repository_selection": "selected",
                        "repository_access": "selected",
                        "permissions": {},
                    },
                    "capability": {"name": "review_session", "permissions": {}},
                    "actor": {
                        "id": 1,
                        "login": "alice",
                        "source_association": "OWNER",
                    },
                },
            }
            trust_doc = {
                "schema_version": "original-trust-enrollment-v1",
                "app_id": 999,
                "authority": auth_doc,
                "generation": 1,
            }
            trust_ref = {
                "schema_version": "trust-enrollment-reference-v1",
                "document": trust_doc,
                "sha256": hashlib.sha256(canonical_bytes(trust_doc)).hexdigest(),
            }
            f.rewrite_record(
                mutate=lambda rec: rec.evolve(
                    generation=rec.generation,
                    trust_enrollment=trust_ref,
                )
            )
            non_plain = parse_session_comment(f.comment["body"], identity=IDENTITY)
            non_plain_old = ApiRunAccounting.from_document(non_plain.api_run_accounting)
            http = GitHubHttp(
                api_url="https://api.github.test",
                opener=lambda *_a, **_kw: json_response([]),
            )
            ledger = GitHubIssueCommentSessionLedger(
                http,
                token="synthetic-token",
                app_slug="reviewsensei[bot]",
                evidence_budget=EvidenceReadBudget(),
            )
            ledger._evidence_author_id = 999
            ledger._mutation_active = True
            ledger._last_selected_row = (
                1_000_000,
                non_plain.record_sha256,
                {
                    **copy.deepcopy(f.comment),
                    "body": render_session_comment(
                        repository_id=123, pull_request=7, record=non_plain
                    ),
                },
            )
            with self.assertRaisesRegex(
                ReviewInputError, "predecessor caller dispatch remains uncertain"
            ):
                require_pre_entry_abort_admission(
                    ledger, IDENTITY, non_plain, non_plain_old
                )
            self._assert_held(f, "trust enrolled current observation unavailable")

        # (m) Unfunded census/archive tail (one-short funding) -> held.
        with tempfile.TemporaryDirectory() as directory:
            f, _ = make_stored_prospective(directory)

            def full_until_last(_surface, page):
                if page == MAX_PAGINATION_PAGES:
                    return []
                return [
                    {
                        "id": 100 * page + i,
                        "user": {"id": 42, "type": "User", "login": "alice"},
                    }
                    for i in range(100)
                ]

            for i in range(899):
                f.extra_comments[3_000_000 + i] = {
                    "id": 3_000_000 + i,
                    "body": "synthetic unrelated issue discussion",
                    "user": {"id": 42, "type": "User", "login": "alice"},
                }
            reads = _install_wire_inventory(self, f, full_until_last)
            self._assert_held(f, "census is unfunded in caller startup")
            self.assertLess(len(reads), 2 * MAX_PAGINATION_PAGES)
            self.assertEqual(
                f.accounted.budget_view.ordinary_remaining(),
                RETAINED_CONFIRMATION_TAIL,
            )

        # (n) Direct admission non-Bot / wrong Bot login check on raw native row -> held.
        with tempfile.TemporaryDirectory() as directory:
            f, record = make_stored_prospective(directory)
            old = ApiRunAccounting.from_document(record.api_run_accounting)
            bad_rows = (
                {
                    **copy.deepcopy(f.comment),
                    "user": {"id": 999, "login": "reviewsensei[bot]", "type": "User"},
                },
                {
                    **copy.deepcopy(f.comment),
                    "user": {"id": 999, "login": "other[bot]", "type": "Bot"},
                },
            )
            for bad_row in bad_rows:
                http = GitHubHttp(
                    api_url="https://api.github.test",
                    opener=lambda *_a, **_kw: json_response([]),
                )
                ledger = GitHubIssueCommentSessionLedger(
                    http,
                    token="synthetic-token",
                    app_slug="reviewsensei[bot]",
                    evidence_budget=EvidenceReadBudget(),
                )
                ledger._evidence_author_id = 999
                ledger._mutation_active = True
                ledger._last_selected_row = (1_000_000, record.record_sha256, bad_row)
                with self.assertRaisesRegex(
                    ReviewInputError, "predecessor caller dispatch remains uncertain"
                ):
                    require_pre_entry_abort_admission(ledger, IDENTITY, record, old)

    def test_earlier_native_rows_intervening_terminal_command_and_packed_v4(
        self,
    ) -> None:
        from review_sensei_github.api_run_host import review_startup_plan
        from review_sensei_github.api_run_plan import owner_allocations

        from review_sensei.api_work import (
            ACTION_NOOP_REASONS,
            ApiRunIdentity,
            initial_accounting,
        )
        from review_sensei.finding_lifecycle_store import _decode_selected_lifecycle

        def clone_rec(base: SessionRecord, **overrides: object) -> SessionRecord:
            fields = {
                key: value
                for key, value in vars(base).items()
                if key != "record_sha256" and not key.startswith("_")
            }
            fields.update(overrides)
            return SessionRecord._construct(**fields)

        # 1. Produce a real published review on `HEAD` with native review + finding_lifecycle.
        with tempfile.TemporaryDirectory() as directory:
            f = EntryFixture(directory, limit=560)
            self.assertEqual(f.invoke(), 0, f.error)
            self.assertGreaterEqual(len(f.reviews), 1)
            pub_rec = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertIsNotNone(pub_rec.transaction)
            self.assertIsNotNone(pub_rec.finding_lifecycle)

            budget = EvidenceReadBudget()
            ro_ledger = GitHubIssueCommentSessionLedger(
                f.http,
                token="synthetic-token",
                app_slug="reviewsensei[bot]",
                evidence_budget=budget,
            )
            ro_ledger._evidence_author_id = 999
            pub_journal = _unpack_journal(
                _decode_selected_lifecycle(ro_ledger, pub_rec)
            )
            self.assertGreaterEqual(len(pub_journal["effects"]), 1)

            # Pack that ancestor's lifecycle using `finding-lifecycle-packed-v4`
            # referencing an archived base envelope stored in `extra_comments`.
            fl_ref = pub_rec.finding_lifecycle
            assert fl_ref is not None
            base_doc = {
                "schema_version": "finding-lifecycle-evidence-v2",
                "lifecycle": _pack_journal(pub_journal),
            }
            base_binding = {**fl_ref["manifest"]["binding"]}
            base_manifest = inline_manifest(
                base_doc, binding=base_binding, item_count=1
            )
            assert base_manifest is not None
            v4_binding = {
                **fl_ref["manifest"]["binding"],
                "generation": fl_ref["manifest"]["binding"]["generation"] + 1,
            }
            packed_v4 = pack_lifecycle_v4(
                pub_journal,
                base=base_doc,
                manifest=base_manifest,
                current_binding=v4_binding,
            )
            v4_envelope = {
                "schema_version": "finding-lifecycle-evidence-v2",
                "lifecycle": packed_v4,
            }
            v4_manifest = inline_manifest(v4_envelope, binding=v4_binding, item_count=1)
            assert v4_manifest is not None
            v4_ref = {
                "schema_version": "finding-lifecycle-reference-v2",
                "operation_id": fl_ref["operation_id"],
                "previous_root_sha256": pub_rec.record_sha256,
                "previous_snapshot_sha256": fl_ref["manifest"]["sha256"],
                "manifest": v4_manifest,
                "accepted_semantic_history": fl_ref.get(
                    "accepted_semantic_history",
                    {
                        "schema_version": "accepted-semantic-history-v1",
                        "qualification": "unavailable",
                        "reason": "none",
                    },
                ),
            }

            # 2. Construct an intervening settled terminal command (`terminal_transaction=None`)
            # preserving `pub_rec.transaction` and carrying `v4_ref` (`finding-lifecycle-packed-v4`).
            pub_acc = ApiRunAccounting.from_document(pub_rec.api_run_accounting)
            cmd_identity = pub_acc.identity
            cmd_cfg = pub_acc.configuration
            cmd_owner = ApiExecutionOwner(1, "command", 0)
            cmd_plan = review_startup_plan("command")
            cmd_alloc = owner_allocations(512, cmd_plan.liability)
            cmd_acc = initial_accounting(
                cmd_identity,
                cmd_cfg,
                cmd_owner,
                cmd_plan,
                prior_nonstartup_liability_upper_bound=0,
            ).reserve("hosted-terminal", cmd_alloc.first_terminal)
            intent = {
                "purpose": "command",
                "source": {
                    "kind": "issue_comment",
                    "id": 901,
                    "updated_at": "2026-11-18T00:00:00Z",
                    "body_sha256": "5" * 64,
                },
                "run_id": cmd_identity.run_id,
                "owner": {"run_attempt": 1, "job": "command", "generation": 0},
                "head_sha": HEAD,
                "configuration_digest": cmd_cfg.digest,
                "operation_key": "a" * 64,
                "source_root": "b" * 64,
            }
            intent_digest = hashlib.sha256(canonical_bytes(intent)).hexdigest()
            outcome = {"kind": "applied", "reason": None}
            cmd_doc = cmd_acc.to_document()
            for blk in cmd_doc["blocks"]:
                blk["pending"] = []
            cmd_doc["terminal_transaction"] = None
            cmd_doc["action"] = {
                "phase": "pending",
                "execution_started": True,
                "intent": intent,
                "outcome": None,
                "result": None,
            }
            cmd_state_stub = ApiRunAccounting.from_document(cmd_doc)
            binding = action_result_binding(IDENTITY, cmd_state_stub, "github-bot:999")
            envelope = {
                "schema_version": "caller-action-result-v1",
                "intent_sha256": intent_digest,
                "outcome": outcome,
                "result": {"status": "ok"},
            }
            carrier = {
                "schema_version": "caller-action-result-v1",
                "payload": encode_evidence(
                    envelope, max_decoded_bytes=MAX_PARTITION_DECODED_BYTES
                ),
            }
            cmd_doc["action"] = {
                "phase": "terminal",
                "execution_started": True,
                "intent": intent,
                "outcome": outcome,
                "result": inline_manifest(carrier, binding=binding, item_count=1),
            }
            cmd_rec = clone_rec(
                pub_rec,
                generation=pub_rec.generation + 1,
                finding_lifecycle=v4_ref,
                api_run_accounting=cmd_doc,
            )

            # Store archived intervening terminal command using `_prepared_archive`.
            from review_sensei_github.api_run_history import _prepared_archive

            def archive_into(rec: SessionRecord, storage_id: int) -> dict[str, object]:
                st, arch_binding, arch_doc, arch_parts, arch_body = _prepared_archive(
                    ro_ledger, rec, None
                )
                f.extra_comments[storage_id] = {
                    "id": storage_id,
                    "body": arch_body,
                    "user": {"type": "Bot", "login": "reviewsensei[bot]", "id": 999},
                    "issue_url": "https://api.github.test/repos/owner/repo/issues/7",
                }
                arch_manifest = evidence_manifest(
                    arch_doc,
                    binding=arch_binding,
                    item_count=1,
                    parts=arch_parts,
                    storage_ids=(str(storage_id),),
                )
                return {
                    "run_id": st.identity.run_id,
                    "root_sha256": rec.record_sha256,
                    "manifest": arch_manifest,
                }

            archive_id = 7_000_001
            archive_ref = archive_into(cmd_rec, archive_id)

            # 3. Current session record is a clean pre-entry abort referencing `archive_ref`
            #    and carrying `v4_ref`.
            f.next_run()
            abort_run_identity = ApiRunIdentity(123, f.run_id)
            abort_tx = ReviewTransaction.create(
                repository=IDENTITY.repository,
                pull_request=IDENTITY.pull_request,
                base_sha=BASE,
                head_sha=HEAD,
                reservation_id="8" * 64,
                policy_digest="2" * 64,
                configuration_digest=cmd_cfg.digest,
                evidence_digest="3" * 64,
                generation=cmd_rec.generation + 1,
            )
            abort_acc = initial_accounting(
                abort_run_identity,
                cmd_cfg,
                ApiExecutionOwner(1, "hosted", 0),
                review_startup_plan("hosted"),
                prior_nonstartup_liability_upper_bound=0,
            )
            abort_doc = abort_acc.to_document()
            for blk in abort_doc["blocks"]:
                blk["pending"] = []
            abort_doc["previous_record"] = archive_ref
            abort_doc["analysis_abort"] = {
                "source_root": "1" * 64,
                "cleanup_root": "2" * 64,
                "transaction": abort_tx.to_dict(),
                "entry_state": "not_started",
                "run_id": abort_run_identity.run_id,
                "owner": {"run_attempt": 1, "job": "hosted", "generation": 0},
            }
            current_abort = clone_rec(
                pub_rec,
                generation=cmd_rec.generation + 2,
                transaction=None,
                reservation_id=None,
                reserved_slot=None,
                finding_lifecycle=v4_ref,
                api_run_accounting=abort_doc,
            )
            saved_comment = render_session_comment(
                repository_id=123, pull_request=7, record=current_abort
            )

            # Negative control A: unauthenticated/non-terminal intervening record
            # (`terminal_transaction=None`, `action=None`) fails closed!
            unclosed_doc = copy.deepcopy(cmd_doc)
            unclosed_doc["action"] = None
            unclosed_cmd_rec = clone_rec(cmd_rec, api_run_accounting=unclosed_doc)
            abort_doc_bad = copy.deepcopy(abort_doc)
            abort_doc_bad["previous_record"] = archive_into(
                unclosed_cmd_rec, archive_id
            )
            current_bad = clone_rec(current_abort, api_run_accounting=abort_doc_bad)
            f.comment["body"] = render_session_comment(
                repository_id=123, pull_request=7, record=current_bad
            )
            f.next_run()
            self._assert_held(f, "late native effect on the aborted scope")

            # Negative control B: corrupt intervening action result -> fails closed!
            corrupt_doc = copy.deepcopy(cmd_doc)
            corrupt_doc["action"]["outcome"] = {
                "kind": "explicit_noop",
                "reason": sorted(ACTION_NOOP_REASONS)[0],
            }
            corrupt_rec = clone_rec(cmd_rec, api_run_accounting=corrupt_doc)
            abort_doc_corrupt = copy.deepcopy(abort_doc)
            abort_doc_corrupt["previous_record"] = archive_into(corrupt_rec, archive_id)
            current_corrupt = clone_rec(
                current_abort, api_run_accounting=abort_doc_corrupt
            )
            f.comment["body"] = render_session_comment(
                repository_id=123, pull_request=7, record=current_corrupt
            )
            f.next_run()
            self._assert_held(f, "earlier native effect evidence is unavailable")

            # Positive: restore authentic command archive + packed-v4 lifecycle -> real rollover succeeds!
            abort_doc["previous_record"] = archive_into(cmd_rec, archive_id)
            f.comment["body"] = saved_comment
            f.next_run()
            self.assertEqual(f.invoke(), 0, f.error)


if __name__ == "__main__":
    unittest.main()

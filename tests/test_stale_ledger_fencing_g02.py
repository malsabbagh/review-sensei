"""Qualification tests for G02: atomic writer containment and stale ledger PATCH fencing."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from review_sensei_github import GitHubHttp
from review_sensei_github.session_ledger import (
    GitHubIssueCommentSessionLedger,
    parse_session_comment,
    render_session_comment,
)

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.recovery_authority import (
    Access,
    AuthorityRefusal,
    Dispatch,
    Funding,
    Receipt,
)
from review_sensei.session import SessionIdentity, SessionRecord
from review_sensei.writer_containment import (
    BYPASS_WRITER_ROUTES,
    CONTAINED_WRITER_ROUTES,
    LIVE_CANONICAL_CUTOVER_ENABLED,
    WRITER_CREDENTIAL_ROUTE_INVENTORY,
    ContainedForwardingCoordinator,
    IssuedWriterToken,
    WireDispatchOutcome,
    WriterContainmentPosture,
    evaluate_physical_budget_guarantees,
    evaluate_side_coordinator_fencing_proof,
    extract_unsettled_dispatches,
    inspect_writer_credential_inventory,
)

try:
    from fake_github_http import FakeHTTPResponse
    from session_fixture_clock import SESSION_FIXTURE_NOW
except ModuleNotFoundError:
    from tests.fake_github_http import FakeHTTPResponse
    from tests.session_fixture_clock import SESSION_FIXTURE_NOW


FIXED_NOW = SESSION_FIXTURE_NOW
PR_IDENTITY = SessionIdentity(
    "example/repo",
    7,
    repository_id=123,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["REVIEWSENSEI_ALLOW_CHECKOUT_IMPORT"] = "1"
    search_paths = [
        str(REPO_ROOT / "src"),
        str(REPO_ROOT / "packages/python/review-sensei-github/src"),
    ]
    existing = env.get("PYTHONPATH")
    if existing:
        search_paths.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(search_paths)
    return env


def _make_access(
    *,
    run_id: int = 41,
    attempt: int = 1,
    caller_tag: str = "b",
    native_exclusive: bool = True,
) -> Access:
    return Access(
        repository="example/repo",
        repository_id=123,
        pull_request=7,
        store_id="a" * 64,
        epoch=1,
        caller_sha256=caller_tag * 64,
        source_sha256="c" * 64,
        head_sha="d" * 40,
        configuration_sha256="e" * 64,
        run_id=run_id,
        attempt=attempt,
        actor_id=99,
        actor_role="admin",
        workflow_sha="5" * 40,
        policy_commit="6" * 40,
        funding=Funding("f" * 64, 512, 50, 100000, 0, 0, 0),
        native_exclusive=native_exclusive,
    )


class StatefulGitHubCommentServer:
    """Thread-safe synthetic GitHub issue-comment server for stale-PATCH races."""

    def __init__(self, initial_record: SessionRecord) -> None:
        self._lock = threading.Lock()
        self.comment_id = 701
        self.comment_body = render_session_comment(
            repository_id=123,
            pull_request=7,
            record=initial_record,
        )
        self.patch_history: list[int] = []

    def current_record(self) -> SessionRecord:
        with self._lock:
            parsed = parse_session_comment(self.comment_body, identity=PR_IDENTITY)
        assert parsed is not None
        return parsed

    def handle(
        self, method: str, path: str, raw_body: bytes | None
    ) -> tuple[int, bytes]:
        with self._lock:
            if method == "GET" and path == "/repos/example/repo":
                return 200, json.dumps({"id": 123, "full_name": "example/repo"}).encode(
                    "utf-8"
                )
            if method == "GET" and path == "/repos/example/repo/pulls/7":
                return (
                    200,
                    json.dumps(
                        {
                            "number": 7,
                            "state": "open",
                            "head": {"sha": "d" * 40},
                            "base": {"repo": {"id": 123}},
                        }
                    ).encode("utf-8"),
                )
            if method == "GET" and path.startswith(
                "/repos/example/repo/issues/7/comments"
            ):
                return (
                    200,
                    json.dumps(
                        [
                            {
                                "id": self.comment_id,
                                "body": self.comment_body,
                                "user": {"type": "Bot", "login": "github-actions[bot]"},
                            }
                        ]
                    ).encode("utf-8"),
                )
            if (
                method == "PATCH"
                and path == f"/repos/example/repo/issues/comments/{self.comment_id}"
            ):
                assert raw_body is not None
                payload = json.loads(raw_body.decode("utf-8"))
                self.comment_body = str(payload["body"])
                parsed = parse_session_comment(self.comment_body, identity=PR_IDENTITY)
                if parsed is not None:
                    self.patch_history.append(parsed.generation)
                return (
                    200,
                    json.dumps(
                        {
                            "id": self.comment_id,
                            "body": self.comment_body,
                        }
                    ).encode("utf-8"),
                )
            raise AssertionError(f"unexpected request: {method} {path}")

    def raw_http(
        self,
        *,
        before_patch: threading.Event | None = None,
        release_patch: threading.Event | None = None,
    ) -> GitHubHttp:
        def opener(request, timeout):
            parsed = urlsplit(request.full_url)
            path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
            if request.method == "PATCH" and before_patch is not None:
                before_patch.set()
                assert release_patch is not None
                if not release_patch.wait(timeout=10):
                    raise TimeoutError("timed out waiting to release delayed PATCH")
            status, body = self.handle(request.method, path, request.data)
            return FakeHTTPResponse(body, status)

        return GitHubHttp(api_url="https://api.github.test", opener=opener)

    def fenced_http(
        self,
        coordinator: ContainedForwardingCoordinator,
        token: IssuedWriterToken,
        *,
        writer_label: str,
        before_patch: threading.Event | None = None,
        release_patch: threading.Event | None = None,
    ) -> GitHubHttp:
        counter = {"index": 0}

        def opener(request, timeout):
            parsed = urlsplit(request.full_url)
            path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
            if request.method == "GET":
                status, body = self.handle("GET", path, None)
                return FakeHTTPResponse(body, status)
            if request.method == "PATCH":
                if before_patch is not None:
                    before_patch.set()
                    assert release_patch is not None
                    if not release_patch.wait(timeout=10):
                        raise TimeoutError("timed out waiting to release fenced PATCH")
                counter["index"] += 1
                idx = counter["index"]
                payload = json.loads((request.data or b"{}").decode("utf-8"))
                reservation_digest = hashlib.sha256(
                    f"{writer_label}:{token.epoch}:{idx}".encode("utf-8")
                ).hexdigest()
                outcome, _snapshot = coordinator.fenced_http_mutation(
                    token,
                    dispatch_id=f"{writer_label}-patch-{idx}",
                    operation="session_patch",
                    method="PATCH",
                    target=parsed.path,
                    payload=payload,
                    reservation_sha256=reservation_digest,
                )
                return FakeHTTPResponse(outcome.response_body, outcome.status_code)
            raise AssertionError(f"unsupported HTTP method: {request.method}")

        return GitHubHttp(api_url="https://api.github.test", opener=opener)


class StaleLedgerFencingG02Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _wire_forwarder(self, server: StatefulGitHubCommentServer):
        def forward(
            method: str, target: str, request_bytes: bytes, binding: dict[str, object]
        ) -> WireDispatchOutcome:
            status, response_bytes = server.handle(method, target, request_bytes)
            return WireDispatchOutcome(
                status_code=status,
                response_body=response_bytes,
                effect_confirmed=200 <= status < 300,
                response_complete=True,
                usage_calls=1,
            )

        return forward

    def test_live_cutover_is_disabled_by_default(self) -> None:
        self.assertFalse(LIVE_CANONICAL_CUTOVER_ENABLED)
        server = StatefulGitHubCommentServer(
            SessionRecord.create(PR_IDENTITY, now=FIXED_NOW)
        )
        coordinator = ContainedForwardingCoordinator(
            self.dir / "default-disabled",
            posture=WriterContainmentPosture.fully_contained(),
            wire_forwarder=self._wire_forwarder(server),
        )
        with self.assertRaisesRegex(
            AuthorityRefusal, "live canonical-authority cutover is disabled by default"
        ):
            coordinator.enroll_initial_writer(
                base_access=_make_access(native_exclusive=True),
                writer_route="actions_hosted",
            )
        token, snapshot = coordinator.enroll_initial_writer(
            base_access=_make_access(native_exclusive=False),
            writer_route="actions_hosted",
        )
        self.assertEqual(token.epoch, 1)
        self.assertFalse(snapshot.native_authority)

    def test_all_writer_routes_and_unfenced_broker_containment_posture(self) -> None:
        server = StatefulGitHubCommentServer(
            SessionRecord.create(PR_IDENTITY, now=FIXED_NOW)
        )
        for index, route in enumerate(sorted(CONTAINED_WRITER_ROUTES), start=1):
            with self.subTest(route=route):
                coordinator = ContainedForwardingCoordinator(
                    self.dir / f"route-{index}",
                    posture=WriterContainmentPosture.fully_contained(),
                    wire_forwarder=self._wire_forwarder(server),
                    synthetic_qualification=True,
                )
                token, snapshot = coordinator.enroll_initial_writer(
                    base_access=_make_access(run_id=100 + index),
                    writer_route=route,
                )
                self.assertEqual(token.writer_route, route)
                self.assertEqual(snapshot.document()["revision"], 0)

        for bypass_route in sorted(BYPASS_WRITER_ROUTES) + ["unknown_writer"]:
            with self.subTest(bypass_route=bypass_route):
                coordinator = ContainedForwardingCoordinator(
                    self.dir / f"bypass-{bypass_route}",
                    posture=WriterContainmentPosture.fully_contained(),
                    wire_forwarder=self._wire_forwarder(server),
                    synthetic_qualification=True,
                )
                with self.assertRaises(AuthorityRefusal):
                    coordinator.enroll_initial_writer(
                        base_access=_make_access(),
                        writer_route=bypass_route,
                    )

        for field_name in (
            "broker_raw_write_tokens_disabled",
            "manual_cli_raw_write_tokens_disabled",
            "actions_raw_write_tokens_disabled",
            "all_mutations_routed_via_forwarder",
            "pre_cutover_inflight_drained",
        ):
            with self.subTest(uncontained_field=field_name):
                posture = replace(
                    WriterContainmentPosture.fully_contained(),
                    **{field_name: False},
                )
                self.assertFalse(posture.is_fully_contained())
                coordinator = ContainedForwardingCoordinator(
                    self.dir / f"posture-{field_name}",
                    posture=posture,
                    wire_forwarder=self._wire_forwarder(server),
                    synthetic_qualification=True,
                )
                with self.assertRaisesRegex(
                    AuthorityRefusal, "defeats canonical writer containment"
                ):
                    coordinator.enroll_initial_writer(
                        base_access=_make_access(),
                        writer_route="actions_hosted",
                    )

    def test_negative_characterization_unfenced_broker_allows_delayed_wire_rollback(
        self,
    ) -> None:
        """Negative threat characterization (pre-cutover): unfenced raw-token PATCH clobbers GitHub.

        This test intentionally characterizes why advisory read-before-write on raw
        broker tokens cannot stop a delayed network PATCH; positive fencing closure is
        verified in ``test_contained_coordinator_fences_delayed_stale_ledger_patch``.
        """

        initial = SessionRecord.create(PR_IDENTITY, now=FIXED_NOW).evolve(
            generation=1, now=FIXED_NOW
        )
        server = StatefulGitHubCommentServer(initial)

        old_at_wire = threading.Event()
        release_old = threading.Event()
        self.addCleanup(release_old.set)
        old_ledger = GitHubIssueCommentSessionLedger(
            server.raw_http(before_patch=old_at_wire, release_patch=release_old),
            token="raw-broker-token-old",
        )
        successor_ledger = GitHubIssueCommentSessionLedger(
            server.raw_http(),
            token="raw-broker-token-new",
        )

        old_errors: list[BaseException] = []

        def run_old_writer() -> None:
            try:
                old_ledger.replace(
                    PR_IDENTITY,
                    lambda cur: cur.evolve(
                        operator_paused=True,
                        generation=cur.generation + 1,
                        now=FIXED_NOW,
                    ),
                    now=FIXED_NOW,
                )
            except BaseException as exc:  # pragma: no cover - asserted empty below
                old_errors.append(exc)

        worker = threading.Thread(target=run_old_writer)
        worker.start()
        self.assertTrue(old_at_wire.wait(timeout=5))

        # Successor advances session comment from generation 1 -> 2 -> 3 while old PATCH is delayed.
        for _ in range(2):
            successor_ledger.replace(
                PR_IDENTITY,
                lambda cur: cur.evolve(
                    operator_paused=False,
                    generation=cur.generation + 1,
                    now=FIXED_NOW,
                ),
                now=FIXED_NOW,
            )
        self.assertEqual(server.current_record().generation, 3)

        # Delayed old wire PATCH lands unconditionally on GitHub and clobbers generation 3 back to 2.
        release_old.set()
        worker.join(timeout=5)
        self.assertEqual(old_errors, [])
        self.assertEqual(server.current_record().generation, 2)
        self.assertTrue(server.current_record().operator_paused)
        self.assertEqual(server.patch_history, [2, 3, 2])

    def test_negative_characterization_unfenced_sidecar_refused_and_unrevoked_bypass_documented(
        self,
    ) -> None:
        """Negative threat characterization: unfenced/undrained posture refuses cutover; raw bypass clobbers.

        Verifies both that ``WriterContainmentPosture`` refuses exclusive enrollment when
        broker raw tokens are enabled OR pre-cutover in-flight writers are undrained, and
        documents why physical credential containment is mandatory rather than optional.
        """

        initial = SessionRecord.create(PR_IDENTITY, now=FIXED_NOW).evolve(
            generation=1, now=FIXED_NOW
        )
        server = StatefulGitHubCommentServer(initial)
        for bad_posture in (
            WriterContainmentPosture.unfenced_broker(),
            replace(
                WriterContainmentPosture.fully_contained(),
                pre_cutover_inflight_drained=False,
            ),
        ):
            coordinator = ContainedForwardingCoordinator(
                self.dir
                / f"unfenced-sidecar-{bad_posture.pre_cutover_inflight_drained}",
                posture=bad_posture,
                wire_forwarder=self._wire_forwarder(server),
                synthetic_qualification=True,
            )
            with self.assertRaisesRegex(
                AuthorityRefusal, "defeats canonical writer containment"
            ):
                coordinator.enroll_initial_writer(
                    base_access=_make_access(native_exclusive=True),
                    writer_route="actions_hosted",
                )

        # Even if a successor uses a fenced coordinator, an old worker still holding a
        # broker-issued raw token bypasses the coordinator and overwrites the comment.
        contained = ContainedForwardingCoordinator(
            self.dir / "contained-for-successor",
            posture=WriterContainmentPosture.fully_contained(),
            wire_forwarder=self._wire_forwarder(server),
            synthetic_qualification=True,
        )
        successor_token, _ = contained.enroll_initial_writer(
            base_access=_make_access(run_id=50, caller_tag="9"),
            writer_route="actions_hosted",
        )
        old_at_wire = threading.Event()
        release_old = threading.Event()
        self.addCleanup(release_old.set)
        bypass_old_ledger = GitHubIssueCommentSessionLedger(
            server.raw_http(before_patch=old_at_wire, release_patch=release_old),
            token="unrevoked-raw-broker-token",
        )
        fenced_successor_ledger = GitHubIssueCommentSessionLedger(
            server.fenced_http(contained, successor_token, writer_label="succ"),
            token="capability-placeholder",
        )

        worker = threading.Thread(
            target=lambda: bypass_old_ledger.replace(
                PR_IDENTITY,
                lambda cur: cur.evolve(
                    operator_paused=True,
                    generation=cur.generation + 1,
                    now=FIXED_NOW,
                ),
                now=FIXED_NOW,
            )
        )
        worker.start()
        self.assertTrue(old_at_wire.wait(timeout=5))
        for _ in range(2):
            fenced_successor_ledger.replace(
                PR_IDENTITY,
                lambda cur: cur.evolve(
                    operator_paused=False,
                    generation=cur.generation + 1,
                    now=FIXED_NOW,
                ),
                now=FIXED_NOW,
            )
        self.assertEqual(server.current_record().generation, 3)
        release_old.set()
        worker.join(timeout=5)
        self.assertEqual(server.current_record().generation, 2)

    def test_contained_coordinator_fences_delayed_stale_ledger_patch(self) -> None:
        """Prove canonical authority fence rejects delayed old PATCH and preserves generation 3."""

        initial = SessionRecord.create(PR_IDENTITY, now=FIXED_NOW).evolve(
            generation=1, now=FIXED_NOW
        )
        server = StatefulGitHubCommentServer(initial)
        coordinator = ContainedForwardingCoordinator(
            self.dir / "fenced-race",
            posture=WriterContainmentPosture.fully_contained(),
            wire_forwarder=self._wire_forwarder(server),
            synthetic_qualification=True,
        )
        epoch1_token, _ = coordinator.enroll_initial_writer(
            base_access=_make_access(run_id=41, attempt=1, caller_tag="1"),
            writer_route="actions_hosted",
        )

        old_at_patch = threading.Event()
        release_old = threading.Event()
        self.addCleanup(release_old.set)
        old_ledger = GitHubIssueCommentSessionLedger(
            server.fenced_http(
                coordinator,
                epoch1_token,
                writer_label="epoch1",
                before_patch=old_at_patch,
                release_patch=release_old,
            ),
            token="capability-only",
        )

        old_outcomes: list[BaseException] = []

        def run_epoch1() -> None:
            try:
                old_ledger.replace(
                    PR_IDENTITY,
                    lambda cur: cur.evolve(
                        operator_paused=True,
                        generation=cur.generation + 1,
                        now=FIXED_NOW,
                    ),
                    now=FIXED_NOW,
                )
            except BaseException as exc:
                old_outcomes.append(exc)

        worker = threading.Thread(target=run_epoch1)
        worker.start()
        self.assertTrue(old_at_patch.wait(timeout=5))

        # Cancellation without cessation cannot advance epoch.
        with self.assertRaisesRegex(AuthorityRefusal, "cancellation is not cessation"):
            coordinator.advance_epoch(
                predecessor_token=epoch1_token,
                successor_access=_make_access(run_id=42, attempt=1, caller_tag="2"),
                writer_route="actions_command",
                predecessor_ceased=False,
            )

        # Once predecessor authority is fenced on epoch cutover, successor advances 1 -> 2 -> 3.
        epoch2_token, _ = coordinator.advance_epoch(
            predecessor_token=epoch1_token,
            successor_access=_make_access(run_id=42, attempt=1, caller_tag="2"),
            writer_route="actions_command",
            predecessor_ceased=True,
        )
        self.assertEqual(
            coordinator.active_epoch(
                base_store_id="a" * 64, repository_id=123, pull_request=7
            ),
            2,
        )
        successor_ledger = GitHubIssueCommentSessionLedger(
            server.fenced_http(coordinator, epoch2_token, writer_label="epoch2"),
            token="capability-only",
        )
        for _ in range(2):
            successor_ledger.replace(
                PR_IDENTITY,
                lambda cur: cur.evolve(
                    operator_paused=False,
                    generation=cur.generation + 1,
                    now=FIXED_NOW,
                ),
                now=FIXED_NOW,
            )
        self.assertEqual(server.current_record().generation, 3)

        # Delayed Epoch 1 wakes up and is deterministically fenced BEFORE wire PATCH.
        release_old.set()
        worker.join(timeout=5)
        self.assertEqual(len(old_outcomes), 1)
        self.assertIsInstance(old_outcomes[0], AuthorityRefusal)
        self.assertIn("stale writer epoch fenced", str(old_outcomes[0]))
        self.assertEqual(server.current_record().generation, 3)
        self.assertFalse(server.current_record().operator_paused)
        self.assertEqual(server.patch_history, [2, 3])

    def test_prepared_old_dispatch_is_fenced_at_forward_after_epoch_cutover(
        self,
    ) -> None:
        server = StatefulGitHubCommentServer(
            SessionRecord.create(PR_IDENTITY, now=FIXED_NOW)
        )
        coordinator = ContainedForwardingCoordinator(
            self.dir / "prepared-then-cutover",
            posture=WriterContainmentPosture.fully_contained(),
            wire_forwarder=self._wire_forwarder(server),
            synthetic_qualification=True,
        )
        token1, root1 = coordinator.enroll_initial_writer(
            base_access=_make_access(run_id=41, caller_tag="1"),
            writer_route="actions_local",
        )
        body_bytes = canonical_bytes({"body": "stale-prepared"})
        req1 = Dispatch(
            dispatch_id="stale-prepared-1",
            operation="session_patch",
            method="PATCH",
            target=f"/repos/example/repo/issues/comments/{server.comment_id}",
            body=body_bytes,
            effect_sha256="1" * 64,
            reservation_sha256="2" * 64,
            liability_calls=4,
            deadline_ms=90000,
        )
        prepared1 = coordinator.candidate.prepare(token1, root1, req1)

        token2, _root2 = coordinator.advance_epoch(
            predecessor_token=token1,
            successor_access=_make_access(run_id=42, caller_tag="2"),
            writer_route="manual_cli",
            predecessor_ceased=True,
        )
        self.assertEqual(token2.epoch, 2)

        with self.assertRaisesRegex(
            AuthorityRefusal, "stale writer epoch fenced before physical dispatch"
        ):
            coordinator.candidate.forward(token1, prepared1, req1)
        self.assertEqual(server.patch_history, [])

    def test_independent_process_forward_invalidates_cutover_preflight(self) -> None:
        """A different host may dispatch after cutover's initial snapshot."""
        state_dir = self.dir / "concurrent-cutover"
        coord = ContainedForwardingCoordinator(
            state_dir,
            posture=WriterContainmentPosture.fully_contained(),
            wire_forwarder=lambda *_: WireDispatchOutcome(200, b"{}"),
            synthetic_qualification=True,
        )
        access = _make_access()
        token, root = coord.enroll_initial_writer(
            base_access=access, writer_route="actions_hosted"
        )
        request = Dispatch(
            dispatch_id="concurrent-forward",
            operation="reply",
            method="POST",
            target="/repos/example/repo/issues/7/comments",
            body=canonical_bytes({"body": "once"}),
            effect_sha256="5" * 64,
            reservation_sha256="6" * 64,
            liability_calls=9,
            deadline_ms=90000,
        )
        prepared = coord.candidate.prepare(token, root, request)
        script = r"""
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from test_stale_ledger_fencing_g02 import _make_access
from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.recovery_authority import Dispatch
from review_sensei.writer_containment import (
    ContainedForwardingCoordinator, WriterContainmentPosture, WireDispatchOutcome,
)
directory = Path(sys.argv[1])
def delayed_wire(*args):
    (directory / "wire-entered").write_text("1", encoding="ascii")
    if sys.stdin.readline() != "release\n":
        raise RuntimeError("wire release missing")
    return WireDispatchOutcome(504, b"", response_complete=False, usage_calls=None)
coord = ContainedForwardingCoordinator(
    directory, posture=WriterContainmentPosture.fully_contained(),
    wire_forwarder=delayed_wire, synthetic_qualification=True,
)
token = coord.reattach_active_writer(
    base_access=_make_access(), writer_route="actions_hosted",
)
request = Dispatch(
    dispatch_id="concurrent-forward", operation="reply", method="POST",
    target="/repos/example/repo/issues/7/comments",
    body=canonical_bytes({"body": "once"}), effect_sha256="5" * 64,
    reservation_sha256="6" * 64, liability_calls=9, deadline_ms=90000,
)
coord.candidate.forward(token, coord.candidate.snapshot(token), request)
"""
        initialize = coord.candidate.initialize
        processes = []

        def interleaved_initialize(*args, **kwargs):
            successor = initialize(*args, **kwargs)
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    script,
                    str(state_dir),
                    str(REPO_ROOT / "tests"),
                ],
                env=_child_env(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            processes.append(proc)
            deadline = time.monotonic() + 15
            while not (state_dir / "wire-entered").exists():
                if proc.poll() is not None or time.monotonic() >= deadline:
                    self.fail("independent host did not enter the physical wire")
                time.sleep(0.01)
            return successor

        try:
            with patch.object(coord.candidate, "initialize", interleaved_initialize):
                with self.assertRaisesRegex(AuthorityRefusal, "predecessor.*changed"):
                    coord.advance_epoch(
                        predecessor_token=token,
                        successor_access=_make_access(run_id=42, caller_tag="2"),
                        writer_route="actions_hosted",
                        predecessor_ceased=True,
                    )
            self.assertEqual(
                coord.active_epoch(
                    base_store_id=access.store_id,
                    repository_id=123,
                    pull_request=7,
                ),
                1,
            )
            current = coord.candidate.snapshot(token)
            self.assertEqual(
                current.document()["held_calls"], prepared.document()["held_calls"]
            )
            self.assertEqual(
                current.document()["dispatches"][request.dispatch_id]["phase"],
                "forwarded",
            )
            self.assertTrue(extract_unsettled_dispatches(current))
        finally:
            for proc in processes:
                try:
                    _, stderr = proc.communicate("release\n", timeout=15)
                    self.assertEqual(proc.returncode, 0, stderr)
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.communicate()

    def test_unsettled_inflight_dispatch_blocks_epoch_cutover_until_settled(
        self,
    ) -> None:
        def uncertain_wire(
            method: str, target: str, request_bytes: bytes, binding: dict[str, object]
        ) -> WireDispatchOutcome:
            return WireDispatchOutcome(
                status_code=504,
                response_body=b"",
                effect_confirmed=True,
                response_complete=False,
                usage_calls=None,
            )

        coordinator = ContainedForwardingCoordinator(
            self.dir / "unsettled-cutover",
            posture=WriterContainmentPosture.fully_contained(),
            wire_forwarder=uncertain_wire,
            synthetic_qualification=True,
            allow_synthetic_overrides=True,
        )
        token1, root1 = coordinator.enroll_initial_writer(
            base_access=_make_access(run_id=41, caller_tag="1"),
            writer_route="actions_hosted",
        )
        req = Dispatch(
            dispatch_id="inflight-1",
            operation="reply",
            method="POST",
            target="/repos/example/repo/issues/7/comments",
            body=canonical_bytes({"body": "ack"}),
            effect_sha256="3" * 64,
            reservation_sha256="4" * 64,
            liability_calls=5,
            deadline_ms=90000,
        )
        prepared = coordinator.candidate.prepare(token1, root1, req)
        forwarded = coordinator.candidate.forward(token1, prepared, req)

        # Forwarded (unsettled) blocks epoch takeover.
        with self.assertRaisesRegex(
            AuthorityRefusal, "predecessor epoch has unsettled physical dispatch"
        ):
            coordinator.advance_epoch(
                predecessor_token=token1,
                successor_access=_make_access(run_id=42, caller_tag="2"),
                writer_route="actions_hosted",
                predecessor_ceased=True,
            )

        # Effect-confirmed without complete usage also blocks epoch takeover.
        effect_only = coordinator.candidate.reconcile(
            token1, forwarded, req.dispatch_id
        )
        self.assertEqual(
            effect_only.document()["dispatches"][req.dispatch_id]["phase"],
            "effect-confirmed",
        )
        with self.assertRaisesRegex(
            AuthorityRefusal, "predecessor epoch has unsettled physical dispatch"
        ):
            coordinator.advance_epoch(
                predecessor_token=token1,
                successor_access=_make_access(run_id=42, caller_tag="2"),
                writer_route="actions_hosted",
                predecessor_ceased=True,
            )

        # Once reconciled with complete usage receipt, epoch advance succeeds cleanly.
        binding = effect_only.document()["dispatches"][req.dispatch_id]["binding"]
        coordinator.set_observation_override(
            req.dispatch_id,
            Receipt(
                binding_sha256=hashlib.sha256(canonical_bytes(binding)).hexdigest(),
                effect_confirmed=True,
                response_complete=True,
                usage_calls=2,
                response_bytes=18,
            ),
            token=token1,
        )
        settled = coordinator.candidate.reconcile(token1, effect_only, req.dispatch_id)
        self.assertEqual(
            settled.document()["dispatches"][req.dispatch_id]["phase"], "settled"
        )
        token2, _ = coordinator.advance_epoch(
            predecessor_token=token1,
            successor_access=_make_access(run_id=42, caller_tag="2"),
            writer_route="actions_hosted",
            predecessor_ceased=True,
        )
        self.assertEqual(token2.epoch, 2)

    def test_crash_before_and_after_frontier_and_restored_db_rollback_detection(
        self,
    ) -> None:
        server = StatefulGitHubCommentServer(
            SessionRecord.create(PR_IDENTITY, now=FIXED_NOW)
        )

        # 1. Crash BEFORE frontier advance leaves prior checkpoint clean and usable.
        before_dir = self.dir / "crash-before-frontier"
        coord_before = ContainedForwardingCoordinator(
            before_dir,
            posture=WriterContainmentPosture.fully_contained(),
            wire_forwarder=self._wire_forwarder(server),
            synthetic_qualification=True,
        )
        token_b, root_b = coord_before.enroll_initial_writer(
            base_access=_make_access(run_id=61),
            writer_route="actions_hosted",
        )
        req_b = Dispatch(
            dispatch_id="before-1",
            operation="reply",
            method="POST",
            target="/repos/example/repo/issues/7/comments",
            body=canonical_bytes({"body": "hello"}),
            effect_sha256="1" * 64,
            reservation_sha256="2" * 64,
            liability_calls=4,
            deadline_ms=90000,
        )
        with (
            patch.object(
                coord_before,
                "advance",
                side_effect=OSError("crash before frontier commit"),
            ),
            self.assertRaises(OSError),
        ):
            coord_before.candidate.prepare(token_b, root_b, req_b)
        self.assertEqual(coord_before.candidate.snapshot(token_b), root_b)

        # 2. Crash AFTER frontier advance (uncertain SQLite commit) holds identity fail-closed.
        after_dir = self.dir / "crash-after-frontier"
        coord_after = ContainedForwardingCoordinator(
            after_dir,
            posture=WriterContainmentPosture.fully_contained(),
            wire_forwarder=self._wire_forwarder(server),
            synthetic_qualification=True,
        )
        token_a, root_a = coord_after.enroll_initial_writer(
            base_access=_make_access(run_id=62),
            writer_route="actions_hosted",
        )
        with (
            patch.object(
                coord_after.candidate,
                "_commit_database",
                side_effect=OSError("sqlite commit lost after frontier advance"),
            ),
            self.assertRaises(OSError),
        ):
            coord_after.candidate.prepare(token_a, root_a, req_b)
        with self.assertRaisesRegex(
            AuthorityRefusal, "nonrollback frontier is unavailable or divergent"
        ):
            coord_after.candidate.snapshot(token_a)
        restarted_after = ContainedForwardingCoordinator(
            after_dir,
            posture=WriterContainmentPosture.fully_contained(),
            wire_forwarder=self._wire_forwarder(server),
            synthetic_qualification=True,
        )
        with self.assertRaisesRegex(
            AuthorityRefusal, "nonrollback frontier is unavailable or divergent"
        ):
            restarted_after.reattach_active_writer(
                base_access=_make_access(run_id=62),
                writer_route="actions_hosted",
            )

        # 3. Restored SQLite DB rollback after physical forward is caught by frontier.sqlite.
        rollback_dir = self.dir / "db-rollback"
        wire_calls: list[str] = []

        def counting_wire(
            method: str, target: str, request_bytes: bytes, binding: dict[str, object]
        ) -> WireDispatchOutcome:
            wire_calls.append(str(binding["dispatch_id"]))
            return WireDispatchOutcome(status_code=200, response_body=b'{"id":1}')

        coord_rb = ContainedForwardingCoordinator(
            rollback_dir,
            posture=WriterContainmentPosture.fully_contained(),
            wire_forwarder=counting_wire,
            synthetic_qualification=True,
        )
        token_r, root_r = coord_rb.enroll_initial_writer(
            base_access=_make_access(run_id=63),
            writer_route="actions_hosted",
        )
        prepared_r = coord_rb.candidate.prepare(token_r, root_r, req_b)
        backup_db = rollback_dir / "pre_forward_backup.sqlite"
        with (
            closing(sqlite3.connect(coord_rb.authority_path)) as src,
            closing(sqlite3.connect(backup_db)) as dst,
        ):
            src.backup(dst)
        coord_rb.candidate.forward(token_r, prepared_r, req_b)
        self.assertEqual(wire_calls, ["before-1"])

        # Restore authority.sqlite to pre-forward state; frontier.sqlite catches rollback!
        with (
            closing(sqlite3.connect(backup_db)) as src,
            closing(sqlite3.connect(coord_rb.authority_path)) as dst,
        ):
            src.backup(dst)
        with self.assertRaisesRegex(
            AuthorityRefusal, "nonrollback frontier is unavailable or divergent"
        ):
            coord_rb.candidate.forward(token_r, prepared_r, req_b)
        self.assertEqual(wire_calls, ["before-1"])

    def test_subprocess_kill_after_physical_effect_preserves_unknown_usage_and_blocks_resend(
        self,
    ) -> None:
        kill_dir = self.dir / "subprocess-kill"
        coord = ContainedForwardingCoordinator(
            kill_dir,
            posture=WriterContainmentPosture.fully_contained(),
            wire_forwarder=lambda *_: WireDispatchOutcome(200, b"{}"),
            synthetic_qualification=True,
        )
        access = _make_access(run_id=71, caller_tag="7")
        token, root = coord.enroll_initial_writer(
            base_access=access,
            writer_route="actions_hosted",
        )
        req = Dispatch(
            dispatch_id="kill-after-effect",
            operation="reply",
            method="POST",
            target="/repos/example/repo/issues/7/comments",
            body=canonical_bytes({"body": "once"}),
            effect_sha256="5" * 64,
            reservation_sha256="6" * 64,
            liability_calls=9,
            deadline_ms=90000,
        )
        prepared = coord.candidate.prepare(token, root, req)
        debited_held_calls = prepared.document()["held_calls"]

        script = """
import json, os, signal, sys
from pathlib import Path
from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.recovery_authority import Access, Dispatch, Funding
from review_sensei.writer_containment import ContainedForwardingCoordinator, WriterContainmentPosture

state_dir = Path(sys.argv[1])
def effect_then_sigkill(method, target, request_bytes, binding):
    (state_dir / "physical_effect.json").write_text(json.dumps(binding), encoding="utf-8")
    if os.name != "nt":
        os.kill(os.getpid(), signal.SIGKILL)
    os._exit(79)

coord = ContainedForwardingCoordinator(
    state_dir,
    posture=WriterContainmentPosture.fully_contained(),
    wire_forwarder=effect_then_sigkill,
    synthetic_qualification=True,
)
access = Access(
    repository="example/repo",
    repository_id=123,
    pull_request=7,
    store_id="a" * 64,
    epoch=1,
    caller_sha256="7" * 64,
    source_sha256="c" * 64,
    head_sha="d" * 40,
    configuration_sha256="e" * 64,
    run_id=71,
    attempt=1,
    actor_id=99,
    actor_role="admin",
    workflow_sha="5" * 40,
    policy_commit="6" * 40,
    funding=Funding("f" * 64, 512, 50, 100000, 0, 0, 0),
    native_exclusive=True,
)
tok = coord.reattach_active_writer(base_access=access, writer_route="actions_hosted")
req = Dispatch(
    dispatch_id="kill-after-effect",
    operation="reply",
    method="POST",
    target="/repos/example/repo/issues/7/comments",
    body=canonical_bytes({"body": "once"}),
    effect_sha256="5" * 64,
    reservation_sha256="6" * 64,
    liability_calls=9,
    deadline_ms=90000,
)
coord.candidate.forward(tok, coord.candidate.snapshot(tok), req)
"""
        proc = subprocess.run(
            [sys.executable, "-c", script, str(kill_dir)],
            env=_child_env(),
            capture_output=True,
            timeout=15,
            check=False,
        )
        expected_codes = (
            {79}
            if os.name == "nt"
            else {-int(signal.SIGKILL), 128 + int(signal.SIGKILL)}
        )
        self.assertIn(
            proc.returncode,
            expected_codes,
            proc.stderr.decode("utf-8"),
        )
        self.assertTrue((kill_dir / "physical_effect.json").is_file())

        restarted = ContainedForwardingCoordinator(
            kill_dir,
            posture=WriterContainmentPosture.fully_contained(),
            wire_forwarder=lambda *_: WireDispatchOutcome(200, b"{}"),
            synthetic_qualification=True,
        )
        reattached = restarted.reattach_active_writer(
            base_access=access,
            writer_route="actions_hosted",
        )
        retained = restarted.candidate.snapshot(reattached)
        item = retained.document()["dispatches"]["kill-after-effect"]
        self.assertEqual(item["phase"], "forwarded")
        self.assertIsNone(item["usage_calls"])
        self.assertFalse(item["response_complete"])
        self.assertEqual(retained.document()["held_calls"], debited_held_calls)
        with self.assertRaisesRegex(AuthorityRefusal, "already consumed"):
            restarted.candidate.forward(reattached, retained, req)

    def test_duplicate_idempotency_exhausted_observation_tail_and_legacy_exclusion(
        self,
    ) -> None:
        # 1. Duplicate prepare idempotency + exhausted 2-attempt observation tail.
        obs_dir = self.dir / "idempotency-and-tail"
        coord = ContainedForwardingCoordinator(
            obs_dir,
            posture=WriterContainmentPosture.fully_contained(),
            wire_forwarder=lambda *_: WireDispatchOutcome(
                status_code=502,
                response_body=b"",
                effect_confirmed=False,
                response_complete=False,
                usage_calls=None,
            ),
            synthetic_qualification=True,
            allow_synthetic_overrides=True,
        )
        token, root = coord.enroll_initial_writer(
            base_access=_make_access(run_id=81),
            writer_route="actions_hosted",
        )
        req = Dispatch(
            dispatch_id="dup-1",
            operation="reply",
            method="POST",
            target="/repos/example/repo/issues/7/comments",
            body=canonical_bytes({"body": "idempotent"}),
            effect_sha256="7" * 64,
            reservation_sha256="8" * 64,
            liability_calls=6,
            deadline_ms=90000,
        )
        prepared_once = coord.candidate.prepare(token, root, req)
        prepared_twice = coord.candidate.prepare(token, prepared_once, req)
        self.assertEqual(prepared_once, prepared_twice)
        with self.assertRaisesRegex(
            AuthorityRefusal, "durable dispatch identity conflicts"
        ):
            coord.candidate.prepare(
                token,
                prepared_once,
                replace(req, body=canonical_bytes({"body": "changed"})),
            )
        with self.assertRaisesRegex(
            AuthorityRefusal, "original reservation is already bound"
        ):
            coord.candidate.prepare(
                token,
                prepared_once,
                replace(req, dispatch_id="dup-2"),
            )

        forwarded = coord.candidate.forward(token, prepared_once, req)
        coord.set_observation_override("dup-1", None, token=token)
        obs1 = coord.candidate.reconcile(token, forwarded, "dup-1")
        obs2 = coord.candidate.reconcile(token, obs1, "dup-1")
        self.assertEqual(obs2.document()["dispatches"]["dup-1"]["observations"], 2)
        self.assertEqual(obs2.document()["dispatches"]["dup-1"]["phase"], "forwarded")
        with self.assertRaisesRegex(
            AuthorityRefusal, "dispatch observation is unsupported or exhausted"
        ):
            coord.candidate.reconcile(token, obs2, "dup-1")
        with self.assertRaisesRegex(AuthorityRefusal, "already consumed"):
            coord.candidate.forward(token, obs2, req)

        # 2. Strict legacy-identity refusal without migration/reset + forged token refusal.
        legacy_bytes = canonical_bytes({"legacy_session_generation": 4})
        legacy_dir = self.dir / "legacy-refusal"
        legacy_coord = ContainedForwardingCoordinator(
            legacy_dir,
            posture=WriterContainmentPosture.fully_contained(),
            wire_forwarder=lambda *_: WireDispatchOutcome(200, b"{}"),
            synthetic_qualification=True,
        )
        with self.assertRaisesRegex(
            AuthorityRefusal, "legacy identity requires explicit migration or reset"
        ):
            legacy_coord.enroll_initial_writer(
                base_access=_make_access(run_id=91, native_exclusive=True),
                writer_route="actions_hosted",
                legacy=legacy_bytes,
            )

        obs_token, obs_root = legacy_coord.enroll_initial_writer(
            base_access=_make_access(run_id=91, native_exclusive=False),
            writer_route="actions_hosted",
            legacy=legacy_bytes,
        )
        self.assertEqual(legacy_coord.candidate.read_legacy(obs_token), legacy_bytes)
        with self.assertRaisesRegex(
            AuthorityRefusal, "legacy effects or writer exclusion block native actions"
        ):
            legacy_coord.candidate.prepare(
                obs_token,
                obs_root,
                Dispatch(
                    dispatch_id="legacy-patch",
                    operation="session_patch",
                    method="PATCH",
                    target="/repos/example/repo/issues/comments/701",
                    body=canonical_bytes({"body": "blocked"}),
                    effect_sha256="9" * 64,
                    reservation_sha256="a" * 64,
                    liability_calls=3,
                    deadline_ms=90000,
                ),
            )
        with self.assertRaisesRegex(
            AuthorityRefusal,
            "legacy identity cannot transition to exclusive native authority",
        ):
            legacy_coord.advance_epoch(
                predecessor_token=obs_token,
                successor_access=_make_access(
                    run_id=92, caller_tag="8", native_exclusive=True
                ),
                writer_route="actions_hosted",
                predecessor_ceased=True,
            )
        next_obs_token, _ = legacy_coord.advance_epoch(
            predecessor_token=obs_token,
            successor_access=_make_access(
                run_id=92, caller_tag="8", native_exclusive=False
            ),
            writer_route="actions_hosted",
            predecessor_ceased=True,
        )
        self.assertEqual(
            legacy_coord.candidate.read_legacy(next_obs_token), legacy_bytes
        )
        with self.assertRaisesRegex(AuthorityRefusal, "unqualified caller"):
            legacy_coord.candidate.snapshot(token)
        with self.assertRaisesRegex(AuthorityRefusal, "unqualified caller"):
            legacy_coord.candidate.snapshot({"forged": True})

    def test_containment_hardening_atomic_cutover_dedup_reconcile_and_scope_invariants(
        self,
    ) -> None:
        from review_sensei.recovery_authority import Snapshot

        # 1. RS-0F1549: advance_epoch rolls back cleanly when candidate.initialize fails.
        atom_coord = ContainedForwardingCoordinator(
            self.dir / "atomic-advance",
            posture=WriterContainmentPosture.fully_contained(),
            wire_forwarder=lambda *_: WireDispatchOutcome(200, b"{}"),
            synthetic_qualification=True,
        )
        tok1, _ = atom_coord.enroll_initial_writer(
            base_access=_make_access(run_id=101, caller_tag="1"),
            writer_route="actions_hosted",
        )
        with (
            patch.object(
                atom_coord.candidate,
                "initialize",
                side_effect=AuthorityRefusal("simulated init failure"),
            ),
            self.assertRaisesRegex(AuthorityRefusal, "simulated init failure"),
        ):
            atom_coord.advance_epoch(
                predecessor_token=tok1,
                successor_access=_make_access(run_id=102, caller_tag="2"),
                writer_route="actions_hosted",
                predecessor_ceased=True,
            )
        self.assertEqual(
            atom_coord.active_epoch(
                base_store_id="a" * 64, repository_id=123, pull_request=7
            ),
            1,
        )
        # Predecessor remains active and subsequent unmocked advance_epoch succeeds.
        atom_coord.fenced_http_mutation(
            tok1,
            dispatch_id="still-active-ep1",
            operation="reply",
            method="POST",
            target="/repos/example/repo/issues/7/comments",
            payload={"body": "ok"},
            reservation_sha256="1" * 64,
        )
        tok2, _ = atom_coord.advance_epoch(
            predecessor_token=tok1,
            successor_access=_make_access(run_id=102, caller_tag="2"),
            writer_route="actions_hosted",
            predecessor_ceased=True,
        )
        self.assertEqual(tok2.epoch, 2)

        # 2. RS-936BBC: concurrent duplicate dispatch_id on forward permits at most 1 wire call.
        wire_entered = threading.Event()
        wire_release = threading.Event()
        self.addCleanup(wire_release.set)
        wire_count = 0
        wire_lock = threading.Lock()

        def slow_wire(
            method: str, target: str, request_bytes: bytes, binding: dict[str, object]
        ) -> WireDispatchOutcome:
            nonlocal wire_count
            with wire_lock:
                wire_count += 1
            wire_entered.set()
            wire_release.wait(timeout=5)
            return WireDispatchOutcome(200, b"{}")

        dup_coord = ContainedForwardingCoordinator(
            self.dir / "concurrent-dup-forward",
            posture=WriterContainmentPosture.fully_contained(),
            wire_forwarder=slow_wire,
            synthetic_qualification=True,
        )
        dup_tok, _ = dup_coord.enroll_initial_writer(
            base_access=_make_access(run_id=111, caller_tag="3"),
            writer_route="actions_hosted",
        )
        dup_access = dup_coord.authenticate(dup_tok)
        body_obj = {"body": "dedup"}
        dup_binding = {
            "dispatch_id": "race-dup-1",
            "operation": "reply",
            "method": "POST",
            "target": "/repos/example/repo/issues/7/comments",
            "body": body_obj,
            "request_sha256": hashlib.sha256(canonical_bytes(body_obj)).hexdigest(),
        }
        race_errors: list[Exception] = []

        def run_first_forward() -> None:
            try:
                dup_coord.forward(dup_access, dup_binding)
            except Exception as exc:  # pragma: no cover
                race_errors.append(exc)

        t_first = threading.Thread(target=run_first_forward)
        t_first.start()
        try:
            self.assertTrue(wire_entered.wait(timeout=5))
            with self.assertRaisesRegex(
                AuthorityRefusal, "physical dispatch already forwarded"
            ):
                dup_coord.forward(dup_access, dup_binding)
        finally:
            wire_release.set()
            t_first.join(timeout=5)
        self.assertEqual(race_errors, [])
        self.assertEqual(wire_count, 1)

        # 3. RS-68E73A: fenced_http_mutation refuses incomplete/unconfirmed wire outcomes.
        for bad_outcome in (
            WireDispatchOutcome(
                504,
                b"",
                effect_confirmed=True,
                response_complete=False,
                usage_calls=None,
            ),
            WireDispatchOutcome(
                502, b"", effect_confirmed=False, response_complete=True, usage_calls=1
            ),
            WireDispatchOutcome(
                200,
                b"{}",
                effect_confirmed=True,
                response_complete=True,
                usage_calls=None,
            ),
        ):
            inc_coord = ContainedForwardingCoordinator(
                self.dir
                / f"inc-{bad_outcome.status_code}-{bad_outcome.effect_confirmed}",
                posture=WriterContainmentPosture.fully_contained(),
                wire_forwarder=lambda *_, out=bad_outcome: out,
                synthetic_qualification=True,
            )
            inc_tok, _ = inc_coord.enroll_initial_writer(
                base_access=_make_access(run_id=121),
                writer_route="actions_hosted",
            )
            with self.assertRaisesRegex(
                AuthorityRefusal, "wire mutation outcome is incomplete or unconfirmed"
            ):
                inc_coord.fenced_http_mutation(
                    inc_tok,
                    dispatch_id="inc-dispatch",
                    operation="reply",
                    method="POST",
                    target="/repos/example/repo/issues/7/comments",
                    payload={"body": "incomplete"},
                    reservation_sha256="2" * 64,
                )
            snap_after = inc_coord.candidate.snapshot(inc_tok)
            self.assertEqual(
                snap_after.document()["dispatches"]["inc-dispatch"]["phase"],
                "forwarded",
            )

        # 4. RS-7945D2: set_observation_override is disabled by default & requires active token.
        with self.assertRaisesRegex(
            AuthorityRefusal, "synthetic observation overrides are disabled"
        ):
            atom_coord.set_observation_override("x", None, token=tok2)
        override_coord = ContainedForwardingCoordinator(
            self.dir / "override-auth",
            posture=WriterContainmentPosture.fully_contained(),
            wire_forwarder=lambda *_: WireDispatchOutcome(200, b"{}"),
            synthetic_qualification=True,
            allow_synthetic_overrides=True,
        )
        ov_tok1, _ = override_coord.enroll_initial_writer(
            base_access=_make_access(run_id=131, caller_tag="4"),
            writer_route="actions_hosted",
        )
        ov_tok2, _ = override_coord.advance_epoch(
            predecessor_token=ov_tok1,
            successor_access=_make_access(run_id=132, caller_tag="5"),
            writer_route="actions_hosted",
            predecessor_ceased=True,
        )
        with self.assertRaisesRegex(AuthorityRefusal, "stale writer epoch fenced"):
            override_coord.set_observation_override("x", None, token=ov_tok1)
        override_coord.set_observation_override("x", None, token=ov_tok2)

        # 5. RS-429DFB, RS-67293A, RS-E3E38A, RS-0E02EA: snapshot schema, store_id, and legacy check.
        for bad_bytes in (
            b"not-json",
            canonical_bytes({"dispatches": []}),
            canonical_bytes({"dispatches": {"d1": {"phase": 123}}}),
        ):
            with self.assertRaises(AuthorityRefusal):
                extract_unsettled_dispatches(Snapshot(bad_bytes))

        with self.assertRaisesRegex(
            AuthorityRefusal, "base writer store_id must be a 64-hex sha256 digest"
        ):
            override_coord.enroll_initial_writer(
                base_access=replace(_make_access(), store_id="%_" + "a" * 62),
                writer_route="actions_hosted",
            )
        with self.assertRaisesRegex(
            AuthorityRefusal, "PR writer scope is not enrolled"
        ):
            override_coord.reattach_active_writer(
                base_access=replace(_make_access(), store_id="b" * 64),
                writer_route="actions_hosted",
            )
        with self.assertRaisesRegex(
            AuthorityRefusal, "legacy lock invariant conflicts"
        ):
            ContainedForwardingCoordinator._require_legacy_invariant(False, b"blob")
        with closing(sqlite3.connect(override_coord.frontier_path)) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE scope_epochs SET legacy_locked=1, legacy_blob=NULL")

    def test_route_inventory_credential_containment_proof_and_external_epoch_floor(
        self,
    ) -> None:
        """Trace every real writer/token route, prove side-coordinator limits, and guard external epoch rollback."""
        import importlib

        self.assertFalse(LIVE_CANONICAL_CUTOVER_ENABLED)
        inventory = inspect_writer_credential_inventory()
        self.assertFalse(inventory["live_cutover_enabled"])
        self.assertFalse(inventory["repository_default_cutover_enabled"])
        self.assertFalse(inventory["exclusive_cutover_ready_in_default_runtime"])
        self.assertEqual(
            frozenset(inventory["contained_route_keys"]),
            CONTAINED_WRITER_ROUTES,
        )
        self.assertEqual(
            frozenset(inventory["bypass_route_keys"]),
            BYPASS_WRITER_ROUTES,
        )
        route_map = {
            entry.route_id: entry for entry in WRITER_CREDENTIAL_ROUTE_INVENTORY
        }
        for expected_route in (
            "actions_hosted",
            "actions_local",
            "actions_command",
            "actions_reply",
            "manual_cli_local_ledger",
            "manual_cli_direct_github",
            "broker_raw_token",
            "pre_cancellation_inflight",
        ):
            self.assertIn(expected_route, route_map)
            entry = route_map[expected_route]
            self.assertTrue(entry.entry_modules)
            for mod_name in entry.entry_modules:
                self.assertIsNotNone(importlib.import_module(mod_name))

        # Local-only CLI has no GitHub write surface and no raw token bypass risk;
        # every network-writing route pre-cutover carries unrestricted_token_bypass_risk.
        self.assertFalse(
            route_map["manual_cli_local_ledger"].unrestricted_token_bypass_risk
        )
        self.assertEqual(route_map["manual_cli_local_ledger"].github_write_surfaces, ())
        for raw_route in (
            "actions_hosted",
            "actions_local",
            "actions_command",
            "actions_reply",
            "manual_cli_direct_github",
            "broker_raw_token",
            "pre_cancellation_inflight",
        ):
            self.assertTrue(route_map[raw_route].unrestricted_token_bypass_risk)

        with self.assertRaisesRegex(
            AuthorityRefusal, "live_cutover_enabled must be a boolean"
        ):
            inspect_writer_credential_inventory(live_cutover_enabled="false")  # type: ignore[arg-type]

        # Prove structurally why a side coordinator cannot fence a writer holding a raw GitHub token.
        unfenced_raw = evaluate_side_coordinator_fencing_proof(
            holds_unrestricted_github_token=True,
            coordinator_epoch_advanced=True,
            uses_contained_forwarding=True,
        )
        self.assertFalse(unfenced_raw.can_fence_delayed_writer)
        self.assertTrue(unfenced_raw.residual_clobber_possible)

        unmediated = evaluate_side_coordinator_fencing_proof(
            holds_unrestricted_github_token=False,
            coordinator_epoch_advanced=True,
            uses_contained_forwarding=False,
        )
        self.assertFalse(unmediated.can_fence_delayed_writer)
        self.assertTrue(unmediated.residual_clobber_possible)

        not_advanced = evaluate_side_coordinator_fencing_proof(
            holds_unrestricted_github_token=False,
            coordinator_epoch_advanced=False,
            uses_contained_forwarding=True,
        )
        self.assertFalse(not_advanced.can_fence_delayed_writer)
        self.assertTrue(not_advanced.residual_clobber_possible)

        fenced = evaluate_side_coordinator_fencing_proof(
            holds_unrestricted_github_token=False,
            coordinator_epoch_advanced=True,
            uses_contained_forwarding=True,
        )
        self.assertTrue(fenced.can_fence_delayed_writer)
        self.assertFalse(fenced.residual_clobber_possible)

        with self.assertRaisesRegex(AuthorityRefusal, "must be a boolean"):
            evaluate_side_coordinator_fencing_proof(
                holds_unrestricted_github_token=1,  # type: ignore[arg-type]
                coordinator_epoch_advanced=True,
                uses_contained_forwarding=True,
            )

        # Verify external epoch floor blocks simultaneous frontier+authority backup rollback.
        floor_coord = ContainedForwardingCoordinator(
            self.dir / "external-floor",
            posture=WriterContainmentPosture.fully_contained(),
            wire_forwarder=lambda *_: WireDispatchOutcome(200, b"{}"),
            synthetic_qualification=True,
        )
        with self.assertRaisesRegex(
            AuthorityRefusal, "PR writer scope is not enrolled"
        ):
            floor_coord.verify_external_epoch_floor(
                base_store_id="a" * 64,
                repository_id=123,
                pull_request=7,
                observed_external_epoch=1,
            )
        t1, _ = floor_coord.enroll_initial_writer(
            base_access=_make_access(run_id=201, caller_tag="1"),
            writer_route="actions_hosted",
        )
        self.assertEqual(
            floor_coord.verify_external_epoch_floor(
                base_store_id="a" * 64,
                repository_id=123,
                pull_request=7,
                observed_external_epoch=1,
            ),
            1,
        )
        with self.assertRaisesRegex(
            AuthorityRefusal,
            "durable frontier regressed behind externally observed epoch",
        ):
            floor_coord.verify_external_epoch_floor(
                base_store_id="a" * 64,
                repository_id=123,
                pull_request=7,
                observed_external_epoch=2,
            )
        with self.assertRaisesRegex(
            AuthorityRefusal, "observed external epoch floor must be a positive integer"
        ):
            floor_coord.verify_external_epoch_floor(
                base_store_id="a" * 64,
                repository_id=123,
                pull_request=7,
                observed_external_epoch=0,
            )
        floor_coord.advance_epoch(
            predecessor_token=t1,
            successor_access=_make_access(run_id=202, caller_tag="2"),
            writer_route="actions_hosted",
            predecessor_ceased=True,
        )
        self.assertEqual(
            floor_coord.verify_external_epoch_floor(
                base_store_id="a" * 64,
                repository_id=123,
                pull_request=7,
                observed_external_epoch=2,
            ),
            2,
        )

    def test_real_api_run_owner_and_requests_fencing_budget_uncertain_and_legacy_refusal(
        self,
    ) -> None:
        """Exercise real ApiRunActionsEvidence, ApiRunRequestTransport, and ApiRunSessionLedger routes."""
        from urllib.request import Request

        from review_sensei_github.action_run import ActionRunLedger
        from review_sensei_github.api_run_owner import ApiRunActionsEvidence
        from review_sensei_github.api_run_requests import (
            ApiRunRequestTransport,
            ApiRunSessionLedger,
        )
        from review_sensei_github.http import GitHubHttp
        from review_sensei_github.session_ledger import GitHubIssueCommentSessionLedger
        from test_api_work_lifecycle import LifecycleFixture
        from test_api_work_persistence import BASE, HEAD, IDENTITY

        from review_sensei.api_work import (
            PARTITIONED_ACTION_ACCOUNTING_VERSION,
            ApiExecutionOwner,
            ApiRunAccounting,
            ApiRunIdentity,
        )
        from review_sensei.errors import ReviewInputError
        from review_sensei.models import ReviewTransaction

        try:
            from fake_github_http import json_response
        except ModuleNotFoundError:
            from tests.fake_github_http import json_response

        # 1. Real ApiRunRequestTransport + ApiRunSessionLedger: delayed old writer vs successor rollover.
        run1_dir = self.dir / "real-transport-run1"
        run1_dir.mkdir(parents=True, exist_ok=True)
        fixture = LifecycleFixture(str(run1_dir), limit=1024)
        native1, config, _evidence1, owner1 = fixture.prelude()
        pre_snap = fixture.accounted.containment_and_budget_snapshot()
        self.assertFalse(pre_snap.is_activated)
        self.assertFalse(pre_snap.live_cutover_enabled)
        self.assertTrue(pre_snap.conservation_holds)

        fixture.accounted.initialize_and_activate(native1, IDENTITY, config, owner1)
        ledger1 = ApiRunSessionLedger(native1, fixture.accounted, IDENTITY)
        act_snap = fixture.accounted.containment_and_budget_snapshot()
        self.assertTrue(act_snap.is_activated)
        self.assertFalse(act_snap.live_cutover_enabled)
        self.assertTrue(act_snap.legacy_accounting_overlap_refused)
        self.assertTrue(act_snap.conservation_holds)
        self.assertEqual(act_snap.configured_limit, 1024)

        tx_active = ReviewTransaction.create(
            repository=IDENTITY.repository,
            pull_request=IDENTITY.pull_request,
            base_sha=BASE,
            head_sha=HEAD,
            policy_digest="2" * 64,
            configuration_digest=config.digest,
            evidence_digest="4" * 64,
            reservation_id="1" * 64,
            generation=1,
        )
        ledger1.replace(
            IDENTITY,
            lambda rec: rec.evolve(
                generation=rec.generation + 1,
                reservation_id=tx_active.reservation_id,
                reserved_slot="initial",
                transaction=tx_active,
            ),
        )
        tx1 = replace(
            tx_active,
            phase="publication_succeeded",
            result_sha256="5" * 64,
        )
        ledger1.claim_publication_completion(IDENTITY, tx1, published=True)
        ledger1.replace(
            IDENTITY,
            lambda rec: rec.evolve(
                generation=rec.generation + 1,
                reservation_id=None,
                reserved_slot=None,
                transaction=tx1,
                last_committed_reservation_id=tx_active.reservation_id,
            ),
        )
        closed_root = fixture.accounted.finish_review_run(ledger1, IDENTITY, tx1)
        self.assertEqual(
            ApiRunAccounting.from_document(
                closed_root.api_run_accounting
            ).terminal_transaction,
            tx1.transaction_id,
        )

        # Successor Run 457 rolls over after Run 456 completed.
        run1_trace = list(fixture.trace)
        fixture.trace.clear()
        run2_dir = self.dir / "real-transport-run2"
        run2_dir.mkdir(parents=True, exist_ok=True)
        caller2 = ApiRunRequestTransport(
            directory=run2_dir,
            run=ApiRunIdentity(123, 457),
            plan=fixture.plan,
            attempt=1,
            job="hosted",
            opener=fixture._external_open,
        )
        fixture.accounted = caller2
        http2 = GitHubHttp(api_url="https://api.github.test", opener=caller2.open)
        native2 = GitHubIssueCommentSessionLedger(
            http2, token="synthetic", app_slug="reviewsensei[bot]"
        )
        native2.load(IDENTITY)
        caller2.rollover_and_activate(
            native2,
            IDENTITY,
            config,
            ApiExecutionOwner(1, "hosted", 0),
            closed_root,
            lambda: None,
        )
        ledger2 = ApiRunSessionLedger(native2, caller2, IDENTITY)
        run2_root = ledger2.load(IDENTITY).record
        assert run2_root is not None
        run2_trace = list(fixture.trace)

        # Delayed old writer from Run 456 attempting replace_checkpoint through its
        # ApiRunSessionLedger is rejected on optimistic root_sha256 mismatch.
        fixture.trace[:] = run1_trace
        fixture.accounted = ledger1.caller
        with self.assertRaisesRegex(
            ReviewInputError, "caller checkpoint root changed|caller terminal"
        ):
            ledger1.replace(
                IDENTITY,
                lambda rec: rec.evolve(
                    generation=rec.generation + 1, operator_paused=True
                ),
            )
        fixture.trace[:] = run2_trace
        fixture.accounted = caller2
        self.assertEqual(
            ledger2.load(IDENTITY).record.record_sha256,
            run2_root.record_sha256,
        )

        # 2. Uncertain mutation permanently debits physical budget and blocks terminal close + rollover.
        unc_dir = self.dir / "real-transport-uncertain"
        unc_dir.mkdir(parents=True, exist_ok=True)
        unc_fixture = LifecycleFixture(str(unc_dir), limit=1024)
        unc_native, unc_cfg, _, unc_owner = unc_fixture.prelude()
        unc_fixture.accounted.initialize_and_activate(
            unc_native, IDENTITY, unc_cfg, unc_owner
        )
        unc_ledger = ActionRunLedger(unc_native, unc_fixture.accounted, IDENTITY)
        intent = unc_fixture.accounted.admit_action(
            unc_ledger,
            IDENTITY,
            purpose="command",
            source={
                "kind": "issue_comment",
                "id": 88,
                "updated_at": "2026-10-25T12:00:00Z",
                "body_sha256": "f" * 64,
            },
            head_sha=HEAD,
            operation_key="e" * 64,
        )
        boundary = unc_fixture.accounted.begin_action_effect(
            unc_ledger, IDENTITY, intent
        )
        unc_fixture.fault = "unknown-absent"
        with self.assertRaises(TimeoutError):
            unc_fixture.accounted.open(
                Request(
                    "https://api.github.test/repos/owner/repo/issues/comments/1000000",
                    data=json.dumps({"body": unc_fixture.comment["body"]}).encode(),
                    method="PATCH",
                ),
                timeout=5,
            )
        unc_snap = unc_fixture.accounted.containment_and_budget_snapshot()
        self.assertGreaterEqual(unc_snap.uncertain_requests, 1)
        self.assertGreaterEqual(unc_snap.unresolved_mutation_count, 1)
        self.assertTrue(unc_snap.conservation_holds)
        with self.assertRaisesRegex(ReviewInputError, "confirmed outcome"):
            unc_fixture.accounted.settle_action_effect(
                unc_ledger,
                IDENTITY,
                boundary,
                outcome={"kind": "applied", "reason": None},
                result_document={"applied": True},
            )

        # Physical budget guarantee validator also rejects conservation violations & undersized tails.
        with self.assertRaisesRegex(
            AuthorityRefusal, "physical caller request conservation invariant violated"
        ):
            evaluate_physical_budget_guarantees(
                configured_limit=512,
                startup_liability=78,
                confirmed_requests=10,
                uncertain_requests=1,
                reserved_undispatched=100,
                remaining_unallocated=999,
            )
        with self.assertRaisesRegex(
            AuthorityRefusal, "write_confirmation_tail must preserve at least"
        ):
            evaluate_physical_budget_guarantees(
                configured_limit=512,
                startup_liability=78,
                confirmed_requests=0,
                uncertain_requests=0,
                reserved_undispatched=400,
                remaining_unallocated=34,
                write_confirmation_tail=2,
            )

        # 3. Unsupported legacy un-accounted record & reader-only accounting overlap are refused fail-closed.
        with self.assertRaisesRegex(
            ReviewInputError, "API accounting cannot be removed"
        ):
            closed_root.evolve(
                generation=closed_root.generation + 1,
                api_run_accounting=None,
            )
        unaccounted_record = SessionRecord.create(IDENTITY)
        fixture.comment["body"] = render_session_comment(
            repository_id=123,
            pull_request=7,
            record=unaccounted_record,
        )
        run3_dir = self.dir / "real-transport-legacy-refusal"
        run3_dir.mkdir(parents=True, exist_ok=True)
        caller3 = ApiRunRequestTransport(
            directory=run3_dir,
            run=ApiRunIdentity(123, 458),
            plan=fixture.plan,
            attempt=1,
            job="hosted",
            opener=fixture._external_open,
        )
        fixture.trace.clear()
        fixture.accounted = caller3
        http3 = GitHubHttp(api_url="https://api.github.test", opener=caller3.open)
        native3 = GitHubIssueCommentSessionLedger(
            http3, token="synthetic", app_slug="reviewsensei[bot]"
        )
        loaded_unaccounted = native3.load(IDENTITY).record
        assert loaded_unaccounted is not None
        with self.assertRaises(ReviewInputError):
            caller3.rollover_and_activate(
                native3,
                IDENTITY,
                config,
                ApiExecutionOwner(1, "hosted", 0),
                loaded_unaccounted,
                lambda: None,
            )

        reader_only_doc = dict(closed_root.api_run_accounting)
        reader_only_doc["version"] = PARTITIONED_ACTION_ACCOUNTING_VERSION
        reader_only_record = SessionRecord.create(
            IDENTITY,
            generation=2,
            last_committed_reservation_id=tx_active.reservation_id,
            transaction=tx1,
            api_run_accounting=reader_only_doc,
        )
        fixture.comment["body"] = render_session_comment(
            repository_id=123,
            pull_request=7,
            record=reader_only_record,
        )
        run3b_dir = self.dir / "real-transport-v3-refusal"
        run3b_dir.mkdir(parents=True, exist_ok=True)
        caller3b = ApiRunRequestTransport(
            directory=run3b_dir,
            run=ApiRunIdentity(123, 459),
            plan=fixture.plan,
            attempt=1,
            job="hosted",
            opener=fixture._external_open,
        )
        fixture.trace.clear()
        fixture.accounted = caller3b
        http3b = GitHubHttp(api_url="https://api.github.test", opener=caller3b.open)
        native3b = GitHubIssueCommentSessionLedger(
            http3b, token="synthetic", app_slug="reviewsensei[bot]"
        )
        loaded_reader_only = native3b.load(IDENTITY).record
        assert loaded_reader_only is not None
        with self.assertRaisesRegex(
            ReviewInputError, "caller-requests-v3 is reader-only"
        ):
            caller3b.rollover_and_activate(
                native3b,
                IDENTITY,
                config,
                ApiExecutionOwner(1, "hosted", 0),
                loaded_reader_only,
                lambda: None,
            )

        # 4. Unknown workflow executor commit & unknown head_sha refused on ApiRunActionsEvidence.
        def actions_handler(req: Request):
            path = urlsplit(req.full_url).path
            if path == "/repos/owner/repo/actions/runs/900":
                return json_response(
                    {
                        "id": 900,
                        "run_attempt": 1,
                        "status": "completed",
                        "conclusion": "success",
                        "head_sha": HEAD,
                        "event": "pull_request",
                        "repository": {"id": 123, "full_name": "owner/repo"},
                        "referenced_workflows": [
                            {
                                "path": "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml",
                                "sha": "9" * 40,  # Unknown/unqualified executor SHA
                                "ref": "refs/tags/v5",
                            }
                        ],
                    }
                )
            raise AssertionError(path)

        ev_http = GitHubHttp(
            api_url="https://api.github.test",
            opener=lambda req, timeout=5.0: actions_handler(req),
        )
        ev = ApiRunActionsEvidence(
            ev_http, token="synthetic-actions", command_run_id="901", job="hosted"
        )
        with self.assertRaisesRegex(
            ReviewInputError, "historical executor source is unrecognized"
        ):
            ev.qualified_publication_cessation(
                IDENTITY,
                900,
                ApiExecutionOwner(1, "hosted", 0),
                head_sha=HEAD,
            )
        with self.assertRaisesRegex(
            ReviewInputError, "historical executor did not prove successful review exit"
        ):
            ev.qualified_publication_cessation(
                IDENTITY,
                900,
                ApiExecutionOwner(1, "hosted", 0),
                head_sha="0" * 40,  # Mismatched/unknown head_sha
            )


if __name__ == "__main__":
    unittest.main()

"""Qualification tests for G02: atomic writer containment and stale ledger PATCH fencing."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
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
    ContainedForwardingCoordinator,
    IssuedWriterToken,
    WireDispatchOutcome,
    WriterContainmentPosture,
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


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["REVIEWSENSEI_ALLOW_CHECKOUT_IMPORT"] = "1"
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

    def test_advisory_generation_readback_cannot_stop_delayed_wire_patch(self) -> None:
        """Prove G02 threat: old read -> successor commits -> delayed old wire PATCH clobbers GitHub."""

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

    def test_coordinator_beside_unfenced_broker_cannot_fence_bypass_token_writer(
        self,
    ) -> None:
        """Prove a coordinator beside a raw-token broker fails both policy check and wire containment."""

        initial = SessionRecord.create(PR_IDENTITY, now=FIXED_NOW).evolve(
            generation=1, now=FIXED_NOW
        )
        server = StatefulGitHubCommentServer(initial)
        unfenced = WriterContainmentPosture.unfenced_broker()
        coordinator = ContainedForwardingCoordinator(
            self.dir / "unfenced-sidecar",
            posture=unfenced,
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

        repo_root = Path(__file__).resolve().parents[1]
        script = """
import json, os, signal, sys
from pathlib import Path
repo = Path(sys.argv[2])
sys.path[:0] = [str(repo / "src"), str(repo / "packages/python/review-sensei-github/src"), str(repo / "tests")]
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
            [sys.executable, "-c", script, str(kill_dir), str(repo_root)],
            env=_child_env(),
            capture_output=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(
            proc.returncode,
            79 if os.name == "nt" else -9,
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

        coord.set_observation_override("dup-1", None)
        forwarded = coord.candidate.forward(token, prepared_once, req)
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


if __name__ == "__main__":
    unittest.main()

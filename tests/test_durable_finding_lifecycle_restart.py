"""Ordinary lifecycle qualification across real interpreter/process boundaries.

Only ledger files and the fake remote service's JSON bytes survive a worker.
The harness never restores publisher dictionaries or injects lifecycle state.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

from review_sensei_github import (
    ConversationPublisher,
    GitHubApplication,
    GitHubWriteOptions,
    LearningPRPublisher,
    ReviewPublisher,
)
from review_sensei_github.finding_lifecycle import _unpack_journal

from review_sensei.active_finding_window import ConfirmedFixEvidence
from review_sensei.baseline import baseline_from_review
from review_sensei.context import ReviewContextCacheKey
from review_sensei.finding_drift import FileDriftDescriptor
from review_sensei.finding_lifecycle_store import read_finding_lifecycle
from review_sensei.models import ReviewResult, ReviewTransaction
from review_sensei.session import (
    LocalSessionLedger,
    SessionIdentity,
    checkpoint_review_analysis,
    prepare_review_transaction,
    session_reservation_id,
)
from tests import test_active_window_lifecycle_g10 as window_fixtures
from tests.test_active_window_lifecycle_g10 import (
    BASE_SHA,
    ORDINARY_DIFF,
    _comment,
    _coverage,
    _result,
    _sha,
    _WindowFakeBroker,
    _WindowGitHubHost,
)
from tests.test_review_transaction import CONFIGURATION, EVIDENCE, POLICY

IDENTITY = SessionIdentity("owner/repo", 1, repository_id=1)
SERVICE_FIELDS = (
    "head_sha",
    "base_sha",
    "reviews",
    "threads",
    "check_runs",
    "issue_comments",
    "calls",
)


class PersistentNativeService(_WindowGitHubHost):
    """Persist every request/effect, including an effect whose response is lost."""

    def __init__(self, path: Path, head_sha: str):
        super().__init__(head_sha=head_sha)
        self.path = path
        self.lose_review_response = False
        self.exit_after_review_effect = False
        if path.exists():
            for name, value in json.loads(path.read_text()).items():
                if name in SERVICE_FIELDS:
                    setattr(self, name, value)
        self.head_sha = head_sha

    def save(self):
        self.path.write_text(
            json.dumps(
                {name: getattr(self, name) for name in SERVICE_FIELDS}, sort_keys=True
            )
        )

    def _open(self, request, timeout=None):
        path = urlparse(request.full_url).path
        if (
            request.method == "GET"
            and "/reviews/" in path
            and path.endswith("/comments")
        ):
            self.calls.append((request.method, path, None))
            rid = int(path.split("/reviews/")[1].split("/")[0])
            comments = [
                dict(c, id=c["databaseId"])
                for t in self.threads
                for c in t["comments"]["nodes"]
                if c["pull_request_review_id"] == rid
            ]
            self.save()
            return self._resp(comments)
        if request.method == "GET" and "/pulls/1/reviews/" in path:
            self.calls.append((request.method, path, None))
            rid = int(path.rsplit("/", 1)[-1])
            item = next((r for r in self.reviews if r["id"] == rid), None)
            self.save()
            return self._resp(item or {"message": "Not Found"}, 200 if item else 404)
        if request.method == "GET" and "/check-runs/" in path:
            self.calls.append((request.method, path, None))
            rid = int(path.rsplit("/", 1)[-1])
            item = next((r for r in self.check_runs if r["id"] == rid), None)
            self.save()
            return self._resp(item or {"message": "Not Found"}, 200 if item else 404)
        # Retained evidence readback uses actual comment IDs, not a census.
        if request.method == "GET" and "/issues/comments/" in path:
            self.calls.append((request.method, path, None))
            cid = int(path.rsplit("/", 1)[-1])
            item = next((c for c in self.issue_comments if c["id"] == cid), None)
            self.save()
            return self._resp(item or {"message": "Not Found"}, 200 if item else 404)
        if request.method == "PATCH" and "/issues/comments/" in path:
            body = json.loads(request.data)
            self.calls.append((request.method, path, body))
            cid = int(path.rsplit("/", 1)[-1])
            item = next(c for c in self.issue_comments if c["id"] == cid)
            item["body"] = body["body"]
            self.save()
            return self._resp(item)
        response = super()._open(request, timeout)
        if request.method == "POST" and path.endswith("/pulls/1/reviews"):
            # Opaque, service-chosen IDs intentionally differ from the old
            # implementation's guessed PRRT_<review>_<index> identities.
            for ordinal, thread in enumerate(self.threads):
                if thread["id"].startswith("PRRT_") and not thread["id"].startswith(
                    "PRRT_native_"
                ):
                    thread["id"] = "PRRT_native_" + _sha(str(ordinal))[:20]
                    for comment in thread["comments"]["nodes"]:
                        cid = 73001 + ordinal * 17
                        comment.update(
                            databaseId=cid,
                            id="PRRC_native_" + _sha(str(cid))[:20],
                            node_id="PRRC_native_" + _sha(str(cid))[:20],
                            user={"id": 9001, "login": self.app_slug, "type": "Bot"},
                            path=thread["path"],
                            line=thread["line"],
                            side="RIGHT",
                            pull_request_url="https://api.github.test/repos/owner/repo/pulls/1",
                        )
                        comment["author"]["databaseId"] = 9001
                        comment["commit"] = {"oid": self.head_sha}
                        comment["pullRequestReview"] = {
                            "databaseId": self.reviews[-1]["id"]
                        }
            self.save()
            if self.exit_after_review_effect:
                os._exit(73)
            if self.lose_review_response:
                self.lose_review_response = False
                from review_sensei_github.errors import GitHubHTTPTransientError

                raise GitHubHTTPTransientError(
                    "response lost after durable native effect"
                )
        self.save()
        return response


def _state_observation(publisher, mode):
    state = (
        publisher.drift_state if mode == "drift" else publisher.active_window_state
    )(repository_id=1, pull_request=1)
    if state is None:
        return None
    if mode == "drift":
        obligations = state.inventory.obligations
        entries = state.active_entries
        rows = [
            {
                "id": o.criterion.obligation_id,
                "criterion": o.criterion.resolution_criterion,
                "fingerprint": o.criterion.original_fingerprint,
                "symbol": o.criterion.origin_symbol,
                "status": o.lifecycle_state,
                "target": o.thread_binding.thread_id if o.thread_binding else None,
                "slot_state": o.thread_binding.slot_state if o.thread_binding else None,
                "path": o.present_anchor.path,
                "candidate_paths": list(o.present_anchor.candidate_paths),
            }
            for o in obligations
        ]
    else:
        obligations = state.authoritative_decision_inventory.obligations
        entries = state.display_active_findings.entries
        rows = [
            {
                "id": o.obligation_id,
                "criterion": o.resolution_criterion,
                "fingerprint": o.fingerprint,
                "symbol": o.symbol,
                "status": o.lifecycle_state,
                "target": o.native_target,
                "slot_state": o.target_state,
                "needs_human": o.comment.needs_human if o.comment else None,
                "effective_blocking": o.comment.effective_blocking
                if o.comment
                else None,
            }
            for o in obligations
        ]
    return {"obligations": rows, "active": [e.obligation_id for e in entries]}


def _durable_observation(root):
    reader = LocalSessionLedger(root / "ledger", enable_finding_lifecycle_writes=True)
    loaded = reader.load(IDENTITY)
    if loaded.record is None:
        return {"load_status": loaded.status, "effects": None}
    stored = read_finding_lifecycle(reader, loaded.record)
    document = _unpack_journal(stored) if stored else None
    return {
        "load_status": loaded.status,
        "effects": document["effects"] if document else None,
    }


def _worker(root: Path, mode: str, action: str, evidence: dict):
    phase = {
        "initial": 1,
        "omitted": 2,
        "promote": 3,
        "finish": 4,
        "replay": 4,
        "lost": 1,
        "crash": 1,
        "lost_replay": 1,
        "command": 1,
        "verify": 1,
        "rename": 2,
        "move": 3,
        "split": 4,
        "partial_split_fix": 5,
        "ambiguous_resume": 6,
    }[action]
    head = str(phase) * 40
    host = PersistentNativeService(root / "service.json", head)
    ledger = LocalSessionLedger(root / "ledger", enable_finding_lifecycle_writes=True)
    if action in {"initial", "lost", "crash"}:
        ledger.initialize(IDENTITY)
    publisher = ReviewPublisher(http=host.http)
    app = GitHubApplication(
        http=host.http,
        broker=_WindowFakeBroker(),
        reviewer=publisher,
        learner=LearningPRPublisher(http=host.http),
        replier=ConversationPublisher(http=host.http),
        session_ledger=ledger,
    )
    options = GitHubWriteOptions(
        auto_review=True,
        auto_approve=True,
        github_writes=True,
        operation_entry="disabled",
        max_active_findings=1,
        enable_drift_tracking=mode == "drift",
    )
    if action in {"command", "verify"}:
        before = ledger.load(IDENTITY).record.to_dict()
        before_calls = len(host.calls)
        error = None
        outcome = None
        try:
            outcome = app.apply_maintainer_command(
                options=options,
                oidc_token="fixture-command",
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                head_sha=head,
                app_slug="reviewsensei[bot]",
                actor_login="maint-lead",
                actor_type="User",
                association="OWNER",
                body=(
                    "@reviewsensei verify"
                    if action == "verify"
                    else "@reviewsensei accept-risk "
                    + evidence["fingerprint"]
                    + " --reason Covered by verified upstream gateway"
                ),
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        host.save()
        print(
            json.dumps(
                {
                    "error": error,
                    "applied": getattr(outcome, "applied", None),
                    "state": _state_observation(publisher, mode),
                    "before_record": before,
                    "record": (
                        ledger.load(IDENTITY).record.to_dict()
                        if ledger.load(IDENTITY).record
                        else None
                    ),
                    "durable": _durable_observation(root),
                    "calls": host.calls[before_calls:],
                    "reviews": host.reviews,
                    "threads": host.threads,
                }
            )
        )
        return
    current_key = ReviewContextCacheKey(
        repository="owner/repo",
        pull_request=1,
        base_sha=BASE_SHA,
        head_sha=head,
        engine="fixture",
        model="fixture-model",
        profile="default",
        stage_digest=_sha("stage"),
        context_digest=_sha("context"),
        learning_digest=_sha("learning"),
    )
    comments = (
        _comment(
            "src/app.py",
            line=5,
            symbol="auth",
            body="Required auth check.",
            severity="critical",
        ),
        _comment(
            "src/db.py",
            line=5,
            symbol="conn",
            body="Required cleanup.",
            severity="high",
        ),
    )
    drift_descriptors = ()
    publication_diff = ORDINARY_DIFF
    drift_targets = {
        "rename": ("src/app.py", ("src/renamed.py",), "file_rename"),
        "move": ("src/renamed.py", ("src/moved.py",), "cross_file_move"),
        "split": (
            "src/moved.py",
            ("src/split_a.py", "src/split_b.py"),
            "cross_file_move",
        ),
    }
    if action in drift_targets:
        source, targets, kind = drift_targets[action]
        drift_descriptors = (FileDriftDescriptor(kind, source, targets, ("auth",)),)
        publication_diff = "\n".join(
            ORDINARY_DIFF.replace("src/app.py", target) for target in targets
        )
    if action in {"replay", "lost_replay"}:
        result = ReviewResult.from_dict(json.loads((root / "result.json").read_text()))
    else:
        selected = comments if action in {"initial", "lost", "crash"} else ()
        if action == "promote":
            selected = (comments[1],)
        if action in drift_targets:
            selected = tuple(
                replace(comments[0], path=target) for target in drift_targets[action][1]
            ) + (comments[1],)
        result = replace(_result(*selected), model="fixture-model")
        if action in drift_targets:
            result = replace(
                result, coverage=_coverage(*drift_targets[action][1], "src/db.py")
            )
        prepared = prepare_review_transaction(
            ledger,
            IDENTITY,
            POLICY,
            reservation_id=session_reservation_id(
                repository="owner/repo", pull_request=1, head_sha=head, kind="publish"
            ),
            base_sha=BASE_SHA,
            head_sha=head,
            configuration_digest=ReviewTransaction.compute_configuration_digest(
                CONFIGURATION
            ),
            evidence_digest=ReviewTransaction.compute_evidence_digest(EVIDENCE),
        )
        baseline = baseline_from_review(
            result,
            cache_key=current_key,
            policy=POLICY,
            generation=prepared.record.generation + 1,
        )
        result = checkpoint_review_analysis(
            ledger, IDENTITY, prepared, result, baseline=baseline
        )
        (root / "result.json").write_text(json.dumps(result.to_dict()))
    fixes = ()
    if action in {"promote", "finish", "partial_split_fix"}:
        fixes = (
            ConfirmedFixEvidence(
                obligation_id=evidence["id"],
                resolution_criterion=evidence["criterion"],
                head_sha=head,
                evidence_sha256=_sha("verified:" + head),
                verified_paths=(
                    (
                        "src/split_a.py"
                        if action == "partial_split_fix"
                        else "src/app.py"
                        if action == "promote"
                        else "src/db.py"
                    ),
                ),
            ),
        )
    host.lose_review_response = action == "lost"
    host.exit_after_review_effect = action == "crash"
    before_calls = len(host.calls)
    before_loaded = ledger.load(IDENTITY)
    before_record = before_loaded.record.to_dict() if before_loaded.record else None
    outcome = None
    error = None
    try:
        outcome = app.publish_review(
            options=options,
            oidc_token="fixture-oidc",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha=head,
            base_branch="main",
            base_sha=BASE_SHA,
            result=result,
            diff=publication_diff,
            drift_descriptors=drift_descriptors,
            app_slug="reviewsensei[bot]",
            convergence_policy=POLICY,
            current_key=current_key,
            configuration_context=CONFIGURATION,
            evidence_context=EVIDENCE,
            blocker_candidates=window_fixtures.OrdinaryPublisherActiveWindowTests._blockers(
                result
            ),
            confirmed_fixes=fixes,
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    host.save()
    print(
        json.dumps(
            {
                "status": outcome.status if outcome else None,
                "diagnostic": outcome.diagnostic if outcome else None,
                "error": error,
                "state": _state_observation(publisher, mode),
                "calls": host.calls[before_calls:],
                "record": (
                    ledger.load(IDENTITY).record.to_dict()
                    if ledger.load(IDENTITY).record
                    else None
                ),
                "durable": _durable_observation(root),
                "before_record": before_record,
                "reviews": host.reviews,
                "threads": host.threads,
            }
        )
    )


class DurableFindingRestartTests(unittest.TestCase):
    def run_phase(self, root, mode, action, evidence=None, *, expected_exit=0):
        repo = Path(__file__).resolve().parents[1]
        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "tests.test_durable_finding_lifecycle_restart",
                str(root),
                mode,
                action,
                json.dumps(evidence or {}),
            ],
            cwd=repo,
            env={
                **os.environ,
                "PYTHONPATH": os.pathsep.join(
                    str(repo / p)
                    for p in (
                        "src",
                        "packages/python/review-sensei-github/src",
                        ".",
                        "tests",
                    )
                ),
            },
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(process.returncode, expected_exit, process.stderr)
        return json.loads(process.stdout) if expected_exit == 0 else None

    def test_process_death_after_dispatch_keeps_intent_and_never_reposts(self):
        for mode in ("window", "drift"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.run_phase(root, mode, "crash", expected_exit=73)
                service = json.loads((root / "service.json").read_text())
                self.assertEqual(len(service["reviews"]), 1)
                recovered = self.run_phase(root, mode, "lost_replay")
                self.assertEqual(len(recovered["reviews"]), 1)
                self.assertEqual(recovered["threads"], service["threads"])
                self.assertIsNone(recovered["error"])
                self.assertIn(recovered["status"], {"published", "already_published"})
                self.assertEqual(
                    recovered["record"]["transaction"]["phase"], "publication_succeeded"
                )
                self.assertTrue(
                    all(
                        e["status"] == "confirmed"
                        for e in recovered["durable"]["effects"]
                    )
                )
                self.assertFalse(
                    any(
                        m == "POST" and p.endswith("/pulls/1/reviews")
                        for m, p, _ in recovered["calls"]
                    )
                )
                self.assertEqual(recovered["record"]["completed_initial_reviews"], 1)
                self.assertEqual(
                    recovered["record"]["completed_verification_rounds"], 0
                )
                self.assertIsNotNone(recovered["state"])
                self.assertEqual(len(recovered["state"]["obligations"]), 2)
                self.assertFalse(
                    any(r["state"] == "APPROVED" for r in recovered["reviews"])
                )

    def test_response_loss_survives_restart_without_duplicate_native_review(self):
        for mode in ("window", "drift"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                lost = self.run_phase(root, mode, "lost")
                self.assertEqual(len(lost["reviews"]), 1)
                recovered = self.run_phase(root, mode, "lost_replay")
                self.assertEqual(len(recovered["reviews"]), 1)
                self.assertEqual(recovered["threads"], lost["threads"])
                self.assertIsNone(recovered["error"])
                self.assertIn(recovered["status"], {"published", "already_published"})
                self.assertEqual(
                    recovered["record"]["transaction"]["phase"], "publication_succeeded"
                )
                self.assertTrue(
                    all(
                        e["status"] == "confirmed"
                        for e in recovered["durable"]["effects"]
                    )
                )
                self.assertFalse(
                    any(
                        m == "POST" and p.endswith("/pulls/1/reviews")
                        for m, p, _ in recovered["calls"]
                    )
                )
                self.assertEqual(recovered["record"]["completed_initial_reviews"], 1)
                self.assertEqual(
                    recovered["record"]["completed_verification_rounds"], 0
                )
                self.assertIsNotNone(recovered["state"])
                self.assertEqual(len(recovered["state"]["obligations"]), 2)
                self.assertFalse(
                    any(r["state"] == "APPROVED" for r in recovered["reviews"])
                )

    def test_mutated_native_review_cannot_discharge_unknown_effect(self):
        for mode in ("window", "drift"):
            for mutation in ("edited", "foreign", "duplicated", "deleted"):
                with (
                    self.subTest(mode=mode, mutation=mutation),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    root = Path(directory)
                    self.run_phase(root, mode, "crash", expected_exit=73)
                    path = root / "service.json"
                    service = json.loads(path.read_text())
                    if mutation == "edited":
                        service["reviews"][0]["body"] += "\nHuman edit"
                    elif mutation == "foreign":
                        service["reviews"][0]["user"]["id"] = 31337
                    elif mutation == "duplicated":
                        duplicate = dict(service["reviews"][0], id=9999)
                        service["reviews"].append(duplicate)
                    else:
                        service["reviews"].clear()
                    path.write_text(json.dumps(service))
                    replay = self.run_phase(root, mode, "lost_replay")
                    self.assertEqual(replay["reviews"], service["reviews"])
                    self.assertEqual(replay["threads"], service["threads"])
                    self.assertFalse(
                        any(
                            m == "POST" and p.endswith("/pulls/1/reviews")
                            for m, p, _ in replay["calls"]
                        )
                    )
                    self.assertNotEqual(
                        replay["record"]["transaction"]["phase"],
                        "publication_succeeded",
                    )
                    self.assertTrue(
                        any(
                            e["status"] == "pending"
                            for e in replay["durable"]["effects"]
                        )
                    )
                    self.assertEqual(replay["record"]["completed_initial_reviews"], 1)
                    self.assertEqual(
                        replay["record"]["completed_verification_rounds"], 0
                    )

    def test_mutated_inline_receipt_never_recreates_unknown_thread(self):
        for mode in ("window", "drift"):
            for mutation in ("edited", "foreign", "duplicated", "deleted"):
                with (
                    self.subTest(mode=mode, mutation=mutation),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    root = Path(directory)
                    self.run_phase(root, mode, "crash", expected_exit=73)
                    path = root / "service.json"
                    service = json.loads(path.read_text())
                    comment = service["threads"][0]["comments"]["nodes"][0]
                    if mutation == "edited":
                        comment["body"] += "\nHuman edit"
                    elif mutation == "foreign":
                        comment["author"]["databaseId"] = 31337
                        comment["user"]["id"] = 31337
                    elif mutation == "duplicated":
                        duplicate = json.loads(json.dumps(service["threads"][0]))
                        duplicate["id"] = "PRRT_duplicate_native"
                        duplicate["comments"]["nodes"][0]["databaseId"] += 1
                        service["threads"].append(duplicate)
                    else:
                        service["threads"].clear()
                    path.write_text(json.dumps(service))
                    replay = self.run_phase(root, mode, "lost_replay")
                    self.assertEqual(len(replay["reviews"]), 1)
                    self.assertEqual(replay["threads"], service["threads"])
                    self.assertFalse(
                        any(
                            m == "POST" and p.endswith("/pulls/1/reviews")
                            for m, p, _ in replay["calls"]
                        )
                    )
                    self.assertIsNotNone(replay["state"])
                    self.assertTrue(
                        any(
                            o["slot_state"] == "uncertain"
                            for o in replay["state"]["obligations"]
                        )
                        or any(
                            e["status"] == "pending"
                            for e in replay["durable"]["effects"]
                        )
                    )
                    self.assertFalse(
                        any(r["state"] == "APPROVED" for r in replay["reviews"])
                    )

    def test_corrupt_selected_root_refuses_before_native_effects(self):
        for mode in ("window", "drift"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                first = self.run_phase(root, mode, "initial")
                ledger = LocalSessionLedger(
                    root / "ledger", enable_finding_lifecycle_writes=True
                )
                path = ledger._path(IDENTITY)
                stored = json.loads(path.read_text())
                stored["generation"] += 1  # No corresponding authenticated digest.
                path.write_text(json.dumps(stored))
                before = path.read_bytes()
                replay = self.run_phase(root, mode, "lost_replay")
                self.assertIsNotNone(replay["error"])
                self.assertEqual(replay["calls"], [])
                self.assertEqual(replay["reviews"], first["reviews"])
                self.assertEqual(path.read_bytes(), before)

    def test_rename_move_and_split_keep_native_identity_across_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self.run_phase(root, "drift", "initial")
            original = next(
                o for o in first["state"]["obligations"] if o["symbol"] == "auth"
            )
            for action, path in (
                ("rename", "src/renamed.py"),
                ("move", "src/moved.py"),
            ):
                moved = self.run_phase(root, "drift", action)
                self.assertIsNone(moved["error"])
                self.assertEqual(moved["status"], "published")
                row = next(
                    o
                    for o in moved["state"]["obligations"]
                    if o["id"] == original["id"]
                )
                self.assertEqual(row["criterion"], original["criterion"])
                self.assertEqual(row["target"], original["target"])
                self.assertEqual(row["path"], path)
                self.assertEqual(len(moved["threads"]), 1)
                self.assertEqual(
                    sum(
                        m == "PATCH" and "/pulls/comments/" in p
                        for m, p, _ in moved["calls"]
                    ),
                    1,
                )
            split = self.run_phase(root, "drift", "split")
            self.assertIsNone(split["error"])
            row = next(
                o for o in split["state"]["obligations"] if o["id"] == original["id"]
            )
            self.assertEqual(row["status"], "ambiguous-multi-target")
            self.assertEqual(
                set(row["candidate_paths"]), {"src/split_a.py", "src/split_b.py"}
            )
            partial = self.run_phase(root, "drift", "partial_split_fix", original)
            self.assertIsNone(partial["error"])
            row = next(
                o for o in partial["state"]["obligations"] if o["id"] == original["id"]
            )
            self.assertEqual(row["status"], "ambiguous-multi-target")
            self.assertEqual(
                set(row["candidate_paths"]), {"src/split_a.py", "src/split_b.py"}
            )
            resumed = self.run_phase(root, "drift", "ambiguous_resume")
            self.assertIsNone(resumed["error"])
            row = next(
                o for o in resumed["state"]["obligations"] if o["id"] == original["id"]
            )
            self.assertEqual(row["status"], "ambiguous-multi-target")
            self.assertEqual(row["target"], original["target"])
            self.assertFalse(any(r["state"] == "APPROVED" for r in resumed["reviews"]))

    def test_cold_maintainer_command_restores_hidden_obligation(self):
        for mode in ("window", "drift"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                first = self.run_phase(root, mode, "initial")
                self.assertIsNone(first["error"])
                rows = {o["symbol"]: o for o in first["state"]["obligations"]}
                command = self.run_phase(root, mode, "command", rows["auth"])
                self.assertIsNone(command["error"])
                self.assertTrue(command["applied"])
                self.assertIsNotNone(command["state"])
                carried = {o["symbol"]: o for o in command["state"]["obligations"]}
                self.assertEqual(carried["auth"]["status"], "authorized-override")
                self.assertEqual(carried["conn"]["id"], rows["conn"]["id"])
                self.assertFalse(
                    any(r["state"] == "APPROVED" for r in command["reviews"])
                )
                verify = self.run_phase(root, mode, "verify")
                self.assertIsNone(verify["error"])
                self.assertIsNotNone(verify["state"])
                self.assertFalse(
                    any(r["state"] == "APPROVED" for r in verify["reviews"])
                )

    def test_complete_ordinary_lifecycle_survives_fresh_processes(self):
        for mode in ("window", "drift"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                initial = self.run_phase(root, mode, "initial")
                self.assertIsNone(initial["error"])
                self.assertEqual(initial["status"], "published")
                self.assertIsNotNone(initial["state"])
                first = {o["symbol"]: o for o in initial["state"]["obligations"]}
                self.assertEqual(set(first), {"auth", "conn"})
                self.assertEqual(initial["state"]["active"], [first["auth"]["id"]])
                omitted = self.run_phase(root, mode, "omitted")
                self.assertIsNone(omitted["error"])
                self.assertEqual(omitted["status"], "published")
                self.assertIsNotNone(
                    omitted["state"],
                    (omitted["status"], omitted["diagnostic"], omitted["error"]),
                )
                carried = {o["symbol"]: o for o in omitted["state"]["obligations"]}
                for symbol in first:
                    self.assertEqual(
                        (carried[symbol]["id"], carried[symbol]["criterion"]),
                        (first[symbol]["id"], first[symbol]["criterion"]),
                    )
                self.assertNotEqual(carried["conn"]["status"], "verified-fixed")
                self.assertFalse(
                    any(r["state"] == "APPROVED" for r in omitted["reviews"])
                )
                promoted = self.run_phase(root, mode, "promote", first["auth"])
                self.assertIsNone(promoted["error"])
                self.assertEqual(promoted["status"], "published")
                self.assertEqual(promoted["state"]["active"], [first["conn"]["id"]])
                # The source's old guessed identities cannot match these server IDs.
                native_ids = {t["id"] for t in promoted["threads"]}
                for row in promoted["state"]["obligations"]:
                    if row["target"]:
                        self.assertIn(row["target"], native_ids)
                finished = self.run_phase(root, mode, "finish", first["conn"])
                self.assertIsNone(finished["error"])
                self.assertEqual(finished["status"], "published")
                self.assertEqual(
                    finished["record"]["transaction"]["phase"], "publication_succeeded"
                )
                self.assertEqual(finished["record"]["completed_initial_reviews"], 1)
                self.assertEqual(finished["record"]["completed_verification_rounds"], 3)
                self.assertTrue(
                    all(
                        o["status"] == "verified-fixed"
                        for o in finished["state"]["obligations"]
                    )
                )
                replay = self.run_phase(root, mode, "replay")
                self.assertIsNone(replay["error"])
                self.assertEqual(replay["status"], "already_published")
                self.assertEqual(replay["record"], replay["before_record"])
                self.assertEqual(replay["record"], finished["record"])
                self.assertEqual(replay["reviews"], finished["reviews"])
                self.assertEqual(replay["threads"], finished["threads"])
                self.assertFalse(
                    any(method != "GET" for method, _, _ in replay["calls"])
                )


if __name__ == "__main__":
    _worker(Path(sys.argv[1]), sys.argv[2], sys.argv[3], json.loads(sys.argv[4]))

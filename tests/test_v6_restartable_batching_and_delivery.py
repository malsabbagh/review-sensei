"""Comprehensive tests for v6 restartable batching, diff completeness, and bounded delivery."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from review_sensei_github.errors import (
    GitHubHTTPError,
    GitHubResponseTooLargeError,
)
from review_sensei_github.v6_coordinator_client import V6CoordinatorClient
from review_sensei_github.v6_credential_matrix import (
    WORKFLOW_READ_ONLY_SCOPES,
    GitHubEventKind,
    RepositoryVisibility,
)
from review_sensei_github.v6_fresh_analysis import (
    V6FreshAnalysisContext,
    V6FreshAnalysisError,
    V6PlannedBatch,
    _resolve_merge_base_via_git_commits,
    compute_trusted_policy_and_config_digests,
    filtered_hosted_environ,
    prepare_v6_fresh_analysis_context,
    verify_expected_hunks_reviewed,
)
from review_sensei_github.v6_pagination import NoProgressGuard
from review_sensei_github.v6_pilot_runner import (
    V6PilotRunConfig,
    V6PilotRunner,
)
from review_sensei_github.v6_recovery_engine import (
    DEFAULT_MAX_NATIVE_REVIEW_BODY_BYTES,
    DEFAULT_MAX_NATIVE_REVIEW_INLINE_COMMENTS,
    MutationSanityBound,
    MutationSanityBoundError,
    RecoveryCompleted,
    RecoveryHold,
    RecoveryPaused,
    RecoveryRequest,
    StaleWriterOwnershipError,
    TakeoverReconciliationResult,
    V6RecoveryEngine,
    _evidence_binding_for_request,
)
from review_sensei_github.v6_recovery_ledger import (
    MAX_MARKER_ENCODED_BYTES,
    BatchAttemptRecord,
    BatchDispatchRecord,
    BatchOutputRecord,
    BatchPlanManifestRecord,
    BatchPlanPartRecord,
    BatchPlanRecord,
    CompletionReceiptRecord,
    DurableHoldRecord,
    UnresolvedResultRecord,
    WriterOwnershipProof,
    canonical_sha256,
    extract_v6_record_markers,
    format_v6_record_marker,
    parse_v6_record_marker,
    record_to_dict,
)
from review_sensei_github.v6_transport import DualCredentialGitHubClient

from review_sensei.configuration import default_configuration
from review_sensei.models import ReviewComment, ReviewResult
from tests.test_v6_one_writer_runner import (
    APP_ID,
    BOT_LOGIN,
    BOT_USER_ID,
    HEAD_A,
    OWNER,
    PR_NUMBER,
    REPO,
    REPO_ID,
    WORKFLOW_SHA,
    _make_owner_record,
    _ScriptedTransport,
    _SimulatedCoordinatorStore,
    _SimulatedPRSurface,
)

BASE_SHA = "1" * 40
SLOT_B = f"v6-pr:github.com:{APP_ID}:7777:{REPO_ID}:{PR_NUMBER}"
SLOT_M = f"v6-pr:github.com:{APP_ID}:7778:{REPO_ID}:{PR_NUMBER}"
SLOT_S = f"v6-pr:github.com:{APP_ID}:7779:{REPO_ID}:{PR_NUMBER}"


class _DictGitTransport:
    """In-memory GitHub transport backing git commits, trees, blobs, and compare."""

    def __init__(
        self,
        *,
        commits: Mapping[str, Mapping[str, Any]],
        trees: Mapping[tuple[str, bool], Mapping[str, Any]],
        blobs: Mapping[str, bytes | Exception],
        compare_handler: Callable[
            [str, Mapping[str, str]], tuple[int, Mapping[str, str], Any]
        ]
        | None = None,
        pr_head_sha: str = HEAD_A,
        pr_base_sha: str = BASE_SHA,
    ) -> None:
        self.commits = dict(commits)
        self.trees = dict(trees)
        self.blobs = dict(blobs)
        self.compare_handler = compare_handler
        self.pr_head_sha = pr_head_sha
        self.pr_base_sha = pr_base_sha
        self.calls: list[tuple[str, str]] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[int, Mapping[str, str], Any]:
        del body, timeout_seconds
        parsed = urlsplit(path)
        self.calls.append((method, path))
        req_headers = headers or {}
        qs = parse_qs(parsed.query)

        if parsed.path == f"/repos/{OWNER}/{REPO}/pulls/{PR_NUMBER}":
            return (
                200,
                {},
                {
                    "number": PR_NUMBER,
                    "title": "Test PR",
                    "state": "open",
                    "head": {"sha": self.pr_head_sha},
                    "base": {"sha": self.pr_base_sha, "repo": {"id": REPO_ID}},
                },
            )

        if parsed.path.startswith(f"/repos/{OWNER}/{REPO}/compare/"):
            if self.compare_handler is not None:
                return self.compare_handler(path, req_headers)
            return 404, {}, {"message": "Compare disabled"}

        if parsed.path.startswith(f"/repos/{OWNER}/{REPO}/git/commits/"):
            sha = parsed.path.rsplit("/", 1)[-1]
            c = self.commits.get(sha)
            if c is None:
                return 404, {}, {"message": "Not Found"}
            if isinstance(c, Exception):
                raise c
            return 200, {}, dict(c)

        if parsed.path.startswith(f"/repos/{OWNER}/{REPO}/git/trees/"):
            sha = parsed.path.rsplit("/", 1)[-1]
            rec = qs.get("recursive", ["0"])[0] == "1"
            t = self.trees.get((sha, rec)) or (
                self.trees.get((sha, False)) if rec else None
            )
            if t is None:
                return 404, {}, {"message": "Not Found"}
            return 200, {}, dict(t)

        if parsed.path.startswith(f"/repos/{OWNER}/{REPO}/git/blobs/"):
            sha = parsed.path.rsplit("/", 1)[-1]
            b = self.blobs.get(sha)
            if b is None:
                return 404, {}, {"message": "Not Found"}
            if isinstance(b, Exception):
                raise b
            return (
                200,
                {},
                {
                    "sha": sha,
                    "size": len(b),
                    "encoding": "base64",
                    "content": base64.b64encode(b).decode("ascii"),
                },
            )

        return 404, {}, {"message": "Not Found"}


def _git(repo_dir: Path, *args: str, env: Mapping[str, str] | None = None) -> str:
    merged_env = dict(os.environ)
    merged_env.update(
        {
            "GIT_AUTHOR_NAME": "Test Bot",
            "GIT_AUTHOR_EMAIL": "bot@example.com",
            "GIT_COMMITTER_NAME": "Test Bot",
            "GIT_COMMITTER_EMAIL": "bot@example.com",
        }
    )
    if env:
        merged_env.update(env)
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo_dir),
        env=merged_env,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return proc.stdout.strip()


def _load_real_repo_into_transport(
    repo_dir: Path,
    *,
    base_sha: str,
    head_sha: str,
    compare_handler: Callable[
        [str, Mapping[str, str]], tuple[int, Mapping[str, str], Any]
    ]
    | None = None,
) -> _DictGitTransport:
    """Export all commits, trees, and blobs from a real git repo into ``_DictGitTransport``."""

    commits: dict[str, dict[str, Any]] = {}
    rev_list = _git(repo_dir, "rev-list", "--all").splitlines()
    for sha in rev_list:
        tree_sha = _git(repo_dir, "rev-parse", f"{sha}^{{tree}}")
        c_date = _git(repo_dir, "show", "-s", "--format=%cI", sha)
        parents_line = _git(repo_dir, "show", "-s", "--format=%P", sha)
        parents = [{"sha": p} for p in parents_line.split() if p]
        commits[sha] = {
            "sha": sha,
            "tree": {"sha": tree_sha},
            "committer": {"date": c_date},
            "parents": parents,
        }

    trees: dict[tuple[str, bool], dict[str, Any]] = {}
    blobs: dict[str, bytes] = {}
    for sha, meta in commits.items():
        tree_sha = str(meta["tree"]["sha"])
        raw_ls = _git(repo_dir, "ls-tree", "-r", tree_sha).splitlines()
        entries: list[dict[str, Any]] = []
        for line in raw_ls:
            mode_type_sha, path = line.split("\t", 1)
            mode, obj_type, obj_sha = mode_type_sha.split(" ")
            entries.append(
                {
                    "path": path,
                    "type": obj_type,
                    "sha": obj_sha,
                    "mode": mode,
                }
            )
            if obj_type == "blob" and obj_sha not in blobs:
                raw_bytes = subprocess.run(
                    ["git", "cat-file", "blob", obj_sha],
                    cwd=str(repo_dir),
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                ).stdout
                blobs[obj_sha] = raw_bytes
        trees[(tree_sha, True)] = {
            "sha": tree_sha,
            "truncated": False,
            "tree": entries,
        }
        trees[(sha, True)] = trees[(tree_sha, True)]

    return _DictGitTransport(
        commits=commits,
        trees=trees,
        blobs=blobs,
        compare_handler=compare_handler,
        pr_head_sha=head_sha,
        pr_base_sha=base_sha,
    )


def _fence_verifier(proof: WriterOwnershipProof) -> dict[str, Any]:
    return {
        "repository_id": proof.repository_id,
        "pull_request": proof.pull_request,
        "pr_head_sha": proof.pr_head_sha,
        "pr_state": "open",
        "active_writer_epoch": proof.writer_epoch,
        "active_run_id": proof.run_id,
        "active_run_attempt": proof.run_attempt,
        "run_status": "in_progress",
        "verified_by_broker": True,
    }


class _NoWriteAppTransport:
    def request(self, *_a: Any, **_kw: Any) -> tuple[int, Mapping[str, str], Any]:
        raise AssertionError("broker_app_transport must not be called")


class V6RestartableBatchingAndDeliveryTests(unittest.TestCase):
    """Coverage for v6 merge-base, completeness, batching, multipart, and detail delivery."""

    def test_paint_down_criss_cross_matches_git_merge_base_and_fails_closed_on_error(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp) / "repo"
            repo_dir.mkdir()
            _git(repo_dir, "init")

            def _commit(msg: str, rel_file: str, content: str, ts: str) -> str:
                p = repo_dir / rel_file
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")
                _git(repo_dir, "add", ".")
                return (
                    _git(
                        repo_dir,
                        "commit",
                        "-m",
                        msg,
                        env={"GIT_COMMITTER_DATE": ts, "GIT_AUTHOR_DATE": ts},
                    ),
                    _git(repo_dir, "rev-parse", "HEAD"),
                )[1]

            _commit("root", "a.txt", "0\n", "2026-11-27T01:00:00Z")
            base_branch = _git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD")
            c0 = _git(repo_dir, "rev-parse", "HEAD")

            # Build criss-cross: B1 from c0, H1 from c0; merge H1->B2, merge B1->H2.
            _git(repo_dir, "checkout", "-b", "branch-a", c0)
            a1 = _commit("a1", "a1.txt", "a1\n", "2026-11-27T02:00:00Z")

            _git(repo_dir, "checkout", "-b", "branch-b", c0)
            b1 = _commit("b1", "b1.txt", "b1\n", "2026-11-27T03:00:00Z")

            _git(
                repo_dir,
                "merge",
                "--no-ff",
                "branch-a",
                "-m",
                "merge a into b",
                env={
                    "GIT_COMMITTER_DATE": "2026-11-27T04:00:00Z",
                    "GIT_AUTHOR_DATE": "2026-11-27T04:00:00Z",
                },
            )
            b2 = _git(repo_dir, "rev-parse", "HEAD")

            _git(repo_dir, "checkout", "branch-a")
            _git(
                repo_dir,
                "merge",
                "--no-ff",
                b1,
                "-m",
                "merge b into a",
                env={
                    "GIT_COMMITTER_DATE": "2026-11-27T05:00:00Z",
                    "GIT_AUTHOR_DATE": "2026-11-27T05:00:00Z",
                },
            )
            a2 = _git(repo_dir, "rev-parse", "HEAD")

            expected_lcas = set(
                _git(repo_dir, "merge-base", "--all", b2, a2).splitlines()
            )
            self.assertEqual(expected_lcas, {a1, b1})

            transport = _load_real_repo_into_transport(
                repo_dir, base_sha=b2, head_sha=a2
            )
            client = DualCredentialGitHubClient(
                workflow_token_transport=transport,
                broker_app_transport=_NoWriteAppTransport(),
            )
            resolved = _resolve_merge_base_via_git_commits(
                read_client=client,
                owner=OWNER,
                repo=REPO,
                base_sha=b2,
                head_sha=a2,
                event=GitHubEventKind.PULL_REQUEST,
                visibility=RepositoryVisibility.PUBLIC,
                granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
                clock=lambda: 1_700_000_000.0,
            )
            self.assertIn(resolved, expected_lcas)

            # Inject HTTP error during parent fetch -> must fail closed (never return empty history).
            broken_commits = dict(transport.commits)
            broken_commits[b1] = GitHubHTTPError("bad gateway", http_status=502)  # type: ignore[assignment]
            broken_transport = _DictGitTransport(
                commits=broken_commits,
                trees=transport.trees,
                blobs=transport.blobs,
                pr_head_sha=a2,
                pr_base_sha=b2,
            )
            broken_client = DualCredentialGitHubClient(
                workflow_token_transport=broken_transport,
                broker_app_transport=_NoWriteAppTransport(),
            )
            with self.assertRaises(V6FreshAnalysisError) as err:
                _resolve_merge_base_via_git_commits(
                    read_client=broken_client,
                    owner=OWNER,
                    repo=REPO,
                    base_sha=b2,
                    head_sha=a2,
                    event=GitHubEventKind.PULL_REQUEST,
                    visibility=RepositoryVisibility.PUBLIC,
                    granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
                    clock=lambda: 1_700_000_000.0,
                )
            self.assertEqual(
                err.exception.diagnostic, "v6_merge_base_ancestry_incomplete"
            )
            del base_branch

    def test_compare_missing_path_recovered_from_trees_and_blobs_before_model_call(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp) / "repo"
            repo_dir.mkdir()
            _git(repo_dir, "init")
            (repo_dir / "src").mkdir()
            (repo_dir / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
            (repo_dir / "src" / "b.py").write_text("y = 1\n", encoding="utf-8")
            _git(repo_dir, "add", ".")
            _git(repo_dir, "commit", "-m", "base")
            base_sha = _git(repo_dir, "rev-parse", "HEAD")

            (repo_dir / "src" / "a.py").write_text("x = 2\n", encoding="utf-8")
            (repo_dir / "src" / "b.py").write_text("y = 2\n", encoding="utf-8")
            _git(repo_dir, "add", ".")
            _git(repo_dir, "commit", "-m", "head")
            head_sha = _git(repo_dir, "rev-parse", "HEAD")

            # Compare fast-path drops src/b.py completely while tree/blob has both.
            incomplete_v3_diff = (
                "diff --git a/src/a.py b/src/a.py\n"
                "--- a/src/a.py\n"
                "+++ b/src/a.py\n"
                "@@ -1 +1 @@\n"
                "-x = 1\n"
                "+x = 2\n"
            )

            def _compare(
                path: str, headers: Mapping[str, str]
            ) -> tuple[int, dict[str, str], Any]:
                if "application/vnd.github.v3.diff" in headers.get("Accept", ""):
                    return 200, {}, incomplete_v3_diff
                return (
                    200,
                    {},
                    {
                        "merge_base_commit": {"sha": base_sha},
                        "files": [{"filename": "src/a.py", "status": "modified"}],
                    },
                )

            transport = _load_real_repo_into_transport(
                repo_dir,
                base_sha=base_sha,
                head_sha=head_sha,
                compare_handler=_compare,
            )

            seen_diffs: list[str] = []

            class _MockSvc:
                def __init__(self, *_a: Any, **_kw: Any) -> None:
                    pass

                def review(self, req: Any) -> ReviewResult:
                    seen_diffs.append(req.diff)
                    return ReviewResult(
                        summary="Reviewed batch",
                        comments=(
                            ReviewComment(
                                path="src/b.py",
                                line=1,
                                side="RIGHT",
                                body="Check b",
                            ),
                        ),
                        provider="openai-compatible",
                        model="gpt-4o-mini",
                        review_status="complete",
                    )

            with (
                mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}),
                mock.patch(
                    "review_sensei_github.v6_fresh_analysis.ReviewService",
                    _MockSvc,
                ),
            ):
                ctx = prepare_v6_fresh_analysis_context(
                    owner=OWNER,
                    repo=REPO,
                    pull_request=PR_NUMBER,
                    head_sha=head_sha,
                    workflow_token_transport=transport,
                    granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
                    provider="openai-compatible",
                    model="gpt-4o-mini",
                    allow_custom_endpoint=True,
                )
                self.assertEqual(
                    len(seen_diffs), 0
                )  # Zero model calls during context prep!
                inv_paths = {e["path"]: e for e in ctx.inventory}
                self.assertEqual(set(inv_paths), {"src/a.py", "src/b.py"})
                combined = "\n".join(b.diff_text for b in ctx.planned_batches)
                self.assertIn("src/a.py", combined)
                self.assertIn("src/b.py", combined)

                # 413/oversized single blob without Compare patch is explicitly accounted unsupported (never dropped).
                bad_blobs = dict(transport.blobs)
                b_sha = next(
                    e["sha"]
                    for e in transport.trees[
                        (transport.commits[head_sha]["tree"]["sha"], True)
                    ]["tree"]
                    if e["path"] == "src/b.py"
                )
                bad_blobs[b_sha] = GitHubResponseTooLargeError("too large")
                oversized_transport = _DictGitTransport(
                    commits=transport.commits,
                    trees=transport.trees,
                    blobs=bad_blobs,
                    compare_handler=_compare,
                    pr_head_sha=head_sha,
                    pr_base_sha=base_sha,
                )
                ctx_unsup = prepare_v6_fresh_analysis_context(
                    owner=OWNER,
                    repo=REPO,
                    pull_request=PR_NUMBER,
                    head_sha=head_sha,
                    workflow_token_transport=oversized_transport,
                    granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
                    provider="openai-compatible",
                    model="gpt-4o-mini",
                    allow_custom_endpoint=True,
                )
                self.assertTrue(ctx_unsup.has_unsupported)
                self.assertTrue(
                    any(
                        e.get("path") == "src/b.py" and e.get("status") == "unsupported"
                        for e in ctx_unsup.non_text_changes
                    )
                )

                # Missing tree fails closed with v6_diff_inventory_incomplete BEFORE any model call.
                missing_tree_transport = _DictGitTransport(
                    commits=transport.commits,
                    trees={},
                    blobs=transport.blobs,
                    compare_handler=_compare,
                    pr_head_sha=head_sha,
                    pr_base_sha=base_sha,
                )
                with self.assertRaises(V6FreshAnalysisError) as err:
                    prepare_v6_fresh_analysis_context(
                        owner=OWNER,
                        repo=REPO,
                        pull_request=PR_NUMBER,
                        head_sha=head_sha,
                        workflow_token_transport=missing_tree_transport,
                        granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
                        provider="openai-compatible",
                        model="gpt-4o-mini",
                        allow_custom_endpoint=True,
                    )
                self.assertEqual(
                    err.exception.diagnostic, "v6_diff_inventory_incomplete"
                )

    def test_real_git_repo_oracle_grouping_subdivision_prompt_budget_and_non_text(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp) / "repo"
            repo_dir.mkdir()
            _git(repo_dir, "init")
            (repo_dir / "src").mkdir()
            (repo_dir / "tests").mkdir()
            (repo_dir / "src" / "auth.py").write_text(
                "def ok():\n    return 1\n", encoding="utf-8"
            )
            (repo_dir / "tests" / "test_auth.py").write_text(
                "def test_ok():\n    assert True\n", encoding="utf-8"
            )
            (repo_dir / "old_name.txt").write_text("same-content\n", encoding="utf-8")
            (repo_dir / "big_mod.py").write_text(
                "\n".join(f"v_{i} = {i}" for i in range(80)) + "\n",
                encoding="utf-8",
            )
            _git(repo_dir, "add", ".")
            _git(repo_dir, "commit", "-m", "base")
            base_sha = _git(repo_dir, "rev-parse", "HEAD")

            (repo_dir / "src" / "auth.py").write_text(
                "def ok():\n    return 2\n", encoding="utf-8"
            )
            (repo_dir / "tests" / "test_auth.py").write_text(
                "def test_ok():\n    assert 2 == 2\n", encoding="utf-8"
            )
            (repo_dir / "old_name.txt").unlink()
            (repo_dir / "new_name.txt").write_text("same-content\n", encoding="utf-8")
            (repo_dir / "bin.dat").write_bytes(b"\x00\x01\xffbinary")
            (repo_dir / "big_mod.py").write_text(
                "\n".join(f"v_{i} = {i + 100}" for i in range(80)) + "\n",
                encoding="utf-8",
            )
            _git(repo_dir, "add", ".")
            _git(repo_dir, "commit", "-m", "head")
            head_sha = _git(repo_dir, "rev-parse", "HEAD")

            transport = _load_real_repo_into_transport(
                repo_dir, base_sha=base_sha, head_sha=head_sha
            )

            class _CapturingService:
                def __init__(self, *_a: Any, **_kw: Any) -> None:
                    pass

                def review(self, req: Any) -> ReviewResult:
                    return ReviewResult(
                        summary="OK",
                        comments=(),
                        provider="openai-compatible",
                        model="gpt-4o-mini",
                        review_status="complete",
                    )

            with (
                mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}),
                mock.patch(
                    "review_sensei_github.v6_fresh_analysis.ReviewService",
                    _CapturingService,
                ),
            ):
                ctx = prepare_v6_fresh_analysis_context(
                    owner=OWNER,
                    repo=REPO,
                    pull_request=PR_NUMBER,
                    head_sha=head_sha,
                    workflow_token_transport=transport,
                    granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
                    provider="openai-compatible",
                    model="gpt-4o-mini",
                    allow_custom_endpoint=True,
                    batch_max_bytes=900,
                    batch_max_hunks=4,
                    batch_prompt_bytes=32_768,
                )
                # Strict 1x primary partition across all batches:
                assigned_hunks: list[str] = []
                for b in ctx.planned_batches:
                    self.assertLessEqual(b.prompt_bytes, 32_768)
                    assigned_hunks.extend(b.primary_hunk_ids)
                self.assertEqual(len(assigned_hunks), len(set(assigned_hunks)))
                self.assertEqual(set(assigned_hunks), set(ctx.all_expected_hunk_ids))

                # Related source + test pair grouped together when they fit:
                pair_batches = [
                    b
                    for b in ctx.planned_batches
                    if "src/auth.py" in b.paths and "tests/test_auth.py" in b.paths
                ]
                self.assertEqual(len(pair_batches), 1)

                # Binary & rename_only explicitly accounted for (never silently dropped):
                non_text = {e["path"]: e["status"] for e in ctx.non_text_changes}
                self.assertEqual(non_text.get("bin.dat"), "binary")
                self.assertEqual(non_text.get("new_name.txt"), "rename_only")

    def test_restartable_batches_reuse_dispatch_ambiguity_hold_and_budget(self) -> None:
        config = default_configuration()
        policy_digest, config_digest = compute_trusted_policy_and_config_digests(
            config, provider_name="openai-compatible", model_name="gpt-4o-mini"
        )
        pr = _SimulatedPRSurface(head_sha=HEAD_A)
        owner_rec = _make_owner_record(slot_key=SLOT_B, generation=1, lease_epoch=1)
        pr.add_comment(format_v6_record_marker(owner_rec), is_app=True)

        diff_a = (
            "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n"
            "@@ -1 +1 @@\n-a = 1\n+a = 2\n"
        )
        diff_b = (
            "diff --git a/src/b.py b/src/b.py\n--- a/src/b.py\n+++ b/src/b.py\n"
            "@@ -1 +1 @@\n-b = 1\n+b = 2\n"
        )

        # Build a 2-batch context with a mock provider counting actual calls.
        calls_made: list[str] = []

        class _Svc:
            def review(self, req: Any) -> ReviewResult:
                calls_made.append(req.diff)
                path = "src/a.py" if "src/a.py" in req.diff else "src/b.py"
                return ReviewResult(
                    summary=f"Summary {path}",
                    comments=(
                        ReviewComment(
                            path=path, line=1, side="RIGHT", body=f"Note {path}"
                        ),
                    ),
                    provider="openai-compatible",
                    model="gpt-4o-mini",
                    review_status="complete",
                )

        from review_sensei_github.v6_fresh_analysis import V6PlannedBatch

        hunk_a = canonical_sha256({"path": "src/a.py", "idx": 0})[:16]
        hunk_b = canonical_sha256({"path": "src/b.py", "idx": 1})[:16]
        b0 = V6PlannedBatch(
            batch_id="batch-000",
            diff_text=diff_a,
            diff_sha256=canonical_sha256(diff_a),
            prompt_bytes=512,
            paths=("src/a.py",),
            primary_hunk_ids=(hunk_a,),
            parent_hunk_ids=(hunk_a,),
        )
        b1 = V6PlannedBatch(
            batch_id="batch-001",
            diff_text=diff_b,
            diff_sha256=canonical_sha256(diff_b),
            prompt_bytes=512,
            paths=("src/b.py",),
            primary_hunk_ids=(hunk_b,),
            parent_hunk_ids=(hunk_b,),
        )
        inv_sha = canonical_sha256(["src/a.py", "src/b.py"])

        def _make_context(max_calls: int = 4) -> V6FreshAnalysisContext:
            return V6FreshAnalysisContext(
                owner=OWNER,
                repo=REPO,
                pull_request=PR_NUMBER,
                pr_title="Two-batch PR",
                base_sha=BASE_SHA,
                merge_base_sha=BASE_SHA,
                head_sha=HEAD_A,
                inventory_sha256=inv_sha,
                inventory=(
                    {
                        "path": "src/a.py",
                        "status": "modified",
                        "disposition": "text",
                        "hunk_ids": [hunk_a],
                    },
                    {
                        "path": "src/b.py",
                        "status": "modified",
                        "disposition": "text",
                        "hunk_ids": [hunk_b],
                    },
                ),
                non_text_changes=(),
                policy_digest=policy_digest,
                configuration_digest=config_digest,
                max_provider_calls=max_calls,
                planned_batches=(b0, b1),
                all_expected_hunk_ids=(hunk_a, hunk_b),
                sub_hunks_by_parent={},
                combined_diff_sha256=canonical_sha256(diff_a + diff_b),
                provider_name="openai-compatible",
                model_name="gpt-4o-mini",
                service=_Svc(),
            )

        client = DualCredentialGitHubClient(
            workflow_token_transport=_ScriptedTransport(pr.handle_wf),
            broker_app_transport=_ScriptedTransport(pr.handle_app),
        )
        proof = WriterOwnershipProof(
            repository_id=REPO_ID,
            pull_request=PR_NUMBER,
            operation_id="op-batch",
            run_id=100,
            run_attempt=1,
            writer_epoch=1,
            pr_head_sha=HEAD_A,
            verified_by_broker=True,
        )
        req = RecoveryRequest(
            owner=OWNER,
            repo=REPO,
            repository_id=REPO_ID,
            pull_request=PR_NUMBER,
            operation_id="op-batch",
            ownership_proof=proof,
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            expected_app_id=APP_ID,
            expected_bot_login=BOT_LOGIN,
            expected_bot_user_id=BOT_USER_ID,
            slot_key=SLOT_B,
            generation=1,
            owner_marker=owner_rec,
            fresh_analysis_context_loader=lambda: _make_context(4),
        )

        # Pre-seed BatchPlan + BatchDispatch(b0) + BatchOutput(b0) -> rerun reuses b0 (1 call total for b1)!
        ctx_tmp = _make_context(4)
        in_dig = canonical_sha256(
            {
                "base_sha": BASE_SHA,
                "merge_base_sha": BASE_SHA,
                "head_sha": HEAD_A,
                "inventory_sha256": inv_sha,
                "policy_digest": policy_digest,
                "configuration_digest": config_digest,
            }
        )[:12]
        plan_rec = BatchPlanRecord(
            record_id=f"batch-plan:op-batch:{HEAD_A[:12]}:{in_dig}",
            operation_id="op-batch",
            base_sha=BASE_SHA,
            merge_base_sha=BASE_SHA,
            pr_head_sha=HEAD_A,
            inventory_sha256=inv_sha,
            policy_digest=policy_digest,
            configuration_digest=config_digest,
            max_provider_calls=4,
            batches=tuple(b.to_plan_dict() for b in ctx_tmp.planned_batches),
            non_text_changes=(),
        )
        plan_sha = canonical_sha256(record_to_dict(plan_rec))
        pr.add_comment(format_v6_record_marker(plan_rec), is_app=True)
        pr.add_comment(
            format_v6_record_marker(
                BatchDispatchRecord(
                    record_id=f"batch-dispatch:op-batch:{HEAD_A[:12]}:{in_dig}:batch-000:1",
                    operation_id="op-batch",
                    batch_id="batch-000",
                    attempt=1,
                    plan_sha256=plan_sha,
                    prompt_sha256=canonical_sha256(b0.to_plan_dict()),
                    generation=1,
                )
            ),
            is_app=True,
        )
        pr.add_comment(
            format_v6_record_marker(
                BatchOutputRecord(
                    record_id=f"batch-output:op-batch:{HEAD_A[:12]}:{in_dig}:batch-000:1",
                    operation_id="op-batch",
                    batch_id="batch-000",
                    plan_sha256=plan_sha,
                    hunk_ids_reviewed=(hunk_a,),
                    findings=(
                        {
                            "path": "src/a.py",
                            "line": 1,
                            "side": "RIGHT",
                            "body": "Cached a",
                        },
                    ),
                    summary="Cached a summary",
                    provider="openai-compatible",
                    model="gpt-4o-mini",
                    review_status="complete",
                    attempt=1,
                )
            ),
            is_app=True,
        )

        # Subcase A: dispatch for batch-001 WITHOUT output -> HOLD ambiguous_batch_inference_outcome (0 calls)!
        pr_ambig = _SimulatedPRSurface(head_sha=HEAD_A)
        for c in pr.comments:
            pr_ambig.add_comment(c["body"], is_app=True)
        pr_ambig.add_comment(
            format_v6_record_marker(
                BatchDispatchRecord(
                    record_id=f"batch-dispatch:op-batch:{HEAD_A[:12]}:{in_dig}:batch-001:1",
                    operation_id="op-batch",
                    batch_id="batch-001",
                    attempt=1,
                    plan_sha256=plan_sha,
                    prompt_sha256=canonical_sha256(b1.to_plan_dict()),
                    generation=1,
                )
            ),
            is_app=True,
        )
        eng_ambig = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=_ScriptedTransport(pr_ambig.handle_wf),
                broker_app_transport=_ScriptedTransport(pr_ambig.handle_app),
            ),
            now_fn=lambda: 1_700_000_000.0,
            sleeper=lambda _s: None,
            broker_fence_verifier=_fence_verifier,
            mutation_bound=MutationSanityBound(max_writes=32),
        )
        res_ambig = eng_ambig.recover_and_publish(
            replace(req, emit_durable_hold_on_pr=True)
        )
        self.assertIsInstance(res_ambig, RecoveryHold)
        assert isinstance(res_ambig, RecoveryHold)
        self.assertEqual(res_ambig.blocker, "ambiguous_batch_inference_outcome")
        self.assertEqual(len(calls_made), 0)
        hold_recs = [
            r
            for c in pr_ambig.comments
            for r in extract_v6_record_markers(c["body"], strict=True)
            if isinstance(r, DurableHoldRecord)
        ]
        self.assertTrue(
            any(h.reason_code == "ambiguous_batch_inference_outcome" for h in hold_recs)
        )

        # Subcase B: budget exhausted (max_provider_calls = 1 and b0 already consumed 1 call) -> HOLD!
        pr_budget = _SimulatedPRSurface(head_sha=HEAD_A)
        plan_1 = replace(plan_rec, max_provider_calls=1)
        plan_1_sha = canonical_sha256(record_to_dict(plan_1))
        pr_budget.add_comment(format_v6_record_marker(owner_rec), is_app=True)
        pr_budget.add_comment(format_v6_record_marker(plan_1), is_app=True)
        pr_budget.add_comment(
            format_v6_record_marker(
                BatchDispatchRecord(
                    record_id=f"batch-dispatch:op-batch:{HEAD_A[:12]}:{in_dig}:batch-000:1",
                    operation_id="op-batch",
                    batch_id="batch-000",
                    attempt=1,
                    plan_sha256=plan_1_sha,
                    prompt_sha256=canonical_sha256(b0.to_plan_dict()),
                    generation=1,
                )
            ),
            is_app=True,
        )
        pr_budget.add_comment(
            format_v6_record_marker(
                BatchOutputRecord(
                    record_id=f"batch-output:op-batch:{HEAD_A[:12]}:{in_dig}:batch-000:1",
                    operation_id="op-batch",
                    batch_id="batch-000",
                    plan_sha256=plan_1_sha,
                    hunk_ids_reviewed=(hunk_a,),
                    findings=(),
                    summary="ok",
                    provider="openai-compatible",
                    model="gpt-4o-mini",
                    attempt=1,
                )
            ),
            is_app=True,
        )
        eng_budget = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=_ScriptedTransport(pr_budget.handle_wf),
                broker_app_transport=_ScriptedTransport(pr_budget.handle_app),
            ),
            now_fn=lambda: 1_700_000_000.0,
            sleeper=lambda _s: None,
            broker_fence_verifier=_fence_verifier,
            mutation_bound=MutationSanityBound(max_writes=32),
        )
        res_budget = eng_budget.recover_and_publish(
            replace(req, fresh_analysis_context_loader=lambda: _make_context(1))
        )
        self.assertIsInstance(res_budget, RecoveryHold)
        assert isinstance(res_budget, RecoveryHold)
        self.assertEqual(res_budget.blocker, "review_provider_call_budget_exhausted")
        self.assertEqual(len(calls_made), 0)

        # Subcase C: resumes batch-001 ONLY (1 model call total) and replays cached batch-000 -> completes!
        eng_ok = V6RecoveryEngine(
            client,
            now_fn=lambda: 1_700_000_000.0,
            sleeper=lambda _s: None,
            broker_fence_verifier=_fence_verifier,
            mutation_bound=MutationSanityBound(max_writes=32),
        )
        res_ok = eng_ok.recover_and_publish(req)
        self.assertIsInstance(res_ok, RecoveryCompleted)
        assert isinstance(res_ok, RecoveryCompleted)
        self.assertEqual(res_ok.model_calls_started, 1)
        self.assertEqual(len(calls_made), 1)
        self.assertIn("src/b.py", calls_made[0])
        self.assertEqual(len(pr.reviews), 1)
        self.assertIn("Cached a summary", pr.reviews[0]["body"])
        self.assertIn("Summary src/b.py", pr.reviews[0]["body"])

        # Subcase D: complete final result already persisted -> rerun replays with 0 model calls!
        calls_made.clear()
        proof2 = replace(proof, writer_epoch=2)
        owner_rec2 = _make_owner_record(
            slot_key=SLOT_B, generation=2, lease_epoch=2, operation_id="op-batch"
        )
        res_replay = eng_ok.recover_and_publish(
            replace(
                req,
                ownership_proof=proof2,
                generation=2,
                owner_marker=owner_rec2,
            )
        )
        self.assertIsInstance(res_replay, RecoveryCompleted)
        assert isinstance(res_replay, RecoveryCompleted)
        self.assertEqual(res_replay.model_calls_started, 0)
        self.assertEqual(len(calls_made), 0)

    def test_post_ledger_marker_no_blind_retry_and_multipart_preflight(self) -> None:
        pr = _SimulatedPRSurface(head_sha=HEAD_A)
        owner_rec = _make_owner_record(slot_key=SLOT_M, generation=1, lease_epoch=1)
        pr.add_comment(format_v6_record_marker(owner_rec), is_app=True)
        post_count = 0

        def _drop_after_land(
            call: dict[str, Any],
        ) -> tuple[int, Mapping[str, str], Any]:
            nonlocal post_count
            if call["method"] == "POST" and call["path"].endswith("/comments"):
                post_count += 1
                pr.handle_app(call)
                raise TimeoutError("simulated lost reply after write")
            return pr.handle_app(call)

        proof = WriterOwnershipProof(
            repository_id=REPO_ID,
            pull_request=PR_NUMBER,
            operation_id="op-rescan",
            run_id=101,
            run_attempt=1,
            writer_epoch=1,
            pr_head_sha=HEAD_A,
            verified_by_broker=True,
        )
        req = RecoveryRequest(
            owner=OWNER,
            repo=REPO,
            repository_id=REPO_ID,
            pull_request=PR_NUMBER,
            operation_id="op-rescan",
            ownership_proof=proof,
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            expected_app_id=APP_ID,
            expected_bot_login=BOT_LOGIN,
            expected_bot_user_id=BOT_USER_ID,
            slot_key=SLOT_M,
            generation=1,
            owner_marker=owner_rec,
        )
        rec = UnresolvedResultRecord(
            record_id=f"result:op-rescan:{HEAD_A[:12]}",
            operation_id="op-rescan",
            pr_head_sha=HEAD_A,
            payload_sha256=canonical_sha256(
                ({"path": "a.py", "line": 1, "body": "x"},)
            ),
            findings_payload=({"path": "a.py", "line": 1, "body": "x"},),
            required_evidence_digests=(),
        )

        # Single POST + single rescan adopts landed record without a second POST:
        eng = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=_ScriptedTransport(pr.handle_wf),
                broker_app_transport=_ScriptedTransport(_drop_after_land),
            ),
            now_fn=lambda: 1_700_000_000.0,
            sleeper=lambda _s: None,
            broker_fence_verifier=_fence_verifier,
            mutation_bound=MutationSanityBound(max_writes=16),
        )
        cid = eng._post_ledger_marker_with_rescan(
            request=req,
            guard=NoProgressGuard(operation_id="op-rescan"),
            prior_hint=None,
            record=rec,
            effect_kind_label="unresolved_result",
            find_existing_cid=lambda ledger: ledger.comment_ids_by_record_id.get(
                rec.record_id
            ),
        )
        self.assertGreater(cid, 0)
        self.assertEqual(post_count, 1)

        # Upfront multipart preflight fails BEFORE writing part 0 when write budget too small:
        pr_preflight = _SimulatedPRSurface(head_sha=HEAD_A)
        pr_preflight.add_comment(format_v6_record_marker(owner_rec), is_app=True)
        oversized_findings = tuple(
            {
                "path": "a.py",
                "line": i + 1,
                "body": "".join(
                    hashlib.sha256(f"{i}:{j}".encode()).hexdigest() for j in range(400)
                ),
            }
            for i in range(6)
        )
        eng_small_budget = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=_ScriptedTransport(pr_preflight.handle_wf),
                broker_app_transport=_ScriptedTransport(pr_preflight.handle_app),
            ),
            now_fn=lambda: 1_700_000_000.0,
            sleeper=lambda _s: None,
            broker_fence_verifier=_fence_verifier,
            mutation_bound=MutationSanityBound(max_writes=2),
        )
        with self.assertRaises(MutationSanityBoundError):
            eng_small_budget._persist_initial_result(
                request=req,
                guard=NoProgressGuard(operation_id="op-rescan"),
                prior_hint=None,
                findings_tuple=oversized_findings,
                raw_digests=(),
                collected=eng_small_budget._collect_full_ledger(
                    (),
                    expected_app_id=APP_ID,
                    expected_bot_login=BOT_LOGIN,
                    expected_bot_user_id=BOT_USER_ID,
                ),
            )
        # No partial ResultPartRecord comments posted!
        self.assertEqual(len(pr_preflight.comments), 1)

    def test_changed_inputs_at_same_head_never_publish_stale_unpublished_result(
        self,
    ) -> None:
        cfg_default = default_configuration()
        pol_1, cfg_1 = compute_trusted_policy_and_config_digests(
            cfg_default, provider_name="openai-compatible", model_name="gpt-4o-mini"
        )
        pol_2 = canonical_sha256({"policy_changed": True})
        pr = _SimulatedPRSurface(head_sha=HEAD_A)
        owner_rec = _make_owner_record(slot_key=SLOT_S, generation=1, lease_epoch=1)
        pr.add_comment(format_v6_record_marker(owner_rec), is_app=True)

        # Request 1 persists unpublished result bound to (BASE_SHA, pol_1, cfg_1).
        findings_1 = ({"path": "src/a.py", "line": 1, "body": "Stale finding"},)
        proof = WriterOwnershipProof(
            repository_id=REPO_ID,
            pull_request=PR_NUMBER,
            operation_id="op-stale",
            run_id=200,
            run_attempt=1,
            writer_epoch=1,
            pr_head_sha=HEAD_A,
            verified_by_broker=True,
        )
        req1 = RecoveryRequest(
            owner=OWNER,
            repo=REPO,
            repository_id=REPO_ID,
            pull_request=PR_NUMBER,
            operation_id="op-stale",
            ownership_proof=proof,
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            expected_app_id=APP_ID,
            expected_bot_login=BOT_LOGIN,
            expected_bot_user_id=BOT_USER_ID,
            slot_key=SLOT_S,
            generation=1,
            owner_marker=owner_rec,
            base_sha=BASE_SHA,
            merge_base_sha=BASE_SHA,
            inventory_sha256="a" * 64,
            policy_digest=pol_1,
            configuration_digest=cfg_1,
        )
        binding = _evidence_binding_for_request(req1, pr_head_sha=HEAD_A)
        self.assertEqual(binding["base_sha"], BASE_SHA)
        self.assertEqual(binding["policy_digest"], pol_1)
        self.assertEqual(binding["configuration_digest"], cfg_1)

        eng = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=_ScriptedTransport(pr.handle_wf),
                broker_app_transport=_ScriptedTransport(pr.handle_app),
            ),
            now_fn=lambda: 1_700_000_000.0,
            sleeper=lambda _s: None,
            broker_fence_verifier=_fence_verifier,
            mutation_bound=MutationSanityBound(max_writes=32),
        )
        eng._persist_initial_result(
            request=req1,
            guard=NoProgressGuard(operation_id="op-stale"),
            prior_hint=None,
            findings_tuple=findings_1,
            raw_digests=(),
            collected=eng._collect_full_ledger(
                (),
                expected_app_id=APP_ID,
                expected_bot_login=BOT_LOGIN,
                expected_bot_user_id=BOT_USER_ID,
            ),
        )

        # Changed policy_digest at the SAME head_sha in replay-only mode must HOLD, never publish stale result!
        req2 = replace(req1, policy_digest=pol_2)
        hold_res = eng.recover_and_publish(req2)
        self.assertIsInstance(hold_res, RecoveryHold)
        assert isinstance(hold_res, RecoveryHold)
        self.assertEqual(hold_res.blocker, "stale_unpublished_result_inputs_changed")
        self.assertEqual(len(pr.reviews), 0)

    def test_oversized_findings_split_into_bounded_comment_review_and_verified_details_idempotent(
        self,
    ) -> None:
        now = 1_700_120_000.0
        pr = _SimulatedPRSurface(head_sha=HEAD_A)
        coord = _SimulatedCoordinatorStore(clock=lambda: now, pr=pr)
        client = (
            V6PilotRunner(
                coordinator=V6PilotRunner,  # replaced below
                workflow_token_transport=_ScriptedTransport(pr.handle_wf),
                app_transport_factory=lambda _g: _ScriptedTransport(pr.handle_app),
            )
            if False
            else None
        )
        del client

        from review_sensei_github.v6_coordinator_client import V6CoordinatorClient

        # Build >50 inline findings + envelope so overflow MUST go into verified detail comments.
        many_comments = [
            {
                "path": f"src/file_{i:02d}.py",
                "line": i + 1,
                "side": "RIGHT",
                "body": f"Finding body #{i:02d}",
            }
            for i in range(DEFAULT_MAX_NATIVE_REVIEW_INLINE_COMMENTS + 7)
        ]
        envelope = (
            {
                "_v6_review_envelope": "reviewsensei:v6:review_envelope:v1",
                "review_body": "Bounded native review summary.",
                "event": "COMMENT",
                "comments": many_comments,
                "findings": [],
                "provider": "openai-compatible",
                "model": "gpt-4o-mini",
                "review_status": "complete",
                "content_digest": "e" * 64,
            },
        )

        coord_client = V6CoordinatorClient(
            base_url="https://coordinator.example",
            oidc_token_provider=lambda: "oidc.jwt.1",
            opener=coord.opener,
            clock=lambda: now,
        )
        runner = V6PilotRunner(
            coordinator=coord_client,
            workflow_token_transport=_ScriptedTransport(pr.handle_wf),
            app_transport_factory=lambda _g: _ScriptedTransport(pr.handle_app),
            clock=lambda: now,
            sleeper=lambda _s: None,
            broker_fence_verifier=lambda _p: {
                "repository_id": REPO_ID,
                "pull_request": PR_NUMBER,
                "pr_head_sha": HEAD_A,
                "pr_state": "open",
                "active_writer_epoch": coord.lease_epoch,
                "active_run_id": 6001,
                "active_run_attempt": 1,
                "run_status": "in_progress",
                "verified_by_broker": True,
            },
        )
        cfg = V6PilotRunConfig(
            owner=OWNER,
            repo=REPO,
            repository_id=REPO_ID,
            pull_request=PR_NUMBER,
            head_sha=HEAD_A,
            operation_id="op-detail-flow",
            run_id=6001,
            run_attempt=1,
            workflow_sha=WORKFLOW_SHA,
            expected_app_id=APP_ID,
            expected_bot_login=BOT_LOGIN,
            expected_bot_user_id=BOT_USER_ID,
            initial_result_loader=lambda: (envelope, ()),
        )
        outcome1 = runner.run(cfg)
        self.assertEqual(outcome1.status, "completed")
        self.assertTrue(outcome1.lease_released)
        self.assertEqual(len(pr.reviews), 1)
        self.assertLessEqual(
            len(pr.reviews[0]["body"].encode("utf-8")),
            DEFAULT_MAX_NATIVE_REVIEW_BODY_BYTES,
        )
        # Detail comments contain the 7 overflow findings with clickable links in the single review.
        detail_comments = [
            c for c in pr.comments if "## ReviewSensei Detail (" in c["body"]
        ]
        self.assertGreaterEqual(len(detail_comments), 1)
        combined_details = "\n".join(c["body"] for c in detail_comments)
        for i in range(
            DEFAULT_MAX_NATIVE_REVIEW_INLINE_COMMENTS,
            DEFAULT_MAX_NATIVE_REVIEW_INLINE_COMMENTS + 7,
        ):
            self.assertIn(f"Finding body #{i:02d}", combined_details)
        for dc in detail_comments:
            self.assertIn(dc["html_url"], pr.reviews[0]["body"])
            self.assertIn(
                "reviewsensei:v6:effect op=op-detail-flow:detail:", dc["body"]
            )

        # Each detail comment has BOTH a WriteIntentRecord and CompletionReceiptRecord.
        recs = [
            r
            for c in pr.comments
            for r in extract_v6_record_markers(c["body"], strict=True)
        ]
        detail_receipts = [
            r
            for r in recs
            if isinstance(r, CompletionReceiptRecord)
            and r.operation_id.startswith("op-detail-flow:detail:")
        ]
        self.assertEqual(len(detail_receipts), len(detail_comments))

        # Rerun is strictly idempotent: 0 duplicate detail comments, 0 duplicate reviews!
        review_count_before = len(pr.reviews)
        detail_count_before = len(detail_comments)
        coord.run_id = 6002
        runner2 = V6PilotRunner(
            coordinator=V6CoordinatorClient(
                base_url="https://coordinator.example",
                oidc_token_provider=lambda: "oidc.jwt.2",
                opener=coord.opener,
                clock=lambda: now,
            ),
            workflow_token_transport=_ScriptedTransport(pr.handle_wf),
            app_transport_factory=lambda _g: _ScriptedTransport(pr.handle_app),
            clock=lambda: now,
            sleeper=lambda _s: None,
            broker_fence_verifier=lambda _p: {
                "repository_id": REPO_ID,
                "pull_request": PR_NUMBER,
                "pr_head_sha": HEAD_A,
                "pr_state": "open",
                "active_writer_epoch": coord.lease_epoch,
                "active_run_id": 6002,
                "active_run_attempt": 1,
                "run_status": "in_progress",
                "verified_by_broker": True,
            },
        )
        outcome2 = runner2.run(replace(cfg, run_id=6002))
        self.assertEqual(outcome2.status, "completed")
        self.assertEqual(len(pr.reviews), review_count_before)
        self.assertEqual(
            len([c for c in pr.comments if "## ReviewSensei Detail (" in c["body"]]),
            detail_count_before,
        )

    def test_partitioned_batch_output_resume_zero_extra_calls(self) -> None:
        from review_sensei_github.v6_fresh_analysis import V6PlannedBatch

        cfg = default_configuration()
        pol_d, cfg_d = compute_trusted_policy_and_config_digests(
            cfg, provider_name="openai-compatible", model_name="gpt-4o-mini"
        )
        pr = _SimulatedPRSurface(head_sha=HEAD_A)
        owner_rec = _make_owner_record(slot_key=SLOT_B, generation=1, lease_epoch=1)
        pr.add_comment(format_v6_record_marker(owner_rec), is_app=True)
        hunk_0 = canonical_sha256({"path": "src/a.py", "idx": 0})[:16]
        hunk_1 = canonical_sha256({"path": "src/b.py", "idx": 0})[:16]
        b0 = V6PlannedBatch(
            batch_id="batch-000",
            diff_text="diff --git a/src/a.py b/src/a.py\n@@ -1 +1 @@\n-1\n+2\n",
            diff_sha256=canonical_sha256("a"),
            prompt_bytes=200,
            paths=("src/a.py",),
            primary_hunk_ids=(hunk_0,),
            parent_hunk_ids=(hunk_0,),
        )
        b1 = V6PlannedBatch(
            batch_id="batch-001",
            diff_text="diff --git a/src/b.py b/src/b.py\n@@ -1 +1 @@\n-1\n+2\n",
            diff_sha256=canonical_sha256("b"),
            prompt_bytes=200,
            paths=("src/b.py",),
            primary_hunk_ids=(hunk_1,),
            parent_hunk_ids=(hunk_1,),
        )
        inv_sha = canonical_sha256(["src/a.py", "src/b.py"])
        huge_comments = tuple(
            ReviewComment(
                path="src/a.py",
                line=1,
                side="RIGHT",
                body="".join(
                    hashlib.sha256(f"{k}:{m}".encode()).hexdigest() for m in range(150)
                ),
            )
            for k in range(10)
        )
        calls: list[str] = []

        class _SvcCrashOnce:
            def __init__(self, *, fail_b1: bool) -> None:
                self.fail_b1 = fail_b1

            def review(self, req: Any) -> ReviewResult:
                calls.append(req.diff)
                if "src/a.py" in req.diff:
                    return ReviewResult(
                        summary="Batch 0",
                        comments=huge_comments,
                        provider="openai-compatible",
                        model="gpt-4o-mini",
                        review_status="complete",
                    )
                if self.fail_b1:
                    raise RuntimeError("transient interruption before batch-001")
                return ReviewResult(
                    summary="Batch 1",
                    comments=(
                        ReviewComment(
                            path="src/b.py", line=1, side="RIGHT", body="OK b1"
                        ),
                    ),
                    provider="openai-compatible",
                    model="gpt-4o-mini",
                    review_status="complete",
                )

        def _ctx(fail_b1: bool) -> V6FreshAnalysisContext:
            return V6FreshAnalysisContext(
                owner=OWNER,
                repo=REPO,
                pull_request=PR_NUMBER,
                pr_title="Partitioned batch output PR",
                base_sha=BASE_SHA,
                merge_base_sha=BASE_SHA,
                head_sha=HEAD_A,
                inventory_sha256=inv_sha,
                inventory=(
                    {
                        "path": "src/a.py",
                        "status": "modified",
                        "disposition": "text",
                        "hunk_ids": [hunk_0],
                    },
                    {
                        "path": "src/b.py",
                        "status": "modified",
                        "disposition": "text",
                        "hunk_ids": [hunk_1],
                    },
                ),
                non_text_changes=(),
                policy_digest=pol_d,
                configuration_digest=cfg_d,
                max_provider_calls=4,
                planned_batches=(b0, b1),
                all_expected_hunk_ids=(hunk_0, hunk_1),
                sub_hunks_by_parent={},
                combined_diff_sha256=canonical_sha256("ab"),
                provider_name="openai-compatible",
                model_name="gpt-4o-mini",
                service=_SvcCrashOnce(fail_b1=fail_b1),
            )

        proof = WriterOwnershipProof(
            repository_id=REPO_ID,
            pull_request=PR_NUMBER,
            operation_id="op-part-batch",
            run_id=300,
            run_attempt=1,
            writer_epoch=1,
            pr_head_sha=HEAD_A,
            verified_by_broker=True,
        )
        # Run 1: executes b0 (persists batch-part:s + __partitioned__ BatchOutputRecord),
        # then simulates process crash right before posting batch-001 dispatch!
        crash_once = True

        def _app_crash_before_b1(
            call: dict[str, Any],
        ) -> tuple[int, Mapping[str, str], Any]:
            nonlocal crash_once
            if (
                crash_once
                and call["method"] == "POST"
                and "batch-dispatch:v1" in str((call.get("body") or {}).get("body", ""))
                and "batch-001" in str((call.get("body") or {}).get("body", ""))
            ):
                crash_once = False
                raise RuntimeError("simulated worker crash before batch-001 dispatch")
            return pr.handle_app(call)

        req1 = RecoveryRequest(
            owner=OWNER,
            repo=REPO,
            repository_id=REPO_ID,
            pull_request=PR_NUMBER,
            operation_id="op-part-batch",
            ownership_proof=proof,
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            expected_app_id=APP_ID,
            expected_bot_login=BOT_LOGIN,
            expected_bot_user_id=BOT_USER_ID,
            slot_key=SLOT_B,
            generation=1,
            owner_marker=owner_rec,
            fresh_analysis_context_loader=lambda: _ctx(False),
        )
        eng1 = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=_ScriptedTransport(pr.handle_wf),
                broker_app_transport=_ScriptedTransport(_app_crash_before_b1),
            ),
            now_fn=lambda: 1_700_000_000.0,
            sleeper=lambda _s: None,
            broker_fence_verifier=_fence_verifier,
            mutation_bound=MutationSanityBound(max_writes=64),
        )
        with self.assertRaises(RuntimeError):
            eng1.recover_and_publish(req1)
        self.assertEqual(len(calls), 1)
        bpart_comments = [c for c in pr.comments if "batch-part:" in c["body"]]
        self.assertGreaterEqual(len(bpart_comments), 2)

        # Rerun reconstructs b0 from batch-part:s (0 calls for b0) and runs only b1!
        calls.clear()
        eng2 = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=_ScriptedTransport(pr.handle_wf),
                broker_app_transport=_ScriptedTransport(pr.handle_app),
            ),
            now_fn=lambda: 1_700_000_000.0,
            sleeper=lambda _s: None,
            broker_fence_verifier=_fence_verifier,
            mutation_bound=MutationSanityBound(max_writes=64),
        )
        res2 = eng2.recover_and_publish(req1)
        self.assertIsInstance(res2, RecoveryCompleted)
        assert isinstance(res2, RecoveryCompleted)
        self.assertEqual(res2.model_calls_started, 1)
        self.assertEqual(len(calls), 1)
        self.assertIn("src/b.py", calls[0])
        self.assertEqual(len(pr.reviews), 1)

    def test_ci_worker_qualification_isolates_v6_from_legacy_failures(self) -> None:
        import scripts.run_worker_qualifications as rq

        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "report.json"
            ok_script = Path(tmp) / "ok.py"
            fail_script = Path(tmp) / "fail.py"
            ok_script.write_text("import sys; sys.exit(0)\n", encoding="utf-8")
            fail_script.write_text("import sys; sys.exit(1)\n", encoding="utf-8")
            import sys

            plan = (
                ("legacy-broken", (sys.executable, str(fail_script)), {}),
                ("v6-one-writer", (sys.executable, str(ok_script)), {}),
            )
            exit_code = rq.run(plan, report_path)
            self.assertEqual(exit_code, 1)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            by_name = {row["name"]: row for row in report["results"]}
            self.assertEqual(by_name["legacy-broken"]["returncode"], 1)
            self.assertEqual(by_name["v6-one-writer"]["returncode"], 0)
            self.assertTrue(
                any(name == "v6-one-writer" for name, _, _ in rq.QUALIFICATIONS)
            )

    def test_batch_output_replay_preserves_line_none_and_provider_calls_budget(
        self,
    ) -> None:
        from types import SimpleNamespace

        from review_sensei.configuration import default_configuration
        from review_sensei.models import ReviewComment, ReviewResult

        diff_a = "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-a = 1\n+a = 2\n"
        diff_b = "diff --git a/src/b.py b/src/b.py\n--- a/src/b.py\n+++ b/src/b.py\n@@ -1 +1 @@\n-b = 1\n+b = 2\n"
        hunk_a = "src/a.py#h0:1,1+1,1:" + ("a" * 16)
        hunk_b = "src/b.py#h0:1,1+1,1:" + ("b" * 16)
        policy_digest, config_digest = compute_trusted_policy_and_config_digests(
            default_configuration(),
            provider_name="openai-compatible",
            model_name="gpt-4o-mini",
        )
        inv = (
            {
                "path": "src/a.py",
                "status": "modified",
                "disposition": "text",
                "hunk_ids": [hunk_a],
            },
            {
                "path": "src/b.py",
                "status": "modified",
                "disposition": "text",
                "hunk_ids": [hunk_b],
            },
        )
        inv_sha = canonical_sha256(inv)
        b0 = V6PlannedBatch(
            batch_id="batch-000",
            diff_text=diff_a,
            diff_sha256=canonical_sha256(diff_a),
            prompt_bytes=200,
            paths=("src/a.py",),
            primary_hunk_ids=(hunk_a,),
            parent_hunk_ids=(hunk_a,),
        )
        b1 = V6PlannedBatch(
            batch_id="batch-001",
            diff_text=diff_b,
            diff_sha256=canonical_sha256(diff_b),
            prompt_bytes=200,
            paths=("src/b.py",),
            primary_hunk_ids=(hunk_b,),
            parent_hunk_ids=(hunk_b,),
        )

        svc_calls = 0

        class _MultiCallSvc:
            def run(self, req: Any, budget: Any = None) -> Any:
                nonlocal svc_calls
                svc_calls += 1
                return SimpleNamespace(
                    outcome=SimpleNamespace(status="complete", provider_calls=2),
                    result=ReviewResult(
                        summary="Summary A",
                        comments=(
                            ReviewComment(
                                path="src/a.py",
                                line=None,
                                side="FILE",
                                body="File-level finding",
                            ),
                        ),
                        provider="openai-compatible",
                        model="gpt-4o-mini",
                        review_status="complete",
                    ),
                )

        pr = _SimulatedPRSurface(head_sha=HEAD_A)
        owner_rec = _make_owner_record(
            operation_id="op-multicall",
            pr_head_sha=HEAD_A,
            slot_key=SLOT_B,
        )
        pr.add_comment(format_v6_record_marker(owner_rec), is_app=True)
        proof = WriterOwnershipProof(
            repository_id=REPO_ID,
            pull_request=PR_NUMBER,
            operation_id="op-multicall",
            run_id=100,
            run_attempt=1,
            writer_epoch=1,
            pr_head_sha=HEAD_A,
            verified_by_broker=True,
        )

        def _make_ctx(max_calls: int) -> V6FreshAnalysisContext:
            return V6FreshAnalysisContext(
                owner=OWNER,
                repo=REPO,
                pull_request=PR_NUMBER,
                pr_title="Multicall test",
                base_sha=BASE_SHA,
                merge_base_sha=BASE_SHA,
                head_sha=HEAD_A,
                inventory_sha256=inv_sha,
                inventory=inv,
                non_text_changes=(),
                policy_digest=policy_digest,
                configuration_digest=config_digest,
                max_provider_calls=max_calls,
                planned_batches=(b0, b1),
                all_expected_hunk_ids=(hunk_a, hunk_b),
                sub_hunks_by_parent={},
                combined_diff_sha256=canonical_sha256(diff_a + diff_b),
                provider_name="openai-compatible",
                model_name="gpt-4o-mini",
                service=_MultiCallSvc(),
            )

        req_tight = RecoveryRequest(
            owner=OWNER,
            repo=REPO,
            repository_id=REPO_ID,
            pull_request=PR_NUMBER,
            operation_id="op-multicall",
            ownership_proof=proof,
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            expected_app_id=APP_ID,
            expected_bot_login=BOT_LOGIN,
            expected_bot_user_id=BOT_USER_ID,
            slot_key=SLOT_B,
            generation=1,
            owner_marker=owner_rec,
            fresh_analysis_context_loader=lambda: _make_ctx(2),
        )
        eng1 = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=_ScriptedTransport(pr.handle_wf),
                broker_app_transport=_ScriptedTransport(pr.handle_app),
            ),
            now_fn=lambda: 1_700_000_000.0,
            sleeper=lambda _s: None,
            broker_fence_verifier=_fence_verifier,
            mutation_bound=MutationSanityBound(max_writes=32),
        )
        res1 = eng1.recover_and_publish(req_tight)
        self.assertIsInstance(res1, RecoveryHold)
        self.assertEqual(svc_calls, 1)

        # Restart with max_provider_calls=2 MUST see b0 consumed 2 calls and hold for b1 with 0 calls!
        eng2 = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=_ScriptedTransport(pr.handle_wf),
                broker_app_transport=_ScriptedTransport(pr.handle_app),
            ),
            now_fn=lambda: 1_700_000_000.0,
            sleeper=lambda _s: None,
            broker_fence_verifier=_fence_verifier,
            mutation_bound=MutationSanityBound(max_writes=32),
        )
        res_budget = eng2.recover_and_publish(req_tight)
        self.assertIsInstance(res_budget, RecoveryHold)
        assert isinstance(res_budget, RecoveryHold)
        self.assertEqual(res_budget.blocker, "review_provider_call_budget_exhausted")
        self.assertEqual(svc_calls, 1)

        # Now increase review budget? Since BatchPlan is bound to max_provider_calls=2, start fresh op with max=4
        # and replay a pre-seeded BatchOutputRecord containing line=None + provider_calls=2 -> completes cleanly!
        pr3 = _SimulatedPRSurface(head_sha=HEAD_A)
        in_dig = canonical_sha256(
            {
                "base_sha": BASE_SHA,
                "merge_base_sha": BASE_SHA,
                "head_sha": HEAD_A,
                "inventory_sha256": inv_sha,
                "policy_digest": policy_digest,
                "configuration_digest": config_digest,
            }
        )[:12]
        plan4 = BatchPlanRecord(
            record_id=f"batch-plan:op-multicall4:{HEAD_A[:12]}:{in_dig}",
            operation_id="op-multicall4",
            base_sha=BASE_SHA,
            merge_base_sha=BASE_SHA,
            pr_head_sha=HEAD_A,
            inventory_sha256=inv_sha,
            policy_digest=policy_digest,
            configuration_digest=config_digest,
            max_provider_calls=4,
            batches=(b0.to_plan_dict(), b1.to_plan_dict()),
        )
        plan4_sha = canonical_sha256(record_to_dict(plan4))
        owner4 = _make_owner_record(
            operation_id="op-multicall4",
            pr_head_sha=HEAD_A,
            slot_key=SLOT_B,
        )
        pr3.add_comment(format_v6_record_marker(owner4), is_app=True)
        pr3.add_comment(format_v6_record_marker(plan4), is_app=True)
        pr3.add_comment(
            format_v6_record_marker(
                BatchOutputRecord(
                    record_id=f"batch-output:op-multicall4:{HEAD_A[:12]}:{in_dig}:batch-000:1",
                    operation_id="op-multicall4",
                    batch_id="batch-000",
                    plan_sha256=plan4_sha,
                    hunk_ids_reviewed=(hunk_a,),
                    findings=(
                        {
                            "path": "src/a.py",
                            "line": None,
                            "side": "FILE",
                            "body": "File-level finding",
                        },
                    ),
                    summary="Summary A",
                    provider="openai-compatible",
                    model="gpt-4o-mini",
                    review_status="complete",
                    attempt=1,
                    provider_calls=2,
                )
            ),
            is_app=True,
        )
        proof4 = replace(proof, operation_id="op-multicall4")
        req4 = RecoveryRequest(
            owner=OWNER,
            repo=REPO,
            repository_id=REPO_ID,
            pull_request=PR_NUMBER,
            operation_id="op-multicall4",
            ownership_proof=proof4,
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            expected_app_id=APP_ID,
            expected_bot_login=BOT_LOGIN,
            expected_bot_user_id=BOT_USER_ID,
            slot_key=SLOT_B,
            generation=1,
            owner_marker=owner4,
            fresh_analysis_context_loader=lambda: _make_ctx(4),
        )
        eng3 = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=_ScriptedTransport(pr3.handle_wf),
                broker_app_transport=_ScriptedTransport(pr3.handle_app),
            ),
            now_fn=lambda: 1_700_000_000.0,
            sleeper=lambda _s: None,
            broker_fence_verifier=_fence_verifier,
            mutation_bound=MutationSanityBound(max_writes=32),
        )
        res_ok = eng3.recover_and_publish(req4)
        self.assertIsInstance(res_ok, RecoveryCompleted)
        self.assertEqual(len(pr3.reviews), 1)
        self.assertIn("File-level finding", pr3.reviews[0]["body"])

    def test_partial_batch_review_status_and_verify_expected_hunks_fail_closed(
        self,
    ) -> None:
        from review_sensei.configuration import default_configuration
        from review_sensei.models import ReviewResult

        diff_a = "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-a = 1\n+a = 2\n"
        hunk_a = "src/a.py#h0:1,1+1,1:" + ("a" * 16)
        policy_digest, config_digest = compute_trusted_policy_and_config_digests(
            default_configuration(),
            provider_name="openai-compatible",
            model_name="gpt-4o-mini",
        )
        b0 = V6PlannedBatch(
            batch_id="batch-000",
            diff_text=diff_a,
            diff_sha256=canonical_sha256(diff_a),
            prompt_bytes=200,
            paths=("src/a.py",),
            primary_hunk_ids=(hunk_a,),
            parent_hunk_ids=(hunk_a,),
        )

        class _PartialSvc:
            def review(self, req: Any) -> ReviewResult:
                return ReviewResult(
                    summary="Partial",
                    comments=(),
                    provider="openai-compatible",
                    model="gpt-4o-mini",
                    review_status="partial",
                )

        ctx = V6FreshAnalysisContext(
            owner=OWNER,
            repo=REPO,
            pull_request=PR_NUMBER,
            pr_title="Partial test",
            base_sha=BASE_SHA,
            merge_base_sha=BASE_SHA,
            head_sha=HEAD_A,
            inventory_sha256="c" * 64,
            inventory=(
                {
                    "path": "src/a.py",
                    "status": "modified",
                    "disposition": "text",
                    "hunk_ids": [hunk_a],
                },
            ),
            non_text_changes=(),
            policy_digest=policy_digest,
            configuration_digest=config_digest,
            max_provider_calls=4,
            planned_batches=(b0,),
            all_expected_hunk_ids=(hunk_a,),
            sub_hunks_by_parent={},
            combined_diff_sha256=canonical_sha256(diff_a),
            provider_name="openai-compatible",
            model_name="gpt-4o-mini",
            service=_PartialSvc(),
        )
        with self.assertRaises(V6FreshAnalysisError) as err_ctx:
            verify_expected_hunks_reviewed(ctx, ())
        self.assertEqual(err_ctx.exception.diagnostic, "v6_batch_incomplete_coverage")

        ctx_sub = replace(
            ctx, sub_hunks_by_parent={hunk_a: (f"{hunk_a}#p0", f"{hunk_a}#p1")}
        )
        with self.assertRaises(V6FreshAnalysisError) as sub_err:
            verify_expected_hunks_reviewed(ctx_sub, (f"{hunk_a}#p0",))
        self.assertEqual(sub_err.exception.diagnostic, "v6_batch_incomplete_coverage")
        verify_expected_hunks_reviewed(ctx_sub, (f"{hunk_a}#p0", f"{hunk_a}#p1"))

        pr = _SimulatedPRSurface(head_sha=HEAD_A)
        owner_rec = _make_owner_record(
            operation_id="op-partial",
            pr_head_sha=HEAD_A,
            slot_key=SLOT_B,
        )
        pr.add_comment(format_v6_record_marker(owner_rec), is_app=True)
        req = RecoveryRequest(
            owner=OWNER,
            repo=REPO,
            repository_id=REPO_ID,
            pull_request=PR_NUMBER,
            operation_id="op-partial",
            ownership_proof=WriterOwnershipProof(
                repository_id=REPO_ID,
                pull_request=PR_NUMBER,
                operation_id="op-partial",
                run_id=100,
                run_attempt=1,
                writer_epoch=1,
                pr_head_sha=HEAD_A,
                verified_by_broker=True,
            ),
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            expected_app_id=APP_ID,
            expected_bot_login=BOT_LOGIN,
            expected_bot_user_id=BOT_USER_ID,
            slot_key=SLOT_B,
            generation=1,
            owner_marker=owner_rec,
            fresh_analysis_context_loader=lambda: ctx,
        )
        eng = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=_ScriptedTransport(pr.handle_wf),
                broker_app_transport=_ScriptedTransport(pr.handle_app),
            ),
            now_fn=lambda: 1_700_000_000.0,
            sleeper=lambda _s: None,
            broker_fence_verifier=_fence_verifier,
            mutation_bound=MutationSanityBound(max_writes=32),
        )
        hold = eng.recover_and_publish(req)
        self.assertIsInstance(hold, RecoveryHold)
        assert isinstance(hold, RecoveryHold)
        self.assertEqual(hold.blocker, "v6_batch_incomplete_coverage")
        self.assertFalse(
            any("batch-output:" in c["body"] for c in pr.comments),
            "partial batch must never persist BatchOutputRecord",
        )

    def test_oversized_multihunk_file_prunes_earlier_hunks_consistently(self) -> None:
        from review_sensei_github.v6_fresh_analysis import (
            _pack_units_into_planned_batches,
            _split_and_group_diff_units,
        )

        from review_sensei.evidence import EvidenceSnapshot

        # Construct a single file with hunk 0 small and hunk 1 exceeding prompt ceiling on a single line.
        raw_diff = (
            "diff --git a/src/mixed.py b/src/mixed.py\n"
            "--- a/src/mixed.py\n"
            "+++ b/src/mixed.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-old\n"
            "+new\n"
            "@@ -50,1 +50,1 @@\n"
            "-x\n"
            "+" + ("Y" * 5000) + "\n"
        )
        snap = EvidenceSnapshot(
            repository=f"{OWNER}/{REPO}",
            pull_request=PR_NUMBER,
            base_sha=BASE_SHA,
            head_sha=HEAD_A,
        )
        units, subs, oversized = _split_and_group_diff_units(
            [raw_diff],
            snapshot=snap,
            batch_max_bytes=2000,
            batch_max_hunks=10,
            prompt_ceiling_bytes=4000,
            measure_prompt_fn=lambda diff: len(diff.encode("utf-8")) + 100,
        )
        self.assertIn("src/mixed.py", oversized)
        self.assertEqual(units, [])
        self.assertEqual(subs, {})
        planned = _pack_units_into_planned_batches(
            units,
            batch_max_bytes=2000,
            batch_max_files=5,
            batch_max_hunks=10,
            prompt_ceiling_bytes=4000,
            measure_prompt_fn=lambda diff: len(diff.encode("utf-8")) + 100,
        )
        self.assertEqual(planned, ())

    def test_env_var_config_digest_parity_with_filtered_hosted_environ(self) -> None:
        from review_sensei.configuration import default_configuration

        cfg = default_configuration()
        env_pair = {
            "REVIEWSENSEI_PROVIDER": "openai-compatible",
            "REVIEWSENSEI_MODEL": "gpt-4o-mini",
        }
        pol_a, cfg_a = compute_trusted_policy_and_config_digests(
            cfg,
            environ=filtered_hosted_environ(env_pair),
        )
        pol_b, cfg_b = compute_trusted_policy_and_config_digests(
            cfg,
            provider_name="openai-compatible",
            model_name="gpt-4o-mini",
            environ=filtered_hosted_environ({}),
        )
        self.assertEqual(pol_a, pol_b)
        self.assertEqual(cfg_a, cfg_b)

    def test_deliver_detail_comments_holds_on_mismatched_receipt_payload_digest(
        self,
    ) -> None:
        from review_sensei_github.v6_recovery_ledger import (
            UnresolvedResultRecord,
            WriteIntentRecord,
        )

        from review_sensei.configuration import default_configuration

        pr = _SimulatedPRSurface(head_sha=HEAD_A)
        policy_digest, config_digest = compute_trusted_policy_and_config_digests(
            default_configuration(),
            provider_name="openai-compatible",
            model_name="gpt-4o-mini",
        )
        op_id = "op-detail-mismatch"
        detail_op = f"{op_id}:detail:0"
        owner_rec = _make_owner_record(
            operation_id=op_id,
            pr_head_sha=HEAD_A,
            slot_key=SLOT_B,
        )
        pr.add_comment(format_v6_record_marker(owner_rec), is_app=True)
        # Seed a stale/mismatched receipt for detail:1 while PR has >50 findings requiring detail:1
        findings = tuple(
            {
                "path": f"src/f{i}.py",
                "line": 1,
                "side": "RIGHT",
                "body": f"Finding body number {i}",
            }
            for i in range(55)
        )
        proof = WriterOwnershipProof(
            repository_id=REPO_ID,
            pull_request=PR_NUMBER,
            operation_id=op_id,
            run_id=100,
            run_attempt=1,
            writer_epoch=1,
            pr_head_sha=HEAD_A,
            verified_by_broker=True,
        )
        req = RecoveryRequest(
            owner=OWNER,
            repo=REPO,
            repository_id=REPO_ID,
            pull_request=PR_NUMBER,
            operation_id=op_id,
            ownership_proof=proof,
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            expected_app_id=APP_ID,
            expected_bot_login=BOT_LOGIN,
            expected_bot_user_id=BOT_USER_ID,
            slot_key=SLOT_B,
            generation=1,
            owner_marker=owner_rec,
            base_sha=BASE_SHA,
            policy_digest=policy_digest,
            configuration_digest=config_digest,
        )
        in_dig = canonical_sha256(
            {
                "base_sha": BASE_SHA,
                "merge_base_sha": None,
                "head_sha": HEAD_A,
                "inventory_sha256": None,
                "policy_digest": policy_digest,
                "configuration_digest": config_digest,
            }
        )[:12]
        unres = UnresolvedResultRecord(
            record_id=f"result:{op_id}:{HEAD_A[:12]}:{in_dig}",
            operation_id=op_id,
            pr_head_sha=HEAD_A,
            payload_sha256=canonical_sha256(findings),
            findings_payload=findings,
            required_evidence_digests=(
                f"v6:base:{BASE_SHA}",
                f"v6:policy:{policy_digest}",
                f"v6:config:{config_digest}",
            ),
        )
        pr.add_comment(format_v6_record_marker(unres), is_app=True)
        wrong_sha = "f" * 64
        pr.add_comment(
            format_v6_record_marker(
                WriteIntentRecord(
                    record_id=f"intent:{detail_op}:1:1",
                    operation_id=detail_op,
                    writer_epoch=1,
                    pr_head_sha=HEAD_A,
                    effect_kind="issue_comment",
                    payload_sha256=wrong_sha,
                    target_digest=canonical_sha256(
                        f"/repos/{OWNER}/{REPO}/issues/{PR_NUMBER}/comments"
                    ),
                    generation=1,
                    target_location=f"/repos/{OWNER}/{REPO}/issues/{PR_NUMBER}/comments",
                )
            ),
            is_app=True,
        )
        pr.add_comment(
            format_v6_record_marker(
                CompletionReceiptRecord(
                    record_id=f"receipt:{detail_op}:999",
                    operation_id=detail_op,
                    writer_epoch=1,
                    github_effect_id=999,
                    payload_sha256=wrong_sha,
                    completed_at="2026-11-27T00:05:00Z",
                )
            ),
            is_app=True,
        )

        eng = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=_ScriptedTransport(pr.handle_wf),
                broker_app_transport=_ScriptedTransport(pr.handle_app),
            ),
            now_fn=lambda: 1_700_000_000.0,
            sleeper=lambda _s: None,
            broker_fence_verifier=_fence_verifier,
            mutation_bound=MutationSanityBound(max_writes=64),
        )
        res = eng.recover_and_publish(req)
        self.assertIsInstance(res, RecoveryHold)
        assert isinstance(res, RecoveryHold)
        self.assertEqual(res.blocker, "stale_detail_receipt_payload_mismatch")

    def test_batch_attempt_record_marker_roundtrip(self) -> None:
        from review_sensei.errors import ReviewInputError

        rec = BatchAttemptRecord(
            record_id="batch-attempt:op-1:aaaaaaaaaaaa:111111111111:batch-000:1",
            operation_id="op-1",
            batch_id="batch-000",
            attempt=1,
            plan_sha256="a" * 64,
            status="partial",
            provider_calls=3,
            diagnostic="v6_batch_incomplete_coverage",
        )
        marker = format_v6_record_marker(rec)
        parsed = extract_v6_record_markers(marker)
        self.assertEqual(parsed, (rec,))

        rec_zero = BatchAttemptRecord(
            record_id="batch-attempt:op-1:aaaaaaaaaaaa:111111111111:batch-000:1",
            operation_id="op-1",
            batch_id="batch-000",
            attempt=1,
            plan_sha256="a" * 64,
            status="budget_exhausted",
            provider_calls=0,
            diagnostic="deadline_exceeded",
        )
        self.assertEqual(
            extract_v6_record_markers(format_v6_record_marker(rec_zero)),
            (rec_zero,),
        )

        for bad_kwargs in (
            {"provider_calls": -1},
            {"provider_calls": True},
            {"status": "bogus_status"},
            {"diagnostic": "x" * 257},
        ):
            kwargs: dict[str, Any] = {
                "record_id": "bad",
                "operation_id": "op-1",
                "batch_id": "batch-000",
                "attempt": 1,
                "plan_sha256": "a" * 64,
                "status": "partial",
                "provider_calls": 0,
                "diagnostic": "v6_batch_incomplete_coverage",
                **bad_kwargs,
            }
            with self.assertRaises(ReviewInputError):
                BatchAttemptRecord(**kwargs)

    def test_incomplete_batch_persists_durable_provider_calls_and_rerun_holds_with_count(
        self,
    ) -> None:
        from types import SimpleNamespace

        from review_sensei.configuration import default_configuration
        from review_sensei.models import ReviewResult

        diff_a = "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-a = 1\n+a = 2\n"
        diff_b = "diff --git a/src/b.py b/src/b.py\n--- a/src/b.py\n+++ b/src/b.py\n@@ -1 +1 @@\n-b = 1\n+b = 2\n"
        hunk_a = "src/a.py#h0:1,1+1,1:" + ("a" * 16)
        hunk_b = "src/b.py#h0:1,1+1,1:" + ("b" * 16)
        policy_digest, config_digest = compute_trusted_policy_and_config_digests(
            default_configuration(),
            provider_name="openai-compatible",
            model_name="gpt-4o-mini",
        )
        inv = (
            {
                "path": "src/a.py",
                "status": "modified",
                "disposition": "text",
                "hunk_ids": [hunk_a],
            },
            {
                "path": "src/b.py",
                "status": "modified",
                "disposition": "text",
                "hunk_ids": [hunk_b],
            },
        )
        inv_sha = canonical_sha256(inv)
        b0 = V6PlannedBatch(
            batch_id="batch-000",
            diff_text=diff_a,
            diff_sha256=canonical_sha256(diff_a),
            prompt_bytes=200,
            paths=("src/a.py",),
            primary_hunk_ids=(hunk_a,),
            parent_hunk_ids=(hunk_a,),
        )
        b1 = V6PlannedBatch(
            batch_id="batch-001",
            diff_text=diff_b,
            diff_sha256=canonical_sha256(diff_b),
            prompt_bytes=200,
            paths=("src/b.py",),
            primary_hunk_ids=(hunk_b,),
            parent_hunk_ids=(hunk_b,),
        )

        # Subcase A: batch-000 ends with review_status='partial' after provider_calls=2.
        partial_calls = 0

        class _PartialMultiSvc:
            def run(self, req: Any, budget: Any = None) -> Any:
                nonlocal partial_calls
                partial_calls += 1
                return SimpleNamespace(
                    outcome=SimpleNamespace(status="complete", provider_calls=2),
                    result=ReviewResult(
                        summary="Partial A",
                        comments=(),
                        provider="openai-compatible",
                        model="gpt-4o-mini",
                        review_status="partial",
                    ),
                )

        pr_a = _SimulatedPRSurface(head_sha=HEAD_A)
        owner_a = _make_owner_record(
            operation_id="op-inc-a",
            pr_head_sha=HEAD_A,
            slot_key=SLOT_B,
        )
        pr_a.add_comment(format_v6_record_marker(owner_a), is_app=True)
        req_a = RecoveryRequest(
            owner=OWNER,
            repo=REPO,
            repository_id=REPO_ID,
            pull_request=PR_NUMBER,
            operation_id="op-inc-a",
            ownership_proof=WriterOwnershipProof(
                repository_id=REPO_ID,
                pull_request=PR_NUMBER,
                operation_id="op-inc-a",
                run_id=100,
                run_attempt=1,
                writer_epoch=1,
                pr_head_sha=HEAD_A,
                verified_by_broker=True,
            ),
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            expected_app_id=APP_ID,
            expected_bot_login=BOT_LOGIN,
            expected_bot_user_id=BOT_USER_ID,
            slot_key=SLOT_B,
            generation=1,
            owner_marker=owner_a,
            fresh_analysis_context_loader=lambda: V6FreshAnalysisContext(
                owner=OWNER,
                repo=REPO,
                pull_request=PR_NUMBER,
                pr_title="Partial multi",
                base_sha=BASE_SHA,
                merge_base_sha=BASE_SHA,
                head_sha=HEAD_A,
                inventory_sha256=inv_sha,
                inventory=inv,
                non_text_changes=(),
                policy_digest=policy_digest,
                configuration_digest=config_digest,
                max_provider_calls=4,
                planned_batches=(b0, b1),
                all_expected_hunk_ids=(hunk_a, hunk_b),
                sub_hunks_by_parent={},
                combined_diff_sha256=canonical_sha256(diff_a + diff_b),
                provider_name="openai-compatible",
                model_name="gpt-4o-mini",
                service=_PartialMultiSvc(),
            ),
        )
        eng_a1 = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=_ScriptedTransport(pr_a.handle_wf),
                broker_app_transport=_ScriptedTransport(pr_a.handle_app),
            ),
            now_fn=lambda: 1_700_000_000.0,
            sleeper=lambda _s: None,
            broker_fence_verifier=_fence_verifier,
            mutation_bound=MutationSanityBound(max_writes=32),
        )
        res_a1 = eng_a1.recover_and_publish(req_a)
        self.assertIsInstance(res_a1, RecoveryHold)
        assert isinstance(res_a1, RecoveryHold)
        self.assertEqual(res_a1.blocker, "v6_batch_incomplete_coverage")
        self.assertEqual(partial_calls, 1)
        self.assertIn("calls:2", res_a1.missing_evidence[0])
        attempt_recs = [
            r
            for c in pr_a.comments
            for r in extract_v6_record_markers(c["body"])
            if isinstance(r, BatchAttemptRecord)
        ]
        self.assertEqual(len(attempt_recs), 1)
        self.assertEqual(attempt_recs[0].provider_calls, 2)
        self.assertEqual(attempt_recs[0].status, "partial")
        self.assertEqual(attempt_recs[0].diagnostic, "v6_batch_incomplete_coverage")
        self.assertFalse(any("batch-output:" in c["body"] for c in pr_a.comments))

        # Normal cold rerun holds with 0 extra provider calls and preserves calls:2 count!
        res_a2 = eng_a1.recover_and_publish(req_a)
        self.assertIsInstance(res_a2, RecoveryHold)
        assert isinstance(res_a2, RecoveryHold)
        self.assertEqual(partial_calls, 1)
        self.assertIn("calls:2", res_a2.missing_evidence[0])

        # Subcase B: execute_v6_batch fails with outcome.status='provider_failed' after provider_calls=3.
        fail_calls = 0

        class _FailedMultiSvc:
            def run(self, req: Any, budget: Any = None) -> Any:
                nonlocal fail_calls
                fail_calls += 1
                if fail_calls == 1:
                    return SimpleNamespace(
                        outcome=SimpleNamespace(
                            status="provider_failed",
                            provider_calls=3,
                            diagnostic="v6_provider_upstream_502",
                        ),
                        result=None,
                    )
                return SimpleNamespace(
                    outcome=SimpleNamespace(status="complete", provider_calls=1),
                    result=ReviewResult(
                        summary="OK B",
                        comments=(),
                        provider="openai-compatible",
                        model="gpt-4o-mini",
                        review_status="complete",
                    ),
                )

        pr_b = _SimulatedPRSurface(head_sha=HEAD_A)
        owner_b = _make_owner_record(
            operation_id="op-inc-b",
            pr_head_sha=HEAD_A,
            slot_key=SLOT_B,
        )
        pr_b.add_comment(format_v6_record_marker(owner_b), is_app=True)
        in_dig = canonical_sha256(
            {
                "base_sha": BASE_SHA,
                "merge_base_sha": BASE_SHA,
                "head_sha": HEAD_A,
                "inventory_sha256": inv_sha,
                "policy_digest": policy_digest,
                "configuration_digest": config_digest,
            }
        )[:12]
        plan_b = BatchPlanRecord(
            record_id=f"batch-plan:op-inc-b:{HEAD_A[:12]}:{in_dig}",
            operation_id="op-inc-b",
            base_sha=BASE_SHA,
            merge_base_sha=BASE_SHA,
            pr_head_sha=HEAD_A,
            inventory_sha256=inv_sha,
            policy_digest=policy_digest,
            configuration_digest=config_digest,
            max_provider_calls=3,
            batches=(b0.to_plan_dict(), b1.to_plan_dict()),
        )
        plan_b_sha = canonical_sha256(record_to_dict(plan_b))
        pr_b.add_comment(format_v6_record_marker(plan_b), is_app=True)
        # Seed BatchAttemptRecord for b0=3 calls AND simulate resolved b0 output or verify b1 hits budget=3.
        # First run b1 directly on fresh op with b0 failing after 3 calls:
        req_b = RecoveryRequest(
            owner=OWNER,
            repo=REPO,
            repository_id=REPO_ID,
            pull_request=PR_NUMBER,
            operation_id="op-inc-b",
            ownership_proof=WriterOwnershipProof(
                repository_id=REPO_ID,
                pull_request=PR_NUMBER,
                operation_id="op-inc-b",
                run_id=100,
                run_attempt=1,
                writer_epoch=1,
                pr_head_sha=HEAD_A,
                verified_by_broker=True,
            ),
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            expected_app_id=APP_ID,
            expected_bot_login=BOT_LOGIN,
            expected_bot_user_id=BOT_USER_ID,
            slot_key=SLOT_B,
            generation=1,
            owner_marker=owner_b,
            fresh_analysis_context_loader=lambda: V6FreshAnalysisContext(
                owner=OWNER,
                repo=REPO,
                pull_request=PR_NUMBER,
                pr_title="Failed multi",
                base_sha=BASE_SHA,
                merge_base_sha=BASE_SHA,
                head_sha=HEAD_A,
                inventory_sha256=inv_sha,
                inventory=inv,
                non_text_changes=(),
                policy_digest=policy_digest,
                configuration_digest=config_digest,
                max_provider_calls=3,
                planned_batches=(b0, b1),
                all_expected_hunk_ids=(hunk_a, hunk_b),
                sub_hunks_by_parent={},
                combined_diff_sha256=canonical_sha256(diff_a + diff_b),
                provider_name="openai-compatible",
                model_name="gpt-4o-mini",
                service=_FailedMultiSvc(),
            ),
        )
        eng_b1 = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=_ScriptedTransport(pr_b.handle_wf),
                broker_app_transport=_ScriptedTransport(pr_b.handle_app),
            ),
            now_fn=lambda: 1_700_000_000.0,
            sleeper=lambda _s: None,
            broker_fence_verifier=_fence_verifier,
            mutation_bound=MutationSanityBound(max_writes=32),
        )
        res_b1 = eng_b1.recover_and_publish(req_b)
        self.assertIsInstance(res_b1, RecoveryHold)
        assert isinstance(res_b1, RecoveryHold)
        self.assertEqual(res_b1.blocker, "v6_provider_upstream_502")
        self.assertEqual(fail_calls, 1)
        self.assertIn("calls:3", res_b1.missing_evidence[0])

        # Also verify restart counts attempt=1 (3 calls from BatchAttemptRecord) + attempt=2 (1 call) = 4 against max_provider_calls=3:
        pr_b2 = _SimulatedPRSurface(head_sha=HEAD_A)
        for c in pr_b.comments:
            pr_b2.add_comment(c["body"], is_app=True)
        # Adding a synthetic attempt=2 BatchOutputRecord (1 call) brings total accounted calls to 4 >= 3 (versus 2 < 3 if attempt=1's 3-call BatchAttemptRecord were ignored):
        pr_b2.add_comment(
            format_v6_record_marker(
                BatchOutputRecord(
                    record_id=f"batch-output:op-inc-b:{HEAD_A[:12]}:{in_dig}:batch-000:2",
                    operation_id="op-inc-b",
                    batch_id="batch-000",
                    plan_sha256=plan_b_sha,
                    hunk_ids_reviewed=(hunk_a,),
                    findings=(),
                    summary="Recovered A",
                    provider="openai-compatible",
                    model="gpt-4o-mini",
                    review_status="complete",
                    attempt=2,
                    provider_calls=1,
                )
            ),
            is_app=True,
        )
        eng_b2 = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=_ScriptedTransport(pr_b2.handle_wf),
                broker_app_transport=_ScriptedTransport(pr_b2.handle_app),
            ),
            now_fn=lambda: 1_700_000_000.0,
            sleeper=lambda _s: None,
            broker_fence_verifier=_fence_verifier,
            mutation_bound=MutationSanityBound(max_writes=32),
        )
        res_b2 = eng_b2.recover_and_publish(req_b)
        self.assertIsInstance(res_b2, RecoveryHold)
        assert isinstance(res_b2, RecoveryHold)
        self.assertEqual(res_b2.blocker, "review_provider_call_budget_exhausted")
        self.assertEqual(res_b2.missing_evidence, ("batch_budget:4/3",))
        self.assertEqual(fail_calls, 1)

    def test_real_provider_adapter_and_service_incomplete_and_unexpected_exception(
        self,
    ) -> None:
        import io
        from email.message import Message
        from urllib.error import HTTPError

        from review_sensei.configuration import default_configuration
        from review_sensei.providers.openai_compatible import OpenAICompatibleProvider
        from review_sensei.service import DEFAULT_STAGES, ReviewService

        diff_a = "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-a = 1\n+a = 2\n"
        diff_b = "diff --git a/src/b.py b/src/b.py\n--- a/src/b.py\n+++ b/src/b.py\n@@ -1 +1 @@\n-b = 1\n+b = 2\n"
        hunk_a = "src/a.py#h0:1,1+1,1:" + ("a" * 16)
        hunk_b = "src/b.py#h0:1,1+1,1:" + ("b" * 16)
        policy_digest, config_digest = compute_trusted_policy_and_config_digests(
            default_configuration(),
            provider_name="openai-compatible",
            model_name="gpt-4o-mini",
        )
        inv = (
            {
                "path": "src/a.py",
                "status": "modified",
                "disposition": "text",
                "hunk_ids": [hunk_a],
            },
        )
        inv_sha = canonical_sha256(inv)
        b0 = V6PlannedBatch(
            batch_id="batch-000",
            diff_text=diff_a,
            diff_sha256=canonical_sha256(diff_a),
            prompt_bytes=200,
            paths=("src/a.py",),
            primary_hunk_ids=(hunk_a,),
            parent_hunk_ids=(hunk_a,),
        )
        opened_errors: list[HTTPError] = []

        def _make_context(
            service: Any, *, max_provider_calls: int = 4
        ) -> V6FreshAnalysisContext:
            return V6FreshAnalysisContext(
                owner=OWNER,
                repo=REPO,
                pull_request=PR_NUMBER,
                pr_title="Real adapter test",
                base_sha=BASE_SHA,
                merge_base_sha=BASE_SHA,
                head_sha=HEAD_A,
                inventory_sha256=inv_sha,
                inventory=inv,
                non_text_changes=(),
                policy_digest=policy_digest,
                configuration_digest=config_digest,
                max_provider_calls=max_provider_calls,
                planned_batches=(b0,),
                all_expected_hunk_ids=(hunk_a,),
                sub_hunks_by_parent={},
                combined_diff_sha256=canonical_sha256(diff_a),
                provider_name="openai-compatible",
                model_name="gpt-4o-mini",
                service=service,
            )

        try:
            # Case 1: Real OpenAICompatibleProvider + real ReviewService (default stages):
            # Attempt 1 fails transiently (HTTP 503 -> retry), Attempt 2 fails non-transiently (HTTP 400).
            transport_calls_1 = 0

            def _opener_503_then_400(
                http_request: Any, *, timeout: float, context: Any
            ) -> Any:
                nonlocal transport_calls_1
                transport_calls_1 += 1
                if transport_calls_1 == 1:
                    hdrs = Message()
                    hdrs["Retry-After"] = "0"
                    err = HTTPError(
                        http_request.full_url,
                        503,
                        "Service Unavailable",
                        hdrs,
                        io.BytesIO(b"unavailable"),
                    )
                    opened_errors.append(err)
                    raise err
                err = HTTPError(
                    http_request.full_url,
                    400,
                    "Bad Request",
                    Message(),
                    io.BytesIO(b"bad request"),
                )
                opened_errors.append(err)
                raise err

            real_provider_1 = OpenAICompatibleProvider(
                base_url="https://api.openai.com/v1",
                model="gpt-4o-mini",
                api_key="sk-test-fake-key",
                opener=_opener_503_then_400,
            )
            real_service_1 = ReviewService(real_provider_1, stages=DEFAULT_STAGES)

            pr1 = _SimulatedPRSurface(head_sha=HEAD_A)
            owner1 = _make_owner_record(
                operation_id="op-real-handled",
                pr_head_sha=HEAD_A,
                slot_key=SLOT_B,
            )
            pr1.add_comment(format_v6_record_marker(owner1), is_app=True)
            req1 = RecoveryRequest(
                owner=OWNER,
                repo=REPO,
                repository_id=REPO_ID,
                pull_request=PR_NUMBER,
                operation_id="op-real-handled",
                ownership_proof=WriterOwnershipProof(
                    repository_id=REPO_ID,
                    pull_request=PR_NUMBER,
                    operation_id="op-real-handled",
                    run_id=101,
                    run_attempt=1,
                    writer_epoch=1,
                    pr_head_sha=HEAD_A,
                    verified_by_broker=True,
                ),
                event=GitHubEventKind.PULL_REQUEST,
                visibility=RepositoryVisibility.PUBLIC,
                granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
                expected_app_id=APP_ID,
                expected_bot_login=BOT_LOGIN,
                expected_bot_user_id=BOT_USER_ID,
                slot_key=SLOT_B,
                generation=1,
                owner_marker=owner1,
                fresh_analysis_context_loader=lambda: _make_context(real_service_1),
            )
            eng1 = V6RecoveryEngine(
                DualCredentialGitHubClient(
                    workflow_token_transport=_ScriptedTransport(pr1.handle_wf),
                    broker_app_transport=_ScriptedTransport(pr1.handle_app),
                ),
                now_fn=lambda: 1_700_000_000.0,
                sleeper=lambda _s: None,
                broker_fence_verifier=_fence_verifier,
                mutation_bound=MutationSanityBound(max_writes=32),
            )
            res1 = eng1.recover_and_publish(req1)
            self.assertIsInstance(res1, RecoveryHold)
            assert isinstance(res1, RecoveryHold)
            self.assertEqual(res1.blocker, "provider_failed")
            self.assertEqual(res1.model_calls_started, 2)
            self.assertEqual(res1.operation_model_calls_total, 2)
            self.assertFalse(res1.operation_model_calls_unknown)
            self.assertFalse(res1.zero_model_calls_started)
            self.assertEqual(transport_calls_1, 2)
            self.assertIn("calls:2", res1.missing_evidence[0])
            attempts1 = [
                r
                for c in pr1.comments
                for r in extract_v6_record_markers(c["body"])
                if isinstance(r, BatchAttemptRecord)
            ]
            self.assertEqual(len(attempts1), 1)
            self.assertEqual(attempts1[0].provider_calls, 2)
            self.assertEqual(attempts1[0].status, "failed")
            self.assertEqual(attempts1[0].diagnostic, "provider_failed")
            self.assertFalse(any("batch-output:" in c["body"] for c in pr1.comments))
            self.assertEqual(pr1.reviews, [])

            # Fresh process on same event holds with 0 extra provider calls, 2 total, and 0 reviews.
            eng1_fresh = V6RecoveryEngine(
                DualCredentialGitHubClient(
                    workflow_token_transport=_ScriptedTransport(pr1.handle_wf),
                    broker_app_transport=_ScriptedTransport(pr1.handle_app),
                ),
                now_fn=lambda: 1_700_000_000.0,
                sleeper=lambda _s: None,
                broker_fence_verifier=_fence_verifier,
                mutation_bound=MutationSanityBound(max_writes=32),
            )
            res1_rerun = eng1_fresh.recover_and_publish(req1)
            self.assertIsInstance(res1_rerun, RecoveryHold)
            assert isinstance(res1_rerun, RecoveryHold)
            self.assertEqual(res1_rerun.blocker, "provider_failed")
            self.assertEqual(res1_rerun.model_calls_started, 0)
            self.assertEqual(res1_rerun.operation_model_calls_total, 2)
            self.assertFalse(res1_rerun.operation_model_calls_unknown)
            self.assertTrue(res1_rerun.zero_model_calls_started)
            self.assertIn("calls:2", res1_rerun.missing_evidence[0])
            self.assertEqual(transport_calls_1, 2)
            self.assertEqual(pr1.reviews, [])

            # Pilot report verification for Case 1: 2 on original run, 0 additional on restart, 2 total.
            with tempfile.TemporaryDirectory() as tmpdir_s:
                tmpdir = Path(tmpdir_s)
                transport_calls_1p = 0

                def _opener_503_then_400_pilot(
                    http_request: Any, *, timeout: float, context: Any
                ) -> Any:
                    nonlocal transport_calls_1p
                    transport_calls_1p += 1
                    if transport_calls_1p == 1:
                        hdrs = Message()
                        hdrs["Retry-After"] = "0"
                        err = HTTPError(
                            http_request.full_url,
                            503,
                            "Service Unavailable",
                            hdrs,
                            io.BytesIO(b"unavailable"),
                        )
                        opened_errors.append(err)
                        raise err
                    err = HTTPError(
                        http_request.full_url,
                        400,
                        "Bad Request",
                        Message(),
                        io.BytesIO(b"bad request"),
                    )
                    opened_errors.append(err)
                    raise err

                svc_1p = ReviewService(
                    OpenAICompatibleProvider(
                        base_url="https://api.openai.com/v1",
                        model="gpt-4o-mini",
                        api_key="sk-test-fake-key",
                        opener=_opener_503_then_400_pilot,
                    ),
                    stages=DEFAULT_STAGES,
                )
                pr1_p = _SimulatedPRSurface(head_sha=HEAD_A)
                coord1_p = _SimulatedCoordinatorStore(
                    clock=lambda: 1_700_000_000.0, pr=pr1_p
                )
                client1_p = V6CoordinatorClient(
                    base_url="https://coordinator.invalid",
                    oidc_token_provider=lambda: "oidc-jwt-token",
                    opener=coord1_p.opener,
                    clock=lambda: 1_700_000_000.0,
                )
                out1_p = V6PilotRunner(
                    coordinator=client1_p,
                    workflow_token_transport=_ScriptedTransport(pr1_p.handle_wf),
                    app_transport_factory=lambda _g: _ScriptedTransport(
                        pr1_p.handle_app
                    ),
                    clock=lambda: 1_700_000_000.0,
                    sleeper=lambda _s: None,
                    broker_fence_verifier=_fence_verifier,
                ).run(
                    V6PilotRunConfig(
                        owner=OWNER,
                        repo=REPO,
                        repository_id=REPO_ID,
                        pull_request=PR_NUMBER,
                        head_sha=HEAD_A,
                        operation_id="op-pilot-handled",
                        workflow_sha=WORKFLOW_SHA,
                        expected_app_id=APP_ID,
                        expected_bot_login=BOT_LOGIN,
                        expected_bot_user_id=BOT_USER_ID,
                        policy_digest=policy_digest,
                        configuration_digest=config_digest,
                        outcome_path=tmpdir / "case1_run1.json",
                        fresh_analysis_context_loader=lambda: _make_context(svc_1p),
                    )
                )
                report1_r1 = json.loads(
                    (tmpdir / "case1_run1.json").read_text(encoding="utf-8")
                )
                self.assertEqual(out1_p.status, "hold")
                self.assertEqual(out1_p.model_calls_started, 2)
                self.assertEqual(out1_p.operation_model_calls_total, 2)
                self.assertEqual(report1_r1["model_calls_started"], 2)
                self.assertEqual(report1_r1["operation_model_calls_total"], 2)

                out1_p2 = V6PilotRunner(
                    coordinator=client1_p,
                    workflow_token_transport=_ScriptedTransport(pr1_p.handle_wf),
                    app_transport_factory=lambda _g: _ScriptedTransport(
                        pr1_p.handle_app
                    ),
                    clock=lambda: 1_700_000_000.0,
                    sleeper=lambda _s: None,
                    broker_fence_verifier=_fence_verifier,
                ).run(
                    V6PilotRunConfig(
                        owner=OWNER,
                        repo=REPO,
                        repository_id=REPO_ID,
                        pull_request=PR_NUMBER,
                        head_sha=HEAD_A,
                        operation_id="op-pilot-handled",
                        run_attempt=2,
                        workflow_sha=WORKFLOW_SHA,
                        expected_app_id=APP_ID,
                        expected_bot_login=BOT_LOGIN,
                        expected_bot_user_id=BOT_USER_ID,
                        policy_digest=policy_digest,
                        configuration_digest=config_digest,
                        outcome_path=tmpdir / "case1_run2.json",
                        fresh_analysis_context_loader=lambda: _make_context(svc_1p),
                    )
                )
                report1_r2 = json.loads(
                    (tmpdir / "case1_run2.json").read_text(encoding="utf-8")
                )
                self.assertEqual(out1_p2.status, "hold")
                self.assertEqual(out1_p2.model_calls_started, 0)
                self.assertEqual(out1_p2.operation_model_calls_total, 2)
                self.assertEqual(report1_r2["model_calls_started"], 0)
                self.assertEqual(report1_r2["operation_model_calls_total"], 2)
                self.assertEqual(transport_calls_1p, 2)
                self.assertEqual(pr1_p.reviews, [])

            # Case 2: Unexpected exception inside real ReviewService.run() with no reliable count:
            # holds fail-closed without recording 0 or guessing; fresh process holds with 0 extra calls.
            transport_calls_2 = 0

            def _opener_503(http_request: Any, *, timeout: float, context: Any) -> Any:
                nonlocal transport_calls_2
                transport_calls_2 += 1
                hdrs = Message()
                hdrs["Retry-After"] = "1"
                err = HTTPError(
                    http_request.full_url,
                    503,
                    "Service Unavailable",
                    hdrs,
                    io.BytesIO(b"unavailable"),
                )
                opened_errors.append(err)
                raise err

            real_provider_2 = OpenAICompatibleProvider(
                base_url="https://api.openai.com/v1",
                model="gpt-4o-mini",
                api_key="sk-test-fake-key",
                opener=_opener_503,
            )
            real_service_2 = ReviewService(real_provider_2, stages=DEFAULT_STAGES)

            pr2 = _SimulatedPRSurface(head_sha=HEAD_A)
            owner2 = _make_owner_record(
                operation_id="op-real-unexpected",
                pr_head_sha=HEAD_A,
                slot_key=SLOT_B,
            )
            pr2.add_comment(format_v6_record_marker(owner2), is_app=True)
            req2 = RecoveryRequest(
                owner=OWNER,
                repo=REPO,
                repository_id=REPO_ID,
                pull_request=PR_NUMBER,
                operation_id="op-real-unexpected",
                ownership_proof=WriterOwnershipProof(
                    repository_id=REPO_ID,
                    pull_request=PR_NUMBER,
                    operation_id="op-real-unexpected",
                    run_id=102,
                    run_attempt=1,
                    writer_epoch=1,
                    pr_head_sha=HEAD_A,
                    verified_by_broker=True,
                ),
                event=GitHubEventKind.PULL_REQUEST,
                visibility=RepositoryVisibility.PUBLIC,
                granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
                expected_app_id=APP_ID,
                expected_bot_login=BOT_LOGIN,
                expected_bot_user_id=BOT_USER_ID,
                slot_key=SLOT_B,
                generation=1,
                owner_marker=owner2,
                fresh_analysis_context_loader=lambda: _make_context(real_service_2),
            )
            eng2 = V6RecoveryEngine(
                DualCredentialGitHubClient(
                    workflow_token_transport=_ScriptedTransport(pr2.handle_wf),
                    broker_app_transport=_ScriptedTransport(pr2.handle_app),
                ),
                now_fn=lambda: 1_700_000_000.0,
                sleeper=lambda _s: None,
                broker_fence_verifier=_fence_verifier,
                mutation_bound=MutationSanityBound(max_writes=32),
            )
            with mock.patch(
                "review_sensei.service.time.sleep",
                side_effect=RuntimeError("unexpected retry interruption"),
            ):
                res2 = eng2.recover_and_publish(req2)

            self.assertIsInstance(res2, RecoveryHold)
            assert isinstance(res2, RecoveryHold)
            self.assertEqual(res2.blocker, "ambiguous_batch_inference_outcome")
            self.assertEqual(
                res2.missing_evidence, ("batch_dispatch:batch-000:attempt:1",)
            )
            self.assertIsNone(res2.model_calls_started)
            self.assertIsNone(res2.operation_model_calls_total)
            self.assertTrue(res2.operation_model_calls_unknown)
            self.assertFalse(res2.zero_model_calls_started)
            self.assertEqual(transport_calls_2, 1)
            self.assertEqual(pr2.reviews, [])
            self.assertFalse(any("batch-attempt:" in c["body"] for c in pr2.comments))
            self.assertFalse(any("batch-output:" in c["body"] for c in pr2.comments))
            self.assertTrue(any("batch-dispatch:" in c["body"] for c in pr2.comments))

            # Fresh process on same event holds without calling provider or writing a review.
            eng2_fresh = V6RecoveryEngine(
                DualCredentialGitHubClient(
                    workflow_token_transport=_ScriptedTransport(pr2.handle_wf),
                    broker_app_transport=_ScriptedTransport(pr2.handle_app),
                ),
                now_fn=lambda: 1_700_000_000.0,
                sleeper=lambda _s: None,
                broker_fence_verifier=_fence_verifier,
                mutation_bound=MutationSanityBound(max_writes=32),
            )
            res2_fresh = eng2_fresh.recover_and_publish(req2)
            self.assertIsInstance(res2_fresh, RecoveryHold)
            assert isinstance(res2_fresh, RecoveryHold)
            self.assertEqual(res2_fresh.blocker, "ambiguous_batch_inference_outcome")
            self.assertEqual(
                res2_fresh.missing_evidence, ("batch_dispatch:batch-000:attempt:1",)
            )
            self.assertEqual(res2_fresh.model_calls_started, 0)
            self.assertIsNone(res2_fresh.operation_model_calls_total)
            self.assertTrue(res2_fresh.operation_model_calls_unknown)
            self.assertTrue(res2_fresh.zero_model_calls_started)
            self.assertEqual(transport_calls_2, 1)
            self.assertEqual(pr2.reviews, [])

            # Pilot report verification for Case 2: unknown on Run 1, 0 additional / unknown total on restart.
            with tempfile.TemporaryDirectory() as tmpdir_s:
                tmpdir = Path(tmpdir_s)
                transport_calls_2p = 0

                def _opener_503_pilot(
                    http_request: Any, *, timeout: float, context: Any
                ) -> Any:
                    nonlocal transport_calls_2p
                    transport_calls_2p += 1
                    hdrs = Message()
                    hdrs["Retry-After"] = "1"
                    err = HTTPError(
                        http_request.full_url,
                        503,
                        "Service Unavailable",
                        hdrs,
                        io.BytesIO(b"unavailable"),
                    )
                    opened_errors.append(err)
                    raise err

                svc_2p = ReviewService(
                    OpenAICompatibleProvider(
                        base_url="https://api.openai.com/v1",
                        model="gpt-4o-mini",
                        api_key="sk-test-fake-key",
                        opener=_opener_503_pilot,
                    ),
                    stages=DEFAULT_STAGES,
                )
                pr2_p = _SimulatedPRSurface(head_sha=HEAD_A)
                coord2_p = _SimulatedCoordinatorStore(
                    clock=lambda: 1_700_000_000.0, pr=pr2_p
                )
                client2_p = V6CoordinatorClient(
                    base_url="https://coordinator.invalid",
                    oidc_token_provider=lambda: "oidc-jwt-token",
                    opener=coord2_p.opener,
                    clock=lambda: 1_700_000_000.0,
                )
                with mock.patch(
                    "review_sensei.service.time.sleep",
                    side_effect=RuntimeError("unexpected retry interruption"),
                ):
                    out2_p = V6PilotRunner(
                        coordinator=client2_p,
                        workflow_token_transport=_ScriptedTransport(pr2_p.handle_wf),
                        app_transport_factory=lambda _g: _ScriptedTransport(
                            pr2_p.handle_app
                        ),
                        clock=lambda: 1_700_000_000.0,
                        sleeper=lambda _s: None,
                        broker_fence_verifier=_fence_verifier,
                    ).run(
                        V6PilotRunConfig(
                            owner=OWNER,
                            repo=REPO,
                            repository_id=REPO_ID,
                            pull_request=PR_NUMBER,
                            head_sha=HEAD_A,
                            operation_id="op-pilot-unexpected",
                            workflow_sha=WORKFLOW_SHA,
                            expected_app_id=APP_ID,
                            expected_bot_login=BOT_LOGIN,
                            expected_bot_user_id=BOT_USER_ID,
                            policy_digest=policy_digest,
                            configuration_digest=config_digest,
                            outcome_path=tmpdir / "case2_run1.json",
                            fresh_analysis_context_loader=lambda: _make_context(svc_2p),
                        )
                    )
                report2_r1 = json.loads(
                    (tmpdir / "case2_run1.json").read_text(encoding="utf-8")
                )
                self.assertEqual(out2_p.status, "hold")
                self.assertEqual(out2_p.model_calls_started, "unknown")
                self.assertEqual(out2_p.operation_model_calls_total, "unknown")
                self.assertEqual(report2_r1["model_calls_started"], "unknown")
                self.assertEqual(report2_r1["operation_model_calls_total"], "unknown")

                out2_p2 = V6PilotRunner(
                    coordinator=client2_p,
                    workflow_token_transport=_ScriptedTransport(pr2_p.handle_wf),
                    app_transport_factory=lambda _g: _ScriptedTransport(
                        pr2_p.handle_app
                    ),
                    clock=lambda: 1_700_000_000.0,
                    sleeper=lambda _s: None,
                    broker_fence_verifier=_fence_verifier,
                ).run(
                    V6PilotRunConfig(
                        owner=OWNER,
                        repo=REPO,
                        repository_id=REPO_ID,
                        pull_request=PR_NUMBER,
                        head_sha=HEAD_A,
                        operation_id="op-pilot-unexpected",
                        run_attempt=2,
                        workflow_sha=WORKFLOW_SHA,
                        expected_app_id=APP_ID,
                        expected_bot_login=BOT_LOGIN,
                        expected_bot_user_id=BOT_USER_ID,
                        policy_digest=policy_digest,
                        configuration_digest=config_digest,
                        outcome_path=tmpdir / "case2_run2.json",
                        fresh_analysis_context_loader=lambda: _make_context(svc_2p),
                    )
                )
                report2_r2 = json.loads(
                    (tmpdir / "case2_run2.json").read_text(encoding="utf-8")
                )
                self.assertEqual(out2_p2.status, "hold")
                self.assertEqual(out2_p2.model_calls_started, 0)
                self.assertEqual(out2_p2.operation_model_calls_total, "unknown")
                self.assertEqual(report2_r2["model_calls_started"], 0)
                self.assertEqual(report2_r2["operation_model_calls_total"], "unknown")
                self.assertEqual(transport_calls_2p, 1)
                self.assertEqual(pr2_p.reviews, [])

            # Case 3: Real pre-provider stop through OpenAICompatibleProvider + ReviewService:
            # 0 provider calls stays 0 in PR ledger, fresh-process restart, aggregation, and pilot report.
            transport_calls_3 = 0

            def _opener_never_called(
                http_request: Any, *, timeout: float, context: Any
            ) -> Any:
                nonlocal transport_calls_3
                transport_calls_3 += 1
                raise AssertionError("pre-provider stop must not call transport")

            real_provider_3 = OpenAICompatibleProvider(
                base_url="https://api.openai.com/v1",
                model="gpt-4o-mini",
                api_key="sk-test-fake-key",
                opener=_opener_never_called,
            )
            real_service_3 = ReviewService(real_provider_3, stages=DEFAULT_STAGES)

            pr3 = _SimulatedPRSurface(head_sha=HEAD_A)
            owner3 = _make_owner_record(
                operation_id="op-real-known-zero",
                pr_head_sha=HEAD_A,
                slot_key=SLOT_B,
            )
            pr3.add_comment(format_v6_record_marker(owner3), is_app=True)
            req3 = RecoveryRequest(
                owner=OWNER,
                repo=REPO,
                repository_id=REPO_ID,
                pull_request=PR_NUMBER,
                operation_id="op-real-known-zero",
                ownership_proof=WriterOwnershipProof(
                    repository_id=REPO_ID,
                    pull_request=PR_NUMBER,
                    operation_id="op-real-known-zero",
                    run_id=103,
                    run_attempt=1,
                    writer_epoch=1,
                    pr_head_sha=HEAD_A,
                    verified_by_broker=True,
                ),
                event=GitHubEventKind.PULL_REQUEST,
                visibility=RepositoryVisibility.PUBLIC,
                granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
                expected_app_id=APP_ID,
                expected_bot_login=BOT_LOGIN,
                expected_bot_user_id=BOT_USER_ID,
                slot_key=SLOT_B,
                generation=1,
                owner_marker=owner3,
                fresh_analysis_context_loader=lambda: _make_context(real_service_3),
            )
            eng3 = V6RecoveryEngine(
                DualCredentialGitHubClient(
                    workflow_token_transport=_ScriptedTransport(pr3.handle_wf),
                    broker_app_transport=_ScriptedTransport(pr3.handle_app),
                ),
                now_fn=lambda: 1_700_000_000.0,
                sleeper=lambda _s: None,
                broker_fence_verifier=_fence_verifier,
                mutation_bound=MutationSanityBound(max_writes=32),
            )
            mono_ticks = 0

            def _expired_monotonic() -> float:
                nonlocal mono_ticks
                mono_ticks += 1
                return 0.0 if mono_ticks == 1 else 10_000.0

            with mock.patch(
                "review_sensei.service.time.monotonic", side_effect=_expired_monotonic
            ):
                res3 = eng3.recover_and_publish(req3)

            self.assertIsInstance(res3, RecoveryHold)
            assert isinstance(res3, RecoveryHold)
            self.assertEqual(res3.blocker, "deadline_exceeded")
            self.assertEqual(res3.model_calls_started, 0)
            self.assertEqual(res3.operation_model_calls_total, 0)
            self.assertFalse(res3.operation_model_calls_unknown)
            self.assertTrue(res3.zero_model_calls_started)
            self.assertEqual(
                res3.missing_evidence,
                ("batch_attempt:batch-000:attempt:1:calls:0:status:failed",),
            )
            self.assertEqual(transport_calls_3, 0)
            attempts3 = [
                r
                for c in pr3.comments
                for r in extract_v6_record_markers(c["body"])
                if isinstance(r, BatchAttemptRecord)
            ]
            self.assertEqual(len(attempts3), 1)
            self.assertEqual(attempts3[0].provider_calls, 0)
            self.assertEqual(attempts3[0].status, "failed")
            self.assertEqual(attempts3[0].diagnostic, "deadline_exceeded")
            self.assertEqual(pr3.reviews, [])

            # Fresh process holds at 0 total calls (does not replay, does not floor to 1).
            eng3_fresh = V6RecoveryEngine(
                DualCredentialGitHubClient(
                    workflow_token_transport=_ScriptedTransport(pr3.handle_wf),
                    broker_app_transport=_ScriptedTransport(pr3.handle_app),
                ),
                now_fn=lambda: 1_700_000_000.0,
                sleeper=lambda _s: None,
                broker_fence_verifier=_fence_verifier,
                mutation_bound=MutationSanityBound(max_writes=32),
            )
            res3_rerun = eng3_fresh.recover_and_publish(req3)
            self.assertIsInstance(res3_rerun, RecoveryHold)
            assert isinstance(res3_rerun, RecoveryHold)
            self.assertEqual(res3_rerun.blocker, "deadline_exceeded")
            self.assertEqual(res3_rerun.model_calls_started, 0)
            self.assertEqual(res3_rerun.operation_model_calls_total, 0)
            self.assertFalse(res3_rerun.operation_model_calls_unknown)
            self.assertIn("calls:0", res3_rerun.missing_evidence[0])
            self.assertEqual(transport_calls_3, 0)
            self.assertEqual(pr3.reviews, [])

            # Restart aggregation proof: attempt=1 (0 calls) + attempt=2 BatchOutputRecord (1 call) = 1 < max_provider_calls=2.
            # Second batch MUST be admitted (proving 0 was NOT floored to 1).
            inv2 = (
                {
                    "path": "src/a.py",
                    "status": "modified",
                    "disposition": "text",
                    "hunk_ids": [hunk_a],
                },
                {
                    "path": "src/b.py",
                    "status": "modified",
                    "disposition": "text",
                    "hunk_ids": [hunk_b],
                },
            )
            b1 = V6PlannedBatch(
                batch_id="batch-001",
                diff_text=diff_b,
                diff_sha256=canonical_sha256(diff_b),
                prompt_bytes=200,
                paths=("src/b.py",),
                primary_hunk_ids=(hunk_b,),
                parent_hunk_ids=(hunk_b,),
            )
            req3_agg_base = replace(
                req3,
                operation_id="op-real-known-zero-agg",
                ownership_proof=replace(
                    req3.ownership_proof, operation_id="op-real-known-zero-agg"
                ),
                owner_marker=_make_owner_record(
                    operation_id="op-real-known-zero-agg",
                    pr_head_sha=HEAD_A,
                    slot_key=SLOT_B,
                ),
            )
            # First compute exact plan_sha256 for 2-batch plan with max_provider_calls=2:
            from types import SimpleNamespace

            in_dig2 = canonical_sha256(
                {
                    "pr_head_sha": HEAD_A,
                    "effect_kind": "pr_review_publish",
                    "findings": [],
                    "required_evidence_digests": [],
                    "artifact_cache_sha256": None,
                }
            )
            plan2_rec = BatchPlanRecord(
                record_id=f"batch-plan:op-real-known-zero-agg:{HEAD_A[:12]}:{in_dig2}",
                operation_id="op-real-known-zero-agg",
                base_sha=BASE_SHA,
                merge_base_sha=BASE_SHA,
                pr_head_sha=HEAD_A,
                inventory_sha256=canonical_sha256(inv2),
                policy_digest=policy_digest,
                configuration_digest=config_digest,
                max_provider_calls=2,
                batches=(b0.to_plan_dict(), b1.to_plan_dict()),
                non_text_changes=(),
            )
            plan2_sha = canonical_sha256(record_to_dict(plan2_rec))
            pr3_agg = _SimulatedPRSurface(head_sha=HEAD_A)
            for rec_to_seed in (
                req3_agg_base.owner_marker,
                plan2_rec,
                BatchDispatchRecord(
                    record_id=f"batch-dispatch:op-real-known-zero-agg:{HEAD_A[:12]}:{in_dig2}:batch-000:1",
                    operation_id="op-real-known-zero-agg",
                    batch_id="batch-000",
                    attempt=1,
                    plan_sha256=plan2_sha,
                    prompt_sha256=canonical_sha256(b0.to_plan_dict()),
                    generation=1,
                ),
                BatchAttemptRecord(
                    record_id=f"batch-attempt:op-real-known-zero-agg:{HEAD_A[:12]}:{in_dig2}:batch-000:1",
                    operation_id="op-real-known-zero-agg",
                    batch_id="batch-000",
                    attempt=1,
                    plan_sha256=plan2_sha,
                    status="failed",
                    provider_calls=0,
                    diagnostic="deadline_exceeded",
                ),
                BatchOutputRecord(
                    record_id=f"batch-output:op-real-known-zero-agg:{HEAD_A[:12]}:{in_dig2}:batch-000:2",
                    operation_id="op-real-known-zero-agg",
                    batch_id="batch-000",
                    plan_sha256=plan2_sha,
                    hunk_ids_reviewed=(hunk_a,),
                    findings=(),
                    summary="Recovered A",
                    provider="openai-compatible",
                    model="gpt-4o-mini",
                    review_status="complete",
                    attempt=2,
                    provider_calls=1,
                ),
            ):
                assert rec_to_seed is not None
                pr3_agg.add_comment(format_v6_record_marker(rec_to_seed), is_app=True)

            b1_remaining_seen: list[int] = []

            def _svc_b1_run(r: Any, *, budget: Any) -> Any:
                b1_remaining_seen.append(budget.max_provider_calls)
                return SimpleNamespace(
                    result=ReviewResult(
                        summary="Batch B ok",
                        comments=(
                            ReviewComment(
                                path="src/b.py", line=1, body="Clean finding B"
                            ),
                        ),
                        provider="openai-compatible",
                        model="gpt-4o-mini",
                        review_status="complete",
                    ),
                    outcome=SimpleNamespace(
                        status="reviewed",
                        diagnostic=None,
                        provider_calls=1,
                    ),
                )

            svc_b1 = SimpleNamespace(run=_svc_b1_run)
            res3_agg = V6RecoveryEngine(
                DualCredentialGitHubClient(
                    workflow_token_transport=_ScriptedTransport(pr3_agg.handle_wf),
                    broker_app_transport=_ScriptedTransport(pr3_agg.handle_app),
                ),
                now_fn=lambda: 1_700_000_000.0,
                sleeper=lambda _s: None,
                broker_fence_verifier=_fence_verifier,
                mutation_bound=MutationSanityBound(max_writes=32),
            ).recover_and_publish(
                replace(
                    req3_agg_base,
                    fresh_analysis_context_loader=lambda: V6FreshAnalysisContext(
                        owner=OWNER,
                        repo=REPO,
                        pull_request=PR_NUMBER,
                        pr_title="Aggregating zero calls",
                        base_sha=BASE_SHA,
                        merge_base_sha=BASE_SHA,
                        head_sha=HEAD_A,
                        inventory_sha256=canonical_sha256(inv2),
                        inventory=inv2,
                        non_text_changes=(),
                        policy_digest=policy_digest,
                        configuration_digest=config_digest,
                        max_provider_calls=2,
                        planned_batches=(b0, b1),
                        all_expected_hunk_ids=(hunk_a, hunk_b),
                        sub_hunks_by_parent={},
                        combined_diff_sha256=canonical_sha256(diff_a + diff_b),
                        provider_name="openai-compatible",
                        model_name="gpt-4o-mini",
                        service=svc_b1,
                    ),
                )
            )
            self.assertIsInstance(res3_agg, RecoveryCompleted)
            assert isinstance(res3_agg, RecoveryCompleted)
            self.assertEqual(b1_remaining_seen, [1])
            self.assertEqual(res3_agg.model_calls_started, 1)
            self.assertEqual(res3_agg.operation_model_calls_total, 2)

            # Pilot report verification for Case 3: 0 on Run 1, 0 additional / 0 total on restart.
            with tempfile.TemporaryDirectory() as tmpdir_s:
                tmpdir = Path(tmpdir_s)
                pr3_p = _SimulatedPRSurface(head_sha=HEAD_A)
                coord3_p = _SimulatedCoordinatorStore(
                    clock=lambda: 1_700_000_000.0, pr=pr3_p
                )
                client3_p = V6CoordinatorClient(
                    base_url="https://coordinator.invalid",
                    oidc_token_provider=lambda: "oidc-jwt-token",
                    opener=coord3_p.opener,
                    clock=lambda: 1_700_000_000.0,
                )
                mono_ticks_p = 0

                def _expired_monotonic_pilot() -> float:
                    nonlocal mono_ticks_p
                    mono_ticks_p += 1
                    return 0.0 if mono_ticks_p == 1 else 10_000.0

                with mock.patch(
                    "review_sensei.service.time.monotonic",
                    side_effect=_expired_monotonic_pilot,
                ):
                    out3_p = V6PilotRunner(
                        coordinator=client3_p,
                        workflow_token_transport=_ScriptedTransport(pr3_p.handle_wf),
                        app_transport_factory=lambda _g: _ScriptedTransport(
                            pr3_p.handle_app
                        ),
                        clock=lambda: 1_700_000_000.0,
                        sleeper=lambda _s: None,
                        broker_fence_verifier=_fence_verifier,
                    ).run(
                        V6PilotRunConfig(
                            owner=OWNER,
                            repo=REPO,
                            repository_id=REPO_ID,
                            pull_request=PR_NUMBER,
                            head_sha=HEAD_A,
                            operation_id="op-pilot-known-zero",
                            workflow_sha=WORKFLOW_SHA,
                            expected_app_id=APP_ID,
                            expected_bot_login=BOT_LOGIN,
                            expected_bot_user_id=BOT_USER_ID,
                            policy_digest=policy_digest,
                            configuration_digest=config_digest,
                            outcome_path=tmpdir / "case3_run1.json",
                            fresh_analysis_context_loader=lambda: _make_context(
                                real_service_3
                            ),
                        )
                    )
                report3_r1 = json.loads(
                    (tmpdir / "case3_run1.json").read_text(encoding="utf-8")
                )
                self.assertEqual(out3_p.status, "hold")
                self.assertEqual(out3_p.model_calls_started, 0)
                self.assertEqual(out3_p.operation_model_calls_total, 0)
                self.assertEqual(report3_r1["model_calls_started"], 0)
                self.assertEqual(report3_r1["operation_model_calls_total"], 0)

                out3_p2 = V6PilotRunner(
                    coordinator=client3_p,
                    workflow_token_transport=_ScriptedTransport(pr3_p.handle_wf),
                    app_transport_factory=lambda _g: _ScriptedTransport(
                        pr3_p.handle_app
                    ),
                    clock=lambda: 1_700_000_000.0,
                    sleeper=lambda _s: None,
                    broker_fence_verifier=_fence_verifier,
                ).run(
                    V6PilotRunConfig(
                        owner=OWNER,
                        repo=REPO,
                        repository_id=REPO_ID,
                        pull_request=PR_NUMBER,
                        head_sha=HEAD_A,
                        operation_id="op-pilot-known-zero",
                        run_attempt=2,
                        workflow_sha=WORKFLOW_SHA,
                        expected_app_id=APP_ID,
                        expected_bot_login=BOT_LOGIN,
                        expected_bot_user_id=BOT_USER_ID,
                        policy_digest=policy_digest,
                        configuration_digest=config_digest,
                        outcome_path=tmpdir / "case3_run2.json",
                        fresh_analysis_context_loader=lambda: _make_context(
                            real_service_3
                        ),
                    )
                )
                report3_r2 = json.loads(
                    (tmpdir / "case3_run2.json").read_text(encoding="utf-8")
                )
                self.assertEqual(out3_p2.status, "hold")
                self.assertEqual(out3_p2.model_calls_started, 0)
                self.assertEqual(out3_p2.operation_model_calls_total, 0)
                self.assertEqual(report3_r2["model_calls_started"], 0)
                self.assertEqual(report3_r2["operation_model_calls_total"], 0)
                self.assertEqual(transport_calls_3, 0)
                self.assertEqual(pr3_p.reviews, [])
        finally:
            for err in opened_errors:
                err.close()

    def test_pilot_outcome_call_report_edge_cases(self) -> None:
        from review_sensei_github.v6_pilot_runner import V6PilotRunOutcome

        class _FakeUnknown:
            def __eq__(self, other: object) -> bool:
                return other == "unknown"

        with self.assertRaises(ValueError):
            V6PilotRunOutcome(status="hold", model_calls_started=_FakeUnknown())  # type: ignore[arg-type]

        # When coordinator is already in HOLD and PR comments read fails transiently,
        # operation_model_calls_total must be "unknown" rather than 0.
        pr_hold = _SimulatedPRSurface(head_sha=HEAD_A)
        coord_hold = _SimulatedCoordinatorStore(
            clock=lambda: 1_700_000_000.0, pr=pr_hold
        )
        coord_hold.state = "HOLD"
        coord_hold.last_reason_code = "v6_lease_hold"
        client_hold = V6CoordinatorClient(
            base_url="https://coordinator.invalid",
            oidc_token_provider=lambda: "oidc-jwt-token",
            opener=coord_hold.opener,
            clock=lambda: 1_700_000_000.0,
        )

        def _failing_wf(call: Mapping[str, Any]) -> tuple[int, Mapping[str, str], Any]:
            if call["method"] == "GET" and "/comments" in call["path"]:
                return 503, {}, {"message": "transient"}
            return pr_hold.handle_wf(call)

        out_hold = V6PilotRunner(
            coordinator=client_hold,
            workflow_token_transport=_ScriptedTransport(_failing_wf),
            clock=lambda: 1_700_000_000.0,
            sleeper=lambda _s: None,
        ).run(
            V6PilotRunConfig(
                owner=OWNER,
                repo=REPO,
                repository_id=REPO_ID,
                pull_request=PR_NUMBER,
                head_sha=HEAD_A,
                operation_id="op-hold-scan-err",
                workflow_sha=WORKFLOW_SHA,
                expected_app_id=APP_ID,
                expected_bot_login=BOT_LOGIN,
                expected_bot_user_id=BOT_USER_ID,
            )
        )
        self.assertEqual(out_hold.status, "hold")
        self.assertEqual(out_hold.model_calls_started, 0)
        self.assertEqual(out_hold.operation_model_calls_total, "unknown")

    def test_pause_after_model_calls_reports_exact_counts_fresh_and_after_takeover(
        self,
    ) -> None:
        from types import SimpleNamespace

        from review_sensei.configuration import default_configuration
        from review_sensei.models import ReviewComment, ReviewResult

        # 1. RecoveryPaused validation allows non-negative int & cumulative total, rejects invalid values.
        hint_sample = NoProgressGuard(operation_id="op-paused-val").to_advisory_hint()
        valid_paused = RecoveryPaused(
            operation_id="op-paused-val",
            resume_after_epoch=1_700_003_600.0,
            checkpoint_state=hint_sample,
            reason="secondary rate limit",
            model_calls_started=2,
            operation_model_calls_total=2,
            operation_model_calls_unknown=False,
        )
        self.assertEqual(valid_paused.model_calls_started, 2)
        self.assertEqual(valid_paused.operation_model_calls_total, 2)
        with self.assertRaises(ValueError):
            RecoveryPaused(
                operation_id="op-paused-val",
                resume_after_epoch=1_700_003_600.0,
                checkpoint_state=hint_sample,
                reason="bad negative",
                model_calls_started=-1,
            )
        with self.assertRaises(ValueError):
            RecoveryPaused(
                operation_id="op-paused-val",
                resume_after_epoch=1_700_003_600.0,
                checkpoint_state=hint_sample,
                reason="bad bool",
                model_calls_started=True,  # type: ignore[arg-type]
            )
        with self.assertRaises(ValueError):
            RecoveryPaused(
                operation_id="op-paused-val",
                resume_after_epoch=1_700_003_600.0,
                checkpoint_state=hint_sample,
                reason="bad unknown",
                model_calls_started=0,
                operation_model_calls_total=1,
                operation_model_calls_unknown=True,
            )

        policy_digest, config_digest = compute_trusted_policy_and_config_digests(
            default_configuration(),
            provider_name="openai-compatible",
            model_name="gpt-4o-mini",
        )
        hunk_a = "src/pa.py#h0:1,1+1,1:" + ("a" * 16)
        hunk_b = "src/pb.py#h0:1,1+1,1:" + ("b" * 16)
        diff_a = (
            "diff --git a/src/pa.py b/src/pa.py\n"
            "--- a/src/pa.py\n+++ b/src/pa.py\n@@ -1 +1 @@\n-1\n+2\n"
        )
        diff_b = (
            "diff --git a/src/pb.py b/src/pb.py\n"
            "--- a/src/pb.py\n+++ b/src/pb.py\n@@ -1 +1 @@\n-3\n+4\n"
        )
        inv = (
            {
                "path": "src/pa.py",
                "status": "modified",
                "disposition": "text",
                "hunk_ids": [hunk_a],
            },
            {
                "path": "src/pb.py",
                "status": "modified",
                "disposition": "text",
                "hunk_ids": [hunk_b],
            },
        )
        inv_sha = canonical_sha256(inv)
        b0 = V6PlannedBatch(
            batch_id="batch-000",
            diff_text=diff_a,
            diff_sha256=canonical_sha256(diff_a),
            prompt_bytes=200,
            paths=("src/pa.py",),
            primary_hunk_ids=(hunk_a,),
            parent_hunk_ids=(hunk_a,),
        )
        b1 = V6PlannedBatch(
            batch_id="batch-001",
            diff_text=diff_b,
            diff_sha256=canonical_sha256(diff_b),
            prompt_bytes=200,
            paths=("src/pb.py",),
            primary_hunk_ids=(hunk_b,),
            parent_hunk_ids=(hunk_b,),
        )

        def _make_two_batch_ctx() -> V6FreshAnalysisContext:
            def _run(req: Any, *, budget: Any = None) -> Any:
                target = "src/pa.py" if "src/pa.py" in req.diff else "src/pb.py"
                return SimpleNamespace(
                    outcome=SimpleNamespace(status="complete", provider_calls=1),
                    result=ReviewResult(
                        summary=f"OK {target}",
                        comments=(
                            ReviewComment(path=target, line=1, body=f"Note {target}"),
                        ),
                        provider="openai-compatible",
                        model="gpt-4o-mini",
                        review_status="complete",
                    ),
                )

            return V6FreshAnalysisContext(
                owner=OWNER,
                repo=REPO,
                pull_request=PR_NUMBER,
                pr_title="Pause call accounting",
                base_sha=BASE_SHA,
                merge_base_sha=BASE_SHA,
                head_sha=HEAD_A,
                inventory_sha256=inv_sha,
                inventory=inv,
                non_text_changes=(),
                policy_digest=policy_digest,
                configuration_digest=config_digest,
                max_provider_calls=4,
                planned_batches=(b0, b1),
                all_expected_hunk_ids=(hunk_a, hunk_b),
                sub_hunks_by_parent={},
                combined_diff_sha256=canonical_sha256(diff_a + diff_b),
                provider_name="openai-compatible",
                model_name="gpt-4o-mini",
                service=SimpleNamespace(run=_run),
            )

        # 2. Fresh run: executes 2 batches (2 calls), then GitHub throttles POST /reviews -> Retry-After 3600.
        with tempfile.TemporaryDirectory() as tmpdir_s:
            tmpdir = Path(tmpdir_s)
            pr_fresh = _SimulatedPRSurface(head_sha=HEAD_A)
            coord_fresh = _SimulatedCoordinatorStore(
                clock=lambda: 1_700_000_000.0, pr=pr_fresh
            )
            client_fresh = V6CoordinatorClient(
                base_url="https://coordinator.invalid",
                oidc_token_provider=lambda: "oidc-jwt-token",
                opener=coord_fresh.opener,
                clock=lambda: 1_700_000_000.0,
            )

            def _throttle_review_app(
                surface: _SimulatedPRSurface,
            ) -> Callable[[dict[str, Any]], tuple[int, Mapping[str, str], Any]]:
                def _handler(
                    call: dict[str, Any],
                ) -> tuple[int, Mapping[str, str], Any]:
                    if call["method"] == "POST" and str(call["path"]).endswith(
                        "/reviews"
                    ):
                        return (
                            429,
                            {"Retry-After": "3600"},
                            {"message": "secondary rate limit on review post"},
                        )
                    return surface.handle_app(call)

                return _handler

            out_fresh = V6PilotRunner(
                coordinator=client_fresh,
                workflow_token_transport=_ScriptedTransport(pr_fresh.handle_wf),
                app_transport_factory=lambda _g: _ScriptedTransport(
                    _throttle_review_app(pr_fresh)
                ),
                clock=lambda: 1_700_000_000.0,
                sleeper=lambda _s: None,
                broker_fence_verifier=_fence_verifier,
            ).run(
                V6PilotRunConfig(
                    owner=OWNER,
                    repo=REPO,
                    repository_id=REPO_ID,
                    pull_request=PR_NUMBER,
                    head_sha=HEAD_A,
                    operation_id="op-pause-fresh-post",
                    workflow_sha=WORKFLOW_SHA,
                    expected_app_id=APP_ID,
                    expected_bot_login=BOT_LOGIN,
                    expected_bot_user_id=BOT_USER_ID,
                    policy_digest=policy_digest,
                    configuration_digest=config_digest,
                    outcome_path=tmpdir / "fresh_paused.json",
                    fresh_analysis_context_loader=_make_two_batch_ctx,
                )
            )
            fresh_report = json.loads(
                (tmpdir / "fresh_paused.json").read_text(encoding="utf-8")
            )
            self.assertEqual(out_fresh.status, "paused")
            self.assertIsInstance(out_fresh.recovery_outcome, RecoveryPaused)
            assert isinstance(out_fresh.recovery_outcome, RecoveryPaused)
            self.assertEqual(out_fresh.recovery_outcome.model_calls_started, 2)
            self.assertEqual(out_fresh.recovery_outcome.operation_model_calls_total, 2)
            self.assertEqual(out_fresh.model_calls_started, 2)
            self.assertEqual(out_fresh.operation_model_calls_total, 2)
            self.assertEqual(fresh_report["status"], "paused")
            self.assertEqual(fresh_report["model_calls_started"], 2)
            self.assertEqual(fresh_report["operation_model_calls_total"], 2)

            # 3. Takeover run: prior generation ran batch-000 (1 call) and left ACQUIRED slot;
            # successor takes over (takeover_recon.operation_model_calls_total == 1),
            # executes batch-001 (1 new call), then GitHub throttles POST /reviews.
            pr_takeover = _SimulatedPRSurface(head_sha=HEAD_A)
            coord_takeover = _SimulatedCoordinatorStore(
                clock=lambda: 1_700_000_000.0, pr=pr_takeover
            )
            old_owner = _make_owner_record(
                operation_id="op-pause-takeover-post",
                slot_key=coord_takeover.slot_key,
                generation=1,
                lease_epoch=1,
                run_id="5001",
                run_attempt=1,
                pr_head_sha=HEAD_A,
            )
            old_owner_cid = int(
                pr_takeover.add_comment(
                    format_v6_record_marker(old_owner), is_app=True
                )["id"]
            )
            in_dig = canonical_sha256(
                {
                    "pr_head_sha": HEAD_A,
                    "effect_kind": "pr_review_publish",
                    "findings": [],
                    "required_evidence_digests": [],
                    "artifact_cache_sha256": None,
                }
            )
            plan_rec = BatchPlanRecord(
                record_id=f"batch-plan:op-pause-takeover-post:{HEAD_A[:12]}:{in_dig}",
                operation_id="op-pause-takeover-post",
                base_sha=BASE_SHA,
                merge_base_sha=BASE_SHA,
                pr_head_sha=HEAD_A,
                inventory_sha256=inv_sha,
                policy_digest=policy_digest,
                configuration_digest=config_digest,
                max_provider_calls=4,
                batches=(b0.to_plan_dict(), b1.to_plan_dict()),
                non_text_changes=(),
            )
            plan_sha = canonical_sha256(record_to_dict(plan_rec))
            b0_disp = BatchDispatchRecord(
                record_id=f"batch-dispatch:op-pause-takeover-post:{HEAD_A[:12]}:{in_dig}:batch-000:1",
                operation_id="op-pause-takeover-post",
                batch_id="batch-000",
                attempt=1,
                plan_sha256=plan_sha,
                prompt_sha256=canonical_sha256(b0.to_plan_dict()),
                generation=1,
            )
            b0_out = BatchOutputRecord(
                record_id=f"batch-output:op-pause-takeover-post:{HEAD_A[:12]}:{in_dig}:batch-000:1",
                operation_id="op-pause-takeover-post",
                batch_id="batch-000",
                plan_sha256=plan_sha,
                hunk_ids_reviewed=(hunk_a,),
                findings=(
                    {"path": "src/pa.py", "line": 1, "side": "RIGHT", "body": "Prior"},
                ),
                summary="Prior Batch 0",
                provider="openai-compatible",
                model="gpt-4o-mini",
                review_status="complete",
                attempt=1,
                provider_calls=1,
            )
            for ledger_rec in (plan_rec, b0_disp, b0_out):
                pr_takeover.add_comment(
                    format_v6_record_marker(ledger_rec), is_app=True
                )

            coord_takeover.state = "ACTIVE"
            coord_takeover.generation = 1
            coord_takeover.lease_epoch = 1
            coord_takeover.reservation_id = old_owner.reservation_id
            coord_takeover.owner_run_id = "5001"
            coord_takeover.owner_run_attempt = 1
            coord_takeover.owner_head_sha = HEAD_A
            coord_takeover.owner_comment_id = old_owner_cid
            coord_takeover.acquired_at_ms = 1_699_999_000_000
            coord_takeover.token_expires_at_ms = 1_700_000_600_000
            coord_takeover.token_cleared = False
            coord_takeover.predecessor_terminal = True

            client_takeover = V6CoordinatorClient(
                base_url="https://coordinator.invalid",
                oidc_token_provider=lambda: "oidc-jwt-token",
                opener=coord_takeover.opener,
                clock=lambda: 1_700_000_000.0,
            )
            out_takeover = V6PilotRunner(
                coordinator=client_takeover,
                workflow_token_transport=_ScriptedTransport(pr_takeover.handle_wf),
                app_transport_factory=lambda _g: _ScriptedTransport(
                    _throttle_review_app(pr_takeover)
                ),
                clock=lambda: 1_700_000_000.0,
                sleeper=lambda _s: None,
                broker_fence_verifier=_fence_verifier,
            ).run(
                V6PilotRunConfig(
                    owner=OWNER,
                    repo=REPO,
                    repository_id=REPO_ID,
                    pull_request=PR_NUMBER,
                    head_sha=HEAD_A,
                    operation_id="op-pause-takeover-post",
                    run_id=5001,
                    run_attempt=2,
                    workflow_sha=WORKFLOW_SHA,
                    expected_app_id=APP_ID,
                    expected_bot_login=BOT_LOGIN,
                    expected_bot_user_id=BOT_USER_ID,
                    policy_digest=policy_digest,
                    configuration_digest=config_digest,
                    outcome_path=tmpdir / "takeover_paused.json",
                    fresh_analysis_context_loader=_make_two_batch_ctx,
                )
            )
            takeover_report = json.loads(
                (tmpdir / "takeover_paused.json").read_text(encoding="utf-8")
            )
            self.assertIsNotNone(out_takeover.takeover_reconciliation)
            assert out_takeover.takeover_reconciliation is not None
            self.assertEqual(
                out_takeover.takeover_reconciliation.operation_model_calls_total, 1
            )
            self.assertEqual(out_takeover.status, "paused")
            self.assertIsInstance(out_takeover.recovery_outcome, RecoveryPaused)
            assert isinstance(out_takeover.recovery_outcome, RecoveryPaused)
            self.assertEqual(out_takeover.recovery_outcome.model_calls_started, 1)
            self.assertEqual(
                out_takeover.recovery_outcome.operation_model_calls_total, 2
            )
            self.assertEqual(out_takeover.model_calls_started, 1)
            self.assertEqual(out_takeover.operation_model_calls_total, 2)
            self.assertEqual(takeover_report["status"], "paused")
            self.assertEqual(takeover_report["model_calls_started"], 1)
            self.assertEqual(takeover_report["operation_model_calls_total"], 2)

        # 4. Mid-batch throttle after Batch 1 completes (1 call) when posting Batch 2 dispatch.
        pr_mid = _SimulatedPRSurface(head_sha=HEAD_A)
        owner_mid = _make_owner_record(
            operation_id="op-pause-mid-batch", pr_head_sha=HEAD_A, slot_key=SLOT_B
        )
        pr_mid.add_comment(format_v6_record_marker(owner_mid), is_app=True)

        def _throttle_b1_dispatch(
            call: dict[str, Any],
        ) -> tuple[int, Mapping[str, str], Any]:
            if call["method"] == "POST" and "/comments" in str(call["path"]):
                body = str((call.get("body") or {}).get("body", ""))
                if (
                    "batch-dispatch:op-pause-mid-batch:" in body
                    and ":batch-001:" in body
                ):
                    return (
                        429,
                        {"Retry-After": "3600"},
                        {"message": "secondary rate limit on batch-001 dispatch"},
                    )
            return pr_mid.handle_app(call)

        eng_mid = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=_ScriptedTransport(pr_mid.handle_wf),
                broker_app_transport=_ScriptedTransport(_throttle_b1_dispatch),
            ),
            now_fn=lambda: 1_700_000_000.0,
            sleeper=lambda _s: None,
            broker_fence_verifier=_fence_verifier,
            mutation_bound=MutationSanityBound(max_writes=32),
        )
        res_mid = eng_mid.recover_and_publish(
            RecoveryRequest(
                owner=OWNER,
                repo=REPO,
                repository_id=REPO_ID,
                pull_request=PR_NUMBER,
                operation_id="op-pause-mid-batch",
                ownership_proof=WriterOwnershipProof(
                    repository_id=REPO_ID,
                    pull_request=PR_NUMBER,
                    operation_id="op-pause-mid-batch",
                    run_id=710,
                    run_attempt=1,
                    writer_epoch=1,
                    pr_head_sha=HEAD_A,
                    verified_by_broker=True,
                ),
                event=GitHubEventKind.PULL_REQUEST,
                visibility=RepositoryVisibility.PUBLIC,
                granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
                expected_app_id=APP_ID,
                expected_bot_login=BOT_LOGIN,
                expected_bot_user_id=BOT_USER_ID,
                slot_key=SLOT_B,
                generation=1,
                owner_marker=owner_mid,
                fresh_analysis_context_loader=_make_two_batch_ctx,
            )
        )
        self.assertIsInstance(res_mid, RecoveryPaused)
        assert isinstance(res_mid, RecoveryPaused)
        self.assertEqual(res_mid.model_calls_started, 1)
        self.assertEqual(res_mid.operation_model_calls_total, 1)

    def test_corrupt_app_batch_dispatch_marker_halts_fail_closed_zero_calls_zero_writes(
        self,
    ) -> None:
        config = default_configuration()
        policy_digest, config_digest = compute_trusted_policy_and_config_digests(
            config, provider_name="openai-compatible", model_name="gpt-4o-mini"
        )
        pr = _SimulatedPRSurface(head_sha=HEAD_A)
        owner_rec = _make_owner_record(slot_key=SLOT_B, generation=1, lease_epoch=1)
        pr.add_comment(format_v6_record_marker(owner_rec), is_app=True)
        # Ordinary human/App prose, non-App malformed marker, and valid effect marker must not disrupt.
        pr.add_comment(
            'Human note: <!-- reviewsensei:v6:record sha256=bad payload={"truncated":true',
            is_app=False,
        )
        pr.add_comment(
            "App summary prose.\n"
            f"<!-- reviewsensei:v6:effect op=op-prior sha256={'e' * 64} -->",
            is_app=True,
        )

        diff_a = (
            "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n"
            "@@ -1 +1 @@\n-a = 1\n+a = 2\n"
        )
        diff_b = (
            "diff --git a/src/b.py b/src/b.py\n--- a/src/b.py\n+++ b/src/b.py\n"
            "@@ -1 +1 @@\n-b = 1\n+b = 2\n"
        )
        hunk_a = canonical_sha256({"path": "src/a.py", "idx": 0})[:16]
        hunk_b = canonical_sha256({"path": "src/b.py", "idx": 1})[:16]
        b0 = V6PlannedBatch(
            batch_id="batch-000",
            diff_text=diff_a,
            diff_sha256=canonical_sha256(diff_a),
            prompt_bytes=512,
            paths=("src/a.py",),
            primary_hunk_ids=(hunk_a,),
            parent_hunk_ids=(hunk_a,),
        )
        b1 = V6PlannedBatch(
            batch_id="batch-001",
            diff_text=diff_b,
            diff_sha256=canonical_sha256(diff_b),
            prompt_bytes=512,
            paths=("src/b.py",),
            primary_hunk_ids=(hunk_b,),
            parent_hunk_ids=(hunk_b,),
        )
        inv_sha = canonical_sha256(["src/a.py", "src/b.py"])
        in_dig = canonical_sha256(
            {
                "base_sha": BASE_SHA,
                "merge_base_sha": BASE_SHA,
                "head_sha": HEAD_A,
                "inventory_sha256": inv_sha,
                "policy_digest": policy_digest,
                "configuration_digest": config_digest,
            }
        )[:12]
        plan_rec = BatchPlanRecord(
            record_id=f"batch-plan:op-batch-corrupt:{HEAD_A[:12]}:{in_dig}",
            operation_id="op-batch-corrupt",
            base_sha=BASE_SHA,
            merge_base_sha=BASE_SHA,
            pr_head_sha=HEAD_A,
            inventory_sha256=inv_sha,
            policy_digest=policy_digest,
            configuration_digest=config_digest,
            max_provider_calls=4,
            batches=tuple(b.to_plan_dict() for b in (b0, b1)),
            non_text_changes=(),
        )
        plan_sha = canonical_sha256(record_to_dict(plan_rec))
        pr.add_comment(format_v6_record_marker(plan_rec), is_app=True)
        pr.add_comment(
            format_v6_record_marker(
                BatchDispatchRecord(
                    record_id=f"batch-dispatch:op-batch-corrupt:{HEAD_A[:12]}:{in_dig}:batch-000:1",
                    operation_id="op-batch-corrupt",
                    batch_id="batch-000",
                    attempt=1,
                    plan_sha256=plan_sha,
                    prompt_sha256=canonical_sha256(b0.to_plan_dict()),
                    generation=1,
                )
            ),
            is_app=True,
        )
        pr.add_comment(
            format_v6_record_marker(
                BatchOutputRecord(
                    record_id=f"batch-output:op-batch-corrupt:{HEAD_A[:12]}:{in_dig}:batch-000:1",
                    operation_id="op-batch-corrupt",
                    batch_id="batch-000",
                    plan_sha256=plan_sha,
                    hunk_ids_reviewed=(hunk_a,),
                    findings=(),
                    summary="ok",
                    provider="openai-compatible",
                    model="gpt-4o-mini",
                    review_status="complete",
                    attempt=1,
                )
            ),
            is_app=True,
        )
        # Corrupt App-authored BatchDispatchRecord for batch-001 (tampered/corrupted digest).
        corrupt_dispatch = format_v6_record_marker(
            BatchDispatchRecord(
                record_id=f"batch-dispatch:op-batch-corrupt:{HEAD_A[:12]}:{in_dig}:batch-001:1",
                operation_id="op-batch-corrupt",
                batch_id="batch-001",
                attempt=1,
                plan_sha256=plan_sha,
                prompt_sha256=canonical_sha256(b1.to_plan_dict()),
                generation=1,
            )
        ).replace('"batch-001"', '"batch-001-corrupted"')
        pr.add_comment(corrupt_dispatch, is_app=True)

        calls_made: list[str] = []

        class _Svc:
            def review(self, req: Any) -> ReviewResult:
                calls_made.append(req.diff)
                raise AssertionError(
                    "must not invoke model when App dispatch marker is corrupt"
                )

        ctx = V6FreshAnalysisContext(
            owner=OWNER,
            repo=REPO,
            pull_request=PR_NUMBER,
            pr_title="Corrupt dispatch PR",
            base_sha=BASE_SHA,
            merge_base_sha=BASE_SHA,
            head_sha=HEAD_A,
            inventory_sha256=inv_sha,
            inventory=(
                {
                    "path": "src/a.py",
                    "status": "modified",
                    "disposition": "text",
                    "hunk_ids": [hunk_a],
                },
                {
                    "path": "src/b.py",
                    "status": "modified",
                    "disposition": "text",
                    "hunk_ids": [hunk_b],
                },
            ),
            non_text_changes=(),
            policy_digest=policy_digest,
            configuration_digest=config_digest,
            max_provider_calls=4,
            planned_batches=(b0, b1),
            all_expected_hunk_ids=(hunk_a, hunk_b),
            sub_hunks_by_parent={},
            combined_diff_sha256=canonical_sha256(diff_a + diff_b),
            provider_name="openai-compatible",
            model_name="gpt-4o-mini",
            service=_Svc(),
        )
        app_transport = _ScriptedTransport(pr.handle_app)
        eng = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=_ScriptedTransport(pr.handle_wf),
                broker_app_transport=app_transport,
            ),
            now_fn=lambda: 1_700_000_000.0,
            sleeper=lambda _s: None,
            broker_fence_verifier=_fence_verifier,
            mutation_bound=MutationSanityBound(max_writes=32),
        )
        req = RecoveryRequest(
            owner=OWNER,
            repo=REPO,
            repository_id=REPO_ID,
            pull_request=PR_NUMBER,
            operation_id="op-batch-corrupt",
            ownership_proof=WriterOwnershipProof(
                repository_id=REPO_ID,
                pull_request=PR_NUMBER,
                operation_id="op-batch-corrupt",
                run_id=100,
                run_attempt=1,
                writer_epoch=1,
                pr_head_sha=HEAD_A,
                verified_by_broker=True,
            ),
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            expected_app_id=APP_ID,
            expected_bot_login=BOT_LOGIN,
            expected_bot_user_id=BOT_USER_ID,
            slot_key=SLOT_B,
            generation=1,
            owner_marker=owner_rec,
            emit_durable_hold_on_pr=True,
            fresh_analysis_context_loader=lambda: ctx,
        )
        hold = eng.recover_and_publish(req)
        self.assertIsInstance(hold, RecoveryHold)
        assert isinstance(hold, RecoveryHold)
        self.assertEqual(hold.blocker, "corrupted_app_ledger_marker")
        self.assertTrue(hold.zero_model_calls_started)
        self.assertEqual(calls_made, [])
        self.assertEqual(app_transport.calls, [])

        takeover = eng.reconcile_takeover_read_only(
            owner=OWNER,
            repo=REPO,
            repository_id=REPO_ID,
            pull_request=PR_NUMBER,
            slot_key=SLOT_B,
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            expected_app_id=APP_ID,
            expected_bot_login=BOT_LOGIN,
            expected_bot_user_id=BOT_USER_ID,
        )
        self.assertIsInstance(takeover, TakeoverReconciliationResult)
        self.assertEqual(takeover.reconciliation_outcome, "ambiguous_hold")
        self.assertEqual(takeover.blocker, "corrupted_app_ledger_marker")
        self.assertEqual(app_transport.calls, [])

    def test_detail_comment_authority_recheck_blocks_retry_after_throttle_sleep(
        self,
    ) -> None:
        pr = _SimulatedPRSurface(head_sha=HEAD_A)
        policy_digest, config_digest = compute_trusted_policy_and_config_digests(
            default_configuration(),
            provider_name="openai-compatible",
            model_name="gpt-4o-mini",
        )
        op_id = "op-detail-recheck"
        owner_rec = _make_owner_record(
            operation_id=op_id, pr_head_sha=HEAD_A, slot_key=SLOT_B
        )
        pr.add_comment(format_v6_record_marker(owner_rec), is_app=True)
        findings = tuple(
            {
                "path": f"src/f{i}.py",
                "line": 1,
                "side": "RIGHT",
                "body": f"Finding body number {i}",
            }
            for i in range(55)
        )
        in_dig = canonical_sha256(
            {
                "base_sha": BASE_SHA,
                "merge_base_sha": None,
                "head_sha": HEAD_A,
                "inventory_sha256": None,
                "policy_digest": policy_digest,
                "configuration_digest": config_digest,
            }
        )[:12]
        unres = UnresolvedResultRecord(
            record_id=f"result:{op_id}:{HEAD_A[:12]}:{in_dig}",
            operation_id=op_id,
            pr_head_sha=HEAD_A,
            payload_sha256=canonical_sha256(findings),
            findings_payload=findings,
            required_evidence_digests=(
                f"v6:base:{BASE_SHA}",
                f"v6:policy:{policy_digest}",
                f"v6:config:{config_digest}",
            ),
        )
        pr.add_comment(format_v6_record_marker(unres), is_app=True)

        state = {"head": HEAD_A, "detail_posts": 0}

        def app_handler(call: dict[str, Any]) -> tuple[int, Mapping[str, str], Any]:
            if (
                call["method"] == "POST"
                and "/issues/" in call["path"]
                and "reviewsensei:v6:record" not in call["body"]["body"]
            ):
                state["detail_posts"] += 1
                if state["detail_posts"] == 1:
                    return (
                        429,
                        {"Retry-After": "5", "X-RateLimit-Remaining": "0"},
                        {"message": "secondary rate limit"},
                    )
                raise AssertionError("must not emit second detail comment POST")
            return pr.handle_app(call)

        eng = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=_ScriptedTransport(pr.handle_wf),
                broker_app_transport=_ScriptedTransport(app_handler),
            ),
            now_fn=lambda: 1_700_000_000.0,
            sleeper=lambda _s: state.__setitem__("head", "c" * 40),
            broker_fence_verifier=lambda _p: {
                "repository_id": REPO_ID,
                "pull_request": PR_NUMBER,
                "pr_head_sha": state["head"],
                "active_writer_epoch": 1,
                "pr_state": "open",
            },
            mutation_bound=MutationSanityBound(max_writes=64),
        )
        req = RecoveryRequest(
            owner=OWNER,
            repo=REPO,
            repository_id=REPO_ID,
            pull_request=PR_NUMBER,
            operation_id=op_id,
            ownership_proof=WriterOwnershipProof(
                repository_id=REPO_ID,
                pull_request=PR_NUMBER,
                operation_id=op_id,
                run_id=100,
                run_attempt=1,
                writer_epoch=1,
                pr_head_sha=HEAD_A,
                verified_by_broker=True,
            ),
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            expected_app_id=APP_ID,
            expected_bot_login=BOT_LOGIN,
            expected_bot_user_id=BOT_USER_ID,
            slot_key=SLOT_B,
            generation=1,
            owner_marker=owner_rec,
            base_sha=BASE_SHA,
            policy_digest=policy_digest,
            configuration_digest=config_digest,
        )
        with self.assertRaises(StaleWriterOwnershipError):
            eng.recover_and_publish(req)
        self.assertEqual(state["detail_posts"], 1)

    def test_audit_regressions_unknown_calls_hold_accounting_blocker_and_config(
        self,
    ) -> None:
        from types import SimpleNamespace

        from review_sensei_github.v6_fresh_analysis import execute_v6_batch
        from review_sensei_github.v6_pilot_runner import (
            _build_fresh_analysis_context_loader,
        )

        from review_sensei.errors import ReviewInputError

        config = default_configuration()
        policy_digest, config_digest = compute_trusted_policy_and_config_digests(
            config, provider_name="openai-compatible", model_name="gpt-4o-mini"
        )
        diff_a = (
            "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n"
            "@@ -1 +1 @@\n-a = 1\n+a = 2\n"
        )
        hunk_a = canonical_sha256({"path": "src/a.py", "idx": 0})[:16]
        b0 = V6PlannedBatch(
            batch_id="batch-000",
            diff_text=diff_a,
            diff_sha256=canonical_sha256(diff_a),
            prompt_bytes=256,
            paths=("src/a.py",),
            primary_hunk_ids=(hunk_a,),
            parent_hunk_ids=(hunk_a,),
        )
        inv = (
            {
                "path": "src/a.py",
                "status": "modified",
                "disposition": "text",
                "hunk_ids": [hunk_a],
            },
        )
        inv_sha = canonical_sha256(inv)

        def _make_ctx(svc: Any, *, max_calls: int = 4) -> V6FreshAnalysisContext:
            return V6FreshAnalysisContext(
                owner=OWNER,
                repo=REPO,
                pull_request=PR_NUMBER,
                pr_title="Audit PR",
                base_sha=BASE_SHA,
                merge_base_sha=BASE_SHA,
                head_sha=HEAD_A,
                inventory_sha256=inv_sha,
                inventory=inv,
                non_text_changes=(),
                policy_digest=policy_digest,
                configuration_digest=config_digest,
                max_provider_calls=max_calls,
                planned_batches=(b0,),
                all_expected_hunk_ids=(hunk_a,),
                sub_hunks_by_parent={},
                combined_diff_sha256=canonical_sha256(diff_a),
                provider_name="openai-compatible",
                model_name="gpt-4o-mini",
                service=svc,
            )

        # 1. RS-B9480C: non-error outcome missing integer provider_calls must fail closed
        # with provider_calls=None (never fabricate 1).
        missing_calls_svc = SimpleNamespace(
            run=lambda _req, budget=None: SimpleNamespace(
                outcome=SimpleNamespace(status="complete", provider_calls=None),
                result=ReviewResult(
                    summary="OK",
                    comments=(),
                    provider="openai-compatible",
                    model="gpt-4o-mini",
                    review_status="complete",
                ),
            )
        )
        with self.assertRaises(V6FreshAnalysisError) as unk_exc:
            execute_v6_batch(
                _make_ctx(missing_calls_svc), b0, remaining_provider_calls=4
            )
        self.assertEqual(
            unk_exc.exception.diagnostic, "v6_batch_provider_calls_unknown"
        )
        self.assertIsNone(unk_exc.exception.provider_calls)

        pr_unk = _SimulatedPRSurface(head_sha=HEAD_A)
        coord_unk = _SimulatedCoordinatorStore(clock=lambda: 1_700_000_000.0, pr=pr_unk)
        with tempfile.TemporaryDirectory() as tmpdir_s:
            out_unk = V6PilotRunner(
                coordinator=V6CoordinatorClient(
                    base_url="https://coordinator.invalid",
                    oidc_token_provider=lambda: "oidc-jwt-token",
                    opener=coord_unk.opener,
                    clock=lambda: 1_700_000_000.0,
                ),
                workflow_token_transport=_ScriptedTransport(pr_unk.handle_wf),
                app_transport_factory=lambda _g: _ScriptedTransport(pr_unk.handle_app),
                clock=lambda: 1_700_000_000.0,
                sleeper=lambda _s: None,
                broker_fence_verifier=_fence_verifier,
            ).run(
                V6PilotRunConfig(
                    owner=OWNER,
                    repo=REPO,
                    repository_id=REPO_ID,
                    pull_request=PR_NUMBER,
                    head_sha=HEAD_A,
                    operation_id="op-missing-calls-unknown",
                    workflow_sha=WORKFLOW_SHA,
                    expected_app_id=APP_ID,
                    expected_bot_login=BOT_LOGIN,
                    expected_bot_user_id=BOT_USER_ID,
                    policy_digest=policy_digest,
                    configuration_digest=config_digest,
                    outcome_path=Path(tmpdir_s) / "unknown_calls.json",
                    fresh_analysis_context_loader=lambda: _make_ctx(missing_calls_svc),
                )
            )
            self.assertEqual(out_unk.status, "hold")
            self.assertEqual(out_unk.diagnostic, "v6_batch_provider_calls_unknown")
            self.assertEqual(out_unk.model_calls_started, "unknown")
            self.assertEqual(out_unk.operation_model_calls_total, "unknown")

        # 2. RS-A0032A: fresh_analysis_context_loader cannot be combined with initial_result_loader.
        with self.assertRaises(ReviewInputError):
            _build_fresh_analysis_context_loader(
                V6PilotRunConfig(
                    owner=OWNER,
                    repo=REPO,
                    repository_id=REPO_ID,
                    pull_request=PR_NUMBER,
                    head_sha=HEAD_A,
                    operation_id="op-conflict-cfg",
                    workflow_sha=WORKFLOW_SHA,
                    expected_app_id=APP_ID,
                    expected_bot_login=BOT_LOGIN,
                    expected_bot_user_id=BOT_USER_ID,
                    initial_result_loader=lambda: [],
                    fresh_analysis_context_loader=lambda: _make_ctx(missing_calls_svc),
                )
            )

        # 3. RS-51613A adjacent: unexpected exception + rate-limited _finalize_hold still
        # yields RecoveryHold with model_calls_started=None / operation_model_calls_unknown=True.
        pr_exc = _SimulatedPRSurface(head_sha=HEAD_A)
        owner_exc = _make_owner_record(
            operation_id="op-exc-hold-throttle", pr_head_sha=HEAD_A, slot_key=SLOT_B
        )
        pr_exc.add_comment(format_v6_record_marker(owner_exc), is_app=True)

        def _boom(_req: Any, budget: Any = None) -> Any:
            raise RuntimeError("unexpected transport crash mid-call")

        def _throttle_hold(call: dict[str, Any]) -> tuple[int, Mapping[str, str], Any]:
            if (
                call["method"] == "POST"
                and "/issues/" in str(call["path"])
                and "hold:v1" in str((call.get("body") or {}).get("body", ""))
            ):
                return (
                    429,
                    {"Retry-After": "3600"},
                    {"message": "rate limit on durable hold"},
                )
            return pr_exc.handle_app(call)

        eng_exc = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=_ScriptedTransport(pr_exc.handle_wf),
                broker_app_transport=_ScriptedTransport(_throttle_hold),
            ),
            now_fn=lambda: 1_700_000_000.0,
            sleeper=lambda _s: None,
            broker_fence_verifier=_fence_verifier,
            mutation_bound=MutationSanityBound(max_writes=32),
        )
        hold_exc = eng_exc.recover_and_publish(
            RecoveryRequest(
                owner=OWNER,
                repo=REPO,
                repository_id=REPO_ID,
                pull_request=PR_NUMBER,
                operation_id="op-exc-hold-throttle",
                ownership_proof=WriterOwnershipProof(
                    repository_id=REPO_ID,
                    pull_request=PR_NUMBER,
                    operation_id="op-exc-hold-throttle",
                    run_id=101,
                    run_attempt=1,
                    writer_epoch=1,
                    pr_head_sha=HEAD_A,
                    verified_by_broker=True,
                ),
                event=GitHubEventKind.PULL_REQUEST,
                visibility=RepositoryVisibility.PUBLIC,
                granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
                expected_app_id=APP_ID,
                expected_bot_login=BOT_LOGIN,
                expected_bot_user_id=BOT_USER_ID,
                slot_key=SLOT_B,
                generation=1,
                owner_marker=owner_exc,
                emit_durable_hold_on_pr=True,
                fresh_analysis_context_loader=lambda: _make_ctx(
                    SimpleNamespace(run=_boom)
                ),
            )
        )
        self.assertIsInstance(hold_exc, RecoveryHold)
        assert isinstance(hold_exc, RecoveryHold)
        self.assertEqual(hold_exc.blocker, "ambiguous_batch_inference_outcome")
        self.assertIsNone(hold_exc.model_calls_started)
        self.assertTrue(hold_exc.operation_model_calls_unknown)

        # 4. RS-F25457: free-form BatchAttemptRecord.diagnostic normalizes blocker code
        # while keeping the diagnostic detail in missing_evidence.
        pr_diag = _SimulatedPRSurface(head_sha=HEAD_A)
        owner_diag = _make_owner_record(
            operation_id="op-freeform-diag", pr_head_sha=HEAD_A, slot_key=SLOT_B
        )
        pr_diag.add_comment(format_v6_record_marker(owner_diag), is_app=True)
        in_dig = canonical_sha256(
            {
                "base_sha": BASE_SHA,
                "merge_base_sha": BASE_SHA,
                "head_sha": HEAD_A,
                "inventory_sha256": inv_sha,
                "policy_digest": policy_digest,
                "configuration_digest": config_digest,
            }
        )[:12]
        plan_diag = BatchPlanRecord(
            record_id=f"batch-plan:op-freeform-diag:{HEAD_A[:12]}:{in_dig}",
            operation_id="op-freeform-diag",
            base_sha=BASE_SHA,
            merge_base_sha=BASE_SHA,
            pr_head_sha=HEAD_A,
            inventory_sha256=inv_sha,
            policy_digest=policy_digest,
            configuration_digest=config_digest,
            max_provider_calls=4,
            batches=(b0.to_plan_dict(),),
            non_text_changes=(),
        )
        plan_diag_sha = canonical_sha256(record_to_dict(plan_diag))
        pr_diag.add_comment(format_v6_record_marker(plan_diag), is_app=True)
        pr_diag.add_comment(
            format_v6_record_marker(
                BatchAttemptRecord(
                    record_id=f"batch-attempt:op-freeform-diag:{HEAD_A[:12]}:{in_dig}:batch-000:1",
                    operation_id="op-freeform-diag",
                    batch_id="batch-000",
                    attempt=1,
                    plan_sha256=plan_diag_sha,
                    status="failed",
                    provider_calls=2,
                    diagnostic="Upstream gateway returned HTTP 502 Bad Gateway!",
                )
            ),
            is_app=True,
        )
        eng_diag = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=_ScriptedTransport(pr_diag.handle_wf),
                broker_app_transport=_ScriptedTransport(pr_diag.handle_app),
            ),
            now_fn=lambda: 1_700_000_000.0,
            sleeper=lambda _s: None,
            broker_fence_verifier=_fence_verifier,
            mutation_bound=MutationSanityBound(max_writes=32),
        )
        hold_diag = eng_diag.recover_and_publish(
            RecoveryRequest(
                owner=OWNER,
                repo=REPO,
                repository_id=REPO_ID,
                pull_request=PR_NUMBER,
                operation_id="op-freeform-diag",
                ownership_proof=WriterOwnershipProof(
                    repository_id=REPO_ID,
                    pull_request=PR_NUMBER,
                    operation_id="op-freeform-diag",
                    run_id=102,
                    run_attempt=1,
                    writer_epoch=1,
                    pr_head_sha=HEAD_A,
                    verified_by_broker=True,
                ),
                event=GitHubEventKind.PULL_REQUEST,
                visibility=RepositoryVisibility.PUBLIC,
                granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
                expected_app_id=APP_ID,
                expected_bot_login=BOT_LOGIN,
                expected_bot_user_id=BOT_USER_ID,
                slot_key=SLOT_B,
                generation=1,
                owner_marker=owner_diag,
                fresh_analysis_context_loader=lambda: _make_ctx(missing_calls_svc),
            )
        )
        self.assertIsInstance(hold_diag, RecoveryHold)
        assert isinstance(hold_diag, RecoveryHold)
        self.assertEqual(hold_diag.blocker, "v6_batch_inference_failed")
        self.assertEqual(hold_diag.operation_model_calls_total, 2)
        self.assertIn(
            "diagnostic:Upstream gateway returned HTTP 502 Bad Gateway!",
            hold_diag.missing_evidence,
        )

        # 5. RS-12E4AD: RecoveryHold after takeover (pre-batch context error) preserves prior
        # operation_model_calls_total instead of falling back to 0.
        pr_hold_tk = _SimulatedPRSurface(head_sha=HEAD_A)
        coord_hold_tk = _SimulatedCoordinatorStore(
            clock=lambda: 1_700_000_000.0, pr=pr_hold_tk
        )
        old_owner_ht = _make_owner_record(
            operation_id="op-hold-takeover-calls",
            slot_key=coord_hold_tk.slot_key,
            generation=1,
            lease_epoch=1,
            run_id="6001",
            run_attempt=1,
            pr_head_sha=HEAD_A,
        )
        old_owner_ht_cid = int(
            pr_hold_tk.add_comment(format_v6_record_marker(old_owner_ht), is_app=True)[
                "id"
            ]
        )
        for rec in (
            BatchPlanRecord(
                record_id=f"batch-plan:op-hold-takeover-calls:{HEAD_A[:12]}:{in_dig}",
                operation_id="op-hold-takeover-calls",
                base_sha=BASE_SHA,
                merge_base_sha=BASE_SHA,
                pr_head_sha=HEAD_A,
                inventory_sha256=inv_sha,
                policy_digest=policy_digest,
                configuration_digest=config_digest,
                max_provider_calls=4,
                batches=(b0.to_plan_dict(),),
                non_text_changes=(),
            ),
            BatchDispatchRecord(
                record_id=f"batch-dispatch:op-hold-takeover-calls:{HEAD_A[:12]}:{in_dig}:batch-000:1",
                operation_id="op-hold-takeover-calls",
                batch_id="batch-000",
                attempt=1,
                plan_sha256=canonical_sha256(
                    record_to_dict(
                        BatchPlanRecord(
                            record_id=f"batch-plan:op-hold-takeover-calls:{HEAD_A[:12]}:{in_dig}",
                            operation_id="op-hold-takeover-calls",
                            base_sha=BASE_SHA,
                            merge_base_sha=BASE_SHA,
                            pr_head_sha=HEAD_A,
                            inventory_sha256=inv_sha,
                            policy_digest=policy_digest,
                            configuration_digest=config_digest,
                            max_provider_calls=4,
                            batches=(b0.to_plan_dict(),),
                            non_text_changes=(),
                        )
                    )
                ),
                prompt_sha256=canonical_sha256(b0.to_plan_dict()),
                generation=1,
            ),
            BatchOutputRecord(
                record_id=f"batch-output:op-hold-takeover-calls:{HEAD_A[:12]}:{in_dig}:batch-000:1",
                operation_id="op-hold-takeover-calls",
                batch_id="batch-000",
                plan_sha256=canonical_sha256(
                    record_to_dict(
                        BatchPlanRecord(
                            record_id=f"batch-plan:op-hold-takeover-calls:{HEAD_A[:12]}:{in_dig}",
                            operation_id="op-hold-takeover-calls",
                            base_sha=BASE_SHA,
                            merge_base_sha=BASE_SHA,
                            pr_head_sha=HEAD_A,
                            inventory_sha256=inv_sha,
                            policy_digest=policy_digest,
                            configuration_digest=config_digest,
                            max_provider_calls=4,
                            batches=(b0.to_plan_dict(),),
                            non_text_changes=(),
                        )
                    )
                ),
                hunk_ids_reviewed=(hunk_a,),
                findings=(),
                summary="Done prior",
                provider="openai-compatible",
                model="gpt-4o-mini",
                review_status="complete",
                attempt=1,
                provider_calls=2,
            ),
        ):
            pr_hold_tk.add_comment(format_v6_record_marker(rec), is_app=True)
        coord_hold_tk.state = "ACTIVE"
        coord_hold_tk.generation = 1
        coord_hold_tk.lease_epoch = 1
        coord_hold_tk.reservation_id = old_owner_ht.reservation_id
        coord_hold_tk.owner_run_id = "6001"
        coord_hold_tk.owner_run_attempt = 1
        coord_hold_tk.owner_head_sha = HEAD_A
        coord_hold_tk.owner_comment_id = old_owner_ht_cid
        coord_hold_tk.acquired_at_ms = 1_699_999_000_000
        coord_hold_tk.token_expires_at_ms = 1_700_000_600_000
        coord_hold_tk.token_cleared = False
        coord_hold_tk.predecessor_terminal = True

        def _pre_batch_fail() -> V6FreshAnalysisContext:
            raise V6FreshAnalysisError("v6_diff_budget_exceeded", "diff fetch blocked")

        out_hold_tk = V6PilotRunner(
            coordinator=V6CoordinatorClient(
                base_url="https://coordinator.invalid",
                oidc_token_provider=lambda: "oidc-jwt-token",
                opener=coord_hold_tk.opener,
                clock=lambda: 1_700_000_000.0,
            ),
            workflow_token_transport=_ScriptedTransport(pr_hold_tk.handle_wf),
            app_transport_factory=lambda _g: _ScriptedTransport(pr_hold_tk.handle_app),
            clock=lambda: 1_700_000_000.0,
            sleeper=lambda _s: None,
            broker_fence_verifier=_fence_verifier,
        ).run(
            V6PilotRunConfig(
                owner=OWNER,
                repo=REPO,
                repository_id=REPO_ID,
                pull_request=PR_NUMBER,
                head_sha=HEAD_A,
                operation_id="op-hold-takeover-calls",
                run_id=6001,
                run_attempt=2,
                workflow_sha=WORKFLOW_SHA,
                expected_app_id=APP_ID,
                expected_bot_login=BOT_LOGIN,
                expected_bot_user_id=BOT_USER_ID,
                policy_digest=policy_digest,
                configuration_digest=config_digest,
                fresh_analysis_context_loader=_pre_batch_fail,
            )
        )
        self.assertEqual(out_hold_tk.status, "hold")
        self.assertEqual(out_hold_tk.model_calls_started, 0)
        self.assertEqual(out_hold_tk.operation_model_calls_total, 2)

    def test_split_batch_plan_inline_vs_parts_resume_and_fail_closed_corruption(
        self,
    ) -> None:
        from review_sensei.configuration import default_configuration
        from review_sensei.models import ReviewComment, ReviewResult

        policy_digest, config_digest = compute_trusted_policy_and_config_digests(
            default_configuration(),
            provider_name="openai-compatible",
            model_name="gpt-4o-mini",
        )

        # 1. Small plan stays inline BatchPlanRecord (<= MAX_MARKER_ENCODED_BYTES)
        small_hunk = "src/small.py#h0:1,1+1,1:" + ("a" * 16)
        small_diff = (
            "diff --git a/src/small.py b/src/small.py\n"
            "--- a/src/small.py\n+++ b/src/small.py\n@@ -1 +1 @@\n-1\n+2\n"
        )
        small_batch = V6PlannedBatch(
            batch_id="batch-000",
            diff_text=small_diff,
            diff_sha256=canonical_sha256(small_diff),
            prompt_bytes=200,
            paths=("src/small.py",),
            primary_hunk_ids=(small_hunk,),
            parent_hunk_ids=(small_hunk,),
        )
        small_inv = (
            {
                "path": "src/small.py",
                "status": "modified",
                "disposition": "text",
                "hunk_ids": [small_hunk],
            },
        )

        class _SimpleSvc:
            def __init__(self, on_call: Callable[[str], None] | None = None) -> None:
                self.on_call = on_call

            def review(self, req: Any) -> ReviewResult:
                if self.on_call is not None:
                    self.on_call(req.diff)
                return ReviewResult(
                    summary="OK",
                    comments=(),
                    provider="openai-compatible",
                    model="gpt-4o-mini",
                    review_status="complete",
                )

        small_ctx = V6FreshAnalysisContext(
            owner=OWNER,
            repo=REPO,
            pull_request=PR_NUMBER,
            pr_title="Small plan",
            base_sha=BASE_SHA,
            merge_base_sha=BASE_SHA,
            head_sha=HEAD_A,
            inventory_sha256=canonical_sha256(small_inv),
            inventory=small_inv,
            non_text_changes=(),
            policy_digest=policy_digest,
            configuration_digest=config_digest,
            max_provider_calls=4,
            planned_batches=(small_batch,),
            all_expected_hunk_ids=(small_hunk,),
            sub_hunks_by_parent={},
            combined_diff_sha256=canonical_sha256(small_diff),
            provider_name="openai-compatible",
            model_name="gpt-4o-mini",
            service=_SimpleSvc(),
        )
        pr_small = _SimulatedPRSurface(head_sha=HEAD_A)
        owner_small = _make_owner_record(
            operation_id="op-small-plan", pr_head_sha=HEAD_A, slot_key=SLOT_B
        )
        pr_small.add_comment(format_v6_record_marker(owner_small), is_app=True)
        req_small = RecoveryRequest(
            owner=OWNER,
            repo=REPO,
            repository_id=REPO_ID,
            pull_request=PR_NUMBER,
            operation_id="op-small-plan",
            ownership_proof=WriterOwnershipProof(
                repository_id=REPO_ID,
                pull_request=PR_NUMBER,
                operation_id="op-small-plan",
                run_id=701,
                run_attempt=1,
                writer_epoch=1,
                pr_head_sha=HEAD_A,
                verified_by_broker=True,
            ),
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            expected_app_id=APP_ID,
            expected_bot_login=BOT_LOGIN,
            expected_bot_user_id=BOT_USER_ID,
            slot_key=SLOT_B,
            generation=1,
            owner_marker=owner_small,
            fresh_analysis_context_loader=lambda: small_ctx,
        )
        eng_small = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=_ScriptedTransport(pr_small.handle_wf),
                broker_app_transport=_ScriptedTransport(pr_small.handle_app),
            ),
            now_fn=lambda: 1_700_000_000.0,
            sleeper=lambda _s: None,
            broker_fence_verifier=_fence_verifier,
            mutation_bound=MutationSanityBound(max_writes=32),
        )
        res_small = eng_small.recover_and_publish(req_small)
        self.assertIsInstance(res_small, RecoveryCompleted)
        small_recs = [
            r
            for c in pr_small.comments
            for r in extract_v6_record_markers(c["body"], strict=True)
        ]
        self.assertEqual(
            len([r for r in small_recs if isinstance(r, BatchPlanRecord)]), 1
        )
        self.assertEqual(
            len([r for r in small_recs if isinstance(r, BatchPlanPartRecord)]), 0
        )
        self.assertEqual(
            len([r for r in small_recs if isinstance(r, BatchPlanManifestRecord)]), 0
        )
        for c in pr_small.comments:
            self.assertLessEqual(
                len(c["body"].encode("utf-8")), MAX_MARKER_ENCODED_BYTES
            )

        # 2. Wide plan whose inline BatchPlanRecord > MAX_MARKER_ENCODED_BYTES (with special chars)
        # builds 2 batches with many paths/hunk IDs + angle brackets/quotes so JSON escaping is exercised.
        wide_batches: list[V6PlannedBatch] = []
        wide_inventory: list[dict[str, Any]] = []
        all_wide_hunks: list[str] = []
        for b_idx in range(2):
            hunk_ids = tuple(
                f'src/dir_<"{b_idx}_{h_idx}">/module_{h_idx:03d}.py#h0:1,1+1,1:'
                + hashlib.sha256(f"{b_idx}:{h_idx}".encode()).hexdigest()[:24]
                for h_idx in range(450)
            )
            paths = tuple(
                f'src/dir_<"{b_idx}_{h_idx}">/module_{h_idx:03d}.py'
                for h_idx in range(450)
            )
            diff_txt = (
                f"diff --git a/src/b{b_idx}.py b/src/b{b_idx}.py\n"
                f"--- a/src/b{b_idx}.py\n+++ b/src/b{b_idx}.py\n"
                f"@@ -1 +1 @@\n-v = {b_idx}\n+v = {b_idx + 10}\n"
            )
            wide_batches.append(
                V6PlannedBatch(
                    batch_id=f"batch-{b_idx:03d}",
                    diff_text=diff_txt,
                    diff_sha256=canonical_sha256(diff_txt),
                    prompt_bytes=1024,
                    paths=paths,
                    primary_hunk_ids=hunk_ids,
                    parent_hunk_ids=hunk_ids,
                )
            )
            for p, h in zip(paths, hunk_ids):
                wide_inventory.append(
                    {
                        "path": p,
                        "status": "modified",
                        "disposition": "text",
                        "hunk_ids": [h],
                    }
                )
            all_wide_hunks.extend(hunk_ids)

        wide_inv_sha = canonical_sha256(wide_inventory)
        # Confirm inline BatchPlanRecord indeed exceeds MAX_MARKER_ENCODED_BYTES.
        raw_inline_plan = BatchPlanRecord(
            record_id=f"batch-plan:op-wide:{HEAD_A[:12]}:probe",
            operation_id="op-wide",
            base_sha=BASE_SHA,
            merge_base_sha=BASE_SHA,
            pr_head_sha=HEAD_A,
            inventory_sha256=wide_inv_sha,
            policy_digest=policy_digest,
            configuration_digest=config_digest,
            max_provider_calls=4,
            batches=tuple(b.to_plan_dict() for b in wide_batches),
            non_text_changes=(),
        )
        with self.assertRaises(Exception) as size_err:
            format_v6_record_marker(raw_inline_plan)
        self.assertGreater(
            len(json.dumps(record_to_dict(raw_inline_plan)).encode("utf-8")),
            MAX_MARKER_ENCODED_BYTES,
        )
        del size_err

        model_calls: list[str] = []
        manifest_present_at_first_call: list[bool] = []
        pr_wide = _SimulatedPRSurface(head_sha=HEAD_A)
        owner_wide = _make_owner_record(
            operation_id="op-wide", pr_head_sha=HEAD_A, slot_key=SLOT_B
        )
        pr_wide.add_comment(format_v6_record_marker(owner_wide), is_app=True)

        class _WideSvc:
            def review(self, req: Any) -> ReviewResult:
                if not model_calls:
                    recs_now = [
                        r
                        for c in pr_wide.comments
                        for r in extract_v6_record_markers(c["body"], strict=True)
                    ]
                    manifest_present_at_first_call.append(
                        any(isinstance(r, BatchPlanManifestRecord) for r in recs_now)
                    )
                model_calls.append(req.diff)
                b_tag = "src/b0.py" if "src/b0.py" in req.diff else "src/b1.py"
                return ReviewResult(
                    summary=f"Wide summary {b_tag}",
                    comments=(
                        ReviewComment(
                            path=b_tag, line=1, side="RIGHT", body=f"Note {b_tag}"
                        ),
                    ),
                    provider="openai-compatible",
                    model="gpt-4o-mini",
                    review_status="complete",
                )

        wide_ctx = V6FreshAnalysisContext(
            owner=OWNER,
            repo=REPO,
            pull_request=PR_NUMBER,
            pr_title="Wide plan PR",
            base_sha=BASE_SHA,
            merge_base_sha=BASE_SHA,
            head_sha=HEAD_A,
            inventory_sha256=wide_inv_sha,
            inventory=tuple(wide_inventory),
            non_text_changes=(),
            policy_digest=policy_digest,
            configuration_digest=config_digest,
            max_provider_calls=4,
            planned_batches=tuple(wide_batches),
            all_expected_hunk_ids=tuple(all_wide_hunks),
            sub_hunks_by_parent={},
            combined_diff_sha256=canonical_sha256("wide"),
            provider_name="openai-compatible",
            model_name="gpt-4o-mini",
            service=_WideSvc(),
        )
        req_wide = RecoveryRequest(
            owner=OWNER,
            repo=REPO,
            repository_id=REPO_ID,
            pull_request=PR_NUMBER,
            operation_id="op-wide",
            ownership_proof=WriterOwnershipProof(
                repository_id=REPO_ID,
                pull_request=PR_NUMBER,
                operation_id="op-wide",
                run_id=702,
                run_attempt=1,
                writer_epoch=1,
                pr_head_sha=HEAD_A,
                verified_by_broker=True,
            ),
            event=GitHubEventKind.PULL_REQUEST,
            visibility=RepositoryVisibility.PUBLIC,
            granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            expected_app_id=APP_ID,
            expected_bot_login=BOT_LOGIN,
            expected_bot_user_id=BOT_USER_ID,
            slot_key=SLOT_B,
            generation=1,
            owner_marker=owner_wide,
            fresh_analysis_context_loader=lambda: wide_ctx,
        )

        # 3. Interrupt runner after only part 0 lands -> 0 model calls started!
        parts_posted_run1 = 0

        def _kill_after_part0(
            call: dict[str, Any],
        ) -> tuple[int, Mapping[str, str], Any]:
            nonlocal parts_posted_run1
            if call["method"] == "POST" and "batch-plan-part:v1" in str(
                (call.get("body") or {}).get("body", "")
            ):
                parts_posted_run1 += 1
                if parts_posted_run1 >= 2:
                    raise RuntimeError("simulated runner kill mid-plan-parts")
            return pr_wide.handle_app(call)

        eng_killed = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=_ScriptedTransport(pr_wide.handle_wf),
                broker_app_transport=_ScriptedTransport(_kill_after_part0),
            ),
            now_fn=lambda: 1_700_000_000.0,
            sleeper=lambda _s: None,
            broker_fence_verifier=_fence_verifier,
            mutation_bound=MutationSanityBound(max_writes=64),
        )
        with self.assertRaises(RuntimeError):
            eng_killed.recover_and_publish(req_wide)
        self.assertEqual(len(model_calls), 0)
        recs_after_kill = [
            r
            for c in pr_wide.comments
            for r in extract_v6_record_markers(c["body"], strict=True)
        ]
        parts_after_kill = [
            r for r in recs_after_kill if isinstance(r, BatchPlanPartRecord)
        ]
        self.assertEqual(len(parts_after_kill), 1)
        self.assertGreaterEqual(parts_after_kill[0].total_parts, 2)
        self.assertEqual(
            len([r for r in recs_after_kill if isinstance(r, BatchPlanManifestRecord)]),
            0,
        )

        # 4. Cold successor reuses part 0 without reposting, finishes remaining parts + manifest,
        # verifies full plan BEFORE starting any model call, then executes batches and completes!
        posted_part_bodies: list[str] = []

        def _track_posts(
            call: dict[str, Any],
        ) -> tuple[int, Mapping[str, str], Any]:
            if call["method"] == "POST" and "batch-plan-part:v1" in str(
                (call.get("body") or {}).get("body", "")
            ):
                posted_part_bodies.append(str((call.get("body") or {}).get("body", "")))
            return pr_wide.handle_app(call)

        eng_resume = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=_ScriptedTransport(pr_wide.handle_wf),
                broker_app_transport=_ScriptedTransport(_track_posts),
            ),
            now_fn=lambda: 1_700_000_000.0,
            sleeper=lambda _s: None,
            broker_fence_verifier=_fence_verifier,
            mutation_bound=MutationSanityBound(max_writes=64),
        )
        res_wide = eng_resume.recover_and_publish(req_wide)
        self.assertIsInstance(res_wide, RecoveryCompleted)
        self.assertEqual(manifest_present_at_first_call, [True])
        self.assertEqual(len(model_calls), 2)
        total_parts = parts_after_kill[0].total_parts
        self.assertEqual(len(posted_part_bodies), total_parts - 1)
        for c in pr_wide.comments:
            self.assertLessEqual(
                len(c["body"].encode("utf-8")), MAX_MARKER_ENCODED_BYTES
            )
            # Roundtrip parse check
            for parsed in extract_v6_record_markers(c["body"], strict=True):
                roundtrip = parse_v6_record_marker(format_v6_record_marker(parsed))
                self.assertEqual(roundtrip, parsed)

        final_recs = [
            r
            for c in pr_wide.comments
            for r in extract_v6_record_markers(c["body"], strict=True)
        ]
        final_parts = [r for r in final_recs if isinstance(r, BatchPlanPartRecord)]
        final_manifests = [
            r for r in final_recs if isinstance(r, BatchPlanManifestRecord)
        ]
        self.assertEqual(len(final_parts), total_parts)
        self.assertEqual(len(final_manifests), 1)
        self.assertEqual(
            len([r for r in final_recs if isinstance(r, BatchPlanRecord)]), 0
        )

        # 5. Reconstruct from manifest + parts on a fresh PR surface (with existing outputs) works;
        # missing part under manifest fails closed with 0 model calls.
        model_calls.clear()
        pr_missing_part = _SimulatedPRSurface(head_sha=HEAD_A)
        pr_missing_part.add_comment(format_v6_record_marker(owner_wide), is_app=True)
        for part_rec in final_parts[:-1]:  # drop last part
            pr_missing_part.add_comment(format_v6_record_marker(part_rec), is_app=True)
        pr_missing_part.add_comment(
            format_v6_record_marker(final_manifests[0]), is_app=True
        )
        eng_missing = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=_ScriptedTransport(pr_missing_part.handle_wf),
                broker_app_transport=_ScriptedTransport(pr_missing_part.handle_app),
            ),
            now_fn=lambda: 1_700_000_000.0,
            sleeper=lambda _s: None,
            broker_fence_verifier=_fence_verifier,
            mutation_bound=MutationSanityBound(max_writes=64),
        )
        hold_missing = eng_missing.recover_and_publish(req_wide)
        self.assertIsInstance(hold_missing, RecoveryHold)
        assert isinstance(hold_missing, RecoveryHold)
        self.assertEqual(
            hold_missing.blocker, "incomplete_partitioned_batch_plan_missing_part"
        )
        self.assertIn(
            f"batch_plan_part:{final_parts[-1].record_id}",
            hold_missing.missing_evidence,
        )
        self.assertEqual(len(model_calls), 0)

        # 6. Corrupted part digest under manifest fails closed with 0 model calls.
        pr_corrupt_manifest = _SimulatedPRSurface(head_sha=HEAD_A)
        pr_corrupt_manifest.add_comment(
            format_v6_record_marker(owner_wide), is_app=True
        )
        for idx_p, part_rec in enumerate(final_parts):
            if idx_p == 0:
                tampered_m_payload = {"chunk": "tampered-chunk-payload"}
                tampered = replace(
                    part_rec,
                    part_payload=tampered_m_payload,
                    part_sha256=canonical_sha256(tampered_m_payload),
                )
                pr_corrupt_manifest.add_comment(
                    format_v6_record_marker(tampered), is_app=True
                )
            else:
                pr_corrupt_manifest.add_comment(
                    format_v6_record_marker(part_rec), is_app=True
                )
        pr_corrupt_manifest.add_comment(
            format_v6_record_marker(final_manifests[0]), is_app=True
        )
        eng_corrupt_m = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=_ScriptedTransport(
                    pr_corrupt_manifest.handle_wf
                ),
                broker_app_transport=_ScriptedTransport(pr_corrupt_manifest.handle_app),
            ),
            now_fn=lambda: 1_700_000_000.0,
            sleeper=lambda _s: None,
            broker_fence_verifier=_fence_verifier,
            mutation_bound=MutationSanityBound(max_writes=64),
        )
        hold_corrupt_m = eng_corrupt_m.recover_and_publish(req_wide)
        self.assertIsInstance(hold_corrupt_m, RecoveryHold)
        assert isinstance(hold_corrupt_m, RecoveryHold)
        self.assertEqual(hold_corrupt_m.blocker, "corrupted_pr_batch_plan")
        self.assertEqual(len(model_calls), 0)

        # 7. Conflicting part digest when NO manifest exists yet fails closed with 0 model calls.
        pr_conflict_part = _SimulatedPRSurface(head_sha=HEAD_A)
        pr_conflict_part.add_comment(format_v6_record_marker(owner_wide), is_app=True)
        tampered_pre_payload = {"chunk": "tampered-pre-manifest"}
        tampered_pre = replace(
            final_parts[0],
            part_payload=tampered_pre_payload,
            part_sha256=canonical_sha256(tampered_pre_payload),
        )
        pr_conflict_part.add_comment(format_v6_record_marker(tampered_pre), is_app=True)
        eng_conflict = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=_ScriptedTransport(pr_conflict_part.handle_wf),
                broker_app_transport=_ScriptedTransport(pr_conflict_part.handle_app),
            ),
            now_fn=lambda: 1_700_000_000.0,
            sleeper=lambda _s: None,
            broker_fence_verifier=_fence_verifier,
            mutation_bound=MutationSanityBound(max_writes=64),
        )
        hold_conflict = eng_conflict.recover_and_publish(req_wide)
        self.assertIsInstance(hold_conflict, RecoveryHold)
        assert isinstance(hold_conflict, RecoveryHold)
        self.assertEqual(hold_conflict.blocker, "corrupted_pr_batch_plan_digest")
        self.assertEqual(len(model_calls), 0)

        # 8. Cold recovery with complete BatchPlanManifestRecord + parts + batch-000 output
        # reconstructs BatchPlanRecord from manifest+parts (0 plan writes) and executes only batch-001!
        pr_manifest_resume = _SimulatedPRSurface(head_sha=HEAD_A)
        pr_manifest_resume.add_comment(format_v6_record_marker(owner_wide), is_app=True)
        for part_rec in final_parts:
            pr_manifest_resume.add_comment(
                format_v6_record_marker(part_rec), is_app=True
            )
        pr_manifest_resume.add_comment(
            format_v6_record_marker(final_manifests[0]), is_app=True
        )
        b0_dispatch = next(
            r
            for r in final_recs
            if isinstance(r, BatchDispatchRecord) and r.batch_id == "batch-000"
        )
        b0_output = next(
            r
            for r in final_recs
            if isinstance(r, BatchOutputRecord) and r.batch_id == "batch-000"
        )
        pr_manifest_resume.add_comment(
            format_v6_record_marker(b0_dispatch), is_app=True
        )
        pr_manifest_resume.add_comment(format_v6_record_marker(b0_output), is_app=True)
        model_calls.clear()
        eng_manifest_resume = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=_ScriptedTransport(
                    pr_manifest_resume.handle_wf
                ),
                broker_app_transport=_ScriptedTransport(pr_manifest_resume.handle_app),
            ),
            now_fn=lambda: 1_700_000_000.0,
            sleeper=lambda _s: None,
            broker_fence_verifier=_fence_verifier,
            mutation_bound=MutationSanityBound(max_writes=64),
        )
        res_manifest_resume = eng_manifest_resume.recover_and_publish(req_wide)
        self.assertIsInstance(res_manifest_resume, RecoveryCompleted)
        assert isinstance(res_manifest_resume, RecoveryCompleted)
        self.assertEqual(res_manifest_resume.model_calls_started, 1)
        self.assertEqual(len(model_calls), 1)
        self.assertIn("src/b1.py", model_calls[0])
        # Verify no duplicate plan parts or manifest were posted.
        resumed_recs = [
            r
            for c in pr_manifest_resume.comments
            for r in extract_v6_record_markers(c["body"], strict=True)
        ]
        self.assertEqual(
            len([r for r in resumed_recs if isinstance(r, BatchPlanPartRecord)]),
            total_parts,
        )
        self.assertEqual(
            len([r for r in resumed_recs if isinstance(r, BatchPlanManifestRecord)]), 1
        )

        # 9. Tampered part_payload (even with unchanged part_sha256 header matching manifest)
        # is rejected by BatchPlanPartRecord.__post_init__ AND fails closed with 0 model calls (pre- and post-manifest).
        from review_sensei_github.v6_recovery_ledger import canonical_json_dumps

        from review_sensei.errors import ReviewInputError

        with self.assertRaises(ReviewInputError):
            replace(final_parts[0], part_payload={"chunk": '{"tampered":true}'})

        tampered_raw_dict = record_to_dict(final_parts[0])
        tampered_raw_dict["part_payload"] = {"chunk": '{"tampered":true}'}
        tampered_raw_sha = canonical_sha256(tampered_raw_dict)
        tampered_raw_marker = (
            f"<!-- reviewsensei:v6:record sha256={tampered_raw_sha} "
            f"payload={canonical_json_dumps(tampered_raw_dict)} -->"
        )
        pr_tampered_payload = _SimulatedPRSurface(head_sha=HEAD_A)
        pr_tampered_payload.add_comment(
            format_v6_record_marker(owner_wide), is_app=True
        )
        for idx_p, part_rec in enumerate(final_parts):
            pr_tampered_payload.add_comment(
                tampered_raw_marker
                if idx_p == 0
                else format_v6_record_marker(part_rec),
                is_app=True,
            )
        pr_tampered_payload.add_comment(
            format_v6_record_marker(final_manifests[0]), is_app=True
        )
        model_calls.clear()
        eng_tampered_payload = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=_ScriptedTransport(
                    pr_tampered_payload.handle_wf
                ),
                broker_app_transport=_ScriptedTransport(pr_tampered_payload.handle_app),
            ),
            now_fn=lambda: 1_700_000_000.0,
            sleeper=lambda _s: None,
            broker_fence_verifier=_fence_verifier,
            mutation_bound=MutationSanityBound(max_writes=64),
        )
        hold_tampered_payload = eng_tampered_payload.recover_and_publish(req_wide)
        self.assertIsInstance(hold_tampered_payload, RecoveryHold)
        assert isinstance(hold_tampered_payload, RecoveryHold)
        self.assertEqual(hold_tampered_payload.blocker, "corrupted_app_ledger_marker")
        self.assertEqual(len(model_calls), 0)

        # Pre-manifest reuse path also rejects tampered part_payload at parse time.
        pr_tampered_pre = _SimulatedPRSurface(head_sha=HEAD_A)
        pr_tampered_pre.add_comment(format_v6_record_marker(owner_wide), is_app=True)
        pr_tampered_pre.add_comment(tampered_raw_marker, is_app=True)
        eng_tampered_pre = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=_ScriptedTransport(pr_tampered_pre.handle_wf),
                broker_app_transport=_ScriptedTransport(pr_tampered_pre.handle_app),
            ),
            now_fn=lambda: 1_700_000_000.0,
            sleeper=lambda _s: None,
            broker_fence_verifier=_fence_verifier,
            mutation_bound=MutationSanityBound(max_writes=64),
        )
        hold_tampered_pre = eng_tampered_pre.recover_and_publish(req_wide)
        self.assertIsInstance(hold_tampered_pre, RecoveryHold)
        assert isinstance(hold_tampered_pre, RecoveryHold)
        self.assertEqual(hold_tampered_pre.blocker, "corrupted_app_ledger_marker")
        self.assertEqual(len(model_calls), 0)

        # 10. Conflicting BatchPlanManifestRecord records fail closed with corrupted_pr_batch_plan.
        pr_conflict_manifest = _SimulatedPRSurface(head_sha=HEAD_A)
        pr_conflict_manifest.add_comment(
            format_v6_record_marker(owner_wide), is_app=True
        )
        for part_rec in final_parts:
            pr_conflict_manifest.add_comment(
                format_v6_record_marker(part_rec), is_app=True
            )
        pr_conflict_manifest.add_comment(
            format_v6_record_marker(final_manifests[0]), is_app=True
        )
        pr_conflict_manifest.add_comment(
            format_v6_record_marker(
                replace(
                    final_manifests[0],
                    record_id=f"{final_manifests[0].record_id}:conflict",
                    plan_sha256="f" * 64,
                )
            ),
            is_app=True,
        )
        eng_conflict_manifest = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=_ScriptedTransport(
                    pr_conflict_manifest.handle_wf
                ),
                broker_app_transport=_ScriptedTransport(
                    pr_conflict_manifest.handle_app
                ),
            ),
            now_fn=lambda: 1_700_000_000.0,
            sleeper=lambda _s: None,
            broker_fence_verifier=_fence_verifier,
            mutation_bound=MutationSanityBound(max_writes=64),
        )
        hold_conflict_manifest = eng_conflict_manifest.recover_and_publish(req_wide)
        self.assertIsInstance(hold_conflict_manifest, RecoveryHold)
        assert isinstance(hold_conflict_manifest, RecoveryHold)
        self.assertEqual(hold_conflict_manifest.blocker, "corrupted_pr_batch_plan")
        self.assertEqual(len(model_calls), 0)

        # 11. RS-3D271E: Extra/unexpected BatchPlanPartRecord for same operation fails closed
        # both post-manifest and pre-manifest with corrupted_pr_batch_plan and 0 model calls.
        extra_part = BatchPlanPartRecord(
            record_id=f"{final_parts[0].record_id}:extra-conflicting",
            operation_id=final_parts[0].operation_id,
            pr_head_sha=final_parts[0].pr_head_sha,
            plan_sha256="a" * 64,
            part_index=0,
            total_parts=1,
            part_sha256=canonical_sha256({"chunk": "{}"}),
            part_payload={"chunk": "{}"},
        )
        pr_extra_post = _SimulatedPRSurface(head_sha=HEAD_A)
        pr_extra_post.add_comment(format_v6_record_marker(owner_wide), is_app=True)
        for part_rec in final_parts:
            pr_extra_post.add_comment(format_v6_record_marker(part_rec), is_app=True)
        pr_extra_post.add_comment(format_v6_record_marker(extra_part), is_app=True)
        pr_extra_post.add_comment(
            format_v6_record_marker(final_manifests[0]), is_app=True
        )
        hold_extra_post = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=_ScriptedTransport(pr_extra_post.handle_wf),
                broker_app_transport=_ScriptedTransport(pr_extra_post.handle_app),
            ),
            now_fn=lambda: 1_700_000_000.0,
            sleeper=lambda _s: None,
            broker_fence_verifier=_fence_verifier,
            mutation_bound=MutationSanityBound(max_writes=64),
        ).recover_and_publish(req_wide)
        self.assertIsInstance(hold_extra_post, RecoveryHold)
        assert isinstance(hold_extra_post, RecoveryHold)
        self.assertEqual(hold_extra_post.blocker, "corrupted_pr_batch_plan")
        self.assertEqual(len(model_calls), 0)

        pr_extra_pre = _SimulatedPRSurface(head_sha=HEAD_A)
        pr_extra_pre.add_comment(format_v6_record_marker(owner_wide), is_app=True)
        pr_extra_pre.add_comment(format_v6_record_marker(extra_part), is_app=True)
        hold_extra_pre = V6RecoveryEngine(
            DualCredentialGitHubClient(
                workflow_token_transport=_ScriptedTransport(pr_extra_pre.handle_wf),
                broker_app_transport=_ScriptedTransport(pr_extra_pre.handle_app),
            ),
            now_fn=lambda: 1_700_000_000.0,
            sleeper=lambda _s: None,
            broker_fence_verifier=_fence_verifier,
            mutation_bound=MutationSanityBound(max_writes=64),
        ).recover_and_publish(req_wide)
        self.assertIsInstance(hold_extra_pre, RecoveryHold)
        assert isinstance(hold_extra_pre, RecoveryHold)
        self.assertEqual(hold_extra_pre.blocker, "corrupted_pr_batch_plan")
        self.assertEqual(len(model_calls), 0)

        # 12. RS-4F6981: Split boundary with heavy <, >, quotes, backslashes, and 4-byte UTF-8 emojis.
        from review_sensei_github.v6_recovery_engine import (
            _reconstruct_split_batch_plan,
            _split_batch_plan_into_parts,
        )

        tricky_hunks = tuple(
            f'src/<tag_"{idx}">_\\\\_🚀/file_{idx}.py#h0:1,1+1,1:{idx:016x}'
            for idx in range(520)
        )
        tricky_plan = BatchPlanRecord(
            record_id=f"batch-plan:op-tricky:{HEAD_A[:12]}:probe",
            operation_id="op-tricky",
            base_sha=BASE_SHA,
            merge_base_sha=BASE_SHA,
            pr_head_sha=HEAD_A,
            inventory_sha256="b" * 64,
            policy_digest=policy_digest,
            configuration_digest=config_digest,
            max_provider_calls=4,
            batches=(
                {
                    "batch_id": "batch-000",
                    "paths": [
                        f'src/<tag_"{i}">_\\\\_🚀/file_{i}.py' for i in range(520)
                    ],
                    "primary_hunk_ids": list(tricky_hunks),
                    "diff_sha256": "c" * 64,
                    "prompt_bytes": 1234,
                },
            ),
            non_text_changes=(),
        )
        _t_sha, tricky_parts, tricky_manifest = _split_batch_plan_into_parts(
            tricky_plan, input_digest="tricky123456"
        )
        self.assertGreaterEqual(len(tricky_parts), 2)
        for tp in tricky_parts:
            marker_str = format_v6_record_marker(tp)
            self.assertLessEqual(
                len(marker_str.encode("utf-8")), MAX_MARKER_ENCODED_BYTES
            )
        rebuilt_tricky = _reconstruct_split_batch_plan(
            manifest_rec=tricky_manifest, plan_parts=tricky_parts
        )
        self.assertEqual(rebuilt_tricky, tricky_plan)


if __name__ == "__main__":
    unittest.main()

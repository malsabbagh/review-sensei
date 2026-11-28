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
    RecoveryRequest,
    StaleWriterOwnershipError,
    TakeoverReconciliationResult,
    V6RecoveryEngine,
    _evidence_binding_for_request,
)
from review_sensei_github.v6_recovery_ledger import (
    MAX_MARKER_ENCODED_BYTES,
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


if __name__ == "__main__":
    unittest.main()

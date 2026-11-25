"""Tests for version-only dependency supplemental document scoping (PR 2)."""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from review_sensei_github.publication_context_recovery import (
    _resolve_recovery_inference,
    reconstruct_publication_contexts,
)

from review_sensei import cli
from review_sensei.baseline import ReviewBaseline
from review_sensei.configuration import ConfigurationError, parse_configuration_text
from review_sensei.context import (
    RepositoryContextStore,
    admit_prompt_context,
    build_review_context_cache_key,
    build_review_context_selection,
    context_configuration_digest,
)
from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.coverage import CoverageManifest, FileCoverage, HunkCoverage
from review_sensei.diff import DiffAnalysis, DiffFileRecord, DiffHunk, analyze_diff
from review_sensei.document_context import (
    POLICY,
    VERSION_ONLY_ACTION_POLICY,
    DocumentSelectionReport,
    SupplementalDocumentPolicy,
    classify_supplemental_scope,
)
from review_sensei.models import (
    ProviderResponse,
    ReviewRequest,
    ReviewResult,
    ReviewTransaction,
    build_transaction_configuration_context,
    transaction_stage_identity,
)
from review_sensei.providers.base import ReviewProvider
from review_sensei.providers.fixture import FixtureProvider
from review_sensei.recovery_context import RecoveryRefusalError
from review_sensei.service import (
    DEFAULT_CATEGORY_CATALOG,
    DEFAULT_STAGES,
    ReviewService,
    review_work_policy_digest,
)
from review_sensei.session import (
    LocalSessionLedger,
    SessionIdentity,
    checkpoint_review_analysis,
    complete_review_publication,
    prepare_review_transaction,
    session_reservation_id,
)
from review_sensei.validation import ReviewLimits

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PR497_PATCH = (
    _REPO_ROOT / "tests" / "fixtures" / "regressions" / "pr497-codeql-update.patch"
)
_BASE_SHA = "1" * 40
_HEAD_SHA = "2" * 40
_LEGACY_HEAD_SHA = "3" * 40


def _capture_service_prompt(
    *, diff_text: str, policy: SupplementalDocumentPolicy, root: Path = _REPO_ROOT
) -> tuple[dict[str, object], int, list[str]]:
    captured: list[str] = []

    class _StubProvider(ReviewProvider):
        name = "stub"
        model = "stub-model"

        def complete(self, request):
            captured.append(request.prompt)
            return ProviderResponse(
                provider=self.name,
                raw_text='{"summary": "Clean.", "comments": []}',
                model=self.model,
            )

    analysis = analyze_diff(diff_text)
    service = ReviewService(_StubProvider())
    selection = build_review_context_selection(
        service.review_categories,
        changed_paths=analysis.changed_paths,
        context_store=RepositoryContextStore(root),
        changed_lines=analysis.changed_lines,
        diff_analysis=analysis,
        document_policy=policy,
    )
    request = ReviewRequest(
        diff=diff_text,
        active_category_ids=selection.active_category_ids,
        lens_contexts=selection.lens_contexts,
        source_context=selection.source_context,
        work_policy_digest=service.work_policy_digest,
    )
    service.run(request)
    report = selection.lens_contexts[0].document_selection
    assert isinstance(report, DocumentSelectionReport)
    return (
        report.to_prompt_dict(),
        len(captured[0].encode("utf-8")),
        captured,
    )


def _seed_source_root(tmp: Path, *, version_only_action_updates: bool) -> Path:
    root = tmp / "repo"
    (root / "docs" / "adr").mkdir(parents=True)
    (root / "docs" / "architecture").mkdir(parents=True)
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / "AGENTS.md").write_text(
        "# Pinned Agent Rules\nMust hold.\n", encoding="utf-8", newline="\n"
    )
    (root / "docs" / "architecture.md").write_text(
        "# General Architecture\nLarge system narrative.\n",
        encoding="utf-8",
        newline="\n",
    )
    (root / "docs" / "architecture" / "mandatory-rules.md").write_text(
        "# Core Rules\nGeneral architecture rules.\n",
        encoding="utf-8",
        newline="\n",
    )
    (root / "docs" / "adr" / "0001-unrelated.md").write_text(
        "# ADR 0001: Provider Neutral Engine\nUnrelated.\n",
        encoding="utf-8",
        newline="\n",
    )
    (root / "docs" / "adr" / "0009-ci-quality-supply-chain-gates.md").write_text(
        "# ADR 0009: CI quality and supply-chain gates\nPin GitHub Actions SHAs.\n",
        encoding="utf-8",
        newline="\n",
    )
    (
        root / "docs" / "adr" / "0011-portable-immutable-github-actions-workflow.md"
    ).write_text(
        "# ADR 0011: Portable immutable GitHub Actions workflow\nKeep pins immutable.\n",
        encoding="utf-8",
        newline="\n",
    )
    (
        root / "docs" / "adr" / "0059-dependency-review-and-partial-publication.md"
    ).write_text(
        "# ADR 0059: Dependency review\nVerify dependency updates.\n",
        encoding="utf-8",
        newline="\n",
    )
    shutil.copy2(
        _REPO_ROOT / ".github" / "workflows" / "ci.yml",
        root / ".github" / "workflows" / "ci.yml",
    )
    flag = "true" if version_only_action_updates else "false"
    (root / ".reviewsensei.yml").write_text(
        "schema: 1\n"
        "inference:\n"
        "  backend: local-ollama\n"
        "  model: llama3.2:latest\n"
        "github:\n"
        "  writes: true\n"
        "  reviews: auto-approve\n"
        "advanced:\n"
        "  context:\n"
        "    supplemental_documents:\n"
        f"      version_only_action_updates: {flag}\n",
        encoding="utf-8",
        newline="\n",
    )
    return root


class VersionOnlyDependencyScopeTests(unittest.TestCase):
    def test_pr497_regression_fixture_scopes_documents_and_reduces_context(
        self,
    ) -> None:
        diff_text = _PR497_PATCH.read_text(encoding="utf-8")
        analysis = analyze_diff(diff_text)
        classification = classify_supplemental_scope(
            analysis,
            policy=SupplementalDocumentPolicy(),
            context_root=_REPO_ROOT,
        )
        self.assertEqual(classification.kind, "version-only-github-action-update")
        self.assertEqual(
            classification.updated_actions,
            ("github/codeql-action/analyze", "github/codeql-action/init"),
        )

        before_dict, before_bytes, before_prompts = _capture_service_prompt(
            diff_text=diff_text,
            policy=SupplementalDocumentPolicy(version_only_action_updates=False),
        )
        after_dict, after_bytes, after_prompts = _capture_service_prompt(
            diff_text=diff_text,
            policy=SupplementalDocumentPolicy(version_only_action_updates=True),
        )

        self.assertEqual(before_dict["policy"], POLICY)
        self.assertIsNone(before_dict.get("scope_classification"))
        self.assertGreater(before_dict["selected"], 16)  # type: ignore[operator]

        self.assertEqual(after_dict["policy"], VERSION_ONLY_ACTION_POLICY)
        self.assertIsNotNone(after_dict.get("scope_classification"))
        selected_paths = {
            item["path"]: item["reason"]
            for item in after_dict["selected_documents"]  # type: ignore[index]
        }
        self.assertEqual(selected_paths["AGENTS.md"], "pinned")
        self.assertEqual(
            selected_paths["docs/adr/0009-ci-quality-supply-chain-gates.md"],
            "scope-match",
        )
        self.assertEqual(
            selected_paths[
                "docs/adr/0011-portable-immutable-github-actions-workflow.md"
            ],
            "scope-match",
        )
        self.assertEqual(
            selected_paths[
                "docs/adr/0059-dependency-review-and-partial-publication.md"
            ],
            "scope-match",
        )
        self.assertLess(after_dict["selected"], before_dict["selected"])  # type: ignore[operator]
        self.assertIn("scope-filtered", after_dict["omission_reasons"])  # type: ignore[operator]
        self.assertNotIn("docs/architecture.md", selected_paths)
        self.assertLess(after_bytes, before_bytes)
        for changed_line in (
            "-      - uses: github/codeql-action/init@33d7b85da1a04171e211befe2c739303b9a4c066 # v4.39.3",
            "+      - uses: github/codeql-action/init@1bd86f2fdb4b86187fa4059f16f31a3202acc9ea # v4.39.4",
            "-      - uses: github/codeql-action/analyze@33d7b85da1a04171e211befe2c739303b9a4c066 # v4.39.3",
            "+      - uses: github/codeql-action/analyze@1bd86f2fdb4b86187fa4059f16f31a3202acc9ea # v4.39.4",
        ):
            self.assertIn(changed_line, after_prompts[0])
        self.assertEqual(len(before_prompts), 1)

    def test_workflow_permission_and_non_trivial_controls_use_ordinary_scope(
        self,
    ) -> None:
        perm_diff = (
            "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n"
            "index 13a3d040..3b733e03 100644\n"
            "--- a/.github/workflows/ci.yml\n"
            "+++ b/.github/workflows/ci.yml\n"
            "@@ -847,9 +847,9 @@ jobs:\n"
            "     permissions:\n"
            "       actions: read\n"
            "-      contents: read\n"
            "+      contents: write\n"
            "     steps:\n"
            "       - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1\n"
            "-      - uses: github/codeql-action/init@33d7b85da1a04171e211befe2c739303b9a4c066 # v4.39.3\n"
            "+      - uses: github/codeql-action/init@1bd86f2fdb4b86187fa4059f16f31a3202acc9ea # v4.39.4\n"
            "         with:\n"
            "           languages: ${{ matrix.language }}\n"
            "           config-file: ./.github/codeql/codeql-config.yml\n"
        )
        perm_dict, _, _ = _capture_service_prompt(
            diff_text=perm_diff,
            policy=SupplementalDocumentPolicy(version_only_action_updates=True),
        )
        self.assertEqual(perm_dict["policy"], POLICY)
        self.assertIsNone(perm_dict.get("scope_classification"))
        self.assertGreater(perm_dict["selected"], 16)  # type: ignore[operator]
        self.assertNotIn("scope-filtered", perm_dict["omission_reasons"])  # type: ignore[operator]

        controls = [
            (
                "identity_changed",
                "diff --git a/.github/workflows/w.yml b/.github/workflows/w.yml\n"
                "--- a/.github/workflows/w.yml\n"
                "+++ b/.github/workflows/w.yml\n"
                "@@ -1,4 +1,4 @@\n"
                " jobs:\n"
                "   test:\n"
                "     steps:\n"
                "-      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v4.1.0\n"
                "+      - uses: evil/checkout@1bd86f2fdb4b86187fa4059f16f31a3202acc9ea # v4.1.1\n",
                "action-identity-changed",
            ),
            (
                "pinning_downgrade",
                "diff --git a/.github/workflows/w.yml b/.github/workflows/w.yml\n"
                "--- a/.github/workflows/w.yml\n"
                "+++ b/.github/workflows/w.yml\n"
                "@@ -1,4 +1,4 @@\n"
                " jobs:\n"
                "   test:\n"
                "     steps:\n"
                "-      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1\n"
                "+      - uses: actions/checkout@v4.1.1\n",
                "pinning-downgrade",
            ),
            (
                "branch_ref",
                "diff --git a/.github/workflows/w.yml b/.github/workflows/w.yml\n"
                "--- a/.github/workflows/w.yml\n"
                "+++ b/.github/workflows/w.yml\n"
                "@@ -1,4 +1,4 @@\n"
                " jobs:\n"
                "   test:\n"
                "     steps:\n"
                "-      - uses: actions/checkout@v4.1.0\n"
                "+      - uses: actions/checkout@main\n",
                "unsafe-action-reference",
            ),
            (
                "local_action",
                "diff --git a/.github/workflows/w.yml b/.github/workflows/w.yml\n"
                "--- a/.github/workflows/w.yml\n"
                "+++ b/.github/workflows/w.yml\n"
                "@@ -1,4 +1,4 @@\n"
                " jobs:\n"
                "   test:\n"
                "     steps:\n"
                "-      - uses: ./actions/a@v1\n"
                "+      - uses: ./actions/a@v2\n",
                "unsafe-action-reference",
            ),
            (
                "docker_action",
                "diff --git a/.github/workflows/w.yml b/.github/workflows/w.yml\n"
                "--- a/.github/workflows/w.yml\n"
                "+++ b/.github/workflows/w.yml\n"
                "@@ -1,4 +1,4 @@\n"
                " jobs:\n"
                "   test:\n"
                "     steps:\n"
                "-      - uses: docker://alpine:3.18@v1\n"
                "+      - uses: docker://alpine:3.19@v2\n",
                "unsafe-action-reference",
            ),
            (
                "reusable_workflow",
                "diff --git a/.github/workflows/w.yml b/.github/workflows/w.yml\n"
                "--- a/.github/workflows/w.yml\n"
                "+++ b/.github/workflows/w.yml\n"
                "@@ -1,3 +1,3 @@\n"
                " jobs:\n"
                "   call:\n"
                "-    uses: org/repo/.github/workflows/reusable.yml@3d3c42e5aac5ba805825da76410c181273ba90b1\n"
                "+    uses: org/repo/.github/workflows/reusable.yml@1bd86f2fdb4b86187fa4059f16f31a3202acc9ea\n",
                "unsafe-action-reference",
            ),
            (
                "trigger_change",
                "diff --git a/.github/workflows/w.yml b/.github/workflows/w.yml\n"
                "--- a/.github/workflows/w.yml\n"
                "+++ b/.github/workflows/w.yml\n"
                "@@ -1,1 +1,1 @@\n"
                "-on: [push]\n"
                "+on: [pull_request]\n",
                "non-action-change",
            ),
            (
                "run_block_spoof",
                "diff --git a/.github/workflows/w.yml b/.github/workflows/w.yml\n"
                "--- a/.github/workflows/w.yml\n"
                "+++ b/.github/workflows/w.yml\n"
                "@@ -1,6 +1,6 @@\n"
                " jobs:\n"
                "   test:\n"
                "     steps:\n"
                "       - run: |\n"
                "           cat <<EOF\n"
                "-          - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v4.1.0\n"
                "+          - uses: actions/checkout@1bd86f2fdb4b86187fa4059f16f31a3202acc9ea # v4.1.1\n",
                "unverified-step-context",
            ),
            (
                "non_workflow_file",
                "diff --git a/README.md b/README.md\n"
                "--- a/README.md\n"
                "+++ b/README.md\n"
                "@@ -1,1 +1,1 @@\n"
                "-old\n"
                "+new\n",
                "non-workflow-path",
            ),
        ]
        for label, raw_diff, expected_reason in controls:
            with self.subTest(control=label):
                c = classify_supplemental_scope(
                    analyze_diff(raw_diff), policy=SupplementalDocumentPolicy()
                )
                self.assertEqual(c.kind, "ordinary")
                self.assertEqual(c.reason, expected_reason)

    def test_spoofed_uses_deep_in_run_block_fails_closed_via_trusted_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            wf = root / ".github" / "workflows" / "w.yml"
            wf.parent.mkdir(parents=True)
            wf.write_text(
                "name: CI\n"
                "on: [push]\n"
                "jobs:\n"
                "  build:\n"
                "    steps:\n"
                "      - name: Script\n"
                "        run: |\n"
                "          echo 1\n"
                "          echo 2\n"
                "          echo 3\n"
                "          echo 4\n"
                "          - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1\n"
                "          echo done\n",
                encoding="utf-8",
                newline="\n",
            )
            diff_text = (
                "diff --git a/.github/workflows/w.yml b/.github/workflows/w.yml\n"
                "--- a/.github/workflows/w.yml\n"
                "+++ b/.github/workflows/w.yml\n"
                "@@ -9,5 +9,5 @@\n"
                "           echo 2\n"
                "           echo 3\n"
                "           echo 4\n"
                "-          - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1\n"
                "+          - uses: actions/checkout@1bd86f2fdb4b86187fa4059f16f31a3202acc9ea\n"
                "           echo done\n"
            )
            c = classify_supplemental_scope(
                analyze_diff(diff_text),
                policy=SupplementalDocumentPolicy(),
                context_root=root,
            )
            self.assertEqual(c.kind, "ordinary")
            self.assertEqual(c.reason, "unverified-step-context")

        # Incomplete diff enumeration and mode changes fail closed.
        incomplete = DiffAnalysis(
            changed_lines={".github/workflows/w.yml": frozenset({5})},
            changed_paths=(".github/workflows/w.yml",),
            diff_bytes=100,
            diff_files=1,
            diff_hunks=1,
            diff_lines=3,
            hunk_records=(
                DiffHunk(
                    index=1,
                    old_path=".github/workflows/w.yml",
                    new_path=".github/workflows/w.yml",
                    old_start=5,
                    old_count=1,
                    new_start=5,
                    new_count=1,
                    text="@@ -5,1 +5,1 @@\n-      - uses: actions/checkout@v4\n+      - uses: actions/checkout@v5\n",
                    added_lines=frozenset({5}),
                    deleted_lines=frozenset({5}),
                ),
            ),
            file_records=(
                DiffFileRecord(
                    old_path=".github/workflows/w.yml",
                    new_path=".github/workflows/w.yml",
                    text="",
                    header="diff --git a/.github/workflows/w.yml b/.github/workflows/w.yml\n",
                    added_lines=frozenset({5}),
                    deleted_lines=frozenset({5}),
                    hunks=(),
                    binary=False,
                ),
            ),
            enumeration_complete=False,
        )
        self.assertEqual(classify_supplemental_scope(incomplete).kind, "ordinary")

    def test_config_opt_out_custom_guidance_paths_and_validation(self) -> None:
        cfg_off = parse_configuration_text(
            "schema: 1\n"
            "advanced:\n"
            "  context:\n"
            "    supplemental_documents:\n"
            "      version_only_action_updates: false\n"
        )
        self.assertFalse(
            cfg_off.advanced.context.supplemental_documents.version_only_action_updates
        )
        diff_text = _PR497_PATCH.read_text(encoding="utf-8")
        analysis = analyze_diff(diff_text)
        sel_off = build_review_context_selection(
            DEFAULT_CATEGORY_CATALOG.categories,
            changed_paths=analysis.changed_paths,
            context_store=RepositoryContextStore(_REPO_ROOT),
            changed_lines=analysis.changed_lines,
            diff_analysis=analysis,
            document_policy=SupplementalDocumentPolicy.from_configuration(cfg_off),
        )
        self.assertEqual(sel_off.lens_contexts[0].document_selection.policy, POLICY)

        cfg_custom = parse_configuration_text(
            "schema: 1\n"
            "advanced:\n"
            "  context:\n"
            "    supplemental_documents:\n"
            "      version_only_action_updates: true\n"
            "      guidance_paths:\n"
            "        - docs/adr/0033-protection-and-findings-gates.md\n"
        )
        sel_custom = build_review_context_selection(
            DEFAULT_CATEGORY_CATALOG.categories,
            changed_paths=analysis.changed_paths,
            context_store=RepositoryContextStore(_REPO_ROOT),
            changed_lines=analysis.changed_lines,
            diff_analysis=analysis,
            document_policy=SupplementalDocumentPolicy.from_configuration(cfg_custom),
        )
        self.assertIn(
            "docs/adr/0033-protection-and-findings-gates.md",
            {d.path for d in sel_custom.lens_contexts[0].documents},
        )

        with self.assertRaises(ConfigurationError):
            parse_configuration_text(
                "schema: 1\n"
                "advanced:\n"
                "  context:\n"
                "    supplemental_documents:\n"
                "      guidance_paths:\n"
                "        - ../secret.md\n"
            )
        with self.assertRaises(ConfigurationError):
            parse_configuration_text(
                "schema: 1\n"
                "advanced:\n"
                "  context:\n"
                "    supplemental_documents:\n"
                "      unknown_key: true\n"
            )

    def test_prompt_admission_preserves_metadata_and_binds_digests(self) -> None:
        diff_text = _PR497_PATCH.read_text(encoding="utf-8")
        analysis = analyze_diff(diff_text)
        scoped = build_review_context_selection(
            DEFAULT_CATEGORY_CATALOG.categories,
            changed_paths=analysis.changed_paths,
            context_store=RepositoryContextStore(_REPO_ROOT),
            changed_lines=analysis.changed_lines,
            diff_analysis=analysis,
            document_policy=SupplementalDocumentPolicy(
                version_only_action_updates=True
            ),
        )
        ordinary = build_review_context_selection(
            DEFAULT_CATEGORY_CATALOG.categories,
            changed_paths=analysis.changed_paths,
            context_store=RepositoryContextStore(_REPO_ROOT),
            changed_lines=analysis.changed_lines,
            diff_analysis=analysis,
            document_policy=SupplementalDocumentPolicy(
                version_only_action_updates=False
            ),
        )
        self.assertNotEqual(
            context_configuration_digest(scoped.lens_contexts),
            context_configuration_digest(ordinary.lens_contexts),
        )
        req_scoped = ReviewRequest(
            diff=diff_text,
            repository="owner/repo",
            pull_request_number=497,
            model="llama3.2:latest",
            base_sha=_BASE_SHA,
            head_sha=_HEAD_SHA,
            active_category_ids=scoped.active_category_ids,
            lens_contexts=scoped.lens_contexts,
            source_context=scoped.source_context,
        )
        req_ord = replace(
            req_scoped,
            active_category_ids=ordinary.active_category_ids,
            lens_contexts=ordinary.lens_contexts,
            source_context=ordinary.source_context,
        )
        key_scoped = build_review_context_cache_key(
            req_scoped, provider_name="ollama", stages=DEFAULT_STAGES
        )
        key_ord = build_review_context_cache_key(
            req_ord, provider_name="ollama", stages=DEFAULT_STAGES
        )
        self.assertIsNotNone(key_scoped)
        self.assertIsNotNone(key_ord)
        self.assertNotEqual(key_scoped, key_ord)

        admitted = admit_prompt_context(
            req_scoped,
            active_category_ids=scoped.active_category_ids,
            changed_paths=analysis.changed_paths,
            render=lambda cand: "\n".join(
                f"{d.path}:{d.content[:50]}"
                for lc in cand.lens_contexts
                for d in lc.documents
            ),
            fits=lambda rendered: len(rendered.encode("utf-8")) <= 120,
        )
        rep = admitted.request.lens_contexts[0].document_selection
        assert isinstance(rep, DocumentSelectionReport)
        self.assertEqual(rep.policy, VERSION_ONLY_ACTION_POLICY)
        self.assertIsNotNone(rep.scope_classification)

    def test_legacy_checkpoint_preserves_coverage_on_remaining_continuation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            root = _seed_source_root(tmp, version_only_action_updates=False)
            (root / ".reviewsensei.yml").write_text(
                "schema: 1\n"
                "inference:\n"
                "  backend: local-ollama\n"
                "  model: qwen3.5:4b\n"
                "advanced:\n"
                "  large_changes:\n"
                "    orchestrate: true\n"
                "  resources:\n"
                "    max_provider_calls: 8\n"
                "  context:\n"
                "    supplemental_documents:\n"
                "      version_only_action_updates: false\n",
                encoding="utf-8",
                newline="\n",
            )
            parts = [_PR497_PATCH.read_text(encoding="utf-8")]
            for idx in range(1, 9):
                wf_name = f"wf_{idx}.yml"
                wf_text = (
                    f"name: WF {idx}\n"
                    "on: [push]\n"
                    "jobs:\n"
                    "  lint:\n"
                    "    runs-on: ubuntu-latest\n"
                    "    steps:\n"
                    "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v4.1.0\n"
                )
                (root / ".github" / "workflows" / wf_name).write_text(
                    wf_text, encoding="utf-8", newline="\n"
                )
                parts.append(
                    f"diff --git a/.github/workflows/{wf_name} b/.github/workflows/{wf_name}\n"
                    "index 11111111..22222222 100644\n"
                    f"--- a/.github/workflows/{wf_name}\n"
                    f"+++ b/.github/workflows/{wf_name}\n"
                    "@@ -4,4 +4,4 @@ jobs:\n"
                    "   lint:\n"
                    "     runs-on: ubuntu-latest\n"
                    "     steps:\n"
                    "-      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v4.1.0\n"
                    "+      - uses: actions/checkout@1bd86f2fdb4b86187fa4059f16f31a3202acc9ea # v4.1.1\n"
                )
            diff_file = tmp / "nine-workflows.patch"
            diff_file.write_text("".join(parts), encoding="utf-8", newline="\n")
            fixture_resp = tmp / "resp.json"
            fixture_resp.write_text(
                '{"summary": "Chunk clean.", "comments": []}',
                encoding="utf-8",
                newline="\n",
            )
            ledger_dir = tmp / "ledger"
            out1 = tmp / "out1.json"
            out2 = tmp / "out2.json"
            sel1 = tmp / "sel1.json"
            sel2 = tmp / "sel2.json"
            small_limits = ReviewLimits(
                max_diff_bytes=64 * 1024,
                max_diff_lines=1600,
                max_diff_files=1,
                max_diff_hunks=1,
            )
            base_args = [
                "--diff",
                str(diff_file),
                "--context-root",
                str(root),
                "--config",
                str(root / ".reviewsensei.yml"),
                "--provider",
                "fixture",
                "--fixture-response",
                str(fixture_resp),
                "--repository",
                "owner/repo",
                "--pull-request",
                "497",
                "--base-sha",
                _BASE_SHA,
                "--head-sha",
                _LEGACY_HEAD_SHA,
                "--transaction",
                "--session-ledger",
                str(ledger_dir),
                "--orchestrate-large-changes",
                "--format",
                "json",
                "--no-learning-proposals",
            ]
            err1 = io.StringIO()
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("review_sensei.cli.DEFAULT_REVIEW_LIMITS", small_limits),
                redirect_stdout(io.StringIO()),
                redirect_stderr(err1),
            ):
                rc1 = cli.main(
                    [
                        *base_args,
                        "--context-selection-output",
                        str(sel1),
                        "--output",
                        str(out1),
                    ]
                )
            self.assertEqual(rc1, 2, err1.getvalue())
            self.assertTrue(out1.exists(), err1.getvalue())
            res1 = ReviewResult.from_dict(json.loads(out1.read_text(encoding="utf-8")))
            self.assertEqual(res1.review_status, "partial")
            assert res1.coverage is not None
            self.assertEqual(
                sum(1 for f in res1.coverage.files if f.outcome == "reviewed"), 8
            )
            self.assertEqual(
                sum(1 for f in res1.coverage.files if f.outcome == "budget-exhausted"),
                1,
            )
            self.assertEqual(
                json.loads(sel1.read_text(encoding="utf-8"))[0]["policy"], POLICY
            )

            assert res1.transaction is not None
            complete_review_publication(
                LocalSessionLedger(ledger_dir),
                SessionIdentity("owner/repo", 497),
                res1.transaction,
                published=True,
            )

            # Toggle trusted config policy to version_only_action_updates=true and continue.
            (root / ".reviewsensei.yml").write_text(
                (root / ".reviewsensei.yml")
                .read_text(encoding="utf-8")
                .replace(
                    "version_only_action_updates: false",
                    "version_only_action_updates: true",
                ),
                encoding="utf-8",
                newline="\n",
            )
            orig_complete = FixtureProvider.complete
            calls_run2 = 0

            def _counting_complete(self_prov, request):
                nonlocal calls_run2
                calls_run2 += 1
                return orig_complete(self_prov, request)

            err2 = io.StringIO()
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("review_sensei.cli.DEFAULT_REVIEW_LIMITS", small_limits),
                patch.object(FixtureProvider, "complete", _counting_complete),
                redirect_stdout(io.StringIO()),
                redirect_stderr(err2),
            ):
                rc2 = cli.main(
                    [
                        *base_args,
                        "--context-selection-output",
                        str(sel2),
                        "--output",
                        str(out2),
                    ]
                )
            self.assertEqual(rc2, 0, err2.getvalue())
            self.assertEqual(calls_run2, 1)
            self.assertTrue(out2.exists(), err2.getvalue())
            res2 = ReviewResult.from_dict(json.loads(out2.read_text(encoding="utf-8")))
            self.assertEqual(res2.review_status, "complete")
            assert res2.coverage is not None
            self.assertTrue(res2.coverage.fully_reviewed)
            reports = json.loads(sel2.read_text(encoding="utf-8"))
            self.assertEqual(reports[0]["policy"], POLICY)

    def test_legacy_checkpoint_preserves_publication_recovery_and_fails_on_tamper(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            root = _seed_source_root(tmp, version_only_action_updates=False)
            diff_text = _PR497_PATCH.read_text(encoding="utf-8")
            ledger = LocalSessionLedger(tmp / "ledger")
            identity = SessionIdentity(repository="owner/repo", pull_request=497)
            policy = ReviewConvergencePolicy()
            config_text = (root / ".reviewsensei.yml").read_text(encoding="utf-8")
            configuration = parse_configuration_text(config_text, root=root)
            work_policy = review_work_policy_digest(
                configuration.advanced.review_work,
                _default_budget(configuration),
                decision=configuration.decision,
            )
            analysis = analyze_diff(diff_text)
            legacy_sel = build_review_context_selection(
                DEFAULT_CATEGORY_CATALOG.categories,
                changed_paths=analysis.changed_paths,
                context_store=RepositoryContextStore(root),
                changed_lines=analysis.changed_lines,
                diff_analysis=analysis,
                document_policy=SupplementalDocumentPolicy(
                    version_only_action_updates=False
                ),
            )
            _, tx_provider, tx_model = _resolve_recovery_inference(
                configuration, cli_provider=None, cli_model=None, environ={}
            )
            stage_id, cat_pol = transaction_stage_identity(DEFAULT_STAGES)
            cfg_ctx = build_transaction_configuration_context(
                provider=tx_provider,
                model=tx_model,
                stages=stage_id,
                category_policy=cat_pol,
                publication_mode=policy.mode,
                orchestration_enabled=False,
                work_policy_digest=work_policy,
            )
            cfg_digest = ReviewTransaction.compute_configuration_digest(cfg_ctx)
            ev_digest = ReviewTransaction.compute_evidence_digest(
                {"evidence_policy": "legacy", "snapshot_sha256": None}
            )
            legacy_key = build_review_context_cache_key(
                ReviewRequest(
                    diff=diff_text,
                    repository="owner/repo",
                    pull_request_number=497,
                    model="llama3.2:latest",
                    active_category_ids=legacy_sel.active_category_ids,
                    lens_contexts=legacy_sel.lens_contexts,
                    source_context=legacy_sel.source_context,
                    base_sha=_BASE_SHA,
                    head_sha=_LEGACY_HEAD_SHA,
                    work_policy_digest=work_policy,
                ),
                provider_name="ollama",
                stages=DEFAULT_STAGES,
                profile="default",
                effective_model="llama3.2:latest",
                stage_provider_bindings={
                    s.name: ("ollama", "llama3.2:latest") for s in DEFAULT_STAGES
                },
            )
            assert legacy_key is not None
            legacy_res = session_reservation_id(
                repository="owner/repo",
                pull_request=497,
                head_sha=_LEGACY_HEAD_SHA,
                kind=(
                    "publish:"
                    + legacy_key.digest()
                    + ":"
                    + policy.digest()
                    + ":"
                    + cfg_digest
                    + ":"
                    + ev_digest
                ),
            )
            tx = prepare_review_transaction(
                ledger,
                identity,
                policy,
                reservation_id=legacy_res,
                base_sha=_BASE_SHA,
                head_sha=_LEGACY_HEAD_SHA,
                configuration_digest=cfg_digest,
                evidence_digest=ev_digest,
            )
            clean = ReviewResult(
                summary="All good.",
                comments=(),
                provider="ollama",
                model="llama3.2:latest",
                review_status="complete",
                coverage=CoverageManifest(
                    enumeration_complete=True,
                    enumerated_paths=(".github/workflows/ci.yml",),
                    files=(
                        FileCoverage(
                            path=".github/workflows/ci.yml", outcome="reviewed"
                        ),
                    ),
                    hunks=(
                        HunkCoverage(
                            index=1, path=".github/workflows/ci.yml", outcome="reviewed"
                        ),
                    ),
                ),
                transaction=tx.transaction,
                work_policy_digest=work_policy,
            )
            legacy_baseline = ReviewBaseline(
                cache_key=legacy_key,
                policy_digest=policy.digest(),
                complete=True,
                coverage_complete=True,
                generation=tx.record.generation + 1,
                findings=(),
                reviewed_paths=(".github/workflows/ci.yml",),
            )
            checkpoint_review_analysis(
                ledger, identity, tx, clean, baseline=legacy_baseline
            )

            # Toggle policy on trusted root to version_only_action_updates=true.
            (root / ".reviewsensei.yml").write_text(
                config_text.replace(
                    "version_only_action_updates: false",
                    "version_only_action_updates: true",
                ),
                encoding="utf-8",
                newline="\n",
            )
            recovered = reconstruct_publication_contexts(
                source_root=root,
                diff=diff_text,
                ledger=ledger,
                identity=identity,
                base_sha=_BASE_SHA,
                head_sha=_LEGACY_HEAD_SHA,
                policy=policy,
                environ={},
            )
            self.assertEqual(recovered.current_key, legacy_key)
            assert recovered.admission_context is not None
            recovered_lenses = recovered.admission_context["lens_contexts"]
            assert isinstance(recovered_lenses, list)
            self.assertEqual(
                recovered_lenses[0]["document_selection"]["policy"], POLICY
            )

            # Tampered / mismatched pinned context document refuses recovery.
            (root / "AGENTS.md").write_text(
                "# Tampered Rules\n", encoding="utf-8", newline="\n"
            )
            with self.assertRaises(RecoveryRefusalError):
                reconstruct_publication_contexts(
                    source_root=root,
                    diff=diff_text,
                    ledger=ledger,
                    identity=identity,
                    base_sha=_BASE_SHA,
                    head_sha=_LEGACY_HEAD_SHA,
                    policy=policy,
                    environ={},
                )

    def test_top_down_yaml_ancestry_and_block_scalar_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            wf = root / ".github" / "workflows" / "w.yml"
            wf.parent.mkdir(parents=True)

            # RS-7C68F8: Nested list under a step's custom mapping is rejected.
            wf.write_text(
                "name: CI\n"
                "on: [push]\n"
                "jobs:\n"
                "  build:\n"
                "    steps:\n"
                "      - name: Sibling\n"
                "        custom_map:\n"
                "        - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1\n",
                encoding="utf-8",
                newline="\n",
            )
            nested_diff = (
                "diff --git a/.github/workflows/w.yml b/.github/workflows/w.yml\n"
                "--- a/.github/workflows/w.yml\n"
                "+++ b/.github/workflows/w.yml\n"
                "@@ -6,3 +6,3 @@\n"
                "       - name: Sibling\n"
                "         custom_map:\n"
                "-        - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1\n"
                "+        - uses: actions/checkout@1bd86f2fdb4b86187fa4059f16f31a3202acc9ea\n"
            )
            c_nested = classify_supplemental_scope(
                analyze_diff(nested_diff),
                policy=SupplementalDocumentPolicy(),
                context_root=root,
            )
            self.assertEqual(c_nested.kind, "ordinary")
            self.assertEqual(c_nested.reason, "unverified-step-context")

            # RS-C19E7A: Block scalar inside the same step mapping is rejected.
            wf.write_text(
                "name: CI\n"
                "on: [push]\n"
                "jobs:\n"
                "  build:\n"
                "    steps:\n"
                "      - name: Scripted\n"
                "        run: >2-\n"
                "          echo start\n"
                "          - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1\n",
                encoding="utf-8",
                newline="\n",
            )
            scalar_diff = (
                "diff --git a/.github/workflows/w.yml b/.github/workflows/w.yml\n"
                "--- a/.github/workflows/w.yml\n"
                "+++ b/.github/workflows/w.yml\n"
                "@@ -7,3 +7,3 @@\n"
                "         run: >2-\n"
                "           echo start\n"
                "-          - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1\n"
                "+          - uses: actions/checkout@1bd86f2fdb4b86187fa4059f16f31a3202acc9ea\n"
            )
            c_scalar = classify_supplemental_scope(
                analyze_diff(scalar_diff),
                policy=SupplementalDocumentPolicy(),
                context_root=root,
            )
            self.assertEqual(c_scalar.kind, "ordinary")
            self.assertEqual(c_scalar.reason, "unverified-step-context")

            # Valid: flush-indented `steps:\n- uses:` and real sibling step after `run: |`.
            wf.write_text(
                "name: CI\n"
                "on: [push]\n"
                "jobs:\n"
                "  build:\n"
                "    steps:\n"
                "    - name: First\n"
                "      run: |\n"
                "        echo ok\n"
                "    - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1\n"
                "    - name: Second\n"
                "      id: s2\n"
                "      uses: github/codeql-action/init@33d7b85da1a04171e211befe2c739303b9a4c066\n",
                encoding="utf-8",
                newline="\n",
            )
            valid_diff = (
                "diff --git a/.github/workflows/w.yml b/.github/workflows/w.yml\n"
                "--- a/.github/workflows/w.yml\n"
                "+++ b/.github/workflows/w.yml\n"
                "@@ -8,5 +8,5 @@\n"
                "         echo ok\n"
                "-    - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1\n"
                "+    - uses: actions/checkout@1bd86f2fdb4b86187fa4059f16f31a3202acc9ea\n"
                "     - name: Second\n"
                "       id: s2\n"
                "-      uses: github/codeql-action/init@33d7b85da1a04171e211befe2c739303b9a4c066\n"
                "+      uses: github/codeql-action/init@1bd86f2fdb4b86187fa4059f16f31a3202acc9ea\n"
            )
            c_valid = classify_supplemental_scope(
                analyze_diff(valid_diff),
                policy=SupplementalDocumentPolicy(),
                context_root=root,
            )
            self.assertEqual(c_valid.kind, "version-only-github-action-update")
            self.assertEqual(
                c_valid.updated_actions,
                ("actions/checkout", "github/codeql-action/init"),
            )

    def test_scoped_partial_continue_and_legacy_fallback_without_scoped_lenses(
        self,
    ) -> None:
        # RS-1515BD: Scoped partial-continue reservation matches on current_matched.
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            root = _seed_source_root(tmp, version_only_action_updates=True)
            (root / ".reviewsensei.yml").write_text(
                "schema: 1\n"
                "inference:\n"
                "  backend: local-ollama\n"
                "  model: qwen3.5:4b\n"
                "advanced:\n"
                "  large_changes:\n"
                "    orchestrate: true\n"
                "  resources:\n"
                "    max_provider_calls: 8\n"
                "  context:\n"
                "    supplemental_documents:\n"
                "      version_only_action_updates: true\n",
                encoding="utf-8",
                newline="\n",
            )
            parts = [_PR497_PATCH.read_text(encoding="utf-8")]
            for idx in range(1, 17):
                wf_name = f"wf_{idx}.yml"
                (root / ".github" / "workflows" / wf_name).write_text(
                    f"name: WF {idx}\n"
                    "on: [push]\n"
                    "jobs:\n"
                    "  lint:\n"
                    "    runs-on: ubuntu-latest\n"
                    "    steps:\n"
                    "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v4.1.0\n",
                    encoding="utf-8",
                    newline="\n",
                )
                parts.append(
                    f"diff --git a/.github/workflows/{wf_name} b/.github/workflows/{wf_name}\n"
                    "index 11111111..22222222 100644\n"
                    f"--- a/.github/workflows/{wf_name}\n"
                    f"+++ b/.github/workflows/{wf_name}\n"
                    "@@ -4,4 +4,4 @@ jobs:\n"
                    "   lint:\n"
                    "     runs-on: ubuntu-latest\n"
                    "     steps:\n"
                    "-      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v4.1.0\n"
                    "+      - uses: actions/checkout@1bd86f2fdb4b86187fa4059f16f31a3202acc9ea # v4.1.1\n"
                )
            diff_file = tmp / "nine-workflows.patch"
            diff_file.write_text("".join(parts), encoding="utf-8", newline="\n")
            fixture_resp = tmp / "resp.json"
            fixture_resp.write_text(
                '{"summary": "Chunk clean.", "comments": []}',
                encoding="utf-8",
                newline="\n",
            )
            ledger_dir = tmp / "ledger"
            out1 = tmp / "out1.json"
            out2 = tmp / "out2.json"
            out3 = tmp / "out3.json"
            sel1 = tmp / "sel1.json"
            sel2 = tmp / "sel2.json"
            sel3 = tmp / "sel3.json"
            small_limits = ReviewLimits(
                max_diff_bytes=64 * 1024,
                max_diff_lines=1600,
                max_diff_files=1,
                max_diff_hunks=1,
            )
            base_args = [
                "--diff",
                str(diff_file),
                "--context-root",
                str(root),
                "--config",
                str(root / ".reviewsensei.yml"),
                "--provider",
                "fixture",
                "--fixture-response",
                str(fixture_resp),
                "--repository",
                "owner/repo",
                "--pull-request",
                "497",
                "--base-sha",
                _BASE_SHA,
                "--head-sha",
                _HEAD_SHA,
                "--transaction",
                "--session-ledger",
                str(ledger_dir),
                "--orchestrate-large-changes",
                "--format",
                "json",
                "--no-learning-proposals",
            ]
            err1 = io.StringIO()
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("review_sensei.cli.DEFAULT_REVIEW_LIMITS", small_limits),
                redirect_stdout(io.StringIO()),
                redirect_stderr(err1),
            ):
                rc1 = cli.main(
                    [
                        *base_args,
                        "--context-selection-output",
                        str(sel1),
                        "--output",
                        str(out1),
                    ]
                )
            self.assertEqual(rc1, 2, err1.getvalue())
            res1 = ReviewResult.from_dict(json.loads(out1.read_text(encoding="utf-8")))
            assert res1.transaction is not None
            complete_review_publication(
                LocalSessionLedger(ledger_dir),
                SessionIdentity("owner/repo", 497),
                res1.transaction,
                published=True,
            )

            # Second window produces partial-continue reservation on scoped context.
            err2 = io.StringIO()
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("review_sensei.cli.DEFAULT_REVIEW_LIMITS", small_limits),
                redirect_stdout(io.StringIO()),
                redirect_stderr(err2),
            ):
                rc2 = cli.main(
                    [
                        *base_args,
                        "--context-selection-output",
                        str(sel2),
                        "--output",
                        str(out2),
                    ]
                )
            self.assertEqual(rc2, 2, err2.getvalue())
            res2 = ReviewResult.from_dict(json.loads(out2.read_text(encoding="utf-8")))
            self.assertEqual(res2.review_status, "partial")
            assert res2.transaction is not None
            self.assertNotEqual(
                res2.transaction.reservation_id, res1.transaction.reservation_id
            )
            complete_review_publication(
                LocalSessionLedger(ledger_dir),
                SessionIdentity("owner/repo", 497),
                res2.transaction,
                published=True,
            )

            # Third window must match the scoped partial-continue reservation without triggering legacy fallback.
            err3 = io.StringIO()
            with (
                patch.dict(os.environ, {}, clear=True),
                patch("review_sensei.cli.DEFAULT_REVIEW_LIMITS", small_limits),
                redirect_stdout(io.StringIO()),
                redirect_stderr(err3),
            ):
                rc3 = cli.main(
                    [
                        *base_args,
                        "--context-selection-output",
                        str(sel3),
                        "--output",
                        str(out3),
                    ]
                )
            self.assertEqual(rc3, 0, err3.getvalue())
            res3 = ReviewResult.from_dict(json.loads(out3.read_text(encoding="utf-8")))
            self.assertEqual(res3.review_status, "complete")
            self.assertEqual(
                json.loads(sel3.read_text(encoding="utf-8"))[0]["policy"],
                VERSION_ONLY_ACTION_POLICY,
            )

        # RS-E61701: Legacy recovery fallback triggers even when selection lenses hold legacy policy.
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            root = _seed_source_root(tmp, version_only_action_updates=False)
            diff_text = _PR497_PATCH.read_text(encoding="utf-8")
            ledger = LocalSessionLedger(tmp / "ledger")
            identity = SessionIdentity(repository="owner/repo", pull_request=497)
            policy = ReviewConvergencePolicy()
            configuration = parse_configuration_text(
                (root / ".reviewsensei.yml").read_text(encoding="utf-8"), root=root
            )
            work_policy = review_work_policy_digest(
                configuration.advanced.review_work,
                _default_budget(configuration),
                decision=configuration.decision,
            )
            analysis = analyze_diff(diff_text)
            legacy_sel = build_review_context_selection(
                DEFAULT_CATEGORY_CATALOG.categories,
                changed_paths=analysis.changed_paths,
                context_store=RepositoryContextStore(root),
                changed_lines=analysis.changed_lines,
                diff_analysis=None,
                document_policy=SupplementalDocumentPolicy(
                    version_only_action_updates=False
                ),
            )
            _, tx_provider, tx_model = _resolve_recovery_inference(
                configuration, cli_provider=None, cli_model=None, environ={}
            )
            stage_id, cat_pol = transaction_stage_identity(DEFAULT_STAGES)
            cfg_digest = ReviewTransaction.compute_configuration_digest(
                build_transaction_configuration_context(
                    provider=tx_provider,
                    model=tx_model,
                    stages=stage_id,
                    category_policy=cat_pol,
                    publication_mode=policy.mode,
                    orchestration_enabled=False,
                    work_policy_digest=work_policy,
                )
            )
            ev_digest = ReviewTransaction.compute_evidence_digest(
                {"evidence_policy": "legacy", "snapshot_sha256": None}
            )
            legacy_key = build_review_context_cache_key(
                ReviewRequest(
                    diff=diff_text,
                    repository="owner/repo",
                    pull_request_number=497,
                    model="llama3.2:latest",
                    active_category_ids=legacy_sel.active_category_ids,
                    lens_contexts=legacy_sel.lens_contexts,
                    source_context=legacy_sel.source_context,
                    base_sha=_BASE_SHA,
                    head_sha=_LEGACY_HEAD_SHA,
                    work_policy_digest=work_policy,
                ),
                provider_name="ollama",
                stages=DEFAULT_STAGES,
                profile="default",
                effective_model="llama3.2:latest",
                stage_provider_bindings={
                    s.name: ("ollama", "llama3.2:latest") for s in DEFAULT_STAGES
                },
            )
            assert legacy_key is not None
            legacy_prefix = (
                "publish:"
                + legacy_key.digest()
                + ":"
                + policy.digest()
                + ":"
                + cfg_digest
                + ":"
                + ev_digest
            )
            legacy_res = session_reservation_id(
                repository="owner/repo",
                pull_request=497,
                head_sha=_LEGACY_HEAD_SHA,
                kind=legacy_prefix,
            )
            tx = prepare_review_transaction(
                ledger,
                identity,
                policy,
                reservation_id=legacy_res,
                base_sha=_BASE_SHA,
                head_sha=_LEGACY_HEAD_SHA,
                configuration_digest=cfg_digest,
                evidence_digest=ev_digest,
            )
            reviewed_cov = CoverageManifest(
                enumeration_complete=True,
                enumerated_paths=(".github/workflows/ci.yml",),
                files=(
                    FileCoverage(
                        path=".github/workflows/ci.yml",
                        outcome="reviewed",
                    ),
                ),
                hunks=(
                    HunkCoverage(
                        index=1,
                        path=".github/workflows/ci.yml",
                        outcome="reviewed",
                    ),
                ),
            )
            partial = ReviewResult(
                summary="Partial.",
                comments=(),
                provider="ollama",
                model="llama3.2:latest",
                review_status="partial",
                coverage=reviewed_cov,
                transaction=tx.transaction,
                work_policy_digest=work_policy,
            )
            tx_cp = checkpoint_review_analysis(ledger, identity, tx, partial)
            rec = complete_review_publication(
                ledger, identity, tx_cp.transaction, published=True
            )
            # Re-bind under partial-continue reservation on retry under toggled policy.
            partial_res = session_reservation_id(
                repository="owner/repo",
                pull_request=497,
                head_sha=_LEGACY_HEAD_SHA,
                kind=legacy_prefix
                + f":partial-continue:{rec.failed_attempts}:{rec.generation}",
            )
            tx2 = prepare_review_transaction(
                ledger,
                identity,
                policy,
                reservation_id=partial_res,
                base_sha=_BASE_SHA,
                head_sha=_LEGACY_HEAD_SHA,
                configuration_digest=cfg_digest,
                evidence_digest=ev_digest,
            )
            clean = replace(
                partial,
                review_status="complete",
                coverage=reviewed_cov,
                transaction=tx2.transaction,
            )
            legacy_baseline = ReviewBaseline(
                cache_key=legacy_key,
                policy_digest=policy.digest(),
                complete=True,
                coverage_complete=True,
                generation=tx2.record.generation + 1,
                findings=(),
                reviewed_paths=(".github/workflows/ci.yml",),
            )
            checkpoint_review_analysis(
                ledger, identity, tx2, clean, baseline=legacy_baseline
            )
            (root / ".reviewsensei.yml").write_text(
                (root / ".reviewsensei.yml")
                .read_text(encoding="utf-8")
                .replace(
                    "version_only_action_updates: false",
                    "version_only_action_updates: true",
                ),
                encoding="utf-8",
                newline="\n",
            )
            recovered = reconstruct_publication_contexts(
                source_root=root,
                diff=diff_text,
                ledger=ledger,
                identity=identity,
                base_sha=_BASE_SHA,
                head_sha=_LEGACY_HEAD_SHA,
                policy=policy,
                environ={},
            )
            self.assertEqual(recovered.current_key, legacy_key)


def _default_budget(configuration):
    from review_sensei.cli import _configured_resource_budget
    from review_sensei.outcomes import ResourceBudget
    from review_sensei.validation import DEFAULT_REVIEW_LIMITS

    return _configured_resource_budget(
        ResourceBudget.for_limits(DEFAULT_REVIEW_LIMITS), configuration
    )

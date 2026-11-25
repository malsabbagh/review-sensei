"""Installed-wheel contracts for version-only dependency document scoping."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_TESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TESTS))
from packaging_guard import assert_distribution_import  # noqa: E402

assert_distribution_import()

from review_sensei.configuration import parse_configuration_text  # noqa: E402
from review_sensei.context import (  # noqa: E402
    RepositoryContextStore,
    admit_prompt_context,
    build_review_context_selection,
)
from review_sensei.diff import analyze_diff  # noqa: E402
from review_sensei.document_context import (  # noqa: E402
    POLICY,
    VERSION_ONLY_ACTION_POLICY,
    DocumentSelectionReport,
    SupplementalDocumentPolicy,
    classify_supplemental_scope,
)
from review_sensei.models import ReviewRequest  # noqa: E402
from review_sensei.service import DEFAULT_CATEGORY_CATALOG  # noqa: E402

_REPO_ROOT = _TESTS.parent
_PR497_PATCH = _TESTS / "fixtures" / "regressions" / "pr497-codeql-update.patch"


class InstalledVersionOnlyDocScopeContractTests(unittest.TestCase):
    def test_installed_config_and_pr497_scoping(self) -> None:
        cfg = parse_configuration_text(
            "schema: 1\n"
            "advanced:\n"
            "  context:\n"
            "    supplemental_documents:\n"
            "      version_only_action_updates: true\n"
            "      guidance_paths:\n"
            "        - docs/adr/0033-protection-and-findings-gates.md\n"
        )
        policy = SupplementalDocumentPolicy.from_configuration(cfg)
        self.assertTrue(policy.version_only_action_updates)
        self.assertEqual(
            policy.guidance_paths, ("docs/adr/0033-protection-and-findings-gates.md",)
        )

        diff_text = _PR497_PATCH.read_text(encoding="utf-8")
        analysis = analyze_diff(diff_text)
        classification = classify_supplemental_scope(
            analysis, policy=policy, context_root=_REPO_ROOT
        )
        self.assertEqual(classification.kind, "version-only-github-action-update")
        self.assertEqual(
            classification.updated_actions,
            ("github/codeql-action/analyze", "github/codeql-action/init"),
        )

        scoped = build_review_context_selection(
            DEFAULT_CATEGORY_CATALOG.categories,
            changed_paths=analysis.changed_paths,
            context_store=RepositoryContextStore(_REPO_ROOT),
            changed_lines=analysis.changed_lines,
            diff_analysis=analysis,
            document_policy=SupplementalDocumentPolicy(),
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
        scoped_arch = scoped.lens_contexts[0]
        ordinary_arch = ordinary.lens_contexts[0]
        assert isinstance(scoped_arch.document_selection, DocumentSelectionReport)
        assert isinstance(ordinary_arch.document_selection, DocumentSelectionReport)
        self.assertEqual(
            scoped_arch.document_selection.policy, VERSION_ONLY_ACTION_POLICY
        )
        self.assertEqual(ordinary_arch.document_selection.policy, POLICY)
        self.assertLess(len(scoped_arch.documents), len(ordinary_arch.documents))
        scoped_paths = {doc.path for doc in scoped_arch.documents}
        self.assertIn("AGENTS.md", scoped_paths)
        self.assertIn("docs/adr/0009-ci-quality-supply-chain-gates.md", scoped_paths)
        self.assertIn(
            "docs/adr/0011-portable-immutable-github-actions-workflow.md",
            scoped_paths,
        )
        self.assertNotIn("docs/architecture.md", scoped_paths)

        # Prompt-budget bisection preserves policy and classification metadata.
        request = ReviewRequest(
            diff=diff_text,
            active_category_ids=scoped.active_category_ids,
            lens_contexts=scoped.lens_contexts,
            source_context=scoped.source_context,
        )
        admission = admit_prompt_context(
            request,
            active_category_ids=scoped.active_category_ids,
            changed_paths=analysis.changed_paths,
            render=lambda cand: "\n".join(
                f"{d.path}:{d.content[:40]}"
                for lc in cand.lens_contexts
                for d in lc.documents
            ),
            fits=lambda rendered: len(rendered.encode("utf-8")) <= 120,
        )
        admitted_report = admission.request.lens_contexts[0].document_selection
        assert isinstance(admitted_report, DocumentSelectionReport)
        self.assertEqual(admitted_report.policy, VERSION_ONLY_ACTION_POLICY)
        self.assertIsNotNone(admitted_report.scope_classification)

    def test_installed_permission_change_uses_ordinary_scope(self) -> None:
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
        analysis = analyze_diff(perm_diff)
        selection = build_review_context_selection(
            DEFAULT_CATEGORY_CATALOG.categories,
            changed_paths=analysis.changed_paths,
            context_store=RepositoryContextStore(_REPO_ROOT),
            changed_lines=analysis.changed_lines,
            diff_analysis=analysis,
            document_policy=SupplementalDocumentPolicy(),
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
        arch = selection.lens_contexts[0]
        ordinary_arch = ordinary.lens_contexts[0]
        assert isinstance(arch.document_selection, DocumentSelectionReport)
        self.assertEqual(arch.document_selection.policy, POLICY)
        self.assertIsNone(arch.document_selection.scope_classification)
        self.assertEqual(arch.documents, ordinary_arch.documents)

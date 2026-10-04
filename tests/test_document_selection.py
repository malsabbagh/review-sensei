import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from review_sensei.context import (
    RepositoryContextStore,
    build_review_context_selection,
    context_configuration_digest,
)
from review_sensei.document_context import MAX_INSPECT_DOCUMENT_BYTES
from review_sensei.errors import ContextLoadError, ReviewInputError
from review_sensei.models import (
    MAX_REVIEW_DOCUMENT_BYTES,
    ReviewLensContext,
    ReviewRequest,
)
from review_sensei.stages import ContextDocumentSource, ReviewCategory

try:
    from isolated_working_directory import IsolatedWorkingDirectoryMixin
except ModuleNotFoundError:
    from tests.isolated_working_directory import IsolatedWorkingDirectoryMixin


class DocumentSelectionTests(IsolatedWorkingDirectoryMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "docs").mkdir()

    def write(self, path, content):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def category(self, id="architecture", sources=None, applies=("**",)):
        return ReviewCategory(
            id=id,
            title=id,
            focus=("boundaries",),
            applies_to=applies,
            document_sources=tuple(sources or (ContextDocumentSource(path="docs"),)),
        )

    def select(self, categories=None, paths=("src/payments.py",)):
        return build_review_context_selection(
            tuple(categories or (self.category(),)),
            changed_paths=paths,
            context_store=RepositoryContextStore(self.root),
        )

    def test_more_than_64_prioritizes_scope_and_keeps_full_guidance(self):
        for index in range(80):
            self.write(f"docs/aaa-{index:03}.md", "Unrelated deployment history.")
        self.write("docs/zzz-payments.md", "# Payments\nNever log card numbers.\n")
        self.write("AGENTS.md", "Do not weaken repository requirements.")
        category = self.category(
            sources=(
                ContextDocumentSource(path="AGENTS.md"),
                ContextDocumentSource(path="docs"),
            )
        )
        selection = self.select((category,))
        lens = selection.lens_contexts[0]
        paths = {doc.path for doc in lens.documents}
        self.assertEqual(len(paths), 64)
        self.assertIn("docs/zzz-payments.md", paths)
        self.assertIn("AGENTS.md", paths)
        report = lens.to_prompt_dict()["document_selection"]
        self.assertEqual(
            (
                report["discovered"],
                report["selected"],
                report["summarized"],
                report["omitted"],
            ),
            (82, 64, 0, 18),
        )
        self.assertFalse(report["omitted_sample_complete"])
        self.assertTrue(report["lossy"])
        self.assertTrue(report["mandatory_satisfied"])
        ReviewRequest(
            diff="synthetic",
            active_category_ids=selection.active_category_ids,
            lens_contexts=selection.lens_contexts,
        )

    def test_existing_fitting_foundational_documents_are_kept(self):
        self.write("docs/foundation.md", "Dependencies point inward.")
        self.assertEqual(len(self.select().lens_contexts[0].documents), 1)

    def test_required_matches_and_present_explicit_files_fail_without_loss(self):
        for index in range(65):
            self.write(f"docs/{index}.md", "Mandatory rule.")
        with self.assertRaisesRegex(ContextLoadError, "required or explicit"):
            self.select(
                (
                    self.category(
                        sources=(ContextDocumentSource(path="docs", required=True),)
                    ),
                )
            )
        self.write("huge.md", "x" * (MAX_REVIEW_DOCUMENT_BYTES + 1))
        with self.assertRaisesRegex(ContextLoadError, "required or explicit"):
            self.select(
                (self.category(sources=(ContextDocumentSource(path="huge.md"),)),)
            )
        with self.assertRaises(ContextLoadError):
            self.select(
                (
                    self.category(
                        sources=(
                            ContextDocumentSource(path="missing.md", required=True),
                        )
                    ),
                )
            )

    def test_global_budget_deduplication_and_lens_ownership(self):
        for index in range(65):
            self.write(f"docs/shared-{index}.md", "Foundation.")
        self.write("security.md", "Security invariant.")
        categories = (
            self.category(),
            self.category(
                "security",
                sources=(
                    ContextDocumentSource(path="docs"),
                    ContextDocumentSource(path="security.md"),
                ),
            ),
        )
        selection = self.select(categories)
        first, second = selection.lens_contexts
        self.assertNotIn("security.md", {doc.path for doc in first.documents})
        self.assertIn("security.md", {doc.path for doc in second.documents})
        self.assertEqual(
            len(
                {doc.path for lens in selection.lens_contexts for doc in lens.documents}
            ),
            64,
        )
        shared_first = {doc.path: doc for doc in first.documents}
        self.assertTrue(
            all(
                shared_first[doc.path] == doc
                for doc in second.documents
                if doc.path in shared_first
            )
        )

    def test_extractive_summary_has_exact_source_provenance_and_loss(self):
        content = (
            "# Storage\n"
            + "Unrelated detail.\n" * 9000
            + "Payment rule: payments must remain idempotent.\nLast line.\n"
        )
        self.write("docs/payments.md", content)
        lens = self.select().lens_contexts[0]
        doc = lens.documents[0]
        decision = lens.document_selection.decisions[0]
        self.assertEqual(decision.status, "summarized")
        self.assertEqual(
            decision.source_sha256, hashlib.sha256(content.encode()).hexdigest()
        )
        self.assertEqual(doc.sha256, decision.payload_sha256)
        self.assertNotEqual(doc.sha256, decision.source_sha256)
        self.assertLessEqual(len(doc.content.encode()), 16 * 1024)
        self.assertEqual(
            doc.content,
            "\n".join(
                "".join(content.splitlines(keepends=True)[start - 1 : end])
                for start, end in decision.line_ranges
            ),
        )
        self.assertIn("idempotent", doc.content)
        self.assertTrue(lens.to_prompt_dict()["document_selection"]["lossy"])

    def test_failed_extract_and_huge_individual_docs_are_explicit_omissions(self):
        self.write(
            "docs/payments.md", "payments " + "x" * (MAX_REVIEW_DOCUMENT_BYTES + 1)
        )
        self.write("docs/huge.md", "x" * (MAX_INSPECT_DOCUMENT_BYTES + 1))
        self.write("docs/small.md", "payments useful context")
        lens = self.select().lens_contexts[0]
        reasons = {item.path: item.reason for item in lens.document_selection.decisions}
        self.assertEqual(reasons["docs/payments.md"], "extract-unavailable")
        self.assertEqual(reasons["docs/huge.md"], "inspection-file-limit")
        self.assertEqual([doc.path for doc in lens.documents], ["docs/small.md"])
        # A failed extractor cannot fall back to the oversized original.
        with patch("review_sensei.document_context._extract", return_value=None):
            self.assertEqual(self.select().lens_contexts[0].documents, lens.documents)

    def test_aggregate_bytes_extract_only_optional_documents(self):
        self.write("AGENTS.md", "Unabridged invariant.\n" * 1000)
        for index in range(6):
            self.write(
                f"docs/payments-{index}.md", "Detail.\n" * 17000 + "payments rule\n"
            )
        lens = self.select(
            (
                self.category(
                    sources=(
                        ContextDocumentSource(path="AGENTS.md"),
                        ContextDocumentSource(path="docs"),
                    )
                ),
            )
        ).lens_contexts[0]
        self.assertLessEqual(
            sum(len(doc.content.encode()) for doc in lens.documents), 512 * 1024
        )
        self.assertEqual(
            next(doc.content for doc in lens.documents if doc.path == "AGENTS.md"),
            (self.root / "AGENTS.md").read_text(),
        )
        self.assertGreater(lens.to_prompt_dict()["document_selection"]["summarized"], 0)

    def test_determinism_and_omission_changes_invalidate_cache_identity(self):
        for index in range(70):
            self.write(f"docs/{index:02}.md", "Noise.")
        self.write("docs/zzz-payments.md", "payments")
        category = self.category(
            sources=(
                ContextDocumentSource(path="docs"),
                ContextDocumentSource(path="docs"),
            )
        )
        first = self.select((category,), paths=("src/payments.py", "src/ledger.py"))
        second = self.select(
            (category,), paths=("src/ledger.py", "src/payments.py", "src/payments.py")
        )
        self.assertEqual(first, second)
        omitted = next(
            item.path
            for item in first.lens_contexts[0].document_selection.decisions
            if item.status == "omitted"
        )
        self.write(omitted, "Changed unrelated historical fact.")
        third = self.select((category,), paths=("src/payments.py", "src/ledger.py"))
        self.assertEqual(
            first.lens_contexts[0].documents, third.lens_contexts[0].documents
        )
        self.assertNotEqual(
            context_configuration_digest(first.lens_contexts),
            context_configuration_digest(third.lens_contexts),
        )

    def test_exhausted_discovery_and_inspection_fail_before_prefix_selection(self):
        self.write("docs/one.md", "context")
        self.write("docs/two.md", "context")
        with patch("review_sensei.context.MAX_DOCUMENT_CANDIDATES", 1):
            with self.assertRaisesRegex(ContextLoadError, "candidate budget"):
                self.select()
        with patch("review_sensei.context.MAX_DOCUMENT_DISCOVERY_ENTRIES", 1):
            with self.assertRaisesRegex(ContextLoadError, "entry budget"):
                self.select()
        with patch("review_sensei.document_context.MAX_INSPECTION_BYTES", 1):
            with self.assertRaisesRegex(ContextLoadError, "inspection byte budget"):
                self.select()

    def test_invalid_optional_inputs_cannot_bypass_permissions_through_ranking(self):
        self.write("docs/secrets.md", "Never transmit")
        with self.assertRaisesRegex(ContextLoadError, "secret-bearing"):
            self.select()
        (self.root / "docs/secrets.md").unlink()
        self.write("outside.md", "Outside selected source")
        (self.root / "docs/link.md").symlink_to(self.root / "outside.md")
        with self.assertRaisesRegex(ContextLoadError, "symlink"):
            self.select()

    def test_report_cannot_claim_documents_absent_from_payload(self):
        self.write("docs/one.md", "context")
        lens = self.select().lens_contexts[0]
        with self.assertRaises(ReviewInputError):
            ReviewLensContext(
                "architecture", document_selection=lens.document_selection
            )

    def test_directory_names_do_not_broaden_file_exclusion_patterns(self):
        self.write("docs/archive.md/rule.md", "Mandatory descendant")
        self.write("docs/drafts/nested/rule.md", "Another mandatory descendant")
        for exclude in (("*.md",), ("drafts/*",)):
            category = self.category(
                sources=(
                    ContextDocumentSource(path="docs", exclude=exclude, required=True),
                )
            )
            paths = {
                doc.path for doc in self.select((category,)).lens_contexts[0].documents
            }
            self.assertIn("docs/archive.md/rule.md", paths)
            self.assertIn("docs/drafts/nested/rule.md", paths)

    def test_deep_ancestor_guidance_is_pinned_only_for_applicable_lens_scope(self):
        for index in range(70):
            self.write(f"docs/aaa-{index:02}.md", "Noise.")
        self.write("docs/zzz/AGENTS.md", "Full ancestor guidance.")
        self.write("docs/other/AGENTS.md", "x" * (MAX_REVIEW_DOCUMENT_BYTES + 1))
        category = self.category(applies=("docs/zzz/**",))
        lens = self.select(
            (category,),
            paths=("docs/zzz/deep/payments.py", "docs/other/deep/ledger.py"),
        ).lens_contexts[0]
        decisions = {item.path: item for item in lens.document_selection.decisions}
        self.assertEqual(decisions["docs/zzz/AGENTS.md"].reason, "pinned")
        self.assertEqual(decisions["docs/zzz/AGENTS.md"].status, "selected")
        self.assertEqual(decisions["docs/other/AGENTS.md"].status, "omitted")

    def test_selected_and_extracted_source_changes_invalidate_context_cache(self):
        self.write("docs/payments.md", "payments rule")
        first = self.select().lens_contexts
        self.write("docs/payments.md", "Detail.\n" * 19000 + "payments rule\n")
        second = self.select().lens_contexts
        self.assertEqual(first[0].document_selection.decisions[0].status, "selected")
        self.assertEqual(second[0].document_selection.decisions[0].status, "summarized")
        self.assertNotEqual(
            context_configuration_digest(first), context_configuration_digest(second)
        )
        # Omitted source sections retain their own digest even if the quoted
        # window is unchanged.
        self.write("docs/payments.md", "Other.\n" * 19000 + "payments rule\n")
        third = self.select().lens_contexts
        self.assertNotEqual(
            context_configuration_digest(second), context_configuration_digest(third)
        )

    def test_exact_path_references_outrank_many_generic_token_matches(self):
        for index in range(64):
            self.write(
                f"docs/audit-billing-ledger-refund-shipping-inventory-{index}.md",
                "Foundational fact",
            )
        self.write("docs/zzz-guidance.md", "src/payments.py must validate idempotency.")
        paths = tuple(
            f"src/{stem}.py"
            for stem in (
                "payments",
                "audit",
                "billing",
                "ledger",
                "refund",
                "shipping",
                "inventory",
            )
        )
        lens = self.select(paths=paths).lens_contexts[0]
        self.assertIn("docs/zzz-guidance.md", {doc.path for doc in lens.documents})

    def test_narrow_include_does_not_probe_unrelated_symlink_directories(self):
        self.write("docs/guide.md", "Mandatory guidance")
        self.write("external/other.md", "Not selected")
        (self.root / "docs/unmatched-link").symlink_to(
            self.root / "external", target_is_directory=True
        )
        category = self.category(
            sources=(
                ContextDocumentSource(
                    path="docs", include=("guide.md",), required=True
                ),
            )
        )
        lens = self.select((category,)).lens_contexts[0]
        self.assertEqual([doc.path for doc in lens.documents], ["docs/guide.md"])

    def test_selection_report_rejects_omitted_or_summarized_mandatory_claims(self):
        from review_sensei.document_context import DocumentDecision

        with self.assertRaises(ReviewInputError):
            DocumentDecision("guide.md", "omitted", "pinned")
        with self.assertRaises(ReviewInputError):
            DocumentDecision(
                "guide.md", "summarized", "pinned", "a" * 64, "b" * 64, 10, ((1, 1),)
            )

    def test_duplicate_source_specs_share_discovery_across_lenses(self):
        for index in range(600):
            self.write(f"docs/{index:03}.md", "x")
        source = ContextDocumentSource(path="docs")
        one = self.select((self.category(sources=(source,)),)).lens_contexts[0]
        duplicated = self.select(
            (self.category(sources=(source,) * 32),)
        ).lens_contexts[0]
        self.assertEqual(one, duplicated)
        store = RepositoryContextStore(self.root)
        with patch.object(
            store, "discover_sources", wraps=store.discover_sources
        ) as discover:
            build_review_context_selection(
                (self.category(), self.category("security")),
                changed_paths=("src/payments.py",),
                context_store=store,
            )
        self.assertEqual(discover.call_count, 1)

    def test_safe_reads_do_not_leak_directory_handles(self):
        fd_root = (
            Path("/proc/self/fd") if Path("/proc/self/fd").is_dir() else Path("/dev/fd")
        )
        if not fd_root.is_dir():
            self.skipTest("platform does not expose file descriptor inventory")
        for index in range(10):
            self.write(f"docs/nested/{index}.md", "payments relevant rule")
        before = len(list(fd_root.iterdir()))
        for _ in range(5):
            self.assertEqual(len(self.select().lens_contexts[0].documents), 10)
        self.assertEqual(len(list(fd_root.iterdir())), before)

    def test_incremental_discovery_stops_before_materializing_large_directory(self):
        for index in range(10):
            self.write(f"docs/{index}.md", "context")
        original = os.scandir
        consumed = []

        class CountedEntries:
            def __init__(self, entries):
                self.entries = entries

            def __enter__(self):
                self.entries.__enter__()
                return self

            def __exit__(self, *args):
                return self.entries.__exit__(*args)

            def __iter__(self):
                for entry in self.entries:
                    consumed.append(entry.name)
                    yield entry

        with (
            patch(
                "review_sensei.context.os.scandir",
                side_effect=lambda directory: CountedEntries(original(directory)),
            ),
            patch("review_sensei.context.MAX_DOCUMENT_DISCOVERY_ENTRIES", 2),
        ):
            with self.assertRaises(ContextLoadError):
                self.select()
        self.assertEqual(len(consumed), 3)

    def test_provenance_rejects_wrong_full_bytes_and_unbounded_line_ranges(self):
        from review_sensei.document_context import (
            DocumentDecision,
            DocumentSelectionReport,
        )
        from review_sensei.models import ReviewDocument

        content = "payments"
        digest = hashlib.sha256(content.encode()).hexdigest()
        report = DocumentSelectionReport(
            (DocumentDecision("guide.md", "selected", "pinned", digest, digest, 1),)
        )
        with self.assertRaises(ReviewInputError):
            ReviewLensContext(
                "architecture",
                documents=(ReviewDocument("guide.md", content, digest),),
                document_selection=report,
            )
        with self.assertRaises(ReviewInputError):
            DocumentDecision(
                "guide.md",
                "summarized",
                "scope-match",
                digest,
                digest,
                8,
                ((1, 10**5000),),
            )

    def test_dense_line_extraction_has_bounded_fallback(self):
        self.write(
            "docs/payments.md",
            "\n" * (MAX_REVIEW_DOCUMENT_BYTES + 1) + "payments rule\n",
        )
        lens = self.select().lens_contexts[0]
        self.assertEqual(lens.documents, ())
        self.assertEqual(
            lens.document_selection.decisions[0].reason, "extract-unavailable"
        )

    def test_packaged_cli_path_selects_overflow_and_writes_metadata_only_sidecar(self):
        from test_cli import DIFF, FakeRegistry, SymbolContextProvider

        from review_sensei.cli import main

        for index in range(80):
            self.write(f"docs/adr/{index}.md", "Historical fact.")
        self.write("docs/adr/zzz-app.md", "src/app.py changed behavior")
        self.write("AGENTS.md", "Full guidance")
        diff = self.root / "synthetic.patch"
        diff.write_text(DIFF)
        output = self.root / "review.json"
        sidecar = self.root / "selection.json"
        provider = SymbolContextProvider()
        stderr = io.StringIO()
        with (
            patch(
                "review_sensei.cli.default_registry",
                return_value=FakeRegistry(provider),
            ),
            redirect_stderr(stderr),
        ):
            status = main(
                [
                    "--diff",
                    str(diff),
                    "--context-root",
                    str(self.root),
                    "--no-learning-proposals",
                    "--format",
                    "json",
                    "--output",
                    str(output),
                    "--context-selection-output",
                    str(sidecar),
                ]
            )
        self.assertEqual(status, 0, stderr.getvalue())
        self.assertIn("zzz-app.md", provider.requests[0].prompt)
        self.assertIn("document-context", stderr.getvalue())
        report = json.loads(sidecar.read_text())[0]
        self.assertEqual(report["discovered"], 82)
        self.assertEqual(report["selected"], 64)
        self.assertEqual(len(report["inventory"]), 82)
        self.assertNotIn("Full guidance", sidecar.read_text())
        self.assertNotIn("source_context", json.loads(output.read_text()))

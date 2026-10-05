import difflib
import hashlib
import json
import unittest
from dataclasses import replace

from review_sensei.context import (
    IncrementalReviewPlan,
    ReviewContextCache,
    build_review_context_cache_key,
    finding_lifecycle_for_comment,
)
from review_sensei.dependencies import dependency_review_note
from review_sensei.diff import analyze_diff
from review_sensei.errors import ProviderError
from review_sensei.models import ProviderResponse, ReviewComment, ReviewRequest
from review_sensei.planning import plan_change
from review_sensei.service import ReviewService
from review_sensei.validation import ReviewLimits


def patch(path, before, after, *, new_path=None, context=3):
    target = new_path or path
    return f"diff --git a/{path} b/{target}\n" + "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{target}",
            n=context,
        )
    )


def npm_update(version=3):
    # Thirteen distinct dependency hunks plus the manifest hunk reproduce the
    # triggering coverage shape using synthetic package metadata only.
    packages = {
        f"node_modules/fixture-{i}": {
            "version": "1.0.0",
            "resolved": f"https://example.invalid/{i}.tgz",
            "integrity": "sha512-old",
            "hasInstallScript": False,
            "dev": True,
            "license": "MIT",
            "engines": {"node": ">=20"},
        }
        for i in range(13)
    }
    before = {"lockfileVersion": version, "packages": packages}
    after = json.loads(json.dumps(before))
    for entry in after["packages"].values():
        entry.update(version="2.0.0", integrity="sha512-new", hasInstallScript=True)
    return patch(
        "deploy/cloudflare/package-lock.json",
        json.dumps(before, indent=2) + "\n",
        json.dumps(after, indent=2) + "\n",
        context=1,
    )


MANIFEST = patch(
    "deploy/cloudflare/package.json", '{"fixture":"1.0.0"}\n', '{"fixture":"2.0.0"}\n'
)
GENERATED = patch("dist/app.min.js", "old\n", "new\n")


class Provider:
    name = "fixture"
    model = "fixture"

    def __init__(self, fail_after=None):
        self.requests = []
        self.fail_after = fail_after

    def complete(self, request):
        self.requests.append(request)
        if self.fail_after is not None and len(self.requests) > self.fail_after:
            raise ProviderError("synthetic provider failure")
        return ProviderResponse(
            text='{"summary":"Synthetic review.","comments":[]}',
            provider=self.name,
        )


class DependencyReviewTests(unittest.TestCase):
    def test_npm_v2_v3_all_thirteen_hunks_are_really_sent_and_reviewed(self):
        for version in (2, 3):
            with self.subTest(version=version):
                diff = MANIFEST + npm_update(version)
                analysis = analyze_diff(diff)
                self.assertEqual(len(analysis.hunk_records), 14)
                provider = Provider()
                result = ReviewService(provider).review(ReviewRequest(diff=diff))
                self.assertEqual(result.review_status, "complete")
                self.assertTrue(result.coverage.fully_reviewed)
                self.assertEqual(len(result.coverage.hunks), 14)
                self.assertIn(diff, provider.requests[0].prompt)
                self.assertIn("install-script metadata", provider.requests[0].prompt)
                self.assertIn('"hasInstallScript": true', provider.requests[0].prompt)

    def test_evidence_is_deterministic_and_bound_to_original_hunk_side_line_digest(
        self,
    ):
        diff = patch("package-lock.json", '  "version": "1"\n', '  "version": "2"\n')
        analysis = analyze_diff(diff)
        note = dependency_review_note(analysis, max_bytes=12000)
        self.assertEqual(note, dependency_review_note(analysis, max_bytes=12000))
        payload = json.loads(note.split("\n")[-2])
        file = payload["files"][0]
        self.assertEqual(file["diff_sha256"], hashlib.sha256(diff.encode()).hexdigest())
        self.assertEqual(file["hunks"], [1])
        self.assertEqual(
            [(e["side"], e["line"]) for e in file["visible_members"]],
            [("LEFT", 1), ("RIGHT", 1)],
        )
        self.assertIn("lockfile version may be unknown", note)

    def test_added_removed_package_nodes_and_unknown_fields_keep_raw_material(self):
        diff = patch(
            "npm-shrinkwrap.json",
            '  "node_modules/old": {\n    "unexpected": "before"\n  }\n',
            '  "node_modules/new": {\n    "unexpected": "after"\n  }\n',
        )
        provider = Provider()
        result = ReviewService(provider).review(ReviewRequest(diff=diff))
        self.assertTrue(result.coverage.fully_reviewed)
        self.assertIn(diff, provider.requests[0].prompt)
        self.assertIn(
            "node_modules/old",
            dependency_review_note(analyze_diff(diff), max_bytes=12000),
        )
        self.assertIn("node_modules/new", provider.requests[0].prompt)

    def test_unsupported_formats_and_generated_exclusions_are_distinct(self):
        for name in (
            "yarn.lock",
            "pnpm-lock.yaml",
            "Cargo.lock",
            "poetry.lock",
            "composer.lock",
            "go.sum",
            "Gemfile.lock",
        ):
            with self.subTest(name=name):
                diff = MANIFEST + patch(name, "old\n", "new\n") + GENERATED
                result = ReviewService(Provider()).review(ReviewRequest(diff=diff))
                entries = {entry.path: entry for entry in result.coverage.files}
                self.assertEqual(result.review_status, "partial")
                self.assertFalse(result.coverage.fully_reviewed)
                self.assertEqual(
                    (entries[name].outcome, entries[name].reason),
                    ("unsupported", "lockfile-format"),
                )
                self.assertEqual(entries["dist/app.min.js"].reason, "generated")

    def test_annotation_limits_do_not_truncate_or_invalidate_raw_review(self):
        diff = npm_update()
        analysis = analyze_diff(diff)
        note = dependency_review_note(analysis, max_bytes=2000)
        self.assertLessEqual(len(note.encode()), 2000)
        self.assertIn('"annotation_truncated": true', note)
        self.assertEqual(dependency_review_note(analysis, max_bytes=1), "")
        provider = Provider()
        service = ReviewService(provider)
        request = ReviewRequest(diff=diff)
        service.review(request)
        prompt = provider.requests[0].prompt
        limit = len(prompt.split("\n\n<dependency-review>")[0].encode())
        result = service.review(
            replace(request, limits=ReviewLimits(max_prompt_bytes=limit))
        )
        self.assertTrue(result.coverage.fully_reviewed)
        self.assertIn(diff, provider.requests[1].prompt)
        self.assertEqual(len(provider.requests[1].prompt.encode()), limit)

    def test_malformed_compact_or_unrecognized_json_is_still_raw_review(self):
        for after in (
            '"bad\\q": 2\n',
            '{"version":"2","other":"new"}\n',
            "invalid JSON\n",
        ):
            with self.subTest(after=after):
                diff = patch("package-lock.json", "old\n", after)
                provider = Provider()
                result = ReviewService(provider).review(ReviewRequest(diff=diff))
                self.assertIn(diff, provider.requests[0].prompt)
                self.assertTrue(result.coverage.fully_reviewed)
                self.assertIn(
                    "not a full graph or validated JSON snapshot",
                    provider.requests[0].prompt,
                )

    def test_rename_to_unsupported_format_does_not_claim_npm_coverage(self):
        diff = patch("package-lock.json", "old\n", "new\n", new_path="yarn.lock")
        plan = plan_change(diff)
        self.assertFalse(plan.coverage.fully_reviewed)
        self.assertEqual(
            {entry.reason for entry in plan.coverage.files}, {"lockfile-format"}
        )

    def test_npm_lock_review_takes_precedence_over_generated_paths_on_either_side(self):
        for old, new in (
            ("dist/data.json", "package-lock.json"),
            ("package-lock.json", "dist/data.json"),
            ("generated/package-lock.json", "generated/package-lock.json"),
        ):
            with self.subTest(old=old, new=new):
                diff = patch(old, "old\n", "new\n", new_path=new)
                provider = Provider()
                result = ReviewService(provider).review(ReviewRequest(diff=diff))
                self.assertTrue(result.coverage.fully_reviewed)
                self.assertIn(diff, provider.requests[0].prompt)

    def test_deleted_npm_lockfile_is_reviewed_on_original_left_lines(self):
        diff = 'diff --git a/package-lock.json b/package-lock.json\ndeleted file mode 100644\n--- a/package-lock.json\n+++ /dev/null\n@@ -1 +0,0 @@\n-"version": "1"\n'
        provider = Provider()
        result = ReviewService(provider).review(ReviewRequest(diff=diff))
        self.assertTrue(result.coverage.fully_reviewed)
        self.assertIn('"side": "LEFT", "line": 1', provider.requests[0].prompt)

    def test_orchestrated_lock_hunks_keep_source_and_provider_failure_partial(self):
        diff = npm_update()
        limits = ReviewLimits(max_diff_bytes=1000)
        provider = Provider(fail_after=1)
        result = ReviewService(provider).review(
            ReviewRequest(diff=diff, limits=limits, orchestrate_large_changes=True)
        )
        self.assertEqual(result.review_status, "partial")
        self.assertFalse(result.coverage.fully_reviewed)
        self.assertTrue(any(e.outcome == "reviewed" for e in result.coverage.hunks))
        self.assertTrue(any(e.outcome != "reviewed" for e in result.coverage.hunks))
        self.assertNotIn("generated", {e.reason for e in result.coverage.hunks})
        self.assertIn(
            plan_change(diff, limits=limits, orchestrate=True).chunks[0].diff,
            provider.requests[0].prompt,
        )

    def test_partial_coverage_does_not_populate_complete_pass_cache(self):
        cache = ReviewContextCache()
        service = ReviewService(Provider(), cache=cache)
        request = ReviewRequest(
            diff=MANIFEST + GENERATED,
            repository="synthetic/repo",
            pull_request_number=1,
            base_sha="a" * 40,
            head_sha="b" * 40,
        )
        key = build_review_context_cache_key(
            request, provider_name="fixture", stages=service.stages
        )
        result = service.review(request)
        self.assertEqual(result.review_status, "partial")
        self.assertIsNone(cache.get(key))

    def test_unreviewed_generated_concern_cannot_be_resolved_or_evict_cached_pass(self):
        cache = ReviewContextCache()
        service = ReviewService(Provider(), cache=cache)
        request = ReviewRequest(
            diff=MANIFEST + GENERATED,
            repository="synthetic/repo",
            pull_request_number=1,
            base_sha="a" * 40,
            head_sha="c" * 40,
        )
        prior_key = build_review_context_cache_key(
            replace(request, head_sha="b" * 40),
            provider_name="fixture",
            stages=service.stages,
        )
        cache.put_if_newer(prior_key, (1, "full", 1), generation=1)
        prior = finding_lifecycle_for_comment(
            ReviewComment(
                path="dist/app.min.js",
                line=1,
                body="Synthetic concern.",
                symbol="fixture",
                defect_kind="compatibility",
            )
        )
        result = service.review(
            request,
            incremental=IncrementalReviewPlan(
                previous_key=prior_key,
                previous_findings=(prior,),
                reviewed_paths=("dist/app.min.js", "deploy/cloudflare/package.json"),
                evidence_confirmed_concerns=(prior.concern,),
                context_complete=True,
            ),
        )
        self.assertEqual(result.review_status, "partial")
        self.assertEqual(result.finding_lifecycles[0].state, "uncertain")
        self.assertEqual(cache.get(prior_key), (1, "full", 1))

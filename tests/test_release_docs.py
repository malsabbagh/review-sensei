from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator, ValidationError

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "release_docs", ROOT / "scripts/release_docs.py"
)
assert SPEC and SPEC.loader
DOCS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DOCS)


def run_record(tag="v0.6.17"):
    return {
        "id": 42,
        "path": ".github/workflows/release.yml",
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "repository": {"full_name": DOCS.REPOSITORY},
        "head_repository": {"full_name": DOCS.REPOSITORY},
        "pull_requests": [],
        "head_branch": tag,
        "head_sha": "a" * 40,
        "run_attempt": 2,
    }


def successful_jobs():
    return [
        {"name": name, "conclusion": "success", "run_attempt": 1}
        for name in DOCS.REQUIRED_JOBS
    ]


class ReleaseDocsQualificationTests(unittest.TestCase):
    def test_any_valid_version_with_required_release_evidence_qualifies(self):
        for tag in ("v0.0.0", "v0.1.2", "v0.6.16", "v0.6.17", "v1.2.3"):

            def release_api(path):
                if path == "actions/runs/42":
                    return run_record(tag)
                if path == f"releases/tags/{tag}":
                    return {"tag_name": tag, "draft": False, "prerelease": False}
                return self.qualify_api(path)

            with (
                self.subTest(tag=tag),
                patch.object(DOCS, "api", side_effect=release_api),
                patch.object(DOCS, "api_pages", side_effect=self.qualify_pages),
                patch.object(DOCS, "tag_identity", return_value=("b" * 40, "a" * 40)),
            ):
                selection = DOCS.qualify(42)
                self.assertEqual(selection["tag"], tag)
                self.assertEqual(selection["version"], tag[1:])

    def test_historical_run_without_docs_job_or_artifact_fails_on_evidence(self):
        tag = "v0.6.16"
        for missing in ("docs-job", "docs-artifact"):

            def release_api(path):
                if path == "actions/runs/42":
                    return run_record(tag)
                if path == f"releases/tags/{tag}":
                    return {"tag_name": tag, "draft": False, "prerelease": False}
                return self.qualify_api(path)

            def release_pages(path, key=None):
                entries = self.qualify_pages(path, key)
                if missing == "docs-job" and key == "jobs":
                    return [
                        job
                        for job in entries
                        if job["name"] != "Build release documentation"
                    ]
                if missing == "docs-artifact" and key == "artifacts":
                    return []
                return entries

            expected = (
                "Build release documentation"
                if missing == "docs-job"
                else "retained release docs artifact"
            )
            with (
                self.subTest(missing=missing),
                patch.object(DOCS, "api", side_effect=release_api),
                patch.object(DOCS, "api_pages", side_effect=release_pages),
                patch.object(DOCS, "tag_identity", return_value=("b" * 40, "a" * 40)),
                self.assertRaisesRegex(ValueError, expected),
            ):
                DOCS.qualify(42)

    def test_post_approval_recheck_rejects_changed_identity_or_newer_release(self):
        selection = {"release_run_id": 42, "artifact_id": 99, "tag": "v0.6.17"}
        argv = ["release_docs.py", "recheck", "--selection-json", json.dumps(selection)]
        with (
            patch.object(sys, "argv", argv),
            patch.object(DOCS, "qualify", return_value=selection),
            patch.object(DOCS, "assert_latest"),
        ):
            self.assertEqual(DOCS.main(), 0)
        for kind in ("changed-artifact", "newer-release"):
            current = dict(selection)
            if kind == "changed-artifact":
                current["artifact_id"] = 100
            with (
                self.subTest(kind=kind),
                patch.object(sys, "argv", argv),
                patch.object(DOCS, "qualify", return_value=current),
                patch.object(
                    DOCS,
                    "assert_latest",
                    side_effect=ValueError("newer release")
                    if kind == "newer-release"
                    else None,
                ),
                patch.object(sys, "stderr", io.StringIO()),
            ):
                self.assertEqual(DOCS.main(), 1)

    def test_publisher_only_retry_retains_earlier_docs_and_other_publications(self):
        jobs = successful_jobs()
        name = "Publish npm packages with Trusted Publishing"
        next(job for job in jobs if job["name"] == name)["conclusion"] = "failure"
        jobs.append({"name": name, "conclusion": "success", "run_attempt": 2})
        DOCS.validate_run(run_record(), jobs)
        jobs[-1]["conclusion"] = "failure"
        with self.assertRaises(ValueError):
            DOCS.validate_run(run_record(), jobs)

    def test_successful_tag_run_requires_all_publication_lanes(self):
        DOCS.validate_run(run_record(), successful_jobs())
        for name in DOCS.REQUIRED_JOBS:
            for conclusion in ("skipped", "failure", "cancelled", None):
                jobs = successful_jobs()
                next(job for job in jobs if job["name"] == name)["conclusion"] = (
                    conclusion
                )
                with (
                    self.subTest(name=name, conclusion=conclusion),
                    self.assertRaises(ValueError),
                ):
                    DOCS.validate_run(run_record(), jobs)
        with self.assertRaises(ValueError):
            DOCS.validate_run(run_record(), successful_jobs() * 2)

    def test_wrong_workflow_branch_fork_pr_or_incomplete_run_is_rejected(self):
        for key, value in (
            ("path", ".github/workflows/ci.yml"),
            ("event", "pull_request"),
            ("event", "workflow_dispatch"),
            ("head_branch", "main"),
            ("head_branch", "v5"),
            ("head_branch", "v0.6.17-rc1"),
            ("head_branch", "v00.6.17"),
            ("head_sha", "main"),
            ("status", "in_progress"),
            ("conclusion", "failure"),
            ("conclusion", "cancelled"),
            ("repository", {"full_name": "attacker/fork"}),
            ("head_repository", {"full_name": "attacker/fork"}),
            ("pull_requests", [{"number": 1}]),
        ):
            record = run_record()
            record[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                DOCS.validate_run(record, successful_jobs())

    def tag_api(self, endpoint):
        if endpoint.startswith("git/ref/"):
            return {
                "ref": "refs/tags/v0.6.17",
                "object": {"type": "tag", "sha": "b" * 40},
            }
        return {
            "tag": "v0.6.17",
            "object": {"type": "commit", "sha": "a" * 40},
            "verification": {"verified": True},
        }

    def test_only_verified_annotated_commit_tags_are_accepted(self):
        with patch.object(DOCS, "api", side_effect=self.tag_api):
            self.assertEqual(DOCS.tag_identity("v0.6.17"), ("b" * 40, "a" * 40))
        for endpoint, field, value in (
            ("ref", "object", {"type": "commit", "sha": "a" * 40}),
            ("ref", "ref", "refs/heads/v0.6.17"),
            ("tag", "verification", {"verified": False}),
            ("tag", "verification", {}),
            ("tag", "object", {"type": "tag", "sha": "a" * 40}),
            ("tag", "tag", "v0.6.18"),
        ):

            def changed_api(path):
                result = self.tag_api(path)
                if (endpoint == "ref") == path.startswith("git/ref/"):
                    result[field] = value
                return result

            with (
                self.subTest(endpoint=endpoint, field=field),
                patch.object(DOCS, "api", side_effect=changed_api),
                self.assertRaises(ValueError),
            ):
                DOCS.tag_identity("v0.6.17")

    def qualify_api(self, path):
        if path == "actions/runs/42":
            return run_record()
        if path.startswith("compare/"):
            return {"status": "ahead"}
        if path.startswith("releases/tags/"):
            return {"tag_name": "v0.6.17", "draft": False, "prerelease": False}
        raise AssertionError(path)

    def qualify_pages(self, path, key=None):
        if key == "jobs":
            return successful_jobs()
        if key == "artifacts":
            return [{"id": 99, "name": DOCS.ARTIFACT, "expired": False}]
        raise AssertionError(path)

    def qualify(self):
        with (
            patch.object(DOCS, "api", side_effect=self.qualify_api),
            patch.object(DOCS, "api_pages", side_effect=self.qualify_pages),
            patch.object(DOCS, "tag_identity", return_value=("b" * 40, "a" * 40)),
        ):
            return DOCS.qualify(42)

    def test_qualification_binds_run_artifact_and_source(self):
        selection = self.qualify()
        self.assertEqual(selection["release_run_id"], 42)
        self.assertEqual(selection["artifact_id"], 99)
        self.assertEqual(selection["source_sha"], "a" * 40)

    def test_moved_tag_unmerged_source_draft_release_and_expired_bundle_fail(self):
        for kind in (
            "moved-tag",
            "unmerged",
            "draft",
            "prerelease",
            "expired",
            "duplicate",
            "wrong-id",
        ):

            def changed_api(path):
                result = self.qualify_api(path)
                if path.startswith("compare/") and kind == "unmerged":
                    result["status"] = "diverged"
                if path.startswith("releases/") and kind in {"draft", "prerelease"}:
                    result[kind] = True
                if path == "actions/runs/42" and kind == "wrong-id":
                    result["id"] = 43
                return result

            def changed_pages(path, key=None):
                result = self.qualify_pages(path, key)
                if key == "artifacts":
                    if kind == "expired":
                        result[0]["expired"] = True
                    if kind == "duplicate":
                        result *= 2
                return result

            with (
                self.subTest(kind=kind),
                patch.object(DOCS, "api", side_effect=changed_api),
                patch.object(DOCS, "api_pages", side_effect=changed_pages),
                patch.object(
                    DOCS,
                    "tag_identity",
                    return_value=("b" * 40, ("c" if kind == "moved-tag" else "a") * 40),
                ),
                self.assertRaises(ValueError),
            ):
                DOCS.qualify(42)

    def test_supersession_uses_numeric_versions_successful_runs_and_all_pages(self):
        # v0.6.20 is newer numerically; pending/failed higher releases do not
        # suppress the last successful cutoff. Unrelated releases are ignored.
        releases = [
            {"tag_name": "v0.6.20", "draft": False, "prerelease": False},
            {"tag_name": "v5"},
            {"tag_name": "v0.6.18", "draft": True},
        ]

        def history(path, key=None):
            if path == "releases":
                return releases
            if key == "workflow_runs":
                self.assertIn("branch=v0.6.20", path)
                return [run_record("v0.6.20")]
            return successful_jobs()

        with (
            patch.object(DOCS, "api_pages", side_effect=history),
            self.assertRaisesRegex(ValueError, "newer successful release"),
        ):
            DOCS.assert_latest({"tag": "v0.6.17"})
        with patch.object(
            DOCS,
            "api_pages",
            side_effect=lambda path, key=None: releases if path == "releases" else [],
        ):
            DOCS.assert_latest({"tag": "v0.6.17"})
        with patch.object(
            DOCS, "api", side_effect=[[{"id": i} for i in range(100)], [{"id": 100}]]
        ) as api:
            self.assertEqual(len(DOCS.api_pages("releases")), 101)
            self.assertIn("page=2", api.call_args.args[0])


class ReleaseDocsBuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name) / "source"
        cls.root.mkdir()
        # Exercise the actual builder, schema, manifest and generated pages
        # using a synthetic reviewed 0.6.17 source tree, without creating tags.
        for directory in (
            "scripts",
            "docs",
            "packages/npm",
            "src",
            ".github",
            "examples",
            "tests",
        ):
            shutil.copytree(
                ROOT / directory,
                cls.root / directory,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
        for filename in ("pyproject.toml", "CHANGELOG.md", "README.md", ".gitignore"):
            shutil.copy(ROOT / filename, cls.root / filename)
        version = DOCS.load_script(ROOT, "check_release_version").project_version(ROOT)
        paths = [
            cls.root / "pyproject.toml",
            cls.root / "CHANGELOG.md",
            cls.root / "docs/site/data/site-manifest.json",
            cls.root / "docs/site/getting-started/index.html",
            *list((cls.root / "packages/npm").rglob("package.json")),
        ]
        for path in paths:
            path.write_text(
                path.read_text(encoding="utf-8").replace(version, "0.6.17"),
                encoding="utf-8",
            )
        builder = DOCS.load_script(cls.root, "build_site_pages")
        builder.build_site_pages()
        for args in (
            ("init", "-q"),
            ("add", "."),
            (
                "-c",
                "user.name=Docs Test",
                "-c",
                "user.email=docs@example.test",
                "commit",
                "-qm",
                "Synthetic release source",
            ),
        ):
            subprocess.run(
                ["git", "-C", str(cls.root), *args], check=True, capture_output=True
            )
        cls.sha = subprocess.check_output(
            ["git", "-C", str(cls.root), "rev-parse", "HEAD"], text=True
        ).strip()
        cls.tag_sha = "b" * 40
        cls.bundle = Path(cls.temporary.name) / "bundle"
        cls.build(cls.bundle)
        cls.selection = {
            "schema_version": "1.0",
            "repository": DOCS.REPOSITORY,
            "version": "0.6.17",
            "tag": "v0.6.17",
            "source_sha": cls.sha,
            "tag_object_sha": cls.tag_sha,
            "release_run_id": 42,
            "release_run_attempt": 2,
        }

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    @classmethod
    def build(cls, output):
        real_check = subprocess.check_output

        def git_output(args, **kwargs):
            if args[-1] == "refs/tags/v0.6.17":
                return cls.tag_sha + "\n"
            return real_check(args, **kwargs)

        with (
            patch.dict(os.environ, {"GITHUB_SHA": cls.sha}),
            patch.object(DOCS, "tag_identity", return_value=(cls.tag_sha, cls.sha)),
            patch.object(DOCS.subprocess, "check_output", side_effect=git_output),
        ):
            DOCS.build(cls.root, output, "v0.6.17", 42, 1)

    def test_actual_build_pins_links_examples_version_and_preserves_source(self):
        before = subprocess.check_output(
            ["git", "-C", str(self.root), "status", "--porcelain"], text=True
        )
        DOCS.verify_bundle(self.bundle, self.selection)
        for path in (self.bundle / "site").rglob("*.html"):
            html = path.read_text(encoding="utf-8")
            self.assertNotIn(f"https://github.com/{DOCS.REPOSITORY}/blob/main/", html)
            self.assertNotIn(f"https://github.com/{DOCS.REPOSITORY}/tree/main/", html)
            self.assertIn("Released docs:", html)
            self.assertIn("v0.6.17", html)
        home = (self.bundle / "site/index.html").read_text(encoding="utf-8")
        self.assertIn("pip install review-sensei==0.6.17", home)
        onboarding = (self.bundle / "site/getting-started/index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("@reviewsensei/cli@0.6.17", onboarding)
        self.assertIn("review-sensei-run.yml@v5", onboarding)
        examples = (self.bundle / "site/examples/index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn(f"blob/{self.sha}/docs/public-contracts.md", examples)
        self.assertEqual(before, "")
        self.assertEqual(
            subprocess.check_output(
                ["git", "-C", str(self.root), "status", "--porcelain"], text=True
            ),
            "",
        )

    def test_actual_builder_uses_arbitrary_input_tag_without_version_floor(self):
        for version in ("0.1.2", "0.6.16", "1.2.3"):
            with (
                self.subTest(version=version),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary) / "source"
                shutil.copytree(
                    self.root,
                    root,
                    ignore=shutil.ignore_patterns(".git", "__pycache__"),
                )
                paths = [
                    root / "pyproject.toml",
                    root / "CHANGELOG.md",
                    root / "docs/site/data/site-manifest.json",
                    root / "docs/site/getting-started/index.html",
                    *list((root / "packages/npm").rglob("package.json")),
                ]
                for path in paths:
                    path.write_text(
                        path.read_text(encoding="utf-8").replace("0.6.17", version),
                        encoding="utf-8",
                    )
                DOCS.load_script(root, "build_site_pages").build_site_pages()
                for args in (
                    ("init", "-q"),
                    ("add", "."),
                    (
                        "-c",
                        "user.name=Docs Test",
                        "-c",
                        "user.email=docs@example.test",
                        "commit",
                        "-qm",
                        "Synthetic tagged source",
                    ),
                ):
                    subprocess.run(
                        ["git", "-C", str(root), *args], check=True, capture_output=True
                    )
                source_sha = subprocess.check_output(
                    ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
                ).strip()
                tag = f"v{version}"
                real_check = subprocess.check_output

                def git_output(args, **kwargs):
                    if args[-1] == f"refs/tags/{tag}":
                        return self.tag_sha + "\n"
                    return real_check(args, **kwargs)

                output = Path(temporary) / "bundle"
                with (
                    patch.dict(os.environ, {"GITHUB_SHA": source_sha}),
                    patch.object(
                        DOCS, "tag_identity", return_value=(self.tag_sha, source_sha)
                    ),
                    patch.object(
                        DOCS.subprocess, "check_output", side_effect=git_output
                    ),
                ):
                    DOCS.build(root, output, tag, 42, 1)
                provenance = json.loads(
                    (output / "site/data/release-provenance.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(provenance["tag"], tag)
                self.assertEqual(provenance["version"], version)
                self.assertEqual(provenance["source_sha"], source_sha)
                self.assertIn(
                    f"pip install review-sensei=={version}",
                    (output / "site/index.html").read_text(encoding="utf-8"),
                )

    def test_tag_build_derives_release_metadata_and_pins_from_older_source_docs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            shutil.copytree(
                self.root, root, ignore=shutil.ignore_patterns(".git", "__pycache__")
            )
            manifest_path = root / "docs/site/data/site-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["release_facts"].update(version="0.6.16", tag="v0.6.16")
            manifest_path.write_text(
                json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
            )
            onboarding = root / "docs/site/getting-started/index.html"
            onboarding.write_text(
                onboarding.read_text(encoding="utf-8").replace("0.6.17", "0.6.16"),
                encoding="utf-8",
            )
            DOCS.load_script(root, "build_site_pages").build_site_pages()
            for args in (
                ("init", "-q"),
                ("add", "."),
                (
                    "-c",
                    "user.name=Docs Test",
                    "-c",
                    "user.email=docs@example.test",
                    "commit",
                    "-qm",
                    "Versioned package with older source docs",
                ),
            ):
                subprocess.run(
                    ["git", "-C", str(root), *args], check=True, capture_output=True
                )
            sha = subprocess.check_output(
                ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
            ).strip()
            real_check = subprocess.check_output

            def git_output(args, **kwargs):
                if args[-1] == "refs/tags/v0.6.17":
                    return self.tag_sha + "\n"
                return real_check(args, **kwargs)

            output = Path(temporary) / "bundle"
            with (
                patch.dict(os.environ, {"GITHUB_SHA": sha}),
                patch.object(DOCS, "tag_identity", return_value=(self.tag_sha, sha)),
                patch.object(DOCS.subprocess, "check_output", side_effect=git_output),
            ):
                DOCS.build(root, output, "v0.6.17", 42, 1)
            DOCS.verify_bundle(output, dict(self.selection, source_sha=sha))
            staged = json.loads((output / "site/data/site-manifest.json").read_text())
            self.assertEqual(staged["release_facts"]["version"], "0.6.17")
            self.assertEqual(
                json.loads(manifest_path.read_text())["release_facts"]["version"],
                "0.6.16",
            )
            self.assertIn(
                "@reviewsensei/cli@0.6.17",
                (output / "site/getting-started/index.html").read_text(),
            )
            self.assertIn("@reviewsensei/cli@0.6.16", onboarding.read_text())
            self.assertEqual(
                subprocess.check_output(
                    ["git", "-C", str(root), "status", "--porcelain"], text=True
                ),
                "",
            )

    def test_provenance_schema_rejects_missing_identity_or_mutable_source(self):
        schema = json.loads(
            (self.bundle / "site/schemas/release-provenance.schema.json").read_text(
                encoding="utf-8"
            )
        )
        provenance = json.loads(
            (self.bundle / "site/data/release-provenance.json").read_text(
                encoding="utf-8"
            )
        )
        validator = Draft202012Validator(schema)
        validator.validate(provenance)
        for key, value in (
            ("source_sha", "main"),
            (
                "source_examples",
                f"https://github.com/{DOCS.REPOSITORY}/tree/main/examples",
            ),
            ("build_run_attempt", 0),
        ):
            changed = dict(provenance)
            changed[key] = value
            with self.subTest(key=key), self.assertRaises(ValidationError):
                validator.validate(changed)

    def test_bundle_fails_for_changed_bytes_missing_file_extra_file_or_symlink(self):
        for kind in ("changed", "missing", "extra", "symlink", "extra-root"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                bundle = Path(temp) / "bundle"
                shutil.copytree(self.bundle, bundle)
                if kind == "changed":
                    (bundle / "site/index.html").write_text("changed", encoding="utf-8")
                elif kind == "missing":
                    (bundle / "site/index.html").unlink()
                elif kind == "extra":
                    (bundle / "site/extra.js").write_text("extra", encoding="utf-8")
                elif kind == "extra-root":
                    (bundle / "extra").write_text("extra", encoding="utf-8")
                else:
                    (bundle / "site/link").symlink_to(bundle / "site/index.html")
                with self.assertRaises(ValueError):
                    DOCS.verify_bundle(bundle, self.selection)

    def test_wrong_source_tag_version_run_or_attempt_fail_even_with_matching_bytes(
        self,
    ):
        for key, value in (
            ("source_sha", "c" * 40),
            ("tag_object_sha", "c" * 40),
            ("tag", "v0.6.18"),
            ("version", "0.6.18"),
            ("release_run_id", 43),
            ("release_run_attempt", 0),
        ):
            selection = dict(self.selection)
            selection[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                DOCS.verify_bundle(self.bundle, selection)
        # The artifact can have been built on attempt 1 while the failed npm
        # publisher was successfully re-run on attempt 2. No rebuild needed.
        DOCS.verify_bundle(self.bundle, self.selection)

    def test_repeated_build_is_deterministic_and_refuses_dirty_source(self):
        with tempfile.TemporaryDirectory() as temp:
            other = Path(temp) / "bundle"
            self.build(other)
            self.assertEqual(
                DOCS.file_inventory(self.bundle), DOCS.file_inventory(other)
            )
        dirty = self.root / "docs/site/unreviewed.html"
        try:
            dirty.write_text("unreviewed", encoding="utf-8")
            with (
                tempfile.TemporaryDirectory() as temp,
                self.assertRaisesRegex(ValueError, "clean source"),
            ):
                self.build(Path(temp) / "bundle")
        finally:
            dirty.unlink()

    def test_tag_version_disagreement_fails_before_writing(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            self.assertRaisesRegex(ValueError, "project metadata"),
        ):
            DOCS.build(self.root, Path(temp) / "bundle", "v0.6.18", 42, 1)

    def test_stale_onboarding_installation_pins_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            site = Path(temporary)
            for snippet in (
                "pip install review-sensei==0.6.16",
                "npx --yes @reviewsensei/cli@0.6.16",
                "Release 0.6.16",
            ):
                (site / "index.html").write_text(snippet, encoding="utf-8")
                with (
                    self.subTest(snippet=snippet),
                    self.assertRaisesRegex(ValueError, "installation version"),
                ):
                    DOCS.validate_installation_versions(site, "0.6.17")


class ReleaseDocsWorkflowTests(unittest.TestCase):
    def test_release_docs_build_is_read_only_and_uses_event_sha(self):
        workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
        docs_job = workflow.split("  docs-build:\n", 1)[1].split("\n  build:\n", 1)[0]
        self.assertIn("ref: ${{ github.sha }}", docs_job)
        self.assertIn("persist-credentials: false", docs_job)
        self.assertIn("contents: read", docs_job)
        self.assertNotIn("id-token:", docs_job)
        self.assertNotIn("secrets.", docs_job)
        self.assertIn("include-hidden-files: true", docs_job)

    def test_pages_never_executes_released_code_or_grants_build_oidc(self):
        workflow = (ROOT / ".github/workflows/pages.yml").read_text(encoding="utf-8")
        self.assertIn("workflows: [Release]", workflow)
        self.assertNotIn("workflows: [CI]", workflow)
        self.assertNotIn("branches: [main]", workflow)
        self.assertNotIn("secrets.", workflow)
        qualifier, remaining = workflow.split("\n  deploy:\n", 1)
        deploy, update = remaining.split("\n  update-main:\n", 1)
        self.assertNotIn("pages: write", qualifier)
        self.assertNotIn("id-token: write", qualifier)
        self.assertIn("ref: ${{ github.sha }}", qualifier)
        self.assertIn("artifact-ids:", qualifier)
        self.assertIn("ref: ${{ github.sha }}", deploy)
        self.assertNotIn("head_sha", deploy)
        self.assertNotIn("download-artifact@", deploy)
        self.assertNotIn("release_docs.py build", deploy)
        self.assertIn("release_docs.py recheck", deploy)
        self.assertLess(
            deploy.index("release_docs.py recheck"),
            deploy.index("actions/deploy-pages@"),
        )
        self.assertIn("needs: qualify", deploy)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertNotIn("git push", workflow)
        self.assertIn("contents: write", update)
        self.assertIn("actions: read", update)
        self.assertNotIn("pages: write", update)
        self.assertNotIn("id-token: write", update)
        self.assertIn("digest-mismatch: error", update)
        self.assertIn("ref: ${{ github.sha }}", update)
        self.assertIn("persist-credentials: false", update)
        self.assertIn("sync_release_docs.py", update)


if __name__ == "__main__":
    unittest.main()

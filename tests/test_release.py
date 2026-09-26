from __future__ import annotations

import importlib.util
import io
import json
import re
import shutil
import tarfile
import tempfile
import tomllib
import unittest
import warnings
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VERSION_CHECK = load_script("check_release_version.py")
ARTIFACT_CHECK = load_script("validate_release.py")
RESUME_RUN_CHECK = load_script("validate_npm_resume_run.py")


class ReleaseMetadataTests(unittest.TestCase):
    def test_pyproject_is_canonical_release_metadata(self):
        with (ROOT / "pyproject.toml").open("rb") as handle:
            document = tomllib.load(handle)
        project = document["project"]
        self.assertEqual(project["version"], "0.6.8")
        self.assertEqual(project["requires-python"], ">=3.11")
        self.assertEqual(project["license"], "MIT")
        self.assertEqual(project["license-files"], ["LICENSE"])
        for key in (
            "Homepage",
            "Source",
            "Issues",
            "Documentation",
            "Changelog",
            "Security",
        ):
            self.assertIn(key, project["urls"])
        package_data = document["tool"]["setuptools"]["package-data"]["review_sensei"]
        self.assertIn("default_categories/*.json", package_data)
        self.assertIn("default_stages/*.json", package_data)

    def test_setup_py_has_no_duplicated_project_metadata(self):
        source = (ROOT / "setup.py").read_text(encoding="utf-8")
        for field in (
            "name=",
            "version=",
            "description=",
            "package_data",
            "entry_points=",
        ):
            self.assertNotIn(field, source)
        self.assertIn("from setuptools import setup", source)
        self.assertIn("setup()", source)

    def test_manifest_covers_release_documents_without_generated_output(self):
        source = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        self.assertIn("graft docs", source)
        self.assertIn("graft examples", source)
        self.assertIn("prune src/review_sensei.egg-info", source)
        self.assertIn("prune tests", source)
        self.assertIn("graft tests/dist_safe", source)
        self.assertIn("graft tests/downstream", source)


class ReleaseVersionTests(unittest.TestCase):
    def test_matching_tag_and_dated_heading_pass(self):
        self.assertEqual(VERSION_CHECK.validate_release_tag("v0.6.8", ROOT), "0.6.8")

    def test_tag_requires_v_prefix_and_exact_metadata(self):
        with self.assertRaises(VERSION_CHECK.ReleaseVersionError):
            VERSION_CHECK.validate_release_tag("0.1.0", ROOT)
        with self.assertRaises(VERSION_CHECK.ReleaseVersionError):
            VERSION_CHECK.validate_release_tag("v0.2.0", ROOT)

    def test_missing_changelog_heading_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pyproject.toml").write_text(
                '[project]\nversion = "0.1.0"\n', encoding="utf-8"
            )
            (root / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")
            with self.assertRaises(VERSION_CHECK.ReleaseVersionError):
                VERSION_CHECK.validate_release_tag("v0.1.0", root)

    def test_npm_manifests_are_checked_when_tree_exists(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copy(ROOT / "pyproject.toml", root / "pyproject.toml")
            (root / "CHANGELOG.md").write_text(
                "## 0.6.8 - 2026-09-26\n", encoding="utf-8"
            )
            shutil.copytree(ROOT / "packages/npm", root / "packages/npm")
            self.assertEqual(
                VERSION_CHECK.validate_release_tag("v0.6.8", root), "0.6.8"
            )
            launcher = root / "packages/npm/cli/package.json"
            value = json.loads(launcher.read_text(encoding="utf-8"))
            value["optionalDependencies"]["@reviewsensei/cli-linux-x64-gnu"] = "9.9.9"
            launcher.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(VERSION_CHECK.ReleaseVersionError):
                VERSION_CHECK.validate_release_tag("v0.1.0", root)
            value["optionalDependencies"]["@reviewsensei/cli-linux-x64-gnu"] = "0.1.0"
            value["repository"] = {"url": "https://example.invalid/repo"}
            launcher.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(VERSION_CHECK.ReleaseVersionError):
                VERSION_CHECK.validate_release_tag("v0.1.0", root)


def _add_tar_file(archive: tarfile.TarFile, name: str, content: bytes = b"x") -> None:
    info = tarfile.TarInfo(name)
    info.size = len(content)
    archive.addfile(info, io.BytesIO(content))


def _write_sdist(directory: Path, *, traversal: bool = False) -> Path:
    path = directory / "review_sensei-0.1.0.tar.gz"
    root = "review_sensei-0.1.0/"
    with tarfile.open(path, "w:gz") as archive:
        for name in (
            "pyproject.toml",
            "README.md",
            "LICENSE",
            "CHANGELOG.md",
            "docs/releasing.md",
            "examples/stages/example.json",
            "src/review_sensei/default_categories/01-correctness.json",
            "src/review_sensei/default_categories/02-security.json",
            "src/review_sensei/default_categories/03-architecture.json",
            "src/review_sensei/default_categories/03-maintainability.json",
            "src/review_sensei/default_categories/04-tests.json",
            "src/review_sensei/default_stages/01-default-review.json",
        ):
            _add_tar_file(archive, root + name)
        if traversal:
            _add_tar_file(archive, root + "../escape.txt")
    return path


def _write_wheel(
    directory: Path,
    *,
    missing_asset: bool = False,
    traversal: bool = False,
    duplicate: bool = False,
    metadata_version: str = "0.1.0",
) -> Path:
    path = directory / "review_sensei-0.1.0-py3-none-any.whl"
    dist_info = "review_sensei-0.1.0.dist-info"
    metadata = (
        "Metadata-Version: 2.4\n"
        "Name: review-sensei\n"
        f"Version: {metadata_version}\n"
        "License-Expression: MIT\n"
        "Requires-Python: >=3.11\n\n"
    ).encode()
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{dist_info}/METADATA", metadata)
        archive.writestr(f"{dist_info}/WHEEL", b"Wheel-Version: 1.0\n")
        archive.writestr(f"{dist_info}/RECORD", b"")
        archive.writestr(f"{dist_info}/licenses/LICENSE", b"MIT License")
        for relative in ARTIFACT_CHECK.DEFAULT_PACKAGE_FILES:
            if missing_asset and relative == "default_stages/01-default-review.json":
                continue
            archive.writestr(f"review_sensei/{relative}", b"{}")
        if traversal:
            archive.writestr("../escape.txt", b"x")
        if duplicate:
            archive.writestr(f"{dist_info}/RECORD", b"")
    return path


class ReleaseArtifactTests(unittest.TestCase):
    def test_archive_path_validation_does_not_normalize_unsafe_segments(self):
        for name in (
            "root//file",
            "root/./file",
            "root/../file",
            "C:file.txt",
            "C:../file.txt",
        ):
            self.assertFalse(ARTIFACT_CHECK._safe_member(name))

    def test_valid_sdist_and_wheel_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _write_sdist(directory)
            _write_wheel(directory)
            ARTIFACT_CHECK.validate_release_directory(directory, "0.1.0")

    def test_valid_macos_platform_tag_passes(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _write_sdist(directory)
            wheel = _write_wheel(directory)
            wheel.rename(
                directory / "review_sensei-0.1.0-py3-none-macosx_10_9_x86_64.whl"
            )
            ARTIFACT_CHECK.validate_release_directory(directory, "0.1.0")

    def test_non_release_version_wheel_name_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _write_sdist(directory)
            wheel = _write_wheel(directory)
            wheel.rename(directory / "review_sensei-0.1.0a1-py3-none-any.whl")
            with self.assertRaises(ARTIFACT_CHECK.ReleaseArtifactError):
                ARTIFACT_CHECK.validate_release_directory(directory)

    def test_missing_asset_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _write_sdist(directory)
            _write_wheel(directory, missing_asset=True)
            with self.assertRaises(ARTIFACT_CHECK.ReleaseArtifactError):
                ARTIFACT_CHECK.validate_release_directory(directory, "0.1.0")

    def test_unsafe_archive_paths_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _write_sdist(directory, traversal=True)
            _write_wheel(directory)
            with self.assertRaises(ARTIFACT_CHECK.ReleaseArtifactError):
                ARTIFACT_CHECK.validate_release_directory(directory, "0.1.0")

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _write_sdist(directory)
            _write_wheel(directory, traversal=True)
            with self.assertRaises(ARTIFACT_CHECK.ReleaseArtifactError):
                ARTIFACT_CHECK.validate_release_directory(directory, "0.1.0")

    def test_duplicate_and_mismatched_artifacts_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _write_sdist(directory)
            _write_wheel(directory)
            (directory / "second.whl").write_bytes(
                (directory / "review_sensei-0.1.0-py3-none-any.whl").read_bytes()
            )
            with self.assertRaises(ARTIFACT_CHECK.ReleaseArtifactError):
                ARTIFACT_CHECK.validate_release_directory(directory, "0.1.0")

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _write_sdist(directory)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                _write_wheel(directory, duplicate=True)
            with self.assertRaises(ARTIFACT_CHECK.ReleaseArtifactError):
                ARTIFACT_CHECK.validate_release_directory(directory, "0.1.0")

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            _write_sdist(directory)
            _write_wheel(directory, metadata_version="0.2.0")
            with self.assertRaises(ARTIFACT_CHECK.ReleaseArtifactError):
                ARTIFACT_CHECK.validate_release_directory(directory, "0.1.0")


class ReleaseWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = (ROOT / ".github/workflows/release.yml").read_text(
            encoding="utf-8"
        )

    def test_actions_are_pinned_to_full_commit_shas(self):
        uses_lines = [line for line in self.workflow.splitlines() if "uses:" in line]
        self.assertGreaterEqual(len(uses_lines), 7)
        for line in uses_lines:
            self.assertRegex(line, r"@[0-9a-f]{40}(?:\s|#|$)")
        self.assertIsNone(
            re.fullmatch(r"uses:\s+[^@\s]+@[0-9a-f]{40}", "uses: example/action@v1")
        )

    def test_permissions_and_publish_boundary_are_narrow(self):
        self.assertIn("permissions:\n  contents: read", self.workflow)
        self.assertIn("id-token: write", self.workflow)
        self.assertIn("attestations: write", self.workflow)
        self.assertIn("contents: write", self.workflow)
        self.assertIn("environment:\n      name: pypi", self.workflow)
        self.assertNotIn("password:", self.workflow)
        self.assertNotIn("PYPI_TOKEN", self.workflow)
        self.assertNotIn("secrets.PYPI", self.workflow)

    def test_build_validation_provenance_and_release_steps_exist(self):
        for marker in (
            "check_release_version.py",
            "write_bundle_metadata.py",
            "python -m build --sdist --wheel",
            "validate_release.py",
            'pip install --disable-pip-version-check "${wheels[0]}"',
            "SHA256SUMS",
            "cd dist && sha256sum -- *.tar.gz *.whl",
            "spdx-json",
            "subject-checksums",
            "attestations: true",
            "gh release create",
            "set -euo pipefail",
            'find "$GITHUB_WORKSPACE/dist"',
            "missing release asset",
            "--verify-tag",
            "--generate-notes",
        ):
            self.assertIn(marker, self.workflow)

    def test_tag_release_publishes_npm(self):
        self.assertIn('tags:\n      - "v*.*.*"', self.workflow)
        self.assertIn("group: publish-npm-${{ github.ref_name }}", self.workflow)
        self.assertIn("cancel-in-progress: false", self.workflow)
        for job in ("native-build", "assemble-npm", "publish-npm"):
            match = re.search(
                rf"(?ms)^  {re.escape(job)}:\n(?P<body>.*?)(?=^  \w|\Z)",
                self.workflow,
            )
            self.assertIsNotNone(match)
            assert match is not None
            self.assertNotIn(
                "refs/heads/__disabled_npm_release__",
                match.group("body"),
            )
        self.assertIn("Publish npm packages with Trusted Publishing", self.workflow)
        self.assertIn("environment:\n      name: npm", self.workflow)
        self.assertIn("source/scripts/publish_npm_release.py preflight", self.workflow)
        self.assertIn(
            "source/scripts/publish_npm_release.py publish-platforms", self.workflow
        )
        self.assertIn(
            "source/scripts/publish_npm_release.py publish-launcher", self.workflow
        )
        self.assertIn('--version "${GITHUB_REF_NAME#v}"', self.workflow)
        self.assertIn('--bundle-dir "$GITHUB_WORKSPACE/release/npm"', self.workflow)
        self.assertIn("persist-credentials: false", self.workflow)
        self.assertIn(
            "Assert helper checkout matches attested build source", self.workflow
        )
        self.assertIn("Record attested source commit", self.workflow)
        self.assertIn(
            "SOURCE_SHA: ${{ needs.assemble-npm.outputs.source_sha }}", self.workflow
        )
        self.assertIn(
            "ref: ${{ needs.assemble-npm.outputs.source_sha }}", self.workflow
        )
        self.assertIn("outputs:\n      source_sha:", self.workflow)
        self.assertNotIn('test "$SOURCE_SHA" = "$GITHUB_SHA"', self.workflow)
        self.assertIn("git -C source status --porcelain=v1", self.workflow)
        self.assertIn("test -f source/scripts/publish_npm_release.py", self.workflow)
        self.assertIn(
            'test "$(git -C source rev-parse HEAD)" = "$SOURCE_SHA"', self.workflow
        )
        self.assertIn("shell: bash", self.workflow)
        self.assertLess(
            self.workflow.index("Check out publish helper sources"),
            self.workflow.index("Assert helper checkout matches attested build source"),
        )
        self.assertLess(
            self.workflow.index("Assert helper checkout matches attested build source"),
            self.workflow.index("Verify registry state against attested tarballs"),
        )
        self.assertLess(
            self.workflow.index("Verify registry state against attested tarballs"),
            self.workflow.index(
                "Publish platform packages first and read back integrity"
            ),
        )
        self.assertLess(
            self.workflow.index(
                "Publish platform packages first and read back integrity"
            ),
            self.workflow.index("Publish launcher last and read back integrity"),
        )
        self.assertNotIn("remote_version=$(npm view", self.workflow)

    def test_workflow_is_tag_only(self):
        self.assertIn('"v*.*.*"', self.workflow)
        self.assertNotIn("workflow_dispatch", self.workflow)


class LinuxStandaloneReleaseBaselineTests(unittest.TestCase):
    def test_both_publish_paths_build_linux_on_debian_12(self):
        for workflow_name in ("release.yml", "publish-npm.yml"):
            with self.subTest(workflow=workflow_name):
                workflow = (ROOT / ".github" / "workflows" / workflow_name).read_text(
                    encoding="utf-8"
                )
                native = workflow.split("  native-build:", 1)[1].split(
                    "  assemble-npm:", 1
                )[0]
                self.assertIn(
                    "- name: Build and smoke Linux standalone on Debian 12\n"
                    "        if: ${{ startsWith(matrix.target, 'linux-') }}\n"
                    "        shell: bash\n"
                    '        run: bash scripts/build_linux_standalone_in_container.sh "${{ matrix.target }}"',
                    native,
                )
                for step in (
                    "Set up Python",
                    "Install standalone smoke oracle",
                    "Install pinned PyInstaller",
                    "Build one-folder standalone executable",
                    "Run native standalone smoke equivalence",
                ):
                    self.assertIn(
                        f"- name: {step}\n"
                        "        if: ${{ !startsWith(matrix.target, 'linux-') }}",
                        native,
                    )

    def test_linux_builder_uses_pinned_baseline_and_clean_consumer(self):
        script = (ROOT / "scripts/build_linux_standalone_in_container.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("python:3.11-bookworm@sha256:", script)
        self.assertIn("debian:12-slim@sha256:", script)
        self.assertEqual(script.count("getconf GNU_LIBC_VERSION"), 2)
        self.assertIn("--network none", script)
        self.assertIn('"$executable" plan', script)

    def test_ci_requires_both_linux_consumer_lanes(self):
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        lane = workflow.split("  linux-standalone:", 1)[1].split("  workers:", 1)[0]
        self.assertIn("target: linux-arm64-gnu", lane)
        self.assertIn("target: linux-x64-gnu", lane)
        self.assertIn("fetch-depth: 2", lane)
        self.assertIn("bash scripts/build_linux_standalone_in_container.sh", lane)
        self.assertIn("linux-standalone", workflow.split("  required-checks:", 1)[1])


class NpmReleaseWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = (ROOT / ".github/workflows/publish-npm.yml").read_text(
            encoding="utf-8"
        )

    def test_manual_lane_is_bound_to_the_public_default_branch_source(self):
        self.assertIn("workflow_dispatch:", self.workflow)
        self.assertNotIn("\n  push:", self.workflow)
        self.assertIn("EXPECTED_REPOSITORY: malsabbagh/review-sensei", self.workflow)
        self.assertIn(
            'test "$GITHUB_REF" = "refs/heads/$DEFAULT_BRANCH"', self.workflow
        )
        self.assertIn("ref: ${{ github.sha }}", self.workflow)
        self.assertIn(
            "ref: ${{ needs.verify-request.outputs.source_sha }}", self.workflow
        )
        self.assertIn(
            "Assert helper checkout matches attested build source", self.workflow
        )
        self.assertIn(
            'test "$(git -C source rev-parse HEAD)" = "$SOURCE_SHA"', self.workflow
        )
        self.assertNotIn(
            'test "$SOURCE_SHA" = "$GITHUB_SHA"',
            self.workflow.split("Assert helper checkout matches attested build source")[
                1
            ].split("Configure bootstrap authentication when present")[0],
        )
        self.assertIn("git -C source status --porcelain=v1", self.workflow)
        self.assertIn(
            'python scripts/check_release_version.py --tag "v$VERSION"', self.workflow
        )

    def test_npm_publish_is_protected_and_platform_first(self):
        publish_section = self.workflow.split("  publish-npm:", maxsplit=1)[1]
        publish_if_line = next(
            line for line in publish_section.splitlines() if line.startswith("    if:")
        )
        self.assertIn("inputs.publish", publish_if_line)
        self.assertIn(
            "needs.verify-request.outputs.resume_mode != 'true' && needs.native-build.result == 'success' && needs.assemble-npm.result == 'success'",
            publish_if_line,
        )
        self.assertIn(
            "needs.verify-request.outputs.resume_mode == 'true' && needs.native-build.result == 'skipped' && needs.assemble-npm.result == 'skipped'",
            publish_if_line,
        )
        self.assertIn("environment:\n      name: npm", self.workflow)
        self.assertIn("id-token: write", self.workflow)
        self.assertIn("group: publish-npm-v${{ inputs.version }}", self.workflow)
        self.assertIn("bootstrap:", self.workflow)
        self.assertIn("NPM_TOKEN: ${{ secrets.NPM_TOKEN }}", self.workflow)
        self.assertIn(
            'test "$VERSION" = "0.1.0"',
            self.workflow,
        )
        self.assertIn(
            "NPM_TOKEN is configured but bootstrap=false; remove the bootstrap secret",
            self.workflow,
        )
        self.assertIn("Verify registry state against attested tarballs", self.workflow)
        self.assertIn("source/scripts/publish_npm_release.py preflight", self.workflow)
        self.assertIn('--bundle-dir "$GITHUB_WORKSPACE/release/npm"', self.workflow)
        self.assertIn(
            "source/scripts/publish_npm_release.py publish-platforms", self.workflow
        )
        self.assertIn(
            "source/scripts/publish_npm_release.py publish-launcher", self.workflow
        )
        self.assertIn("persist-credentials: false", self.workflow)
        self.assertIn("test -f source/scripts/publish_npm_release.py", self.workflow)
        self.assertNotIn("remote_version=$(npm view", self.workflow)
        self.assertLess(
            self.workflow.index("Check out publish helper sources"),
            self.workflow.index("Assert helper checkout matches attested build source"),
        )
        self.assertLess(
            self.workflow.index("Assert helper checkout matches attested build source"),
            self.workflow.index("Verify registry state against attested tarballs"),
        )
        self.assertLess(
            self.workflow.index("Verify registry state against attested tarballs"),
            self.workflow.index("Publish platform packages first"),
        )
        self.assertIn("Verify a clean Linux consumer installation", self.workflow)
        self.assertIn(
            "review-sensei command is not owned by the launcher package",
            self.workflow,
        )
        self.assertIn("npm audit signatures", self.workflow)

    def test_partial_publish_can_resume_prior_attested_bundle(self):
        self.assertIn("resume_bundle_run_id:", self.workflow)
        self.assertIn("resume_mode=true", self.workflow)
        self.assertIn("resume_mode=false", self.workflow)
        self.assertIn(
            "run-name: publish-npm ${{ github.event.inputs.version }}", self.workflow
        )
        self.assertIn("needs.verify-request.outputs.resume_mode", self.workflow)
        self.assertIn(
            "Download npm release bundle from prior workflow run", self.workflow
        )
        self.assertIn("gh run download", self.workflow)
        self.assertIn("publish_npm_release.py verify-bundle", self.workflow)
        resume_section = self.workflow.split(
            "Download npm release bundle from prior workflow run", maxsplit=1
        )[1].split("Verify downloaded npm tarball checksums", maxsplit=1)[0]
        self.assertIn(
            'gh api "repos/$GITHUB_REPOSITORY/actions/runs/$RESUME_BUNDLE_RUN_ID"',
            resume_section,
        )
        self.assertNotIn("gh run view", resume_section)
        self.assertIn("validate_npm_resume_run.py", resume_section)
        self.assertIn("display_title", resume_section)
        self.assertIn("resume_staging", resume_section)
        self.assertIn("sha256sum --check SHA256SUMS", resume_section)
        self.assertLess(
            resume_section.index("sha256sum --check SHA256SUMS"),
            resume_section.index("gh attestation verify"),
        )
        self.assertIn("list-attestation-subjects", resume_section)
        self.assertNotIn("while read -r _ _ filename", resume_section)
        self.assertIn("gh attestation verify", resume_section)
        self.assertIn('--source-digest "$bundle_source_sha"', resume_section)
        self.assertIn('--source-ref "refs/heads/$DEFAULT_BRANCH"', resume_section)
        self.assertIn(
            "verify-request did not produce a dispatch source SHA", resume_section
        )
        self.assertIn("DISPATCH_SOURCE_SHA", resume_section)
        self.assertIn("--dispatch-source-sha", resume_section)
        self.assertIn("checkout --detach", resume_section)
        self.assertIn("test -f release/npm/integrity.jsonl", resume_section)
        self.assertIn('--version "$VERSION"', resume_section)
        self.assertIn('--expected-source-sha "$bundle_source_sha"', resume_section)
        publish_section = self.workflow.split("  publish-npm:", maxsplit=1)[1]
        publish_permissions = publish_section.split("    steps:", maxsplit=1)[0]
        self.assertIn("actions: read", publish_permissions)
        self.assertIn("attestations: read", publish_permissions)
        publish_if = publish_section.split("    if:", maxsplit=1)[1].split(
            "\n", maxsplit=1
        )[0]
        self.assertIn("needs.verify-request.outputs.resume_mode == 'true'", publish_if)
        self.assertIn("needs.assemble-npm.result == 'skipped'", publish_if)
        preflight_section = self.workflow.split(
            "Verify registry state against attested tarballs", maxsplit=1
        )[1].split("Publish platform packages first", maxsplit=1)[0]
        self.assertIn("--expected-source-sha", preflight_section)
        self.assertIn("RESUME_BUNDLE_RUN_ID", preflight_section)
        self.assertIn(
            "resume publish requires bundle_source_sha from resume-bundle step",
            preflight_section,
        )
        self.assertIn("write_bundle_metadata.py", self.workflow)
        self.assertIn("Check out repository for resumed run validation", self.workflow)
        self.assertIn("resume-source", self.workflow)
        native_section = self.workflow.split("  native-build:", maxsplit=1)[1].split(
            "  assemble-npm:", maxsplit=1
        )[0]
        native_if = next(
            line for line in native_section.splitlines() if line.startswith("    if:")
        )
        self.assertIn("needs.verify-request.outputs.resume_mode != 'true'", native_if)
        assemble_section = self.workflow.split("  assemble-npm:", maxsplit=1)[1].split(
            "  publish-npm:", maxsplit=1
        )[0]
        assemble_if = next(
            line for line in assemble_section.splitlines() if line.startswith("    if:")
        )
        self.assertIn("needs.verify-request.outputs.resume_mode != 'true'", assemble_if)
        self.assertIn(
            "if: ${{ needs.verify-request.outputs.resume_mode == 'true' }}",
            self.workflow,
        )
        self.assertIn(
            "if: ${{ needs.verify-request.outputs.resume_mode != 'true' }}",
            self.workflow,
        )
        verify_section = self.workflow.split(
            "Verify resumed npm release bundle provenance", maxsplit=1
        )[1].split("Configure bootstrap authentication when present", maxsplit=1)[0]
        self.assertIn(
            '--expected-source-sha "$bundle_source_sha"',
            verify_section,
        )
        self.assertIn(
            "python resume-source/scripts/publish_npm_release.py verify-bundle",
            verify_section,
        )
        self.assertIn("list-attestation-subjects", verify_section)
        self.assertIn("gh attestation verify", verify_section)
        self.assertIn('--source-digest "$bundle_source_sha"', verify_section)
        self.assertNotIn("env.BUNDLE_SOURCE_SHA", verify_section)
        self.assertIn("id: resume-bundle", self.workflow)


class NpmResumeRunTests(unittest.TestCase):
    def setUp(self):
        self.sha = "a" * 40
        self.run = {
            "id": 123,
            "repository": "malsabbagh/review-sensei",
            "path": ".github/workflows/publish-npm.yml",
            "conclusion": "failure",
            "event": "workflow_dispatch",
            "head_branch": "main",
            "display_title": "publish-npm 0.6.7",
            "head_sha": self.sha,
        }

    def validate(self, run):
        return RESUME_RUN_CHECK.validate_resume_run(
            run,
            run_id="123",
            repository="malsabbagh/review-sensei",
            default_branch="main",
            version="0.6.7",
            dispatch_source_sha=self.sha,
        )

    def test_rest_run_from_matching_manual_dispatch_is_accepted(self):
        self.assertEqual(self.validate(self.run), self.sha)

    def test_tag_release_and_other_repositories_are_rejected(self):
        for field, value in (
            ("path", ".github/workflows/release.yml"),
            ("event", "push"),
            ("repository", "other/review-sensei"),
        ):
            with self.subTest(field=field):
                run = {**self.run, field: value}
                with self.assertRaises(RESUME_RUN_CHECK.ResumeRunError):
                    self.validate(run)

    def test_different_version_or_source_commit_is_rejected(self):
        for field, value in (
            ("display_title", "publish-npm 0.6.4"),
            ("head_sha", "b" * 40),
        ):
            with self.subTest(field=field):
                run = {**self.run, field: value}
                with self.assertRaises(RESUME_RUN_CHECK.ResumeRunError):
                    self.validate(run)


class ReleaseDocumentationTests(unittest.TestCase):
    def test_runbook_covers_external_setup_and_recovery(self):
        source = (ROOT / "docs/releasing.md").read_text(encoding="utf-8")
        for marker in (
            "Trusted Publisher",
            "signed tag",
            "Rollback and yank",
            "Compromised-release response",
            "SHA256SUMS",
            "publish-npm.yml",
            "NPM_TOKEN",
        ):
            self.assertIn(marker, source)


if __name__ == "__main__":
    unittest.main()

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


class ReleaseMetadataTests(unittest.TestCase):
    def test_pyproject_is_canonical_release_metadata(self):
        with (ROOT / "pyproject.toml").open("rb") as handle:
            document = tomllib.load(handle)
        project = document["project"]
        self.assertEqual(project["version"], "0.1.1")
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


class ReleaseVersionTests(unittest.TestCase):
    def test_matching_tag_and_dated_heading_pass(self):
        self.assertEqual(VERSION_CHECK.validate_release_tag("v0.1.1", ROOT), "0.1.1")

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
                "## 0.1.1 - 2026-09-08\n", encoding="utf-8"
            )
            shutil.copytree(ROOT / "packages/npm", root / "packages/npm")
            self.assertEqual(
                VERSION_CHECK.validate_release_tag("v0.1.1", root), "0.1.1"
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

    def test_tag_release_does_not_publish_npm(self):
        self.assertIn('tags:\n      - "v*.*.*"', self.workflow)
        for job in ("native-build", "assemble-npm", "publish-npm"):
            match = re.search(
                rf"(?ms)^  {re.escape(job)}:\n(?P<body>.*?)(?=^  \w|\Z)",
                self.workflow,
            )
            self.assertIsNotNone(match)
            assert match is not None
            self.assertIn(
                "    if: ${{ startsWith(github.ref, 'refs/heads/__disabled_npm_release__') }}",
                match.group("body"),
            )
        self.assertIn("Publish npm packages with Trusted Publishing", self.workflow)

    def test_workflow_is_tag_only(self):
        self.assertIn('"v*.*.*"', self.workflow)
        self.assertNotIn("workflow_dispatch", self.workflow)


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
        self.assertIn('test "$source_sha" = "$GITHUB_SHA"', self.workflow)
        self.assertIn(
            'python scripts/check_release_version.py --tag "v$VERSION"', self.workflow
        )

    def test_npm_publish_is_protected_and_platform_first(self):
        self.assertIn("if: ${{ inputs.publish }}", self.workflow)
        self.assertIn("environment:\n      name: npm", self.workflow)
        self.assertIn("id-token: write", self.workflow)
        self.assertIn("group: publish-npm-${{ inputs.version }}", self.workflow)
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
        self.assertIn("publish-state.json", self.workflow)
        self.assertIn("Registry already contains the attested bytes", self.workflow)
        self.assertEqual(self.workflow.count("npm publish --ignore-scripts"), 2)
        self.assertLess(
            self.workflow.index("Publish platform packages first"),
            self.workflow.index("Publish launcher last"),
        )
        self.assertIn("Verify a clean Linux consumer installation", self.workflow)
        self.assertIn(
            "review-sensei command is not owned by the launcher package",
            self.workflow,
        )
        self.assertIn("npm audit signatures", self.workflow)


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

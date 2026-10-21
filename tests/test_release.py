from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
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
PROJECT_VERSION = VERSION_CHECK.project_version(ROOT)


class ReleaseMetadataTests(unittest.TestCase):
    def test_pyproject_is_canonical_release_metadata(self):
        with (ROOT / "pyproject.toml").open("rb") as handle:
            document = tomllib.load(handle)
        project = document["project"]
        self.assertRegex(
            project["version"], r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$"
        )
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
        self.assertEqual(
            VERSION_CHECK.validate_release_tag("v" + PROJECT_VERSION, ROOT),
            PROJECT_VERSION,
        )

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
                f"## {PROJECT_VERSION} - 2026-10-06\n", encoding="utf-8"
            )
            shutil.copytree(ROOT / "packages/npm", root / "packages/npm")
            self.assertEqual(
                VERSION_CHECK.validate_release_tag("v" + PROJECT_VERSION, root),
                PROJECT_VERSION,
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
            "github_release.py draft",
            "github_release.py publish",
            "attestations: read",
            "set -euo pipefail",
            'find "$GITHUB_WORKSPACE/dist"',
            "sha256sum -- review-sensei-sbom.spdx.json",
        ):
            self.assertIn(marker, self.workflow)

    def test_tag_release_publishes_npm(self):
        npm_workflow = (ROOT / ".github/workflows/publish-npm.yml").read_text()
        self.assertNotIn("  publish-npm:", self.workflow)
        self.assertIn('tags:\n      - "v*.*.*"', npm_workflow)
        self.assertIn("group: publish-npm-${{ github.ref_name }}", npm_workflow)
        self.assertIn("cancel-in-progress: false", npm_workflow)
        for job in ("native-build", "assemble-npm", "publish-npm"):
            match = re.search(
                rf"(?ms)^  {re.escape(job)}:\n(?P<body>.*?)(?=^  \w|\Z)",
                npm_workflow,
            )
            self.assertIsNotNone(match)
            assert match is not None
            self.assertNotIn(
                "refs/heads/__disabled_npm_release__",
                match.group("body"),
            )
        self.assertIn("Publish npm packages with Trusted Publishing", npm_workflow)
        self.assertIn("environment:\n      name: npm", npm_workflow)
        self.assertIn("source/scripts/publish_npm_release.py preflight", npm_workflow)
        self.assertIn(
            "source/scripts/publish_npm_release.py publish-platforms", npm_workflow
        )
        self.assertIn(
            "source/scripts/publish_npm_release.py publish-launcher", npm_workflow
        )
        self.assertIn('--version "${GITHUB_REF_NAME#v}"', npm_workflow)
        self.assertIn('--bundle-dir "$GITHUB_WORKSPACE/release/npm"', npm_workflow)
        self.assertIn("persist-credentials: false", npm_workflow)
        self.assertIn(
            "Assert helper checkout matches attested build source", npm_workflow
        )
        self.assertIn("Record attested source commit", npm_workflow)
        self.assertIn(
            "SOURCE_SHA: ${{ needs.assemble-npm.outputs.source_sha }}", npm_workflow
        )
        self.assertIn("ref: ${{ needs.assemble-npm.outputs.source_sha }}", npm_workflow)
        self.assertIn("outputs:\n      source_sha:", npm_workflow)
        self.assertNotIn('test "$SOURCE_SHA" = "$GITHUB_SHA"', npm_workflow)
        self.assertIn("git -C source status --porcelain=v1", npm_workflow)
        self.assertIn("test -f source/scripts/publish_npm_release.py", npm_workflow)
        self.assertIn(
            'test "$(git -C source rev-parse HEAD)" = "$SOURCE_SHA"', npm_workflow
        )
        self.assertIn("shell: bash", npm_workflow)
        self.assertLess(
            npm_workflow.index("Check out publish helper sources"),
            npm_workflow.index("Assert helper checkout matches attested build source"),
        )
        self.assertLess(
            npm_workflow.index("Assert helper checkout matches attested build source"),
            npm_workflow.index("Verify registry state against attested tarballs"),
        )
        self.assertLess(
            npm_workflow.index("Verify registry state against attested tarballs"),
            npm_workflow.index(
                "Publish platform packages first and read back integrity"
            ),
        )
        self.assertLess(
            npm_workflow.index(
                "Publish platform packages first and read back integrity"
            ),
            npm_workflow.index("Publish launcher last and read back integrity"),
        )
        self.assertNotIn("remote_version=$(npm view", npm_workflow)

    def test_workflow_is_tag_only(self):
        self.assertIn('"v*.*.*"', self.workflow)
        self.assertNotIn("workflow_dispatch", self.workflow)


class LinuxStandaloneReleaseBaselineTests(unittest.TestCase):
    def _run_container_script(self, directory, *, fail_all=False, fail_consumer=False):
        docker = directory / "docker"
        docker.write_text(
            "#!/bin/sh\n"
            'printf "%s\\0" "$*" >> "$PULL_RECEIPT"\n'
            'if [ "$1" = pull ]; then\n'
            '  case "$*" in\n'
            '    *public.ecr.aws*) echo "synthetic Data limit exceeded" >&2; exit 1 ;;\n'
            '    *docker.io/library/debian*) [ "$FAIL_CONSUMER" = 1 ] && exit 1 ;;\n'
            "  esac\n"
            '  [ "$FAIL_ALL" = 1 ] && exit 1\n'
            "fi\n"
            "exit 0\n"
        )
        docker.chmod(0o755)
        sleep = directory / "sleep"
        sleep.write_text(
            '#!/bin/sh\nprintf "%s\\0" "sleep $*" >> "$PULL_RECEIPT"\nexit 0\n'
        )
        sleep.chmod(0o755)
        receipt = directory / "calls"
        result = subprocess.run(
            [
                "bash",
                str(ROOT / "scripts/build_linux_standalone_in_container.sh"),
                "linux-arm64-gnu",
            ],
            cwd=ROOT,
            env={
                **os.environ,
                "PATH": str(directory) + os.pathsep + os.environ["PATH"],
                "PULL_RECEIPT": str(receipt),
                "FAIL_ALL": "1" if fail_all else "0",
                "FAIL_CONSUMER": "1" if fail_consumer else "0",
            },
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result, receipt.read_text().rstrip("\0").split("\0")

    @unittest.skipIf(sys.platform == "win32", "Linux pull policy uses Bash")
    def test_exhausted_pull_stops_the_actual_script_before_any_container(self):
        with tempfile.TemporaryDirectory() as scratch:
            result, calls = self._run_container_script(Path(scratch), fail_all=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(calls), 2)
        self.assertTrue(
            all(call.startswith("pull --platform linux/arm64 ") for call in calls)
        )
        self.assertIn("public.ecr.aws/docker/library/python:", calls[0])
        self.assertIn("docker.io/library/python:", calls[1])
        self.assertEqual(calls[0].split("@", 1)[1], calls[1].split("@", 1)[1])
        self.assertEqual(result.stderr.count("synthetic Data limit exceeded"), 1)
        self.assertIn(
            "Pinned image admission failed after 2 approved origins", result.stderr
        )

    @unittest.skipIf(sys.platform == "win32", "Linux pull policy uses Bash")
    def test_actual_script_runs_only_the_admitted_upstream_references(self):
        with tempfile.TemporaryDirectory() as scratch:
            result, calls = self._run_container_script(Path(scratch))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 6)
        pulls, runs = calls[:4], calls[4:]
        self.assertTrue(all(call.startswith("pull ") for call in pulls))
        self.assertTrue(all(call.startswith("run ") for call in runs))
        self.assertIn("docker.io/library/python:", runs[0])
        self.assertIn("docker.io/library/debian:", runs[1])
        self.assertTrue(all("--pull never" in call for call in runs))
        self.assertTrue(all("public.ecr.aws" not in call for call in runs))
        self.assertIn("--network none", runs[1])
        self.assertEqual(
            result.stderr.count("Trying approved upstream with the same digest"), 2
        )

    @unittest.skipIf(sys.platform == "win32", "Linux pull policy uses Bash")
    def test_consumer_admission_failure_stops_before_building(self):
        with tempfile.TemporaryDirectory() as scratch:
            result, calls = self._run_container_script(
                Path(scratch), fail_consumer=True
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(calls), 4)
        self.assertTrue(all(call.startswith("pull ") for call in calls))

    @unittest.skipIf(sys.platform == "win32", "Linux pull policy uses Bash")
    def test_pinned_origin_attempts_preserve_digest_platform_and_selected_reference(
        self,
    ):
        script = (ROOT / "scripts/build_linux_standalone_in_container.sh").read_text()
        functions = re.findall(r"(?ms)^pull_pinned_image\(\) \{(.*?)^\}", script)
        self.assertEqual(len(functions), 1)
        function = functions[0]
        image = "public.ecr.aws/docker/library/python:3.11-bookworm@sha256:" + "a" * 64
        fallback = image.replace("public.ecr.aws/docker/", "docker.io/", 1)
        for platform in ("linux/arm64", "linux/amd64"):
            for primary, secondary, expected in (
                (0, 0, image),
                (1, 0, fallback),
                (1, 1, None),
            ):
                with (
                    self.subTest(
                        platform=platform, primary=primary, secondary=secondary
                    ),
                    tempfile.TemporaryDirectory() as scratch,
                ):
                    shell = f'''set -euo pipefail
platform={platform}
docker() {{
  printf '%s\\n' "$*" >>calls
  echo "synthetic Docker stdout"
  case "$*" in
    *public.ecr.aws*) return {primary} ;;
    *docker.io*) return {secondary} ;;
  esac
}}
sleep() {{ exit 99; }}
pull_pinned_image() {{{function}
}}
selected=$(pull_pinned_image "{image}")
printf '%s\\n' "$selected" >selected
'''
                    result = subprocess.run(
                        ["bash", "-c", shell],
                        cwd=scratch,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    self.assertEqual(
                        result.returncode, 0 if expected else 1, result.stderr
                    )
                    calls = (Path(scratch) / "calls").read_text().splitlines()
                    refs = [image] if primary == 0 else [image, fallback]
                    self.assertEqual(
                        calls, [f"pull --platform {platform} {ref}" for ref in refs]
                    )
                    if expected:
                        self.assertEqual(
                            (Path(scratch) / "selected").read_text().strip(), expected
                        )
                    else:
                        self.assertIn("failed after 2 approved origins", result.stderr)

    @unittest.skipIf(sys.platform == "win32", "Linux pull policy uses Bash")
    def test_mutable_or_unapproved_image_is_rejected_before_any_pull(self):
        script = (ROOT / "scripts/build_linux_standalone_in_container.sh").read_text()
        function = re.findall(r"(?ms)^pull_pinned_image\(\) \{(.*?)^\}", script)[0]
        for image in (
            "public.ecr.aws/docker/library/python:latest",
            "public.ecr.aws/docker/library/python@sha256:abc",
            "unapproved.invalid/library/python@sha256:" + "a" * 64,
        ):
            with self.subTest(image=image), tempfile.TemporaryDirectory() as scratch:
                shell = f'''set -euo pipefail
platform=linux/amd64
docker() {{ echo called >calls; return 0; }}
pull_pinned_image() {{{function}
}}
pull_pinned_image "{image}"
'''
                result = subprocess.run(
                    ["bash", "-c", shell],
                    cwd=scratch,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((Path(scratch) / "calls").exists())

    def test_both_publish_paths_build_linux_on_debian_12(self):
        for workflow_name in ("publish-npm.yml",):
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
        self.assertIn("public.ecr.aws/docker/library/python:", script)
        self.assertIn("public.ecr.aws/docker/library/debian:", script)
        self.assertEqual(script.count('docker pull --platform "$platform"'), 1)
        self.assertIn('for candidate in "$image" "$fallback"', script)
        self.assertIn('pull_pinned_image "$build_image"', script)
        self.assertIn('pull_pinned_image "$consumer_image"', script)
        self.assertLess(
            script.index('consumer_image=$(pull_pinned_image "$consumer_image")'),
            script.index("docker run"),
        )
        self.assertEqual(script.count("--pull never"), 2)
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
        self.workflow = (ROOT / ".github/workflows/publish-npm.yml").read_text()

    def test_only_marshal_signed_tag_can_start_npm_publication(self):
        self.assertIn('tags:\n      - "v*.*.*"', self.workflow)
        for retired in (
            "workflow_dispatch",
            "resume_bundle_run_id",
            "inputs.version",
            "NPM_TOKEN",
            "secrets.",
            "workflow_run",
        ):
            self.assertNotIn(retired, self.workflow)
        self.assertIn("ref: ${{ github.sha }}", self.workflow)
        self.assertIn("release_channels.py qualify --channel npm", self.workflow)

    def test_npm_publish_retains_approval_order_and_registry_readback(self):
        self.assertIn("environment:\n      name: npm", self.workflow)
        publisher = self.workflow.split("  publish-npm:\n", 1)[1].split(
            "    steps:\n", 1
        )[0]
        self.assertIn("contents: read", publisher)
        self.assertNotIn("contents: write", publisher)
        self.assertIn("id-token: write", self.workflow)
        self.assertIn("group: publish-npm-${{ github.ref_name }}", self.workflow)
        self.assertIn("cancel-in-progress: false", self.workflow)
        self.assertLess(
            self.workflow.index("publish-platforms"),
            self.workflow.index("publish-launcher"),
        )
        self.assertIn("Verify a clean Linux consumer installation", self.workflow)
        self.assertIn("npm audit signatures", self.workflow)
        self.assertIn('--expected-source-sha "${GITHUB_SHA}"', self.workflow)

    def test_recovery_uses_exact_same_run_bundle_without_cross_run_dispatch(self):
        self.assertIn(
            "artifact-ids: ${{ needs.assemble-npm.outputs.artifact_id }}", self.workflow
        )
        self.assertIn("digest-mismatch: error", self.workflow)
        self.assertIn("merge-multiple: true", self.workflow)
        self.assertIn(
            "BUNDLE_BUILD_ATTEMPT: ${{ needs.assemble-npm.outputs.build_attempt }}",
            self.workflow,
        )
        self.assertIn(
            "BUNDLE_ARTIFACT_DIGEST: ${{ needs.assemble-npm.outputs.artifact_digest }}",
            self.workflow,
        )
        self.assertNotIn("gh run download", self.workflow)
        self.assertNotIn("run-id:", self.workflow)
        self.assertNotIn("overwrite: true", self.workflow)
        self.assertIn("release_channels.py verify-npm", self.workflow)
        self.assertLess(
            self.workflow.index("release_channels.py verify-npm"),
            self.workflow.index("publish_npm_release.py preflight"),
        )

    def test_channels_record_separate_results_and_cannot_cancel_each_other(self):
        python = (ROOT / ".github/workflows/release.yml").read_text()
        self.assertIn("name: Release npm", self.workflow)
        self.assertIn(
            "release-npm-result-${{ github.run_id }}-${{ github.run_attempt }}",
            self.workflow,
        )
        self.assertIn(
            "release-python-result-${{ github.run_id }}-${{ github.run_attempt }}",
            python,
        )
        self.assertNotIn("native-build", python)
        self.assertNotIn("assemble-npm", python)
        self.assertNotIn("publish-npm", python)
        self.assertNotIn("cancel-in-progress: true", python + self.workflow)
        for text in (python, self.workflow):
            self.assertIn("always() && needs.qualify-source.result == 'success'", text)
            self.assertNotIn("continue-on-error", text)
            self.assertNotIn("actions: write", text)
            self.assertIn("sign_release.py --check-publication-gates", text)


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
            "display_title": "publish-npm 0.6.16",
            "head_sha": self.sha,
        }

    def validate(self, run):
        return RESUME_RUN_CHECK.validate_resume_run(
            run,
            run_id="123",
            repository="malsabbagh/review-sensei",
            default_branch="main",
            version="0.6.16",
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
            "Owner migration hold before activation",
        ):
            self.assertIn(marker, source)


if __name__ == "__main__":
    unittest.main()

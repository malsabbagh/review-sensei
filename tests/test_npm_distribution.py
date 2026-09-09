from __future__ import annotations

import importlib.util
import io
import json
import shutil
import stat
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ASSEMBLER = load_script("prepare_npm_packages.py")
BUILDER = load_script("build_standalone.py")
VALIDATOR = load_script("validate_npm_package.py")
SMOKE = load_script("standalone_smoke.py")


class ManifestTests(unittest.TestCase):
    def test_launcher_manifest_has_exact_target_set(self):
        manifest = json.loads(
            (ROOT / "packages/npm/cli/package.json").read_text(encoding="utf-8")
        )
        launcher = ROOT / "packages/npm/cli/bin/review-sensei.js"
        self.assertTrue(
            launcher.read_text(encoding="utf-8").startswith("#!/usr/bin/env node\n")
        )
        self.assertEqual(manifest["name"], "@reviewsensei/cli")
        self.assertEqual(manifest["version"], "0.1.1")
        self.assertEqual(manifest["license"], "MIT")
        self.assertEqual(manifest["repository"], VALIDATOR.NPM_PUBLIC_REPOSITORY)
        self.assertEqual(manifest["engines"], {"node": ">=22"})
        self.assertEqual(manifest["bin"], {"review-sensei": "bin/review-sensei.js"})
        self.assertEqual(
            set(manifest["optionalDependencies"]), set(VALIDATOR.VERSIONED_TARGETS)
        )
        self.assertNotIn("dependencies", manifest)
        self.assertNotIn("scripts", manifest)
        for target in VALIDATOR.VERSIONED_TARGETS.values():
            platform = (
                ROOT
                / "packages/npm/platforms"
                / str(target["directory"])
                / "package.json"
            )
            platform_manifest = json.loads(platform.read_text(encoding="utf-8"))
            self.assertEqual(
                platform_manifest["repository"],
                VALIDATOR.NPM_PUBLIC_REPOSITORY,
            )
            self.assertNotIn("bin", platform_manifest)

    def test_source_check_requires_metadata_collection(self):
        BUILDER.source_check(ROOT)
        self.assertEqual(
            BUILDER.pinned_pyinstaller_version(ROOT / BUILDER.PYINSTALLER_REQUIREMENTS),
            "6.16.0",
        )
        spec = (ROOT / BUILDER.PYINSTALLER_SPEC).read_text(encoding="utf-8")
        self.assertIn('["entrypoint.py"]', spec)
        self.assertNotIn('["packaging/standalone/entrypoint.py"]', spec)
        self.assertIn('collect_data_files("certifi")', spec)
        with tempfile.TemporaryDirectory() as tmp:
            requirements = Path(tmp) / "requirements.txt"
            requirements.write_text("pyinstaller==6.15.0\n", encoding="utf-8")
            with self.assertRaises(BUILDER.StandaloneBuildError):
                BUILDER.pinned_pyinstaller_version(requirements)


class AssemblyAndSetTests(unittest.TestCase):
    def setUp(self):
        self.dist = ROOT / "dist"
        self.dist.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=self.dist)
        self.stage = Path(self.tmp.name) / "npm"
        self.bundles: dict[str, Path] = {}
        for target_id, target in ASSEMBLER.TARGETS.items():
            bundle = Path(self.tmp.name) / target_id / "review-sensei"
            bundle.mkdir(parents=True)
            filename = Path(str(target["payload"])).name
            payload = bundle / filename
            payload.write_bytes(b"native payload\n")
            payload.chmod(payload.stat().st_mode | stat.S_IXUSR)
            internal = bundle / "_internal"
            internal.mkdir()
            (internal / "runtime.dat").write_bytes(b"runtime sibling\n")
            certifi = internal / "certifi"
            certifi.mkdir()
            (certifi / "cacert.pem").write_bytes(b"public CA roots\n")
            self.bundles[target_id] = bundle

    def tearDown(self):
        self.tmp.cleanup()

    def test_assembly_is_deterministic_and_validates_as_six_packages(self):
        ASSEMBLER.assemble_packages(ROOT, self.stage, self.bundles)
        report = VALIDATOR.validate_package_set(self.stage, "0.1.1")
        self.assertEqual(report["version"], "0.1.1")
        self.assertEqual(len(report["packages"]), 6)
        first = (self.stage / "checksums.json").read_bytes()
        ASSEMBLER.assemble_packages(ROOT, self.stage, self.bundles)
        self.assertEqual(first, (self.stage / "checksums.json").read_bytes())
        self.assertTrue((self.stage / "SHA256SUMS").is_file())
        for target in ASSEMBLER.TARGETS.values():
            self.assertEqual(
                (
                    self.stage / str(target["directory"]) / "bin/_internal/runtime.dat"
                ).read_bytes(),
                b"runtime sibling\n",
            )
        self.assertFalse((self.stage / "cli/test").exists())
        (self.stage / "unexpected.txt").write_text("stale\n", encoding="utf-8")
        with self.assertRaises(VALIDATOR.NpmPackageValidationError):
            VALIDATOR.validate_package_set(self.stage, "0.1.1")
        (self.stage / "unexpected.txt").unlink()
        alias = self.stage / "unexpected-link"
        try:
            alias.symlink_to(self.stage / "cli", target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlinks unavailable: {exc}")
        with self.assertRaises(VALIDATOR.NpmPackageValidationError):
            VALIDATOR.validate_package_set(self.stage, "0.1.1")

    def test_directory_validation_allows_windows_missing_posix_modes(self):
        ASSEMBLER.assemble_packages(ROOT, self.stage, self.bundles)
        for target in ASSEMBLER.TARGETS.values():
            payload = (
                self.stage
                / str(target["directory"])
                / "bin"
                / Path(str(target["payload"])).name
            )
            if payload.suffix.lower() != ".exe":
                payload.chmod(0o644)

        with patch.object(VALIDATOR.os, "name", "nt"):
            report = VALIDATOR.validate_package_set(self.stage, "0.1.1")

        self.assertEqual(report["version"], "0.1.1")

    def test_assembly_rejects_missing_target_and_outside_staging(self):
        missing = dict(self.bundles)
        missing.pop("win32-x64")
        with self.assertRaises(ASSEMBLER.NpmAssemblyError):
            ASSEMBLER.assemble_packages(ROOT, self.stage, missing)
        with self.assertRaises(ASSEMBLER.NpmAssemblyError):
            ASSEMBLER.assemble_packages(ROOT, ROOT / "tmp-package-output", self.bundles)

        symlinked = dict(self.bundles)
        alias = Path(self.tmp.name) / "symlinked-bundle"
        try:
            alias.symlink_to(self.bundles["linux-x64-gnu"], target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlinks unavailable: {exc}")
        symlinked["linux-x64-gnu"] = alias
        with self.assertRaises(ASSEMBLER.NpmAssemblyError):
            ASSEMBLER.assemble_packages(ROOT, self.stage, symlinked)

    def test_source_manifest_rejects_pack_hooks_and_wildcard_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(ROOT / "packages/npm", root / "packages/npm")
            manifest_path = root / "packages/npm/platforms/linux-x64-gnu/package.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["scripts"] = {"prepack": "echo unsafe"}
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(ASSEMBLER.NpmAssemblyError):
                ASSEMBLER.validate_source_manifests(root, "0.1.1")
            manifest.pop("scripts")
            manifest["files"] = ["**"]
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(ASSEMBLER.NpmAssemblyError):
                ASSEMBLER.validate_source_manifests(root, "0.1.1")
            manifest["files"] = ["bin", "README.md", "LICENSE"]
            manifest["bin"] = {"review-sensei": "bin/review-sensei"}
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(ASSEMBLER.NpmAssemblyError):
                ASSEMBLER.validate_source_manifests(root, "0.1.1")
            manifest.pop("bin")
            manifest["repository"] = {"url": "https://example.invalid/repo"}
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(ASSEMBLER.NpmAssemblyError):
                ASSEMBLER.validate_source_manifests(root, "0.1.1")


def _tar_member(
    archive: tarfile.TarFile, name: str, data: bytes, mode: int = 0o644
) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    archive.addfile(info, io.BytesIO(data))


def _valid_platform_tar(
    directory: Path,
    *,
    lifecycle: str | bool = False,
    version: str = "0.1.0",
    files: object = None,
    repository: object = VALIDATOR.NPM_PUBLIC_REPOSITORY,
    command_bin: bool = False,
    extra_member: str | None = None,
) -> Path:
    path = directory / "platform.tgz"
    manifest = {
        "name": "@reviewsensei/cli-linux-x64-gnu",
        "version": version,
        "license": "MIT",
        "os": ["linux"],
        "cpu": ["x64"],
        "files": ["bin", "README.md", "LICENSE"] if files is None else files,
        "repository": repository,
    }
    if command_bin:
        manifest["bin"] = {"review-sensei": "bin/review-sensei"}
    if lifecycle:
        manifest["scripts"] = {
            lifecycle if isinstance(lifecycle, str) else "postinstall": "echo unsafe"
        }
    with tarfile.open(path, "w:gz") as archive:
        _tar_member(archive, "package/package.json", json.dumps(manifest).encode())
        _tar_member(archive, "package/README.md", b"# ReviewSensei\n")
        _tar_member(archive, "package/LICENSE", b"MIT License\n")
        _tar_member(archive, "package/bin/review-sensei", b"native", 0o755)
        if extra_member is not None:
            _tar_member(archive, extra_member, b"unexpected")
    return path


class ValidatorTests(unittest.TestCase):
    def test_valid_tarball_and_negative_metadata_cases(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            valid = _valid_platform_tar(directory)
            self.assertEqual(
                VALIDATOR.validate_tarball(valid, "0.1.0")["role"], "platform"
            )
            for lifecycle in VALIDATOR.LIFECYCLE_KEYS:
                with self.assertRaises(VALIDATOR.NpmPackageValidationError):
                    VALIDATOR.validate_tarball(
                        _valid_platform_tar(directory, lifecycle=lifecycle), "0.1.0"
                    )
            with self.assertRaises(VALIDATOR.NpmPackageValidationError):
                VALIDATOR.validate_tarball(
                    _valid_platform_tar(directory, version="0.2.0"), "0.1.0"
                )
            for extra_member in ("package/customer-review.json", "outside-package.txt"):
                with self.assertRaises(VALIDATOR.NpmPackageValidationError):
                    VALIDATOR.validate_tarball(
                        _valid_platform_tar(directory, extra_member=extra_member),
                        "0.1.0",
                    )
            with self.assertRaises(VALIDATOR.NpmPackageValidationError):
                VALIDATOR.validate_tarball(
                    _valid_platform_tar(directory, files=["**"]), "0.1.0"
                )
            with self.assertRaises(VALIDATOR.NpmPackageValidationError):
                VALIDATOR.validate_tarball(
                    _valid_platform_tar(directory, repository={}), "0.1.0"
                )
            with self.assertRaises(VALIDATOR.NpmPackageValidationError):
                VALIDATOR.validate_tarball(
                    _valid_platform_tar(directory, command_bin=True), "0.1.0"
                )

            certifi_bundle = _valid_platform_tar(
                directory, extra_member="package/bin/_internal/certifi/cacert.pem"
            )
            self.assertEqual(
                VALIDATOR.validate_tarball(certifi_bundle, "0.1.0")["role"],
                "platform",
            )
            for private_path in (
                "package/bin/other.pem",
                "package/bin/_internal/not-certifi/cacert.pem",
            ):
                with self.assertRaises(VALIDATOR.NpmPackageValidationError):
                    VALIDATOR.validate_tarball(
                        _valid_platform_tar(directory, extra_member=private_path),
                        "0.1.0",
                    )

    def test_validator_rejects_traversal_links_special_and_private_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            path = directory / "unsafe.tgz"
            with tarfile.open(path, "w:gz") as archive:
                _tar_member(archive, "package/package.json", b"{}")
                _tar_member(archive, "package/LICENSE", b"MIT License")
                _tar_member(archive, "package/bin/review-sensei", b"x", 0o755)
                _tar_member(archive, "package/../escape", b"x")
            with self.assertRaises(VALIDATOR.NpmPackageValidationError):
                VALIDATOR.validate_tarball(path)

            drive_path = directory / "drive.tgz"
            with tarfile.open(drive_path, "w:gz") as archive:
                _tar_member(archive, "C:escape", b"x")
            with self.assertRaises(VALIDATOR.NpmPackageValidationError):
                VALIDATOR.validate_tarball(drive_path)

            link = directory / "link.tgz"
            with tarfile.open(link, "w:gz") as archive:
                info = tarfile.TarInfo("package/bin/review-sensei")
                info.type = tarfile.SYMTYPE
                info.linkname = "/tmp/secret"
                archive.addfile(info)
            with self.assertRaises(VALIDATOR.NpmPackageValidationError):
                VALIDATOR.validate_tarball(link)

            for member_type in (
                tarfile.LNKTYPE,
                tarfile.FIFOTYPE,
                tarfile.CHRTYPE,
                tarfile.BLKTYPE,
            ):
                special = directory / f"special-{member_type.decode('ascii')}.tgz"
                with tarfile.open(special, "w:gz") as archive:
                    info = tarfile.TarInfo("package/special")
                    info.type = member_type
                    info.linkname = "target"
                    archive.addfile(info)
                with self.assertRaises(VALIDATOR.NpmPackageValidationError):
                    VALIDATOR.validate_tarball(special)

    def test_validator_enforces_archive_member_and_expanded_size_limits(self):
        class FakeArchive:
            def __init__(self, members):
                self.members = members

            def getmembers(self):
                return self.members

        oversized = tarfile.TarInfo("package/oversized")
        oversized.size = VALIDATOR.MAX_MEMBER_SIZE + 1
        with self.assertRaises(VALIDATOR.NpmPackageValidationError):
            VALIDATOR._archive_members(FakeArchive([oversized]))

        too_many = [
            tarfile.TarInfo(f"package/{index}")
            for index in range(VALIDATOR.MAX_ARCHIVE_MEMBERS + 1)
        ]
        with self.assertRaises(VALIDATOR.NpmPackageValidationError):
            VALIDATOR._archive_members(FakeArchive(too_many))

        too_large_total = [
            tarfile.TarInfo(f"package/chunk-{index}") for index in range(4)
        ]
        for member in too_large_total:
            member.size = VALIDATOR.MAX_MEMBER_SIZE
        too_large_total.append(tarfile.TarInfo("package/overflow"))
        too_large_total[-1].size = 1
        with self.assertRaises(VALIDATOR.NpmPackageValidationError):
            VALIDATOR._archive_members(FakeArchive(too_large_total))

    def test_directory_validator_rejects_symlink_lifecycle_and_license(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "package"
            (package / "bin").mkdir(parents=True)
            (package / "bin/review-sensei").write_bytes(b"x")
            (package / "bin/review-sensei").chmod(0o755)
            (package / "LICENSE").write_text("GPL\n", encoding="utf-8")
            manifest = {
                "name": "@reviewsensei/cli-linux-x64-gnu",
                "version": "0.1.0",
                "license": "MIT",
                "os": ["linux"],
                "cpu": ["x64"],
                "scripts": {"install": "unsafe"},
            }
            (package / "package.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            with self.assertRaises(VALIDATOR.NpmPackageValidationError):
                VALIDATOR.validate_package_directory(package, "0.1.0")

    def test_directory_validator_rejects_unlisted_member(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "package"
            (package / "bin").mkdir(parents=True)
            payload = package / "bin/review-sensei"
            payload.write_bytes(b"x")
            payload.chmod(0o755)
            (package / "LICENSE").write_text("MIT License\n", encoding="utf-8")
            (package / "README.md").write_text("# package\n", encoding="utf-8")
            (package / "customer-review.json").write_text("{}", encoding="utf-8")
            (package / "package.json").write_text(
                json.dumps(
                    {
                        "name": "@reviewsensei/cli-linux-x64-gnu",
                        "version": "0.1.0",
                        "license": "MIT",
                        "os": ["linux"],
                        "cpu": ["x64"],
                        "files": ["bin", "README.md", "LICENSE"],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(VALIDATOR.NpmPackageValidationError):
                VALIDATOR.validate_package_directory(package, "0.1.0")

    def test_directory_validator_rejects_symlinked_package_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            package = directory / "package"
            package.mkdir()
            alias = directory / "alias"
            try:
                alias.symlink_to(package, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlinks unavailable: {exc}")
            with self.assertRaises(VALIDATOR.NpmPackageValidationError):
                VALIDATOR.validate_package_directory(alias, "0.1.0")


class SmokeTests(unittest.TestCase):
    def test_smoke_input_validation_does_not_require_native_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SMOKE.SmokeError):
                SMOKE.validate_executable(Path(tmp) / "missing")

            executable = Path(tmp) / "review-sensei"
            executable.write_bytes(b"native")
            executable.chmod(0o755)
            alias = Path(tmp) / "alias"
            try:
                alias.symlink_to(executable)
            except OSError as exc:
                self.skipTest(f"file symlinks unavailable: {exc}")
            with self.assertRaises(SMOKE.SmokeError):
                SMOKE.validate_executable(alias)

    def test_prepare_diff_does_not_delete_preexisting_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = Path(tmp)
            executable = repository / "review-sensei"
            executable.write_bytes(b"native")
            executable.chmod(0o755)
            sentinel = repository / "standalone-smoke-pr.patch"
            sentinel.write_bytes(b"keep me")
            with self.assertRaises(SMOKE.SmokeError):
                SMOKE.compare_case(
                    "preexisting",
                    [],
                    executable=executable,
                    python_executable="python",
                    repository=repository,
                    output_path=sentinel,
                )

            def fake_run(command, cwd, env):
                if "--output" in command:
                    output_name = command[command.index("--output") + 1]
                    (Path(cwd) / output_name).write_bytes(b"matching patch")
                return subprocess.CompletedProcess(command, 0, "ok\n", "")

            with patch.object(SMOKE, "_run", side_effect=fake_run):
                report = SMOKE.run_smoke(
                    executable,
                    repository=repository,
                    base_ref="base",
                    head_ref="head",
                    python_executable="python",
                )
            self.assertEqual(len(report), 3)
            self.assertEqual(sentinel.read_bytes(), b"keep me")


if __name__ == "__main__":
    unittest.main()

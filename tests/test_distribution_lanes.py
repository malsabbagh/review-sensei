from __future__ import annotations

import ast
import importlib.util
import io
import json
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = json.loads(
    (ROOT / "tests" / "fixtures" / "distribution-contract.json").read_text(
        encoding="utf-8"
    )
)


def _load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _manifest_directives() -> list[str]:
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    return [line.strip() for line in manifest.splitlines() if line.strip()]


def _manifest_covers(relative: str) -> bool:
    directives = _manifest_directives()
    if f"include {relative}" in directives:
        return True
    for directive in directives:
        if not directive.startswith("graft "):
            continue
        grafted = directive.removeprefix("graft ").strip()
        if relative == grafted or relative.startswith(f"{grafted}/"):
            return True
    return False


def _top_level_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def _lane_modules() -> list[Path]:
    modules: list[Path] = []
    for relative in CONTRACT["lanes"]["dist_safe"]["discover"]:
        modules.extend(sorted((ROOT / relative).rglob("*.py")))
    return modules


class DistributionLaneTests(unittest.TestCase):
    def test_checkout_only_modules_exist_and_are_excluded_from_the_contract_lane(self):
        modules = CONTRACT["lanes"]["checkout_only"]["modules"]
        self.assertTrue(modules)
        for relative in modules:
            with self.subTest(relative=relative):
                self.assertTrue((ROOT / relative).is_file())
        for relative in CONTRACT["lanes"]["dist_safe"]["helpers"]:
            with self.subTest(helper=relative):
                self.assertTrue((ROOT / relative).is_file())
        for relative in CONTRACT["lanes"]["dist_safe"]["discover"]:
            with self.subTest(discover=relative):
                self.assertTrue((ROOT / relative).is_dir())

    def test_ci_and_manifest_match_the_recorded_distribution_contract(self):
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        command = CONTRACT["supported_command"]
        self.assertIn(command["build"], ci)
        self.assertIn(command["record_digests"], ci)
        self.assertIn("pip install dist/*.whl", ci)
        self.assertIn('unittest discover -s "$suite/dist_safe" -v', ci)
        self.assertIn('unittest discover -s "$suite/downstream" -v', ci)
        self.assertIn("prune tests", manifest)
        self.assertIn("graft tests/dist_safe", manifest)
        self.assertIn("graft tests/downstream", manifest)
        canary = ROOT / CONTRACT["canary"]["workflow"]
        self.assertTrue(canary.is_file())
        canary_text = canary.read_text(encoding="utf-8")
        self.assertIn(CONTRACT["canary"]["environment"], canary_text)

    def test_ci_verifies_the_sdist_through_the_contract_verifier(self):
        """CI must not repeat the packaged allowlist as inline tar assertions."""

        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        verifier = CONTRACT["sdist_entries"]["verifier"]
        self.assertTrue((ROOT / verifier).is_file())
        self.assertIn(f"python {verifier}", ci)
        self.assertNotIn("tar -tzf dist/*.tar.gz | grep", ci)

    def test_manifest_covers_every_packaged_lane_asset(self):
        """A helper added to the contract but not MANIFEST.in fails here first.

        Without this the omission would only surface as a ModuleNotFoundError or
        FileNotFoundError in the CI dist-safe lane, after the wheel install.
        """

        for relative in CONTRACT["lanes"]["dist_safe"]["helpers"]:
            with self.subTest(helper=relative):
                self.assertTrue(_manifest_covers(relative), relative)
        for relative in CONTRACT["lanes"]["dist_safe"]["discover"]:
            with self.subTest(discover=relative):
                self.assertTrue(_manifest_covers(relative), relative)
        for relative in CONTRACT["sdist_entries"]["required"]:
            with self.subTest(required=relative):
                self.assertTrue((ROOT / relative).is_file())
        directives = _manifest_directives()
        grafts = [
            index
            for index, directive in enumerate(directives)
            if directive.startswith("graft tests/")
        ]
        self.assertTrue(grafts)
        # MANIFEST.in applies directives in order, so the prune must come first
        # or it would discard the re-grafted lane directories.
        self.assertLess(directives.index("prune tests"), min(grafts))

    def test_dist_safe_lane_imports_only_declared_helpers(self):
        """Every tests-root import in the lane must be a packaged helper."""

        helpers = set(CONTRACT["lanes"]["dist_safe"]["helpers"])
        lane_modules = _lane_modules()
        self.assertTrue(lane_modules)
        for module in lane_modules:
            for name in sorted(_top_level_imports(module)):
                candidate = ROOT / "tests" / f"{name}.py"
                if not candidate.is_file():
                    continue
                with self.subTest(module=module.name, imported=name):
                    self.assertIn(f"tests/{name}.py", helpers)

    def test_sdist_verifier_fails_closed_on_missing_and_forbidden_entries(self):
        verifier = _load_script(Path(CONTRACT["sdist_entries"]["verifier"]).name)
        required = verifier.required_entries(CONTRACT)
        forbidden = verifier.forbidden_entries(CONTRACT)
        self.assertIn("tests/packaging_guard.py", required)
        self.assertIn("tests/downstream/test_acceptance.py", required)
        self.assertIn("tests/test_ci_policy.py", forbidden)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            complete = self._write_archive(root / "complete.tar.gz", required)
            self.assertEqual(verifier.check(complete, CONTRACT), [])
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(verifier.main([str(complete)]), 0)
            self.assertIn("required entries present", stdout.getvalue())

            dropped = sorted(set(required) - {"tests/packaging_guard.py"})
            incomplete = self._write_archive(root / "incomplete.tar.gz", dropped)
            problems = verifier.check(incomplete, CONTRACT)
            self.assertEqual(problems, ["missing from sdist: tests/packaging_guard.py"])
            self.assertEqual(verifier.main([str(incomplete)]), 1)

            leaked = self._write_archive(
                root / "leaked.tar.gz", [*required, "tests/test_ci_policy.py"]
            )
            self.assertEqual(
                verifier.check(leaked, CONTRACT),
                ["checkout-only module packaged in sdist: tests/test_ci_policy.py"],
            )

    def _write_archive(self, path: Path, entries: list[str]) -> Path:
        payload = b"placeholder\n"
        with tarfile.open(path, "w:gz") as tar:
            for relative in entries:
                info = tarfile.TarInfo(f"review_sensei-0.0.0/{relative}")
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
        return path


if __name__ == "__main__":
    unittest.main()

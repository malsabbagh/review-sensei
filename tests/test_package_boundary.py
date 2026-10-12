"""The review engine does not import the hosted GitHub runtime."""

from __future__ import annotations

import ast
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "src" / "review_sensei"
HOSTED = (
    ROOT
    / "packages"
    / "python"
    / "review-sensei-github"
    / "src"
    / "review_sensei_github"
)
FORBIDDEN = ("review_sensei_github", "review_sensei.hosting")


def _imported_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module)
        elif isinstance(node, ast.Call):
            func = node.func
            imported = func.attr if isinstance(func, ast.Attribute) else None
            if imported == "import_module" or (
                isinstance(func, ast.Name) and func.id == "__import__"
            ):
                if node.args and isinstance(node.args[0], ast.Constant):
                    value = node.args[0].value
                    if isinstance(value, str):
                        roots.add(value)
    return roots


class PackageBoundaryTests(unittest.TestCase):
    def test_engine_does_not_import_the_hosted_runtime(self) -> None:
        offenders: list[str] = []
        for path in ENGINE.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for root in _imported_roots(tree):
                if root == "review_sensei_github" or root.startswith(
                    "review_sensei_github."
                ):
                    offenders.append(path.relative_to(ROOT).as_posix())
                if root == "review_sensei.hosting" or root.startswith(
                    "review_sensei.hosting."
                ):
                    offenders.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(offenders, [])

    def test_trigger_shim_matches_the_hosted_resolver(self) -> None:
        shim = ENGINE / "hosting" / "github" / "trigger.py"
        hosted = HOSTED / "trigger.py"
        self.assertEqual(shim.read_bytes(), hosted.read_bytes())

    def test_hosted_package_pins_the_engine_version(self) -> None:
        engine = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        hosted = tomllib.loads(
            (
                ROOT / "packages" / "python" / "review-sensei-github" / "pyproject.toml"
            ).read_text(encoding="utf-8")
        )
        version = engine["project"]["version"]
        self.assertIn(f"review-sensei=={version}", hosted["project"]["dependencies"])


if __name__ == "__main__":
    unittest.main()

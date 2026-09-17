from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "docs" / "site" / "data" / "site-manifest.json"
SCHEMA_PATH = ROOT / "docs" / "site" / "schemas" / "site-manifest.schema.json"


def _load_module(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


validate_module = _load_module("validate_site_manifest")
build_module = _load_module("build_site_pages")


class SiteManifestValidationTests(unittest.TestCase):
    def test_committed_manifest_passes(self) -> None:
        document = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        validated = validate_module.validate_site_manifest(
            document, root=ROOT, schema=schema
        )
        self.assertEqual(
            validated["release_facts"]["version"],
            validate_module.project_version(ROOT),
        )

    def test_wrong_release_version_is_rejected(self) -> None:
        document = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        document["release_facts"]["version"] = "9.9.9"
        document["release_facts"]["tag"] = "v9.9.9"
        with self.assertRaises(validate_module.SiteManifestError):
            validate_module.validate_site_manifest(document, root=ROOT, schema=schema)

    def test_openrouter_marked_supported_is_rejected(self) -> None:
        document = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        for entry in document["providers"]:
            if entry["id"] == "openrouter":
                entry["status_labels"] = ["supported", "available-in-distribution"]
        with self.assertRaises(validate_module.SiteManifestError):
            validate_module.validate_site_manifest(document, root=ROOT, schema=schema)

    def test_missing_evidence_path_is_rejected(self) -> None:
        document = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        document["providers"][0]["evidence"].append("does/not/exist.py")
        with self.assertRaises(validate_module.SiteManifestError):
            validate_module.validate_site_manifest(document, root=ROOT, schema=schema)

    def test_unknown_registry_provider_is_rejected(self) -> None:
        document = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        document["providers"][0]["provider"] = "missing-provider"
        document["providers"][0]["engine_support"] = {
            "cli": True,
            "evaluate": True,
            "npx_launcher": True,
        }
        with self.assertRaises(validate_module.SiteManifestError):
            validate_module.validate_site_manifest(document, root=ROOT, schema=schema)

    def test_shipped_provider_without_implemented_label_is_rejected(self) -> None:
        document = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        document["providers"][0]["status_labels"] = ["supported"]
        with self.assertRaises(validate_module.SiteManifestError):
            validate_module.validate_site_manifest(document, root=ROOT, schema=schema)

    def test_validate_script_exits_zero(self) -> None:
        self.assertEqual(validate_module.main([]), 0)

    def test_build_generates_provider_and_release_pages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            providers = directory / "providers" / "index.html"
            releases = directory / "releases" / "index.html"
            build_module.build_site_pages(
                manifest_path=MANIFEST_PATH,
                providers_output=providers,
                releases_output=releases,
            )
            provider_html = providers.read_text(encoding="utf-8")
            release_html = releases.read_text(encoding="utf-8")
            self.assertIn("Ollama (local)", provider_html)
            self.assertIn("OpenRouter", provider_html)
            self.assertIn("not implemented", provider_html)
            self.assertIn(
                f"Version {validate_module.project_version(ROOT)}", release_html
            )
            self.assertIn("review-sensei-run.yml", release_html)

    def test_committed_site_pages_match_manifest(self) -> None:
        build_module.check_site_pages(manifest_path=MANIFEST_PATH)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Validate the ReviewSensei site manifest against schema and repository facts."""

from __future__ import annotations

import argparse
import ast
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, ValidationError

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "docs" / "site" / "data" / "site-manifest.json"
DEFAULT_SCHEMA = ROOT / "docs" / "site" / "schemas" / "site-manifest.schema.json"
NPM_LAUNCHER_RELATIVE = Path("packages") / "npm" / "cli" / "package.json"
REGISTRY_RELATIVE_PATH = Path("src") / "review_sensei" / "providers" / "registry.py"

UNSUPPORTED_USER_FACING_LABELS = frozenset({"supported", "available-in-distribution"})
UNSHIPPED_STATUS_LABELS = frozenset(
    {"not-implemented", "planned", "source-only", "experimental"}
)


class SiteManifestError(ValueError):
    """Raised when the site manifest fails validation."""


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SiteManifestError(f"could not read JSON from {path}") from exc


def project_version(root: Path) -> str:
    try:
        with (root / "pyproject.toml").open("rb") as handle:
            project = tomllib.load(handle).get("project", {})
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SiteManifestError("project metadata could not be read") from exc
    version = project.get("version") if isinstance(project, dict) else None
    if not isinstance(version, str) or not version:
        raise SiteManifestError("project version is missing")
    return version


def npm_launcher_version(root: Path) -> str:
    launcher = root / NPM_LAUNCHER_RELATIVE
    if not launcher.exists():
        raise SiteManifestError("npm launcher package.json is missing")
    value = _load_json(launcher)
    if not isinstance(value, dict):
        raise SiteManifestError("npm launcher package.json is invalid")
    version = value.get("version")
    if not isinstance(version, str) or not version:
        raise SiteManifestError("npm launcher version is missing")
    return version


def registered_provider_names(root: Path) -> frozenset[str]:
    registry_path = root / REGISTRY_RELATIVE_PATH
    try:
        tree = ast.parse(registry_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise SiteManifestError("provider registry could not be parsed") from exc
    names: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "register"
            and isinstance(func.value, ast.Name)
            and func.value.id == "registry"
        ):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            names.append(first.value)
    if not names:
        raise SiteManifestError("provider registry registrations could not be parsed")
    return frozenset(names)


def validate_schema(document: Any, schema: dict[str, Any]) -> None:
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(document)


def validate_evidence_paths(root: Path, document: dict[str, Any]) -> None:
    missing: list[str] = []
    for entry in document["providers"]:
        for path_text in entry["evidence"]:
            if not (root / path_text).exists():
                missing.append(path_text)
    release = document["release_facts"]
    for reference in release["workflow_references"]:
        if not (root / reference["path"]).exists():
            missing.append(reference["path"])
    for path_text in release.get("documentation", ()):
        if not (root / path_text).exists():
            missing.append(path_text)
    compatibility_manifest = release.get("compatibility_manifest")
    if isinstance(compatibility_manifest, str) and compatibility_manifest:
        if not (root / compatibility_manifest).exists():
            missing.append(compatibility_manifest)
    if missing:
        raise SiteManifestError(
            "manifest evidence references missing paths: "
            + ", ".join(sorted(set(missing)))
        )


def validate_release_alignment(root: Path, document: dict[str, Any]) -> None:
    release = document["release_facts"]
    expected = project_version(root)
    manifest_version = release["version"]
    if manifest_version != expected:
        raise SiteManifestError(
            f"release_facts.version {manifest_version!r} does not match "
            f"pyproject.toml {expected!r}"
        )
    npm_version = npm_launcher_version(root)
    if manifest_version != npm_version:
        raise SiteManifestError(
            f"release_facts.version {manifest_version!r} does not match "
            f"{NPM_LAUNCHER_RELATIVE} {npm_version!r}"
        )
    expected_tag = f"v{expected}"
    if release["tag"] != expected_tag:
        raise SiteManifestError(
            f"release_facts.tag {release['tag']!r} does not match {expected_tag!r}"
        )


def _provider_is_shipped(entry: dict[str, Any], registered: frozenset[str]) -> bool:
    engine = entry["engine_support"]
    return entry["provider"] in registered and any(
        bool(engine.get(surface)) for surface in ("cli", "evaluate", "npx_launcher")
    )


def validate_provider_consistency(root: Path, document: dict[str, Any]) -> None:
    registered = registered_provider_names(root)
    for entry in document["providers"]:
        provider_id = entry["id"]
        provider = entry["provider"]
        labels = set(entry["status_labels"])

        if not _provider_is_shipped(entry, registered):
            if labels & UNSUPPORTED_USER_FACING_LABELS:
                raise SiteManifestError(
                    f"provider {provider_id!r} must not claim supported or "
                    "distribution-ready status before a named profile and public "
                    "operator path exist"
                )
            if not labels & UNSHIPPED_STATUS_LABELS:
                raise SiteManifestError(
                    f"provider {provider_id!r} must include at least one planned, "
                    "source-only, experimental, or not-implemented label"
                )
            continue

        if provider not in registered:
            raise SiteManifestError(
                f"provider {provider_id!r} references unknown registry key "
                f"{provider!r}; registered keys: {', '.join(sorted(registered))}"
            )
        if "implemented-on-main" not in labels:
            raise SiteManifestError(
                f"shipped provider {provider_id!r} must include implemented-on-main"
            )
        if entry["engine_support"]["cli"] and "supported" not in labels:
            raise SiteManifestError(
                f"CLI-enabled provider {provider_id!r} must include supported"
            )


def validate_site_manifest(
    document: Any,
    *,
    root: Path = ROOT,
    schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise SiteManifestError("site manifest must be a JSON object")
    if schema is None:
        schema = _load_json(DEFAULT_SCHEMA)
        if not isinstance(schema, dict):
            raise SiteManifestError("site manifest schema is invalid")
    try:
        validate_schema(document, schema)
    except ValidationError as exc:
        raise SiteManifestError(f"schema validation failed: {exc.message}") from exc
    validate_release_alignment(root, document)
    validate_evidence_paths(root, document)
    validate_provider_consistency(root, document)
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help="Path to site-manifest.json",
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=DEFAULT_SCHEMA,
        help="Path to site-manifest.schema.json",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="Repository root for evidence and version checks",
    )
    args = parser.parse_args(argv)
    try:
        document = _load_json(args.manifest)
        schema = _load_json(args.schema)
        if not isinstance(schema, dict):
            raise SiteManifestError("site manifest schema is invalid")
        validate_site_manifest(document, root=args.root, schema=schema)
    except SiteManifestError as exc:
        print(f"site manifest validation failed: {exc}", file=sys.stderr)
        return 1
    print(
        "site manifest validation passed "
        f"(release {document['release_facts']['version']}, "
        f"{len(document['providers'])} providers)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

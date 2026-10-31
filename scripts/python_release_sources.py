"""Prove recognized core build inputs unchanged when only hosted source moves."""

from __future__ import annotations

import ast
import base64
import hashlib
import re
import tomllib

import release_docs as docs

INPUTS = (
    "src",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "MANIFEST.in",
    "README.md",
    "LICENSE",
    ".gitattributes",
)
REQUIRED = {"src", "pyproject.toml", "MANIFEST.in", "README.md", "LICENSE"}
SETUPTOOLS = {
    "packages": {"find": {"where": ["src"]}},
    "package-dir": {"": "src"},
    "package-data": {
        "review_sensei": [
            "default_categories/*.json",
            "default_stages/*.json",
            "schemas/*.json",
        ]
    },
}

INSTALLATION_SELECTOR = b"# reviewsensei-core-installation: pypi-exact-v1"
WORKFLOW_PATH = ".github/workflows/review-sensei-run.yml"
# Trusted, append-only validator policy. Each digest admits the entire reviewed
# workflow under the registry-only core contract. Never derive this allowlist
# from a candidate, its selector, a receipt or downloaded release data.
REGISTRY_ONLY_WORKFLOWS = frozenset(
    {
        "2f7ea8f388968b8cadab9dae156e90a8fb636c945b28585cde6ae04412bccf0c",
        # Current run-owned lifecycle; exact four installation lanes reviewed.
        "5c06dc37b6875df24c9ac6ece3a410d32eeb7b1d2671e7c1bfae412703656185",
        # actions/setup-python v7.1.0 pin update across the four installation lanes.
        "9df61a2e77436be2bb609379618dae2e3bcd8f5752267cb17c810b17259f6ada",
        # Explicit isolated quarantine intent; the four exact installation lanes
        # retain their registry-only core and source-owned adapter contracts.
        "9b1d9c66ece39c17fe393ec7b634ba5408ceafabc38f39451f3e8e5bacb867a9",
        # Controlled restart invocation; all four core installers retain
        # the independently reviewed registry-only installation contract.
        "27c3041eda29f4ac49240f00de0049b17a94654a9aada7e00d2c0efe3e806ab4",
        # Independently reviewed protected hold and frozen later-package launch;
        # normal core installation lanes retain their exact registry contract.
        "c2044264760b7a1837fdf103926a877a4602403a1446a9b4380fdb41c4a215bd",
    }
)


def registry_core_contract(raw: bytes) -> dict | None:
    """Recognize reviewed exact bytes, without executing selected source code."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= 256 * 1024:
        raise ValueError("core installation workflow bytes are invalid or oversized")
    digest = hashlib.sha256(raw).hexdigest()
    if b"reviewsensei-core-installation:" not in raw:
        return None  # Historical workflow-source fallback still needs equivalence.
    declarations = [
        line for line in raw.splitlines() if b"reviewsensei-core-installation:" in line
    ]
    if declarations != [INSTALLATION_SELECTOR] or digest not in REGISTRY_ONLY_WORKFLOWS:
        raise ValueError("registry-only core installation workflow is not recognized")
    return {
        "mode": "pypi-exact-v1",
        "workflow_sha256": digest,
        "installation_lanes": 4,
    }


def workflow_source_bytes(source: str) -> tuple[bytes, str]:
    """Read one regular immutable Git path with bounded complete inventories."""
    if not isinstance(source, str) or not docs.SHA.fullmatch(source):
        raise ValueError("core installation needs an exact workflow source")
    commit = docs.api(f"git/commits/{source}")
    if not isinstance(commit, dict) or commit.get("sha") != source:
        raise ValueError("core installation workflow commit is unavailable")
    selected_tree = commit.get("tree")
    tree = selected_tree.get("sha") if isinstance(selected_tree, dict) else None
    if not isinstance(tree, str) or not docs.SHA.fullmatch(tree):
        raise ValueError("core installation workflow tree is unavailable")
    for index, name in enumerate(WORKFLOW_PATH.split("/")):
        inventory = docs.api(f"git/trees/{tree}")
        entries = inventory.get("tree") if isinstance(inventory, dict) else None
        if (
            not isinstance(inventory, dict)
            or inventory.get("sha") != tree
            or inventory.get("truncated") is not False
            or not isinstance(entries, list)
            or not 0 < len(entries) <= 4096
        ):
            raise ValueError("core installation workflow inventory is incomplete")
        selected = {}
        for item in entries:
            path = item.get("path") if isinstance(item, dict) else None
            if (
                not isinstance(path, str)
                or not path
                or "/" in path
                or "\\" in path
                or path in {".", ".."}
                or path in selected
            ):
                raise ValueError("core installation workflow inventory is invalid")
            selected[path] = item
        item = selected.get(name, {})
        digest = item.get("sha")
        leaf = index == 2
        if (
            not isinstance(digest, str)
            or not docs.SHA.fullmatch(digest)
            or (not leaf and (item.get("type"), item.get("mode")) != ("tree", "040000"))
            or (
                leaf
                and (
                    item.get("type") != "blob"
                    or item.get("mode") not in {"100644", "100755"}
                )
            )
        ):
            raise ValueError("core installation workflow path is not regular")
        tree = digest
    return source_blob(tree), tree


def core_installation(engine_source: str, workflow_source: str) -> dict | None:
    if not isinstance(engine_source, str) or not docs.SHA.fullmatch(engine_source):
        raise ValueError("core installation needs an exact published engine source")
    raw, blob = workflow_source_bytes(workflow_source)
    contract = registry_core_contract(raw)
    if contract is None:
        return None
    return {
        **contract,
        "engine_source": engine_source,
        "workflow_source": workflow_source,
        "workflow_path": WORKFLOW_PATH,
        "workflow_blob_sha": blob,
    }


def root_inputs(source: str) -> dict:
    if not docs.SHA.fullmatch(source):
        raise ValueError("core equivalence needs exact immutable sources")
    commit = docs.api(f"git/commits/{source}")
    tree = commit.get("tree", {}).get("sha")
    if (
        commit.get("sha") != source
        or not isinstance(tree, str)
        or not docs.SHA.fullmatch(tree)
    ):
        raise ValueError("core source commit/tree identity is unavailable")
    inventory = docs.api(f"git/trees/{tree}")
    entries = inventory.get("tree")
    if (
        inventory.get("sha") != tree
        or inventory.get("truncated") is not False
        or not isinstance(entries, list)
        or not 0 < len(entries) <= 4096
    ):
        raise ValueError("core root tree inventory is incomplete")
    indexed = {}
    for item in entries:
        path = item.get("path")
        if (
            not isinstance(path, str)
            or not path
            or "/" in path
            or "\\" in path
            or path in indexed
        ):
            raise ValueError("core root tree entry is invalid or duplicate")
        indexed[path] = item
    selected = {}
    for name in INPUTS:
        item = indexed.get(name)
        if item is None:
            if name in REQUIRED:
                raise ValueError("required core build input is missing")
            selected[name] = None
            continue
        kind, mode, digest = item.get("type"), item.get("mode"), item.get("sha")
        if (
            not isinstance(digest, str)
            or not docs.SHA.fullmatch(digest)
            or (name == "src" and (kind, mode) != ("tree", "040000"))
            or (name != "src" and (kind != "blob" or mode not in {"100644", "100755"}))
        ):
            raise ValueError("core build input is not a regular file/tree")
        selected[name] = {"type": kind, "mode": mode, "sha": digest}
    return selected


def source_blob(digest: str) -> bytes:
    value = docs.api(f"git/blobs/{digest}")
    if (
        not isinstance(value, dict)
        or value.get("sha") != digest
        or value.get("encoding") != "base64"
        or type(value.get("size")) is not int
        or not 0 < value["size"] <= 256 * 1024
        or not isinstance(value.get("content"), str)
        or len(value["content"]) > 512 * 1024
    ):
        raise ValueError("core build recipe blob is unavailable or oversized")
    raw = base64.b64decode("".join(value["content"].split()), validate=True)
    identity = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()
    if len(raw) != value["size"] or identity != digest:
        raise ValueError("core build recipe bytes differ from the Git object")
    return raw


def recognized_recipe(inputs: dict) -> None:
    project = tomllib.loads(source_blob(inputs["pyproject.toml"]["sha"]).decode())
    if project.get("build-system") != {
        "requires": ["setuptools>=77.0", "jsonschema>=4.18,<5"],
        "build-backend": "setuptools.build_meta",
    }:
        raise ValueError("cross-source core reuse needs the recognized build backend")
    metadata = project.get("project", {})
    if (
        metadata.get("dynamic") not in (None, [])
        or metadata.get("readme") != "README.md"
        or not isinstance(metadata.get("license"), str)
        or metadata.get("license-files") != ["LICENSE"]
        or project.get("tool", {}).get("setuptools") != SETUPTOOLS
        or inputs["setup.cfg"] is not None
    ):
        raise ValueError("cross-source core metadata/build graph is unrecognized")
    if inputs["setup.py"] is not None:
        body = ast.parse(source_blob(inputs["setup.py"]["sha"])).body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body = body[1:]
        expected = ast.parse("from setuptools import setup\nsetup()\n").body
        if [ast.dump(node) for node in body] != [ast.dump(node) for node in expected]:
            raise ValueError("cross-source core setup shim contains custom behavior")


def regular_source_tree(digest: str) -> None:
    inventory = docs.api(f"git/trees/{digest}?recursive=1")
    entries = inventory.get("tree")
    if (
        inventory.get("sha") != digest
        or inventory.get("truncated") is not False
        or not isinstance(entries, list)
        or not 0 < len(entries) <= 10000
    ):
        raise ValueError("core source tree inventory is incomplete")
    seen = set()
    for item in entries:
        path = item.get("path")
        if (
            not isinstance(path, str)
            or not path
            or path.startswith("/")
            or "\\" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or path in seen
            or not isinstance(item.get("sha"), str)
            or not docs.SHA.fullmatch(item["sha"])
            or (item.get("type"), item.get("mode"))
            not in {("tree", "040000"), ("blob", "100644"), ("blob", "100755")}
        ):
            raise ValueError("core source tree has unsupported or duplicate entries")
        seen.add(path)
    if "review_sensei/__init__.py" not in seen:
        raise ValueError("core source package is missing")


def core_equivalence(engine_source: str, workflow_source: str) -> dict:
    if not all(
        isinstance(value, str) and re.fullmatch(r"[a-f0-9]{40}", value)
        for value in (engine_source, workflow_source)
    ):
        raise ValueError("core equivalence needs exact immutable sources")
    result = {"engine_source": engine_source, "workflow_source": workflow_source}
    if engine_source == workflow_source:
        return {**result, "mode": "same-source"}
    engine, workflow = root_inputs(engine_source), root_inputs(workflow_source)
    if engine != workflow:
        raise ValueError("workflow source changes published core build inputs")
    recognized_recipe(engine)
    regular_source_tree(engine["src"]["sha"])
    return {**result, "mode": "recognized-build-input-equivalence", "inputs": engine}

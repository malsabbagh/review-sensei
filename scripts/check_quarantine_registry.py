#!/usr/bin/env python3
"""Read-only append-only registration check against an exact prior commit."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path


class RegistryCheckError(ValueError):
    """Candidate source cannot preserve its released registration prefix."""


def require_registry_successor(before, after):
    # Compare data using only stdlib before installing candidate code. Runtime
    # closed row validation and authority issuance remain separate.
    def rows(raw):
        if len(raw) > 32_768:
            raise RegistryCheckError("registry exceeds its byte bound")
        value = json.loads(raw)
        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "registrations"}
            or value["schema_version"] != "quarantine-activation-registry-v1"
            or not isinstance(value["registrations"], list)
            or len(value["registrations"]) > 32
            or any(not isinstance(row, dict) for row in value["registrations"])
        ):
            raise RegistryCheckError("registry container is unsupported")
        return value["registrations"]

    previous, candidate = rows(before), rows(after)
    if candidate[: len(previous)] != previous:
        raise RegistryCheckError(
            "registered creation slots cannot be deleted, reordered or reassigned"
        )
    return candidate


REGISTRY = "packages/python/review-sensei-github/src/review_sensei_github/quarantine_activations.json"
EMPTY = b'{"schema_version":"quarantine-activation-registry-v1","registrations":[]}'


def check(root: Path, base_revision: str):
    if re.fullmatch(r"[a-f0-9]{40}", base_revision) is None:
        raise RegistryCheckError("registry baseline must be an exact existing commit")

    def git(*arguments, maximum):
        result = subprocess.run(
            ["git", "--no-pager", *arguments],
            cwd=root,
            capture_output=True,
            check=True,
            timeout=30,
        )
        if len(result.stdout) > maximum:
            raise RegistryCheckError("registry baseline exceeds its read bound")
        return result.stdout

    if git("cat-file", "-t", base_revision, maximum=16) != b"commit\n":
        raise RegistryCheckError("registry baseline is not a commit")
    tree = git("ls-tree", "-z", base_revision, "--", REGISTRY, maximum=1024)
    if not tree:
        previous = EMPTY
    else:
        fields = tree.rstrip(b"\0").split(b"\t")
        if (
            len(fields) != 2
            or fields[1] != REGISTRY.encode()
            or not re.fullmatch(rb"100644 blob [a-f0-9]{40}", fields[0])
        ):
            raise RegistryCheckError(
                "registry baseline is not the expected regular file"
            )
        previous = git(
            "cat-file", "blob", base_revision + ":" + REGISTRY, maximum=32_768
        )
    target = root / REGISTRY
    if target.is_symlink() or not target.is_file() or target.stat().st_size > 32_768:
        raise RegistryCheckError("candidate registry is unavailable or oversized")
    return require_registry_successor(previous, target.read_bytes())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-revision", required=True)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        rows = check(args.root, args.base_revision)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, f"quarantine registry check failed: {error}\n")
    print(f"quarantine registry unchanged/append-only: {len(rows)} entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

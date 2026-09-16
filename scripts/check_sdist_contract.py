#!/usr/bin/env python3
"""Verify a built sdist against the recorded distribution contract.

``tests/fixtures/distribution-contract.json`` is the single source of truth for
the entries the dist-safe lane needs inside the sdist. Adding a helper, fixture,
or lane module therefore requires editing the contract and ``MANIFEST.in`` only:
this checker, the CI package job, and the checkout lane tests all read the
contract instead of repeating the allowlist.
"""

from __future__ import annotations

import argparse
import json
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "tests" / "fixtures" / "distribution-contract.json"


def load_contract(path: Path = CONTRACT_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_archive(contract: dict, archive: Path | None) -> Path:
    if archive is not None:
        return archive
    pattern = contract["archive_identification"]["sdist_glob"]
    matches = sorted(ROOT.glob(pattern))
    if len(matches) != 1:
        raise SystemExit(
            f"expected exactly one sdist matching {pattern}, found {len(matches)}"
        )
    return matches[0]


def archive_entries(archive: Path) -> set[str]:
    """Return archive members with the ``review_sensei-<version>/`` root removed."""

    entries: set[str] = set()
    with tarfile.open(archive, "r:gz") as tar:
        for name in tar.getnames():
            _, separator, relative = name.partition("/")
            if separator and relative:
                entries.add(relative)
    return entries


def required_entries(contract: dict) -> list[str]:
    """Return every path the dist-safe lane must find inside the sdist."""

    lane = contract["lanes"]["dist_safe"]
    entries = list(contract["sdist_entries"]["required"])
    entries.extend(lane["helpers"])
    for relative in lane["discover"]:
        directory = ROOT / relative
        if not directory.is_dir():
            raise SystemExit(f"contract discover path is missing: {relative}")
        modules = sorted(directory.rglob("*.py"))
        if not modules:
            raise SystemExit(f"contract discover path has no modules: {relative}")
        entries.extend(module.relative_to(ROOT).as_posix() for module in modules)
    return sorted(dict.fromkeys(entries))


def forbidden_entries(contract: dict) -> list[str]:
    return sorted(contract["lanes"]["checkout_only"]["modules"])


def check(archive: Path, contract: dict) -> list[str]:
    entries = archive_entries(archive)
    problems = [
        f"missing from sdist: {relative}"
        for relative in required_entries(contract)
        if relative not in entries
    ]
    problems.extend(
        f"checkout-only module packaged in sdist: {relative}"
        for relative in forbidden_entries(contract)
        if relative in entries
    )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "archive",
        nargs="?",
        type=Path,
        help="sdist tarball to inspect; defaults to the recorded sdist glob",
    )
    arguments = parser.parse_args(argv)
    contract = load_contract()
    archive = resolve_archive(contract, arguments.archive)
    problems = check(archive, contract)
    if problems:
        print(f"sdist contract violations in {archive.name}:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    required = len(required_entries(contract))
    forbidden = len(forbidden_entries(contract))
    print(
        f"{archive.name}: {required} required entries present, "
        f"{forbidden} checkout-only modules excluded"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

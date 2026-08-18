"""Check that repository GitHub Actions use immutable commit pins.

The checker intentionally reads workflow text without executing or resolving
any workflow expressions.  Local actions and Docker actions are valid without a
commit SHA; every third-party action must carry a full 40-character SHA and an
inline release-tag comment for maintainability.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_USES = re.compile(
    r"^\s*(?:-\s*)?uses:\s*(?P<reference>[^\s#]+)(?:\s+#\s*(?P<tag>[^\n]*))?\s*$"
)
_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_WORKFLOW_SUFFIXES = {".yaml", ".yml"}


def iter_workflow_files(root: Path) -> tuple[Path, ...]:
    """Return workflow and documented example files in stable order."""

    candidates: list[Path] = []
    for directory in (
        root / ".github" / "workflows",
        root / "examples" / "github-actions",
    ):
        if not directory.is_dir():
            continue
        candidates.extend(
            path
            for path in directory.rglob("*")
            if path.is_file() and path.suffix.lower() in _WORKFLOW_SUFFIXES
        )
    return tuple(sorted(set(candidates)))


def check_workflow_text(text: str, *, source: str = "workflow") -> list[str]:
    """Return policy violations found in one workflow document."""

    violations: list[str] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        match = _USES.match(line)
        if match is None:
            continue
        reference = match.group("reference")
        if reference.startswith("./") or reference.startswith("docker://"):
            continue
        if "@" not in reference:
            violations.append(
                f"{source}:{line_number}: Action reference has no pin: {reference}"
            )
            continue
        action, pin = reference.rsplit("@", 1)
        if not action or not _SHA.fullmatch(pin):
            violations.append(
                f"{source}:{line_number}: Action pin must be a 40-character commit SHA: {reference}"
            )
            continue
        tag = (match.group("tag") or "").strip()
        if not tag:
            violations.append(
                f"{source}:{line_number}: pinned Action must document its release tag inline"
            )
    return violations


def check_action_pins(root: Path) -> list[str]:
    """Check every repository workflow and manual example."""

    violations: list[str] = []
    for path in iter_workflow_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            violations.append(f"{path}: unable to read workflow: {exc}")
            continue
        violations.extend(
            check_workflow_text(text, source=path.relative_to(root).as_posix())
        )
    return violations


def main(argv: list[str] | None = None) -> int:
    root = Path(argv[0]).resolve() if argv else Path(__file__).resolve().parents[1]
    violations = check_action_pins(root)
    if violations:
        print("\n".join(violations), file=sys.stderr)
        return 1
    print(f"Action pin check passed ({len(iter_workflow_files(root))} workflow files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

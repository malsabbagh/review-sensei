"""Check that repository GitHub Actions use immutable commit pins.

The checker intentionally reads workflow text without executing or resolving
any workflow expressions. Local actions and Docker actions are valid without a
commit SHA; third-party actions must carry a full 40-character SHA and an
inline release-tag comment, except for the public ReviewSensei reusable
workflow whose protected `@v4` tag is the setup-v4 update channel.
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
_PUBLIC_REUSABLE_V4 = (
    "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@v4"
)


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
        # The ReviewSensei reusable workflow is intentionally the sole
        # tag-following dependency: v4 is the operator-managed release channel.
        # The broker resolves the tag and verifies the executing SHA at runtime.
        if reference == _PUBLIC_REUSABLE_V4:
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

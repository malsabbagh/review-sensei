"""Check that repository GitHub Actions use immutable commit pins.

The checker intentionally reads workflow text without executing or resolving
any workflow expressions. Local actions and Docker actions are valid without a
commit SHA; third-party actions must carry a full 40-character SHA and an
inline release-tag comment, except for the public ReviewSensei reusable
workflow whose protected `@v5` tag is the setup-v4 update channel. A single
workflow must also pin each Action repository at exactly one commit.
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
_PUBLIC_REUSABLE_TAGS = frozenset(
    {
        "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@v5",
    }
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


def _action_repository(action: str) -> str:
    """Return the ``owner/repo`` of an Action reference, ignoring any subpath."""

    return "/".join(action.split("/")[:2])


def _version_parity_violations(
    pins_by_repository: dict[str, list[tuple[str, str, int]]], *, source: str
) -> list[str]:
    """Flag one Action repository pinned at several versions in one workflow.

    CodeQL's ``init`` and ``analyze`` sub-actions reject a configuration written
    by another release, so a per-workflow version skew fails the job that pins
    it. Bump tooling that edits one sub-action at a time trips this.
    """

    violations: list[str] = []
    for repository, entries in sorted(pins_by_repository.items()):
        if len({pin for pin, _, _ in entries}) > 1:
            rendered = ", ".join(
                f"{pin} (#{tag}) at line {line_number}"
                for pin, tag, line_number in entries
            )
            violations.append(
                f"{source}: Action {repository} must use one commit pin per "
                f"workflow: {rendered}"
            )
            continue
        tags = sorted({tag for _, tag, _ in entries})
        if len(tags) > 1:
            violations.append(
                f"{source}: Action {repository} pins one commit but documents "
                f"several release tags: {', '.join(tags)}"
            )
    return violations


def check_workflow_text(text: str, *, source: str = "workflow") -> list[str]:
    """Return policy violations found in one workflow document."""

    violations: list[str] = []
    pins_by_repository: dict[str, list[tuple[str, str, int]]] = {}
    for line_number, line in enumerate(text.splitlines(), start=1):
        match = _USES.match(line)
        if match is None:
            continue
        reference = match.group("reference")
        if reference.startswith("./") or reference.startswith("docker://"):
            continue
        # The ReviewSensei reusable workflow is intentionally the sole
        # tag-following dependency: v5 is the operator-managed release channel.
        # The broker resolves the tag and verifies the executing SHA at runtime.
        if reference in _PUBLIC_REUSABLE_TAGS:
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
            continue
        pins_by_repository.setdefault(_action_repository(action), []).append(
            (pin, tag, line_number)
        )
    violations.extend(_version_parity_violations(pins_by_repository, source=source))
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

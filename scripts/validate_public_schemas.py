from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# The script is intentionally executable from a source checkout; imports are
# resolved after adding the repository's src directory above.
# ruff: noqa: E402
from review_sensei.errors import ReviewInputError
from review_sensei.schemas import validate_public_document


def _json_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        raise FileNotFoundError(
            f"missing schema fixture directory: {directory.relative_to(ROOT)}"
        )
    return sorted(Path(directory).glob("*.json"))


def _validate_directory(directory: Path, schema_name: str) -> None:
    for path in _json_files(directory):
        instance = json.loads(path.read_text(encoding="utf-8"))
        validate_public_document(instance, schema_name)
        print(f"OK {path.relative_to(ROOT)} against {schema_name}")


def _validate_golden(directory: Path) -> None:
    for path in _json_files(directory):
        schema_name = path.name[: -len(".json")]
        instance = json.loads(path.read_text(encoding="utf-8"))
        validate_public_document(instance, schema_name)
        print(f"OK {path.relative_to(ROOT)} against {schema_name}")


def _validate_negative(directory: Path) -> None:
    for path in _json_files(directory):
        schema_name = path.name[: -len(".json")]
        instance = json.loads(path.read_text(encoding="utf-8"))
        try:
            validate_public_document(instance, schema_name)
        except ReviewInputError:
            print(f"OK {path.relative_to(ROOT)} rejected by {schema_name}")
        else:
            raise ReviewInputError(
                f"{path.relative_to(ROOT)} should be rejected by {schema_name}"
            )


def main() -> int:
    checks = (
        (ROOT / "src/review_sensei/default_categories", "review-category"),
        (ROOT / "src/review_sensei/default_stages", "stage"),
        (ROOT / "examples/categories", "review-category"),
        (ROOT / "examples/stages", "stage"),
    )
    try:
        for directory, schema_name in checks:
            _validate_directory(directory, schema_name)
        _validate_golden(ROOT / "tests/fixtures/schemas/golden")
        _validate_negative(ROOT / "tests/fixtures/schemas/negative")
    except Exception as exc:
        print(f"schema validation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

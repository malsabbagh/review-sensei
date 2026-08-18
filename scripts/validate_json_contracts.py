"""Validate packaged JSON schemas and maintained ReviewSensei documents."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, SchemaError, ValidationError

SCHEMA_NAMES = (
    "review-category.schema.json",
    "review-stage.schema.json",
    "learning-entry.schema.json",
)


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _schema_path(root: Path, name: str) -> Path:
    if name not in SCHEMA_NAMES:
        raise ValueError(f"unknown schema: {name}")
    return root / "src" / "review_sensei" / "schemas" / name


def load_schemas(root: Path) -> dict[str, dict[str, Any]]:
    schemas: dict[str, dict[str, Any]] = {}
    for name in SCHEMA_NAMES:
        schema = _load(_schema_path(root, name))
        if not isinstance(schema, dict):
            raise ValueError(f"schema is not a JSON object: {name}")
        Draft202012Validator.check_schema(schema)
        schemas[name] = schema
    return schemas


def validate_document(document: Any, schema: dict[str, Any]) -> None:
    Draft202012Validator(schema).validate(document)


def _validate_directory(directory: Path, validator: Draft202012Validator) -> list[Any]:
    values: list[Any] = []
    if not directory.is_dir():
        return values
    for path in sorted(directory.glob("*.json")):
        value = _load(path)
        validator.validate(value)
        values.append(value)
    return values


def validate_repository(root: Path) -> None:
    schemas = load_schemas(root)
    category_validator = Draft202012Validator(schemas["review-category.schema.json"])
    stage_validator = Draft202012Validator(schemas["review-stage.schema.json"])
    learning_validator = Draft202012Validator(schemas["learning-entry.schema.json"])

    for schema in schemas.values():
        for example in schema.get("examples", []):
            validate_document(example, schema)

    category_directories = (
        root / "src" / "review_sensei" / "default_categories",
        root / "examples" / "categories",
    )
    stage_directories = (
        root / "src" / "review_sensei" / "default_stages",
        root / "examples" / "stages",
    )
    category_values: list[Any] = []
    for directory in category_directories:
        category_values.extend(_validate_directory(directory, category_validator))
    for directory in stage_directories:
        _validate_directory(directory, stage_validator)

    learning_examples = schemas["learning-entry.schema.json"].get("examples", [])
    for example in learning_examples:
        learning_validator.validate(example)
    learning_directory = root / ".github" / "review-sensei" / "learnings"
    _validate_directory(learning_directory, learning_validator)

    # Structural validation is paired with the runtime parsers, which remain
    # the semantic authority for accepted configuration.
    from review_sensei.learnings import LearningEntry
    from review_sensei.stages import (
        load_review_categories_from_dir,
        load_stages_from_dir,
    )

    for value in category_values:
        from review_sensei.stages import ReviewCategory

        ReviewCategory.from_dict(value)
    # Each maintained tree has its own category catalog.  Validate the pairs
    # explicitly so adding an optional example tree cannot silently associate it
    # with a different catalog merely because directory-list indexes align.
    stage_catalog_pairs = (
        (
            root / "src" / "review_sensei" / "default_stages",
            root / "src" / "review_sensei" / "default_categories",
        ),
        (root / "examples" / "stages", root / "examples" / "categories"),
    )
    for stage_directory, category_directory in stage_catalog_pairs:
        if stage_directory.is_dir():
            catalog = (
                load_review_categories_from_dir(category_directory)
                if category_directory.is_dir()
                else None
            )
            load_stages_from_dir(stage_directory, category_catalog=catalog)
    for example in learning_examples:
        LearningEntry.from_dict(example)
    for path in (
        sorted(learning_directory.glob("*.json")) if learning_directory.is_dir() else ()
    ):
        LearningEntry.from_dict(_load(path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", choices=SCHEMA_NAMES)
    parser.add_argument("--document", type=Path)
    parser.add_argument("--root", type=Path)
    args = parser.parse_args(argv)
    root = (args.root or Path(__file__).resolve().parents[1]).resolve()
    try:
        if args.document:
            if not args.schema:
                parser.error("--schema is required with --document")
            schema = load_schemas(root)[args.schema]
            validate_document(_load(args.document), schema)
            print(f"JSON document is valid under {args.schema}")
        else:
            validate_repository(root)
            print("JSON contract validation passed")
    except (
        OSError,
        UnicodeError,
        ValueError,
        KeyError,
        SchemaError,
        ValidationError,
    ) as exc:
        print(f"JSON contract validation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

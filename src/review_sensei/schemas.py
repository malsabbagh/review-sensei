from __future__ import annotations

import json
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from .errors import ReviewInputError

SCHEMA_DIR = Path(__file__).parent / "schemas"


def load_schema(name: str) -> dict[str, object]:
    """Return parsed JSON for one packaged public schema."""

    path = SCHEMA_DIR / f"{name}.schema.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReviewInputError(f"schema '{name}' could not be loaded") from exc
    if not isinstance(value, dict):
        raise ReviewInputError(f"schema '{name}' must be a JSON object")
    return value


def _build_schema_registry() -> Registry:
    registry: Registry = Registry()
    for path in sorted(SCHEMA_DIR.glob("*.schema.json")):
        contents = json.loads(path.read_text(encoding="utf-8"))
        resource = Resource.from_contents(contents)
        schema_id = resource.id()
        if not isinstance(schema_id, str):
            raise ReviewInputError(f"schema '{path.name}' is missing an $id")
        registry = registry.with_resource(schema_id, resource)
    return registry


_SCHEMA_REGISTRY = _build_schema_registry()


def _schema_registry() -> Registry:
    return _SCHEMA_REGISTRY


@lru_cache(maxsize=None)
def _validator(schema_name: str) -> Draft202012Validator:
    schema = load_schema(schema_name)
    try:
        return Draft202012Validator(schema, registry=_schema_registry())
    except Exception as exc:
        raise ReviewInputError(
            f"schema '{schema_name}' is not a valid JSON Schema"
        ) from exc


def validate_public_document(instance: object, schema_name: str) -> None:
    """Validate one public document, raising ``ReviewInputError`` on failure."""

    errors = sorted(
        _validator(schema_name).iter_errors(instance),
        key=lambda error: list(error.path),
    )
    if errors:
        raise ReviewInputError(
            f"{schema_name} document failed schema validation ({len(errors)} error(s))"
        )


def validate_public_documents(instances: Iterable[object], schema_name: str) -> None:
    """Validate an iterable of public documents against one schema."""

    for index, instance in enumerate(instances):
        try:
            validate_public_document(instance, schema_name)
        except ReviewInputError as exc:
            raise ReviewInputError(
                f"{schema_name} document {index} failed schema validation"
            ) from exc

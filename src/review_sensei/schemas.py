from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable
from copy import deepcopy
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.validators import extend
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from .errors import ReviewInputError

SCHEMA_DIR = Path(__file__).parent / "schemas"


def _accounting_pair_prefix(validator, prefixes, instance, schema):
    """Specialize one unchanged schema program, never an instance's validity.

    Other keywords (including length and additional items) still execute in
    the original validator. Nonplain values, errors and changed schemas retain
    the original keyword handler and its complete error paths/counts.
    """
    pattern = "^[A-Za-z0-9_.:-]{1,128}$"
    if (
        validator.TYPE_CHECKER is Draft202012Validator.TYPE_CHECKER
        and all(
            validator.VALIDATORS.get(name) is Draft202012Validator.VALIDATORS[name]
            for name in ("type", "pattern", "minimum", "maximum")
        )
        and prefixes
        == [
            {"type": "string", "pattern": pattern},
            {"type": "integer", "minimum": 1, "maximum": 9007199254740991},
        ]
        and type(instance) is list
        and len(instance) >= 2
        and type(instance[0]) is str
        and type(instance[1]) is int
        and 1 <= instance[1] <= 9007199254740991
        and re.search(pattern, instance[0]) is not None
    ):
        return
    yield from Draft202012Validator.VALIDATORS["prefixItems"](
        validator, prefixes, instance, schema
    )


def _accounting_pair_items(validator, items, instance, schema):
    """Validate the unchanged completed vector without per-pair validators.

    This only completes the items keyword for wholly valid plain pairs. Every
    sibling keyword still runs; any other schema, value or keyword program
    retains the original traversal, including its exact error locations.
    """
    pattern = "^[A-Za-z0-9_.:-]{1,128}$"
    if (
        type(instance) is list
        and "prefixItems" not in schema
        and type(items) is dict
        and set(items) - {"description"}
        == {"type", "prefixItems", "items", "minItems", "maxItems"}
        and items["type"] == "array"
        and items["items"] is False
        and items["minItems"] == items["maxItems"] == 2
        and items["prefixItems"]
        == [
            {"type": "string", "pattern": pattern},
            {"type": "integer", "minimum": 1, "maximum": 9007199254740991},
        ]
        and validator.TYPE_CHECKER is Draft202012Validator.TYPE_CHECKER
        and validator.VALIDATORS.get("prefixItems") is _accounting_pair_prefix
        and "description" not in validator.VALIDATORS
        and all(
            validator.VALIDATORS.get(name) is Draft202012Validator.VALIDATORS[name]
            for name in (
                "type",
                "minItems",
                "maxItems",
                "pattern",
                "minimum",
                "maximum",
            )
        )
        and all(
            type(pair) is list
            and len(pair) == 2
            and type(pair[0]) is str
            and type(pair[1]) is int
            and 1 <= pair[1] <= 9007199254740991
            and re.search(pattern, pair[0]) is not None
            for pair in instance
        )
    ):
        return
    yield from Draft202012Validator.VALIDATORS["items"](
        validator, items, instance, schema
    )


_AccountingValidator = extend(
    Draft202012Validator,
    {"prefixItems": _accounting_pair_prefix, "items": _accounting_pair_items},
)


def _inline_accounting_leaves(schema: dict) -> dict:
    """Expand pure local leaf references in the cached accounting validator.

    This derives validation code from the unchanged packaged schema, never
    caches an instance or a successful validation. References with siblings,
    nested scopes, external targets or references inside the target keep the
    original resolver. Only schema-valued keywords are traversed: enum/const
    data, examples and defaults are not schema programs.
    """
    definitions = schema.get("$defs", {})
    if type(definitions) is not dict:
        return schema

    def scoped(value):
        if type(value) is dict:
            return any(
                key
                in {
                    "$ref",
                    "$dynamicRef",
                    "$recursiveRef",
                    "$schema",
                    "$id",
                    "$anchor",
                    "$dynamicAnchor",
                    "$recursiveAnchor",
                }
                or scoped(item)
                for key, item in value.items()
            )
        return type(value) is list and any(scoped(item) for item in value)

    leaves = {
        "#/$defs/" + name: value
        for name in ("owner", "request", "request-pair")
        if type(value := definitions.get(name)) is dict and not scoped(value)
    }

    def expand(value, *, root=False):
        if type(value) is not dict:
            return value
        if not root and any(
            key in value
            for key in (
                "$schema",
                "$id",
                "$anchor",
                "$dynamicAnchor",
                "$recursiveAnchor",
            )
        ):
            return value
        if set(value) == {"$ref"} and type(value["$ref"]) is str:
            target = leaves.get(value["$ref"])
            if target is not None:
                return deepcopy(target)
        result = dict(value)
        for key in ("$defs", "properties", "patternProperties", "dependentSchemas"):
            children = value.get(key)
            if type(children) is dict:
                result[key] = {name: expand(child) for name, child in children.items()}
        for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
            children = value.get(key)
            if type(children) is list:
                result[key] = [expand(child) for child in children]
        for key in (
            "items",
            "additionalProperties",
            "contains",
            "not",
            "if",
            "then",
            "else",
            "propertyNames",
            "unevaluatedProperties",
            "unevaluatedItems",
        ):
            if key in value:
                result[key] = expand(value[key])
        return result

    return expand(schema, root=True)


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
        if path.name == "api-run-accounting.schema.json":
            contents = _inline_accounting_leaves(contents)
        resource = Resource.from_contents(contents)
        schema_id = resource.id()
        if not isinstance(schema_id, str):
            raise ReviewInputError(f"schema '{path.name}' is missing an $id")
        registry = registry.with_resource(schema_id, resource)
    return registry


_SCHEMA_REGISTRY = _build_schema_registry()


def _schema_registry() -> Registry:
    return _SCHEMA_REGISTRY


def _accounting_reference_registry(registry: Registry) -> Registry:
    """Compile the known referenced program under its identical fixed dialect.

    An explicit Resource specification retains Draft202012 reference semantics.
    Omitting only the redundant dialect selector from this private program lets
    descendant validators retain the equivalent accounting specialization.
    Public schema bytes, IDs, reference scopes and the shared registry stay
    unchanged. Unknown dialects keep the original resource and validator.
    """
    uri = "https://reviewsensei.dev/schemas/v1/api-run-accounting.schema.json"
    try:
        source = registry.contents(uri)
    except KeyError:
        return registry
    if (
        type(source) is not dict
        or source.get("$schema") != "https://json-schema.org/draft/2020-12/schema"
        or source.get("$id") != uri
    ):
        return registry
    program = _inline_accounting_leaves(deepcopy(source))
    program.pop("$schema")
    return registry.with_resource(
        uri, Resource.from_contents(program, default_specification=DRAFT202012)
    )


@lru_cache(maxsize=None)
def _validator(schema_name: str) -> Draft202012Validator:
    schema = load_schema(schema_name)
    if schema_name == "api-run-accounting":
        schema = _inline_accounting_leaves(schema)
    try:
        validator = (
            _AccountingValidator
            if schema_name in {"api-run-accounting", "session-record"}
            else Draft202012Validator
        )
        registry = _schema_registry()
        if schema_name == "session-record":
            registry = _accounting_reference_registry(registry)
        return validator(schema, registry=registry)
    except Exception as exc:
        raise ReviewInputError(
            f"schema '{schema_name}' is not a valid JSON Schema"
        ) from exc


_VALID_DOCUMENT_LIMIT = 4096
_VALID_DOCUMENT_DEPTH = 64
_valid_documents: dict[tuple[str, bytes], object] = {}


def _exact_json(value: object, depth: int = 0) -> bool:
    if depth > _VALID_DOCUMENT_DEPTH:
        return False
    kind = type(value)
    if value is None or kind is str or kind is bool or kind is int:
        return True
    if kind is float:
        return math.isfinite(value)  # type: ignore[arg-type]
    if kind is list:
        return all(_exact_json(item, depth + 1) for item in value)  # type: ignore[attr-defined]
    if kind is dict:
        return all(
            type(key) is str and _exact_json(item, depth + 1)
            for key, item in value.items()  # type: ignore[attr-defined]
        )
    return False


def _document_digest(instance: object) -> bytes | None:
    # Subclasses can present a validator different content than the encoder
    # serializes, so only exact JSON types share an outcome by content.
    if not _exact_json(instance):
        return None
    try:
        text = json.dumps(
            instance, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (ValueError, RecursionError):
        return None
    return hashlib.sha256(text.encode("utf-8")).digest()


def validate_public_document(instance: object, schema_name: str) -> None:
    """Validate one public document, raising ``ReviewInputError`` on failure.

    A document whose exact JSON content already validated against the same
    validator is accepted without a second traversal. Invalid documents are
    never remembered, so every failure reports its complete error count.
    """

    validator = _validator(schema_name)
    digest = _document_digest(instance)
    if digest is not None and _valid_documents.get((schema_name, digest)) is validator:
        return
    errors = sorted(
        validator.iter_errors(instance),
        key=lambda error: list(error.path),
    )
    if errors:
        raise ReviewInputError(
            f"{schema_name} document failed schema validation ({len(errors)} error(s))"
        )
    if digest is not None:
        if len(_valid_documents) >= _VALID_DOCUMENT_LIMIT:
            _valid_documents.clear()
        _valid_documents[schema_name, digest] = validator


def validate_public_documents(instances: Iterable[object], schema_name: str) -> None:
    """Validate an iterable of public documents against one schema."""

    for index, instance in enumerate(instances):
        try:
            validate_public_document(instance, schema_name)
        except ReviewInputError as exc:
            raise ReviewInputError(
                f"{schema_name} document {index} failed schema validation"
            ) from exc


from review_sensei.source_registry import seal_module as _seal_original_source  # noqa: E402

_seal_original_source()

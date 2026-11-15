"""Static leaf expansion keeps the original schema oracle and public errors."""

import json
import unittest
from copy import deepcopy
from dataclasses import replace

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from review_sensei.api_work import PendingApiRequest
from review_sensei.schemas import (
    SCHEMA_DIR,
    _inline_accounting_leaves,
    _validator,
    load_schema,
)
from review_sensei.session import SessionIdentity, SessionRecord
from tests.test_api_work import accounting


def original_registry():
    registry = Registry()
    for path in SCHEMA_DIR.glob("*.schema.json"):
        resource = Resource.from_contents(json.loads(path.read_text()))
        registry = registry.with_resource(resource.id(), resource)
    return registry


def errors(validator, document):
    # These are the observable count and instance locations; internal schema
    # paths naturally name the expanded location instead of the old $defs.
    return sorted(
        (repr(list(error.path)), error.validator, error.message)
        for error in validator.iter_errors(document)
    )


class AccountingLeafSchemaTests(unittest.TestCase):
    def test_all_versions_and_mutated_values_match_unmodified_schema(self):
        registry = original_registry()
        oracle = Draft202012Validator(
            load_schema("api-run-accounting"), registry=registry
        )
        optimized = _validator("api-run-accounting")
        state = accounting().reserve("review", 6)
        state = state.dispatch("review", PendingApiRequest("unknown", 99))
        done = PendingApiRequest("confirmed", 100)
        state = state.dispatch("review", done).confirm(done)
        mutations = (None, True, False, -1, 0, 1.5, "", "bad value", [], {})
        count = 0
        for version in (
            "caller-requests-v1",
            "caller-requests-v2",
            "caller-requests-v3",
        ):
            document = replace(state, version=version).to_document()
            self.assertEqual(errors(oracle, document), [])
            self.assertEqual(errors(optimized, document), [])

            def paths(value, prefix=()):
                yield prefix
                if type(value) is dict:
                    for key, child in value.items():
                        yield from paths(child, (*prefix, key))
                elif type(value) is list:
                    for index, child in enumerate(value):
                        yield from paths(child, (*prefix, index))

            for path in paths(document):
                for mutation in mutations:
                    candidate = deepcopy(document)
                    if not path:
                        candidate = mutation
                    else:
                        parent = candidate
                        for part in path[:-1]:
                            parent = parent[part]
                        parent[path[-1]] = mutation
                    with self.subTest(version=version, path=path, mutation=mutation):
                        self.assertEqual(
                            errors(optimized, candidate), errors(oracle, candidate)
                        )
                    count += 1
            record = SessionRecord.create(
                SessionIdentity("owner/repo", 7, 123), api_run_accounting=document
            ).to_dict()
            session_oracle = Draft202012Validator(
                load_schema("session-record"), registry=registry
            )
            self.assertEqual(
                errors(_validator("session-record"), record),
                errors(session_oracle, record),
            )
            record["api_run_accounting"]["blocks"][0]["pending"][0][
                "deadline_unix_ms"
            ] = False
            self.assertEqual(
                errors(_validator("session-record"), record),
                errors(session_oracle, record),
            )
        self.assertGreater(count, 1500)

    def test_scopes_reference_siblings_and_literal_data_keep_original_meaning(self):
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://example.invalid/outer",
            "$defs": {"request": {"type": "string", "minLength": 2}},
            "type": "object",
            "properties": {
                "plain": {"$ref": "#/$defs/request"},
                "sibling": {"$ref": "#/$defs/request", "maxLength": 3},
                "literal": {"const": {"$ref": "#/$defs/request"}},
                "nested": {
                    "$id": "nested",
                    "$defs": {"request": {"const": "inner"}},
                    "$ref": "#/$defs/request",
                },
            },
        }
        saved = deepcopy(schema)
        expanded = _inline_accounting_leaves(schema)
        self.assertEqual(schema, saved)
        original = Draft202012Validator(schema)
        candidate = Draft202012Validator(expanded)
        for value in (
            None,
            1,
            "a",
            "abc",
            "abcd",
            "inner",
            {"$ref": "#/$defs/request"},
        ):
            for key in schema["properties"]:
                with self.subTest(key=key, value=value):
                    self.assertEqual(
                        errors(original, {key: value}), errors(candidate, {key: value})
                    )
        # A definition that itself resolves a reference must stay under the
        # original resolver; its base and recursive behavior are not flattened.
        for keyword in (
            "$schema",
            "$id",
            "$anchor",
            "$dynamicAnchor",
            "$recursiveAnchor",
            "$ref",
            "$dynamicRef",
            "$recursiveRef",
        ):
            changed = deepcopy(schema)
            changed["$defs"]["request"][keyword] = "scope"
            self.assertEqual(_inline_accounting_leaves(changed), changed)

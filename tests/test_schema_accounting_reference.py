"""Compiled accounting references preserve dialect, scope and public errors."""

import unittest
from copy import deepcopy
from dataclasses import replace

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from test_api_work import accounting
from test_schema_accounting_leaves import errors, original_registry

from review_sensei.api_work import PendingApiRequest
from review_sensei.schemas import (
    _accounting_reference_registry,
    _AccountingValidator,
    _validator,
    load_schema,
)
from review_sensei.session import SessionIdentity, SessionRecord


class AccountingReferenceSchemaTests(unittest.TestCase):
    def test_nested_accounting_mutations_match_the_unmodified_schema(self):
        oracle = Draft202012Validator(
            load_schema("session-record"), registry=original_registry()
        )
        candidate = _validator("session-record")
        state = accounting().reserve("review", 6)
        state = state.dispatch("review", PendingApiRequest("unknown", 99))
        done = PendingApiRequest("confirmed", 100)
        state = state.dispatch("review", done).confirm(done)
        count = 0
        for version in (
            "caller-requests-v1",
            "caller-requests-v2",
            "caller-requests-v3",
        ):
            document = SessionRecord.create(
                SessionIdentity("owner/repo", 7, 123),
                api_run_accounting=replace(state, version=version).to_document(),
            ).to_dict()

            def paths(value, prefix=()):
                yield prefix
                if type(value) is dict:
                    for key, child in value.items():
                        yield from paths(child, (*prefix, key))
                elif type(value) is list:
                    for index, child in enumerate(value):
                        yield from paths(child, (*prefix, index))

            for path in paths(document["api_run_accounting"], ("api_run_accounting",)):
                for mutation in (
                    None,
                    True,
                    False,
                    -1,
                    0,
                    1.0,
                    1.5,
                    "",
                    "bad value",
                    [],
                    {},
                ):
                    changed = deepcopy(document)
                    parent = changed
                    for key in path[:-1]:
                        parent = parent[key]
                    parent[path[-1]] = mutation
                    with self.subTest(version=version, path=path, mutation=mutation):
                        self.assertEqual(
                            errors(candidate, changed), errors(oracle, changed)
                        )
                    count += 1
        self.assertGreater(count, 1500)

    def test_original_resources_and_foreign_dialects_are_preserved(self):
        source = load_schema("api-run-accounting")
        uri = source["$id"]
        for nested_dialect in (False, True):
            changed = deepcopy(source)
            if nested_dialect:
                changed["$defs"]["request-pair"]["$schema"] = (
                    "http://json-schema.org/draft-07/schema#"
                )
            saved = deepcopy(changed)
            registry = Registry().with_resource(uri, Resource.from_contents(changed))
            compiled = _accounting_reference_registry(registry)
            self.assertEqual(registry.contents(uri), saved)
            self.assertEqual(compiled.contents(uri)["$id"], uri)
            self.assertNotIn("$schema", compiled.contents(uri))
            oracle = Draft202012Validator({"$ref": uri}, registry=registry)
            candidate = _AccountingValidator({"$ref": uri}, registry=compiled)
            for pair in (["r", 1], ["r", False], [1, "bad"], ["r", 1, 2]):
                document = accounting().to_document()
                document["completed"] = [pair]

                def normalized_errors(validator):
                    return sorted(
                        (repr(list(error.path)), str(error.validator), error.message)
                        for error in validator.iter_errors(document)
                    )

                self.assertEqual(
                    normalized_errors(candidate), normalized_errors(oracle)
                )
        foreign = {**source, "$schema": "http://json-schema.org/draft-07/schema#"}
        registry = Registry().with_resource(uri, Resource.from_contents(foreign))
        self.assertIs(_accounting_reference_registry(registry), registry)
        missing = Registry()
        self.assertIs(_accounting_reference_registry(missing), missing)

"""Request-pair specialization retains the original schema and error oracle."""

import itertools
import unittest
from copy import deepcopy

from jsonschema import Draft202012Validator
from jsonschema.validators import extend
from test_schema_accounting_leaves import errors

from review_sensei.schemas import _AccountingValidator, load_schema


class AccountingPairSchemaTests(unittest.TestCase):
    def test_custom_keyword_and_type_checker_keep_their_original_behavior(self):
        schema = load_schema("api-run-accounting")["$defs"]["request-pair"]
        checker = Draft202012Validator.TYPE_CHECKER.redefine(
            "integer", lambda checker, value: False
        )
        for options in (
            {"type_checker": checker},
            {"validators": {"minimum": lambda *args: iter(())}},
        ):
            original = extend(Draft202012Validator, **options)(schema)
            optimized = extend(_AccountingValidator, **options)(schema)
            for candidate in (["r", 1], ["r", 0], ["r", True]):
                self.assertEqual(
                    errors(optimized, candidate), errors(original, candidate)
                )

    def test_pair_boundaries_shapes_and_nonplain_values_match_original_errors(self):
        class Text(str):
            pass

        class Number(int):
            pass

        class Array(list):
            pass

        schema = load_schema("api-run-accounting")["$defs"]["request-pair"]
        original = Draft202012Validator(schema)
        optimized = _AccountingValidator(schema)
        names = (
            "r",
            "r\n",
            "a" * 128,
            "a" * 129,
            "",
            "bad value",
            1,
            True,
            None,
            Text("r"),
        )
        deadlines = (
            1,
            9007199254740991,
            0,
            -1,
            9007199254740992,
            True,
            1.0,
            1.5,
            "1",
            None,
            Number(1),
        )
        for name, deadline in itertools.product(names, deadlines):
            pair = [name, deadline]
            for candidate in (
                pair,
                pair + ["extra"],
                pair[:1],
                Array(pair),
                tuple(pair),
            ):
                with self.subTest(candidate=candidate):
                    self.assertEqual(
                        errors(optimized, candidate), errors(original, candidate)
                    )
        for candidate in (None, {}, 1, "r", [], ["r"]):
            self.assertEqual(errors(optimized, candidate), errors(original, candidate))

    def test_changed_schema_and_sibling_keywords_still_use_original_semantics(self):
        source = load_schema("api-run-accounting")["$defs"]["request-pair"]
        schemas = [source, {**source, "maxItems": 1}, {**source, "const": ["other", 2]}]
        for field, value in (("minimum", 2), ("maximum", 1), ("const", 7)):
            changed = deepcopy(source)
            changed["prefixItems"][1][field] = value
            schemas.append(changed)
        changed = deepcopy(source)
        changed["prefixItems"][0]["pattern"] = "^other$"
        schemas.append(changed)
        for schema, candidate in itertools.product(
            schemas, (["r", 1], ["other", 2], ["r", 7], ["r", True])
        ):
            with self.subTest(schema=schema, candidate=candidate):
                self.assertEqual(
                    errors(_AccountingValidator(schema), candidate),
                    errors(Draft202012Validator(schema), candidate),
                )

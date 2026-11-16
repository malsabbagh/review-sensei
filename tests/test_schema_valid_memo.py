"""Remembered valid content never changes a public validation outcome."""

import unittest
from copy import deepcopy
from dataclasses import replace
from unittest.mock import patch

from review_sensei import schemas
from review_sensei.errors import ReviewInputError
from review_sensei.schemas import _document_digest, _validator, validate_public_document
from tests.test_api_work import accounting

SCHEMA = "api-run-accounting"


class ValidDocumentMemoTests(unittest.TestCase):
    def setUp(self):
        schemas._valid_documents.clear()
        self.addCleanup(schemas._valid_documents.clear)
        self.validator = _validator(SCHEMA)
        self.traversals = 0
        original = type(self.validator).iter_errors
        test = self

        def counting(validator, *args, **kwargs):
            if validator is test.validator:
                test.traversals += 1
            return original(validator, *args, **kwargs)

        patcher = patch.object(type(self.validator), "iter_errors", counting)
        patcher.start()
        self.addCleanup(patcher.stop)

    def refusal(self, document):
        with self.assertRaises(ReviewInputError) as refused:
            validate_public_document(document, SCHEMA)
        return str(refused.exception)

    def test_identical_exact_content_is_traversed_once(self):
        document = accounting().to_document()
        validate_public_document(document, SCHEMA)
        validate_public_document(deepcopy(document), SCHEMA)
        validate_public_document(document, SCHEMA)
        self.assertEqual(self.traversals, 1)
        self.assertEqual(
            schemas._valid_documents,
            {(SCHEMA, _document_digest(document)): self.validator},
        )

    def test_a_replaced_validator_never_reuses_a_remembered_outcome(self):
        document = accounting().to_document()
        validate_public_document(document, SCHEMA)
        key = (SCHEMA, _document_digest(document))
        schemas._valid_documents[key] = object()
        validate_public_document(document, SCHEMA)
        self.assertEqual(self.traversals, 2)
        self.assertIs(schemas._valid_documents[key], self.validator)

    def test_invalid_documents_are_never_remembered(self):
        document = accounting().to_document()
        document["version"] = 7
        first = self.refusal(document)
        second = self.refusal(deepcopy(document))
        self.assertEqual(first, second)
        self.assertRegex(
            first,
            r"^api-run-accounting document failed schema validation \(\d+ error\(s\)\)$",
        )
        self.assertEqual(self.traversals, 2)
        self.assertEqual(schemas._valid_documents, {})

    def test_changed_content_after_acceptance_is_fully_validated(self):
        document = accounting().to_document()
        validate_public_document(document, SCHEMA)
        expected_traversals = 1
        for value in (None, True, 1, 1.0, "", [], {}, "bad value"):
            changed = deepcopy(document)
            changed["version"] = value
            schemas._valid_documents.clear()
            fresh = self.refusal(changed)
            validate_public_document(document, SCHEMA)
            self.assertEqual(self.refusal(changed), fresh)
            expected_traversals += 3
        self.assertEqual(self.traversals, expected_traversals)

    def test_equal_but_differently_typed_values_do_not_share_a_digest(self):
        digests = {
            _document_digest(value)
            for value in (1, 1.0, True, "1", [1], {"1": 1}, 0.0, -0.0, None)
        }
        self.assertEqual(len(digests), 9)

    def test_nonexact_documents_are_traversed_every_time(self):
        class Document(dict):
            pass

        class Text(str):
            pass

        document = accounting().to_document()
        for nonexact in (
            Document(document),
            {
                Text(key) if key == "version" else key: value
                for key, value in document.items()
            },
            {**document, "version": Text(document["version"])},
        ):
            self.assertIsNone(_document_digest(nonexact))
            before = self.traversals
            for _ in range(2):
                try:
                    validate_public_document(nonexact, SCHEMA)
                except ReviewInputError:
                    pass
            self.assertEqual(self.traversals, before + 2)
        self.assertEqual(schemas._valid_documents, {})

    def test_digest_never_invokes_subclass_hooks(self):
        class Hostile(dict):
            def items(self):
                raise AssertionError("items hook")

            def keys(self):
                raise AssertionError("keys hook")

        class HostileList(list):
            def __iter__(self):
                raise AssertionError("iter hook")

        self.assertIsNone(_document_digest(Hostile(a=1)))
        self.assertIsNone(_document_digest({"a": HostileList([1])}))
        self.assertIsNone(_document_digest((1, 2)))
        self.assertIsNone(_document_digest(b"bytes"))

    def test_nonfinite_deep_and_cyclic_content_is_never_remembered(self):
        cyclic: dict = {}
        cyclic["self"] = cyclic
        deep: list = []
        for _ in range(schemas._VALID_DOCUMENT_DEPTH + 1):
            deep = [deep]
        for value in (float("nan"), float("inf"), -float("inf"), cyclic, deep):
            self.assertIsNone(_document_digest(value))
        self.assertIsNotNone(_document_digest([[[1.5]]]))

    def test_memo_is_bounded(self):
        state = accounting()
        versions = ("caller-requests-v1", "caller-requests-v2", "caller-requests-v3")
        with patch.object(schemas, "_VALID_DOCUMENT_LIMIT", 2):
            for version in versions:
                validate_public_document(
                    replace(state, version=version).to_document(), SCHEMA
                )
                self.assertLessEqual(len(schemas._valid_documents), 2)
        self.assertEqual(self.traversals, 3)


if __name__ == "__main__":
    unittest.main()

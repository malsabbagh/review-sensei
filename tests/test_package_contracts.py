import json
import unittest
from importlib import resources
from pathlib import Path

from jsonschema import Draft202012Validator

SCHEMAS = (
    "review-category.schema.json",
    "review-stage.schema.json",
    "learning-entry.schema.json",
    "evaluation-corpus.schema.json",
    "evaluation-report.schema.json",
)


class PackageContractTests(unittest.TestCase):
    def test_all_public_schemas_are_packaged_and_self_validating(self):
        package_root = resources.files("review_sensei")
        for name in SCHEMAS:
            with self.subTest(name=name):
                schema = json.loads(
                    package_root.joinpath("schemas", name).read_text(encoding="utf-8")
                )
                Draft202012Validator.check_schema(schema)
                validator = Draft202012Validator(schema)
                self.assertTrue(schema.get("examples"))
                for example in schema["examples"]:
                    validator.validate(example)

    def test_empty_document_does_not_match_any_public_schema(self):
        package_root = resources.files("review_sensei")
        for name in SCHEMAS:
            with self.subTest(name=name):
                schema = json.loads(
                    package_root.joinpath("schemas", name).read_text(encoding="utf-8")
                )
                with self.assertRaises(Exception):
                    Draft202012Validator(schema).validate({})

    def test_manifest_includes_evaluation_corpus(self):
        root = Path(__file__).resolve().parents[1]
        manifest = (root / "MANIFEST.in").read_text(encoding="utf-8")
        self.assertIn("graft evaluation", manifest)
        self.assertTrue((root / "evaluation" / "v1" / "corpus.json").is_file())


if __name__ == "__main__":
    unittest.main()

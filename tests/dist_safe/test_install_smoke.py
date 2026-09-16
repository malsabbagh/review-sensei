from __future__ import annotations

import importlib.metadata
import importlib.resources
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))

from packaging_guard import assert_distribution_import  # noqa: E402

assert_distribution_import()

from review_sensei.cli import main  # noqa: E402
from review_sensei.models import ProviderRequest  # noqa: E402
from review_sensei.providers.fixture import FixtureProvider  # noqa: E402

# Release versions must stay PEP 440 parseable. CI pins the exact artifact
# through REVIEWSENSEI_EXPECTED_VERSION so a version bump does not edit tests.
PEP440 = re.compile(r"^\d+(\.\d+)*((a|b|rc)\d+)?(\.post\d+)?(\.dev\d+)?$")


class InstallSmokeTests(unittest.TestCase):
    def test_packaged_cli_and_schemas_are_available(self) -> None:
        package_file = assert_distribution_import()
        installed_version = importlib.metadata.version("review-sensei")
        self.assertRegex(installed_version, PEP440)
        expected = os.environ.get("REVIEWSENSEI_EXPECTED_VERSION")
        if expected:
            self.assertEqual(installed_version, expected)
        package_root = importlib.resources.files("review_sensei")
        for name in (
            "review-category.schema.json",
            "review-stage.schema.json",
            "learning-entry.schema.json",
            "evaluation-corpus.schema.json",
            "evaluation-report.schema.json",
        ):
            with self.subTest(name=name):
                self.assertTrue(package_root.joinpath("schemas", name).is_file())
        stdout = io.StringIO()
        with patch("sys.stdout", stdout), self.assertRaises(SystemExit) as caught:
            main(["--help"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("review-sensei", stdout.getvalue())
        if os.environ.get("REVIEWSENSEI_DIST_SAFE_LANE") == "1":
            self.assertIn(Path(sys.prefix).resolve(), package_file.parents)

    def test_fixture_provider_does_not_require_live_credentials(self) -> None:
        payload = json.dumps({"summary": "Fixture review.", "comments": []})
        with tempfile.TemporaryDirectory() as temp_dir:
            response_path = Path(temp_dir) / "response.json"
            response_path.write_text(payload, encoding="utf-8")
            with patch.dict(os.environ, {"OLLAMA_API_KEY": "must-not-be-read"}):
                provider = FixtureProvider(response_path, model="fixture-v1")
                response = provider.complete(ProviderRequest(prompt="prompt"))
        self.assertEqual(response.provider, "fixture")
        self.assertEqual(response.text, payload)


if __name__ == "__main__":
    unittest.main()

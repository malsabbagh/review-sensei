"""The one-time import of the retired flat setup configuration."""

import hashlib
import json
import re
import unittest
from pathlib import Path

from review_sensei.configuration import (
    MAX_CONFIG_BYTES,
    RETIRED_FIELDS,
    import_legacy_setup_configuration,
    parse_configuration_text,
)
from review_sensei.hosting.github.setup import (
    _setup_pull_request_body,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "legacy-setup-import.json"
CASES = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["cases"]


class LegacySetupImportFixtureTests(unittest.TestCase):
    """The shared fixture is read by the Cloudflare Worker suite as well."""

    def test_shared_fixture_cases_match_the_importer(self):
        self.assertTrue(CASES)
        for case in CASES:
            with self.subTest(name=case["name"]):
                result = import_legacy_setup_configuration(case["content"])
                self.assertEqual(list(result.carried), case["carried"])
                self.assertEqual(list(result.notes), case["notes"])
                self.assertEqual(result.content, case["rendered"])
                self.assertEqual(
                    _setup_pull_request_body(result.carried),
                    case["pull_request_body"],
                )

    def test_imported_documents_parse_as_canonical_configuration(self):
        for case in CASES:
            with self.subTest(name=case["name"]):
                parsed = parse_configuration_text(case["rendered"])
                self.assertEqual(parsed.schema, 1)
                for field_path in case["carried"]:
                    self.assertTrue(parsed.is_declared(field_path))

    def test_every_explicit_choice_survives_the_import(self):
        content = (
            "provider: ollama\n"
            "provider_mode: cloud-ollama\n"
            "model: ''\n"
            "cloud_model: 'qwen3.5:cloud'\n"
            "cloud_base_url: https://ollama.internal.example/api\n"
            "auto_review: true\n"
            "github_writes: true\n"
            "auto_approve: false\n"
            "mention_replies: false\n"
            "learning_prs: true\n"
            "upload_artifacts: true\n"
        )
        result = import_legacy_setup_configuration(content)
        parsed = parse_configuration_text(result.content)
        self.assertEqual(parsed.inference.backend, "cloud-ollama")
        self.assertEqual(parsed.inference.model, "qwen3.5:cloud")
        self.assertEqual(
            parsed.advanced.endpoint.base_url, "https://ollama.internal.example/api"
        )
        self.assertTrue(parsed.advanced.endpoint.allow_custom_endpoint)
        self.assertTrue(parsed.github.automatic_reviews)
        self.assertTrue(parsed.github.writes)
        self.assertEqual(parsed.github.reviews, "advisory")
        self.assertFalse(parsed.github.mentions)
        self.assertEqual(parsed.github.learning, "pull-requests")
        self.assertEqual(parsed.github.artifacts, "diagnostics")

    def test_explicitly_disabled_settings_stay_disabled(self):
        result = import_legacy_setup_configuration(
            "provider_mode: local\n"
            "auto_review: false\n"
            "github_writes: false\n"
            "mention_replies: false\n"
        )
        parsed = parse_configuration_text(result.content)
        self.assertFalse(parsed.github.automatic_reviews)
        self.assertFalse(parsed.github.writes)
        self.assertFalse(parsed.github.mentions)
        self.assertEqual(parsed.github.learning, "disabled")
        self.assertEqual(parsed.github.artifacts, "none")

    def test_untranslatable_values_are_reported_and_never_guessed(self):
        result = import_legacy_setup_configuration(
            "provider_mode: bespoke\n"
            "auto_review: yes\n"
            "learning_prs: sometimes\n"
            "advanced_setting: 1\n"
        )
        rendered = result.content
        self.assertNotIn("backend: bespoke", rendered)
        self.assertIn("backend: local-ollama", rendered)
        self.assertIn("'provider_mode': 'bespoke' is not a backend", rendered)
        self.assertIn("'auto_review': 'yes' is not a boolean", rendered)
        self.assertIn("'advanced_setting' is not a retired setup setting", rendered)

    def test_a_canonical_document_at_the_retired_path_is_not_misread(self):
        result = import_legacy_setup_configuration(
            "schema: 1\n"
            "\n"
            "inference:\n"
            "  backend: cloud-ollama\n"
            "  model: deepseek-v4.1-flash:cloud\n"
        )
        self.assertEqual(result.carried, ())
        self.assertNotIn("cloud-ollama", result.content)
        self.assertIn(
            "the retired path holds a canonical configuration; it was not imported",
            result.content,
        )

    def test_import_notes_are_bounded(self):
        content = "".join(f"unknown_key_{index}: 1\n" for index in range(12))
        result = import_legacy_setup_configuration(content)
        self.assertEqual(len(result.notes), 12)
        rendered_notes = [
            line for line in result.content.splitlines() if line.startswith("# NOTE: ")
        ]
        self.assertEqual(len(rendered_notes), 9)
        self.assertIn("4 more import notes were omitted", result.content)

    def test_import_bounds_size_lines_and_reporting(self):
        oversized = import_legacy_setup_configuration(
            "# " + "x" * (MAX_CONFIG_BYTES + 1)
        )
        self.assertEqual(
            oversized.notes, ("the retired file is larger than the import bound",)
        )
        self.assertEqual(oversized.carried, ())
        long_line = import_legacy_setup_configuration(
            "local_model: " + "x" * 1500 + "\n"
        )
        self.assertEqual(
            long_line.notes,
            ("a line longer than the import bound was not imported",),
        )
        unreadable = import_legacy_setup_configuration(None)
        self.assertEqual(unreadable.notes, ("the retired file could not be read",))
        invalid = import_legacy_setup_configuration("\ud800")
        self.assertEqual(invalid.notes, ("the retired file is not valid UTF-8",))

    def test_retired_keys_report_their_replacements(self):
        result = import_legacy_setup_configuration(
            "version: 0.6.8\nreview_mode: merge-focused\nstages_dir: stages\n"
        )
        joined = "\n".join(result.notes)
        self.assertIn("'version' was retired;", joined)
        self.assertIn("'review_mode' was retired;", joined)
        self.assertIn("'stages_dir' was retired;", joined)

    def test_worker_replacement_table_matches_the_package(self):
        # The Worker importer is a second implementation of the same table.
        # Both sides must name the same replacement for the same retired key,
        # or the setup pull request says different things depending on which
        # entry point created it.
        source = (ROOT / "deploy/cloudflare/src/setup-content.ts").read_text(
            encoding="utf-8"
        )
        block = source.split(
            "LEGACY_SETUP_RETIRED_FIELDS: Readonly<Record<string, string>> = {",
            1,
        )[1].split("};", 1)[0]
        replacements = dict(re.findall(r'(\w+):\s*"((?:[^"\\]|\\.)*)"', block))
        self.assertTrue(replacements)
        for key, replacement in replacements.items():
            with self.subTest(key=key):
                self.assertIn(key, RETIRED_FIELDS)
                self.assertEqual(replacement, RETIRED_FIELDS[key])


class SetupPullRequestBodyTests(unittest.TestCase):
    def test_body_shows_the_default_approval_policy(self):
        body = _setup_pull_request_body()
        self.assertIn("github.reviews: auto-approve", body)
        self.assertIn("github.reviews: blocking", body)
        self.assertIn("github.reviews: advisory", body)
        self.assertNotIn("REVIEWSENSEI_AUTO_APPROVE", body)

    def test_body_limits_old_variable_cleanup_to_an_authorized_pass(self):
        body = _setup_pull_request_body()
        self.assertIn("explicitly authorized cleanup", body)
        self.assertIn("leave credentials and unrelated repository settings", body)
        self.assertNotIn("REVIEWSENSEI_REVIEW_MODE", body)
        self.assertNotIn("REVIEWSENSEI_PROVIDER_MODE", body)
        self.assertNotIn("REVIEWSENSEI_GITHUB_WRITES", body)

    def test_body_reports_the_one_time_import(self):
        body = _setup_pull_request_body(("inference.backend", "github.writes"))
        self.assertIn(
            "carried these settings into .reviewsensei.yml: inference.backend, "
            "github.writes",
            body,
        )
        self.assertIn(
            "plus the settings imported from your retired configuration", body
        )
        plain = _setup_pull_request_body()
        self.assertNotIn("carried these settings", plain)
        self.assertIn("backend choice and nothing else", plain)

    def test_body_bytes_are_shared_with_the_worker_builder(self):
        # The Worker renders the same prose through setupPullRequestBody(), and
        # its suite asserts the same fixture bytes this suite pins.
        source = (ROOT / "deploy/cloudflare/src/setup-content.ts").read_text(
            encoding="utf-8"
        )
        self.assertIn("export function setupPullRequestBody(", source)
        no_import = next(case for case in CASES if case["name"] == "empty-file")
        self.assertEqual(no_import["carried"], [])
        self.assertEqual(_setup_pull_request_body(), no_import["pull_request_body"])
        self.assertEqual(
            hashlib.sha256(_setup_pull_request_body().encode("utf-8")).hexdigest(),
            hashlib.sha256(no_import["pull_request_body"].encode("utf-8")).hexdigest(),
        )

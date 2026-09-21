import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLOUDFLARE = ROOT / "deploy" / "cloudflare"


class CloudflarePackageTests(unittest.TestCase):
    def test_package_contains_worker_only_runtime(self):
        required = (
            CLOUDFLARE / "README.md",
            CLOUDFLARE / "package.json",
            CLOUDFLARE / "package-lock.json",
            CLOUDFLARE / "tsconfig.json",
            CLOUDFLARE / "wrangler.jsonc",
            CLOUDFLARE / "src" / "worker.ts",
            CLOUDFLARE / "src" / "delivery-ledger.ts",
            CLOUDFLARE / "src" / "github-app.ts",
            CLOUDFLARE / "src" / "setup-content.ts",
            CLOUDFLARE / "src" / "broker-ledger.ts",
            CLOUDFLARE / "src" / "token-broker.ts",
        )
        for path in required:
            with self.subTest(path=path):
                self.assertTrue(path.is_file())

        self.assertFalse((CLOUDFLARE / "src" / "container.ts").exists())
        self.assertFalse((CLOUDFLARE / "container" / "Dockerfile").exists())
        self.assertFalse((CLOUDFLARE / "container" / "server.py").exists())
        package = json.loads((CLOUDFLARE / "package.json").read_text())
        self.assertTrue(package["private"])
        self.assertNotIn("@cloudflare/containers", package.get("dependencies", {}))
        self.assertEqual(package["scripts"]["deploy"], "wrangler deploy")
        lock = json.loads((CLOUDFLARE / "package-lock.json").read_text())
        self.assertNotIn("node_modules/@cloudflare/containers", lock["packages"])

    def test_wrangler_declares_sqlite_ledgers(self):
        config = json.loads((CLOUDFLARE / "wrangler.jsonc").read_text())
        self.assertEqual(config["main"], "src/worker.ts")
        self.assertNotIn("containers", config)
        self.assertEqual(
            config["durable_objects"]["bindings"],
            [
                {"name": "DELIVERY_LEDGER", "class_name": "DeliveryLedger"},
                {"name": "BROKER_LEDGER", "class_name": "BrokerLedger"},
            ],
        )
        self.assertEqual(
            config["migrations"][0]["new_sqlite_classes"], ["DeliveryLedger"]
        )
        self.assertEqual(
            config["migrations"][1]["new_sqlite_classes"], ["BrokerLedger"]
        )
        self.assertEqual(
            config["vars"],
            {
                "PUBLIC_WORKFLOW_TAG": "v5",
            },
        )

    def test_package_does_not_embed_secrets_or_payload_storage(self):
        worker = (CLOUDFLARE / "src" / "worker.ts").read_text()
        ledger = (CLOUDFLARE / "src" / "delivery-ledger.ts").read_text()
        github_app = (CLOUDFLARE / "src" / "github-app.ts").read_text()
        setup_content = (CLOUDFLARE / "src" / "setup-content.ts").read_text()
        readme = (CLOUDFLARE / "README.md").read_text()
        for content in (worker, ledger, github_app, setup_content):
            self.assertNotIn("BEGIN RSA PRIVATE KEY", content)
            self.assertNotIn("ghs_", content)
        self.assertNotIn("BEGIN RSA PRIVATE KEY", readme)
        self.assertIn("REPLACE_WITH_LOCAL_PEM_VALUE", readme)
        self.assertIn("never written to SQLite", ledger)
        self.assertNotIn("@cloudflare/containers", worker)
        self.assertIn("RSASSA-PKCS1-v1_5", github_app)
        self.assertIn("review-sensei/setup", github_app)
        self.assertIn('"actions_variables"', github_app)
        self.assertIn('"variables"', github_app)
        self.assertIn("actions/variables", github_app)
        self.assertIn("skipped_unknown_setup", github_app)
        self.assertIn("contents/", github_app)

    def test_delivery_retention_and_setup_idempotency_are_documented(self):
        ledger = (CLOUDFLARE / "src" / "delivery-ledger.ts").read_text()
        readme = (CLOUDFLARE / "README.md").read_text()
        adr = (
            ROOT / "docs" / "adr" / "0020-cloudflare-github-app-package.md"
        ).read_text()
        self.assertIn("const RETENTION_MS = 60 * 60 * 1000;", ledger)
        self.assertIn("retained as accepted for one hour", readme)
        self.assertIn("existing setup branch and pull request", readme)
        self.assertIn("accepted identities for one hour", adr)
        self.assertNotIn("30 days", readme)
        self.assertNotIn("30 days", adr)

    def test_setup_content_is_tagged_and_secret_free(self):
        source = (CLOUDFLARE / "src" / "setup-content.ts").read_text()
        self.assertIn("SETUP_VERSION = 5", source)
        self.assertIn("ReviewSensei setup version: 5", source)
        self.assertIn("PUBLIC_WORKFLOW_TAG", source)
        self.assertNotIn("PUBLIC_WORKFLOW_SHA=", source)
        self.assertNotIn("PUBLIC_WORKFLOW_LEGACY_SHAS", source)
        self.assertIn("secrets.OLLAMA_API_KEY", source)
        self.assertIn("secrets.OPENROUTER_API_KEY", source)
        self.assertIn("REVIEWSENSEI_MODEL", source)
        self.assertIn("github.event.comment.author_association == 'OWNER'", source)
        self.assertIn("github.event.comment.user.type != 'Bot'", source)
        # The Worker's copy of the caller must re-apply the mention,
        # association, and user-type checks on the command arm itself, not only
        # in the resolver job's condition that produces operation=command.
        self.assertIn(
            "      ((github.event_name == 'issue_comment' &&\n"
            "      github.event.action == 'created' &&\n"
            "      github.event.issue.pull_request &&\n"
            "      contains(github.event.comment.body, '@sensei') &&\n"
            "      (github.event.comment.author_association == 'OWNER' ||\n"
            "      github.event.comment.author_association == 'MEMBER' ||\n"
            "      github.event.comment.author_association == 'COLLABORATOR') &&\n"
            "      github.event.comment.user.type != 'Bot' &&\n"
            "      (needs.resolve-trigger.outputs.operation == 'command' ||\n"
            "      vars.REVIEWSENSEI_MENTION_REPLIES == 'true')) ||\n",
            source,
        )
        self.assertIn(
            "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@", source
        )
        self.assertIn("id-token: write", source)
        self.assertNotIn("OLLAMA_API_KEY_VALUE", source)
        self.assertIn(
            "actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803", source
        )
        self.assertIn("REVIEWSENSEI_PROVIDER_MODE", source)
        self.assertIn("REVIEWSENSEI_LOCAL_MODEL", source)
        self.assertIn("REVIEWSENSEI_CLOUD_MODEL", source)
        self.assertIn("qwen3.5:4b", source)
        self.assertIn("deepseek-v4.1-flash:cloud", source)
        self.assertIn("review-sensei-uninstall.yml", source)
        self.assertNotIn("GITHUB_APP_PRIVATE_KEY", source)
        self.assertNotIn("GITHUB_APP_WEBHOOK_SECRET", source)
        self.assertIn(
            'DEFAULT_OPENROUTER_MODEL = "deepseek/deepseek-v4.1-flash"', source
        )

    def test_v4_config_matches_ts_builder_bytes(self):
        from review_sensei.hosting.github.setup import (
            CURRENT_PACKAGE_VERSION,
            DEFAULT_CLOUD_MODEL,
            DEFAULT_LOCAL_MODEL,
            _v4_config_file,
        )

        expected_ts = (
            "# ReviewSensei setup version: 5\n"
            "setup_version: 5\n"
            "provider: ollama\n"
            "provider_mode: local\n"
            "model: ''\n"
            "review_mode: merge-focused\n"
            "base_url: http://127.0.0.1:11434/api\n"
            "cloud_base_url: https://ollama.com/api\n"
            f"local_model: {DEFAULT_LOCAL_MODEL}\n"
            f"cloud_model: {DEFAULT_CLOUD_MODEL}\n"
            f"version: {CURRENT_PACKAGE_VERSION}\n"
            "auto_review: false\n"
            "learning_proposals: false\n"
            "github_writes: false\n"
            "learning_prs: false\n"
            "mention_replies: false\n"
            "upload_artifacts: false\n"
            "stages_dir: ''\n"
            "categories_dir: ''\n"
        )
        self.assertEqual(_v4_config_file(), expected_ts)

    def test_v4_config_core_fields_match_ts_builder(self):
        from review_sensei.hosting.github.setup import (
            CURRENT_PACKAGE_VERSION,
            DEFAULT_CLOUD_MODEL,
            DEFAULT_LOCAL_MODEL,
            _v4_config_file,
        )

        def parse_fields(content: str) -> dict[str, str]:
            fields: dict[str, str] = {}
            for line in content.splitlines():
                if not line or line.startswith("#"):
                    continue
                key, _, value = line.partition(":")
                fields[key.strip()] = value.strip()
            return fields

        py_fields = parse_fields(_v4_config_file())
        ts_source = (CLOUDFLARE / "src" / "setup-content.ts").read_text(
            encoding="utf-8"
        )
        self.assertEqual(
            py_fields,
            {
                "setup_version": "5",
                "provider": "ollama",
                "provider_mode": "local",
                "model": "''",
                "review_mode": "merge-focused",
                "base_url": "http://127.0.0.1:11434/api",
                "cloud_base_url": "https://ollama.com/api",
                "local_model": DEFAULT_LOCAL_MODEL,
                "cloud_model": DEFAULT_CLOUD_MODEL,
                "version": CURRENT_PACKAGE_VERSION,
                "auto_review": "false",
                "learning_proposals": "false",
                "github_writes": "false",
                "learning_prs": "false",
                "mention_replies": "false",
                "upload_artifacts": "false",
                "stages_dir": "''",
                "categories_dir": "''",
            },
        )
        self.assertIn("provider_mode: local", ts_source)
        self.assertIn("model: ''", ts_source)
        self.assertIn("review_mode: merge-focused", ts_source)
        self.assertIn("local_model: ${DEFAULT_LOCAL_MODEL}", ts_source)
        self.assertIn("cloud_model: ${DEFAULT_CLOUD_MODEL}", ts_source)
        self.assertIn("learning_proposals: false", ts_source)

    def test_released_runner_switch_v4_fixture_copies_share_canonical_digest(self):
        import hashlib

        from review_sensei.hosting.github.setup import (
            RELEASED_RUNNER_SWITCH_V4_SHA256,
            _released_runner_switch_v4_caller_bytes,
            _released_runner_switch_v4_workflow,
            _tagged_workflow,
        )

        canonical = (
            ROOT
            / "tests"
            / "fixtures"
            / "setup-legacy"
            / "released-v4-resolve-trigger-runner-switch.yml"
        ).read_text(encoding="utf-8")
        copies = {
            "python-package": (
                ROOT
                / "src"
                / "review_sensei"
                / "hosting"
                / "github"
                / "fixtures"
                / "released-v4-resolve-trigger-runner-switch.yml"
            ).read_text(encoding="utf-8"),
            "worker-bundle-source": (
                CLOUDFLARE
                / "fixtures"
                / "released-v4-resolve-trigger-runner-switch.yml"
            ).read_text(encoding="utf-8"),
        }
        self.assertEqual(
            hashlib.sha256(canonical.encode()).hexdigest(),
            RELEASED_RUNNER_SWITCH_V4_SHA256,
        )
        for name, content in copies.items():
            with self.subTest(copy=name):
                self.assertEqual(content, canonical)
        self.assertEqual(_released_runner_switch_v4_caller_bytes(), canonical)
        self.assertEqual(_released_runner_switch_v4_workflow("v4"), canonical)
        self.assertEqual(
            _tagged_workflow("v5"),
            (
                ROOT / "examples" / "github-actions" / "review-sensei-review.yml"
            ).read_text(encoding="utf-8"),
        )
        ts_source = (
            CLOUDFLARE / "src" / "released-runner-switch-v4-caller.ts"
        ).read_text(encoding="utf-8")
        self.assertIn("releasedRunnerSwitchV4CallerFixture", ts_source)
        self.assertIn(RELEASED_RUNNER_SWITCH_V4_SHA256, ts_source)

    def test_setup_builders_share_constants_and_variables(self):
        import re

        from review_sensei.hosting.github.setup import (
            CURRENT_PACKAGE_VERSION,
            DEFAULT_CLOUD_MODEL,
            DEFAULT_LOCAL_MODEL,
            DEFAULT_OPENROUTER_MODEL,
            DEFAULT_PROVIDER_MODE,
            SETUP_VARIABLES,
            SetupPlanBuilder,
        )

        ts_source = (CLOUDFLARE / "src" / "setup-content.ts").read_text(
            encoding="utf-8"
        )
        for const_name, value in (
            ("DEFAULT_PROVIDER_MODE", DEFAULT_PROVIDER_MODE),
            ("DEFAULT_LOCAL_MODEL", DEFAULT_LOCAL_MODEL),
            ("DEFAULT_CLOUD_MODEL", DEFAULT_CLOUD_MODEL),
            ("DEFAULT_OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL),
        ):
            self.assertIn(f'{const_name} = "{value}"', ts_source)
        ts_constants = {
            "DEFAULT_PROVIDER_MODE": DEFAULT_PROVIDER_MODE,
            "DEFAULT_LOCAL_MODEL": DEFAULT_LOCAL_MODEL,
            "DEFAULT_CLOUD_MODEL": DEFAULT_CLOUD_MODEL,
            "DEFAULT_OPENROUTER_MODEL": DEFAULT_OPENROUTER_MODEL,
            "REVIEWSENSEI_VERSION": CURRENT_PACKAGE_VERSION,
        }
        block_match = re.search(
            r"export const SETUP_VARIABLES.*?=\s*\[(.*?)\];",
            ts_source,
            re.DOTALL,
        )
        self.assertIsNotNone(block_match)
        ts_variables: list[tuple[str, str]] = []
        for match in re.finditer(
            r'\{\s*name:\s*"([^"]+)"\s*,\s*value:\s*(?:"([^"]*)"|([A-Z][A-Z0-9_]*))\s*\}',
            block_match.group(1),
        ):
            name, literal, const_ref = match.groups()
            value = literal if literal is not None else ts_constants[const_ref]
            ts_variables.append((name, value))
        self.assertEqual(ts_variables, list(SETUP_VARIABLES))
        py_workflow = SetupPlanBuilder().build("owner/repo").files[0].content
        example = (
            ROOT / "examples" / "github-actions" / "review-sensei-review.yml"
        ).read_text(encoding="utf-8")
        self.assertEqual(py_workflow, example)

    def test_user_guidance_describes_setup_v4_publication_contract(self):
        readme = (ROOT / "README.md").read_text()
        installation = (ROOT / "docs" / "installation.md").read_text()
        for content in (readme, installation):
            self.assertIn("setup-v5", content)
            self.assertIn("nine", content)
            self.assertIn("five", content)
            self.assertIn("automatic", content)
            self.assertIn("summary", content)
            self.assertIn("inline", content)
            self.assertIn("REVIEWSENSEI_UPLOAD_ARTIFACTS", content)
            self.assertNotIn("review-sensei-version.txt", content)

    def test_worker_does_not_dispatch_reviews_or_invent_sha_concurrency_keys(self):
        worker = (CLOUDFLARE / "src" / "worker.ts").read_text(encoding="utf-8")
        github_app = (CLOUDFLARE / "src" / "github-app.ts").read_text(encoding="utf-8")
        setup = (CLOUDFLARE / "src" / "setup-content.ts").read_text(encoding="utf-8")
        example = (
            ROOT / "examples" / "github-actions" / "review-sensei-review.yml"
        ).read_text(encoding="utf-8")
        readme = (CLOUDFLARE / "README.md").read_text(encoding="utf-8")
        for content in (worker, github_app):
            with self.subTest(source="runtime"):
                self.assertNotIn("concurrency:", content)
                self.assertNotIn("source_comment_id || head_sha", content)
                self.assertNotIn("head_sha || head_ref || run_id", content)
                self.assertNotIn("reviewsensei-review-", content)
        self.assertIn("review-sensei-run.yml@", setup)
        self.assertNotIn("source_comment_id || head_sha", setup)
        self.assertNotIn("head_sha || head_ref || run_id", setup)
        self.assertNotRegex(example, r"(?m)^concurrency:")
        self.assertNotIn("group:", example)
        self.assertIn(
            "uses: malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@v5",
            example,
        )
        self.assertIn("not a hosted review engine", readme)
        self.assertIn("does not invent a SHA-based concurrency key", readme)
        self.assertIn("reusable workflow", readme)


if __name__ == "__main__":
    unittest.main()

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
        # Setup provisions no repository variables, so the App holds no
        # variable API surface at all: there is nothing left that could write
        # customer settings the product does not read.
        self.assertNotIn("actions_variables", github_app)
        self.assertNotIn("variables", github_app)
        self.assertNotIn("actions/variables", github_app)
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
        # Every credential is a declared, name-only optional secret, and the
        # current caller forwards exactly the three provider keys.
        for name in ("OLLAMA_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY"):
            with self.subTest(secret=name):
                self.assertIn(f"secrets.{name}", source)
        self.assertIn(
            "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@", source
        )
        self.assertIn("review-sensei-uninstall.yml", source)
        self.assertNotIn("OLLAMA_API_KEY_VALUE", source)
        self.assertNotIn("GITHUB_APP_PRIVATE_KEY", source)
        self.assertNotIn("GITHUB_APP_WEBHOOK_SECRET", source)
        # The current caller is the same thin bootstrap the Python package
        # emits: event-shape-only routing, read-only plus OIDC permissions, and
        # no configuration read of any kind.
        caller = source.split("function resolveTriggerWorkflowTemplate", 1)[1]
        caller = caller.split("\n}\n", 1)[0]
        self.assertIn("github.event.comment.author_association == 'OWNER'", caller)
        self.assertIn("github.event.comment.user.type != 'Bot'", caller)
        self.assertIn(
            "      (github.event.comment.author_association == 'OWNER' ||\n"
            "      github.event.comment.author_association == 'MEMBER' ||\n"
            "      github.event.comment.author_association == 'COLLABORATOR') &&\n"
            "      github.event.comment.user.type != 'Bot') ||\n",
            caller,
        )
        self.assertIn(
            "permissions:\n"
            "  contents: read\n"
            "  pull-requests: read\n"
            "  issues: read\n"
            "  id-token: write\n",
            caller,
        )
        self.assertIn("needs.resolve-trigger.outputs.operation", caller)
        self.assertIn(
            "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1", caller
        )
        self.assertNotIn("vars.", caller)

    def test_current_config_matches_ts_builder_bytes(self):
        from review_sensei.hosting.github.setup import _current_config_file

        ts_source = (CLOUDFLARE / "src" / "setup-content.ts").read_text(
            encoding="utf-8"
        )
        builder = ts_source.split("export function currentConfigFile", 1)[1]
        builder = builder.split("\n}\n", 1)[0]
        # The Worker builder concatenates the same literal fragments the Python
        # builder emits, so a change to either side breaks this test.
        fragments = (
            "`# ReviewSensei setup version: ${SETUP_VERSION}\\n`",
            '"schema: 1\\n"',
            '"\\n"',
            '"inference:\\n"',
            '"  backend: local-ollama\\n"',
        )
        for fragment in fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, builder)
        self.assertEqual(
            _current_config_file(),
            "# ReviewSensei setup version: 5\n"
            "schema: 1\n"
            "\n"
            "inference:\n"
            "  backend: local-ollama\n",
        )

    def test_current_config_names_no_behavior_switch(self):
        from review_sensei.hosting.github.setup import _current_config_file

        content = _current_config_file()
        self.assertIn("schema: 1", content)
        self.assertIn("inference:", content)
        self.assertIn("  backend: local-ollama", content)
        # The minimal file the App generates names the setup-time backend
        # choice and nothing else: every behavior field lives in the operator
        # copy of the file, not in generated bytes.
        for retired in (
            "provider_mode",
            "review_mode",
            "local_model",
            "cloud_model",
            "auto_review",
            "github_writes",
            "learning_prs",
            "learning_proposals",
            "mention_replies",
            "upload_artifacts",
            "stages_dir",
            "categories_dir",
        ):
            with self.subTest(retired=retired):
                self.assertNotIn(retired, content)

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

    def test_setup_builders_share_paths_and_minimal_bytes(self):
        from review_sensei.hosting.github.setup import (
            SETUP_FILE_PATHS,
            SetupPlanBuilder,
            _current_config_file,
            _current_uninstall_workflow,
        )

        ts_source = (CLOUDFLARE / "src" / "setup-content.ts").read_text(
            encoding="utf-8"
        )
        # Python and the Worker manage exactly the same three paths, and no
        # builder on either side creates a repository variable.
        self.assertEqual(
            SETUP_FILE_PATHS,
            (
                ".github/workflows/review-sensei-review.yml",
                ".github/workflows/review-sensei-uninstall.yml",
                ".reviewsensei.yml",
            ),
        )
        block_start = ts_source.index("export const SETUP_FILE_PATHS")
        block_end = ts_source.index("];", block_start) + len("];")
        self.assertEqual(
            "export const SETUP_FILE_PATHS: readonly string[] = [\n"
            "  SETUP_WORKFLOW_PATH,\n"
            "  SETUP_UNINSTALL_WORKFLOW_PATH,\n"
            "  CONFIG_PATH,\n"
            "];",
            ts_source[block_start:block_end],
        )
        for path in SETUP_FILE_PATHS:
            with self.subTest(path=path):
                self.assertIn(f'"{path}"', ts_source)
        self.assertNotIn("SETUP_VARIABLES", ts_source)
        self.assertNotIn("actions/variables", ts_source)
        # Both uninstall builders remove the retired configuration location
        # alongside the current files.
        for content in (_current_uninstall_workflow(), ts_source):
            with self.subTest(builder="uninstall"):
                self.assertIn('".github/review-sensei/config.yml",', content)
        self.assertIn('".reviewsensei.yml",', _current_uninstall_workflow())
        plan = SetupPlanBuilder().build("owner/repo")
        self.assertEqual([file.path for file in plan.files], list(SETUP_FILE_PATHS))
        self.assertEqual(plan.files[-1].content, _current_config_file())
        example = (
            ROOT / "examples" / "github-actions" / "review-sensei-review.yml"
        ).read_text(encoding="utf-8")
        self.assertEqual(plan.files[0].content, example)
        self.assertEqual(plan.files[1].content, _current_uninstall_workflow())

    def test_user_guidance_describes_setup_v5_publication_contract(self):
        from review_sensei.hosting.github.setup import SETUP_FILE_PATHS

        readme = (ROOT / "README.md").read_text()
        installation = (ROOT / "docs" / "installation.md").read_text()
        for content in (readme, installation):
            normalized = " ".join(content.split())
            self.assertIn("setup-v5", content)
            self.assertIn(f"{len(SETUP_FILE_PATHS)} generated files", normalized)
            self.assertIn("creates no repository variables", normalized)
            self.assertIn("REVIEWSENSEI_PROVIDER", content)
            self.assertIn("REVIEWSENSEI_MODEL", content)
            self.assertIn(".reviewsensei.yml", content)
            self.assertIn("automatic", content)
            self.assertIn("summary", content)
            self.assertIn("inline", content)
            self.assertNotIn("review-sensei-version.txt", content)
            self.assertNotIn("repository variables, including", normalized)
            self.assertNotIn("boolean controls", normalized)
        for path in SETUP_FILE_PATHS:
            with self.subTest(path=path):
                self.assertIn(f"`{path}`", installation)
        # Every behavior the old variable surface controlled is now a
        # documented field of the canonical configuration.
        for field_path in (
            "github.automatic_reviews",
            "github.writes",
            "github.reviews",
            "github.mentions",
            "github.learning",
            "github.artifacts",
        ):
            with self.subTest(field=field_path):
                self.assertIn(field_path, installation)
        self.assertIn("inference.backend", installation)
        self.assertIn("inference.model", installation)

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

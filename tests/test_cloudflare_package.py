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
            CLOUDFLARE / "src" / "github-app.ts",
            CLOUDFLARE / "src" / "setup-content.ts",
            CLOUDFLARE / "src" / "token-broker.ts",
            CLOUDFLARE / "src" / "signed-mac.ts",
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
        self.assertNotIn("durable_objects", config)
        self.assertNotIn("kv_namespaces", config)
        self.assertNotIn("d1_databases", config)
        self.assertNotIn("r2_buckets", config)
        self.assertNotIn("ratelimits", config)
        self.assertEqual(
            config["migrations"][2]["deleted_classes"],
            ["DeliveryLedger", "BrokerLedger"],
        )
        self.assertEqual(
            config["vars"],
            {
                "PUBLIC_WORKFLOW_TAG": "v5",
            },
        )

    def test_package_does_not_embed_secrets_or_payload_storage(self):
        worker = (CLOUDFLARE / "src" / "worker.ts").read_text()
        github_app = (CLOUDFLARE / "src" / "github-app.ts").read_text()
        setup_content = (CLOUDFLARE / "src" / "setup-content.ts").read_text()
        readme = (CLOUDFLARE / "README.md").read_text()
        for content in (worker, github_app, setup_content):
            self.assertNotIn("BEGIN RSA PRIVATE KEY", content)
            self.assertNotIn("ghs_", content)
        self.assertNotIn("BEGIN RSA PRIVATE KEY", readme)
        self.assertIn("REPLACE_WITH_LOCAL_PEM_VALUE", readme)
        self.assertIn("The Worker stores nothing", readme)
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
        readme = (CLOUDFLARE / "README.md").read_text()
        adr = (
            ROOT / "docs" / "adr" / "0020-cloudflare-github-app-package.md"
        ).read_text()
        self.assertIn("`DeliveryLedger` and `BrokerLedger` are removed", readme)
        self.assertIn("existing setup branch and pull request", readme)
        self.assertIn("The Worker stores nothing", readme)
        self.assertIn("`DeliveryLedger` and `BrokerLedger` are removed", adr)
        self.assertNotIn("30 days", readme)
        self.assertNotIn("30 days", adr)

    def test_setup_content_is_tagged_and_secret_free(self):
        source = (CLOUDFLARE / "src" / "setup-content.ts").read_text()
        self.assertIn("SETUP_VERSION = 6", source)
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
        caller = source.split("function resolveTriggerV5WorkflowTemplate", 1)[1]
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
            '"# ReviewSensei setup version: 5\\n"',
            '"# Configuration version. Leave this at 1.\\n"',
            '"schema: 1\\n"',
            '"# Backend and model used for review.\\n"',
            '"inference:\\n"',
            '"  # local-ollama, cloud-ollama, openrouter, or openai-compatible\\n"',
            '"  backend: local-ollama\\n"',
            '"  # Model slug for the selected backend\\n"',
            '"  model: qwen3.5:4b\\n"',
            '"# GitHub publication and conversation policy.\\n"',
            '"github:\\n"',
            '"  # Review eligible pull requests automatically\\n"',
            '"  automatic_reviews: true\\n"',
            '"  # Publish the validated result to GitHub\\n"',
            '"  writes: false\\n"',
            '"  # auto-approve (request changes or approve), blocking, or advisory\\n"',
            '"  reviews: auto-approve\\n"',
            '"  # Reply to authorized @reviewsensei (or @sensei) mentions\\n"',
            '"  mentions: true\\n"',
            '"  # disabled, proposals, or pull-requests\\n"',
            '"  learning: disabled\\n"',
            '"  # none or diagnostics\\n"',
            '"  artifacts: none\\n"',
            '"  operation_entry: enabled\\n"',
            '"# Optional limits and endpoint overrides.\\n"',
            '"    # Required to send requests to a custom API root\\n"',
            '"    allow_custom_endpoint: false\\n"',
            '"      # Follow symbols from the diff into trusted files\\n"',
            '"      enabled: false\\n"',
            '"      # Maximum files loaded for symbol lookup\\n"',
            '"      max_files: 16\\n"',
            '"      # Maximum bytes loaded for symbol lookup\\n"',
            '"      max_bytes: 131072\\n"',
            '"      # Maximum symbol-follow depth\\n"',
            '"      max_depth: 1\\n"',
            '"    # Permit non-loopback provider traffic\\n"',
            '"    allow_data_egress: false\\n"',
            '"    # Split a large diff into bounded review slices\\n"',
            '"    orchestrate: false\\n"',
            '"    # Provider request timeout in seconds\\n"',
            '"    timeout_seconds: 900\\n"',
            '"    # Maximum provider calls for one review\\n"',
            '"    max_provider_calls: 8\\n"',
        )
        for fragment in fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, builder)
        self.assertEqual(
            _current_config_file(),
            "# ReviewSensei setup version: 5\n"
            "# Configuration version. Leave this at 1.\n"
            "schema: 1\n"
            "\n"
            "# Backend and model used for review.\n"
            "inference:\n"
            "  # local-ollama, cloud-ollama, openrouter, or openai-compatible\n"
            "  backend: local-ollama\n"
            "  # Model slug for the selected backend\n"
            "  model: qwen3.5:4b\n"
            "\n"
            "# GitHub publication and conversation policy.\n"
            "github:\n"
            "  # Review eligible pull requests automatically\n"
            "  automatic_reviews: true\n"
            "  # Publish the validated result to GitHub\n"
            "  writes: false\n"
            "  # auto-approve (request changes or approve), blocking, or advisory\n"
            "  reviews: auto-approve\n"
            "  # Reply to authorized @reviewsensei (or @sensei) mentions\n"
            "  mentions: true\n"
            "  # disabled, proposals, or pull-requests\n"
            "  learning: disabled\n"
            "  # none or diagnostics\n"
            "  artifacts: none\n"
            "  # enabled uses the shared operation entry. disabled keeps the previous paths.\n"
            "  # The 64/60 budget for this route is not yet measured.\n"
            "  operation_entry: enabled\n"
            "\n"
            "# Optional limits and endpoint overrides.\n"
            "advanced:\n"
            "  endpoint:\n"
            "    # Required to send requests to a custom API root\n"
            "    allow_custom_endpoint: false\n"
            "  context:\n"
            "    symbol_context:\n"
            "      # Follow symbols from the diff into trusted files\n"
            "      enabled: false\n"
            "      # Maximum files loaded for symbol lookup\n"
            "      max_files: 16\n"
            "      # Maximum bytes loaded for symbol lookup\n"
            "      max_bytes: 131072\n"
            "      # Maximum symbol-follow depth\n"
            "      max_depth: 1\n"
            "  egress:\n"
            "    # Permit non-loopback provider traffic\n"
            "    allow_data_egress: false\n"
            "  large_changes:\n"
            "    # Split a large diff into bounded review slices\n"
            "    orchestrate: false\n"
            "  resources:\n"
            "    # Provider request timeout in seconds\n"
            "    timeout_seconds: 900\n"
            "    # Maximum provider calls for one review\n"
            "    max_provider_calls: 8\n",
        )

    def test_current_config_lists_packaged_defaults(self):
        from review_sensei.configuration import parse_configuration_text
        from review_sensei.hosting.github.setup import _current_config_file

        content = _current_config_file()
        configuration = parse_configuration_text(content, source=".reviewsensei.yml")
        self.assertEqual(configuration.inference.backend, "local-ollama")
        self.assertEqual(configuration.inference.model, "qwen3.5:4b")
        self.assertTrue(configuration.github.automatic_reviews)
        self.assertFalse(configuration.github.writes)
        self.assertEqual(configuration.github.reviews, "auto-approve")
        self.assertTrue(configuration.github.mentions)
        self.assertEqual(configuration.github.learning, "disabled")
        self.assertEqual(configuration.github.artifacts, "none")
        self.assertFalse(configuration.advanced.endpoint.allow_custom_endpoint)
        self.assertFalse(configuration.advanced.context.symbol_context.enabled)
        self.assertEqual(configuration.advanced.resources.timeout_seconds, 900)
        self.assertEqual(configuration.advanced.resources.max_provider_calls, 8)
        self.assertIn("# Configuration version. Leave this at 1.", content)
        self.assertIn(
            "# auto-approve (request changes or approve), blocking, or advisory",
            content,
        )
        self.assertIn("# Maximum provider calls for one review", content)
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
        group_lines = [
            line.strip()
            for line in example.splitlines()
            if line.strip().startswith("group:")
        ]
        self.assertEqual(
            group_lines,
            [
                "group: reviewsensei-provider-review-${{ github.repository }}-${{ needs.resolve-trigger.outputs.pull_request_number || github.event.pull_request.number || github.run_id }}",
            ],
        )
        self.assertNotIn("sha", group_lines[0])
        self.assertIn(
            "uses: malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@v5",
            example,
        )
        self.assertIn("not a hosted review engine", readme)
        self.assertIn("does not invent a SHA-based concurrency key", readme)
        self.assertIn("reusable workflow", readme)


if __name__ == "__main__":
    unittest.main()

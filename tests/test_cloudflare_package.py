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
                "PUBLIC_WORKFLOW_TAG": "v4",
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
        self.assertIn("SETUP_VERSION = 4", source)
        self.assertIn("ReviewSensei setup version: 4", source)
        self.assertIn("PUBLIC_WORKFLOW_TAG", source)
        self.assertNotIn("PUBLIC_WORKFLOW_SHA=", source)
        self.assertNotIn("PUBLIC_WORKFLOW_LEGACY_SHAS", source)
        self.assertIn("secrets.OLLAMA_API_KEY", source)
        self.assertIn("github.event.comment.author_association == 'OWNER'", source)
        self.assertIn("github.event.comment.user.type != 'Bot'", source)
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
        self.assertIn("deepseek-v4-flash:cloud", source)
        self.assertIn("review-sensei-uninstall.yml", source)
        self.assertNotIn("GITHUB_APP_PRIVATE_KEY", source)
        self.assertNotIn("GITHUB_APP_WEBHOOK_SECRET", source)

    def test_user_guidance_describes_setup_v4_publication_contract(self):
        readme = (ROOT / "README.md").read_text()
        installation = (ROOT / "docs" / "installation.md").read_text()
        for content in (readme, installation):
            self.assertIn("setup-v4", content)
            self.assertIn("nine", content)
            self.assertIn("five", content)
            self.assertIn("automatic", content)
            self.assertIn("summary", content)
            self.assertIn("inline", content)
            self.assertIn("REVIEWSENSEI_UPLOAD_ARTIFACTS", content)
            self.assertNotIn("review-sensei-version.txt", content)


if __name__ == "__main__":
    unittest.main()

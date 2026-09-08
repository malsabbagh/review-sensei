import hashlib
import hmac
import json
import unittest

from review_sensei.hosting.github import (
    EnvWebhookSecretSource,
    GitHubWebhookError,
    GitHubWebhookSignatureError,
    InMemoryDeliveryLedger,
    WebhookVerifier,
)


class BytesWebhookSecretSource:
    def __init__(self, secret):
        self.secret = secret

    def load_webhook_secret(self):
        return self.secret


def signature(body, secret=b"webhook-secret"):
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def installation_created_body(repository="owner/repo", *, suspended=False):
    return json.dumps(
        {
            "action": "created",
            "installation": {
                "id": 7,
                "suspended_at": "2026-01-01T00:00:00Z" if suspended else None,
                "permissions": {"contents": "write", "pull_requests": "write"},
            },
            "repository": {"id": 11, "full_name": repository},
        },
        separators=(",", ":"),
    ).encode("utf-8")


def installation_repositories_added_body(repositories):
    return json.dumps(
        {
            "action": "added",
            "installation": {
                "id": 7,
                "suspended_at": None,
                "permissions": {"contents": "write", "pull_requests": "write"},
            },
            "repositories": [
                {"id": 11, "full_name": repositories[0]},
                {"id": 12, "full_name": repositories[1]},
            ],
        },
        separators=(",", ":"),
    ).encode("utf-8")


def installation_repositories_removed_body(repositories):
    return json.dumps(
        {
            "action": "removed",
            "installation": {
                "id": 7,
                "suspended_at": None,
                "permissions": {"contents": "write", "pull_requests": "write"},
            },
            "repositories_removed": [
                {"id": 11, "full_name": repositories[0]},
                {"id": 12, "full_name": repositories[1]},
            ],
        },
        separators=(",", ":"),
    ).encode("utf-8")


def installation_new_permissions_body(repositories):
    return json.dumps(
        {
            "action": "new_permissions_accepted",
            "installation": {
                "id": 7,
                "suspended_at": None,
                "permissions": {"contents": "write", "pull_requests": "write"},
            },
            "repositories": [
                {"id": 11, "full_name": repositories[0]},
                {"id": 12, "full_name": repositories[1]},
            ],
        },
        separators=(",", ":"),
    ).encode("utf-8")


class GitHubWebhookTests(unittest.TestCase):
    def make_verifier(self, **kwargs):
        return WebhookVerifier(
            app_id=123,
            secret_source=BytesWebhookSecretSource(b"webhook-secret"),
            **kwargs,
        )

    def test_accepts_valid_signature_and_returns_delivery_metadata(self):
        body = installation_created_body()
        delivery = self.make_verifier().verify(
            body=body,
            signature=signature(body),
            event="installation",
            delivery_id="delivery-1",
        )

        self.assertEqual(delivery.app_id, 123)
        self.assertEqual(delivery.event, "installation")
        self.assertEqual(delivery.action, "created")
        self.assertEqual(delivery.installation_id, 7)
        self.assertEqual(delivery.repository, "owner/repo")
        self.assertEqual(delivery.repositories, ("owner/repo",))
        self.assertEqual(delivery.repository_id, 11)
        self.assertFalse(delivery.suspended)
        self.assertEqual(len(delivery.digest), 64)

    def test_accepts_multiple_repositories_and_returns_all_slugs(self):
        body = installation_repositories_added_body(("owner/one", "owner/two"))
        delivery = self.make_verifier().verify(
            body=body,
            signature=signature(body),
            event="installation_repositories",
            delivery_id="delivery-1",
        )

        self.assertEqual(delivery.event, "installation_repositories")
        self.assertEqual(delivery.repository, "owner/one")
        self.assertEqual(delivery.repositories, ("owner/one", "owner/two"))

    def test_accepts_removed_repositories_without_requiring_added_metadata(self):
        body = installation_repositories_removed_body(("owner/one", "owner/two"))
        delivery = self.make_verifier().verify(
            body=body,
            signature=signature(body),
            event="installation_repositories",
            delivery_id="delivery-removed",
        )

        self.assertEqual(delivery.event, "installation_repositories")
        self.assertEqual(delivery.action, "removed")
        self.assertIsNone(delivery.repository)
        self.assertEqual(delivery.repositories, ())

    def test_new_permissions_accepted_returns_multiple_repositories(self):
        body = installation_new_permissions_body(("owner/one", "owner/two"))
        delivery = self.make_verifier().verify(
            body=body,
            signature=signature(body),
            event="installation",
            delivery_id="delivery-1",
        )

        self.assertEqual(delivery.event, "installation")
        self.assertEqual(delivery.action, "new_permissions_accepted")
        self.assertEqual(delivery.repository, "owner/one")
        self.assertEqual(delivery.repositories, ("owner/one", "owner/two"))

    def test_rejects_missing_signature(self):
        body = installation_created_body()
        with self.assertRaises(GitHubWebhookSignatureError) as raised:
            self.make_verifier().verify(
                body=body,
                signature=None,
                event="installation",
                delivery_id="delivery-1",
            )
        self.assertEqual(raised.exception.error_category, "github_webhook_signature")

    def test_rejects_invalid_signature_encoding(self):
        body = installation_created_body()
        with self.assertRaises(GitHubWebhookSignatureError):
            self.make_verifier().verify(
                body=body,
                signature="sha256=\u263a",
                event="installation",
                delivery_id="delivery-1",
            )

    def test_rejects_invalid_signature(self):
        body = installation_created_body()
        with self.assertRaises(GitHubWebhookSignatureError):
            self.make_verifier().verify(
                body=body,
                signature="sha256=" + "0" * 64,
                event="installation",
                delivery_id="delivery-1",
            )

    def test_rejects_malformed_and_oversized_bodies(self):
        verifier = self.make_verifier(max_body_bytes=1024)
        body = b"not-json"
        with self.assertRaises(GitHubWebhookError):
            verifier.verify(
                body=body,
                signature=signature(body),
                event="installation",
                delivery_id="delivery-1",
            )
        oversized = b"x" * 1025
        with self.assertRaises(GitHubWebhookError):
            verifier.verify(
                body=oversized,
                signature=signature(oversized),
                event="installation",
                delivery_id="delivery-2",
            )

    def test_rejects_duplicate_delivery_before_payload_validation(self):
        ledger = InMemoryDeliveryLedger(max_entries=100)
        ledger.record(app_id=123, delivery_id="delivery-1")
        verifier = self.make_verifier(ledger=ledger)
        body = installation_created_body()
        with self.assertRaises(GitHubWebhookError):
            verifier.verify(
                body=body,
                signature=signature(body),
                event="installation",
                delivery_id="delivery-1",
            )

    def test_ledger_evicts_old_entries(self):
        ledger = InMemoryDeliveryLedger(max_entries=1)
        ledger.record(app_id=1, delivery_id="one")
        ledger.record(app_id=2, delivery_id="two")
        self.assertFalse(ledger.is_known(app_id=1, delivery_id="one"))
        self.assertTrue(ledger.is_known(app_id=2, delivery_id="two"))

    def test_ledger_rejects_invalid_size(self):
        with self.assertRaises(GitHubWebhookError):
            InMemoryDeliveryLedger(max_entries=0)

    def test_rejects_duplicate_delivery(self):
        ledger = InMemoryDeliveryLedger(max_entries=100)
        verifier = self.make_verifier(ledger=ledger)
        body = installation_created_body()

        verifier.verify(
            body=body,
            signature=signature(body),
            event="installation",
            delivery_id="delivery-1",
        )

        with self.assertRaises(GitHubWebhookError) as raised:
            verifier.verify(
                body=body,
                signature=signature(body),
                event="installation",
                delivery_id="delivery-1",
            )
        self.assertEqual(raised.exception.error_category, "github_webhook")

    def test_valid_signed_malformed_delivery_is_deduplicated(self):
        ledger = InMemoryDeliveryLedger(max_entries=100)
        verifier = self.make_verifier(ledger=ledger)
        malformed = json.dumps(
            {
                "action": "created",
                "installation": {"id": 7, "suspended_at": None},
            },
            separators=(",", ":"),
        ).encode("utf-8")

        with self.assertRaises(GitHubWebhookError):
            verifier.verify(
                body=malformed,
                signature=signature(malformed),
                event="installation",
                delivery_id="delivery-malformed",
            )
        self.assertTrue(ledger.is_known(app_id=123, delivery_id="delivery-malformed"))

        with self.assertRaises(GitHubWebhookError):
            verifier.verify(
                body=malformed,
                signature=signature(malformed),
                event="installation",
                delivery_id="delivery-malformed",
            )

    def test_unsupported_event_is_not_recorded_as_processed(self):
        ledger = InMemoryDeliveryLedger(max_entries=100)
        verifier = self.make_verifier(ledger=ledger)
        body = installation_created_body()

        with self.assertRaises(GitHubWebhookError):
            verifier.verify(
                body=body,
                signature=signature(body),
                event="push",
                delivery_id="delivery-push",
            )
        self.assertFalse(ledger.is_known(app_id=123, delivery_id="delivery-push"))

    def test_rejects_unsupported_event_and_missing_repository(self):
        body = installation_created_body()
        with self.assertRaises(GitHubWebhookError):
            self.make_verifier().verify(
                body=body,
                signature=signature(body),
                event="push",
                delivery_id="delivery-1",
            )
        missing_repo = json.dumps(
            {
                "action": "created",
                "installation": {"id": 7, "suspended_at": None},
            },
            separators=(",", ":"),
        ).encode("utf-8")
        with self.assertRaises(GitHubWebhookError):
            self.make_verifier().verify(
                body=missing_repo,
                signature=signature(missing_repo),
                event="installation",
                delivery_id="delivery-2",
            )

    def test_records_suspended_delivery(self):
        body = installation_created_body(suspended=True)
        delivery = self.make_verifier().verify(
            body=body,
            signature=signature(body),
            event="installation",
            delivery_id="delivery-1",
        )
        self.assertTrue(delivery.suspended)

    def test_env_secret_source_reads_variable(self):
        import os

        os.environ["CODE_SENSEI_TEST_WEBHOOK_SECRET"] = "env-secret"
        try:
            self.assertEqual(
                EnvWebhookSecretSource(
                    "CODE_SENSEI_TEST_WEBHOOK_SECRET"
                ).load_webhook_secret(),
                b"env-secret",
            )
        finally:
            del os.environ["CODE_SENSEI_TEST_WEBHOOK_SECRET"]

    def test_env_secret_source_requires_variable(self):
        import os

        if "CODE_SENSEI_MISSING_WEBHOOK_SECRET" in os.environ:
            del os.environ["CODE_SENSEI_MISSING_WEBHOOK_SECRET"]
        with self.assertRaises(GitHubWebhookError):
            EnvWebhookSecretSource(
                "CODE_SENSEI_MISSING_WEBHOOK_SECRET"
            ).load_webhook_secret()

    def test_env_secret_source_rejects_empty_secret(self):
        import os

        os.environ["CODE_SENSEI_EMPTY_WEBHOOK_SECRET"] = ""
        try:
            with self.assertRaises(GitHubWebhookError):
                EnvWebhookSecretSource(
                    "CODE_SENSEI_EMPTY_WEBHOOK_SECRET"
                ).load_webhook_secret()
        finally:
            del os.environ["CODE_SENSEI_EMPTY_WEBHOOK_SECRET"]

    def test_verifier_rejects_empty_secret_source(self):
        class EmptyWebhookSecretSource:
            def load_webhook_secret(self):
                return b""

        verifier = WebhookVerifier(
            app_id=123,
            secret_source=EmptyWebhookSecretSource(),
        )
        body = installation_created_body()
        with self.assertRaises(GitHubWebhookError):
            verifier.verify(
                body=body,
                signature=signature(body),
                event="installation",
                delivery_id="delivery-1",
            )

    def test_verifier_rejects_invalid_app_id_and_body_limit(self):
        with self.assertRaises(GitHubWebhookError):
            WebhookVerifier(
                app_id=0,
                secret_source=BytesWebhookSecretSource(b"webhook-secret"),
            )
        with self.assertRaises(GitHubWebhookError):
            WebhookVerifier(
                app_id=123,
                secret_source=BytesWebhookSecretSource(b"webhook-secret"),
                max_body_bytes=0,
            )


if __name__ == "__main__":
    unittest.main()

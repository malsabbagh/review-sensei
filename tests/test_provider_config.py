import unittest

from review_sensei.errors import ReviewInputError
from review_sensei.provider_config import (
    DEFAULT_OPENROUTER_MODEL,
    DEFAULT_OPENROUTER_UPSTREAM,
    HOSTED_OPENROUTER_DEFAULTS,
    hosted_openrouter_upstream,
    published_hosted_openrouter_models,
    validate_hosted_workflow_model,
)


class HostedWorkflowModelValidationTests(unittest.TestCase):
    def test_empty_model_is_allowed(self) -> None:
        validate_hosted_workflow_model(
            provider_mode="cloud-ollama",
            workflow_mode="automatic",
            model="",
        )

    def test_openrouter_rejects_ollama_cloud_slug(self) -> None:
        with self.assertRaisesRegex(
            ReviewInputError, "must not use ollama cloud suffix"
        ):
            validate_hosted_workflow_model(
                provider_mode="openrouter",
                workflow_mode="automatic",
                model="deepseek/deepseek-v4.1-flash:cloud",
            )

    def test_openrouter_rejects_unallowlisted_vendor(self) -> None:
        with self.assertRaisesRegex(
            ReviewInputError, "not allowlisted for hosted workflows"
        ):
            validate_hosted_workflow_model(
                provider_mode="openrouter",
                workflow_mode="automatic",
                model="anthropic/claude-3.5-sonnet",
            )

    def test_openrouter_accepts_published_model(self) -> None:
        validate_hosted_workflow_model(
            provider_mode="openrouter",
            workflow_mode="automatic",
            model=DEFAULT_OPENROUTER_MODEL,
        )

    def test_hosted_openrouter_upstream_resolves_default(self) -> None:
        self.assertEqual(
            hosted_openrouter_upstream(DEFAULT_OPENROUTER_MODEL),
            DEFAULT_OPENROUTER_UPSTREAM,
        )

    def test_hosted_openrouter_upstream_rejects_unknown(self) -> None:
        with self.assertRaisesRegex(ReviewInputError, "not allowlisted"):
            hosted_openrouter_upstream("anthropic/claude-3.5-sonnet")

    def test_hosted_openrouter_upstream_rejects_mismatch(self) -> None:
        with self.assertRaisesRegex(ReviewInputError, "does not match hosted model"):
            hosted_openrouter_upstream(
                DEFAULT_OPENROUTER_MODEL,
                configured_upstream="anthropic",
            )

    def test_ollama_rejects_openrouter_slug(self) -> None:
        with self.assertRaisesRegex(
            ReviewInputError, "must not use vendor/model openrouter slug"
        ):
            validate_hosted_workflow_model(
                provider_mode="cloud-ollama",
                workflow_mode="automatic",
                model="deepseek/deepseek-v4.1-flash",
            )

    def test_model_must_not_start_with_dash(self) -> None:
        with self.assertRaisesRegex(ReviewInputError, "must not start with '-'"):
            validate_hosted_workflow_model(
                provider_mode="openrouter",
                workflow_mode="automatic",
                model="-deepseek/deepseek-v4.1-flash",
            )

    def test_default_openrouter_model_is_in_hosted_allowlist(self) -> None:
        self.assertIn(DEFAULT_OPENROUTER_MODEL, published_hosted_openrouter_models())
        self.assertIn(
            (DEFAULT_OPENROUTER_MODEL, DEFAULT_OPENROUTER_UPSTREAM),
            HOSTED_OPENROUTER_DEFAULTS,
        )


if __name__ == "__main__":
    unittest.main()

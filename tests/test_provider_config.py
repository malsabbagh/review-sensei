import unittest

from review_sensei.errors import ReviewInputError
from review_sensei.provider_config import (
    DEFAULT_OPENROUTER_MODEL,
    DEFAULT_OPENROUTER_UPSTREAM,
    HOSTED_OPENROUTER_DEFAULTS,
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
        with self.assertRaisesRegex(ReviewInputError, "vendor is not allowlisted"):
            validate_hosted_workflow_model(
                provider_mode="openrouter",
                workflow_mode="automatic",
                model="qwen3.5/local-model",
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

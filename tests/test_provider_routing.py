from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from review_sensei.errors import ProviderError, ReviewInputError
from review_sensei.providers.profiles import ProviderProfile, get_provider_profile
from review_sensei.providers.registry import ProviderSettings, default_registry
from review_sensei.providers.routing import (
    bind_stage_providers,
    resolve_stage_profile_name,
)
from review_sensei.stages import Stage


def _stage(name: str, *, profile: str | None = None) -> Stage:
    return Stage(
        name=name,
        prompt_template="{diff}",
        outputs=("summary",),
        provider_profile=profile,
    )


class ProviderRoutingTests(unittest.TestCase):
    def test_stage_inherits_run_profile_when_omitted(self) -> None:
        self.assertEqual(
            resolve_stage_profile_name(run_profile="local-private", stage_profile=None),
            "local-private",
        )

    def test_unprofiled_run_cannot_select_remote_stage_profile(self) -> None:
        with self.assertRaisesRegex(ProviderError, "cannot select a remote"):
            resolve_stage_profile_name(run_profile=None, stage_profile="fast-triage")

    def test_local_profile_cannot_fall_back_to_remote_stage(self) -> None:
        with self.assertRaisesRegex(ProviderError, "cannot fall back to a remote"):
            resolve_stage_profile_name(
                run_profile="local-private", stage_profile="fast-triage"
            )

    def test_remote_run_may_narrow_to_local_private(self) -> None:
        self.assertEqual(
            resolve_stage_profile_name(
                run_profile="fast-triage", stage_profile="local-private"
            ),
            "local-private",
        )

    def test_stage_cannot_switch_remote_adapters(self) -> None:
        with self.assertRaisesRegex(ProviderError, "cannot switch providers"):
            resolve_stage_profile_name(
                run_profile="fast-triage", stage_profile="deep-verification"
            )

    def test_stage_aliases_resolve_like_run_profile(self) -> None:
        self.assertEqual(
            resolve_stage_profile_name(run_profile=None, stage_profile="local"),
            "local-private",
        )
        self.assertEqual(
            resolve_stage_profile_name(
                run_profile="fast-triage", stage_profile="local/private"
            ),
            "local-private",
        )

    def test_bind_reuses_run_provider_when_stage_matches(self) -> None:
        stages = [_stage("Default Review Stage")]
        provider, mapping = bind_stage_providers(
            registry=default_registry(),
            settings=ProviderSettings.for_profile("local-private"),
            stages=stages,
        )
        self.assertEqual(provider.name, "ollama")
        self.assertEqual(dict(mapping), {})

    def test_bind_reuses_run_provider_when_run_profile_uses_alias(self) -> None:
        stages = [_stage("Default Review Stage")]
        provider, mapping = bind_stage_providers(
            registry=default_registry(),
            settings=ProviderSettings.for_profile("local"),
            stages=stages,
        )
        self.assertEqual(provider.name, "ollama")
        self.assertEqual(dict(mapping), {})

    def test_bind_does_not_forward_remote_credential_when_narrowing(self) -> None:
        stages = [_stage("Summary", profile="local-private")]
        provider, mapping = bind_stage_providers(
            registry=default_registry(),
            settings=ProviderSettings.for_profile("fast-triage", api_key="secret"),
            stages=stages,
        )
        self.assertEqual(provider.name, "openai-compatible")
        self.assertEqual(getattr(provider, "api_key", None), "secret")
        local = mapping["Summary"]
        self.assertEqual(local.name, "ollama")
        self.assertIsNot(local, provider)
        self.assertIsNone(getattr(local, "api_key", "missing"))

    def test_fixture_run_rejects_stage_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "ok.json"
            path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ProviderError, "fixture"):
                bind_stage_providers(
                    registry=default_registry(),
                    settings=ProviderSettings(name="fixture", fixture_response=path),
                    stages=[_stage("Summary", profile="local-private")],
                )

    def test_bind_selects_declared_per_stage_model_without_endpoint_change(
        self,
    ) -> None:
        profile = replace(
            get_provider_profile("fast-triage"),
            stage_models=(("Comments", "gpt-4o"),),
        )
        stages = [_stage("Comments"), _stage("Summary")]

        def profile_lookup(name: str) -> ProviderProfile:
            selected = get_provider_profile(name)
            if selected.name == "fast-triage":
                return profile
            return selected

        with patch(
            "review_sensei.providers.routing.get_provider_profile",
            side_effect=profile_lookup,
        ), patch(
            "review_sensei.providers.registry.get_provider_profile",
            side_effect=profile_lookup,
        ):
            run_provider, mapping = bind_stage_providers(
                registry=default_registry(),
                settings=ProviderSettings.for_profile("fast-triage", api_key="secret"),
                stages=stages,
            )
        self.assertEqual(run_provider.model, "gpt-4o-mini")
        self.assertEqual(run_provider.base_url, "https://api.openai.com/v1")
        self.assertEqual(mapping["Comments"].model, "gpt-4o")
        self.assertNotIn("Summary", mapping)

    def test_profile_rejects_undeclared_stage_model(self) -> None:
        with self.assertRaisesRegex(ProviderError, "model cannot be overridden"):
            default_registry().create(
                ProviderSettings(
                    name="openai-compatible",
                    profile="fast-triage",
                    model="attacker-model",
                    api_key="secret",
                )
            )

    def test_custom_profile_stage_models_must_be_unique(self) -> None:
        with self.assertRaisesRegex(ValueError, "unique"):
            ProviderProfile(
                name="broken",
                provider="ollama",
                model="qwen3.5:4b",
                base_url="http://127.0.0.1:11434/api",
                endpoint_scope="local",
                timeout_seconds=1,
                max_output_tokens=1,
                stage_models=(("Summary", "one"), ("Summary", "two")),
            )

    def test_profiles_declare_json_output_and_no_fallback(self) -> None:
        for name in ("local-private", "fast-triage", "deep-verification"):
            profile = get_provider_profile(name)
            self.assertEqual(profile.structured_output, "json_object")
            self.assertEqual(profile.permitted_fallback, "none")

    def test_installed_workflows_do_not_pass_profile_or_custom_provider_url(
        self,
    ) -> None:
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github" / "workflows" / "review-sensei-run.yml").read_text(
            encoding="utf-8"
        )
        example = (
            root / "examples" / "github-actions" / "review-sensei-review.yml"
        ).read_text(encoding="utf-8")
        for text in (workflow, example):
            self.assertNotIn("--profile", text)
            self.assertNotIn("OPENAI_API_KEY", text)
            self.assertNotIn("api.openai.com", text)


class StageProviderProfileTests(unittest.TestCase):
    def test_stage_json_accepts_canonical_profile(self) -> None:
        stage = Stage.from_dict(
            {
                "name": "Summary",
                "prompt_template": "{diff}",
                "outputs": ["summary"],
                "provider_profile": "local-private",
            }
        )
        self.assertEqual(stage.provider_profile, "local-private")

    def test_stage_json_rejects_profile_alias_and_unknown_name(self) -> None:
        with self.assertRaises(ReviewInputError):
            Stage.from_dict(
                {
                    "name": "Summary",
                    "prompt_template": "{diff}",
                    "outputs": ["summary"],
                    "provider_profile": "local",
                }
            )
        with self.assertRaises(ReviewInputError) as raised:
            Stage.from_dict(
                {
                    "name": "Summary",
                    "prompt_template": "{diff}",
                    "outputs": ["summary"],
                    "provider_profile": "not-a-profile",
                }
            )
        self.assertEqual(str(raised.exception), "stage provider_profile is unknown")
        self.assertNotIn("not-a-profile", str(raised.exception))


if __name__ == "__main__":
    unittest.main()

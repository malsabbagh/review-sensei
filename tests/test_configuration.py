import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from review_sensei.cli import main
from review_sensei.configuration import (
    BACKEND_DEFAULTS,
    MAX_CONFIG_BYTES,
    SUPPORTED_SCHEMA_VERSION,
    ConfigurationError,
    customization_directories,
    default_configuration,
    documented_backend_names,
    load_configuration,
    parse_configuration_text,
    render_configuration,
    resolve_inference,
    retired_environment_settings,
)

SOURCE = ".reviewsensei.yml"
MINIMAL = """schema: 1

inference:
  backend: local-ollama
  model: qwen3.5:4b
"""
GITHUB_CONFIG = """schema: 1

inference:
  backend: cloud-ollama
  model: deepseek-v4.1-flash:cloud

github:
  writes: true
  reviews: auto-approve
"""


def parse(text, source=SOURCE, root=None):
    return parse_configuration_text(text, source=source, root=root)


class ConfigurationDocumentTests(unittest.TestCase):
    def test_minimal_local_configuration_documents_defaults(self):
        configuration = parse(MINIMAL)
        self.assertEqual(configuration.schema, SUPPORTED_SCHEMA_VERSION)
        self.assertEqual(configuration.inference.backend, "local-ollama")
        self.assertEqual(configuration.inference.model, "qwen3.5:4b")
        self.assertTrue(configuration.github.automatic_reviews)
        self.assertFalse(configuration.github.writes)
        self.assertEqual(configuration.github.reviews, "auto-approve")
        self.assertTrue(configuration.github.mentions)
        self.assertEqual(configuration.github.learning, "disabled")
        self.assertEqual(configuration.github.artifacts, "none")
        self.assertIn("inference.backend", configuration.declared)
        self.assertEqual(configuration.line_of("inference.backend"), 4)
        self.assertFalse(configuration.is_declared("github.writes"))

    def test_schema_is_required_and_versioned(self):
        with self.assertRaises(ConfigurationError) as missing:
            parse("inference:\n  backend: local-ollama\n")
        self.assertIn("schema: 1", str(missing.exception))
        with self.assertRaises(ConfigurationError) as unsupported:
            parse("schema: 2\n")
        self.assertIn("schema 2 is not supported", str(unsupported.exception))
        with self.assertRaises(ConfigurationError) as wrong_type:
            parse('schema: "1"\n')
        self.assertIn("schema must be the integer version", str(wrong_type.exception))

    def test_unknown_and_retired_fields_are_actionable(self):
        with self.assertRaises(ConfigurationError) as unknown:
            parse("schema: 1\nreviews: auto-approve\n")
        message = str(unknown.exception)
        self.assertIn(f"{SOURCE}:2: reviews:", message)
        self.assertIn("not supported", message)
        with self.assertRaises(ConfigurationError) as retired:
            parse("schema: 1\nprovider: ollama\n")
        self.assertIn("was retired; use inference.backend", str(retired.exception))
        with self.assertRaises(ConfigurationError) as retired_github:
            parse("schema: 1\ngithub:\n  auto_review: true\n")
        message = str(retired_github.exception)
        self.assertIn("github.auto_review", message)
        self.assertIn("use github.automatic_reviews", message)

    def test_empty_valued_keys_are_reported_as_omitted(self):
        configuration = parse(
            "schema: 1\ninference:\n  backend:\n  model: qwen3.5:4b\n"
        )
        self.assertEqual(configuration.inference.backend, "local-ollama")
        self.assertFalse(configuration.is_declared("inference.backend"))
        self.assertTrue(configuration.is_declared("inference.model"))

    def test_an_empty_valued_key_does_not_swallow_the_next_section(self):
        with self.assertRaises(ConfigurationError) as error:
            parse(
                "schema: 1\nadvanced:\n  requests:\n    headers:\n"
                "  egress:\n    allow_data_egress: true\n"
            )
        message = str(error.exception)
        self.assertIn("advanced.requests", message)
        self.assertIn("not supported", message)
        self.assertNotIn("duplicate", message)

    def test_duplicate_keys_are_rejected_with_a_field_path(self):
        text = (
            "schema: 1\ninference:\n  backend: local-ollama\n  backend: cloud-ollama\n"
        )
        with self.assertRaises(ConfigurationError) as error:
            parse(text)
        message = str(error.exception)
        self.assertIn(f"{SOURCE}:4: inference.backend:", message)
        self.assertIn("duplicate field 'backend'", message)

    def test_strict_types_reject_quoted_booleans_and_wrong_shapes(self):
        with self.assertRaises(ConfigurationError) as quoted:
            parse('schema: 1\ngithub:\n  writes: "true"\n')
        message = str(quoted.exception)
        self.assertIn("github.writes", message)
        self.assertIn("must be true or false", message)
        with self.assertRaises(ConfigurationError) as section:
            parse("schema: 1\ngithub: true\n")
        self.assertIn("must be a mapping of fields", str(section.exception))
        with self.assertRaises(ConfigurationError) as sequence:
            parse("schema: 1\ngithub:\n  - writes\n")
        self.assertIn("must be a mapping of fields", str(sequence.exception))

    def test_enums_reject_unknown_policies(self):
        for field, value in (
            ("reviews", "strict"),
            ("learning", "always"),
            ("artifacts", "screenshots"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(ConfigurationError) as error:
                    parse(f"schema: 1\ngithub:\n  {field}: {value}\n")
                message = str(error.exception)
                self.assertIn(f"github.{field}", message)
                self.assertIn("is not supported; use one of", message)

    def test_unsafe_yaml_constructs_are_rejected(self):
        for text, expected in (
            ("schema: 1\ninference: !!python/object/apply:os.system ['ls']\n", "tags"),
            ("schema: 1\ndefaults: &defaults\n  backend: local-ollama\n", "anchors"),
            ("schema: 1\ninference: *defaults\n", "aliases"),
            ("schema: 1\ninference: {backend: local-ollama}\n", "flow collections"),
            ("schema: 1\nmodel: |\n  local-ollama\n", "block scalars"),
            ("schema: 1\n---\nschema: 1\n", "multiple YAML documents"),
            ("schema: 1\n\tinference:\n", "indentation must use spaces"),
        ):
            with self.subTest(text=text):
                with self.assertRaises(ConfigurationError) as error:
                    parse(text)
                self.assertIn(expected, str(error.exception))

    def test_environment_interpolation_is_literal_text(self):
        configuration = parse(
            "schema: 1\nadvanced:\n  context:\n    symbol_context:\n"
            "      allowed_paths:\n        - '${HOME}/src'\n"
        )
        self.assertEqual(
            configuration.advanced.context.symbol_context.allowed_paths,
            ("${HOME}/src",),
        )
        with self.assertRaises(ConfigurationError) as error:
            resolve_inference(
                parse("schema: 1\ninference:\n  model: ${OLLAMA_MODEL}\n")
            )
        self.assertIn("not valid for backend 'local-ollama'", str(error.exception))

    def test_document_bounds_are_enforced(self):
        oversized = "schema: 1\ninference:\n  model: " + "a" * MAX_CONFIG_BYTES + "\n"
        with self.assertRaises(ConfigurationError) as error:
            parse(oversized)
        self.assertIn("exceeds", str(error.exception))
        with self.assertRaises(ConfigurationError) as empty:
            parse("")
        self.assertIn("configuration is empty", str(empty.exception))
        nested = "schema: 1\n"
        indent = ""
        for depth in range(10):
            nested += f"{indent}key{depth}:\n"
            indent += "  "
        with self.assertRaises(ConfigurationError) as deep:
            parse(nested)
        self.assertTrue(
            "too deep" in str(deep.exception) or "not supported" in str(deep.exception)
        )

    def test_advanced_schema_is_typed_and_bounded(self):
        configuration = parse(
            "schema: 1\nadvanced:\n"
            "  endpoint:\n"
            "    base_url: http://192.168.1.10:11434/api\n"
            "    allow_custom_endpoint: true\n"
            "  context:\n"
            "    symbol_context:\n"
            "      enabled: true\n"
            "      allowed_paths:\n"
            "        - src/**\n"
            "      max_files: 4\n"
            "  large_changes:\n"
            "    orchestrate: true\n"
            "  resources:\n"
            "    timeout_seconds: 120\n"
        )
        advanced = configuration.advanced
        self.assertEqual(advanced.endpoint.base_url, "http://192.168.1.10:11434/api")
        self.assertTrue(advanced.endpoint.allow_custom_endpoint)
        self.assertTrue(advanced.context.symbol_context.enabled)
        self.assertEqual(advanced.context.symbol_context.allowed_paths, ("src/**",))
        self.assertEqual(advanced.context.symbol_context.max_files, 4)
        self.assertEqual(advanced.context.symbol_context.max_depth, 1)
        self.assertTrue(advanced.large_changes.orchestrate)
        self.assertEqual(advanced.resources.timeout_seconds, 120.0)
        self.assertFalse(advanced.egress.allow_data_egress)

    def test_advanced_hard_ceilings_are_not_configurable(self):
        with self.assertRaises(ConfigurationError) as files:
            parse(
                "schema: 1\nadvanced:\n  context:\n    symbol_context:\n      max_files: 99\n"
            )
        self.assertIn("packaged ceiling", str(files.exception))
        with self.assertRaises(ConfigurationError) as timeout:
            parse("schema: 1\nadvanced:\n  resources:\n    timeout_seconds: 99999\n")
        self.assertIn("packaged ceiling", str(timeout.exception))
        with self.assertRaises(ConfigurationError) as unknown:
            parse("schema: 1\nadvanced:\n  requests:\n    headers: value\n")
        self.assertIn("advanced.requests", str(unknown.exception))


class ConfigurationResolutionTests(unittest.TestCase):
    def test_backend_registry_is_documented_and_complete(self):
        self.assertEqual(
            documented_backend_names(),
            ("local-ollama", "cloud-ollama", "openrouter", "openai-compatible"),
        )
        for name in documented_backend_names():
            defaults = BACKEND_DEFAULTS[name]
            self.assertEqual(defaults.backend, name)
            self.assertTrue(defaults.model)
            self.assertFalse(defaults.internal)

    def test_no_override_uses_the_configuration_file(self):
        resolved = resolve_inference(parse(GITHUB_CONFIG), environ={})
        self.assertEqual(resolved.backend, "cloud-ollama")
        self.assertEqual(resolved.model, "deepseek-v4.1-flash:cloud")
        self.assertEqual(resolved.base_url, "https://ollama.com/api")
        self.assertEqual(resolved.credential_env, "OLLAMA_API_KEY")
        self.assertEqual(resolved.runner_kind, "hosted")
        self.assertEqual(resolved.inference_location, "remote")
        self.assertEqual(resolved.source_of("backend").source, "configuration file")

    def test_packaged_defaults_apply_without_a_configuration_file(self):
        resolved = resolve_inference(default_configuration(), environ={})
        defaults = BACKEND_DEFAULTS["local-ollama"]
        self.assertEqual(resolved.backend, "local-ollama")
        self.assertEqual(resolved.model, defaults.model)
        self.assertEqual(resolved.base_url, defaults.base_url)
        self.assertEqual(resolved.runner_kind, "local")
        self.assertEqual(resolved.inference_location, "local")
        self.assertEqual(resolved.source_of("model").source, "packaged default")

    def test_model_only_override_keeps_the_configured_backend(self):
        configuration = parse(MINIMAL)
        resolved = resolve_inference(
            configuration, environ={"REVIEWSENSEI_MODEL": "qwen3.6:8b"}
        )
        self.assertEqual(resolved.backend, "local-ollama")
        self.assertEqual(resolved.model, "qwen3.6:8b")
        self.assertEqual(resolved.source_of("model").detail, "REVIEWSENSEI_MODEL")

    def test_provider_only_override_uses_the_backend_default_model(self):
        configuration = parse("schema: 1\ninference:\n  backend: local-ollama\n")
        resolved = resolve_inference(
            configuration, environ={"REVIEWSENSEI_PROVIDER": "cloud-ollama"}
        )
        self.assertEqual(resolved.backend, "cloud-ollama")
        self.assertEqual(resolved.model, BACKEND_DEFAULTS["cloud-ollama"].model)
        self.assertEqual(resolved.base_url, "https://ollama.com/api")

    def test_provider_only_override_keeps_a_model_valid_for_the_new_backend(self):
        resolved = resolve_inference(
            parse(MINIMAL), environ={"REVIEWSENSEI_PROVIDER": "cloud-ollama"}
        )
        self.assertEqual(resolved.backend, "cloud-ollama")
        self.assertEqual(resolved.model, "qwen3.5:4b")

    def test_provider_only_override_explains_an_invalid_model(self):
        with self.assertRaises(ConfigurationError) as error:
            resolve_inference(
                parse(MINIMAL), environ={"REVIEWSENSEI_PROVIDER": "openrouter"}
            )
        message = str(error.exception)
        self.assertIn("inference.model", message)
        self.assertIn("not valid for backend 'openrouter'", message)
        self.assertIn("vendor/model", message)
        self.assertIn("REVIEWSENSEI_MODEL", message)

    def test_provider_and_model_override_together(self):
        resolved = resolve_inference(
            parse(MINIMAL),
            environ={
                "REVIEWSENSEI_PROVIDER": "openrouter",
                "REVIEWSENSEI_MODEL": "deepseek/deepseek-v4.1-flash",
            },
        )
        self.assertEqual(resolved.backend, "openrouter")
        self.assertEqual(resolved.model, "deepseek/deepseek-v4.1-flash")
        self.assertEqual(resolved.upstream_provider, "morph")
        self.assertEqual(resolved.credential_env, "OPENROUTER_API_KEY")

    def test_invocation_overrides_beat_the_environment(self):
        resolved = resolve_inference(
            parse(MINIMAL),
            cli_provider="openrouter",
            cli_model="deepseek/deepseek-v4.1-flash",
            environ={
                "REVIEWSENSEI_PROVIDER": "local-ollama",
                "REVIEWSENSEI_MODEL": "qwen3.5:4b",
            },
        )
        self.assertEqual(resolved.backend, "openrouter")
        self.assertEqual(resolved.model, "deepseek/deepseek-v4.1-flash")
        self.assertEqual(resolved.source_of("backend").source, "command line")

    def test_whitespace_only_overrides_are_not_overrides(self):
        resolved = resolve_inference(
            parse(MINIMAL),
            environ={"REVIEWSENSEI_PROVIDER": "   ", "REVIEWSENSEI_MODEL": "\t"},
        )
        self.assertEqual(resolved.backend, "local-ollama")
        self.assertEqual(resolved.model, "qwen3.5:4b")
        self.assertEqual(resolved.source_of("backend").source, "configuration file")

    def test_invalid_model_override_for_the_selected_backend_fails(self):
        with self.assertRaises(ConfigurationError) as error:
            resolve_inference(
                parse(
                    "schema: 1\ninference:\n  backend: openrouter\n  model: deepseek/deepseek-v4.1-flash\n"
                ),
                environ={"REVIEWSENSEI_MODEL": "qwen3.5:4b"},
            )
        message = str(error.exception)
        self.assertIn("REVIEWSENSEI_MODEL", message)
        self.assertIn("not valid for backend 'openrouter'", message)

    def test_legacy_synonyms_get_actionable_replacement_errors(self):
        with self.assertRaises(ConfigurationError) as error:
            resolve_inference(
                parse(MINIMAL), environ={"REVIEWSENSEI_PROVIDER": "cloud"}
            )
        message = str(error.exception)
        self.assertIn("REVIEWSENSEI_PROVIDER", message)
        self.assertIn("not a canonical backend; use 'cloud-ollama'", message)
        with self.assertRaises(ConfigurationError) as provider_mode:
            parse("schema: 1\ninference:\n  provider_mode: local\n")
        self.assertIn("was retired", str(provider_mode.exception))

    def test_internal_fixture_backend_is_not_selectable_from_yaml(self):
        with self.assertRaises(ConfigurationError) as error:
            parse("schema: 1\ninference:\n  backend: fixture\n")
        self.assertIn("not a canonical backend", str(error.exception))
        resolved = resolve_inference(parse(MINIMAL), cli_provider="fixture", environ={})
        self.assertEqual(resolved.backend, "fixture")
        self.assertEqual(resolved.model, "qwen3.5:4b")
        self.assertEqual(resolved.source_of("backend").source, "command line")
        self.assertEqual(resolved.inference_location, "local")

    def test_retired_environment_settings_fail_with_replacement(self):
        with self.assertRaises(ConfigurationError) as error:
            resolve_inference(parse(MINIMAL), environ={"OLLAMA_MODEL": "qwen3.5:4b"})
        message = str(error.exception)
        self.assertIn("OLLAMA_MODEL is retired", message)
        self.assertIn("REVIEWSENSEI_MODEL", message)

    def test_credential_reference_never_reuses_another_backend_key(self):
        resolved = resolve_inference(
            parse(
                "schema: 1\ninference:\n  backend: openrouter\n"
                "  model: deepseek/deepseek-v4.1-flash\n"
            ),
            environ={"OLLAMA_API_KEY": "ollama-secret"},
        )
        self.assertEqual(resolved.credential_env, "OPENROUTER_API_KEY")
        self.assertFalse(resolved.credential_present)
        self.assertTrue(resolved.credential_required)
        self.assertNotIn(
            "ollama-secret", render_configuration(parse(MINIMAL), resolved)
        )

    def test_custom_endpoint_requires_explicit_opt_in(self):
        with self.assertRaises(ConfigurationError) as error:
            resolve_inference(
                parse(
                    "schema: 1\nadvanced:\n  endpoint:\n"
                    "    base_url: http://127.0.0.1:11434/api\n"
                ),
                environ={},
            )
        self.assertIn("allow_custom_endpoint", str(error.exception))
        resolved = resolve_inference(
            parse(
                "schema: 1\nadvanced:\n  endpoint:\n"
                "    base_url: http://127.0.0.1:11434/api\n"
                "    allow_custom_endpoint: true\n"
            ),
            environ={},
        )
        self.assertEqual(resolved.base_url, "http://127.0.0.1:11434/api")
        self.assertEqual(resolved.inference_location, "local")

    def test_plaintext_transport_is_limited_to_local_hosts(self):
        with self.assertRaises(ConfigurationError) as error:
            resolve_inference(
                parse(
                    "schema: 1\nadvanced:\n  endpoint:\n"
                    "    base_url: http://reviewer.example.com/api\n"
                    "    allow_custom_endpoint: true\n"
                ),
                environ={},
            )
        self.assertIn("must use https", str(error.exception))
        resolved = resolve_inference(
            parse(
                "schema: 1\nadvanced:\n  endpoint:\n"
                "    base_url: http://gpu-box:11434/api\n"
                "    allow_custom_endpoint: true\n"
            ),
            environ={},
        )
        self.assertEqual(resolved.inference_location, "remote")

    def test_openrouter_keeps_its_allowlisted_endpoint(self):
        with self.assertRaises(ConfigurationError) as error:
            resolve_inference(
                parse(
                    "schema: 1\ninference:\n  backend: openrouter\n"
                    "  model: deepseek/deepseek-v4.1-flash\n"
                    "advanced:\n  endpoint:\n"
                    "    base_url: https://gateway.example.com/v1\n"
                    "    allow_custom_endpoint: true\n"
                ),
                environ={},
            )
        self.assertIn("allowlisted packaged endpoint", str(error.exception))
        resolved = resolve_inference(
            parse(
                "schema: 1\ninference:\n  backend: openrouter\n"
                "  model: deepseek/deepseek-v4.1-flash\n"
            ),
            environ={},
        )
        self.assertEqual(resolved.base_url, "https://openrouter.ai/api/v1")

    def test_credential_reference_is_only_configurable_for_openai_compatible(self):
        with self.assertRaises(ConfigurationError) as error:
            resolve_inference(
                parse(
                    "schema: 1\nadvanced:\n  endpoint:\n    credential_env: MY_KEY\n"
                ),
                environ={},
            )
        self.assertIn(
            "only supported for backend 'openai-compatible'", str(error.exception)
        )
        resolved = resolve_inference(
            parse(
                "schema: 1\ninference:\n  backend: openai-compatible\n"
                "advanced:\n  endpoint:\n    credential_env: MY_GATEWAY_KEY\n"
            ),
            environ={"MY_GATEWAY_KEY": "secret"},
        )
        self.assertEqual(resolved.credential_env, "MY_GATEWAY_KEY")
        self.assertTrue(resolved.credential_present)
        self.assertFalse(resolved.credential_required is None)

    def test_routing_restrictions_apply_to_openrouter_only(self):
        with self.assertRaises(ConfigurationError) as error:
            resolve_inference(
                parse(
                    "schema: 1\nadvanced:\n  routing:\n    upstream_provider: morph\n"
                ),
                environ={},
            )
        self.assertIn("applies only to backend 'openrouter'", str(error.exception))
        with self.assertRaises(ConfigurationError) as mismatch:
            resolve_inference(
                parse(
                    "schema: 1\ninference:\n  backend: openrouter\n"
                    "  model: deepseek/deepseek-v4.1-flash\n"
                    "advanced:\n  routing:\n    upstream_provider: anthropic\n"
                ),
                environ={},
            )
        self.assertIn("routing restrictions cannot be relaxed", str(mismatch.exception))
        resolved = resolve_inference(
            parse(
                "schema: 1\ninference:\n  backend: openrouter\n"
                "  model: meta-llama/llama-4-70b\n"
                "advanced:\n  routing:\n    upstream_provider: together\n"
            ),
            environ={},
        )
        self.assertEqual(resolved.upstream_provider, "together")

    def test_explicit_egress_and_resource_opt_ins_stay_explicit(self):
        resolved = resolve_inference(
            parse(
                "schema: 1\nadvanced:\n  egress:\n    allow_data_egress: true\n"
                "  resources:\n    timeout_seconds: 30\n"
                "    max_provider_calls: 4\n"
            ),
            environ={},
        )
        configuration = parse(
            "schema: 1\nadvanced:\n  egress:\n    allow_data_egress: true\n"
            "  resources:\n    timeout_seconds: 30\n"
            "    max_provider_calls: 4\n"
        )
        self.assertTrue(configuration.advanced.egress.allow_data_egress)
        self.assertEqual(configuration.advanced.resources.max_provider_calls, 4)
        self.assertEqual(resolved.timeout_seconds, 30.0)
        default_resolved = resolve_inference(parse(MINIMAL), environ={})
        self.assertEqual(default_resolved.timeout_seconds, 900.0)
        self.assertFalse(
            parse(MINIMAL).advanced.egress.allow_data_egress,
            "egress is never enabled implicitly",
        )

    def test_retired_environment_settings_are_audited(self):
        environ = {"AUTO_APPROVE": "true", "REVIEWSENSEI_STAGES_DIR": "stages"}
        self.assertEqual(
            retired_environment_settings(environ),
            ("AUTO_APPROVE", "REVIEWSENSEI_STAGES_DIR"),
        )
        self.assertEqual(
            retired_environment_settings(environ, include_provider_overrides=False),
            ("AUTO_APPROVE", "REVIEWSENSEI_STAGES_DIR"),
        )
        self.assertEqual(retired_environment_settings({}), ())


class ConfigurationCustomizationTests(unittest.TestCase):
    def test_missing_customization_uses_packaged_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            configuration = parse(MINIMAL, root=Path(tmp))
            directories = customization_directories(configuration)
            self.assertIsNone(directories.stages)
            self.assertIsNone(directories.categories)
            self.assertEqual(directories.selected(), ())

    def test_present_customization_directories_are_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".reviewsensei" / "stages").mkdir(parents=True)
            (root / ".reviewsensei" / "stages" / "default.json").write_text(
                "{}", encoding="utf-8"
            )
            configuration = parse(MINIMAL, root=root)
            directories = customization_directories(configuration)
            self.assertEqual(directories.stages, root / ".reviewsensei" / "stages")
            self.assertIsNone(directories.categories)
            self.assertEqual(
                directories.selected(),
                (("stages", root / ".reviewsensei" / "stages"),),
            )

    def test_present_but_invalid_customization_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            categories = root / ".reviewsensei" / "categories"
            categories.mkdir(parents=True)
            with self.assertRaises(ConfigurationError) as error:
                customization_directories(parse(MINIMAL, root=root))
            self.assertIn("contains no configuration", str(error.exception))
            (categories / "notes.txt").write_text("x", encoding="utf-8")
            with self.assertRaises(ConfigurationError) as suffix:
                customization_directories(parse(MINIMAL, root=root))
            self.assertIn("only .json files are read", str(suffix.exception))
            (categories / "notes.txt").unlink()
            categories.rmdir()
            (root / ".reviewsensei" / "categories").write_text("[]", encoding="utf-8")
            with self.assertRaises(ConfigurationError) as shape:
                customization_directories(parse(MINIMAL, root=root))
            self.assertIn("must be a directory", str(shape.exception))

    def test_symlinked_customization_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "elsewhere"
            target.mkdir()
            (target / "stages.json").write_text("{}", encoding="utf-8")
            (root / ".reviewsensei").mkdir()
            os.symlink(target, root / ".reviewsensei" / "stages")
            with self.assertRaises(ConfigurationError) as error:
                customization_directories(parse(MINIMAL, root=root))
            self.assertIn("must not be a symbolic link", str(error.exception))


class ConfigurationLoadingTests(unittest.TestCase):
    def test_default_filename_is_discovered_relative_to_the_working_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous = Path.cwd()
            os.chdir(tmp)
            try:
                self.assertIsNone(load_configuration().source)
                Path(".reviewsensei.yml").write_text(GITHUB_CONFIG, encoding="utf-8")
                configuration = load_configuration()
                self.assertEqual(configuration.source, ".reviewsensei.yml")
                self.assertEqual(configuration.root, Path(tmp).resolve())
                self.assertEqual(configuration.inference.backend, "cloud-ollama")
            finally:
                os.chdir(previous)

    def test_selected_file_must_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "custom.yml"
            with self.assertRaises(ConfigurationError) as error:
                load_configuration(missing)
            self.assertIn("configuration file not found", str(error.exception))

    def test_oversized_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "big.yml"
            path.write_text("schema: 1\n" + "# pad\n" * 20000, encoding="utf-8")
            with self.assertRaises(Exception) as error:
                load_configuration(path)
            self.assertIn("exceeds", str(error.exception))


class ConfigurationRenderingTests(unittest.TestCase):
    def test_explain_shows_provenance_and_omitted_defaults(self):
        configuration = parse(GITHUB_CONFIG)
        resolved = resolve_inference(
            configuration, environ={"REVIEWSENSEI_MODEL": "deepseek-v4.1-flash:cloud"}
        )
        output = render_configuration(configuration, resolved, explain=True)
        self.assertIn("effective inference:", output)
        self.assertIn(
            "backend: cloud-ollama [.reviewsensei.yml inference.backend]", output
        )
        self.assertIn(
            "model: deepseek-v4.1-flash:cloud [REVIEWSENSEI_MODEL]",
            output,
        )
        self.assertIn("omitted fields and their sources:", output)
        self.assertIn("github.mentions: true [packaged default]", output)
        self.assertIn("github.automatic_reviews: true [packaged default]", output)
        self.assertIn("customization: packaged defaults", output)
        self.assertNotIn("credential_env", output)

    def test_plain_show_hides_provenance(self):
        configuration = parse(GITHUB_CONFIG)
        resolved = resolve_inference(configuration, environ={})
        output = render_configuration(configuration, resolved, explain=False)
        self.assertIn("effective inference:", output)
        self.assertNotIn("omitted fields and their sources:", output)
        self.assertNotIn("[packaged default", output)

    def test_explain_reports_declared_advanced_fields(self):
        configuration = parse(
            "schema: 1\nadvanced:\n  egress:\n    allow_data_egress: true\n"
            "  large_changes:\n    orchestrate: true\n"
        )
        resolved = resolve_inference(configuration, environ={})
        output = render_configuration(configuration, resolved, explain=True)
        self.assertIn("advanced:", output)
        self.assertIn("egress.allow_data_egress: true", output)
        self.assertIn("large_changes.orchestrate: true", output)


class ConfigurationCliTests(unittest.TestCase):
    def _write(self, directory, text):
        path = Path(directory) / ".reviewsensei.yml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_config_validate_accepts_a_minimal_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, MINIMAL)
            stdout = io.StringIO()
            with patch.dict("os.environ", {}, clear=True):
                with redirect_stderr(io.StringIO()):
                    with patch("sys.stdout", stdout):
                        status = main(["config", "validate", "--config", str(path)])
            self.assertEqual(status, 0)
            self.assertIn("configuration valid", stdout.getvalue())
            self.assertIn("backend local-ollama", stdout.getvalue())

    def test_config_validate_reports_field_paths_and_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "schema: 1\ngithub:\n  writes: yes please\n")
            stderr = io.StringIO()
            with patch.dict("os.environ", {}, clear=True):
                with redirect_stderr(stderr):
                    with patch("sys.stdout", io.StringIO()):
                        status = main(["config", "validate", "--config", str(path)])
            self.assertEqual(status, 2)
            message = stderr.getvalue()
            self.assertIn("github.writes", message)
            self.assertIn("must be true or false", message)

    def test_config_validate_rejects_invalid_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, MINIMAL)
            stderr = io.StringIO()
            with patch.dict("os.environ", {"REVIEWSENSEI_PROVIDER": "openrouter"}):
                with redirect_stderr(stderr):
                    with patch("sys.stdout", io.StringIO()):
                        status = main(["config", "validate", "--config", str(path)])
            self.assertEqual(status, 2)
            self.assertIn("not valid for backend 'openrouter'", stderr.getvalue())

    def test_config_validate_rejects_retired_environment_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, MINIMAL)
            stderr = io.StringIO()
            with patch.dict("os.environ", {"AUTO_APPROVE": "true"}):
                with redirect_stderr(stderr):
                    with patch("sys.stdout", io.StringIO()):
                        status = main(["config", "validate", "--config", str(path)])
            self.assertEqual(status, 2)
            self.assertIn("AUTO_APPROVE", stderr.getvalue())
            self.assertIn("github.reviews", stderr.getvalue())

    def test_config_show_explain_prints_defaults_and_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, MINIMAL)
            stdout = io.StringIO()
            with patch.dict("os.environ", {}, clear=True):
                with redirect_stderr(io.StringIO()):
                    with patch("sys.stdout", stdout):
                        status = main(
                            ["config", "show", "--explain", "--config", str(path)]
                        )
            self.assertEqual(status, 0)
            output = stdout.getvalue()
            self.assertIn("effective inference:", output)
            self.assertIn("omitted fields and their sources:", output)
            self.assertIn("local-ollama", output)

    def test_missing_configuration_file_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            stderr = io.StringIO()
            with patch.dict("os.environ", {}, clear=True):
                with redirect_stderr(stderr):
                    with patch("sys.stdout", io.StringIO()):
                        status = main(
                            ["config", "show", "--config", str(Path(tmp) / "none.yml")]
                        )
            self.assertEqual(status, 2)
            self.assertIn("configuration file not found", stderr.getvalue())

    def test_config_commands_call_no_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, MINIMAL)
            with patch.dict("os.environ", {}, clear=True):
                with redirect_stderr(io.StringIO()):
                    with patch("sys.stdout", io.StringIO()):
                        with patch(
                            "review_sensei.providers.ollama.OllamaProvider.complete",
                            side_effect=AssertionError("inference is not allowed"),
                        ):
                            self.assertEqual(
                                main(["config", "validate", "--config", str(path)]), 0
                            )
                            self.assertEqual(
                                main(["config", "show", "--config", str(path)]), 0
                            )


class ConfigurationScalarTests(unittest.TestCase):
    def test_quoted_scalars_decode_supported_escapes(self):
        configuration = parse('schema: 1\ninference:\n  model: "qwen3\\u002e5:4b"\n')
        self.assertEqual(configuration.inference.model, "qwen3.5:4b")
        resolved = resolve_inference(configuration, environ={})
        self.assertEqual(resolved.model, "qwen3.5:4b")

    def test_unsupported_escapes_are_rejected(self):
        for text, expected in (
            ('schema: 1\ninference:\n  model: "a\\qb"\n', "is not supported"),
            ('schema: 1\ninference:\n  model: "a\\u00zz"\n', "invalid \\u escape"),
            ('schema: 1\ninference:\n  model: "unterminated\n', "unterminated"),
            ("schema: 1\ninference:\n  model: 'single\n", "unterminated"),
        ):
            with self.subTest(text=text):
                with self.assertRaises(ConfigurationError) as error:
                    parse(text)
                self.assertIn(expected, str(error.exception))

    def test_control_characters_are_rejected(self):
        with self.assertRaises(ConfigurationError) as error:
            parse('schema: 1\ninference:\n  model: "a\\u0007b"\n')
        self.assertIn("control character", str(error.exception))

    def test_plain_scalars_keep_strict_types(self):
        with self.assertRaises(ConfigurationError) as bool_as_int:
            parse("schema: 1\ngithub:\n  writes: 1\n")
        self.assertIn("must be true or false", str(bool_as_int.exception))
        with self.assertRaises(ConfigurationError) as float_schema:
            parse("schema: 1.0\n")
        self.assertIn("must be the integer version", str(float_schema.exception))
        with self.assertRaises(ConfigurationError) as number_field:
            parse("schema: 1\ninference:\n  backend: 7\n")
        self.assertIn("inference.backend", str(number_field.exception))
        self.assertIn("must be a string", str(number_field.exception))
        null_field = parse("schema: 1\ninference:\n  backend: null\n")
        self.assertEqual(null_field.inference.backend, "local-ollama")
        self.assertFalse(null_field.is_declared("inference.backend"))
        with self.assertRaises(ConfigurationError) as negative:
            parse("schema: 1\nadvanced:\n  resources:\n    max_provider_calls: -1\n")
        self.assertIn("positive integer", str(negative.exception))
        with self.assertRaises(ConfigurationError) as zero:
            parse("schema: 1\nadvanced:\n  resources:\n    max_provider_calls: 0\n")
        self.assertIn("positive integer", str(zero.exception))

    def test_sequences_reject_nested_structures(self):
        with self.assertRaises(ConfigurationError) as error:
            parse(
                "schema: 1\nadvanced:\n  context:\n    symbol_context:\n"
                "      allowed_paths:\n        - src/\n        - nested:\n"
            )
        self.assertIn("sequence items must be scalars", str(error.exception))

    def test_allowed_paths_reject_absolute_and_traversing_paths(self):
        for path, expected in (
            ("/etc/passwd", "repository-relative"),
            ("../../secrets", "traverse"),
        ):
            with self.subTest(path=path):
                with self.assertRaises(ConfigurationError) as error:
                    parse(
                        "schema: 1\nadvanced:\n  context:\n    symbol_context:\n"
                        f"      allowed_paths:\n        - {path}\n"
                    )
                self.assertIn(expected, str(error.exception))

    def test_declared_field_lines_are_tracked_for_every_section(self):
        configuration = parse(
            "schema: 1\ninference:\n  backend: local-ollama\n"
            "github:\n  writes: true\n"
            "advanced:\n  resources:\n    timeout_seconds: 60\n"
        )
        self.assertEqual(configuration.line_of("github.writes"), 5)
        self.assertEqual(configuration.line_of("advanced.resources.timeout_seconds"), 8)
        self.assertTrue(configuration.is_declared("advanced.resources.timeout_seconds"))
        self.assertIsNone(configuration.line_of("github.mentions"))


if __name__ == "__main__":
    unittest.main()

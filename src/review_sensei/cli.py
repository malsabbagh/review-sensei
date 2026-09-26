from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import re
import sys
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

from .baseline import (
    admission_context_document,
    admission_context_from_document,
    baseline_from_history_document,
    plan_verification_scope,
)
from .context import (
    ContextSnapshot,
    RepositoryContextStore,
    SymbolAwareContextPolicy,
    build_review_context_cache_key,
    build_review_context_selection,
)
from .diff import analyze_diff
from .errors import ReviewInputError, ReviewSenseiError
from .learnings import (
    DEFAULT_LEARNING_DIRECTORY,
    LearningStore,
    build_learning_diagnostic_report,
    load_learning_feedback,
    load_repository_learnings,
    summarize_learning_feedback,
)
from .models import (
    LearningEntry,
    ReviewRequest,
    ReviewTransaction,
    build_transaction_configuration_context,
    transaction_stage_identity,
)
from .outcomes import (
    DEFAULT_RECOVERY_TTL_SECONDS,
    RecoveryArtifact,
    ResourceBudget,
    RunOutcome,
    diagnostic_for_recovery_error,
    emit_host_outcome,
    load_recovery_artifact,
    recovery_expires_at,
    run_outcome_exit_code,
)
from .planning import DEFAULT_TOTAL_WORK_BUDGET, related_paths_for_change
from .provider_config import (
    openrouter_policy_from_env,
    openrouter_timeout_default,
    openrouter_upstream_default,
    resolve_effective_provider_configuration,
    validate_profile_provider_match,
)
from .providers import ProviderSettings, default_registry
from .providers.openai_compatible import is_allowlisted_openai_compatible_endpoint
from .providers.openrouter import (
    OpenRouterRoutingPolicy,
)
from .providers.profiles import get_provider_profile
from .providers.routing import bind_stage_providers
from .service import DEFAULT_STAGES, ReviewService
from .validation import DEFAULT_REVIEW_LIMITS, read_bounded_utf8
from .workflow import prepare_diff

# The parser intentionally narrows the namespace type after argparse parsing;
# the generic overload on argparse.ArgumentParser is broader than this seam.
# mypy: disable-error-code=override

DEFAULT_PROVIDER_MODE = "local"
DEFAULT_LOCAL_MODEL = "qwen3.5:4b"
DEFAULT_CLOUD_MODEL = "deepseek-v4.1-flash:cloud"
DEFAULT_LOCAL_BASE_URL = "http://127.0.0.1:11434/api"
DEFAULT_CLOUD_BASE_URL = "https://ollama.com/api"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_OPENROUTER_MODEL = "deepseek/deepseek-v4.1-flash"
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_OPENROUTER_UPSTREAM = "morph"


def _provider_mode() -> str:
    return (
        os.getenv("REVIEWSENSEI_PROVIDER_MODE", DEFAULT_PROVIDER_MODE).strip().lower()
    )


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _default_ollama_base_url() -> str:
    configured = os.getenv("OLLAMA_BASE_URL")
    if configured:
        return configured
    return (
        DEFAULT_CLOUD_BASE_URL
        if _provider_mode() == "cloud"
        else DEFAULT_LOCAL_BASE_URL
    )


def _default_ollama_model() -> str:
    configured = os.getenv("OLLAMA_MODEL")
    if configured:
        return configured
    if _provider_mode() == "cloud":
        return os.getenv("REVIEWSENSEI_CLOUD_MODEL", DEFAULT_CLOUD_MODEL)
    return os.getenv("REVIEWSENSEI_LOCAL_MODEL", DEFAULT_LOCAL_MODEL)


def _is_registered_option_token(
    token: str, option_actions: dict[str, argparse.Action]
) -> bool:
    if token in option_actions:
        return True
    return any(
        token.startswith(f"{option}=")
        for option in option_actions
        if option.startswith("--")
    )


def _explicit_cli_options(
    parser: argparse.ArgumentParser, arguments: list[str]
) -> set[str]:
    """Return long options from *arguments* that include an explicit value."""

    option_actions = parser._option_string_actions
    long_options = sorted(
        (option for option in option_actions if option.startswith("--")),
        key=len,
        reverse=True,
    )
    explicit: set[str] = set()
    index = 0
    prior_option_unsatisfied = False
    while index < len(arguments):
        token = arguments[index]
        matched_option: str | None = None
        matched_action: argparse.Action | None = None
        for option in long_options:
            if token == option:
                matched_option = option
                matched_action = option_actions[option]
                break
            prefix = f"{option}="
            if token.startswith(prefix):
                if token[len(prefix) :]:
                    explicit.add(option)
                prior_option_unsatisfied = False
                index += 1
                matched_option = ""
                break
        if matched_option == "":
            continue
        if matched_option is None:
            index += 1
            continue
        assert matched_action is not None
        if matched_action.nargs == 0:
            explicit.add(matched_option)
            prior_option_unsatisfied = False
            index += 1
            continue
        if index + 1 >= len(arguments):
            prior_option_unsatisfied = True
            index += 1
            continue
        next_token = arguments[index + 1]
        if _is_registered_option_token(next_token, option_actions):
            prior_option_unsatisfied = True
            index += 1
            continue
        if not prior_option_unsatisfied:
            explicit.add(matched_option)
        prior_option_unsatisfied = False
        index += 2
    return explicit


def _cli_option_set(
    arguments: list[str],
    option: str,
    *,
    explicit: set[str] | None = None,
) -> bool:
    """Return True when *option* was explicitly passed on the command line."""

    if explicit is not None:
        return option in explicit
    prefix = f"{option}="
    if any(value.startswith(prefix) and value[len(prefix) :] for value in arguments):
        return True
    try:
        index = arguments.index(option)
    except ValueError:
        return False
    return bool(
        index + 1 < len(arguments)
        and arguments[index + 1]
        and not arguments[index + 1].startswith("-")
    )


def _validate_profile_cli_args(args: argparse.Namespace, argv: list[str]) -> None:
    """Ensure explicit profile and provider selections stay aligned."""

    profile_name = getattr(args, "profile", None)
    if not profile_name:
        return
    selected = get_provider_profile(profile_name)
    if bool(getattr(args, "allow_custom_endpoint", False)):
        raise ReviewInputError(
            "--allow-custom-endpoint cannot be combined with --profile"
        )
    provider_name = str(args.provider).strip().lower()
    explicit = getattr(args, "_explicit_cli_options", None)
    if _cli_option_set(argv, "--provider", explicit=explicit):
        if provider_name == "fixture":
            raise ReviewInputError(
                "--provider fixture cannot be combined with --profile"
            )
        if provider_name != selected.provider:
            raise ReviewInputError(
                f"--provider {provider_name} does not match profile "
                f"'{selected.name}' (requires {selected.provider})"
            )
    elif provider_name != selected.provider:
        raise ReviewInputError(
            f"profile '{selected.name}' requires --provider {selected.provider}; "
            f"the current default is {provider_name!r} from --provider or "
            "REVIEWSENSEI_PROVIDER"
        )
    if _cli_option_set(argv, "--api-key-env", explicit=explicit):
        actual = getattr(args, "api_key_env", None)
        expected = selected.api_key_env
        if expected is None:
            raise ReviewInputError(
                f"--api-key-env cannot be combined with profile '{selected.name}'"
            )
        if actual != expected:
            raise ReviewInputError(
                f"--api-key-env {actual} does not match profile "
                f"'{selected.name}' (requires {expected})"
            )
    if _cli_option_set(argv, "--base-url", explicit=explicit):
        actual = getattr(args, "base_url", None)
        if actual is not None and actual.rstrip("/") != selected.base_url.rstrip("/"):
            raise ReviewInputError(
                f"--base-url cannot override profile '{selected.name}' endpoint"
            )


def _openai_timeout_default() -> float:
    configured = os.getenv("REVIEWSENSEI_OPENAI_TIMEOUT_SECONDS")
    source = "REVIEWSENSEI_OPENAI_TIMEOUT_SECONDS"
    if configured is None:
        configured = os.getenv("OPENAI_TIMEOUT_SECONDS", "120")
        source = "OPENAI_TIMEOUT_SECONDS"
    try:
        return _positive_float(configured)
    except argparse.ArgumentTypeError as exc:
        raise ReviewInputError(f"{source} must be a positive number") from exc


def _openrouter_timeout_default() -> float:
    return openrouter_timeout_default()


def _openrouter_upstream_default() -> str:
    return openrouter_upstream_default()


def _assign_if_present(args: argparse.Namespace, name: str, value: object) -> None:
    if hasattr(args, name):
        setattr(args, name, value)


def _apply_provider_defaults(args: argparse.Namespace, arguments: list[str]) -> None:
    """Resolve endpoint, model, credential, and timeout defaults by adapter."""

    if not hasattr(args, "provider"):
        return
    if getattr(args, "profile", None):
        return
    explicit = getattr(args, "_explicit_cli_options", None)
    provider = str(args.provider).strip().lower()
    if provider == "openai-compatible":
        if not _cli_option_set(arguments, "--base-url", explicit=explicit):
            _assign_if_present(
                args, "base_url", os.getenv("OPENAI_BASE_URL", DEFAULT_OPENAI_BASE_URL)
            )
        if not _cli_option_set(arguments, "--model", explicit=explicit):
            _assign_if_present(
                args, "model", os.getenv("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
            )
        if not _cli_option_set(arguments, "--api-key-env", explicit=explicit):
            _assign_if_present(args, "api_key_env", "OPENAI_API_KEY")
        if not _cli_option_set(arguments, "--timeout-seconds", explicit=explicit):
            _assign_if_present(args, "timeout_seconds", _openai_timeout_default())
    elif provider == "openrouter":
        if not _cli_option_set(arguments, "--base-url", explicit=explicit):
            _assign_if_present(
                args,
                "base_url",
                os.getenv("OPENROUTER_BASE_URL", DEFAULT_OPENROUTER_BASE_URL),
            )
        if not _cli_option_set(arguments, "--model", explicit=explicit):
            _assign_if_present(
                args,
                "model",
                os.getenv("OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL),
            )
        if not _cli_option_set(arguments, "--api-key-env", explicit=explicit):
            _assign_if_present(args, "api_key_env", "OPENROUTER_API_KEY")
        if not _cli_option_set(arguments, "--timeout-seconds", explicit=explicit):
            _assign_if_present(args, "timeout_seconds", _openrouter_timeout_default())
    elif provider == "fixture":
        if not _cli_option_set(arguments, "--model", explicit=explicit):
            _assign_if_present(args, "model", "fixture-v1")
        if not _cli_option_set(arguments, "--api-key-env", explicit=explicit):
            _assign_if_present(args, "api_key_env", None)


def _add_allow_custom_endpoint_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--allow-custom-endpoint",
        action="store_true",
        help=(
            "Allow an openai-compatible base URL outside api.openai.com; "
            "required for OPENAI_BASE_URL or --base-url that is not allowlisted"
        ),
    )


def _validate_live_profile_gates(args: argparse.Namespace, argv: list[str]) -> None:
    _validate_profile_cli_args(args, argv)
    _validate_unqualified_profile_gate(args)


def _require_allowlisted_openrouter_configuration(args: argparse.Namespace) -> None:
    """Reject non-allowlisted OpenRouter endpoints using effective defaults."""

    provider = str(getattr(args, "provider", "")).strip().lower()
    if provider != "openrouter":
        return
    resolve_effective_provider_configuration(
        profile=getattr(args, "profile", None),
        provider=provider,
        base_url=getattr(args, "base_url", None),
        model=getattr(args, "model", None),
        api_key_env=getattr(args, "api_key_env", None),
    )


def _require_allowlisted_openai_endpoint(args: argparse.Namespace) -> None:
    """Reject non-allowlisted OpenAI endpoints at the CLI boundary."""

    provider = str(getattr(args, "provider", "")).strip().lower()
    if provider != "openai-compatible":
        return
    if bool(getattr(args, "allow_custom_endpoint", False)):
        return
    base_url = str(getattr(args, "base_url", "") or "")
    if not is_allowlisted_openai_compatible_endpoint(base_url):
        raise ReviewInputError(
            "openai-compatible endpoint is not allowlisted; pass "
            "--allow-custom-endpoint only for an explicitly trusted service"
        )


def _openrouter_policy_from_args(args: argparse.Namespace) -> OpenRouterRoutingPolicy:
    profile_name = getattr(args, "profile", None)
    if profile_name:
        validate_profile_provider_match(
            profile_name=profile_name,
            provider_name=str(getattr(args, "provider", "")),
        )
        profile = get_provider_profile(profile_name)
        if profile.openrouter_policy is None:
            raise ReviewInputError(
                f"profile '{profile.name}' does not declare an OpenRouter routing policy"
            )
        return profile.openrouter_policy
    return openrouter_policy_from_env()


def _resolve_api_key(
    args: argparse.Namespace,
    *,
    argv: list[str],
) -> str | None:
    profile_name = getattr(args, "profile", None)
    if profile_name:
        profile = get_provider_profile(profile_name)
        if not profile.requires_api_key or not profile.api_key_env:
            return None
        api_key = os.getenv(profile.api_key_env)
        if not api_key:
            raise ReviewInputError(
                f"environment variable {profile.api_key_env} is unavailable"
            )
        return api_key
    provider_name = str(args.provider).strip().lower()
    if provider_name == "fixture":
        return None
    api_key_env = args.api_key_env
    api_key = os.getenv(api_key_env)
    if provider_name == "openrouter" and not api_key:
        raise ReviewInputError(f"environment variable {api_key_env} is unavailable")
    return api_key


def _validate_unqualified_profile_gate(args: argparse.Namespace) -> None:
    profile_name = getattr(args, "profile", None)
    if not profile_name:
        return
    profile = get_provider_profile(profile_name)
    if profile.qualification_status != "unqualified":
        return
    if getattr(args, "allow_unqualified_profile", False):
        return
    raise ReviewInputError(
        f"profile '{profile.name}' is unqualified; pass --allow-unqualified-profile "
        "to authorize remote egress before qualification evidence exists"
    )


def _provider_settings_from_args(
    args: argparse.Namespace,
    *,
    api_key: str | None,
    fixture_response: Path | None = None,
    argv: list[str] | None = None,
) -> ProviderSettings:
    profile_name = getattr(args, "profile", None)
    explicit = getattr(args, "_explicit_cli_options", None)
    provider_name = str(args.provider).strip().lower()
    openrouter_policy = (
        _openrouter_policy_from_args(args) if provider_name == "openrouter" else None
    )
    if provider_name == "openrouter":
        _require_allowlisted_openrouter_configuration(args)
    if profile_name:
        return ProviderSettings(
            name=provider_name,
            profile=profile_name,
            model=args.model
            if argv is None or _cli_option_set(argv, "--model", explicit=explicit)
            else None,
            base_url=(
                args.base_url
                if argv is None
                or _cli_option_set(argv, "--base-url", explicit=explicit)
                else None
            ),
            api_key=api_key,
            fixture_response=fixture_response,
            timeout_seconds=(
                args.timeout_seconds
                if argv is None
                or _cli_option_set(argv, "--timeout-seconds", explicit=explicit)
                else None
            ),
            allow_custom_endpoint=False,
            openrouter_policy=openrouter_policy,
        )
    if provider_name == "openai-compatible":
        _require_allowlisted_openai_endpoint(args)
    return ProviderSettings(
        name=provider_name,
        model=args.model,
        base_url=args.base_url,
        api_key=api_key,
        fixture_response=fixture_response,
        timeout_seconds=args.timeout_seconds,
        allow_custom_endpoint=bool(getattr(args, "allow_custom_endpoint", False)),
        openrouter_policy=openrouter_policy,
    )


def _checkpoint_cache_request(
    request: ReviewRequest,
    *,
    base_sha: str | None,
    head_sha: str | None,
) -> ReviewRequest:
    """Return the request the durable checkpoint binds to the trusted SHAs.

    The live inference request deliberately stays unbound: ADR 0053 keeps F2 off
    the inference path, so `service.run` must never derive coverage or cache
    identity from the trusted SHAs. Only this copy carries them, and it is used
    solely to derive the checkpoint cache key, which keeps the invariant in one
    place instead of depending on where a caller happens to build the key.
    """

    return replace(
        request,
        base_sha=(base_sha or "").strip().lower() or None,
        head_sha=(head_sha or "").strip().lower() or None,
    )


def _transaction_provider_identity(
    settings: ProviderSettings,
) -> tuple[dict[str, object], str | None]:
    """Return secret-free provider identity and the effective model.

    ``ProviderSettings`` is the same normalized object passed to stage
    routing. Named profiles intentionally leave omitted fields as ``None`` in
    that object, so fill those values from the immutable profile definition
    before hashing. Credentials are deliberately excluded.
    """

    provider_name = settings.name.strip().lower()
    profile = get_provider_profile(settings.profile) if settings.profile else None
    model = settings.model or (profile.model if profile is not None else None)
    base_url = settings.base_url or (profile.base_url if profile is not None else None)
    timeout_seconds = settings.timeout_seconds
    max_output_tokens = settings.max_output_tokens
    allow_custom_endpoint = settings.allow_custom_endpoint
    routing_policy = settings.openrouter_policy
    if profile is not None:
        timeout_seconds = (
            settings.timeout_seconds
            if settings.timeout_seconds is not None
            else profile.timeout_seconds
        )
        max_output_tokens = (
            settings.max_output_tokens
            if settings.max_output_tokens is not None
            else profile.max_output_tokens
        )
        allow_custom_endpoint = settings.allow_custom_endpoint
        routing_policy = settings.openrouter_policy or profile.openrouter_policy
    provider_identity: dict[str, object] = {
        "name": provider_name,
        "profile": profile.name if profile is not None else None,
        "base_url": base_url,
        "timeout_seconds": timeout_seconds,
        "max_output_tokens": max_output_tokens,
        "allow_custom_endpoint": allow_custom_endpoint,
        "openrouter_policy": (
            dict(routing_policy.identity_fields())
            if routing_policy is not None
            else None
        ),
    }
    return provider_identity, model


class _ProviderArgumentParser(argparse.ArgumentParser):
    """Argument parser that applies adapter-specific defaults after parsing."""

    def parse_args(
        self,
        args: Iterable[str] | None = None,
        namespace: argparse.Namespace | None = None,
    ) -> argparse.Namespace:
        raw_arguments = list(args) if args is not None else sys.argv[1:]
        parsed = super().parse_args(args, namespace)
        parsed._explicit_cli_options = _explicit_cli_options(self, raw_arguments)
        _apply_provider_defaults(parsed, raw_arguments)
        return parsed


def _package_version() -> str:
    try:
        return importlib.metadata.version("review-sensei")
    except importlib.metadata.PackageNotFoundError as exc:
        raise ReviewInputError("review-sensei package metadata is unavailable") from exc


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _continuation_rounds(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed not in {0, 1}:
        raise argparse.ArgumentTypeError("must be 0 or 1")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = _ProviderArgumentParser(
        prog="review-sensei",
        description="Run a provider-neutral AI review against a unified diff.",
        epilog=(
            "Additional commands use the same first-token dispatch as "
            "prepare-diff, evaluate, github, and promotion: config, doctor, "
            "plan, learnings, evaluate-convergence, prepare-diff, evaluate, "
            "github, promotion, resolve-hosted-openrouter."
        ),
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Print the installed ReviewSensei version and exit",
    )
    parser.add_argument("--diff", type=Path, help="Path to a unified diff file")
    parser.add_argument(
        "--profile",
        default=None,
        help=(
            "Named provider profile (local-private, fast-triage, deep-verification, "
            "openrouter-sonnet, openrouter-gpt)"
        ),
    )
    parser.add_argument(
        "--provider", default=os.getenv("REVIEWSENSEI_PROVIDER", "ollama")
    )
    parser.add_argument("--base-url", default=_default_ollama_base_url())
    parser.add_argument("--model", default=_default_ollama_model())
    parser.add_argument("--api-key-env", default="OLLAMA_API_KEY")
    parser.add_argument(
        "--fixture-response",
        type=Path,
        help=(
            "Path to a response file for --provider fixture; rejected for "
            "other providers"
        ),
    )
    parser.add_argument(
        "--timeout-seconds",
        type=_positive_float,
        default=os.getenv("OLLAMA_TIMEOUT_SECONDS", "900"),
    )
    _add_allow_custom_endpoint_argument(parser)
    parser.add_argument(
        "--allow-unqualified-profile",
        action="store_true",
        help=(
            "Authorize live review with an unqualified provider profile before "
            "qualification evidence exists"
        ),
    )
    parser.add_argument("--repository")
    parser.add_argument("--pull-request", type=int)
    parser.add_argument("--title")
    parser.add_argument("--instructions")
    parser.add_argument(
        "--base-sha",
        help=(
            "Exact reviewed base commit SHA; also the trusted snapshot for "
            "symbol-aware context"
        ),
    )
    parser.add_argument(
        "--head-sha",
        help=(
            "Exact reviewed head commit SHA; recorded only as coverage metadata "
            "and never used as the context snapshot or trusted configuration"
        ),
    )
    parser.add_argument(
        "--learning-root",
        type=Path,
        help=(
            "Checked-out target-branch repository containing approved learnings "
            "under .github/review-sensei/learnings"
        ),
    )
    parser.add_argument(
        "--learning-directory",
        type=Path,
        default=DEFAULT_LEARNING_DIRECTORY,
        help="Repository-relative directory containing learning JSON files",
    )
    parser.add_argument(
        "--context-root",
        type=Path,
        default=os.getenv("REVIEWSENSEI_CONTEXT_ROOT"),
        help=(
            "Trusted target-branch checkout used for explicitly configured lens "
            "documents; defaults to --learning-root when omitted"
        ),
    )
    parser.add_argument(
        "--enable-symbol-context",
        action="store_true",
        help=(
            "Opt in to bounded Python symbol-aware source context from the "
            "trusted base snapshot. Default remains documents and learnings only."
        ),
    )
    parser.add_argument(
        "--symbol-context-allowed-path",
        action="append",
        default=[],
        help="Allowed repository-relative path pattern for symbol-aware context",
    )
    parser.add_argument(
        "--symbol-context-max-files",
        type=int,
        default=16,
        help="Maximum files selected by symbol-aware context (default: 16)",
    )
    parser.add_argument(
        "--symbol-context-max-bytes",
        type=int,
        default=128 * 1024,
        help="Maximum total bytes selected by symbol-aware context (default: 131072)",
    )
    parser.add_argument(
        "--symbol-context-max-depth",
        type=int,
        default=1,
        help="Maximum relationship depth for symbol-aware context (default: 1)",
    )
    parser.add_argument(
        "--orchestrate-large-changes",
        action="store_true",
        help=(
            "Opt in to bounded chunk orchestration for changes that exceed a "
            "single per-request diff while remaining within the total-work budget"
        ),
    )
    parser.add_argument(
        "--no-learning-proposals",
        action="store_false",
        dest="propose_learnings",
        help="Do not ask the provider to propose durable repository learnings",
    )
    parser.add_argument(
        "--categories-dir",
        type=Path,
        default=os.getenv("REVIEWSENSEI_CATEGORIES_DIR"),
        help="Trusted directory containing reusable review category JSON files",
    )
    parser.add_argument(
        "--stages-dir",
        type=Path,
        default=os.getenv("REVIEWSENSEI_STAGES_DIR"),
        help="Trusted directory containing ordered stage configuration JSON files",
    )
    parser.add_argument(
        "--output", type=Path, help="Write JSON to a file instead of stdout"
    )
    parser.add_argument(
        "--transaction",
        action="store_true",
        help=(
            "Opt into the identity-bound analysis/publication transaction; "
            "use --configuration-context-output when the later publisher "
            "needs a serialized trusted configuration context"
        ),
    )
    parser.add_argument(
        "--configuration-context-output",
        type=Path,
        help=(
            "Write the trusted, secret-free configuration context needed to "
            "publish an identity-bound result; this also opts into the "
            "transaction and requires an operator review mode plus an explicit "
            "session ledger. Protect the emitted file: it becomes trusted "
            "admission input for the later github review command"
        ),
    )
    parser.add_argument(
        "--admission-context-output",
        type=Path,
        help=(
            "Write the trusted, secret-free admission context (prior baseline "
            "and current review context cache key) needed to publish a later "
            "verification round; this also opts into the transaction. Protect "
            "the emitted file: it becomes trusted admission input for the "
            "later github review command"
        ),
    )
    parser.add_argument(
        "--outcome",
        type=Path,
        help="Write the structured run-outcome JSON for this invocation",
    )
    parser.add_argument(
        "--recovery-artifact",
        type=Path,
        help="Write an opt-in, identity-bound publication recovery artifact",
    )
    parser.add_argument(
        "--recovery-ttl-seconds",
        type=int,
        default=DEFAULT_RECOVERY_TTL_SECONDS,
        help="Expiry window for --recovery-artifact (max 86400 seconds)",
    )
    parser.add_argument(
        "--review-mode",
        help=(
            "Review-convergence mode: merge-focused (default), advisory, or "
            "strict. Legacy settings must be migrated before execution. "
            "Operator modes enforce C5 round "
            "admission before inference when a session ledger is present."
        ),
    )
    parser.add_argument(
        "--session-ledger",
        type=Path,
        help=(
            "Local directory for the issue #136 durable session ledger. "
            "Operator modes reserve before inference and refuse unadmitted rounds."
        ),
    )
    parser.add_argument(
        "--github-session-ledger",
        action="store_true",
        help=(
            "Checkpoint the identity-bound transaction into the "
            "broker-attested GitHub issue-comment session ledger; the hosted "
            "publication boundary reads that same comment, so a fresh runner "
            "continues the durable rounds and baselines. Requires --transaction."
        ),
    )
    parser.add_argument(
        "--repository-id",
        type=int,
        help="GitHub repository database ID required for the hosted session ledger",
    )
    parser.add_argument(
        "--app-slug",
        default="reviewsensei[bot]",
        help=(
            "App login whose issue comments are trusted as the hosted session marker"
        ),
    )
    parser.add_argument(
        "--oidc-token",
        help="Optional GitHub Actions OIDC assertion for the hosted ledger.",
    )
    parser.add_argument(
        "--continue-rounds",
        type=_continuation_rounds,
        default=0,
        help="Authenticated bounded continuation: admit one extra verification round (0 or 1).",
    )
    return parser


def _resolve_hosted_openrouter_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review-sensei resolve-hosted-openrouter",
        description=(
            "Resolve and validate hosted OpenRouter model and upstream "
            "from workflow environment variables."
        ),
    )
    parser.add_argument(
        "action",
        choices=("model", "upstream", "both"),
        help="Emit resolved model, upstream, or both as a tab-separated line.",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Print the installed ReviewSensei version and exit",
    )
    return parser


def _prepare_diff_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review-sensei prepare-diff",
        description="Validate refs and prepare a bounded unified diff.",
    )
    parser.add_argument("--base-ref")
    parser.add_argument("--head-ref")
    parser.add_argument("--head-repository")
    parser.add_argument("--repository", type=Path, default=Path("."))
    parser.add_argument("--output", type=Path, default=Path("pr.patch"))
    parser.add_argument("--max-diff-bytes", type=int)
    parser.add_argument("--max-diff-lines", type=int)
    parser.add_argument("--max-diff-files", type=int)
    parser.add_argument("--max-diff-hunks", type=int)
    parser.add_argument(
        "--version",
        action="store_true",
        help="Print the installed ReviewSensei version and exit",
    )
    return parser


def _evaluate_parser() -> argparse.ArgumentParser:
    parser = _ProviderArgumentParser(
        prog="review-sensei evaluate",
        description="Run a provider-neutral evaluation against a versioned corpus.",
    )
    parser.add_argument(
        "--corpus", type=Path, default=Path("evaluation/v1/corpus.json")
    )
    parser.add_argument(
        "--mode",
        choices=("fixture", "live"),
        default="fixture",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help=(
            "Named provider profile (local-private, fast-triage, deep-verification, "
            "openrouter-sonnet, openrouter-gpt)"
        ),
    )
    parser.add_argument(
        "--provider", default=os.getenv("REVIEWSENSEI_PROVIDER", "ollama")
    )
    parser.add_argument("--base-url", default=_default_ollama_base_url())
    parser.add_argument("--model", default=_default_ollama_model())
    parser.add_argument("--api-key-env", default="OLLAMA_API_KEY")
    parser.add_argument(
        "--timeout-seconds",
        type=_positive_float,
        default=os.getenv("OLLAMA_TIMEOUT_SECONDS", "900"),
    )
    _add_allow_custom_endpoint_argument(parser)
    parser.add_argument(
        "--allow-unqualified-profile",
        action="store_true",
        help=(
            "Authorize live provider use with an unqualified profile before "
            "qualification evidence exists"
        ),
    )
    parser.add_argument("--provider-version")
    parser.add_argument(
        "--allow-live-model",
        action="store_true",
        help="Acknowledge that a live provider may be called.",
    )
    parser.add_argument(
        "--allow-data-egress",
        action="store_true",
        help="Acknowledge that synthetic corpus data may leave the machine.",
    )
    parser.add_argument(
        "--learning-root",
        type=Path,
        help="Trusted target-branch checkout used for --compare-learnings",
    )
    parser.add_argument(
        "--learning-directory",
        type=Path,
        default=DEFAULT_LEARNING_DIRECTORY,
        help="Repository-relative learnings directory for --compare-learnings",
    )
    parser.add_argument(
        "--learning-id",
        action="append",
        default=[],
        help="Limit --compare-learnings to selected approved learning ids",
    )
    parser.add_argument(
        "--compare-learnings",
        action="store_true",
        help=(
            "Compare fixture cases with and without selected approved learnings. "
            "Reports estimates, not causal proof."
        ),
    )
    parser.add_argument("--output", type=Path)
    return parser


def _doctor_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review-sensei doctor",
        description="Run bounded, read-only installation diagnostics.",
    )
    parser.add_argument("--stages-dir", type=Path)
    parser.add_argument("--categories-dir", type=Path)
    parser.add_argument("--context-root", type=Path)
    parser.add_argument(
        "--network",
        action="store_true",
        help="Run optional read-only probes (never generate, never mint tokens).",
    )
    parser.add_argument("--repository")
    parser.add_argument("--profile")
    parser.add_argument("--provider")
    parser.add_argument("--base-url")
    parser.add_argument("--model")
    parser.add_argument("--api-key-env")
    parser.add_argument("--compatibility-manifest", type=Path)
    parser.add_argument(
        "--review-mode",
        help=(
            "Review-convergence mode: merge-focused (default), advisory, or "
            "strict. Legacy settings must be migrated before execution. "
            "Operator modes apply trusted blocker admission at publication."
        ),
    )
    parser.add_argument(
        "--session-ledger",
        type=Path,
        help=(
            "Local directory for the issue #136 C3 durable session ledger. "
            "Also reads REVIEWSENSEI_SESSION_LEDGER. Requires --repository "
            "and --pull-request. Display only; doctor never writes."
        ),
    )
    parser.add_argument("--pull-request", type=int)
    parser.add_argument(
        "--allow-data-egress",
        action="store_true",
        help="Authorize read-only probes of a non-loopback provider endpoint.",
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _plan_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review-sensei plan",
        description="Preview a review execution plan without provider or GitHub calls.",
    )
    parser.add_argument("--diff", type=Path)
    parser.add_argument(
        "--repository",
        help="PR repository identity used when displaying --session-ledger",
    )
    parser.add_argument(
        "--pull-request",
        type=int,
        help="PR number used when displaying --session-ledger",
    )
    parser.add_argument("--title")
    parser.add_argument("--stage", action="append", default=[])
    parser.add_argument("--profile")
    parser.add_argument("--provider")
    parser.add_argument("--base-url")
    parser.add_argument("--model")
    parser.add_argument("--api-key-env")
    parser.add_argument("--provider-mode")
    parser.add_argument(
        "--review-mode",
        help=(
            "Review-convergence mode: merge-focused (default), advisory, or "
            "strict. Legacy settings must be migrated before execution. "
            "Operator modes apply trusted blocker admission at publication."
        ),
    )
    parser.add_argument("--base-sha")
    parser.add_argument("--head-sha")
    parser.add_argument("--categories-dir", type=Path)
    parser.add_argument(
        "--session-ledger",
        type=Path,
        help=(
            "Local directory for the issue #136 C3 durable session ledger. "
            "Also reads REVIEWSENSEI_SESSION_LEDGER. Display only; plan never writes."
        ),
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _learnings_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review-sensei learnings",
        description=(
            "Inspect approved learning lifecycle diagnostics or opt-in finding "
            "feedback without mutating trusted knowledge."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    diagnose = subparsers.add_parser(
        "diagnose",
        help="Report stale, conflicting, or untraceable approved learnings",
    )
    # ADR 0005 forbids implicitly scanning the current working directory, so
    # the trusted target/base checkout must be supplied explicitly.
    diagnose.add_argument(
        "--learning-root",
        type=Path,
        required=True,
        help="Explicit trusted target/base checkout root to inspect",
    )
    diagnose.add_argument(
        "--learning-directory",
        type=Path,
        default=DEFAULT_LEARNING_DIRECTORY,
    )
    diagnose.add_argument("--json", action="store_true", dest="as_json")
    feedback = subparsers.add_parser(
        "feedback",
        help="Summarize opt-in finding feedback without treating silence as approval",
    )
    feedback.add_argument("--file", type=Path, required=True)
    feedback.add_argument("--learning-root", type=Path)
    feedback.add_argument(
        "--learning-directory",
        type=Path,
        default=DEFAULT_LEARNING_DIRECTORY,
    )
    feedback.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _run_evaluate(args: argparse.Namespace, *, argv: list[str]) -> int:
    from .evaluation import (
        compare_learning_effect,
        endpoint_scope,
        evaluate_fixture,
        evaluate_live,
        load_corpus,
    )

    if args.mode == "fixture":
        if args.allow_live_model:
            raise ReviewInputError("--allow-live-model is only valid with --mode live")
        if args.allow_data_egress:
            raise ReviewInputError("--allow-data-egress is only valid with --mode live")
        corpus = load_corpus(args.corpus)
        if args.compare_learnings:
            store = (
                load_repository_learnings(
                    args.learning_root, directory=args.learning_directory
                )
                if args.learning_root
                else LearningStore()
            )
            selected = store.selectable_entries
            if args.learning_id:
                wanted = set(args.learning_id)
                selected = tuple(entry for entry in selected if entry.id in wanted)
            report = compare_learning_effect(corpus, selected)
            rendered = json.dumps(report, indent=2) + "\n"
            if args.output:
                args.output.write_text(rendered, encoding="utf-8")
            else:
                sys.stdout.write(rendered)
            # The comparison already ran the fixture evaluation with the
            # selected learnings, so adding this flag must not drop the
            # fixture pass/fail contract callers depend on.
            return 0 if report["with_learnings_passed"] else 1
        report = evaluate_fixture(corpus)
    else:
        if args.compare_learnings:
            raise ReviewInputError(
                "--compare-learnings is only valid with --mode fixture"
            )
        if not args.allow_live_model:
            raise ReviewInputError("--mode live requires --allow-live-model")
        if not args.provider_version:
            raise ReviewInputError("--mode live requires --provider-version")
        _validate_live_profile_gates(args, argv)
        if args.profile:
            profile = get_provider_profile(args.profile)
            scope = "loopback" if profile.endpoint_scope == "local" else "remote"
            live_model = profile.model
        else:
            scope = endpoint_scope(args.base_url)
            live_model = args.model
        if scope == "remote" and not args.allow_data_egress:
            raise ReviewInputError(
                "--mode live with a remote endpoint requires --allow-data-egress"
            )
        corpus = load_corpus(args.corpus)
        provider = default_registry().create(
            _provider_settings_from_args(
                args,
                api_key=_resolve_api_key(args, argv=argv),
                argv=argv,
            )
        )
        report = evaluate_live(
            corpus,
            provider,
            provider_version=args.provider_version,
            model=provider.model or live_model,
            endpoint_scope=scope,
        )
    rendered = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0 if report["passed"] else 1


def _parse_reproducibility_json(value: str) -> dict[str, object]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ReviewInputError("reproducibility must be a JSON object") from exc
    if not isinstance(parsed, dict):
        raise ReviewInputError("reproducibility must be a JSON object")
    return parsed


def _load_reproducibility(args: argparse.Namespace) -> dict[str, object]:
    if getattr(args, "reproducibility_file", None) is not None:
        return _parse_reproducibility_json(
            read_bounded_utf8(
                args.reproducibility_file,
                maximum=16 * 1024,
                label="reproducibility",
            )
        )
    return _parse_reproducibility_json(args.reproducibility_json)


def _load_context_document(path: Path, *, label: str) -> dict[str, object]:
    """Load a bounded trusted context document for publication admission."""

    try:
        value = json.loads(read_bounded_utf8(path, maximum=64 * 1024, label=label))
    except json.JSONDecodeError as exc:
        raise ReviewInputError(f"{label} must be a JSON object") from exc
    if not isinstance(value, dict):
        raise ReviewInputError(f"{label} must be a JSON object")
    return value


def _promotion_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review-sensei promotion",
        description=(
            "Emit or validate a promotion-record from evaluation report files. "
            "This command never calls a live provider."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    emit = subparsers.add_parser(
        "emit",
        help="Build a promotion-record from independent evaluation reports",
    )
    emit.add_argument("--report", type=Path, action="append", required=True)
    emit.add_argument("--observed-revision", required=True)
    emit.add_argument("--evaluated-at", required=True)
    reproducibility = emit.add_mutually_exclusive_group(required=True)
    reproducibility.add_argument("--reproducibility-json")
    reproducibility.add_argument("--reproducibility-file", type=Path)
    emit.add_argument(
        "--rollback-decision",
        choices=("revert-to-baseline", "hold", "none"),
        default="revert-to-baseline",
    )
    emit.add_argument(
        "--status",
        choices=("supported", "insufficient", "unsupported"),
    )
    emit.add_argument("--output", type=Path)

    validate = subparsers.add_parser(
        "validate",
        help="Validate a promotion-record against evaluation reports",
    )
    validate.add_argument("--record", type=Path, required=True)
    validate.add_argument("--report", type=Path, action="append", required=True)
    validate.add_argument(
        "--require-supported",
        action="store_true",
        help="Fail unless the record is a validated supported promotion.",
    )
    return parser


def _run_promotion(argv: list[str]) -> int:
    from .evaluation import (
        load_evaluation_report,
        promotion_record_from_reports,
        require_supported_promotion,
        validate_promotion_against_report,
        validate_promotion_record,
    )

    args = _promotion_parser().parse_args(argv)
    reports = [load_evaluation_report(path) for path in args.report]
    if args.command == "emit":
        record = promotion_record_from_reports(
            reports,
            observed_revision=args.observed_revision,
            reproducibility=_load_reproducibility(args),
            evaluated_at=args.evaluated_at,
            rollback_decision=args.rollback_decision,
            status=args.status,
        )
        rendered = json.dumps(record.to_dict(), indent=2) + "\n"
        if args.output:
            args.output.write_text(rendered, encoding="utf-8")
        else:
            sys.stdout.write(rendered)
        return 0
    record = validate_promotion_record(
        json.loads(
            read_bounded_utf8(args.record, maximum=262144, label="promotion record")
        )
    )
    if args.require_supported:
        require_supported_promotion(record, reports)
    else:
        for report in reports:
            validate_promotion_against_report(record, report)
    sys.stdout.write(json.dumps(record.to_dict(), indent=2) + "\n")
    return 0


def _github_parser() -> argparse.ArgumentParser:
    from .configuration import REVIEW_POLICIES

    parser = argparse.ArgumentParser(
        prog="review-sensei github",
        description="Run GitHub publication seams with write opt-ins.",
    )
    _add_allow_custom_endpoint_argument(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    review = subparsers.add_parser("review", help="Publish a validated review result")
    publication_source = review.add_mutually_exclusive_group()
    publication_source.add_argument("--result", type=Path)
    review.add_argument(
        "--configuration-context",
        type=Path,
        help=(
            "Trusted configuration-context JSON emitted alongside an "
            "identity-bound analysis result"
        ),
    )
    review.add_argument(
        "--evidence-context",
        type=Path,
        help="Optional trusted evidence-context JSON for an identity-bound result",
    )
    review.add_argument(
        "--admission-context",
        type=Path,
        help=(
            "Trusted admission-context JSON (prior baseline and current review "
            "context cache key) emitted by the analysis that produced this "
            "result; required to publish a verification round, which cannot "
            "reconstruct either input from the durable transaction alone"
        ),
    )
    review.add_argument("--diff", type=Path, required=True)
    review.add_argument("--repository", required=True)
    review.add_argument("--repository-id", type=int, required=True)
    review.add_argument("--pull-request", type=int, required=True)
    review.add_argument("--head-sha", required=True)
    review.add_argument("--base-branch")
    review.add_argument("--base-sha")
    review.add_argument("--app-slug", default="reviewsensei[bot]")
    review.add_argument("--oidc-token")
    review.add_argument(
        "--outcome",
        type=Path,
        help="Write the structured run-outcome JSON for this publication",
    )
    publication_source.add_argument(
        "--recover-from",
        type=Path,
        help="Publish a retained recovery artifact without invoking a model. Ambient REVIEWSENSEI_REVIEW_MODE does not apply; an explicit operator --review-mode is refused because serialized results drop admission state.",
    )
    review.add_argument(
        "--allow-write",
        action="store_true",
        help="Acknowledge that GitHub writes are enabled for this invocation.",
    )
    review.add_argument(
        "--enable-review",
        action="store_true",
        help="Opt into review publication for this invocation.",
    )
    review.add_argument(
        "--enable-auto-approve",
        dest="enable_auto_approve",
        action="store_true",
        default=True,
        help="Approve eligible complete reviews (the default).",
    )
    review.add_argument(
        "--no-auto-approve",
        dest="enable_auto_approve",
        action="store_false",
        help=(
            "Enforce required fixes through the ReviewSensei check without "
            "emitting an approval, unless --reviews-policy says otherwise."
        ),
    )
    review.add_argument(
        "--reviews-policy",
        choices=list(REVIEW_POLICIES),
        default=None,
        help=(
            "Canonical github.reviews mode: auto-approve (default), blocking, "
            "or advisory. Takes precedence over the approval boolean."
        ),
    )
    review.add_argument(
        "--enable-learning-prs",
        action="store_true",
        help="Opt into deterministic draft learning-PR publication.",
    )
    review.add_argument(
        "--review-mode",
        help=(
            "Review-convergence mode: merge-focused (default), advisory, or "
            "strict. Legacy settings must be migrated before execution. "
            "Operator modes apply trusted blocker admission before GitHub review "
            "events. Recovery artifacts cannot be republished under operator modes."
        ),
    )
    review.add_argument(
        "--session-ledger",
        type=Path,
        help=(
            "Local directory for the issue #136 durable session ledger. "
            "Operator modes reserve/commit a counted round around publication "
            "and refuse unadmitted REQUEST_CHANGES (C5)."
        ),
    )
    review.add_argument(
        "--github-session-ledger",
        action="store_true",
        help=(
            "Persist the C3 session ledger as one GitHub issue comment on the "
            "source pull request. Operator modes only count rounds."
        ),
    )
    review.add_argument(
        "--continue-rounds",
        type=_continuation_rounds,
        default=0,
        help="Authenticated bounded continuation: admit one extra verification round (0 or 1).",
    )

    command = subparsers.add_parser(
        "command",
        help="Apply an authenticated @sensei maintainer command to the session ledger",
    )
    command.add_argument("--comment-body", required=True)
    command.add_argument("--actor", required=True)
    command.add_argument("--actor-type", default="User")
    command.add_argument("--association", required=True)
    command.add_argument("--repository", required=True)
    command.add_argument(
        "--repository-id",
        type=int,
        help="GitHub repository database ID required for the hosted session ledger",
    )
    command.add_argument("--pull-request", type=int, required=True)
    command.add_argument("--head-sha")
    command.add_argument(
        "--source-comment-id",
        type=int,
        help="GitHub issue-comment ID required for a hosted command mutation",
    )
    command.add_argument("--app-slug", default="reviewsensei[bot]")
    command.add_argument(
        "--session-ledger",
        type=Path,
        help=(
            "Local operator directory for the durable session ledger; hosted "
            "webhook commands use GitHubApplication and broker authorization."
        ),
    )
    command.add_argument(
        "--github-session-ledger",
        action="store_true",
        help="Use the broker-attested GitHub issue-comment session ledger.",
    )
    command.add_argument(
        "--session-attestation",
        type=Path,
        help="Broker-bound hosted command attestation JSON.",
    )
    command.add_argument(
        "--oidc-token",
        help="Optional GitHub Actions OIDC assertion for the hosted ledger.",
    )
    command.add_argument(
        "--allow-write",
        action="store_true",
        help=(
            "Required for mutating session commands and when exchanging a "
            "GitHub capability; read-only status does not require it."
        ),
    )

    reply = subparsers.add_parser("reply", help="Generate or publish a mention reply")
    reply.add_argument("--reply", type=Path)
    reply.add_argument(
        "--generate",
        action="store_true",
        help="Build bounded GitHub context, invoke the provider, and publish the reply.",
    )
    reply.add_argument("--repository", required=True)
    reply.add_argument("--pull-request", type=int, required=True)
    reply.add_argument("--source-comment-id", type=int, required=True)
    reply.add_argument("--source-updated-at", required=True)
    reply.add_argument("--head-sha")
    reply.add_argument(
        "--source-kind",
        choices=("inline", "issue"),
        default="inline",
    )
    reply.add_argument("--app-slug", default="reviewsensei[bot]")
    reply.add_argument("--root-comment-id", type=int, default=0)
    reply.add_argument("--oidc-token")
    reply.add_argument("--github-token-env", default="GITHUB_TOKEN")
    reply.add_argument(
        "--profile",
        default=None,
        help=(
            "Named provider profile (local-private, fast-triage, deep-verification, "
            "openrouter-sonnet, openrouter-gpt)"
        ),
    )
    reply.add_argument(
        "--provider", default=os.getenv("REVIEWSENSEI_PROVIDER", "ollama")
    )
    reply.add_argument("--base-url", default=_default_ollama_base_url())
    reply.add_argument("--model", default=_default_ollama_model())
    reply.add_argument("--api-key-env", default="OLLAMA_API_KEY")
    reply.add_argument(
        "--timeout-seconds",
        type=_positive_float,
        default=os.getenv("OLLAMA_TIMEOUT_SECONDS", "900"),
    )
    reply.add_argument(
        "--allow-unqualified-profile",
        action="store_true",
        help=(
            "Authorize live provider use with an unqualified profile before "
            "qualification evidence exists"
        ),
    )
    reply.add_argument(
        "--allow-write",
        action="store_true",
        help="Acknowledge that GitHub writes may be enabled for this invocation.",
    )
    reply.add_argument(
        "--enable-reply",
        action="store_true",
        help="Enable mention reply publication for this invocation.",
    )
    return parser


def _bind_hosted_command_attestation(
    attestation: dict[str, object],
    *,
    repository: str,
    repository_id: int,
    pull_request: int,
    head_sha: str,
    source_comment_id: int,
) -> None:
    """Reject a hosted command attestation that disagrees with this invocation.

    The broker re-checks this binding against the OIDC claims; checking it here
    fails a mismatched attestation before any credential is exchanged.

    `run_id` is intentionally not compared: the CLI has no trustworthy run
    identity of its own, so the broker is the only place that can bind it.
    """

    if (
        attestation.get("version") != 1
        or attestation.get("operation") != "command"
        or attestation.get("repository") != repository
        or attestation.get("repository_id") != repository_id
        or attestation.get("pull_request") != pull_request
        or attestation.get("head_sha") != head_sha
        or attestation.get("source_comment_id") != source_comment_id
        or attestation.get("concurrency_group")
        != f"reviewsensei-session-{repository_id}-{pull_request}"
    ):
        raise ReviewInputError(
            "session attestation does not match the hosted command identity"
        )


def _run_github(args: argparse.Namespace, *, argv: list[str]) -> int:
    from .convergence import (
        OPERATOR_REVIEW_MODES,
        ReviewConvergencePolicy,
        resolve_review_convergence_policy,
        resolve_review_mode,
    )
    from .hosting.github import (
        BrokerClient,
        ConversationPublisher,
        GitHubApplication,
        GitHubHttp,
        GitHubWriteOptions,
        LearningPRPublisher,
        LearningPRResult,
        ReviewPublisher,
    )
    from .hosting.github.errors import (
        GitHubPublicationError,
        GitHubPublicationTransientError,
    )
    from .hosting.github.publication import outcome_from_publication
    from .models import ConversationReply, ReviewResult
    from .session import resolve_local_session_ledger

    if args.command == "command":
        from .disposition import (
            apply_session_command,
            authorized_maintainer,
            parse_maintainer_command,
        )
        from .session import SessionIdentity

        parsed = parse_maintainer_command(
            args.comment_body,
            actor=args.actor,
            head_sha=getattr(args, "head_sha", None),
        )
        if parsed is None:
            raise ReviewInputError("comment is not a supported maintainer command")
        if not authorized_maintainer(
            login=args.actor,
            user_type=args.actor_type,
            association=args.association,
            app_slug=args.app_slug,
        ):
            raise ReviewInputError("maintainer command is unauthorized")
        if parsed.action != "status" and not args.allow_write:
            raise ReviewInputError("github writes require --allow-write")
        if getattr(args, "github_session_ledger", False):
            if args.session_ledger is not None:
                raise ReviewInputError(
                    "hosted maintainer commands cannot use --session-ledger"
                )
            if (
                isinstance(args.repository_id, bool)
                or not isinstance(args.repository_id, int)
                or args.repository_id <= 0
            ):
                raise ReviewInputError(
                    "hosted maintainer commands require --repository-id"
                )
            if not isinstance(args.head_sha, str) or not re.fullmatch(
                r"[a-f0-9]{40}", args.head_sha
            ):
                raise ReviewInputError("hosted maintainer commands require --head-sha")
            source_comment_id = getattr(args, "source_comment_id", None)
            session_attestation = None
            if parsed.action != "status":
                if (
                    isinstance(source_comment_id, bool)
                    or not isinstance(source_comment_id, int)
                    or source_comment_id <= 0
                ):
                    raise ReviewInputError(
                        "hosted command mutations require --source-comment-id"
                    )
                attestation_path = getattr(args, "session_attestation", None)
                if attestation_path is None:
                    raise ReviewInputError(
                        "hosted command mutations require --session-attestation"
                    )
                try:
                    loaded_attestation = json.loads(
                        read_bounded_utf8(
                            attestation_path,
                            maximum=16 * 1024,
                            label="session attestation",
                        )
                    )
                except json.JSONDecodeError as exc:
                    raise ReviewInputError(
                        "session attestation is invalid JSON"
                    ) from exc
                if not isinstance(loaded_attestation, dict):
                    raise ReviewInputError("session attestation must be an object")
                _bind_hosted_command_attestation(
                    loaded_attestation,
                    repository=args.repository,
                    repository_id=args.repository_id,
                    pull_request=args.pull_request,
                    head_sha=args.head_sha,
                    source_comment_id=source_comment_id,
                )
                session_attestation = loaded_attestation
            http = GitHubHttp()
            application = GitHubApplication(
                broker=BrokerClient(),
                http=http,
                reviewer=ReviewPublisher(http=http),
                learner=LearningPRPublisher(http=http),
                replier=ConversationPublisher(http=http),
            )
            result = application.apply_maintainer_command(
                options=GitHubWriteOptions(
                    github_writes=args.allow_write,
                    github_session_ledger=True,
                ),
                oidc_token=getattr(args, "oidc_token", None),
                repository=args.repository,
                repository_id=args.repository_id,
                pull_request=args.pull_request,
                head_sha=args.head_sha,
                body=args.comment_body,
                actor_login=args.actor,
                actor_type=args.actor_type,
                association=args.association,
                app_slug=args.app_slug,
                source_comment_id=source_comment_id,
                session_attestation=session_attestation,
            )
            print(result.summary)
            return 0
        for flag, value in (
            ("--repository-id", getattr(args, "repository_id", None)),
            ("--source-comment-id", getattr(args, "source_comment_id", None)),
            ("--session-attestation", getattr(args, "session_attestation", None)),
            ("--oidc-token", getattr(args, "oidc_token", None)),
        ):
            if value is not None:
                raise ReviewInputError(
                    f"{flag} requires --github-session-ledger for maintainer commands"
                )
        ledger = resolve_local_session_ledger(getattr(args, "session_ledger", None))
        if ledger is None:
            raise ReviewInputError("maintainer commands require a session ledger")
        session_identity = SessionIdentity(
            repository=args.repository, pull_request=args.pull_request
        )
        _record, command_result = apply_session_command(
            ledger, session_identity, parsed
        )
        print(command_result.summary)
        return 0

    if not args.allow_write:
        raise ReviewInputError("github writes require --allow-write")
    broker = BrokerClient()
    http = GitHubHttp()
    session_ledger = None
    if args.command == "review":
        if (
            getattr(args, "session_ledger", None) is not None
            or getattr(args, "github_session_ledger", False)
        ) and (
            not getattr(args, "repository", None)
            or getattr(args, "pull_request", None) is None
        ):
            raise ReviewInputError(
                "session ledger requires --repository and --pull-request"
            )
        session_ledger = resolve_local_session_ledger(
            getattr(args, "session_ledger", None)
        )
    application = GitHubApplication(
        broker=broker,
        http=http,
        reviewer=ReviewPublisher(http=http),
        learner=LearningPRPublisher(http=http),
        replier=ConversationPublisher(http=http),
        session_ledger=session_ledger,
    )
    if args.command == "review":
        diff = read_bounded_utf8(args.diff, maximum=1_048_576, label="diff")
        if args.enable_review and (not args.base_branch or not args.base_sha):
            raise ReviewInputError(
                "review publication requires --base-branch and --base-sha"
            )
        identity = {
            "repository": args.repository,
            "pull_request_number": args.pull_request,
            "base_sha": args.base_sha,
            "head_sha": args.head_sha,
        }
        if args.recover_from and args.enable_learning_prs:
            outcome = RunOutcome(
                "publication_failed",
                diagnostic="recovery_learning_prs_refused",
                **identity,
            )
            emit_host_outcome(outcome, output_path=args.outcome)
            print(
                "review-sensei: publication recovery cannot write learning pull requests",
                file=sys.stderr,
            )
            print(outcome.status)
            return run_outcome_exit_code(outcome.status)
        explicit_mode = getattr(args, "review_mode", None)
        if args.recover_from:
            if (
                explicit_mode is not None
                and resolve_review_mode(explicit_mode) in OPERATOR_REVIEW_MODES
            ):
                convergence_policy = resolve_review_convergence_policy(
                    mode=explicit_mode
                )
            else:
                convergence_policy = ReviewConvergencePolicy()
        else:
            convergence_policy = resolve_review_convergence_policy(mode=explicit_mode)
        if args.recover_from:
            try:
                artifact = load_recovery_artifact(args.recover_from)
                artifact.validate(
                    repository=args.repository,
                    pull_request_number=args.pull_request,
                    base_sha=args.base_sha,
                    head_sha=args.head_sha,
                )
                review_publication = application.recover_review(
                    options=GitHubWriteOptions(
                        github_writes=True,
                        auto_review=args.enable_review,
                        auto_approve=args.enable_auto_approve,
                        reviews_policy=getattr(args, "reviews_policy", None),
                    ),
                    oidc_token=args.oidc_token,
                    repository=args.repository,
                    repository_id=args.repository_id,
                    pull_request=args.pull_request,
                    head_sha=args.head_sha,
                    base_branch=args.base_branch,
                    base_sha=args.base_sha,
                    artifact=artifact,
                    diff=diff,
                    app_slug=args.app_slug,
                    convergence_policy=convergence_policy,
                )
            except (GitHubPublicationTransientError, GitHubPublicationError) as exc:
                diagnostic = (
                    "publication_ambiguous"
                    if isinstance(exc, GitHubPublicationTransientError)
                    else "publication_failed"
                )
                outcome = RunOutcome(
                    "publication_failed",
                    diagnostic=diagnostic,
                    **identity,
                )
                emit_host_outcome(outcome, output_path=args.outcome)
                print(f"review-sensei: {exc}", file=sys.stderr)
                print(outcome.status)
                return run_outcome_exit_code(outcome.status)
            except ReviewInputError as exc:
                outcome = RunOutcome(
                    "publication_failed",
                    diagnostic=diagnostic_for_recovery_error(exc),
                    **identity,
                )
                emit_host_outcome(outcome, output_path=args.outcome)
                print(f"review-sensei: {exc}", file=sys.stderr)
                print(outcome.status)
                return run_outcome_exit_code(outcome.status)
            outcome = outcome_from_publication(review_publication, **identity)
            emit_host_outcome(outcome, output_path=args.outcome)
            print(review_publication.status)
            return run_outcome_exit_code(outcome.status)
        if args.result is None:
            outcome = RunOutcome(
                "publication_failed",
                diagnostic="publication_failed",
                **identity,
            )
            emit_host_outcome(outcome, output_path=args.outcome)
            print(
                "review-sensei: review publication requires --result or --recover-from",
                file=sys.stderr,
            )
            print(outcome.status)
            return run_outcome_exit_code(outcome.status)
        result = ReviewResult.from_dict(
            json.loads(
                read_bounded_utf8(args.result, maximum=2_097_152, label="result")
            )
        )
        configuration_context = (
            _load_context_document(
                args.configuration_context,
                label="configuration context",
            )
            if args.configuration_context is not None
            else None
        )
        evidence_context = (
            _load_context_document(args.evidence_context, label="evidence context")
            if args.evidence_context is not None
            else None
        )
        admission_baseline = None
        admission_current_key = None
        if args.admission_context is not None:
            if configuration_context is None:
                # The identity-bound transaction is what makes the admission
                # inputs meaningful; accepting them without it would let a
                # hosted caller bypass F3 admission with caller-supplied state.
                raise ReviewInputError(
                    "admission context requires an identity-bound configuration context"
                )
            try:
                admission_baseline, admission_current_key = (
                    admission_context_from_document(
                        _load_context_document(
                            args.admission_context, label="admission context"
                        )
                    )
                )
            except ReviewInputError as exc:
                raise ReviewInputError(f"admission context is invalid: {exc}") from exc
        try:
            review_outcome = application.publish_review(
                options=GitHubWriteOptions(
                    github_writes=True,
                    auto_review=args.enable_review,
                    auto_approve=args.enable_auto_approve,
                    reviews_policy=getattr(args, "reviews_policy", None),
                    github_session_ledger=getattr(args, "github_session_ledger", False),
                ),
                oidc_token=args.oidc_token,
                repository=args.repository,
                repository_id=args.repository_id,
                pull_request=args.pull_request,
                head_sha=args.head_sha,
                base_branch=args.base_branch,
                base_sha=args.base_sha,
                result=result,
                diff=diff,
                app_slug=args.app_slug,
                convergence_policy=convergence_policy,
                continuation_rounds=getattr(args, "continue_rounds", 0),
                configuration_context=configuration_context,
                evidence_context=evidence_context,
                baseline=admission_baseline,
                current_key=admission_current_key,
            )
        except GitHubPublicationTransientError as exc:
            outcome = RunOutcome(
                "publication_failed",
                diagnostic="publication_ambiguous",
                **identity,
            )
            emit_host_outcome(outcome, output_path=args.outcome)
            print(f"review-sensei: {exc}", file=sys.stderr)
            print(outcome.status)
            return run_outcome_exit_code(outcome.status)
        except GitHubPublicationError as exc:
            outcome = RunOutcome(
                "publication_failed",
                diagnostic="publication_failed",
                **identity,
            )
            emit_host_outcome(outcome, output_path=args.outcome)
            print(f"review-sensei: {exc}", file=sys.stderr)
            print(outcome.status)
            return run_outcome_exit_code(outcome.status)
        learning_outcomes: tuple[LearningPRResult, ...] = ()
        review_allows_learning = not args.enable_review or review_outcome.status in {
            "published",
            "already_published",
        }
        if args.enable_learning_prs and review_allows_learning:
            if not args.base_branch or not args.base_sha:
                raise ReviewInputError(
                    "learning PR publication requires --base-branch and --base-sha"
                )
            learning_outcomes = application.publish_learning_proposals(
                options=GitHubWriteOptions(
                    github_writes=True,
                    learning_prs=True,
                ),
                oidc_token=args.oidc_token,
                repository=args.repository,
                repository_id=args.repository_id,
                pull_request=args.pull_request,
                head_sha=args.head_sha,
                base_branch=args.base_branch,
                base_sha=args.base_sha,
                result=result,
            )
        outcome = outcome_from_publication(review_outcome, **identity)
        emit_host_outcome(outcome, output_path=args.outcome)
        statuses = [review_outcome.status]
        statuses.extend(item.status for item in learning_outcomes)
        print(" ".join(statuses))
        return run_outcome_exit_code(outcome.status)
    if args.generate:
        if not args.enable_reply:
            print("disabled")
            return 0
        read_token = os.getenv(args.github_token_env)
        if not read_token:
            raise ReviewInputError(
                f"GitHub read token environment variable {args.github_token_env} is unavailable"
            )
        _validate_live_profile_gates(args, argv)
        provider = default_registry().create(
            _provider_settings_from_args(
                args,
                api_key=_resolve_api_key(args, argv=argv),
                argv=argv,
            )
        )
        reply_outcome = application.generate_and_publish_reply(
            options=GitHubWriteOptions(
                github_writes=True,
                mention_replies=True,
            ),
            oidc_token=args.oidc_token,
            read_token=read_token,
            repository=args.repository,
            pull_request=args.pull_request,
            source_comment_id=args.source_comment_id,
            source_updated_at=args.source_updated_at,
            expected_head_sha=args.head_sha or None,
            reply_provider=provider,
            model=args.model,
            app_slug=args.app_slug,
            root_comment_id=args.root_comment_id or None,
            source_kind=args.source_kind,
        )
        print(reply_outcome.status)
        return 0
    if args.reply is None or not args.head_sha:
        raise ReviewInputError(
            "publishing a reply requires --reply and --head-sha unless --generate is used"
        )
    reply = ConversationReply.from_dict(
        json.loads(read_bounded_utf8(args.reply, maximum=16 * 1024, label="reply"))
    )
    reply_outcome = application.publish_reply(
        options=GitHubWriteOptions(
            github_writes=True,
            mention_replies=args.enable_reply,
        ),
        oidc_token=args.oidc_token,
        repository=args.repository,
        pull_request=args.pull_request,
        source_comment_id=args.source_comment_id,
        source_updated_at=args.source_updated_at,
        head_sha=args.head_sha,
        reply=reply,
        app_slug=args.app_slug,
        root_comment_id=args.root_comment_id,
        source_kind=args.source_kind,
    )
    print(reply_outcome.status)
    return 0


def _print_offline_error(exc: BaseException) -> None:
    print(f"review-sensei: {exc}", file=sys.stderr)


def _run_doctor_command(arguments: list[str]) -> int:
    args = _doctor_parser().parse_args(arguments)
    try:
        from .diagnostics import (
            DOCTOR_ACTION_REQUIRED,
            DOCTOR_UNKNOWN,
            render_diagnostic,
            run_doctor,
        )

        report = run_doctor(
            stages_dir=args.stages_dir,
            categories_dir=args.categories_dir,
            context_root=args.context_root,
            include_network=args.network,
            provider_mode=os.getenv("REVIEWSENSEI_PROVIDER_MODE"),
            review_mode=args.review_mode,
            repository=args.repository,
            profile=args.profile,
            provider=args.provider,
            base_url=args.base_url,
            model=args.model,
            api_key_env=args.api_key_env,
            compatibility_manifest=args.compatibility_manifest,
            allow_data_egress=args.allow_data_egress,
            session_ledger=args.session_ledger,
            pull_request=args.pull_request,
        )
        sys.stdout.write(render_diagnostic(report, as_json=args.as_json))
        return (
            DOCTOR_ACTION_REQUIRED
            if report["status"] == "action"
            else DOCTOR_UNKNOWN
            if report["status"] == "unknown"
            else 0
        )
    except (OSError, ValueError, TypeError, ReviewSenseiError) as exc:
        _print_offline_error(exc)
        return 2
    except Exception as exc:  # pragma: no cover - unexpected diagnostic failure
        _print_offline_error(
            RuntimeError(f"unexpected diagnostic failure: {type(exc).__name__}")
        )
        return 2


def _run_plan_command(arguments: list[str]) -> int:
    args = _plan_parser().parse_args(arguments)
    try:
        from .diagnostics import build_plan, render_diagnostic

        diff = (
            read_bounded_utf8(
                args.diff,
                maximum=DEFAULT_REVIEW_LIMITS.max_diff_bytes,
                label="diff",
            )
            if args.diff
            else None
        )
        report = build_plan(
            diff=diff,
            repository=args.repository,
            pull_request=args.pull_request,
            title=args.title,
            stages=args.stage,
            profile=args.profile,
            provider=args.provider,
            base_url=args.base_url,
            model=args.model,
            api_key_env=args.api_key_env,
            provider_mode=args.provider_mode,
            review_mode=args.review_mode,
            base_sha=args.base_sha,
            head_sha=args.head_sha,
            categories_dir=args.categories_dir,
            session_ledger=args.session_ledger,
        )
        sys.stdout.write(render_diagnostic(report, as_json=args.as_json))
        return 0 if report["status"] == "ready" else 3
    except (OSError, ValueError, TypeError, ReviewSenseiError) as exc:
        _print_offline_error(exc)
        return 2
    except Exception as exc:  # pragma: no cover - unexpected diagnostic failure
        _print_offline_error(
            RuntimeError(f"unexpected diagnostic failure: {type(exc).__name__}")
        )
        return 2


def _run_learnings_command(arguments: list[str]) -> int:
    args = _learnings_parser().parse_args(arguments)
    try:
        if args.command == "diagnose":
            store = load_repository_learnings(
                args.learning_root, directory=args.learning_directory
            )
            report = build_learning_diagnostic_report(store)
            if args.as_json:
                sys.stdout.write(json.dumps(report, indent=2) + "\n")
            else:
                diagnostics = report["diagnostics"]
                if not isinstance(diagnostics, list) or not diagnostics:
                    sys.stdout.write("No learning lifecycle diagnostics.\n")
                else:
                    for item in diagnostics:
                        if not isinstance(item, dict):
                            continue
                        related = item.get("related_ids") or []
                        detail = item.get("detail") or ""
                        suffix = f" {detail}".rstrip() if detail else ""
                        related_text = (
                            f" related={','.join(str(value) for value in related)}"
                            if related
                            else ""
                        )
                        sys.stdout.write(
                            f"{item.get('code')} {item.get('entry_id')}"
                            f"{related_text}{suffix}\n"
                        )
                sys.stdout.write(
                    "Diagnostics are advisory; approved entries are unchanged.\n"
                )
            return 0
        records = load_learning_feedback(args.file)
        known_ids: tuple[str, ...] | None = None
        if args.learning_root:
            store = load_repository_learnings(
                args.learning_root, directory=args.learning_directory
            )
            known_ids = tuple(entry.id for entry in store.all_entries)
        summary = summarize_learning_feedback(records, known_learning_ids=known_ids)
        if args.as_json:
            sys.stdout.write(json.dumps(summary, indent=2) + "\n")
        else:
            by_outcome = summary["by_outcome"]
            sys.stdout.write(f"records {summary['record_count']}\n")
            if isinstance(by_outcome, dict):
                for outcome, count in by_outcome.items():
                    sys.stdout.write(f"{outcome} {count}\n")
            without_feedback = summary["known_learning_ids_without_feedback"]
            if summary["known_learning_ids_scope"] == "unset":
                sys.stdout.write(
                    "known learnings without feedback (not enumerated: no "
                    "approved store loaded; pass --learning-root)\n"
                )
            elif isinstance(without_feedback, list):
                # Shown so text readers can tell an empty list apart from an
                # unavailable one.
                sys.stdout.write(
                    "known learnings without feedback "
                    f"{', '.join(str(value) for value in without_feedback) or 'none'}\n"
                )
            sys.stdout.write("Absence of feedback is not approval.\n")
        return 0
    except (OSError, ValueError, TypeError, ReviewSenseiError) as exc:
        _print_offline_error(exc)
        return 1


def _evaluate_convergence_parser() -> argparse.ArgumentParser:
    from .sequence import UNAVAILABLE_EVIDENCE_IDENTITY

    parser = argparse.ArgumentParser(
        prog="review-sensei evaluate-convergence",
        description=(
            "Replay a frozen synthetic review sequence against a convergence "
            "policy. Observation-only; does not publish or change the runtime default. "
            "With --observed, exit 0 only when cutover_status is passed. A completed "
            "not_ready report is still written and the process exits 1. Invalid "
            "input, including a legacy review mode, exits 1 without a report."
        ),
    )
    parser.add_argument(
        "--review-mode",
        default="merge-focused",
        help="Policy to replay (default merge-focused). Publication default is merge-focused.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Write JSON instead of a short summary",
    )
    parser.add_argument(
        "--compare-default",
        action="store_true",
        help="Also replay the compatible legacy default for observation-only comparison",
    )
    parser.add_argument(
        "--observed",
        action="store_true",
        help=(
            "Run the real-component offline evidence harness with mocked model "
            "and GitHub edges. Exit 0 only when the cutover gate passes."
        ),
    )
    parser.add_argument(
        "--source-identity",
        default=os.getenv("GITHUB_SHA", UNAVAILABLE_EVIDENCE_IDENTITY),
        help=(
            "Exact source revision for --observed evidence "
            "(default: GITHUB_SHA, or the sentinel unavailable). "
            "Recorded as asserted; not authenticated as a Git SHA."
        ),
    )
    parser.add_argument(
        "--package-identity",
        default=_package_version(),
        help="Exact package identity for --observed evidence",
    )
    parser.add_argument(
        "--workflow-identity",
        default=os.getenv("GITHUB_WORKFLOW_REF", UNAVAILABLE_EVIDENCE_IDENTITY),
        help=(
            "Exact workflow identity for --observed evidence "
            "(default: GITHUB_WORKFLOW_REF, or the sentinel unavailable). "
            "Recorded as asserted; not authenticated as a workflow ref."
        ),
    )
    return parser


def _asserted_observed_identity(value: object) -> str:
    from .sequence import UNAVAILABLE_EVIDENCE_IDENTITY

    if not isinstance(value, str) or not value.strip():
        return UNAVAILABLE_EVIDENCE_IDENTITY
    return value


def _run_evaluate_convergence_command(arguments: list[str]) -> int:
    args = _evaluate_convergence_parser().parse_args(arguments)
    try:
        from .convergence import (
            DEFAULT_REVIEW_MODE,
            resolve_review_convergence_policy,
        )
        from .sequence import (
            ObservedEvidenceIdentity,
            SequenceStep,
            compare_sequence_policies,
            identity_is_unavailable,
            replay_review_sequence,
        )

        policy = resolve_review_convergence_policy(mode=args.review_mode)
        steps = (
            SequenceStep(
                head_sha="a" * 40,
                blocking_identities=("defect-a", "defect-b"),
                expected_material_finding_ids=("material-a",),
                fixture_material_finding_ids=("material-a",),
                label="initial",
            ),
            SequenceStep(
                head_sha="b" * 40,
                blocking_identities=(),
                independently_approval_eligible=True,
                # Fixture emits nothing here. material-a is labelled on the
                # initial step only, so dropping that emission fails the gate.
                expected_material_finding_ids=("material-a",),
                fixture_material_finding_ids=(),
                label="verification-emits-nothing",
            ),
            SequenceStep(
                head_sha="c" * 40,
                blocking_identities=(),
                independently_approval_eligible=True,
                label="second-verification",
            ),
            SequenceStep(
                head_sha="e" * 40,
                blocking_identities=(),
                independently_approval_eligible=True,
                label="third-verification",
            ),
            SequenceStep(
                head_sha="f" * 40,
                blocking_identities=(),
                independently_approval_eligible=True,
                label="fourth-verification",
            ),
            SequenceStep(
                head_sha="1" * 40,
                blocking_identities=(),
                independently_approval_eligible=True,
                label="fifth-verification",
            ),
            SequenceStep(
                head_sha="d" * 40,
                blocking_identities=(),
                independently_approval_eligible=True,
                latest_head_reviewed=True,
                coverage_complete=True,
                label="at-cap",
            ),
        )
        if args.observed and args.compare_default:
            raise ReviewInputError(
                "observed evidence and legacy comparison cannot run in one report"
            )
        if args.observed:
            from .hosting.github.observed import run_observed_review_sequence

            source_identity = _asserted_observed_identity(args.source_identity)
            package_identity = _asserted_observed_identity(args.package_identity)
            workflow_identity = _asserted_observed_identity(args.workflow_identity)
            observed = run_observed_review_sequence(
                steps,
                policy,
                evidence_identity=ObservedEvidenceIdentity(
                    source_identity=source_identity,
                    package_identity=package_identity,
                    workflow_identity=workflow_identity,
                    configuration_digest=policy.digest(),
                    fixture_identity="observed-convergence-fixture-v1",
                    command=(
                        "review-sensei evaluate-convergence --observed "
                        f"--review-mode {policy.mode}"
                    ),
                ),
            )
            payload = observed.to_dict()
            if args.as_json:
                sys.stdout.write(json.dumps(payload, indent=2) + "\n")
            else:
                sys.stdout.write(
                    f"mode={observed.mode} events={len(observed.events)} "
                    f"cutover_status={observed.cutover_status} "
                    f"approval_events={observed.approval_events} "
                    f"cap_created_approval={observed.cap_created_approval}\n"
                )
            unavailable_fields = [
                label
                for label, value in (
                    ("source", source_identity),
                    ("package", package_identity),
                    ("workflow", workflow_identity),
                )
                if identity_is_unavailable(value)
            ]
            if unavailable_fields:
                named = ", ".join(unavailable_fields)
                verb = "is" if len(unavailable_fields) == 1 else "are"
                sys.stderr.write(
                    "observed evidence identity unavailable for "
                    f"{named}; cutover_status cannot pass until {named} {verb} "
                    "explicit\n"
                )
            return 0 if observed.cutover_status == "passed" else 1
        report = replay_review_sequence(steps, policy)
        if args.compare_default:
            payload = compare_sequence_policies(steps, proposed=policy)
            if args.as_json:
                sys.stdout.write(json.dumps(payload, indent=2) + "\n")
            else:
                sys.stdout.write(
                    f"default={payload['publication_default']} "
                    f"proposed={report.mode} handoffs={report.handoffs} "
                    f"cap_created_approval={payload['cap_created_approval']}\n"
                )
            return 0
        if args.as_json:
            sys.stdout.write(json.dumps(report.to_dict(), indent=2) + "\n")
        else:
            sys.stdout.write(
                f"mode={report.mode} default={DEFAULT_REVIEW_MODE} "
                f"handoffs={report.handoffs} cap_created_approval="
                f"{report.cap_created_approval}\n"
            )
        return 0
    except (OSError, ValueError, TypeError, ReviewSenseiError) as exc:
        _print_offline_error(exc)
        return 1


def _config_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review-sensei config",
        description=(
            "Validate and explain the canonical .reviewsensei.yml configuration. "
            "These commands make no inference calls and request no write capability."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, summary in (
        (
            "validate",
            "Validate the configuration and the effective provider/model combination",
        ),
        ("show", "Print the effective configuration"),
    ):
        subparser = subparsers.add_parser(name, help=summary)
        subparser.add_argument(
            "--config",
            type=Path,
            help="Alternative configuration file (default: .reviewsensei.yml)",
        )
        subparser.add_argument(
            "--provider", default=None, help="Preview an invocation backend override"
        )
        subparser.add_argument(
            "--model", default=None, help="Preview an invocation model override"
        )
        if name == "show":
            subparser.add_argument(
                "--explain",
                action="store_true",
                help="Include provenance and the omitted fields' default sources",
            )
    return parser


def _run_config_command(arguments: list[str]) -> int:
    args = _config_parser().parse_args(arguments)
    try:
        from .configuration import (
            ConfigurationError,
            customization_directories,
            load_configuration,
            render_configuration,
            resolve_inference,
            retired_environment_remedies,
        )

        configuration = load_configuration(args.config)
        resolved = resolve_inference(
            configuration,
            cli_provider=args.provider,
            cli_model=args.model,
        )
        retired = retired_environment_remedies(
            os.environ, include_provider_overrides=False
        )
        if retired:
            detail = "; ".join(
                f"{name} ({replacement})" for name, replacement in retired
            )
            raise ConfigurationError(
                f"retired settings have no effect: {detail}. Move them into "
                f"{configuration.source or '.reviewsensei.yml'}",
                source="environment",
            )
        customization = customization_directories(configuration)
        if args.command == "validate":
            print(
                "configuration valid: "
                f"{configuration.source or 'none (packaged defaults)'} "
                f"(schema {configuration.schema}); backend {resolved.backend}; "
                f"model {resolved.model}"
            )
            return 0
        sys.stdout.write(
            render_configuration(
                configuration,
                resolved,
                explain=bool(getattr(args, "explain", False)),
                customization=customization,
            )
        )
        return 0
    except (OSError, ValueError, TypeError, ReviewSenseiError) as exc:
        _print_offline_error(exc)
        return 2


_OFFLINE_COMMANDS = {
    "doctor": _run_doctor_command,
    "plan": _run_plan_command,
    "learnings": _run_learnings_command,
    "evaluate-convergence": _run_evaluate_convergence_command,
}


def main(argv: list[str] | None = None) -> int:
    args_list = list(argv) if argv is not None else sys.argv[1:]
    # The default review command is flag-based, so optional commands cannot be
    # required argparse subparsers. config/doctor/plan/learnings/
    # evaluate-convergence use the same first-token command map as prepare-diff,
    # evaluate, github, and promotion.
    if args_list and args_list[0] in _OFFLINE_COMMANDS:
        return _OFFLINE_COMMANDS[args_list[0]](args_list[1:])
    if args_list and args_list[0] == "config":
        return _run_config_command(args_list[1:])
    if args_list and args_list[0] == "promotion":
        try:
            return _run_promotion(args_list[1:])
        except (
            OSError,
            ValueError,
            KeyError,
            IndexError,
            ReviewSenseiError,
        ) as exc:
            print(f"review-sensei: {exc}", file=sys.stderr)
            return 1
    if args_list and args_list[0] == "resolve-hosted-openrouter":
        args = _resolve_hosted_openrouter_parser().parse_args(args_list[1:])
        if args.version:
            print(_package_version())
            return 0
        from .hosting.openrouter_workflow import emit_hosted_openrouter_resolution

        return emit_hosted_openrouter_resolution(args.action)
    if args_list and args_list[0] == "prepare-diff":
        args = _prepare_diff_parser().parse_args(args_list[1:])
        if args.version:
            print(_package_version())
            return 0
        try:
            if not args.base_ref or not args.head_ref:
                raise ReviewInputError("--base-ref and --head-ref are required")
            prepare_diff(
                base_ref=args.base_ref,
                head_ref=args.head_ref,
                head_repository=args.head_repository,
                repository=args.repository,
                output=args.output,
                max_diff_bytes=args.max_diff_bytes,
                max_diff_lines=args.max_diff_lines,
                max_diff_files=args.max_diff_files,
                max_diff_hunks=args.max_diff_hunks,
            )
            return 0
        except (OSError, ValueError, ReviewSenseiError) as exc:
            print(f"review-sensei: {exc}", file=sys.stderr)
            return 1
    if args_list and args_list[0] == "evaluate":
        evaluate_argv = args_list[1:]
        try:
            args = _evaluate_parser().parse_args(evaluate_argv)
        except ReviewInputError as exc:
            print(f"review-sensei: {exc}", file=sys.stderr)
            return 1
        try:
            return _run_evaluate(args, argv=evaluate_argv)
        except (OSError, ValueError, ReviewSenseiError) as exc:
            print(f"review-sensei: {exc}", file=sys.stderr)
            return 1
    if args_list and args_list[0] == "github":
        github_argv = args_list[1:]
        github_parser = _github_parser()
        args = github_parser.parse_args(github_argv)
        args._explicit_cli_options = _explicit_cli_options(github_parser, github_argv)
        try:
            if getattr(args, "command", None) == "reply":
                _apply_provider_defaults(args, github_argv)
            return _run_github(args, argv=github_argv)
        except (OSError, ValueError, ReviewSenseiError) as exc:
            print(f"review-sensei: {exc}", file=sys.stderr)
            return 1
    try:
        args = _parser().parse_args(args_list)
    except ReviewInputError as exc:
        print(f"review-sensei: {exc}", file=sys.stderr)
        return 1
    try:
        if args.version:
            print(_package_version())
            return 0
        if not args.diff:
            raise ReviewInputError("--diff is required")
        _validate_live_profile_gates(args, args_list)
        hosted_session_ledger = bool(getattr(args, "github_session_ledger", False))
        transaction_requested = (
            bool(getattr(args, "transaction", False))
            or args.configuration_context_output is not None
            or args.admission_context_output is not None
        )
        if not hosted_session_ledger:
            for flag, value in (
                ("--oidc-token", getattr(args, "oidc_token", None)),
                ("--repository-id", getattr(args, "repository_id", None)),
            ):
                if value is not None:
                    raise ReviewInputError(f"{flag} requires --github-session-ledger")
        elif not transaction_requested:
            raise ReviewInputError(
                "--github-session-ledger requires an identity-bound transaction"
            )
        if transaction_requested:
            from .convergence import (
                OPERATOR_REVIEW_MODES,
                resolve_review_convergence_policy,
            )

            if (
                resolve_review_convergence_policy(
                    mode=getattr(args, "review_mode", None)
                ).mode
                not in OPERATOR_REVIEW_MODES
            ):
                raise ReviewInputError("--transaction requires an operator review mode")
            if (
                (args.session_ledger is None and not hosted_session_ledger)
                or not args.repository
                or args.pull_request is None
                or (hosted_session_ledger and not isinstance(args.repository_id, int))
                or not isinstance(args.base_sha, str)
                or len(args.base_sha) != 40
                or any(
                    character not in "0123456789abcdef" for character in args.base_sha
                )
                or not isinstance(args.head_sha, str)
                or len(args.head_sha) != 40
                or any(
                    character not in "0123456789abcdef" for character in args.head_sha
                )
            ):
                raise ReviewInputError(
                    "--transaction requires a session ledger "
                    "(--session-ledger or --github-session-ledger), "
                    "repository/PR identity, and 40-character base/head SHAs"
                )
        provider_name = str(args.provider).strip().lower()
        if provider_name == "fixture":
            if not args.fixture_response:
                raise ReviewInputError("--provider fixture requires --fixture-response")
            api_key = None
        else:
            if args.fixture_response is not None:
                raise ReviewInputError(
                    "--fixture-response is only valid with --provider fixture"
                )
            api_key = _resolve_api_key(args, argv=args_list)
        if args.categories_dir and not args.stages_dir:
            raise ReviewInputError("--categories-dir requires --stages-dir")
        limits = DEFAULT_REVIEW_LIMITS
        orchestrate = bool(getattr(args, "orchestrate_large_changes", False))
        # Read and preflight before loading any provider adapter.  The helper
        # performs a bounded ``maximum + 1`` read and strict UTF-8
        # decoding; the shared analysis validates all diff/path dimensions.
        work_budget = DEFAULT_TOTAL_WORK_BUDGET
        if orchestrate:
            from .planning import plan_change

            diff = read_bounded_utf8(
                args.diff,
                maximum=work_budget.max_total_diff_bytes,
                label="diff",
            )
            analysis = plan_change(
                diff, limits=limits, orchestrate=True, work_budget=work_budget
            ).analysis
        else:
            diff = read_bounded_utf8(
                args.diff,
                maximum=limits.max_diff_bytes,
                label="diff",
            )
            analysis = analyze_diff(diff, limits=limits)
        changed_paths = analysis.changed_paths
        learnings: tuple[LearningEntry, ...] = ()
        if args.learning_root:
            learning_store = load_repository_learnings(
                args.learning_root,
                directory=args.learning_directory,
            )
            learnings = learning_store.for_paths(changed_paths)
        stages = None
        if args.stages_dir:
            from .stages import (
                category_catalog_for_configured_stages,
                load_stages_from_dir,
            )

            stages = load_stages_from_dir(
                args.stages_dir,
                category_catalog=category_catalog_for_configured_stages(
                    args.categories_dir
                ),
            )
        from .convergence import (
            OPERATOR_REVIEW_MODES,
            resolve_review_convergence_policy,
        )
        from .session import (
            SessionIdentity,
            SessionLedger,
            admission_diagnostic,
            checkpoint_review_analysis,
            prepare_review_transaction,
            prepare_session_round,
            record_session_failed_attempt,
            resolve_local_session_ledger,
            session_reservation_id,
            should_skip_automation,
        )

        policy = resolve_review_convergence_policy(
            mode=getattr(args, "review_mode", None)
        )
        provider_settings = _provider_settings_from_args(
            args,
            api_key=api_key,
            fixture_response=args.fixture_response,
            argv=args_list,
        )
        # Normalize the caller-supplied snapshot identity once.  The live
        # inference request intentionally remains unbound; these values are
        # the trusted identity used only for session admission, checkpoint
        # cache keys, and public outcome metadata.
        resolved_base_sha = (args.base_sha or "").strip().lower() or None
        resolved_head_sha = (args.head_sha or "").strip().lower() or None
        effective_profile = (
            get_provider_profile(provider_settings.profile).name
            if provider_settings.profile
            else "default"
        )
        transaction_provider_identity, transaction_model = (
            _transaction_provider_identity(provider_settings)
        )
        ledger: SessionLedger | None = resolve_local_session_ledger(
            getattr(args, "session_ledger", None)
        )
        if ledger is None and hosted_session_ledger:
            assert resolved_head_sha is not None
            assert isinstance(args.repository_id, int)
            from .hosting.github.application import resolve_hosted_session_ledger
            from .hosting.github.broker_client import BrokerClient
            from .hosting.github.http import GitHubHttp

            ledger = resolve_hosted_session_ledger(
                broker=BrokerClient(),
                http=GitHubHttp(),
                oidc_token=getattr(args, "oidc_token", None),
                repository=args.repository,
                repository_id=args.repository_id,
                pull_request=args.pull_request,
                head_sha=resolved_head_sha,
                app_slug=getattr(args, "app_slug", None),
            )
        identity = None
        reservation = None
        held_reservation: str | None = None
        prepared_round = None
        prepared_transaction = None
        transaction_configuration_digest: str | None = None
        transaction_configuration_context: dict[str, object] | None = None
        transaction_evidence_digest: str | None = None

        def cleanup_analysis_reservation(*, charge_failed_attempt: bool) -> None:
            """Clean up only while this invocation still owns the reservation.

            A ledger write can be durable even when its adapter reports an
            exception after the replacement.  In that case checkpointing has
            already advanced the transaction to ``publication_pending`` and
            cleared the reservation; charging a failed attempt would both
            obscure the committed result and risk mutating a later retry.
            Re-read the record before cleanup and preserve any state that no
            longer carries this reservation.
            """

            if (
                ledger is None
                or identity is None
                or held_reservation is None
                or policy.mode not in OPERATOR_REVIEW_MODES
            ):
                return
            loaded = ledger.load(identity)
            if loaded.status not in {"ok", "migrated"} or loaded.record is None:
                raise ReviewInputError(f"session ledger load failed: {loaded.status}")
            current = loaded.record
            if current.reservation_id != held_reservation:
                # The checkpoint may have committed despite an adapter error,
                # or another writer may now own the identity.  Neither state
                # is an analysis reservation this invocation may charge.
                return
            if charge_failed_attempt:
                record_session_failed_attempt(
                    ledger,
                    identity,
                    reservation_id=held_reservation,
                    expected_generation=current.generation,
                )
            else:
                ledger.abort(
                    identity,
                    reservation_id=held_reservation,
                    expected_generation=current.generation,
                )

        def cleanup_analysis_error(
            analysis_error: BaseException, *, charge_failed_attempt: bool
        ) -> None:
            if (
                ledger is None
                or identity is None
                or held_reservation is None
                or policy.mode not in OPERATOR_REVIEW_MODES
            ):
                return
            try:
                cleanup_analysis_reservation(
                    charge_failed_attempt=charge_failed_attempt
                )
            except BaseException as cleanup_error:
                analysis_error.add_note(
                    "analysis reservation cleanup failed: "
                    f"{type(cleanup_error).__name__}: "
                    f"{str(cleanup_error).replace(chr(10), ' ')[:160]}"
                )

        def emit_durable_baseline_recovery() -> int:
            cleanup_analysis_reservation(charge_failed_attempt=False)
            outcome = RunOutcome(
                "action_required",
                repository=args.repository,
                pull_request_number=args.pull_request,
                base_sha=resolved_base_sha,
                head_sha=resolved_head_sha,
                diagnostic="durable_baseline_recovery_required",
                provider_calls=0,
            )
            emit_host_outcome(outcome, output_path=args.outcome)
            print(outcome.status)
            return run_outcome_exit_code(outcome.status)

        if (
            ledger is not None
            and args.repository
            and args.pull_request is not None
            and policy.mode in OPERATOR_REVIEW_MODES
        ):
            if resolved_head_sha is None:
                raise ReviewInputError(
                    "operator-mode session admission requires --head-sha"
                )
            head_sha = resolved_head_sha
            identity = SessionIdentity(
                repository=args.repository,
                pull_request=args.pull_request,
                repository_id=(
                    getattr(args, "repository_id", None)
                    if hosted_session_ledger
                    else None
                ),
            )
            reservation = session_reservation_id(
                repository=args.repository,
                pull_request=args.pull_request,
                head_sha=head_sha,
                kind="publish",
            )
            if transaction_requested:
                effective_base_sha = resolved_base_sha or ""
                effective_stages = stages if stages is not None else DEFAULT_STAGES
                stage_identity, category_policy = transaction_stage_identity(
                    effective_stages
                )
                transaction_configuration_context = (
                    build_transaction_configuration_context(
                        provider=transaction_provider_identity,
                        model=transaction_model,
                        stages=stage_identity,
                        category_policy=category_policy,
                        publication_mode=policy.mode,
                        orchestration_enabled=orchestrate,
                        continue_rounds=getattr(args, "continue_rounds", 0),
                    )
                )
                transaction_configuration_digest = (
                    ReviewTransaction.compute_configuration_digest(
                        transaction_configuration_context
                    )
                )
                # The analysis CLI produces the compatible single-pass result
                # contract only. Confirmed evidence is a separate publication
                # gate and requires a reviewed snapshot plus candidate
                # verification, so it cannot be represented by this path.
                transaction_evidence_digest = ReviewTransaction.compute_evidence_digest(
                    {"evidence_policy": "legacy", "snapshot_sha256": None}
                )
                prepared_round = prepare_review_transaction(
                    ledger,
                    identity,
                    policy,
                    reservation_id=reservation,
                    base_sha=effective_base_sha,
                    head_sha=head_sha,
                    configuration_digest=transaction_configuration_digest,
                    evidence_digest=transaction_evidence_digest,
                    continuation_rounds=getattr(args, "continue_rounds", 0),
                )
                prepared_transaction = prepared_round.transaction
            else:
                prepared_round = prepare_session_round(
                    ledger,
                    identity,
                    policy,
                    reservation_id=reservation,
                    continuation_rounds=getattr(args, "continue_rounds", 0),
                )
            held_reservation = (
                prepared_round.reservation_id if prepared_round.decision.admit else None
            )
            if should_skip_automation(prepared_round.decision, inference=True):
                status = (
                    "action_required"
                    if prepared_round.decision.handoff
                    else "skipped_policy"
                )
                outcome = RunOutcome(
                    status,
                    repository=args.repository,
                    pull_request_number=args.pull_request,
                    base_sha=resolved_base_sha,
                    head_sha=head_sha,
                    diagnostic=admission_diagnostic(prepared_round.decision),
                    provider_calls=0,
                )
                emit_host_outcome(outcome, output_path=args.outcome)
                print(outcome.status)
                return run_outcome_exit_code(outcome.status)
        provider, stage_providers = bind_stage_providers(
            registry=default_registry(),
            settings=provider_settings,
            stages=stages if stages is not None else DEFAULT_STAGES,
        )

        service = ReviewService(
            provider,
            stages=stages,
            stage_providers=stage_providers,
        )
        context_root = args.context_root or args.learning_root
        context_store = (
            RepositoryContextStore(context_root) if context_root is not None else None
        )
        symbol_context_enabled = bool(args.enable_symbol_context) or _env_flag(
            "REVIEWSENSEI_ENABLE_SYMBOL_CONTEXT"
        )
        source_policy = SymbolAwareContextPolicy(enabled=False)
        snapshot = None
        untrusted_head_sha = None
        if symbol_context_enabled:
            allowed_paths = tuple(args.symbol_context_allowed_path) or ("**",)
            source_policy = SymbolAwareContextPolicy(
                enabled=True,
                allowed_paths=allowed_paths,
                max_files=args.symbol_context_max_files,
                max_bytes=args.symbol_context_max_bytes,
                max_depth=args.symbol_context_max_depth,
            )
            base_sha = resolved_base_sha or ""
            if not base_sha:
                raise ReviewInputError(
                    "--enable-symbol-context requires --base-sha for the trusted "
                    "base snapshot"
                )
            snapshot = ContextSnapshot(base_sha, kind="base")
            untrusted_head_sha = resolved_head_sha
        context_selection = build_review_context_selection(
            service.review_categories,
            changed_paths=changed_paths,
            learnings=learnings,
            context_store=context_store,
            source_context_policy=source_policy,
            snapshot=snapshot,
            changed_lines=analysis.changed_lines,
        )

        effective_model = provider_settings.model or provider.model or args.model
        request = ReviewRequest(
            diff=diff,
            repository=args.repository,
            pull_request_number=args.pull_request,
            title=args.title,
            instructions=args.instructions,
            model=effective_model,
            learnings=learnings,
            active_category_ids=context_selection.active_category_ids,
            lens_contexts=context_selection.lens_contexts,
            propose_learnings=args.propose_learnings,
            limits=limits,
            source_context=context_selection.source_context,
            untrusted_head_sha=untrusted_head_sha,
            orchestrate_large_changes=orchestrate,
            work_budget=work_budget,
        )
        incremental = None
        current_key = None
        verification_scope = None
        admission_baseline = None
        # F3 durable-baseline enforcement is explicitly opted into by the
        # identity-bound transaction/artifact request. Legacy operator runs
        # keep their prior full-review behavior until that flag is selected.
        baseline_enforcement_requested = transaction_requested
        if (
            ledger is not None
            and identity is not None
            and prepared_round is not None
            and policy.mode in OPERATOR_REVIEW_MODES
            and baseline_enforcement_requested
            and prepared_round.record.completed_initial_reviews > 0
        ):
            history = prepared_round.record.convergence_history
            if not isinstance(history, dict) or history.get("state") != "completed":
                return emit_durable_baseline_recovery()
            try:
                persisted_baseline = baseline_from_history_document(
                    history.get("baseline")
                )
            except ReviewInputError:
                return emit_durable_baseline_recovery()
            # This round classifies the new head against the durable baseline,
            # so the publication boundary of the same run needs the exact
            # baseline and current key this analysis admitted with. The
            # transaction alone cannot reconstruct either.
            admission_baseline = persisted_baseline
            current_key = build_review_context_cache_key(
                _checkpoint_cache_request(
                    request,
                    base_sha=resolved_base_sha,
                    head_sha=resolved_head_sha,
                ),
                provider_name=service.provider.name,
                stages=service.stages,
                profile=effective_profile,
            )
            scope = plan_verification_scope(
                policy=policy,
                baseline=persisted_baseline,
                current_key=current_key,
                changed_paths=analysis.changed_paths,
            )
            if scope.status != "verify" or scope.incremental is None:
                return emit_durable_baseline_recovery()
            verification_scope = scope
            incremental = scope.incremental
        try:
            run = service.run(
                request,
                incremental=incremental,
                current_key=current_key,
                trusted_base_sha=resolved_base_sha,
                trusted_head_sha=resolved_head_sha,
                profile=effective_profile,
                budget=ResourceBudget.for_limits(limits),
            )
        except (KeyboardInterrupt, SystemExit) as analysis_error:
            cleanup_analysis_error(analysis_error, charge_failed_attempt=False)
            raise
        except Exception as analysis_error:
            cleanup_analysis_error(analysis_error, charge_failed_attempt=True)
            raise
        if (
            run.result is None
            and ledger is not None
            and identity is not None
            and held_reservation is not None
            and policy.mode in OPERATOR_REVIEW_MODES
        ):
            cleanup_analysis_reservation(charge_failed_attempt=True)
        outcome = replace(
            run.outcome,
            base_sha=resolved_base_sha,
            head_sha=resolved_head_sha,
        )
        if run.result is None:
            emit_host_outcome(outcome, output_path=args.outcome)
            if run.error is not None:
                print(f"review-sensei: {run.error}", file=sys.stderr)
            else:
                print(f"review-sensei: {outcome.status}", file=sys.stderr)
            return run_outcome_exit_code(outcome.status)
        result = run.result
        if (
            prepared_transaction is not None
            and ledger is not None
            and identity is not None
        ):
            if prepared_round is None:
                raise ReviewInputError("review transaction admission is incomplete")
            if held_reservation is None:
                raise ReviewInputError("review transaction reservation is missing")
            if result.review_status != "complete":
                # Partial coverage is a valid run outcome, but it is not a
                # publishable transaction. Release the analysis reservation
                # through the bounded failed-attempt path and preserve the
                # partial result for a caller that only wanted a review instead
                # of turning it into a generic checkpoint error.
                cleanup_analysis_reservation(charge_failed_attempt=True)
                prepared_round = None
                prepared_transaction = None
                if (
                    args.configuration_context_output is not None
                    or args.admission_context_output is not None
                ):
                    # A caller that asks for the identity-bound publication
                    # artifacts publishes from them, so it cannot be told the
                    # round succeeded while they are missing. Fail closed
                    # instead, which is what a partial path must do.
                    raise ReviewInputError(
                        "identity-bound analysis produced a "
                        f"{result.review_status} review that cannot be "
                        "checkpointed or published"
                    )
            elif result.evidence_policy != "legacy":
                # The analysis CLI currently produces only the compatible
                # single-pass evidence contract. Confirmed evidence requires
                # a reviewed snapshot and candidate verification at the
                # publication boundary; do not checkpoint a misleading legacy
                # digest for a future result shape.
                cleanup_analysis_reservation(charge_failed_attempt=True)
                prepared_round = None
                prepared_transaction = None
                raise ReviewInputError(
                    "identity-bound analysis currently supports only legacy evidence"
                )
            else:
                cache_key = build_review_context_cache_key(
                    _checkpoint_cache_request(
                        request,
                        base_sha=resolved_base_sha,
                        head_sha=resolved_head_sha,
                    ),
                    provider_name=service.provider.name,
                    stages=service.stages,
                    profile=effective_profile,
                )
                if (
                    args.configuration_context_output is not None
                    or args.admission_context_output is not None
                ):
                    try:
                        if args.configuration_context_output is not None:
                            if transaction_configuration_context is None:
                                raise ReviewInputError(
                                    "configuration context is unavailable"
                                )
                            if (
                                ReviewTransaction.compute_configuration_digest(
                                    transaction_configuration_context
                                )
                                != prepared_transaction.configuration_digest
                            ):
                                raise ReviewInputError(
                                    "configuration context does not match the "
                                    "identity-bound transaction"
                                )
                            args.configuration_context_output.write_text(
                                json.dumps(transaction_configuration_context, indent=2)
                                + "\n",
                                encoding="utf-8",
                            )
                        if args.admission_context_output is not None:
                            # A verification round publishes the exact key it
                            # admitted with, so the artifact can never carry a
                            # key the classification did not see.
                            artifact_key = (
                                current_key if current_key is not None else cache_key
                            )
                            if artifact_key is None:
                                raise ReviewInputError(
                                    "admission context is unavailable"
                                )
                            args.admission_context_output.write_text(
                                json.dumps(
                                    admission_context_document(
                                        admission_baseline, artifact_key
                                    ),
                                    indent=2,
                                )
                                + "\n",
                                encoding="utf-8",
                            )
                        # Persist every trusted artifact before checkpointing
                        # the ledger. A failed write must not leave a durable
                        # transaction that publication cannot reconstruct.
                    except BaseException as cleanup_error:
                        try:
                            cleanup_analysis_reservation(charge_failed_attempt=True)
                        except BaseException as reservation_cleanup_error:
                            cleanup_error.add_note(
                                "analysis reservation cleanup failed: "
                                f"{type(reservation_cleanup_error).__name__}: "
                                f"{str(reservation_cleanup_error).replace(chr(10), ' ')[:160]}"
                            )
                        raise
                try:
                    from .baseline import baseline_from_review
                    from .session import next_session_generation

                    if prepared_round.record is None:
                        raise ReviewInputError(
                            "prepared review transaction has no session record"
                        )
                    verification_related_paths = (
                        verification_scope.related_paths
                        if verification_scope is not None
                        else related_paths_for_change(analysis.changed_paths)
                    )
                    checkpoint_baseline = (
                        baseline_from_review(
                            result,
                            cache_key=cache_key,
                            policy=policy,
                            related_paths=verification_related_paths,
                            generation=next_session_generation(prepared_round.record),
                        )
                        if cache_key is not None
                        else None
                    )
                    result = checkpoint_review_analysis(
                        ledger,
                        identity,
                        prepared_round,
                        result,
                        baseline=(
                            checkpoint_baseline
                            if checkpoint_baseline is not None
                            and checkpoint_baseline.complete
                            else None
                        ),
                    )
                except BaseException as checkpoint_error:
                    if held_reservation is not None:
                        try:
                            cleanup_analysis_reservation(charge_failed_attempt=True)
                        except BaseException as cleanup_error:
                            checkpoint_error.add_note(
                                "analysis reservation cleanup failed: "
                                f"{type(cleanup_error).__name__}: "
                                f"{str(cleanup_error).replace(chr(10), ' ')[:160]}"
                            )
                    raise
        elif args.configuration_context_output is not None:
            raise ReviewInputError(
                "configuration context requires an admitted review transaction"
            )
        elif args.admission_context_output is not None:
            raise ReviewInputError(
                "admission context requires an admitted review transaction"
            )
        emit_host_outcome(outcome, output_path=args.outcome)
        rendered = json.dumps(result.to_dict(), indent=2) + "\n"
        if args.output:
            args.output.write_text(rendered, encoding="utf-8")
        else:
            sys.stdout.write(rendered)
        if args.recovery_artifact:
            if not args.repository or args.pull_request is None:
                raise ReviewInputError(
                    "recovery artifacts require --repository and --pull-request"
                )
            if not outcome.base_sha or not outcome.head_sha:
                raise ReviewInputError(
                    "recovery artifacts require --base-sha and --head-sha"
                )
            artifact = RecoveryArtifact.create(
                repository=args.repository,
                pull_request_number=args.pull_request,
                base_sha=outcome.base_sha,
                head_sha=outcome.head_sha,
                result=result.to_dict(),
                expires_at=recovery_expires_at(ttl_seconds=args.recovery_ttl_seconds),
            )
            args.recovery_artifact.write_text(
                json.dumps(artifact.to_dict(), indent=2) + "\n",
                encoding="utf-8",
            )
        return run_outcome_exit_code(outcome.status)
    except (OSError, ValueError, ReviewSenseiError) as exc:
        print(f"review-sensei: {exc}", file=sys.stderr)
        return 1

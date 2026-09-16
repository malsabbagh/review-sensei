from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import sys
from collections.abc import Iterable
from pathlib import Path

from .context import RepositoryContextStore, build_review_context_selection
from .diff import analyze_diff
from .errors import ReviewInputError, ReviewSenseiError
from .learnings import DEFAULT_LEARNING_DIRECTORY, load_repository_learnings
from .models import LearningEntry, ReviewRequest
from .providers import ProviderSettings, default_registry
from .providers.openai_compatible import is_allowlisted_openai_compatible_endpoint
from .providers.profiles import get_provider_profile
from .service import ReviewService
from .validation import DEFAULT_REVIEW_LIMITS, read_bounded_utf8
from .workflow import prepare_diff

# The parser intentionally narrows the namespace type after argparse parsing;
# the generic overload on argparse.ArgumentParser is broader than this seam.
# mypy: disable-error-code=override

DEFAULT_PROVIDER_MODE = "local"
DEFAULT_LOCAL_MODEL = "qwen3.5:4b"
DEFAULT_CLOUD_MODEL = "deepseek-v4-flash:cloud"
DEFAULT_LOCAL_BASE_URL = "http://127.0.0.1:11434/api"
DEFAULT_CLOUD_BASE_URL = "https://ollama.com/api"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"


def _provider_mode() -> str:
    return (
        os.getenv("REVIEWSENSEI_PROVIDER_MODE", DEFAULT_PROVIDER_MODE).strip().lower()
    )


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
    return os.getenv(args.api_key_env)


def _provider_settings_from_args(
    args: argparse.Namespace,
    *,
    api_key: str | None,
    fixture_response: Path | None = None,
    argv: list[str] | None = None,
) -> ProviderSettings:
    profile_name = getattr(args, "profile", None)
    explicit = getattr(args, "_explicit_cli_options", None)
    if profile_name:
        return ProviderSettings(
            name=str(args.provider).strip().lower(),
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
        )
    _require_allowlisted_openai_endpoint(args)
    return ProviderSettings(
        name=str(args.provider).strip().lower(),
        model=args.model,
        base_url=args.base_url,
        api_key=api_key,
        fixture_response=fixture_response,
        timeout_seconds=args.timeout_seconds,
        allow_custom_endpoint=bool(getattr(args, "allow_custom_endpoint", False)),
    )


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


def _parser() -> argparse.ArgumentParser:
    parser = _ProviderArgumentParser(
        prog="review-sensei",
        description="Run a provider-neutral AI review against a unified diff.",
        epilog=(
            "Additional commands use the same first-token dispatch as "
            "prepare-diff, evaluate, and github: doctor, plan, prepare-diff, "
            "evaluate, github."
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
        help=("Named provider profile (local-private, fast-triage, deep-verification)"),
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
    parser.add_argument("--repository")
    parser.add_argument("--pull-request", type=int)
    parser.add_argument("--title")
    parser.add_argument("--instructions")
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
        help=("Named provider profile (local-private, fast-triage, deep-verification)"),
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
        help="Report optional network checks as unknown (never probes).",
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _plan_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review-sensei plan",
        description="Preview a review execution plan without provider or GitHub calls.",
    )
    parser.add_argument("--diff", type=Path)
    parser.add_argument("--repository")
    parser.add_argument("--pull-request", type=int)
    parser.add_argument("--title")
    parser.add_argument("--stage", action="append", default=[])
    parser.add_argument("--provider-mode")
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _run_evaluate(args: argparse.Namespace, *, argv: list[str]) -> int:
    from .evaluation import (
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
        report = evaluate_fixture(corpus)
    else:
        if not args.allow_live_model:
            raise ReviewInputError("--mode live requires --allow-live-model")
        if not args.provider_version:
            raise ReviewInputError("--mode live requires --provider-version")
        scope = endpoint_scope(args.base_url)
        if scope == "remote" and not args.allow_data_egress:
            raise ReviewInputError(
                "--mode live with a remote endpoint requires --allow-data-egress"
            )
        corpus = load_corpus(args.corpus)
        _validate_profile_cli_args(args, argv)
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
            model=args.model,
            endpoint_scope=scope,
        )
    rendered = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0 if report["passed"] else 1


def _github_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review-sensei github",
        description="Run GitHub publication seams with write opt-ins.",
    )
    _add_allow_custom_endpoint_argument(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    review = subparsers.add_parser("review", help="Publish a validated review result")
    review.add_argument("--result", type=Path, required=True)
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
        help="Publish COMMENT rather than APPROVE after review checks.",
    )
    review.add_argument(
        "--enable-learning-prs",
        action="store_true",
        help="Opt into deterministic draft learning-PR publication.",
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
        help=("Named provider profile (local-private, fast-triage, deep-verification)"),
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
        "--allow-write",
        action="store_true",
        help="Acknowledge that GitHub writes may be enabled for this invocation.",
    )
    reply.add_argument(
        "--enable-reply",
        action="store_true",
        help="Enable mention reply publication for this invocation.",
    )
    reply.add_argument(
        "--enable-auto-approve",
        dest="enable_auto_approve",
        action="store_true",
        default=True,
        help="Finalize approval after an AI-resolved blocking finding (the default).",
    )
    reply.add_argument(
        "--no-auto-approve",
        dest="enable_auto_approve",
        action="store_false",
        help="Resolve the thread without emitting an automatic approval.",
    )
    return parser


def _run_github(args: argparse.Namespace, *, argv: list[str]) -> int:
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
    from .models import ConversationReply, ReviewResult

    if not args.allow_write:
        raise ReviewInputError("github writes require --allow-write")
    broker = BrokerClient()
    http = GitHubHttp()
    application = GitHubApplication(
        broker=broker,
        http=http,
        reviewer=ReviewPublisher(http=http),
        learner=LearningPRPublisher(http=http),
        replier=ConversationPublisher(http=http),
    )
    if args.command == "review":
        result = ReviewResult.from_dict(
            json.loads(
                read_bounded_utf8(args.result, maximum=2_097_152, label="result")
            )
        )
        diff = read_bounded_utf8(args.diff, maximum=1_048_576, label="diff")
        if args.enable_review and (not args.base_branch or not args.base_sha):
            raise ReviewInputError(
                "review publication requires --base-branch and --base-sha"
            )
        review_outcome = application.publish_review(
            options=GitHubWriteOptions(
                github_writes=True,
                auto_review=args.enable_review,
                auto_approve=args.enable_auto_approve,
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
        )
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
        statuses = [review_outcome.status]
        statuses.extend(outcome.status for outcome in learning_outcomes)
        print(" ".join(statuses))
        return 0
    if args.generate:
        if not args.enable_reply:
            print("disabled")
            return 0
        read_token = os.getenv(args.github_token_env)
        if not read_token:
            raise ReviewInputError(
                f"GitHub read token environment variable {args.github_token_env} is unavailable"
            )
        _validate_profile_cli_args(args, argv)
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
                auto_approve=args.enable_auto_approve,
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
            auto_approve=args.enable_auto_approve,
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


def main(argv: list[str] | None = None) -> int:
    args_list = list(argv) if argv is not None else sys.argv[1:]
    # doctor/plan share first-token dispatch with prepare-diff, evaluate, and
    # github because the default review command is flag-based, not a subparser.
    if args_list and args_list[0] == "doctor":
        args = _doctor_parser().parse_args(args_list[1:])
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
            print(f"review-sensei: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:  # pragma: no cover - unexpected diagnostic failure
            print(
                f"review-sensei: unexpected diagnostic failure: {exc}",
                file=sys.stderr,
            )
            return 2
    if args_list and args_list[0] == "plan":
        args = _plan_parser().parse_args(args_list[1:])
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
                provider_mode=args.provider_mode,
            )
            sys.stdout.write(render_diagnostic(report, as_json=args.as_json))
            return 0 if report["status"] == "ready" else 3
        except (OSError, ValueError, TypeError, ReviewSenseiError) as exc:
            print(f"review-sensei: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:  # pragma: no cover - unexpected diagnostic failure
            print(
                f"review-sensei: unexpected diagnostic failure: {exc}",
                file=sys.stderr,
            )
            return 2
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
        _validate_profile_cli_args(args, args_list)
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
        # Read and preflight before loading any provider adapter.  The helper
        # performs a bounded ``max_diff_bytes + 1`` read and strict UTF-8
        # decoding; the shared analysis validates all diff/path dimensions.
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
            from .stages import load_review_categories_from_dir, load_stages_from_dir

            category_catalog = (
                load_review_categories_from_dir(args.categories_dir)
                if args.categories_dir
                else None
            )
            stages = load_stages_from_dir(
                args.stages_dir,
                category_catalog=category_catalog,
            )
        provider = default_registry().create(
            _provider_settings_from_args(
                args,
                api_key=api_key,
                fixture_response=args.fixture_response,
                argv=args_list,
            )
        )

        service = ReviewService(provider, stages=stages)
        context_root = args.context_root or args.learning_root
        context_store = (
            RepositoryContextStore(context_root) if context_root is not None else None
        )
        context_selection = build_review_context_selection(
            service.review_categories,
            changed_paths=changed_paths,
            learnings=learnings,
            context_store=context_store,
        )

        result = service.review(
            ReviewRequest(
                diff=diff,
                repository=args.repository,
                pull_request_number=args.pull_request,
                title=args.title,
                instructions=args.instructions,
                model=args.model,
                learnings=learnings,
                active_category_ids=context_selection.active_category_ids,
                lens_contexts=context_selection.lens_contexts,
                propose_learnings=args.propose_learnings,
                limits=limits,
            )
        )
        rendered = json.dumps(result.to_dict(), indent=2) + "\n"
        if args.output:
            args.output.write_text(rendered, encoding="utf-8")
        else:
            sys.stdout.write(rendered)
        return 0
    except (OSError, ValueError, ReviewSenseiError) as exc:
        print(f"review-sensei: {exc}", file=sys.stderr)
        return 1

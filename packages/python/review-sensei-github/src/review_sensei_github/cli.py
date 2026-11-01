"""Command line for the hosted GitHub runtime."""

from __future__ import annotations

import sys

from review_sensei.cli import (
    _apply_provider_defaults,
    _doctor_parser,
    _explicit_cli_options,
    _print_offline_error,
    _run_evaluate_convergence_command,
    _sigterm_as_keyboard_interrupt,
)
from review_sensei.diagnostics import (
    DOCTOR_ACTION_REQUIRED,
    DOCTOR_UNKNOWN,
    render_diagnostic,
    run_doctor,
)
from review_sensei.errors import ReviewSenseiError
from review_sensei_github.commands import (
    github_parser,
    report_broker_failure,
    resolve_hosted_openrouter_parser,
    run_github,
    run_host_plan,
)
from review_sensei_github.doctor import with_github_checks
from review_sensei_github.observed import run_observed_review_sequence
from review_sensei_github.openrouter_workflow import emit_hosted_openrouter_resolution

_PUBLIC = (
    "review",
    "review-run",
    "restart-run",
    "restart-handoff",
    "restart-caller",
    "command-run",
    "reply-run",
    "publish",
    "reply",
    "command",
    "host-plan",
    "resolve-hosted-openrouter",
    "evaluate-observed",
    "release-reservation",
    "doctor",
    "setup",
)
_HELP = {
    "review": "engine review plus hosted hooks",
    "review-run": "one metered review lifecycle",
    "restart-run": "one registered serialized fresh review",
    "restart-handoff": "offline evidence mapping for a current-head human review",
    "restart-caller": "print an explicit manual launcher caller for review",
    "command-run": "one metered maintainer command lifecycle",
    "reply-run": "one metered mention reply lifecycle",
    "publish": "publish a prepared review",
    "reply": "publish a reply",
    "command": "run a maintainer command",
    "host-plan": "resolve the hosted runner plan",
    "resolve-hosted-openrouter": "resolve the hosted OpenRouter model",
    "evaluate-observed": "evaluate an observed review",
    "release-reservation": "release this run's own session reservation",
    "doctor": "run hosted diagnostics",
    "setup": "inspect, diff, or migrate the installed caller workflow",
}


def _usage() -> str:
    width = max(len(name) for name in _PUBLIC)
    lines = ["usage: review-sensei-github <command> ..."]
    lines.extend(f"  {name.ljust(width)}  {_HELP[name]}" for name in _PUBLIC)
    return "\n".join(lines)


def _normalize(argv: list[str]) -> list[str]:
    """Current command spelling only; historical aliases confer no authority."""
    return list(argv)


def _publication(args: list[str]) -> int:
    parser = github_parser()
    parsed = parser.parse_args(args)
    parsed._explicit_cli_options = _explicit_cli_options(
        parser, args, subcommand=parsed.command
    )
    try:
        if getattr(parsed, "command", None) == "reply":
            _apply_provider_defaults(parsed, args)
        return run_github(parsed, argv=args)
    except (OSError, ValueError, ReviewSenseiError) as exc:
        if not report_broker_failure(exc, parsed):
            print(f"review-sensei: {exc}", file=sys.stderr)
        return 1


def _doctor(arguments: list[str]) -> int:
    parsed = _doctor_parser().parse_args(arguments)
    try:
        report = with_github_checks(
            run_doctor(
                config=parsed.config,
                stages_dir=parsed.stages_dir,
                categories_dir=parsed.categories_dir,
                context_root=parsed.context_root,
                include_network=parsed.network,
                review_mode=parsed.review_mode,
                repository=parsed.repository,
                profile=parsed.profile,
                provider=parsed.provider,
                base_url=parsed.base_url,
                allow_custom_endpoint=parsed.allow_custom_endpoint,
                model=parsed.model,
                api_key_env=parsed.api_key_env,
                compatibility_manifest=parsed.compatibility_manifest,
                allow_data_egress=parsed.allow_data_egress,
                session_ledger=parsed.session_ledger,
                pull_request=parsed.pull_request,
            ),
            context_root=parsed.context_root,
        )
        sys.stdout.write(render_diagnostic(report, as_json=parsed.as_json))
        if report["status"] == "action":
            return DOCTOR_ACTION_REQUIRED
        if report["status"] == "unknown":
            return DOCTOR_UNKNOWN
        return 0
    except (OSError, ValueError, TypeError, ReviewSenseiError) as exc:
        _print_offline_error(exc)
        return 2


def _resolve_openrouter(arguments: list[str]) -> int:
    parsed = resolve_hosted_openrouter_parser().parse_args(arguments)
    return emit_hosted_openrouter_resolution(parsed.action)


def _release_reservation(arguments: list[str]) -> int:
    print(
        "review-sensei: separate cleanup is retired; use the owning lifecycle",
        file=sys.stderr,
    )
    return 1


def _setup(arguments: list[str]) -> int:
    import argparse
    import json
    from pathlib import Path

    from review_sensei_github.setup import (
        DEFAULT_PUBLIC_WORKFLOW_TAG,
        WORKFLOW_PATH,
        inspect_installed_caller_drift,
        migrate_caller_preserving_customizations,
    )

    parser = argparse.ArgumentParser(
        prog="review-sensei-github setup",
        description=(
            "Inspect, diff, or migrate the installed ReviewSensei caller workflow "
            "while preserving job customizations."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify whether the installed caller workflow matches the current contract.",
    )
    parser.add_argument(
        "--diff",
        action="store_true",
        help="Print a unified diff migrating the installed caller workflow.",
    )
    parser.add_argument(
        "--migrate",
        action="store_true",
        help="Migrate the installed caller workflow while preserving customizations.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write the migrated caller workflow in place when used with --migrate.",
    )
    parser.add_argument(
        "--context-root",
        type=Path,
        default=Path("."),
        help="Repository root containing .github/workflows/review-sensei-review.yml.",
    )
    parser.add_argument(
        "--workflow-file",
        type=Path,
        default=None,
        help="Explicit path to a caller workflow file to inspect or migrate.",
    )
    parser.add_argument(
        "--public-workflow-tag",
        default=DEFAULT_PUBLIC_WORKFLOW_TAG,
        help="Reusable workflow tag (default: v5).",
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    parsed = parser.parse_args(arguments)
    if parsed.write and not parsed.migrate:
        parser.error("--write requires --migrate")
    try:
        workflow_path = (
            parsed.workflow_file
            if parsed.workflow_file is not None
            else (parsed.context_root / WORKFLOW_PATH)
        )
        content = workflow_path.read_text(encoding="utf-8")
        drift = inspect_installed_caller_drift(
            content, public_workflow_tag=parsed.public_workflow_tag
        )
        if parsed.migrate or parsed.diff:
            migrated, diff_text = migrate_caller_preserving_customizations(
                content, public_workflow_tag=parsed.public_workflow_tag
            )
            if parsed.write:
                workflow_path.write_text(migrated, encoding="utf-8")
            if parsed.as_json:
                sys.stdout.write(
                    json.dumps(
                        {
                            "drift": drift.to_dict(),
                            "diff": diff_text,
                            "migrated": migrated,
                            "written": bool(parsed.write),
                        },
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n"
                )
            elif parsed.diff:
                sys.stdout.write(diff_text)
            elif parsed.write:
                sys.stdout.write(diff_text or f"{WORKFLOW_PATH}: already current\n")
            else:
                sys.stdout.write(migrated)
            if parsed.check and not drift.is_current and not parsed.write:
                return DOCTOR_ACTION_REQUIRED
            return 0
        if parsed.as_json:
            sys.stdout.write(
                json.dumps(drift.to_dict(), indent=2, sort_keys=True) + "\n"
            )
        elif drift.is_current:
            sys.stdout.write(
                f"{WORKFLOW_PATH}: current (contract {drift.contract_version})\n"
            )
        else:
            stale = ", ".join(drift.stale_sections) or "setup_version"
            sys.stdout.write(
                f"{WORKFLOW_PATH}: stale (contract {drift.contract_version}; "
                f"stale={stale})\n"
            )
        return 0 if drift.is_current else DOCTOR_ACTION_REQUIRED
    except (OSError, ValueError, TypeError, ReviewSenseiError) as exc:
        _print_offline_error(exc)
        return 2


def main(argv: list[str] | None = None) -> int:
    try:
        with _sigterm_as_keyboard_interrupt():
            return _main(argv)
    except KeyboardInterrupt:
        print("review-sensei: cancelled", file=sys.stderr)
        return 130


def _main(argv: list[str] | None = None) -> int:
    args = _normalize(list(argv) if argv is not None else sys.argv[1:])
    if not args or args[0] in {"-h", "--help"}:
        print(_usage(), file=sys.stderr)
        return 0 if args and args[0] in {"-h", "--help"} else 2
    command, rest = args[0], args[1:]
    if command == "restart-caller":
        import argparse

        from .controlled_restart_caller import restart_caller

        parser = argparse.ArgumentParser(
            description="Print source only; no install, grant or dispatch."
        )
        parser.add_argument("--public-workflow-tag", default="v5")
        options = parser.parse_args(rest)
        try:
            print(restart_caller(options.public_workflow_tag), end="")
            return 0
        except (ValueError, ReviewSenseiError) as error:
            print(f"review-sensei: {error}", file=sys.stderr)
            return 1
    if command == "restart-handoff":
        from .controlled_restart_review import main as handoff

        try:
            return handoff(rest)
        except (OSError, ValueError, ReviewSenseiError) as error:
            print(f"review-sensei: {error}", file=sys.stderr)
            return 1
    if command in {"command-run", "reply-run"}:
        from .action_run import run_action
        from .review_lifecycle_diagnostics import lifecycle

        with lifecycle(command.removesuffix("-run")) as diagnostic:
            try:
                return diagnostic.returned(
                    run_action(rest, purpose=command.removesuffix("-run"))
                )
            except (OSError, ValueError, ReviewSenseiError) as error:
                diagnostic.failed(error)
                print(f"review-sensei: {error}", file=sys.stderr)
                return diagnostic.returned(1)
    if command.startswith("-") or command in {"publish", "reply", "command"}:
        return _publication(args)
    if command in {"review-run", "restart-run"}:
        from .review_lifecycle_diagnostics import lifecycle
        from .review_run import run_review

        with lifecycle(command.removesuffix("-run")) as diagnostic:
            try:
                return diagnostic.returned(
                    run_review(rest, controlled_restart=command == "restart-run")
                )
            except (OSError, ValueError, ReviewSenseiError) as error:
                diagnostic.failed(error)
                print(f"review-sensei: {error}", file=sys.stderr)
                return diagnostic.returned(1)
    if command == "review":
        print(
            "review-sensei: hosted phase entry is retired; use review-run",
            file=sys.stderr,
        )
        return 1
    if command == "host-plan":
        return run_host_plan(rest)
    if command == "resolve-hosted-openrouter":
        return _resolve_openrouter(rest)
    if command == "evaluate-observed":
        return _run_evaluate_convergence_command(
            ["--observed", *rest], observed_runner=run_observed_review_sequence
        )
    if command == "release-reservation":
        return _release_reservation(rest)
    if command == "doctor":
        return _doctor(rest)
    if command == "setup":
        return _setup(rest)
    print(
        f"review-sensei: unknown command {command}",
        file=sys.stderr,
    )
    return 2

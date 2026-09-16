"""Emit or validate a promotion-record from evaluation report files.

This operator tool never calls a live provider and never reads credentials.
Ordinary CI remains fixture-only; fixture reports cannot mint status=supported.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from review_sensei.errors import ReviewInputError, ReviewSenseiError  # noqa: E402
from review_sensei.evaluation import (  # noqa: E402
    load_evaluation_report,
    promotion_record_from_reports,
    require_supported_promotion,
    validate_promotion_against_report,
    validate_promotion_record,
)
from review_sensei.validation import read_bounded_utf8  # noqa: E402

MAX_PROMOTION_RECORD_BYTES = 256 * 1024


def _parse_reproducibility_json(value: str) -> dict[str, object]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ReviewInputError("reproducibility must be a JSON object") from exc
    if not isinstance(parsed, dict):
        raise ReviewInputError("reproducibility must be a JSON object")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    emit = subparsers.add_parser(
        "emit",
        help="Build a promotion-record from independent evaluation reports",
    )
    emit.add_argument("--report", type=Path, action="append", required=True)
    emit.add_argument("--observed-revision", required=True)
    emit.add_argument("--evaluated-at", required=True)
    emit.add_argument("--reproducibility-json", required=True)
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


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        reports = [load_evaluation_report(path) for path in args.report]
        if args.command == "emit":
            record = promotion_record_from_reports(
                reports,
                observed_revision=args.observed_revision,
                reproducibility=_parse_reproducibility_json(args.reproducibility_json),
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
        value = json.loads(
            read_bounded_utf8(
                args.record,
                maximum=MAX_PROMOTION_RECORD_BYTES,
                label="promotion record",
            )
        )
        record = validate_promotion_record(value)
        if args.require_supported:
            require_supported_promotion(record, reports)
        else:
            for report in reports:
                validate_promotion_against_report(record, report)
        sys.stdout.write(json.dumps(record.to_dict(), indent=2) + "\n")
        return 0
    except (OSError, ValueError, ReviewSenseiError) as exc:
        print(f"promotion record validation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

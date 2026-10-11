#!/usr/bin/env python3
"""Supplemental boundaries unavailable as configurable console options."""

import argparse
import dataclasses
import hashlib
import importlib.metadata
import json
import sys
from pathlib import Path

from review_sensei.budgets import ProviderCapabilities, ReviewWorkBudgets
from review_sensei.human_assessment import HumanAssessmentDecision, HumanAssessmentReply
from review_sensei.models import (
    ConversationContext,
    ConversationMessage,
    ConversationReply,
    ReviewDocument,
    ReviewRequest,
)
from review_sensei.outcomes import ResourceBudget
from review_sensei.validation import ReviewLimits, TotalWorkBudget


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--expected-version", required=True)
    p.add_argument("--report", type=Path, required=True)
    a = p.parse_args()
    assert importlib.metadata.version("review-sensei") == a.expected_version
    a.report.mkdir(parents=True, exist_ok=True)
    rows = []
    inventory = []

    def check(id, group, fn, accepted=True):
        try:
            fn()
            actual = True
            error = None
        except Exception as e:
            actual = False
            error = type(e).__name__ + ": " + str(e)
        rows.append(
            {
                "id": id,
                "group": group,
                "status": "PASS" if actual == accepted else "FAIL",
                "expected_accept": accepted,
                "actual_accept": actual,
                "error": error,
                "level": "installed-seam",
            }
        )

    for cls in [ReviewLimits, TotalWorkBudget, ResourceBudget]:
        defaults = dataclasses.asdict(cls())
        ceilings = {**defaults}
        if cls is TotalWorkBudget:
            ceilings.update(max_chunks=32, max_provider_calls=32)
        if cls is ResourceBudget:
            ceilings.update(
                max_provider_calls=32, timeout_ms=600000, max_prompt_bytes=4194304
            )
        for field, default in defaults.items():
            maximum = ceilings[field]
            minimum = 0 if cls is ResourceBudget else 1
            inventory.append(
                {
                    "id": cls.__name__ + "." + field,
                    "default": default,
                    "minimum": minimum,
                    "maximum": maximum,
                    "entrypoint": "ResourceBudget.create"
                    if cls is ResourceBudget
                    else cls.__name__,
                    "precedence": "intersected with ReviewLimits/resource budget; only supported YAML fields change CLI values",
                    "level": "installed-seam",
                }
            )
            for label, value in [
                ("unset", None),
                ("minimum", minimum),
                ("default", default),
                ("below", maximum - 1),
                ("at", maximum),
                ("above", maximum + 1),
                ("zero", 0),
                ("negative", -1),
                ("boolean", True),
                ("float", 1.5),
                ("string", "1"),
            ]:
                kwargs = {} if value is None else {field: value}
                accepted = (
                    value is None or type(value) is int and minimum <= value <= maximum
                )
                factory = ResourceBudget.create if cls is ResourceBudget else cls
                check(
                    cls.__name__ + "." + field + "-" + label,
                    "numeric-construction",
                    lambda f=factory, k=kwargs: f(**k),
                    accepted,
                )
    # Capabilities are exact-identity, qualified only; context tokens are never
    # inferred from model names or bytes/4 approximations.
    cap_defaults = {
        "context_tokens": 20000,
        "max_output_tokens": 4096,
        "framing_tokens": 256,
    }
    for field, maximum in [
        ("context_tokens", 4194304),
        ("max_output_tokens", 16384),
        ("framing_tokens", 16384),
    ]:
        for value in [0, 1, maximum - 1, maximum, maximum + 1, -1, True, "1", 1.5]:
            kw = {**cap_defaults, field: value}
            kw["context_tokens"] = (
                kw["context_tokens"] if field == "context_tokens" else 4194304
            )
            ok = (
                type(value) is int
                and 1 <= value <= maximum
                and (field != "context_tokens" or value > 4096 + 256)
            )
            check(
                "ProviderCapabilities." + field + "-" + repr(value),
                "provider-capability",
                lambda k=kw: ProviderCapabilities(**k),
                ok,
            )
    cap = ProviderCapabilities(
        **cap_defaults,
        qualified=True,
        provider_name="fixture",
        model="synthetic",
        endpoint="https://fixture.example/api",
        completion_contract="bounded-complete-text-v1",
    )
    threshold = 20000 - 4096 - 256 - 1000
    for n in [threshold - 1, threshold, threshold + 1]:
        check(
            "capability-fit-" + str(n),
            "provider-capacity",
            lambda n=n: assert_value(cap.fits("x" * n), n <= threshold),
        )
    for output in [1, 767, 768, 2303, 2304, 4096, 16384]:
        effective = ReviewWorkBudgets(mode="unified").effective(
            limits=ReviewLimits(),
            resource=ResourceBudget.create(),
            mode="reassessment",
            output_tokens=output,
        )
        expected = min(4, max(0, (output - 256) // 512))
        check(
            "reassessment-output-packing-" + str(output),
            "provider-capacity",
            lambda e=effective, x=expected: assert_value(e.max_findings_per_batch, x),
        )
    for field, maximum in [
        ("repository", 512),
        ("title", 4096),
        ("instructions", 65536),
        ("model", 256),
    ]:
        for n in [0, 1, maximum - 1, maximum, maximum + 1]:
            check(
                "ReviewRequest." + field + "-" + str(n),
                "request-text",
                lambda f=field, n=n: ReviewRequest(
                    diff="synthetic diff", **{f: "x" * n}
                ),
                n <= maximum,
            )
    for field, maximum in [
        ("metadata-key", 128),
        ("metadata-value", 4096),
        ("metadata-count", 64),
    ]:
        for n in [0, 1, maximum - 1, maximum, maximum + 1]:
            md = (
                {"x" * n: "v"}
                if field == "metadata-key"
                else {"k": "x" * n}
                if field == "metadata-value"
                else {str(i): "v" for i in range(n)}
            )
            check(
                field + "-" + str(n),
                "request-metadata",
                lambda md=md: ReviewRequest(diff="synthetic diff", metadata=md),
                n <= maximum and (field != "metadata-key" or n > 0),
            )
    for field, maximum in [
        ("pull_request_body", 8192),
        ("diff_context", 16384),
        ("pull_request_title", 4096),
    ]:
        for n in [0, 1, maximum - 1, maximum, maximum + 1]:
            check(
                "ConversationContext." + field + "-" + str(n),
                "reply-context",
                lambda f=field, n=n: ConversationContext(**{f: "x" * n}),
                0 < n <= maximum,
            )
    for n in [0, 1, 19, 20, 21]:
        check(
            "conversation-messages-" + str(n),
            "reply-context",
            lambda n=n: ConversationContext(
                messages=tuple(
                    ConversationMessage(author="a", body="b", created_at="created")
                    for _ in range(n)
                )
            ),
            n <= 20,
        )
    for n in [0, 1, 16383, 16384, 16385]:
        check(
            "conversation-reply-" + str(n),
            "reply-result",
            lambda n=n: ConversationReply(body="x" * n),
            0 < n <= 16384,
        )
    for n in [131071, 131072, 131073]:
        check(
            "review-document-" + str(n),
            "context-document",
            lambda n=n: ReviewDocument(
                path="x.md",
                content="x" * n,
                sha256=hashlib.sha256(("x" * n).encode()).hexdigest(),
            ),
            n <= 131072,
        )
    for field, maximum in [
        ("rationale", 1024),
        ("human_evidence", 512),
        ("diff_evidence", 512),
    ]:
        for n in [0, 1, maximum - 1, maximum, maximum + 1]:
            kw = {
                "fingerprint": "a" * 64,
                "decision": "unresolved",
                "rationale": "r",
                "human_evidence": "h",
                "diff_evidence": "d",
                field: "x" * n,
            }
            check(
                "human-decision-" + field + "-" + str(n),
                "human-reassessment",
                lambda k=kw: HumanAssessmentDecision(**k),
                n <= maximum,
            )
    for n in [0, 1, 4095, 4096, 4097]:
        check(
            "human-reply-body-" + str(n),
            "human-reassessment",
            lambda n=n: HumanAssessmentReply(body="x" * n, decisions=()),
            0 < n <= 4096,
        )
    for n in [249, 250, 251]:
        decision = HumanAssessmentDecision("a" * 64, "unresolved", "", "", "")
        check(
            "human-reply-decisions-" + str(n),
            "human-reassessment",
            lambda n=n: HumanAssessmentReply(
                body="synthetic", decisions=tuple(decision for _ in range(n))
            ),
            n <= 250,
        )
    data = {
        "version": a.expected_version,
        "total": len(rows),
        "pass": sum(r["status"] == "PASS" for r in rows),
        "fail": sum(r["status"] == "FAIL" for r in rows),
        "rows": rows,
        "inventory": inventory,
    }
    (a.report / "results.json").write_text(json.dumps(data, indent=2))
    print(
        json.dumps(
            {k: v for k, v in data.items() if k not in ["rows", "inventory"]}, indent=2
        )
    )
    for r in rows:
        if r["status"] == "FAIL":
            print(json.dumps(r))
    return bool(data["fail"])


def assert_value(actual, expected):
    if actual != expected:
        raise AssertionError(f"{actual!r} != {expected!r}")


if __name__ == "__main__":
    sys.exit(main())

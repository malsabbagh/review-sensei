#!/usr/bin/env python3
"""Deterministic installed-CLI qualification; stdlib only, no credentials.

Run with the interpreter from a clean environment containing review-sensei.
Each case executes that environment's console script in a separate synthetic
working directory. Exit 1 means at least one expectation failed; gaps are
reported separately and never counted as passing assertions.
"""

import argparse
import concurrent.futures
import csv
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

PATCH = (
    "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-x = 0\n+x = 1\n"
)
CLEAN = {"summary": "Synthetic clean", "comments": [], "learning_proposals": []}
NUMERIC = {
    "advanced.context.symbol_context.max_files": (16, 16, "int"),
    "advanced.context.symbol_context.max_bytes": (131072, 131072, "int"),
    "advanced.context.symbol_context.max_depth": (1, 1, "int"),
    "advanced.resources.timeout_seconds": (900, 3600, "float"),
    "advanced.resources.max_provider_calls": (8, 32, "int"),
    "advanced.resources.review_deadline_seconds": (120, 600, "int"),
    "advanced.review_work.batch_diff_bytes": (131072, 1048576, "int"),
    "advanced.review_work.batch_prompt_bytes": (262144, 4194304, "int"),
    "advanced.review_work.max_total_prompt_bytes": (2097152, 2097152, "int"),
    "advanced.review_work.max_total_output_bytes": (1048576, 1048576, "int"),
}
BOOLS = {
    "github.automatic_reviews": True,
    "github.writes": False,
    "github.mentions": True,
    "advanced.endpoint.allow_custom_endpoint": False,
    "advanced.context.symbol_context.enabled": False,
    "advanced.egress.allow_data_egress": False,
    "advanced.large_changes.orchestrate": False,
}
ENUMS = {
    "github.reviews": ["auto-approve", "blocking", "advisory"],
    "github.learning": ["disabled", "proposals", "pull-requests"],
    "github.artifacts": ["none", "diagnostics"],
    "github.operation_entry": ["enabled", "disabled"],
    "advanced.review_work.mode": ["legacy", "unified"],
    "inference.backend": [
        "local-ollama",
        "cloud-ollama",
        "openai-compatible",
        "openrouter",
    ],
}


def yaml_fields(fields):
    tree = {"schema": 1}
    for key, value in fields.items():
        cur = tree
        bits = key.split(".")
        for bit in bits[:-1]:
            cur = cur.setdefault(bit, {})
        cur[bits[-1]] = value

    def render(obj, indent=0):
        lines = []
        for k, v in obj.items():
            if isinstance(v, dict):
                lines += [" " * indent + k + ":"] + render(v, indent + 2)
            elif isinstance(v, list):
                lines += [" " * indent + k + ":"] + [
                    " " * (indent + 2) + "- " + json.dumps(x) for x in v
                ]
            else:
                lines += [" " * indent + k + ": " + json.dumps(v)]
        return lines

    return "\n".join(render(tree)) + "\n"


def block():
    return {
        "summary": "Synthetic required fix",
        "comments": [
            {
                "path": "x.py",
                "line": 1,
                "body": "Synthetic concrete defect",
                "blocking": True,
                "severity": "high",
                "fix_effort": "small",
                "category": "correctness",
            }
        ],
        "learning_proposals": [],
    }


def one_file(size=None, lines=1, path="x.py"):
    prefix = f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -0,0 +1,{lines} @@\n"
    if size is not None:
        assert size >= len(prefix) + 2
        return prefix + "+" + "x" * (size - len(prefix) - 2) + "\n"
    return prefix + ("+x\n" * lines)


def many_files(count):
    return "".join(one_file(path=f"f{i}.py") for i in range(count))


def many_hunks(count):
    return "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n" + "".join(
        f"@@ -{i},0 +{i},1 @@\n+x\n" for i in range(1, count + 1)
    )


def cases():
    out = []

    def add(id, group, argv=None, expected=0, **kw):
        out.append(dict(id=id, group=group, argv=argv, expected=expected, **kw))

    # Acceptance uses config validate to avoid spending inference work on maxima.
    for field, (default, maximum, kind) in NUMERIC.items():
        vals = {
            "unset": None,
            "minimum": 0.001 if kind == "float" else 1,
            "default": default,
            "maximum": maximum,
            "below": maximum - 1,
            "above": maximum + 1,
            "zero": 0,
            "negative": -1,
            "string": "12",
            "boolean": True,
            "fraction": 1.5,
            "null": "NULL",
        }
        for label, v in vals.items():
            accepted = (
                v is None
                or v == "NULL"
                or (
                    type(v) in ([int, float] if kind == "float" else [int])
                    and v > 0
                    and v <= maximum
                )
            )
            fields = {} if v is None else {field: None if v == "NULL" else v}
            add(
                field + "-" + label,
                "yaml-numeric",
                ["config", "validate", "--config", "config.yml"],
                0 if accepted else 2,
                fields=fields,
            )
    for field, default in BOOLS.items():
        for label, v, ok in [
            ("unset", None, True),
            ("true", True, True),
            ("false", False, True),
            ("string", "true", False),
            ("zero", 0, False),
            ("negative", -1, False),
            ("null", "NULL", True),
        ]:
            add(
                field + "-" + label,
                "yaml-bool",
                ["config", "validate", "--config", "config.yml"],
                0 if ok else 2,
                fields={} if v is None else {field: None if v == "NULL" else v},
            )
    for field, legal in ENUMS.items():
        for v in legal + ["invalid", 0, False, None]:
            add(
                field + "-" + str(v),
                "yaml-enum",
                ["config", "validate", "--config", "config.yml"],
                0 if v in legal or v is None else 2,
                fields={field: v},
            )
    for field, valid, invalid in [
        ("inference.model", ["ab", "fixture-v1"], ["", True, 1]),
        (
            "advanced.endpoint.credential_env",
            ["A", "A" * 64],
            ["A" * 65, "lower", "A-B", "", True],
        ),
        (
            "advanced.routing.upstream_provider",
            ["a", "a" * 64],
            ["a" * 65, "UPPER", "../x", 1],
        ),
        (
            "advanced.context.symbol_context.allowed_paths",
            [["x.py"], ["x" * 256]],
            [["x" * 257], ["../x"], ["/x"], "x", [], [True]],
        ),
    ]:
        # Empty sequence syntax is not supported by this bounded YAML writer.
        for index, v in enumerate(valid + invalid):
            add(
                field + "-" + str(index),
                "yaml-format",
                ["config", "validate", "--config", "config.yml"],
                0 if index < len(valid) or v == [] else 2,
                fields={
                    **(
                        {
                            "inference.backend": "openrouter",
                            "inference.model": "custom/model",
                        }
                        if field == "advanced.routing.upstream_provider"
                        else {"inference.backend": "openai-compatible"}
                        if field == "advanced.endpoint.credential_env"
                        else {}
                    ),
                    field: v,
                },
            )
    for name, text in {
        "duplicate": "schema: 1\nschema: 1\n",
        "unknown": "schema: 1\nunknown: true\n",
        "tab": "schema: 1\ninference:\n\tbackend: local-ollama\n",
        "alias": "schema: 1\ninference: &x {}\n",
        "invalid-utf8": b"schema: 1\n\xff",
        "bom": b"\xef\xbb\xbfschema: 1\n",
        "schema-zero": "schema: 0\n",
        "schema-string": 'schema: "1"\n',
        "retired": "schema: 1\nprovider: ollama\n",
    }.items():
        add(
            "config-" + name,
            "yaml-parser",
            ["config", "validate", "--config", "config.yml"],
            2,
            config=text,
        )
    for n in [65535, 65536, 65537]:
        text = "schema: 1\n" + ("#" + ("x" * (n - 12)) + "\n")
        assert len(text) == n
        add(
            "config-bytes-" + str(n),
            "yaml-parser",
            ["config", "validate", "--config", "config.yml"],
            0 if n <= 65536 else 2,
            config=text,
        )
    for n in [1999, 2000, 2001]:
        add(
            "config-lines-" + str(n),
            "yaml-parser",
            ["config", "validate", "--config", "config.yml"],
            0 if n <= 2000 else 2,
            config="schema: 1\n" + "#\n" * (n - 1),
        )
    for n in [4093, 4094, 4095, 4096, 4097]:
        add(
            "config-scalar-" + str(n),
            "yaml-parser",
            ["config", "validate", "--config", "config.yml"],
            0 if n <= 4094 else 2,
            fields={"inference.model": "a" * n},
        )
    for n in [127, 128, 129]:
        add(
            "config-sequence-" + str(n),
            "yaml-parser",
            ["config", "validate", "--config", "config.yml"],
            0 if n <= 128 else 2,
            fields={"advanced.context.symbol_context.allowed_paths": ["x.py"] * n},
        )
    for field, limit in [
        ("title", 4096),
        ("instructions", 65536),
        ("repository", 512),
        ("model", 256),
    ]:
        for n in [0, 1, limit - 1, limit, limit + 1]:
            args = ["--" + field, "a" * n]
            add(
                "input-" + field + "-" + str(n),
                "input-text",
                extra=args,
                expected=0
                if n <= limit
                and (field != "model" or n > 0)
                and (field != "repository" or 0 < n <= 256)
                else 2,
            )
    for semantics in ["review", "operational"]:
        for fmt in ["json", "text", "markdown"]:
            for status in ["clean", "blocking", "partial"]:
                add(
                    "render-" + semantics + "-" + fmt + "-" + status,
                    "output-exit",
                    response=block() if status == "blocking" else CLEAN,
                    patch=PATCH
                    if status != "partial"
                    else "diff --git a/a.png b/a.png\nBinary files a/a.png and b/a.png differ\n",
                    extra=["--format", fmt, "--exit-semantics", semantics],
                    expected=1
                    if status == "blocking" and semantics == "review"
                    else 2
                    if status == "partial" and semantics == "review"
                    else 0,
                    check_complete=status != "partial",
                    fmt=fmt,
                )
    for n in [1048575, 1048576, 1048577]:
        add("diff-bytes-" + str(n), "diff", patch=one_file(n), expected=2)
    for n in [49999, 50000, 50001]:
        # Count includes the four file headers plus one hunk header.
        add(
            "diff-lines-" + str(n),
            "diff",
            patch=one_file(lines=n - 4),
            expected=0 if n <= 50000 else 2,
        )
    for n in [499, 500, 501]:
        add(
            "diff-files-" + str(n),
            "diff",
            patch=many_files(n),
            expected=0 if n <= 500 else 2,
        )
    for n in [4999, 5000, 5001]:
        add(
            "diff-hunks-" + str(n),
            "diff",
            patch=many_hunks(n),
            expected=0 if n <= 5000 else 2,
        )
    for name, patch in {
        "empty": "",
        "nul": PATCH + "\x00",
        "invalid-utf8": b"\xff",
        "traversal": one_file(path="../x.py"),
        "segment-255": one_file(path="x" * 255),
        "segment-256": one_file(path="x" * 256),
        "binary": "diff --git a/a.png b/a.png\nBinary files a/a.png and b/a.png differ\n",
        "rename": "diff --git a/old.py b/new.py\nsimilarity index 100%\nrename from old.py\nrename to new.py\n",
        "mode": "diff --git a/x.py b/x.py\nold mode 100644\nnew mode 100755\n",
    }.items():
        add(
            "diff-" + name,
            "diff",
            patch=patch,
            expected=0 if name in ["segment-255", "rename", "mode", "nul"] else 2,
            check_complete=name not in ["binary"],
        )
    for n in [32767, 32768, 32769]:
        add(
            "summary-bytes-" + str(n),
            "result",
            response={**CLEAN, "summary": "x" * n},
            expected=0 if n <= 32768 else 2,
        )
    for n in [16383, 16384, 16385]:
        r = block()
        r["comments"][0]["body"] = "x" * n
        add(
            "comment-body-" + str(n),
            "result",
            response=r,
            expected=1 if n <= 16384 else 2,
        )
    for n in [249, 250, 251]:
        r = block()
        r["comments"] = [
            {**r["comments"][0], "body": "Synthetic defect " + str(i)} for i in range(n)
        ]
        add(
            "comment-count-" + str(n),
            "result",
            response=r,
            expected=1 if n <= 250 else 2,
        )
    for n in [1048575, 1048576, 1048577]:
        raw = json.dumps(CLEAN)
        raw += " " * (n - len(raw))
        add(
            "provider-response-" + str(n),
            "result",
            response_raw=raw,
            expected=0 if n <= 1048576 else 2,
        )
    for raw in ["{", "[]", "null", "42", '{"summary":false,"comments":[]}']:
        add(
            "malformed-" + hashlib.sha256(raw.encode()).hexdigest()[:8],
            "retries",
            response_raw=raw,
            expected=2,
            expect_max_calls=3,
        )
    for option in [
        "timeout-seconds",
        "symbol-context-max-files",
        "symbol-context-max-bytes",
        "symbol-context-max-depth",
        "recovery-ttl-seconds",
    ]:
        for val in ["0", "-1", "nan", "inf", "1.5", "nope"]:
            add(
                "cli-" + option + "-" + val,
                "cli-types",
                extra=["--" + option, val],
                expected=2
                if (option == "timeout-seconds" and val != "1.5")
                or (
                    option != "timeout-seconds" and val in ["nan", "inf", "1.5", "nope"]
                )
                else 0,
            )
    # Tiny work targets must stop before dispatch; maxima are validated offline above.
    for field in [
        "batch_diff_bytes",
        "batch_prompt_bytes",
        "max_total_prompt_bytes",
        "max_total_output_bytes",
    ]:
        add(
            "unified-" + field + "-one",
            "work-budget",
            fields={
                "advanced.review_work.mode": "unified",
                "advanced.review_work." + field: 1,
            },
            expected=2,
            expect_max_calls=0
            if "prompt" in field or field == "batch_diff_bytes"
            else 1,
            check_complete=False,
        )
    for calls in [1, 2, 8, 31, 32]:
        add(
            "unified-calls-" + str(calls),
            "work-budget",
            fields={
                "advanced.review_work.mode": "unified",
                "advanced.resources.max_provider_calls": calls,
            },
            expected=0,
            expect_max_calls=calls,
        )
    for n in [131071, 131072, 131073, 1048577]:
        add(
            "unified-chunk-bytes-" + str(n),
            "chunking",
            fields={"advanced.review_work.mode": "unified"},
            patch=one_file(n),
            expected=2,
            check_complete=False,
        )
    for n in [8388607, 8388608, 8388609]:
        add(
            "total-diff-bytes-" + str(n),
            "chunking",
            fields={"advanced.review_work.mode": "unified"},
            patch=one_file(n),
            expected=2,
            check_complete=False,
        )

    def stage(name):
        return {
            "name": name,
            "outputs": ["summary", "comments", "learning_proposals"],
            "prompt_template": "Synthetic review {diff} {review_categories} {review_context} {learnings}",
        }

    for n in [0, 1, 7, 8, 9, 31, 32, 33]:
        add(
            "stage-count-" + str(n),
            "stages",
            setup={
                f"stages/{i}.json": json.dumps(stage("s" + str(i))) for i in range(n)
            },
            mkdir=["stages"],
            extra=["--stages-dir", "stages"],
            expected=0 if 1 <= n <= 8 else 2,
            expect_max_calls=min(n, 8) if 1 <= n <= 32 else None,
        )
    for n in [131071, 131072, 131073]:
        raw = json.dumps(stage("one"))
        raw += " " * (n - len(raw))
        add(
            "stage-file-bytes-" + str(n),
            "stages",
            setup={"stages/one.json": raw},
            extra=["--stages-dir", "stages"],
            expected=0 if n <= 131072 else 2,
        )
    for n in [15, 16, 17]:
        st = stage("one")
        st["categories"] = [
            {"id": "c" + str(i), "title": "Synthetic", "focus": ["correctness"]}
            for i in range(n)
        ]
        add(
            "categories-per-stage-" + str(n),
            "stages",
            setup={"stages/one.json": json.dumps(st)},
            extra=["--stages-dir", "stages"],
            expected=0 if n <= 16 else 2,
        )
    for n in [15, 16, 17]:
        st = stage("one")
        st["categories"] = [
            {
                "id": "synthetic",
                "title": "Synthetic",
                "focus": ["item" + str(i) for i in range(n)],
            }
        ]
        add(
            "focus-per-category-" + str(n),
            "stages",
            setup={"stages/one.json": json.dumps(st)},
            extra=["--stages-dir", "stages"],
            expected=0 if n <= 16 else 2,
        )
    for n in [63, 64, 65]:
        setup = {
            f"categories/{i}.json": json.dumps(
                {"id": "c" + str(i), "title": "Synthetic", "focus": ["correctness"]}
            )
            for i in range(n)
        }
        setup["stages/one.json"] = json.dumps({**stage("one"), "category_ids": ["c0"]})
        add(
            "category-file-count-" + str(n),
            "stages",
            setup=setup,
            extra=["--stages-dir", "stages", "--categories-dir", "categories"],
            expected=0 if n <= 64 else 2,
        )
    for n in [65535, 65536, 65537]:
        raw = json.dumps({"id": "c0", "title": "Synthetic", "focus": ["correctness"]})
        raw += " " * (n - len(raw))
        setup = {
            "categories/one.json": raw,
            "stages/one.json": json.dumps({**stage("one"), "category_ids": ["c0"]}),
        }
        add(
            "category-file-bytes-" + str(n),
            "stages",
            setup=setup,
            extra=["--stages-dir", "stages", "--categories-dir", "categories"],
            expected=0 if n <= 65536 else 2,
        )
    for n in [0, 1, 15, 16, 17, 20, 21, 49, 50, 51, 99, 100, 101]:
        setup = {
            f"learnings/.github/review-sensei/learnings/{i}.json": json.dumps(
                {
                    "id": "l" + str(i),
                    "title": "Synthetic",
                    "rule": "Synthetic scoped rule",
                    "scope": ["x.py"],
                }
            )
            for i in range(n)
        }
        add(
            "learning-file-count-" + str(n),
            "learnings",
            setup=setup,
            mkdir=["learnings"],
            extra=["--learning-root", "learnings"],
            expected=0 if n <= 50 else 2,
        )
    for n in [99, 100, 101]:
        setup = {
            f"learnings/.github/review-sensei/learnings/{i}.json": json.dumps(
                {
                    "id": "l" + str(i),
                    "title": "Synthetic",
                    "rule": "Synthetic scoped rule",
                    "scope": ["x.py"],
                }
            )
            for i in range(n)
        }
        setup["stages/one.json"] = json.dumps(stage("uncategorized"))
        add(
            "learning-file-single-stage-" + str(n),
            "learnings",
            setup=setup,
            extra=["--learning-root", "learnings", "--stages-dir", "stages"],
            expected=0 if n <= 100 else 2,
        )
    for n in [0, 1, 86399, 86400, 86401, -1]:
        add(
            "recovery-ttl-active-" + str(n),
            "persistence",
            extra=[
                "--recovery-artifact",
                "recovery.json",
                "--recovery-ttl-seconds",
                str(n),
                "--repository",
                "owner/repo",
                "--pull-request",
                "1",
                "--base-sha",
                "a" * 40,
                "--head-sha",
                "b" * 40,
            ],
            expected=0 if 1 <= n <= 86400 else 2,
        )
    for n in [0, 31, 32, 33, 4095, 4096, 4097]:
        add(
            "work-recovery-key-" + str(n),
            "persistence",
            setup={"key.bin": b"k" * n},
            private=["key.bin"],
            fields={
                "advanced.review_work.mode": "unified",
                "github.artifacts": "diagnostics",
            },
            extra=[
                "--work-recovery-dir",
                "receipts",
                "--work-recovery-key-file",
                "key.bin",
            ],
            expected=0 if 32 <= n <= 4096 else 2,
        )
    for env, label in [
        ({}, "yaml"),
        ({"REVIEWSENSEI_MODEL": "env-v1"}, "env"),
        ({"REVIEWSENSEI_MODEL": " "}, "blank-env"),
    ]:
        add(
            "precedence-" + label,
            "config-precedence",
            ["config", "show", "--config", "config.yml", "--explain"],
            fields={"inference.model": "yaml-v1"},
            env=env,
            stdout_contains="model: env-v1" if label == "env" else "model: yaml-v1",
        )
    add(
        "precedence-cli",
        "config-precedence",
        ["config", "show", "--config", "config.yml", "--model", "cli-v1", "--explain"],
        fields={"inference.model": "yaml-v1"},
        env={"REVIEWSENSEI_MODEL": "env-v1"},
        stdout_contains="model: cli-v1",
    )
    for command in ["doctor", "plan"]:
        add(
            "canonical-cloud-" + command,
            "known-baseline-defects",
            [command, "--json"],
            setup={
                ".reviewsensei.yml": yaml_fields(
                    {
                        "inference.backend": "cloud-ollama",
                        "inference.model": "synthetic:cloud",
                    }
                )
            },
            stdout_contains="https://ollama.com/api",
            expected_any=[0, 1, 2, 3],
        )
    add(
        "helper-version",
        "known-baseline-defects",
        ["resolve-hosted-openrouter", "--version"],
    )
    for count, cap in [
        (1, 1),
        (2, 1),
        (2, 2),
        (3, 2),
        (7, 8),
        (8, 8),
        (9, 8),
        (31, 31),
        (32, 32),
    ]:
        add(
            f"unified-stage-calls-{count}-cap-{cap}",
            "cumulative-calls",
            setup={
                f"stages/{i}.json": json.dumps(
                    {
                        **stage("s" + str(i)),
                        "categories": [
                            {
                                "id": "correctness",
                                "title": "Correctness",
                                "focus": ["Boundaries"],
                            }
                        ],
                    }
                )
                for i in range(count)
            },
            fields={
                "advanced.review_work.mode": "unified",
                "advanced.resources.max_provider_calls": cap,
            },
            extra=["--stages-dir", "stages"],
            expected=0 if count <= cap else 2,
            expect_max_calls=cap,
            check_complete=count <= cap,
        )
    for count, cap in [
        (1, 1),
        (2, 1),
        (2, 2),
        (8, 8),
        (9, 8),
        (31, 32),
        (32, 32),
        (33, 32),
    ]:
        add(
            f"chunk-count-{count}-cap-{cap}",
            "chunking",
            patch=many_files(count),
            fields={
                "advanced.review_work.mode": "unified",
                "advanced.review_work.batch_diff_bytes": 100,
                "advanced.resources.max_provider_calls": cap,
            },
            expected=0 if count <= cap else 2,
            expect_max_calls=cap,
            check_complete=count <= cap,
        )
    for dimension, maximum, maker in [
        ("files", 4000, many_files),
        ("hunks", 40000, many_hunks),
        ("lines", 400000, lambda n: one_file(lines=n - 4)),
    ]:
        for n in [maximum - 1, maximum, maximum + 1]:
            add(
                f"total-diff-{dimension}-{n}",
                "large-total-work",
                patch=maker(n),
                fields={"advanced.review_work.mode": "unified"},
                expected=2,
                check_complete=False,
            )
    for option, maximum in [
        ("bytes", 1048576),
        ("lines", 50000),
        ("files", 500),
        ("hunks", 5000),
    ]:
        for label, value in [
            ("minimum", 1),
            ("below", maximum - 1),
            ("at", maximum),
            ("above", maximum + 1),
            ("zero", 0),
            ("negative", -1),
            ("string", "bad"),
            ("float", 1.5),
        ]:
            accepted = type(value) is int and 1 <= value <= maximum
            expected = (
                2
                if type(value) is not int
                else 1
                if not accepted or value == 1 and option in ["bytes", "lines"]
                else 0
            )
            add(
                f"prepare-diff-{option}-{label}",
                "prepare-diff",
                argv=[
                    "prepare-diff",
                    "--repository",
                    "GIT_FIXTURE",
                    "--base-ref",
                    "BASE_FIXTURE",
                    "--head-ref",
                    "HEAD_FIXTURE",
                    "--output",
                    "prepared.patch",
                    "--max-diff-" + option,
                    str(value),
                ],
                expected=expected,
            )
    add(
        "work-recovery-resume",
        "persistence-resume",
        setup={"key.bin": b"k" * 32},
        private=["key.bin"],
        fields={
            "advanced.review_work.mode": "unified",
            "github.artifacts": "diagnostics",
        },
        extra=[
            "--work-recovery-dir",
            "receipts",
            "--work-recovery-key-file",
            "key.bin",
        ],
        expected=0,
        resume=True,
    )
    return out


class Runner:
    def __init__(self, args):
        self.args = args
        self.root = args.report.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.cli = Path(sys.executable).parent / (
            "review-sensei.exe" if os.name == "nt" else "review-sensei"
        )
        self.env = {
            k: os.environ[k]
            for k in [
                "PATH",
                "SYSTEMROOT",
                "WINDIR",
                "COMSPEC",
                "PATHEXT",
                "SSL_CERT_FILE",
            ]
            if k in os.environ
        }
        self.env.update(LANG="C.UTF-8", PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")

    def run(self, c):
        wd = self.root / "cases" / c["id"]
        wd.mkdir(parents=True, exist_ok=True)
        home = wd / "home"
        home.mkdir(exist_ok=True)
        env = {
            **self.env,
            "HOME": str(home),
            "USERPROFILE": str(home),
            "XDG_STATE_HOME": str(home / "state"),
        }
        env.update(c.get("env", {}))
        for path in c.get("mkdir", []):
            (wd / path).mkdir(parents=True, exist_ok=True)
        for path, content in c.get("setup", {}).items():
            target = wd / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(
                content if isinstance(content, bytes) else content.encode()
            )
        for path in c.get("private", []):
            (wd / path).chmod(0o600)
        patch = c.get("patch", PATCH)
        (wd / "input.patch").write_bytes(
            patch if isinstance(patch, bytes) else patch.encode()
        )
        raw = c.get(
            "response_raw", json.dumps(c.get("response", CLEAN), ensure_ascii=False)
        )
        (wd / "response.json").write_text(raw, encoding="utf-8")
        cfg = c.get("config", yaml_fields(c.get("fields", {})))
        (wd / "config.yml").write_bytes(cfg if isinstance(cfg, bytes) else cfg.encode())
        default = [
            "--diff",
            "input.patch",
            "--provider",
            "fixture",
            "--fixture-response",
            "response.json",
            "--config",
            "config.yml",
            "--no-learning-proposals",
            "--format",
            "json",
            "--outcome",
            "outcome.json",
        ]
        argv = [str(self.cli)] + (
            c["argv"] if c.get("argv") is not None else default + c.get("extra", [])
        )
        start = time.monotonic()
        try:
            cp = subprocess.run(
                argv,
                cwd=wd,
                env=env,
                capture_output=True,
                timeout=self.args.case_timeout,
            )
            first_receipt = None
            if c.get("resume"):
                first_receipt = {
                    "exit": cp.returncode,
                    "outcome": json.loads((wd / "outcome.json").read_text())
                    if (wd / "outcome.json").exists()
                    else None,
                }
                # A fresh provider call would fail. Accepted receipts must replay
                # while retaining the original charged calls and absolute deadline.
                (wd / "response.json").write_text(
                    "{invalid synthetic response", encoding="utf-8"
                )
                (wd / "outcome.json").unlink(missing_ok=True)
                cp = subprocess.run(
                    argv,
                    cwd=wd,
                    env=env,
                    capture_output=True,
                    timeout=self.args.case_timeout,
                )
            stdout = cp.stdout.decode("utf-8", errors="replace")
            stderr = cp.stderr.decode("utf-8", errors="replace")
            code = cp.returncode
        except subprocess.TimeoutExpired as e:
            stdout = (e.stdout or b"").decode("utf-8", errors="replace")
            stderr = (e.stderr or b"").decode("utf-8", errors="replace")
            code = "TIMEOUT"
        (wd / "stdout.txt").write_text(stdout, encoding="utf-8")
        (wd / "stderr.txt").write_text(stderr, encoding="utf-8")
        checks = {
            "exit": code in c.get("expected_any", [c["expected"]]),
            "no_traceback": "Traceback" not in stderr,
        }
        if c.get("stdout_contains"):
            checks["expected_stdout"] = c["stdout_contains"] in stdout
        outcome = (
            json.loads((wd / "outcome.json").read_text())
            if (wd / "outcome.json").exists()
            else None
        )
        if c.get("resume"):
            checks["resume_complete"] = (
                first_receipt is not None and first_receipt["exit"] == 0 and code == 0
            )
            checks["original_accounting"] = (
                outcome is not None
                and first_receipt["outcome"]["provider_calls"]
                == outcome["provider_calls"]
            )
        doc = None
        if c.get("argv") is None and c.get("fmt", "json") == "json" and stdout.strip():
            try:
                doc = json.loads(stdout)
                checks["machine_json"] = True
            except ValueError:
                checks["machine_json"] = False
        if doc and c.get("check_complete") is not False and c["expected"] in [0, 1]:
            checks["coverage_complete"] = (
                doc.get("review_status") == "complete"
                and doc.get("coverage", {}).get("fully_reviewed") is True
            )
        if doc and c.get("check_complete") is False:
            checks["coverage_safe"] = (
                doc.get("coverage", {}).get("fully_reviewed") is not True
            )
        if c.get("expect_max_calls") is not None:
            checks["calls_bounded"] = (
                outcome is not None
                and outcome["provider_calls"] <= c["expect_max_calls"]
            )
        row = {
            "id": c["id"],
            "group": c["group"],
            "status": "PASS" if all(checks.values()) else "FAIL",
            "checks": checks,
            "exit": code,
            "expected_exit": c["expected"],
            "argv": [self.cli.name] + argv[1:],
            "elapsed_seconds": round(time.monotonic() - start, 3),
            "level": "installed-CLI-fixture",
            "outcome": outcome,
            "review_status": doc.get("review_status") if doc else None,
            "fully_reviewed": doc.get("coverage", {}).get("fully_reviewed")
            if doc
            else None,
            "stderr_excerpt": stderr[:1000],
            "input_sha256": hashlib.sha256(
                (wd / "input.patch").read_bytes()
            ).hexdigest(),
            "configuration_sha256": hashlib.sha256(
                (wd / "config.yml").read_bytes()
            ).hexdigest(),
        }
        (wd / "receipt.json").write_text(json.dumps(row, indent=2), encoding="utf-8")
        return row


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--expected-version", required=True)
    p.add_argument("--expected-github-version", required=True)
    p.add_argument("--report", type=Path, required=True)
    p.add_argument("--jobs", type=int, default=4)
    p.add_argument("--case-timeout", type=float, default=25)
    p.add_argument("--filter", default="")
    p.add_argument("--large-total-work", action="store_true")
    args = p.parse_args()
    version = importlib.metadata.version("review-sensei")
    if version != args.expected_version:
        p.error("installed distribution does not match exact expected version")
    if (
        importlib.metadata.version("review-sensei-github")
        != args.expected_github_version
    ):
        p.error("installed review-sensei-github does not match exact expected version")
    import review_sensei

    origin = str(Path(review_sensei.__file__).resolve())
    if "site-packages" not in origin and "dist-packages" not in origin:
        p.error("qualification requires an installed package, not a source import")
    runner = Runner(args)
    all_cases = cases()
    selected = [
        c
        for c in all_cases
        if args.filter in c["id"]
        and (args.large_total_work or c["group"] != "large-total-work")
    ]
    if not selected:
        p.error("no cases selected")
    for c in selected:
        if c["id"] == "helper-version":
            c["stdout_contains"] = args.expected_github_version
    if any(c["group"] == "prepare-diff" for c in selected):
        repo = runner.root / "git-fixture"
        repo.mkdir(exist_ok=True)
        git_env = {
            **runner.env,
            "HOME": str(repo),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_AUTHOR_NAME": "Synthetic",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Synthetic",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
        }

        def git(*argv):
            return subprocess.check_output(
                ["git", *argv],
                cwd=repo,
                env=git_env,
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()

        git("init")
        (repo / "x.py").write_text("x = 0\n")
        git("add", "x.py")
        git("commit", "-m", "base")
        base = git("rev-parse", "HEAD")
        (repo / "x.py").write_text("x = 1\n")
        git("add", "x.py")
        git("commit", "-m", "head")
        head = git("rev-parse", "HEAD")
        for c in selected:
            if c["group"] == "prepare-diff":
                c["argv"] = [
                    str(repo)
                    if x == "GIT_FIXTURE"
                    else base
                    if x == "BASE_FIXTURE"
                    else head
                    if x == "HEAD_FIXTURE"
                    else x
                    for x in c["argv"]
                ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        rows = list(pool.map(runner.run, selected))
    summary = {
        "version": version,
        "package_origin": origin,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "total": len(rows),
        "pass": sum(x["status"] == "PASS" for x in rows),
        "fail": sum(x["status"] == "FAIL" for x in rows),
        "filtered": bool(args.filter),
        "credential_free": True,
        "level": "installed-CLI-fixture",
        "rows": rows,
    }
    (runner.root / "results.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    with (runner.root / "matrix.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "id",
                "group",
                "status",
                "exit",
                "expected_exit",
                "elapsed_seconds",
                "level",
                "review_status",
                "fully_reviewed",
            ],
        )
        w.writeheader()
        w.writerows({k: r[k] for k in w.fieldnames} for r in rows)
    print(json.dumps({k: v for k, v in summary.items() if k != "rows"}, indent=2))
    for r in rows:
        if r["status"] == "FAIL":
            print(
                json.dumps(
                    {
                        "id": r["id"],
                        "exit": r["exit"],
                        "expected": r["expected_exit"],
                        "checks": r["checks"],
                        "stderr": r["stderr_excerpt"],
                    }
                )
            )
    return 1 if summary["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())

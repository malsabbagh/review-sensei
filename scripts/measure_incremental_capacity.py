#!/usr/bin/env python3
"""Offline sizing qualification; fixture rates are not production statistics."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from review_sensei.baseline import baseline_history_document  # noqa: E402
from review_sensei.planning import MAX_RELATED_PATHS  # noqa: E402
from review_sensei.service import _chunk_context_note  # noqa: E402
from tests.test_context_capacity import capacity_fixture, encoded  # noqa: E402


def measure() -> dict[str, object]:
    rows = []
    for count, width, unicode in (
        (32, None, False),
        (41, None, False),
        (41, 52, False),
        (55, 52, False),
        (64, 52, False),
        (41, None, True),
    ):
        _policy, baseline, current = capacity_fixture(
            count, width=width, unicode=unicode
        )
        related = tuple(dict.fromkeys((*baseline.related_paths, *current)))
        reviewed = tuple(dict.fromkeys((*baseline.reviewed_paths, *current, *related)))
        candidate = replace(baseline, reviewed_paths=reviewed, related_paths=related)
        document = baseline_history_document(candidate)
        full_document = {
            **document,
            "complete": candidate.complete,
            "coverage_complete": candidate.coverage_complete,
            "reviewed_paths": sorted(candidate.reviewed_paths),
            "related_paths": sorted(candidate.related_paths),
        }
        # Baseline document must be complete under each writer allocation;
        # otherwise legacy writers produce incomplete evidence and need a full
        # pass on the next round. Count overflow requires an immediate full pass.
        modes = {}
        for label, paths, bytes_ in (
            ("previous", 32, 3072),
            ("8KiB", MAX_RELATED_PATHS, 7168),
            ("12KiB", MAX_RELATED_PATHS, 11264),
        ):
            projected = baseline_history_document(candidate, max_bytes=bytes_)
            modes[label] = (
                "incremental"
                if len(related) <= paths and projected["complete"]
                else "fallback-required"
            )
        rows.append(
            {
                "related_paths": count,
                "path_shape": "unicode-100-é"
                if unicode
                else f"ascii-{width or 'short'}",
                "required_baseline_bytes": len(encoded(full_document)),
                "stored_projection_bytes": len(encoded(document)),
                "complete_at_12KiB": document["complete"],
                "chunk_note_utf8_bytes": len(_chunk_context_note(related).encode()),
                "modes": modes,
            }
        )
    return {
        "fixtures": rows,
        "fallback_required": {
            label: sum(row["modes"][label] == "fallback-required" for row in rows)
            for label in ("previous", "8KiB", "12KiB")
        },
        "fixture_count": len(rows),
        "cost_scope": "Canonical metadata and instruction bytes only; no token, latency, currency or production-rate inference. Both direct CLI routes make one provider call and exact retries make none.",
    }


if __name__ == "__main__":
    print(json.dumps(measure(), indent=2, ensure_ascii=False))

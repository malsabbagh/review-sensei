#!/usr/bin/env python3
"""Dispatch validation after an operator's promotion; this helper never moves tags."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

from major_tag_gate import MAJOR, docs, sha, signed_major


def command(channel, tag_object, source):
    if not MAJOR.fullmatch(channel):
        raise ValueError("a canonical lowercase numeric-major channel is required")
    expected = (sha(tag_object), sha(source))
    if signed_major(channel) != expected:
        raise ValueError("major channel changed; do not dispatch a different promotion")
    return [
        "gh",
        "workflow",
        "run",
        "major-tag-validation.yml",
        "--repo",
        docs.REPOSITORY,
        "--ref",
        "main",
        "-f",
        "channel=" + channel,
        "-f",
        "tag_object=" + expected[0],
        "-f",
        "source_sha=" + expected[1],
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", required=True)
    parser.add_argument("--tag-object", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument(
        "--dispatch",
        action="store_true",
        help="send the workflow_dispatch using already-authorized gh authentication",
    )
    args = parser.parse_args()
    try:
        argv = command(args.channel, args.tag_object, args.source_sha)
        if args.dispatch:
            subprocess.run(argv, check=True, capture_output=True, timeout=30)
        print(
            json.dumps(
                {"command": argv, "dispatched": args.dispatch, "tag_moved": False}
            )
        )
        return 0
    except Exception as error:
        print(
            "major validation dispatch failed: "
            + (str(error) if isinstance(error, ValueError) else type(error).__name__),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())

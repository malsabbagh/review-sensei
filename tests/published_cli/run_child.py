"""One current installed CLI case; external fixtures only, no live connections."""

import json
import sys
from pathlib import Path

from run_fixture import (
    HEAD,
    IDENTITY,
    ReviewFixture,
    commit_policy,
    invoke,
    parse_session_comment,
    receipt,
)

spec = json.loads(sys.argv[1])
fixture = ReviewFixture(Path.cwd(), partial=spec["partial"])
mode = spec["listing"]
seeded = mode in {
    "matching",
    "mismatch",
    "second-page-matching",
    "second-page-mismatch",
} and not spec.get("login")
seed_exit = None
seed = None
if seeded:
    seed_exit = invoke(fixture)
    if seed_exit != 0:
        raise AssertionError("retained record setup did not complete: " + fixture.error)
    seed = receipt(fixture)
    seed.pop("requests")
    if "mismatch" in mode:
        record = parse_session_comment(fixture.comment["body"], identity=IDENTITY)
        fixture.comment["body"] = fixture.comment["body"].replace(
            record.record_sha256, "0" * 64, 1
        )
    fixture.next_run()
    fixture.live_head = fixture.requested_head = HEAD
fixture.mode = mode
if spec.get("login"):
    from review_sensei_github.operation_host import OPERATION_COMMENT_MARKER

    fixture.extra_comments[333] = {
        "id": 333,
        "body": OPERATION_COMMENT_MARKER + "\n" + "0" * 64,
        "user": {"id": 998, "type": "Bot", "login": spec["login"]},
        "issue_url": "https://api.github.test/repos/owner/repo/issues/7",
    }
if spec.get("entry") == "disabled":
    fixture.config_bytes = fixture.config_bytes.replace(
        b"  writes: true", b"  operation_entry: disabled\n  writes: true"
    )
    commit_policy(fixture)
before_provider, before_reviews = fixture.provider_calls, len(fixture.reviews)
outcome = fixture.root / "publication.json"
outcome.unlink(missing_ok=True)
code = invoke(
    fixture,
    token=spec["token"] == "present",
    appid=spec["appid"],
    runid=spec.get("runid", "100") != "",
)
data = receipt(fixture)
data.update(
    exit=code,
    seed_exit=seed_exit,
    seed=seed,
    provider_calls=fixture.provider_calls - before_provider,
    reviews=data["reviews"][before_reviews:],
    fault_hits=fixture.hit,
    live_network="blocked",
    level="installed-current-review-run/external-HTTP-only",
)
data["completed"] = data["terminal_transaction"] is not None
data["terminal_reason"] = (
    fixture.accounted._terminal_reason if fixture.accounted else None
)
data["outcome"] = json.loads(outcome.read_text()) if outcome.exists() else None
print(json.dumps(data))
sys.exit(code)

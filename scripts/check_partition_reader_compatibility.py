"""Immutable historical readers reject partition authority without old fallback."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
READERS = (
    "5a121dfe39fb978c7d71def0d08dc570aad83f1d",  # published v0.6.16
    "70305207647c22595660e5ebb9e4a33991489ffe",  # pre-partition main
)


def worker(source: Path, fixture_path: Path) -> None:
    sys.path.insert(0, str(source))
    from review_sensei.errors import ReviewInputError
    from review_sensei.hosting.github import GitHubHttp
    from review_sensei.hosting.github.session_ledger import (
        GitHubIssueCommentSessionLedger,
    )
    from review_sensei.session import LocalSessionLedger, SessionIdentity

    payload = json.loads(fixture_path.read_text())
    identity = SessionIdentity(**payload["identity"])

    # A latest authenticated partition root alongside an older clean comment
    # must withhold, even though an immutable reader cannot resolve parts.
    class Response:
        status = 200
        headers: dict[str, str] = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size):
            return json.dumps(payload["comments"]).encode()[:size]

    http = GitHubHttp(
        api_url="https://api.github.test", opener=lambda request, timeout: Response()
    )
    hosted = GitHubIssueCommentSessionLedger(
        http, token="synthetic", app_slug="sensei[bot]"
    )
    loaded = hosted.load(identity)
    if (
        loaded.status not in {"integrity-failed", "conflict"}
        or loaded.record is not None
    ):
        raise AssertionError("historical reader exposed old clean authority")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "owner%2Frepo" / f"{identity.pull_request}.json"
        path.parent.mkdir()
        path.write_text(json.dumps(payload["record"]))
        local = LocalSessionLedger(Path(directory))
        loaded = local.load(identity)
        if loaded.status != "integrity-failed" or loaded.record is not None:
            raise AssertionError("historical local reader accepted partition authority")
        try:
            local.initialize(identity)
        except ReviewInputError:
            pass
        else:
            raise AssertionError(
                "historical local reader initialized over unreadable authority"
            )
    print(
        f"PASS immutable reader {source.parent.name}: partition latest authority withholds"
    )


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--worker":
        worker(Path(sys.argv[2]), Path(sys.argv[3]))
        return 0
    sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
    from review_sensei.hosting.github.session_ledger import render_session_comment
    from review_sensei.session import LocalSessionLedger, SessionRecord
    from tests import test_review_transaction as fixture
    from tests.test_authenticated_partitions import checkpoint

    with tempfile.TemporaryDirectory(prefix="lane-a-readers-") as temporary:
        directory = Path(temporary)
        ledger = LocalSessionLedger(directory / "ledger", enable_partition_writes=True)
        checkpoint(ledger, 250)
        record = ledger.load(fixture.IDENTITY, now=fixture.NOW).record
        if record is None:
            raise AssertionError("current partition reader failed")
        comments = [
            {
                "id": 1,
                "body": render_session_comment(
                    repository_id=99,
                    pull_request=fixture.IDENTITY.pull_request,
                    record=SessionRecord.create(fixture.IDENTITY, now=fixture.NOW),
                ),
                "user": {"id": 55, "login": "sensei[bot]", "type": "Bot"},
            },
            {
                "id": 2,
                "body": render_session_comment(
                    repository_id=99,
                    pull_request=fixture.IDENTITY.pull_request,
                    record=record,
                ),
                "user": {"id": 55, "login": "sensei[bot]", "type": "Bot"},
            },
        ]
        data_path = directory / "fixture.json"
        data_path.write_text(
            json.dumps(
                {
                    "identity": {
                        "repository": fixture.IDENTITY.repository,
                        "repository_id": 99,
                        "pull_request": fixture.IDENTITY.pull_request,
                    },
                    "record": record.to_dict(),
                    "comments": comments,
                }
            )
        )
        for commit in READERS:
            archive = subprocess.run(
                ["git", "archive", "--format=tar", commit, "src"],
                cwd=ROOT,
                check=True,
                capture_output=True,
            ).stdout
            target = directory / commit
            with tarfile.open(fileobj=io.BytesIO(archive)) as tree:
                for member in tree.getmembers():
                    path = Path(member.name)
                    if (
                        path.is_absolute()
                        or ".." in path.parts
                        or path.parts[0] != "src"
                    ):
                        raise ValueError("invalid archived reader path")
                    if member.isdir():
                        continue
                    if not member.isfile():
                        raise ValueError("reader archive must contain regular files")
                    destination = target / path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    stream = tree.extractfile(member)
                    if stream is None:
                        raise ValueError("reader archive missing bytes")
                    destination.write_bytes(stream.read())
            subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(Path(__file__).resolve()),
                    "--worker",
                    str(target / "src"),
                    str(data_path),
                ],
                cwd=directory,
                check=True,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

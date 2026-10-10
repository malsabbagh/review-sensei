"""Immutable legacy consumers must withhold a latest owned v4 root."""

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
    "5a121dfe39fb978c7d71def0d08dc570aad83f1d",
    "70305207647c22595660e5ebb9e4a33991489ffe",
    "647a2db3a17e0e01e7c3017e5cecde9cc7a1ec10",
    "39764935c7e1a66012833b51bc152d67700b1b8c",
)


def worker(source: Path, fixture: Path) -> None:
    sys.path.insert(0, str(source))
    from review_sensei.hosting.github.errors import GitHubPublicationError
    from review_sensei.hosting.github.http import GitHubHttp
    from review_sensei.hosting.github.publication import ReviewApprovalFinalizer

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size):
            return fixture.read_bytes()[:size]

    http = GitHubHttp(
        api_url="https://api.github.test", opener=lambda request, timeout: Response()
    )
    finalizer = ReviewApprovalFinalizer(http=http)
    kwargs = dict(
        token="synthetic-read",
        repository="owner/repo",
        pull_request=1,
        head_sha="b" * 40,
        app_slug="review-sensei[bot]",
    )
    if finalizer.load_eligibility(**kwargs) is not None:
        raise AssertionError("immutable reader revealed an older clean root")
    try:
        finalizer.load_eligibility(**kwargs, require_valid=True)
    except GitHubPublicationError:
        pass
    else:
        raise AssertionError("immutable reader accepted unsupported latest authority")
    print(
        f"PASS immutable {source.parent.name}: latest v4 withholds, no older clean fallback"
    )


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--worker":
        worker(Path(sys.argv[2]), Path(sys.argv[3]))
        return 0
    sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
    from review_sensei.hosting.github.approval import approval_eligibility_from_result
    from review_sensei.hosting.github.publication import approval_eligibility_marker
    from review_sensei.models import ReviewResult
    from tests.test_partitioned_eligibility_reader import Bundle

    clean = approval_eligibility_from_result(
        ReviewResult(
            summary="Synthetic old clean authority", comments=(), provider="fixture"
        ),
        head_sha="b" * 40,
        enabled=True,
        app_authored=False,
    )
    bundle = Bundle(1)
    reviews = [
        dict(
            id=9999,
            commit_id="b" * 40,
            state="COMMENTED",
            body=approval_eligibility_marker(clean),
            user={"login": "review-sensei[bot]", "type": "Bot", "id": 12345},
        ),
        dict(
            id=10000,
            commit_id="b" * 40,
            state="COMMENTED",
            body=bundle.marker(),
            user={"login": "review-sensei[bot]", "type": "Bot", "id": 12345},
        ),
    ]
    with tempfile.TemporaryDirectory(prefix="lane-b-v4-readers-") as temporary:
        folder = Path(temporary)
        fixture = folder / "reviews.json"
        fixture.write_text(json.dumps(reviews))
        for commit in READERS:
            target = folder / commit
            target.mkdir()
            archive = subprocess.run(
                ["git", "archive", commit, "src"],
                cwd=ROOT,
                check=True,
                capture_output=True,
            ).stdout
            with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
                stream.extractall(target, filter="data")
            subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(Path(__file__).resolve()),
                    "--worker",
                    str(target / "src"),
                    str(fixture),
                ],
                cwd=folder,
                check=True,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

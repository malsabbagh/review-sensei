from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

from scripts.publish_npm_release import (
    ALL_PACKAGES,
    PLATFORM_PACKAGES,
    PublishError,
    load_integrity_records,
    package_action,
    preflight,
    publish_package,
    publish_platforms,
    read_back_with_retry,
    retry_delay_seconds,
    write_publish_state,
)


class PublishNpmReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.bundle_dir = Path(self.tempdir.name)
        integrity_lines = []
        for package in ALL_PACKAGES:
            integrity_lines.append(
                {
                    "file": f"{package.removeprefix('@reviewsensei/cli-') or 'cli'}-0.5.0.tgz",
                    "name": package,
                    "version": "0.5.0",
                    "sha256": "deadbeef",
                    "integrity": f"sha512-{package}",
                }
            )
        (self.bundle_dir / "integrity.jsonl").write_text(
            "\n".join(json.dumps(line) for line in integrity_lines) + "\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_preflight_marks_missing_packages_for_publish(self) -> None:
        def fake_fetch(package: str, version: str) -> dict[str, object]:
            if package.endswith("darwin-arm64"):
                return {
                    "name": package,
                    "version": version,
                    "dist": {"integrity": f"sha512-{package}"},
                }
            raise HTTPError("url", 404, "not found", hdrs=None, fp=io.BytesIO(b""))

        with mock.patch(
            "scripts.publish_npm_release.fetch_registry_package",
            side_effect=fake_fetch,
        ):
            preflight(self.bundle_dir, "0.5.0")

        state = json.loads(
            (self.bundle_dir / "publish-state.json").read_text(encoding="utf-8")
        )
        actions = {item["name"]: item["action"] for item in state["packages"]}
        self.assertEqual(actions["@reviewsensei/cli-darwin-arm64"], "verified")
        self.assertEqual(actions["@reviewsensei/cli-linux-x64-gnu"], "publish")

    def test_load_integrity_records_rejects_version_mismatch(self) -> None:
        with self.assertRaisesRegex(PublishError, "invalid package integrity set"):
            load_integrity_records(self.bundle_dir, "0.4.0")

    def test_package_action_rejects_stale_publish_state_version(self) -> None:
        write_publish_state(
            self.bundle_dir,
            "0.4.0",
            [{"name": "@reviewsensei/cli", "action": "publish"}],
        )
        with self.assertRaisesRegex(PublishError, "does not match requested '0.5.0'"):
            package_action(self.bundle_dir, "@reviewsensei/cli", "0.5.0")

    def test_read_back_retries_until_registry_is_visible(self) -> None:
        responses: list[object] = [
            HTTPError("url", 404, "not found", hdrs=None, fp=io.BytesIO(b"")),
            {
                "name": "@reviewsensei/cli-linux-x64-gnu",
                "version": "0.5.0",
                "dist": {
                    "integrity": "sha512-@reviewsensei/cli-linux-x64-gnu",
                },
            },
        ]

        with (
            mock.patch(
                "scripts.publish_npm_release.fetch_registry_package",
                side_effect=responses,
            ),
            mock.patch("scripts.publish_npm_release.time.sleep") as sleep,
        ):
            read_back_with_retry(
                "@reviewsensei/cli-linux-x64-gnu",
                "0.5.0",
                "sha512-@reviewsensei/cli-linux-x64-gnu",
                max_attempts=3,
                initial_delay_seconds=2.0,
                max_delay_seconds=30.0,
            )

        sleep.assert_called_once_with(2.0)

    def test_read_back_retries_on_transient_http_errors(self) -> None:
        responses: list[object] = [
            HTTPError("url", 503, "unavailable", hdrs=None, fp=io.BytesIO(b"")),
            HTTPError(
                "url",
                429,
                "rate limited",
                hdrs={"Retry-After": "5"},
                fp=io.BytesIO(b""),
            ),
            {
                "name": "@reviewsensei/cli-linux-x64-gnu",
                "version": "0.5.0",
                "dist": {
                    "integrity": "sha512-@reviewsensei/cli-linux-x64-gnu",
                },
            },
        ]

        with (
            mock.patch(
                "scripts.publish_npm_release.fetch_registry_package",
                side_effect=responses,
            ),
            mock.patch("scripts.publish_npm_release.time.sleep") as sleep,
        ):
            read_back_with_retry(
                "@reviewsensei/cli-linux-x64-gnu",
                "0.5.0",
                "sha512-@reviewsensei/cli-linux-x64-gnu",
                max_attempts=3,
                initial_delay_seconds=2.0,
                max_delay_seconds=30.0,
            )

        self.assertEqual(sleep.mock_calls[0], mock.call(2.0))
        self.assertEqual(sleep.mock_calls[1], mock.call(5.0))

    def test_read_back_exhausts_retry_budget(self) -> None:
        with (
            mock.patch(
                "scripts.publish_npm_release.fetch_registry_package",
                side_effect=HTTPError(
                    "url",
                    404,
                    "not found",
                    hdrs=None,
                    fp=io.BytesIO(b""),
                ),
            ),
            mock.patch("scripts.publish_npm_release.time.sleep"),
        ):
            with self.assertRaisesRegex(PublishError, "attempt 2/2"):
                read_back_with_retry(
                    "@reviewsensei/cli",
                    "0.5.0",
                    "sha512-@reviewsensei/cli",
                    max_attempts=2,
                    initial_delay_seconds=1.0,
                    max_delay_seconds=1.0,
                )

    def test_read_back_fails_on_terminal_http_error(self) -> None:
        with mock.patch(
            "scripts.publish_npm_release.fetch_registry_package",
            side_effect=HTTPError(
                "url",
                403,
                "forbidden",
                hdrs=None,
                fp=io.BytesIO(b""),
            ),
        ):
            with self.assertRaisesRegex(PublishError, "HTTP 403"):
                read_back_with_retry(
                    "@reviewsensei/cli",
                    "0.5.0",
                    "sha512-@reviewsensei/cli",
                    max_attempts=3,
                )

    def test_read_back_maps_transport_error(self) -> None:
        with mock.patch(
            "scripts.publish_npm_release.fetch_registry_package",
            side_effect=TimeoutError("timed out"),
        ):
            with self.assertRaisesRegex(PublishError, "transport failure"):
                read_back_with_retry(
                    "@reviewsensei/cli",
                    "0.5.0",
                    "sha512-@reviewsensei/cli",
                    max_attempts=1,
                )

    def test_publish_platforms_publishes_missing_platform_and_retries_readback(
        self,
    ) -> None:
        records = load_integrity_records(self.bundle_dir, "0.5.0")
        target = PLATFORM_PACKAGES[1]
        write_publish_state(
            self.bundle_dir,
            "0.5.0",
            [
                *[
                    {"name": package, "action": "verified"}
                    for package in PLATFORM_PACKAGES
                    if package != target
                ],
                {"name": target, "action": "publish"},
                {"name": "@reviewsensei/cli", "action": "publish"},
            ],
        )
        tarball = self.bundle_dir / records[target]["file"]
        tarball.write_bytes(b"tarball")
        responses: list[object] = [
            HTTPError("url", 404, "not found", hdrs=None, fp=io.BytesIO(b"")),
            {
                "name": target,
                "version": "0.5.0",
                "dist": {"integrity": records[target]["integrity"]},
            },
        ]

        with (
            mock.patch(
                "scripts.publish_npm_release.publish_tarball",
            ) as publish,
            mock.patch(
                "scripts.publish_npm_release.fetch_registry_package",
                side_effect=responses,
            ),
            mock.patch("scripts.publish_npm_release.time.sleep"),
        ):
            publish_platforms(
                self.bundle_dir,
                "0.5.0",
                max_attempts=2,
                initial_delay_seconds=1.0,
                max_delay_seconds=1.0,
            )

        publish.assert_called_once_with(tarball)

    def test_read_back_maps_url_error(self) -> None:
        with mock.patch(
            "scripts.publish_npm_release.fetch_registry_package",
            side_effect=URLError("temporary failure in name resolution"),
        ):
            with self.assertRaisesRegex(PublishError, "temporary failure"):
                read_back_with_retry(
                    "@reviewsensei/cli",
                    "0.5.0",
                    "sha512-@reviewsensei/cli",
                    max_attempts=1,
                )

    def test_read_back_fails_when_integrity_mismatches(self) -> None:
        with mock.patch(
            "scripts.publish_npm_release.fetch_registry_package",
            return_value={
                "name": "@reviewsensei/cli",
                "version": "0.5.0",
                "dist": {"integrity": "sha512-wrong"},
            },
        ):
            with self.assertRaisesRegex(
                PublishError,
                "published npm bytes do not match the attested release bundle",
            ):
                read_back_with_retry(
                    "@reviewsensei/cli",
                    "0.5.0",
                    "sha512-@reviewsensei/cli",
                    max_attempts=1,
                )

    def test_retry_delay_honors_retry_after_header(self) -> None:
        exc = HTTPError(
            "url",
            429,
            "rate limited",
            hdrs={"Retry-After": "12"},
            fp=io.BytesIO(b""),
        )
        self.assertEqual(retry_delay_seconds(exc, 2.0, 30.0), 12.0)

    def test_publish_package_skips_verified_packages(self) -> None:
        records = load_integrity_records(self.bundle_dir, "0.5.0")
        write_publish_state(
            self.bundle_dir,
            "0.5.0",
            [{"name": package, "action": "verified"} for package in ALL_PACKAGES],
        )
        with mock.patch("scripts.publish_npm_release.publish_tarball") as publish:
            publish_package(
                self.bundle_dir,
                "@reviewsensei/cli-darwin-arm64",
                "0.5.0",
                records,
                max_attempts=1,
                initial_delay_seconds=0.0,
                max_delay_seconds=0.0,
            )
        publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()

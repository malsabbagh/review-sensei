from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.publish_npm_release import (  # noqa: E402
    ALL_PACKAGES,
    PLATFORM_PACKAGES,
    IntegrityMismatchError,
    PackumentProbeState,
    PublishError,
    RegistryTransportError,
    classify_registry_state,
    fetch_registry_package,
    load_integrity_records,
    package_action,
    preflight,
    publish_package,
    publish_platforms,
    read_back_with_retry,
    retry_delay_seconds,
    tarball_for_package,
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

    def test_fetch_registry_package_propagates_http_error(self) -> None:
        error = HTTPError("url", 404, "not found", hdrs=None, fp=io.BytesIO(b""))
        with mock.patch(
            "scripts.publish_npm_release.urlopen",
            side_effect=error,
        ):
            with self.assertRaises(HTTPError) as ctx:
                fetch_registry_package("@reviewsensei/cli", "0.5.0")
        self.assertEqual(ctx.exception.code, 404)

    def test_fetch_registry_package_wraps_transport_errors(self) -> None:
        with mock.patch(
            "scripts.publish_npm_release.urlopen",
            side_effect=TimeoutError("timed out"),
        ):
            with self.assertRaises(RegistryTransportError) as ctx:
                fetch_registry_package("@reviewsensei/cli", "0.5.0")
        self.assertIn("timed out", str(ctx.exception))

    def _version_document_url(self, package: str, version: str) -> str:
        from urllib.parse import quote

        return (
            "https://registry.npmjs.org/"
            f"{quote(package, safe='')}/{quote(version, safe='')}"
        )

    def _packument_url(self, package: str) -> str:
        from urllib.parse import quote

        return f"https://registry.npmjs.org/{quote(package, safe='')}"

    def _registry_url(self, url: object) -> str:
        from urllib.request import Request

        if isinstance(url, Request):
            return url.full_url
        return str(url)

    def test_classify_registry_state_retries_via_urlopen(self) -> None:
        package = "@reviewsensei/cli-linux-x64-gnu"
        version = "0.5.0"
        integrity = f"sha512-{package}"
        version_url = self._version_document_url(package, version)
        responses: dict[str, list[object]] = {
            version_url: [
                HTTPError("url", 404, "not found", hdrs=None, fp=io.BytesIO(b"")),
                io.BytesIO(
                    json.dumps(
                        {
                            "name": package,
                            "version": version,
                            "dist": {"integrity": integrity},
                        }
                    ).encode("utf-8")
                ),
            ],
            self._packument_url(package): [
                HTTPError("url", 404, "not found", hdrs=None, fp=io.BytesIO(b"")),
            ],
        }

        def fake_urlopen(url: str, **_kwargs: object) -> io.BytesIO:
            queue = responses[self._registry_url(url)]
            item = queue.pop(0)
            if isinstance(item, HTTPError):
                raise item
            return item

        with (
            mock.patch(
                "scripts.publish_npm_release.urlopen",
                side_effect=fake_urlopen,
            ),
            mock.patch("scripts.publish_npm_release.time.sleep") as sleep,
        ):
            action = classify_registry_state(
                package,
                version,
                integrity,
                max_attempts=3,
                preflight_404_attempts=3,
                initial_delay_seconds=2.0,
                max_delay_seconds=30.0,
            )

        self.assertEqual(action, "verified")
        sleep.assert_called_once_with(2.0)

    def test_classify_registry_state_retries_on_packument_transient_error(
        self,
    ) -> None:
        package = "@reviewsensei/cli-linux-x64-gnu"
        version = "0.5.0"
        integrity = f"sha512-{package}"
        version_url = self._version_document_url(package, version)
        packument_url = self._packument_url(package)
        responses: dict[str, list[object]] = {
            version_url: [
                HTTPError("url", 404, "not found", hdrs=None, fp=io.BytesIO(b"")),
                HTTPError("url", 404, "not found", hdrs=None, fp=io.BytesIO(b"")),
                io.BytesIO(
                    json.dumps(
                        {
                            "name": package,
                            "version": version,
                            "dist": {"integrity": integrity},
                        }
                    ).encode("utf-8")
                ),
            ],
            packument_url: [
                HTTPError("url", 503, "unavailable", hdrs=None, fp=io.BytesIO(b"")),
                io.BytesIO(
                    json.dumps(
                        {
                            "name": package,
                            "versions": {
                                version: {"name": package, "version": version}
                            },
                        }
                    ).encode("utf-8")
                ),
            ],
        }

        def fake_urlopen(url: str, **_kwargs: object) -> io.BytesIO:
            queue = responses[self._registry_url(url)]
            item = queue.pop(0)
            if isinstance(item, HTTPError):
                raise item
            return item

        with (
            mock.patch(
                "scripts.publish_npm_release.urlopen",
                side_effect=fake_urlopen,
            ),
            mock.patch("scripts.publish_npm_release.time.sleep") as sleep,
        ):
            action = classify_registry_state(
                package,
                version,
                integrity,
                max_attempts=3,
                preflight_404_attempts=1,
                initial_delay_seconds=2.0,
                max_delay_seconds=30.0,
            )

        self.assertEqual(action, "verified")
        self.assertGreaterEqual(len(sleep.mock_calls), 2)

    def test_classify_registry_state_retries_when_final_probe_is_ambiguous(
        self,
    ) -> None:
        package = "@reviewsensei/cli-darwin-x64"
        version = "0.5.0"
        integrity = f"sha512-{package}"
        version_url = self._version_document_url(package, version)
        packument_url = self._packument_url(package)
        responses: dict[str, list[object]] = {
            version_url: [
                HTTPError("url", 404, "not found", hdrs=None, fp=io.BytesIO(b"")),
                HTTPError("url", 404, "not found", hdrs=None, fp=io.BytesIO(b"")),
                io.BytesIO(
                    json.dumps(
                        {
                            "name": package,
                            "version": version,
                            "dist": {"integrity": integrity},
                        }
                    ).encode("utf-8")
                ),
            ],
            packument_url: [
                HTTPError("url", 404, "not found", hdrs=None, fp=io.BytesIO(b"")),
                HTTPError("url", 503, "unavailable", hdrs=None, fp=io.BytesIO(b"")),
                io.BytesIO(
                    json.dumps(
                        {
                            "name": package,
                            "versions": {
                                version: {"name": package, "version": version}
                            },
                        }
                    ).encode("utf-8")
                ),
            ],
        }

        def fake_urlopen(url: str, **_kwargs: object) -> io.BytesIO:
            queue = responses[self._registry_url(url)]
            item = queue.pop(0)
            if isinstance(item, HTTPError):
                raise item
            return item

        with (
            mock.patch(
                "scripts.publish_npm_release.urlopen",
                side_effect=fake_urlopen,
            ),
            mock.patch("scripts.publish_npm_release.time.sleep"),
        ):
            action = classify_registry_state(
                package,
                version,
                integrity,
                max_attempts=3,
                preflight_404_attempts=1,
                initial_delay_seconds=1.0,
                max_delay_seconds=1.0,
            )

        self.assertEqual(action, "verified")

    def test_classify_registry_state_waits_when_packument_transiently_missing(
        self,
    ) -> None:
        package = "@reviewsensei/cli-darwin-arm64"
        version = "0.5.0"
        integrity = f"sha512-{package}"
        version_url = self._version_document_url(package, version)
        packument_url = self._packument_url(package)
        indexed_packument = json.dumps(
            {
                "name": package,
                "versions": {version: {"name": package, "version": version}},
            }
        ).encode("utf-8")
        responses: dict[str, list[object]] = {
            version_url: [
                HTTPError("url", 404, "not found", hdrs=None, fp=io.BytesIO(b"")),
                HTTPError("url", 404, "not found", hdrs=None, fp=io.BytesIO(b"")),
                HTTPError("url", 404, "not found", hdrs=None, fp=io.BytesIO(b"")),
                io.BytesIO(
                    json.dumps(
                        {
                            "name": package,
                            "version": version,
                            "dist": {"integrity": integrity},
                        }
                    ).encode("utf-8")
                ),
            ],
            packument_url: [
                HTTPError("url", 404, "not found", hdrs=None, fp=io.BytesIO(b"")),
                *[io.BytesIO(indexed_packument) for _ in range(8)],
            ],
        }

        def fake_urlopen(url: str, **_kwargs: object) -> io.BytesIO:
            queue = responses[self._registry_url(url)]
            item = queue.pop(0)
            if isinstance(item, HTTPError):
                raise item
            return item

        with (
            mock.patch(
                "scripts.publish_npm_release.urlopen",
                side_effect=fake_urlopen,
            ),
            mock.patch("scripts.publish_npm_release.time.sleep"),
        ):
            action = classify_registry_state(
                package,
                version,
                integrity,
                max_attempts=4,
                preflight_404_attempts=2,
                initial_delay_seconds=1.0,
                max_delay_seconds=1.0,
            )

        self.assertEqual(action, "verified")

    def test_classify_registry_state_waits_when_packument_already_lists_version(
        self,
    ) -> None:
        package = "@reviewsensei/cli-darwin-arm64"
        version = "0.5.0"
        integrity = f"sha512-{package}"
        version_url = self._version_document_url(package, version)
        packument_url = self._packument_url(package)
        packument_body = json.dumps(
            {
                "name": package,
                "versions": {version: {"name": package, "version": version}},
            }
        ).encode("utf-8")
        responses: dict[str, list[object]] = {
            version_url: [
                HTTPError("url", 404, "not found", hdrs=None, fp=io.BytesIO(b"")),
                HTTPError("url", 404, "not found", hdrs=None, fp=io.BytesIO(b"")),
                io.BytesIO(
                    json.dumps(
                        {
                            "name": package,
                            "version": version,
                            "dist": {"integrity": integrity},
                        }
                    ).encode("utf-8")
                ),
            ],
            packument_url: [io.BytesIO(packument_body) for _ in range(6)],
        }

        def fake_urlopen(url: str, **_kwargs: object) -> io.BytesIO:
            queue = responses[self._registry_url(url)]
            item = queue.pop(0)
            if isinstance(item, HTTPError):
                raise item
            return io.BytesIO(item.read())

        with (
            mock.patch(
                "scripts.publish_npm_release.urlopen",
                side_effect=fake_urlopen,
            ),
            mock.patch("scripts.publish_npm_release.time.sleep"),
        ):
            action = classify_registry_state(
                package,
                version,
                integrity,
                max_attempts=3,
                preflight_404_attempts=1,
                initial_delay_seconds=1.0,
                max_delay_seconds=1.0,
            )

        self.assertEqual(action, "verified")

    def test_classify_registry_state_fails_when_packument_probe_stays_inconclusive(
        self,
    ) -> None:
        package = "@reviewsensei/cli-darwin-x64"
        version = "0.5.0"
        integrity = f"sha512-{package}"
        version_url = self._version_document_url(package, version)
        packument_url = self._packument_url(package)
        responses: dict[str, list[object]] = {
            version_url: [
                HTTPError("url", 404, "not found", hdrs=None, fp=io.BytesIO(b""))
            ]
            * 2,
            packument_url: [
                HTTPError("url", 503, "unavailable", hdrs=None, fp=io.BytesIO(b""))
            ]
            * 2,
        }

        def fake_urlopen(url: str, **_kwargs: object) -> io.BytesIO:
            queue = responses[self._registry_url(url)]
            item = queue.pop(0)
            if isinstance(item, HTTPError):
                raise item
            return item

        with (
            mock.patch(
                "scripts.publish_npm_release.urlopen",
                side_effect=fake_urlopen,
            ),
            mock.patch("scripts.publish_npm_release.time.sleep"),
        ):
            with self.assertRaisesRegex(
                PublishError,
                "packument probe remained inconclusive after 2 attempts",
            ):
                classify_registry_state(
                    package,
                    version,
                    integrity,
                    max_attempts=2,
                    preflight_404_attempts=1,
                    initial_delay_seconds=1.0,
                    max_delay_seconds=1.0,
                )

    def test_classify_registry_state_publishes_missing_package_when_preflight_exceeds_attempts(
        self,
    ) -> None:
        package = "@reviewsensei/cli-linux-x64-gnu"
        version = "0.5.0"
        integrity = f"sha512-{package}"
        version_url = self._version_document_url(package, version)
        packument_url = self._packument_url(package)
        responses: dict[str, list[object]] = {
            version_url: [
                HTTPError("url", 404, "not found", hdrs=None, fp=io.BytesIO(b""))
            ]
            * 2,
            packument_url: [
                HTTPError("url", 404, "not found", hdrs=None, fp=io.BytesIO(b""))
            ]
            * 4,
        }

        def fake_urlopen(url: object, **_kwargs: object) -> io.BytesIO:
            queue = responses[self._registry_url(url)]
            item = queue.pop(0)
            if isinstance(item, HTTPError):
                raise item
            return item

        with (
            mock.patch(
                "scripts.publish_npm_release.urlopen",
                side_effect=fake_urlopen,
            ),
            mock.patch("scripts.publish_npm_release.time.sleep"),
        ):
            action = classify_registry_state(
                package,
                version,
                integrity,
                max_attempts=2,
                preflight_404_attempts=5,
                initial_delay_seconds=1.0,
                max_delay_seconds=1.0,
            )

        self.assertEqual(action, "publish")

    def test_classify_registry_state_fails_closed_when_replication_never_resolves(
        self,
    ) -> None:
        package = "@reviewsensei/cli-darwin-arm64"
        version = "0.5.0"
        integrity = f"sha512-{package}"
        version_url = self._version_document_url(package, version)
        packument_url = self._packument_url(package)
        indexed_packument = json.dumps(
            {
                "name": package,
                "versions": {version: {"name": package, "version": version}},
            }
        ).encode("utf-8")
        responses: dict[str, list[object]] = {
            version_url: [
                HTTPError("url", 404, "not found", hdrs=None, fp=io.BytesIO(b""))
            ]
            * 2,
            packument_url: [io.BytesIO(indexed_packument) for _ in range(6)],
        }

        def fake_urlopen(url: object, **_kwargs: object) -> io.BytesIO:
            queue = responses[self._registry_url(url)]
            item = queue.pop(0)
            if isinstance(item, HTTPError):
                raise item
            return io.BytesIO(item.read())

        with (
            mock.patch(
                "scripts.publish_npm_release.urlopen",
                side_effect=fake_urlopen,
            ),
            mock.patch("scripts.publish_npm_release.time.sleep"),
        ):
            with self.assertRaisesRegex(
                PublishError,
                "version document replicating \\(attempt 2/2\\)",
            ):
                classify_registry_state(
                    package,
                    version,
                    integrity,
                    max_attempts=2,
                    preflight_404_attempts=5,
                    initial_delay_seconds=1.0,
                    max_delay_seconds=1.0,
                )

    def test_preflight_marks_propagating_package_verified_via_urlopen(self) -> None:
        target = "@reviewsensei/cli-darwin-arm64"
        version = "0.5.0"
        integrity = f"sha512-{target}"
        target_version_url = self._version_document_url(target, version)
        responses: dict[str, list[object]] = {
            target_version_url: [
                HTTPError("url", 404, "not found", hdrs=None, fp=io.BytesIO(b"")),
                io.BytesIO(
                    json.dumps(
                        {
                            "name": target,
                            "version": version,
                            "dist": {"integrity": integrity},
                        }
                    ).encode("utf-8")
                ),
            ],
            self._packument_url(target): [
                HTTPError("url", 404, "not found", hdrs=None, fp=io.BytesIO(b"")),
            ],
        }
        for package in ALL_PACKAGES:
            if package == target:
                continue
            version_url = self._version_document_url(package, version)
            packument_url = self._packument_url(package)
            responses[version_url] = [
                HTTPError("url", 404, "not found", hdrs=None, fp=io.BytesIO(b""))
            ] * 6
            responses[packument_url] = [
                HTTPError("url", 404, "not found", hdrs=None, fp=io.BytesIO(b""))
            ] * 12

        def fake_urlopen(url: str, **_kwargs: object) -> io.BytesIO:
            queue = responses[self._registry_url(url)]
            item = queue.pop(0)
            if isinstance(item, HTTPError):
                raise item
            return item

        with (
            mock.patch(
                "scripts.publish_npm_release.urlopen",
                side_effect=fake_urlopen,
            ),
            mock.patch("scripts.publish_npm_release.time.sleep") as sleep,
        ):
            preflight(
                self.bundle_dir,
                version,
                max_attempts=3,
                preflight_404_attempts=3,
                initial_delay_seconds=2.0,
                max_delay_seconds=30.0,
            )

        self.assertIn(mock.call(2.0), sleep.mock_calls)
        state = json.loads(
            (self.bundle_dir / "publish-state.json").read_text(encoding="utf-8")
        )
        actions = {item["name"]: item["action"] for item in state["packages"]}
        self.assertEqual(actions[target], "verified")
        self.assertNotEqual(actions[target], "publish")

    def test_read_back_retries_http_error_from_fetch(self) -> None:
        responses: list[object] = [
            HTTPError("url", 404, "not found", hdrs=None, fp=io.BytesIO(b"")),
            io.BytesIO(
                json.dumps(
                    {
                        "name": "@reviewsensei/cli-linux-x64-gnu",
                        "version": "0.5.0",
                        "dist": {
                            "integrity": "sha512-@reviewsensei/cli-linux-x64-gnu",
                        },
                    }
                ).encode("utf-8")
            ),
        ]

        def fake_urlopen(*_args: object, **_kwargs: object) -> io.BytesIO:
            item = responses.pop(0)
            if isinstance(item, HTTPError):
                raise item
            return item

        with (
            mock.patch(
                "scripts.publish_npm_release.urlopen",
                side_effect=fake_urlopen,
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

    def test_preflight_marks_missing_packages_for_publish(self) -> None:
        def fake_fetch(package: str, version: str) -> dict[str, object]:
            if package.endswith("darwin-arm64"):
                return {
                    "name": package,
                    "version": version,
                    "dist": {"integrity": f"sha512-{package}"},
                }
            raise HTTPError("url", 404, "not found", hdrs=None, fp=io.BytesIO(b""))

        with (
            mock.patch(
                "scripts.publish_npm_release.fetch_registry_package",
                side_effect=fake_fetch,
            ),
            mock.patch(
                "scripts.publish_npm_release.probe_packument_version_state",
                return_value=PackumentProbeState.VERSION_ABSENT,
            ),
            mock.patch("scripts.publish_npm_release.time.sleep"),
        ):
            preflight(
                self.bundle_dir,
                "0.5.0",
                max_attempts=3,
                preflight_404_attempts=1,
                initial_delay_seconds=1.0,
                max_delay_seconds=1.0,
            )

        state = json.loads(
            (self.bundle_dir / "publish-state.json").read_text(encoding="utf-8")
        )
        actions = {item["name"]: item["action"] for item in state["packages"]}
        self.assertEqual(actions["@reviewsensei/cli-darwin-arm64"], "verified")
        self.assertEqual(actions["@reviewsensei/cli-linux-x64-gnu"], "publish")

    def test_preflight_retries_404_until_package_is_visible(self) -> None:
        target = "@reviewsensei/cli-darwin-arm64"
        responses: dict[str, list[object]] = {
            target: [
                HTTPError("url", 404, "not found", hdrs=None, fp=io.BytesIO(b"")),
                {
                    "name": target,
                    "version": "0.5.0",
                    "dist": {"integrity": f"sha512-{target}"},
                },
            ]
        }

        def fake_fetch(package: str, version: str) -> dict[str, object]:
            queue = responses.get(package)
            if queue is None:
                raise HTTPError("url", 404, "not found", hdrs=None, fp=io.BytesIO(b""))
            item = queue.pop(0)
            if isinstance(item, HTTPError):
                raise item
            return item

        with (
            mock.patch(
                "scripts.publish_npm_release.fetch_registry_package",
                side_effect=fake_fetch,
            ),
            mock.patch(
                "scripts.publish_npm_release.probe_packument_version_state",
                return_value=PackumentProbeState.VERSION_ABSENT,
            ),
            mock.patch("scripts.publish_npm_release.time.sleep") as sleep,
        ):
            preflight(
                self.bundle_dir,
                "0.5.0",
                max_attempts=3,
                preflight_404_attempts=2,
                initial_delay_seconds=2.0,
                max_delay_seconds=30.0,
            )

        self.assertIn(mock.call(2.0), sleep.mock_calls)
        state = json.loads(
            (self.bundle_dir / "publish-state.json").read_text(encoding="utf-8")
        )
        actions = {item["name"]: item["action"] for item in state["packages"]}
        self.assertEqual(actions[target], "verified")

    def test_preflight_waits_when_package_metadata_lists_version(self) -> None:
        target = "@reviewsensei/cli-darwin-arm64"
        responses: dict[str, list[object]] = {
            target: [
                HTTPError("url", 404, "not found", hdrs=None, fp=io.BytesIO(b"")),
                {
                    "name": target,
                    "version": "0.5.0",
                    "dist": {"integrity": f"sha512-{target}"},
                },
            ]
        }

        def fake_fetch(package: str, version: str) -> dict[str, object]:
            queue = responses.get(package)
            if queue is None:
                raise HTTPError("url", 404, "not found", hdrs=None, fp=io.BytesIO(b""))
            item = queue.pop(0)
            if isinstance(item, HTTPError):
                raise item
            return item

        with (
            mock.patch(
                "scripts.publish_npm_release.fetch_registry_package",
                side_effect=fake_fetch,
            ),
            mock.patch(
                "scripts.publish_npm_release.probe_packument_version_state",
                side_effect=lambda package, version: (
                    PackumentProbeState.VERSION_INDEXED
                    if package == target
                    else PackumentProbeState.VERSION_ABSENT
                ),
            ),
            mock.patch("scripts.publish_npm_release.time.sleep") as sleep,
        ):
            preflight(
                self.bundle_dir,
                "0.5.0",
                max_attempts=3,
                preflight_404_attempts=1,
                initial_delay_seconds=2.0,
                max_delay_seconds=30.0,
            )

        self.assertGreaterEqual(len(sleep.mock_calls), 1)
        state = json.loads(
            (self.bundle_dir / "publish-state.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            state["packages"][0]["action"],
            "verified",
        )

    def test_preflight_retries_transient_http_errors(self) -> None:
        target = "@reviewsensei/cli-linux-x64-gnu"
        responses: dict[str, list[object]] = {
            target: [
                HTTPError("url", 503, "unavailable", hdrs=None, fp=io.BytesIO(b"")),
                {
                    "name": target,
                    "version": "0.5.0",
                    "dist": {"integrity": f"sha512-{target}"},
                },
            ]
        }

        def fake_fetch(package: str, version: str) -> dict[str, object]:
            queue = responses.get(package)
            if queue is None:
                raise HTTPError("url", 404, "not found", hdrs=None, fp=io.BytesIO(b""))
            item = queue.pop(0)
            if isinstance(item, HTTPError):
                raise item
            return item

        with (
            mock.patch(
                "scripts.publish_npm_release.fetch_registry_package",
                side_effect=fake_fetch,
            ),
            mock.patch(
                "scripts.publish_npm_release.probe_packument_version_state",
                return_value=PackumentProbeState.VERSION_ABSENT,
            ),
            mock.patch("scripts.publish_npm_release.time.sleep") as sleep,
        ):
            preflight(
                self.bundle_dir,
                "0.5.0",
                max_attempts=3,
                preflight_404_attempts=2,
                initial_delay_seconds=2.0,
                max_delay_seconds=30.0,
            )

        self.assertIn(mock.call(2.0), sleep.mock_calls)
        state = json.loads(
            (self.bundle_dir / "publish-state.json").read_text(encoding="utf-8")
        )
        actions = {item["name"]: item["action"] for item in state["packages"]}
        self.assertEqual(actions[target], "verified")
        self.assertEqual(actions["@reviewsensei/cli-darwin-arm64"], "publish")

    def test_load_integrity_records_rejects_version_mismatch(self) -> None:
        with self.assertRaisesRegex(PublishError, "invalid package integrity set"):
            load_integrity_records(self.bundle_dir, "0.4.0")

    def test_tarball_for_package_rejects_paths_outside_bundle(self) -> None:
        records = load_integrity_records(self.bundle_dir, "0.5.0")
        package = "@reviewsensei/cli-darwin-arm64"
        records[package] = {**records[package], "file": "../outside.tgz"}
        outside = self.bundle_dir.parent / "outside.tgz"
        outside.write_bytes(b"tarball")
        try:
            with self.assertRaisesRegex(PublishError, "escapes bundle directory"):
                tarball_for_package(self.bundle_dir, package, "0.5.0", records)
        finally:
            outside.unlink(missing_ok=True)

    def test_tarball_for_package_rejects_non_tgz_names(self) -> None:
        records = load_integrity_records(self.bundle_dir, "0.5.0")
        package = "@reviewsensei/cli-darwin-arm64"
        records[package] = {**records[package], "file": "cli-0.5.0.txt"}
        with self.assertRaisesRegex(PublishError, "invalid tarball name"):
            tarball_for_package(self.bundle_dir, package, "0.5.0", records)

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
            side_effect=RegistryTransportError(
                "registry request failed for @reviewsensei/cli: timed out"
            ),
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

        def fake_fetch(package: str, version: str) -> dict[str, object]:
            if package != target:
                return {
                    "name": package,
                    "version": version,
                    "dist": {"integrity": records[package]["integrity"]},
                }
            item = responses.pop(0)
            if isinstance(item, HTTPError):
                raise item
            return item

        with (
            mock.patch(
                "scripts.publish_npm_release.publish_tarball",
            ) as publish,
            mock.patch(
                "scripts.publish_npm_release.fetch_registry_package",
                side_effect=fake_fetch,
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

        publish.assert_called_once_with(tarball.resolve())

    def test_read_back_retries_transport_error_via_urlopen(self) -> None:
        responses: list[object] = [
            URLError("temporary failure in name resolution"),
            io.BytesIO(
                json.dumps(
                    {
                        "name": "@reviewsensei/cli",
                        "version": "0.5.0",
                        "dist": {"integrity": "sha512-@reviewsensei/cli"},
                    }
                ).encode("utf-8")
            ),
        ]

        def fake_urlopen(*_args: object, **_kwargs: object) -> io.BytesIO:
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        with (
            mock.patch(
                "scripts.publish_npm_release.urlopen",
                side_effect=fake_urlopen,
            ),
            mock.patch("scripts.publish_npm_release.time.sleep") as sleep,
        ):
            read_back_with_retry(
                "@reviewsensei/cli",
                "0.5.0",
                "sha512-@reviewsensei/cli",
                max_attempts=2,
                initial_delay_seconds=2.0,
                max_delay_seconds=30.0,
            )

        sleep.assert_called_once_with(2.0)

    def test_read_back_fails_when_integrity_mismatches(self) -> None:
        with mock.patch(
            "scripts.publish_npm_release.fetch_registry_package",
            return_value={
                "name": "@reviewsensei/cli",
                "version": "0.5.0",
                "dist": {"integrity": "sha512-wrong"},
            },
        ):
            with self.assertRaises(IntegrityMismatchError):
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
        with (
            mock.patch("scripts.publish_npm_release.publish_tarball") as publish,
            mock.patch(
                "scripts.publish_npm_release.read_back_with_retry",
            ) as readback,
        ):
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
        readback.assert_not_called()

    def test_classify_registry_state_does_not_publish_on_stale_packument_at_budget(
        self,
    ) -> None:
        package = "@reviewsensei/cli-linux-x64-gnu"
        version = "0.5.0"
        integrity = f"sha512-{package}"
        version_url = self._version_document_url(package, version)
        packument_url = self._packument_url(package)
        absent_packument = json.dumps(
            {
                "name": package,
                "versions": {"0.4.0": {"name": package, "version": "0.4.0"}},
            }
        ).encode("utf-8")
        responses: dict[str, list[object]] = {
            version_url: [
                HTTPError("url", 404, "not found", hdrs=None, fp=io.BytesIO(b"")),
                HTTPError("url", 404, "not found", hdrs=None, fp=io.BytesIO(b"")),
                io.BytesIO(
                    json.dumps(
                        {
                            "name": package,
                            "version": version,
                            "dist": {"integrity": integrity},
                        }
                    ).encode("utf-8")
                ),
            ],
            packument_url: [io.BytesIO(absent_packument) for _ in range(6)],
        }

        def fake_urlopen(url: str, **_kwargs: object) -> io.BytesIO:
            queue = responses[self._registry_url(url)]
            item = queue.pop(0)
            if isinstance(item, HTTPError):
                raise item
            return io.BytesIO(item.read())

        with (
            mock.patch(
                "scripts.publish_npm_release.urlopen",
                side_effect=fake_urlopen,
            ),
            mock.patch("scripts.publish_npm_release.time.sleep"),
        ):
            action = classify_registry_state(
                package,
                version,
                integrity,
                max_attempts=3,
                preflight_404_attempts=1,
                initial_delay_seconds=1.0,
                max_delay_seconds=1.0,
            )

        self.assertEqual(action, "verified")

    def test_preflight_keeps_partially_published_package_verified_via_urlopen(
        self,
    ) -> None:
        target = "@reviewsensei/cli-darwin-arm64"
        version = "0.5.0"
        integrity = f"sha512-{target}"
        indexed_packument = json.dumps(
            {
                "name": target,
                "versions": {version: {"name": target, "version": version}},
            }
        ).encode("utf-8")
        target_version_url = self._version_document_url(target, version)
        target_packument_url = self._packument_url(target)
        responses: dict[str, list[object]] = {
            target_version_url: [
                HTTPError("url", 404, "not found", hdrs=None, fp=io.BytesIO(b"")),
                HTTPError("url", 404, "not found", hdrs=None, fp=io.BytesIO(b"")),
                io.BytesIO(
                    json.dumps(
                        {
                            "name": target,
                            "version": version,
                            "dist": {"integrity": integrity},
                        }
                    ).encode("utf-8")
                ),
            ],
            target_packument_url: [io.BytesIO(indexed_packument) for _ in range(12)],
        }
        for package in ALL_PACKAGES:
            if package == target:
                continue
            version_url = self._version_document_url(package, version)
            packument_url = self._packument_url(package)
            responses[version_url] = [
                HTTPError("url", 404, "not found", hdrs=None, fp=io.BytesIO(b""))
            ] * 6
            responses[packument_url] = [
                HTTPError("url", 404, "not found", hdrs=None, fp=io.BytesIO(b""))
            ] * 12

        def fake_urlopen(url: str, **_kwargs: object) -> io.BytesIO:
            queue = responses[self._registry_url(url)]
            item = queue.pop(0)
            if isinstance(item, HTTPError):
                raise item
            return io.BytesIO(item.read())

        with (
            mock.patch(
                "scripts.publish_npm_release.urlopen",
                side_effect=fake_urlopen,
            ),
            mock.patch("scripts.publish_npm_release.time.sleep"),
        ):
            preflight(
                self.bundle_dir,
                version,
                max_attempts=3,
                preflight_404_attempts=1,
                initial_delay_seconds=1.0,
                max_delay_seconds=1.0,
            )

        state = json.loads(
            (self.bundle_dir / "publish-state.json").read_text(encoding="utf-8")
        )
        actions = {item["name"]: item["action"] for item in state["packages"]}
        self.assertEqual(actions[target], "verified")
        self.assertEqual(actions["@reviewsensei/cli-linux-x64-gnu"], "publish")

    def test_publish_package_recovers_from_publish_failure_when_registry_matches(
        self,
    ) -> None:
        records = load_integrity_records(self.bundle_dir, "0.5.0")
        package = "@reviewsensei/cli-darwin-x64"
        write_publish_state(
            self.bundle_dir,
            "0.5.0",
            [{"name": package, "action": "publish"}],
        )
        tarball = self.bundle_dir / records[package]["file"]
        tarball.write_bytes(b"tarball")
        with (
            mock.patch(
                "scripts.publish_npm_release.publish_tarball",
                side_effect=PublishError("npm publish failed with exit code 1"),
            ) as publish,
            mock.patch(
                "scripts.publish_npm_release.read_back_with_retry",
            ) as readback,
        ):
            publish_package(
                self.bundle_dir,
                package,
                "0.5.0",
                records,
                max_attempts=2,
                initial_delay_seconds=0.0,
                max_delay_seconds=0.0,
            )
        publish.assert_called_once()
        readback.assert_called_once()

    def test_publish_package_surfaces_integrity_mismatch_after_publish_failure(
        self,
    ) -> None:
        records = load_integrity_records(self.bundle_dir, "0.5.0")
        package = "@reviewsensei/cli-darwin-x64"
        write_publish_state(
            self.bundle_dir,
            "0.5.0",
            [{"name": package, "action": "publish"}],
        )
        tarball = self.bundle_dir / records[package]["file"]
        tarball.write_bytes(b"tarball")
        with (
            mock.patch(
                "scripts.publish_npm_release.publish_tarball",
                side_effect=PublishError("npm publish failed with exit code 1"),
            ),
            mock.patch(
                "scripts.publish_npm_release.read_back_with_retry",
                side_effect=IntegrityMismatchError(
                    "published npm bytes do not match the attested release bundle: "
                    f"{package}@0.5.0"
                ),
            ),
        ):
            with self.assertRaises(IntegrityMismatchError):
                publish_package(
                    self.bundle_dir,
                    package,
                    "0.5.0",
                    records,
                    max_attempts=1,
                    initial_delay_seconds=0.0,
                    max_delay_seconds=0.0,
                )

    def test_publish_package_reraises_publish_error_when_readback_exhausted(
        self,
    ) -> None:
        records = load_integrity_records(self.bundle_dir, "0.5.0")
        package = "@reviewsensei/cli-darwin-x64"
        write_publish_state(
            self.bundle_dir,
            "0.5.0",
            [{"name": package, "action": "publish"}],
        )
        tarball = self.bundle_dir / records[package]["file"]
        tarball.write_bytes(b"tarball")
        with (
            mock.patch(
                "scripts.publish_npm_release.publish_tarball",
                side_effect=PublishError("npm publish failed with exit code 1"),
            ),
            mock.patch(
                "scripts.publish_npm_release.read_back_with_retry",
                side_effect=PublishError(
                    "@reviewsensei/cli-darwin-x64@0.5.0 registry readback not ready yet "
                    "(HTTP 404, attempt 1/1)"
                ),
            ),
        ):
            with self.assertRaisesRegex(
                PublishError, "npm publish failed with exit code 1"
            ):
                publish_package(
                    self.bundle_dir,
                    package,
                    "0.5.0",
                    records,
                    max_attempts=1,
                    initial_delay_seconds=0.0,
                    max_delay_seconds=0.0,
                )

    def test_publish_package_trusts_preflight_for_verified_packages(self) -> None:
        records = load_integrity_records(self.bundle_dir, "0.5.0")
        package = "@reviewsensei/cli-darwin-arm64"
        write_publish_state(
            self.bundle_dir,
            "0.5.0",
            [{"name": package, "action": "verified"}],
        )
        with (
            mock.patch("scripts.publish_npm_release.publish_tarball") as publish,
            mock.patch(
                "scripts.publish_npm_release.read_back_with_retry",
            ) as readback,
        ):
            publish_package(
                self.bundle_dir,
                package,
                "0.5.0",
                records,
                max_attempts=1,
                initial_delay_seconds=0.0,
                max_delay_seconds=0.0,
            )
        publish.assert_not_called()
        readback.assert_not_called()

    def test_verify_resumed_bundle_accepts_legacy_bundle_without_metadata(self) -> None:
        from scripts.publish_npm_release import verify_resumed_bundle

        verify_resumed_bundle(
            self.bundle_dir,
            "0.5.0",
            expected_source_sha="a" * 40,
        )

    def test_verify_resumed_bundle_checks_metadata_source_sha(self) -> None:
        from scripts.publish_npm_release import verify_resumed_bundle

        (self.bundle_dir / "bundle-metadata.json").write_text(
            json.dumps({"version": "0.5.0", "source_sha": "a" * 40}) + "\n",
            encoding="utf-8",
        )
        verify_resumed_bundle(
            self.bundle_dir,
            "0.5.0",
            expected_source_sha="a" * 40,
        )
        with self.assertRaisesRegex(PublishError, "does not match attested run"):
            verify_resumed_bundle(
                self.bundle_dir,
                "0.5.0",
                expected_source_sha="b" * 40,
            )

    def test_verify_bundle_version_rejects_metadata_version_mismatch(self) -> None:
        from scripts.publish_npm_release import verify_bundle_version

        (self.bundle_dir / "bundle-metadata.json").write_text(
            json.dumps({"version": "0.4.0", "source_sha": "a" * 40}) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(PublishError, "does not match requested"):
            verify_bundle_version(self.bundle_dir, "0.5.0")


if __name__ == "__main__":
    unittest.main()

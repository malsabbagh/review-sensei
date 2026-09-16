"""Fail packaged-lane tests that do not import the installed distribution."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ALLOW_CHECKOUT_IMPORT = "REVIEWSENSEI_ALLOW_CHECKOUT_IMPORT"
CHECKOUT_ROOT = "REVIEWSENSEI_CHECKOUT_ROOT"


def is_disarmed() -> bool:
    """Return whether the deliberate opt-out is set."""

    return os.environ.get(ALLOW_CHECKOUT_IMPORT) == "1"


def checkout_src_root(checkout_root: str | Path) -> Path:
    return Path(checkout_root).resolve() / "src"


def is_loaded_from_checkout_src(
    package_file: str | Path, checkout_root: str | Path
) -> bool:
    return Path(package_file).resolve().is_relative_to(checkout_src_root(checkout_root))


def assert_distribution_import() -> Path:
    """Return the loaded package path, failing anything but an installed import.

    These lanes exist to prove the built artifact is what gets imported, so the
    check is armed by default rather than by environment variable: the loaded
    ``review_sensei`` must resolve under ``sys.prefix``. Running the lane from a
    checkout against an editable install therefore fails, which is the misimport
    the lane is for. Set ``REVIEWSENSEI_ALLOW_CHECKOUT_IMPORT=1`` to opt out
    deliberately.

    ``REVIEWSENSEI_CHECKOUT_ROOT`` is not an arming switch. When set it adds a
    second, more specific rule -- the package must not resolve under that
    checkout's ``src/`` -- and it must name a directory containing ``src/``, so a
    typo or a wrong container path fails closed instead of silently skipping
    that rule.
    """

    import review_sensei

    package_file = Path(review_sensei.__file__).resolve()
    if is_disarmed():
        return package_file
    checkout = os.environ.get(CHECKOUT_ROOT)
    if checkout:
        src_root = checkout_src_root(checkout)
        if not src_root.is_dir():
            raise AssertionError(
                f"{CHECKOUT_ROOT} does not contain a src/ directory: "
                f"{src_root} checkout={checkout}"
            )
        if is_loaded_from_checkout_src(package_file, checkout):
            raise AssertionError(
                "review_sensei loaded from checkout src/: "
                f"{package_file} checkout={checkout}"
            )
    prefix = Path(sys.prefix).resolve()
    if not package_file.is_relative_to(prefix):
        raise AssertionError(
            "review_sensei is not installed under sys.prefix: "
            f"{package_file} prefix={prefix}"
        )
    return package_file

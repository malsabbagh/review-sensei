"""Fail dist-safe tests that accidentally import checkout ``src/``."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def checkout_src_root(checkout_root: str | Path) -> Path:
    return Path(checkout_root).resolve() / "src"


def is_loaded_from_checkout_src(
    package_file: str | Path, checkout_root: str | Path
) -> bool:
    try:
        Path(package_file).resolve().relative_to(checkout_src_root(checkout_root))
    except ValueError:
        return False
    return True


def assert_distribution_import() -> Path:
    """Return the loaded package path, failing on a checkout ``src/`` import.

    The guard is armed when ``REVIEWSENSEI_CHECKOUT_ROOT`` or
    ``REVIEWSENSEI_DIST_SAFE_LANE=1`` is set so the checkout unit suite can
    still import an editable install.
    """

    import review_sensei

    package_file = Path(review_sensei.__file__).resolve()
    checkout = os.environ.get("REVIEWSENSEI_CHECKOUT_ROOT")
    if checkout and is_loaded_from_checkout_src(package_file, checkout):
        raise AssertionError(
            "review_sensei loaded from checkout src/: "
            f"{package_file} checkout={checkout}"
        )
    if os.environ.get("REVIEWSENSEI_DIST_SAFE_LANE") == "1":
        prefix = Path(sys.prefix).resolve()
        if prefix not in package_file.parents:
            raise AssertionError(
                "review_sensei is not installed under sys.prefix: "
                f"{package_file} prefix={prefix}"
            )
    return package_file

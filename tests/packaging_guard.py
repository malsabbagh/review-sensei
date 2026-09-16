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
    """Return the loaded package path, failing a non-distribution import.

    Setting ``REVIEWSENSEI_CHECKOUT_ROOT`` or ``REVIEWSENSEI_DIST_SAFE_LANE=1``
    arms the guard; with neither set it is disarmed so the checkout unit suite
    can still import an editable install. While armed the package must resolve
    under ``sys.prefix``, so an unrelated site-packages install cannot stand in
    for the freshly built wheel. It must additionally not resolve under
    ``$REVIEWSENSEI_CHECKOUT_ROOT/src``, which is only checkable when that
    variable is set.
    """

    import review_sensei

    package_file = Path(review_sensei.__file__).resolve()
    checkout = os.environ.get("REVIEWSENSEI_CHECKOUT_ROOT")
    armed = bool(checkout) or os.environ.get("REVIEWSENSEI_DIST_SAFE_LANE") == "1"
    if checkout and is_loaded_from_checkout_src(package_file, checkout):
        raise AssertionError(
            "review_sensei loaded from checkout src/: "
            f"{package_file} checkout={checkout}"
        )
    if armed:
        prefix = Path(sys.prefix).resolve()
        if not package_file.is_relative_to(prefix):
            raise AssertionError(
                "review_sensei is not installed under sys.prefix: "
                f"{package_file} prefix={prefix}"
            )
    return package_file

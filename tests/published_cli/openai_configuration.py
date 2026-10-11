#!/usr/bin/env python3
"""Run socket-free OpenAI configuration regressions against one exact wheel.

Use the clean wheel environment's interpreter with -I. No source checkout
package may satisfy imports; all installed product files must match the wheel.
"""

import argparse
import hashlib
import importlib.metadata
import importlib.util
import io
import json
import subprocess
import sys
import unittest
import zipfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()

    import review_sensei

    package = Path(review_sensei.__file__).resolve().parent
    assert Path(sys.prefix).resolve() in package.parents, (package, sys.prefix)
    assert importlib.metadata.version("review-sensei") == args.expected_version
    matched = 0
    with zipfile.ZipFile(args.wheel) as wheel:
        for name in wheel.namelist():
            if name.startswith("review_sensei/") and not name.endswith("/"):
                installed = package.parent / name
                assert installed.read_bytes() == wheel.read(name), name
                matched += 1
    assert matched > 0
    console = Path(sys.executable).parent / "review-sensei"
    version = subprocess.run(
        [str(console), "--version"], capture_output=True, text=True, check=True
    )
    assert args.expected_version in version.stdout, version.stdout

    tests = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(tests))
    spec = importlib.util.spec_from_file_location(
        "test_openai_configuration", tests / "test_openai_configuration.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromModule(module)
    )
    receipt = {
        "version": args.expected_version,
        "wheel_sha256": hashlib.sha256(args.wheel.read_bytes()).hexdigest(),
        "installed_package": str(package),
        "wheel_files_verified": matched,
        "console_version": version.stdout.strip(),
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "network": "socket connections forbidden; synthetic injected HTTPS transport only",
        "success": result.wasSuccessful(),
        "output": stream.getvalue(),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(stream.getvalue(), end="")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())

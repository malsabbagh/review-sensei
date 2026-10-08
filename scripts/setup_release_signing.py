#!/usr/bin/env python3
"""USER-RUN ONLY: generate/back up a dedicated key and configure GitHub signing."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

REPOSITORY = "malsabbagh/review-sensei"
ENVIRONMENT = "release-docs-main"
ROOT = Path(__file__).resolve().parents[1]
PUBLIC_VARIABLES = ("RELEASE_SIGNING_FINGERPRINT", "RELEASE_SIGNING_EMAIL")
SECRET_NAMES = ("RELEASE_SIGNING_PRIVATE_KEY", "RELEASE_SIGNING_PASSPHRASE")


def command(
    args: list[str], *, stdin: Path | None = None, env: dict[str, str] | None = None
) -> str:
    # No inherited stdout/stderr, shell, tracing, secret argv or error excerpts.
    with stdin.open("rb") if stdin else open(os.devnull, "rb") as handle:
        result = subprocess.run(args, stdin=handle, env=env, capture_output=True)
    if result.returncode:
        raise ValueError(
            "setup command failed; private diagnostics withheld; backup retained"
        )
    return result.stdout.decode("utf-8")


def api(path: str) -> Any:
    return json.loads(command(["gh", "api", path]))


def safe_backup(path: Path) -> Path:
    # Refuse symlink traversal and repository storage, never replace key files.
    path = path.expanduser().absolute()
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError("backup path must not traverse symlinks")
    if (
        path == ROOT
        or ROOT in path.parents
        or any((p / ".git").exists() for p in path.parents)
    ):
        raise ValueError("keep the signing backup outside every Git checkout")
    if not path.parent.is_dir():
        raise ValueError("backup parent must already exist")
    if path.exists():
        if (
            not path.is_dir()
            or path.stat().st_mode & 0o077
            or path.stat().st_uid != os.geteuid()
        ):
            raise ValueError("existing backup must be a private 0700 directory")
    else:
        path.mkdir(mode=0o700)
    return path.resolve()


def private_file(path: Path, value: str) -> None:
    with open(
        path, "x", encoding="utf-8", opener=lambda p, f: os.open(p, f, 0o600)
    ) as handle:
        handle.write(value)


def check_gates() -> None:
    for name in ("npm", "pypi"):
        value = api(f"repos/{REPOSITORY}/environments/{name}")
        reviewers = [
            rule.get("reviewers", [])
            for rule in value.get("protection_rules", [])
            if rule.get("type") == "required_reviewers"
        ]
        if (
            len(reviewers) != 1
            or len(reviewers[0]) != 1
            or (
                reviewers[0][0].get("type") != "User"
                or reviewers[0][0].get("reviewer", {}).get("id") != 13791232
                or reviewers[0][0].get("reviewer", {}).get("login") != "malsabbagh"
            )
        ):
            raise ValueError("npm/PyPI must retain Mo's sole required-reviewer gate")
    signing = api(f"repos/{REPOSITORY}/environments/{ENVIRONMENT}")
    if signing.get("deployment_branch_policy") != {
        "protected_branches": False,
        "custom_branch_policies": True,
    }:
        raise ValueError(
            "existing signing environment must restrict deployment to main"
        )
    policies = api(
        f"repos/{REPOSITORY}/environments/{ENVIRONMENT}/deployment-branch-policies"
    )
    entries = policies.get("branch_policies", [])
    if (
        policies.get("total_count") != 1
        or len(entries) != 1
        or (entries[0].get("name") != "main" or entries[0].get("type") != "branch")
    ):
        raise ValueError("existing signing environment must allow only the main branch")


def setup(backup: Path, email: str | None) -> dict[str, str]:
    if os.name != "posix":
        raise ValueError("secure key setup requires macOS/Linux file permissions")
    if email is not None and not re.fullmatch(
        r"[A-Za-z0-9._+%-]+@[A-Za-z0-9.-]+", email
    ):
        raise ValueError(
            "provide the verified email of the authenticated signing account"
        )
    for executable in ("gpg", "gpgconf", "gh"):
        if shutil.which(executable) is None:
            raise ValueError("install GnuPG and GitHub CLI before running setup")
    user = api("user")
    # A separate key registered to Mo is supported; a GitHub App cannot own
    # a user GPG key. Authentication uses the existing App, not this account.
    if user.get("login") != "malsabbagh" or user.get("id") != 13791232:
        raise ValueError("setup must use Mo's existing owner account")
    emails = json.loads(command(["gh", "api", "--paginate", "--slurp", "user/emails"]))
    if email is None:
        primary = [
            item["email"]
            for page in emails
            for item in page
            if item.get("primary") is True and item.get("verified") is True
        ]
        if len(primary) != 1 or not re.fullmatch(
            r"[A-Za-z0-9._+%-]+@[A-Za-z0-9.-]+", primary[0]
        ):
            raise ValueError("provide an explicit verified signing email")
        email = primary[0]
    if not any(
        item.get("email") == email and item.get("verified") is True
        for page in emails
        for item in page
    ):
        raise ValueError(
            "signing email must already be verified on Mo's GitHub account"
        )
    check_gates()
    existing = json.loads(
        command(["gh", "api", "--paginate", "--slurp", "user/gpg_keys"])
    )
    existing_keys = [item for page in existing for item in page]
    variables = api(f"repos/{REPOSITORY}/actions/variables?per_page=100")
    if variables.get("total_count", 0) > 100:
        raise ValueError("variable inventory is truncated; inspect setup locally")
    current = {item["name"]: item["value"] for item in variables.get("variables", [])}
    names = api(f"repos/{REPOSITORY}/environments/{ENVIRONMENT}/secrets?per_page=100")
    if names.get("total_count", 0) > 100:
        raise ValueError("secret-name inventory is truncated; inspect setup locally")
    configured = {item["name"] for item in names.get("secrets", [])}
    backup = safe_backup(backup)
    manifest = backup / "setup.json"
    home = backup / "gnupg"
    password = backup / "passphrase"
    private = backup / "private.asc"
    public = backup / "public.asc"
    previous = manifest.exists()
    if previous:
        record = json.loads(manifest.read_text(encoding="utf-8"))
        if (
            record.get("repository") != REPOSITORY
            or record.get("email") != email
            or record.get("account") != "malsabbagh"
        ):
            raise ValueError(
                "existing setup backup has a different identity; do not rotate implicitly"
            )
        fingerprint = record.get("fingerprint", "")
        if not re.fullmatch(r"[A-F0-9]{40}", fingerprint):
            raise ValueError("existing setup fingerprint is invalid")
        for path in (password, private, public, manifest):
            if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
                raise ValueError("backup contains missing, linked or non-private files")
    else:
        if configured.intersection(SECRET_NAMES) or set(current).intersection(
            PUBLIC_VARIABLES
        ):
            raise ValueError(
                "signing already configured; reuse its original backup, never replace it implicitly"
            )
        if list(backup.iterdir()):
            raise ValueError(
                "incomplete backup retained; inspect it locally, never overwrite or regenerate"
            )
        home.mkdir(mode=0o700)
        private_file(password, secrets.token_urlsafe(48))
        env = {**os.environ, "GNUPGHOME": str(home)}
        gpg = [
            "gpg",
            "--batch",
            "--no-tty",
            "--homedir",
            str(home),
            "--pinentry-mode",
            "loopback",
            "--passphrase-file",
            str(password),
        ]
        try:
            command(
                [
                    *gpg,
                    "--quick-generate-key",
                    f"ReviewSensei Release Marshal <{email}>",
                    "rsa4096",
                    "sign",
                    "1y",
                ],
                env=env,
            )
            listing = command([*gpg, "--with-colons", "--list-secret-keys"], env=env)
            fingerprints = re.findall(
                r"^fpr:::::::::([A-F0-9]{40}):", listing, re.MULTILINE
            )
            if len(fingerprints) != 1:
                raise ValueError("dedicated single signing key generation failed")
            fingerprint = fingerprints[0]
            command(
                [
                    *gpg,
                    "--armor",
                    "--output",
                    str(private),
                    "--export-secret-keys",
                    fingerprint,
                ],
                env=env,
            )
            command(
                [*gpg, "--armor", "--output", str(public), "--export", fingerprint],
                env=env,
            )
            private.chmod(0o600)
            public.chmod(0o600)
            private_file(
                manifest,
                json.dumps(
                    {
                        "repository": REPOSITORY,
                        "account": "malsabbagh",
                        "email": email,
                        "fingerprint": fingerprint,
                    },
                    indent=2,
                )
                + "\n",
            )
        finally:
            command(["gpgconf", "--homedir", str(home), "--kill", "all"], env=env)
    # Refresh metadata after generation/registration preparation. Another
    # owner setup must not silently replace the identity while keygen runs.
    variables = api(f"repos/{REPOSITORY}/actions/variables?per_page=100")
    names = api(f"repos/{REPOSITORY}/environments/{ENVIRONMENT}/secrets?per_page=100")
    if variables.get("total_count", 0) > 100 or names.get("total_count", 0) > 100:
        raise ValueError("setup metadata inventory is truncated")
    current = {item["name"]: item["value"] for item in variables.get("variables", [])}
    configured = {item["name"] for item in names.get("secrets", [])}
    # Pin any existing configuration: a rerun resumes this exact backup, but
    # never silently replaces another key's public identity or environment key.
    expected = dict(zip(PUBLIC_VARIABLES, (fingerprint, email)))
    if any(
        name in current and current[name] != value for name, value in expected.items()
    ):
        raise ValueError(
            "different signing identity is configured; explicit rotation required"
        )
    if configured.intersection(SECRET_NAMES) and any(
        current.get(name) != value for name, value in expected.items()
    ):
        raise ValueError(
            "existing secret identity is unproven; no secret replacement allowed"
        )
    if not previous and configured.intersection(SECRET_NAMES):
        raise ValueError(
            "signing secrets appeared during generation; no replacement allowed"
        )
    if not any(
        item.get("key_id", "").upper() == fingerprint[-16:] for item in existing_keys
    ):
        command(
            [
                "gh",
                "gpg-key",
                "add",
                str(public),
                "--title",
                "ReviewSensei Release Marshal (dedicated automation key)",
            ]
        )
    registered = json.loads(
        command(["gh", "api", "--paginate", "--slurp", "user/gpg_keys"])
    )
    matches = [
        item
        for page in registered
        for item in page
        if item.get("key_id", "").upper() == fingerprint[-16:]
    ]
    if len(matches) != 1 or not any(
        item.get("email") == email and item.get("verified") is True
        for item in matches[0].get("emails", [])
    ):
        raise ValueError(
            "GitHub has not verified the registered signing email; hold signing"
        )
    # Public metadata first provides an identity fence for interrupted retries.
    for name, value in expected.items():
        command(["gh", "variable", "set", name, "--repo", REPOSITORY, "--body", value])
    for name, path in zip(SECRET_NAMES, (private, password)):
        command(
            ["gh", "secret", "set", name, "--repo", REPOSITORY, "--env", ENVIRONMENT],
            stdin=path,
        )
    # Read only public metadata/secret names: GitHub never returns private values.
    confirmed = api(f"repos/{REPOSITORY}/actions/variables?per_page=100")
    readback = {item["name"]: item["value"] for item in confirmed.get("variables", [])}
    confirmed_names = api(
        f"repos/{REPOSITORY}/environments/{ENVIRONMENT}/secrets?per_page=100"
    )
    if any(readback.get(name) != value for name, value in expected.items()) or not set(
        SECRET_NAMES
    ).issubset({item["name"] for item in confirmed_names.get("secrets", [])}):
        raise ValueError(
            "setup metadata readback failed; preserve backup and retry exact setup"
        )
    registered = json.loads(
        command(["gh", "api", "--paginate", "--slurp", "user/gpg_keys"])
    )
    matches = [
        item
        for page in registered
        for item in page
        if item.get("key_id", "").upper() == fingerprint[-16:]
    ]
    if len(matches) != 1 or not any(
        item.get("email") == email and item.get("verified") is True
        for item in matches[0].get("emails", [])
    ):
        raise ValueError(
            "GitHub has not verified the registered signing email; hold signing"
        )
    return {
        "repository": REPOSITORY,
        "environment": ENVIRONMENT,
        "fingerprint": fingerprint,
        "email": email,
        "backup": str(backup),
    }


def verify_configuration() -> dict[str, str]:
    """Public metadata only; never read a local private key or secret value."""
    check_gates()
    value = api(f"repos/{REPOSITORY}/actions/variables?per_page=100")
    variables = {item["name"]: item["value"] for item in value.get("variables", [])}
    fingerprint = variables.get(PUBLIC_VARIABLES[0], "")
    email = variables.get(PUBLIC_VARIABLES[1], "")
    if not re.fullmatch(r"[A-F0-9]{40}", fingerprint) or not re.fullmatch(
        r"[A-Za-z0-9._+%-]+@[A-Za-z0-9.-]+", email
    ):
        raise ValueError("public signing identity is missing or invalid")
    names = api(f"repos/{REPOSITORY}/environments/{ENVIRONMENT}/secrets?per_page=100")
    if not set(SECRET_NAMES).issubset(
        {item["name"] for item in names.get("secrets", [])}
    ):
        raise ValueError("required environment secret names are missing")
    registered = api("users/malsabbagh/gpg_keys?per_page=100")
    matches = [
        item
        for item in registered
        if item.get("key_id", "").upper() == fingerprint[-16:]
    ]
    if (
        len(matches) != 1
        or matches[0].get("can_sign") is not True
        or matches[0].get("revoked") is not False
        or not any(
            item.get("email") == email and item.get("verified") is True
            for item in matches[0].get("emails", [])
        )
    ):
        raise ValueError("dedicated public key/email is not registered and verified")
    return {
        "repository": REPOSITORY,
        "environment": ENVIRONMENT,
        "fingerprint": fingerprint,
        "email": email,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--email")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.verify_only:
            print(json.dumps(verify_configuration()))
            print(
                "Public identity, secret names and approval gates verified; private values were not read."
            )
            return 0
        if args.backup_dir is None:
            raise ValueError("user-run setup requires a private backup-dir")
        result = setup(args.backup_dir, args.email)
        print(json.dumps(result))
        print(
            "Setup complete. Retain the private backup securely. No tag or package was published."
        )
        return 0
    except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        detail = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        print(f"signing setup stopped: {detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

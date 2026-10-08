#!/usr/bin/env python3
"""Sign one CI-qualified prepared release with the dedicated GPG key."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import prepare_release as prep
import release_docs as docs
import release_docs_bot as bot
from sync_release_docs import git

OWNER_ID = 13791232


def signing_identity() -> tuple[str, str]:
    fingerprint = os.environ.get("RELEASE_SIGNING_FINGERPRINT", "")
    email = os.environ.get("RELEASE_SIGNING_EMAIL", "")
    if not re.fullmatch(r"[A-F0-9]{40}", fingerprint):
        raise ValueError("dedicated release GPG fingerprint is not configured")
    if not re.fullmatch(r"[A-Za-z0-9._+%-]+@[A-Za-z0-9.-]+", email):
        raise ValueError("dedicated release signing email is not configured")
    return fingerprint, email


def registered_identity(fingerprint: str, email: str) -> None:
    keys = bot.api("users/malsabbagh/gpg_keys?per_page=100")
    matches = [
        key for key in keys if key.get("key_id", "").upper() == fingerprint[-16:]
    ]
    if len(matches) != 1 or (
        matches[0].get("can_sign") is not True
        or matches[0].get("revoked") is not False
        or not any(
            item.get("email") == email and item.get("verified") is True
            for item in matches[0].get("emails", [])
        )
    ):
        raise ValueError(
            "dedicated release key/email is not registered and verified on GitHub"
        )


def publication_gates() -> None:
    """Require Mo as the sole reviewer; never create or relax environments."""
    for name in ("npm", "pypi"):
        environment = docs.api(f"environments/{name}")
        rules = [
            rule
            for rule in environment.get("protection_rules", [])
            if rule.get("type") == "required_reviewers"
        ]
        if environment.get("name") != name or len(rules) != 1:
            raise ValueError("publication requires the existing owner approval gates")
        reviewers = rules[0].get("reviewers", [])
        if len(reviewers) != 1 or (
            reviewers[0].get("type") != "User"
            or reviewers[0].get("reviewer", {}).get("id") != OWNER_ID
            or reviewers[0].get("reviewer", {}).get("login") != "malsabbagh"
        ):
            raise ValueError("npm/PyPI publication must await Mo's approval")


def qualify(root: Path, sha: str, version: str, date: str) -> dict[str, Any]:
    prep.inputs(version, date, sha)
    if prep.remote_main() != sha:
        raise ValueError("main advanced before signing; prepare fresh reviewed source")
    receipt = prep.verify_commit(root, sha)
    if receipt["version"] != version or receipt["release_date"] != date:
        raise ValueError("signed release intent differs from the prepared receipt")
    if prep.ci_once(sha) is None:
        raise ValueError("prepared source CI is not terminal success")
    publication_gates()
    return receipt


def run(args: list[str], *, env: dict[str, str], cwd: Path | None = None) -> str:
    # Never print command output on failure: GPG and credential tools can
    # return private input in diagnostics. Only public tag metadata is emitted.
    result = subprocess.run(args, env=env, cwd=cwd, capture_output=True, text=True)
    if result.returncode:
        raise ValueError("release signing command failed; private diagnostics withheld")
    return result.stdout


def valid_signature(status: str, fingerprint: str) -> None:
    records = re.findall(r"^\[GNUPG:\] VALIDSIG (\S+) .*$", status, re.MULTILINE)
    if records != [fingerprint] or re.search(
        r"^\[GNUPG:\] (?:BADSIG|ERRSIG|EXPSIG|EXPKEYSIG|REVKEYSIG)\b",
        status,
        re.MULTILINE,
    ):
        raise ValueError("tag is not validly signed by the dedicated release key")


def sign(root: Path, sha: str, version: str, date: str) -> dict[str, str]:
    prep.workflow_context()
    fingerprint, email = signing_identity()
    if git(root, "remote", "get-url", "origin").removesuffix(".git") != (
        f"https://github.com/{docs.REPOSITORY}"
    ):
        raise ValueError("signing origin must be the fixed release repository")
    bot.check(
        os.environ.get("RELEASE_DOCS_APP_ID", ""),
        os.environ.get("RELEASE_DOCS_APP_SLUG", ""),
    )
    registered_identity(fingerprint, email)
    qualify(root, sha, version, date)
    # Deliberately refuse even a same-version existing tag. Publication retry
    # belongs to the existing Release run, never a second signing operation.
    prep.unused(version)
    tag = "v" + version
    with tempfile.TemporaryDirectory(prefix="release-signing-") as temporary:
        home = Path(temporary)
        home.chmod(0o700)
        key = home / "key.asc"
        password = home / "passphrase"
        for path, variable in (
            (key, "RELEASE_SIGNING_PRIVATE_KEY"),
            (password, "RELEASE_SIGNING_PASSPHRASE"),
        ):
            value = os.environ.get(variable, "")
            if not value:
                raise ValueError("dedicated signing secret is missing")
            with open(
                path, "x", encoding="utf-8", opener=lambda p, f: os.open(p, f, 0o600)
            ) as handle:
                handle.write(value)
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"RELEASE_SIGNING_PRIVATE_KEY", "RELEASE_SIGNING_PASSPHRASE"}
            and not key.startswith("GIT_CONFIG_")
        }
        env.update(
            GNUPGHOME=str(home), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull
        )
        gpg = ["gpg", "--batch", "--no-tty", "--homedir", str(home)]
        created = False
        try:
            run([*gpg, "--import", str(key)], env=env)
            keys = run([*gpg, "--with-colons", "--list-secret-keys"], env=env)
            fingerprints = re.findall(r"^fpr:::::::::([A-F0-9]+):", keys, re.MULTILINE)
            if fingerprints != [fingerprint]:
                raise ValueError(
                    "imported key differs from the pinned single signing key"
                )
            uids = [
                line.split(":")[9]
                for line in keys.splitlines()
                if line.startswith("uid:")
            ]
            if uids != [f"ReviewSensei Release Marshal <{email}>"]:
                raise ValueError(
                    "imported dedicated key identity differs from the tagger"
                )
            # This wrapper contains a path, never the passphrase or key bytes.
            wrapper = home / "gpg-sign"
            wrapper.write_text(
                "#!/bin/sh\nexec gpg --batch --no-tty --pinentry-mode loopback "
                '--passphrase-file "$GNUPGHOME/passphrase" "$@"\n',
                encoding="utf-8",
            )
            wrapper.chmod(0o700)
            command = [
                "git",
                "-C",
                str(root),
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "gpg.format=openpgp",
                "-c",
                f"gpg.program={wrapper}",
                "-c",
                f"user.signingkey={fingerprint}!",
                "-c",
                "user.name=ReviewSensei Release Marshal",
                "-c",
                f"user.email={email}",
            ]
            run(
                [*command, "tag", "--sign", tag, sha, "-m", f"ReviewSensei {version}"],
                env=env,
            )
            created = True
            tag_object = git(root, "rev-parse", f"refs/tags/{tag}")
            verified = subprocess.run(
                [*command, "verify-tag", "--raw", tag],
                env=env,
                capture_output=True,
                text=True,
            )
            if verified.returncode:
                raise ValueError("dedicated tag signature failed local verification")
            valid_signature(verified.stderr, fingerprint)
            # Refresh every authority after importing/signing, just before write.
            qualify(root, sha, version, date)
            prep.unused(version)
            bot.check(
                os.environ["RELEASE_DOCS_APP_ID"], os.environ["RELEASE_DOCS_APP_SLUG"]
            )
            registered_identity(fingerprint, email)
            if prep.remote_main() != sha or prep.ci_once(sha) is None:
                raise ValueError("main/CI advanced before immutable tag push")
            pushed = subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "-c",
                    "credential.helper=",
                    "-c",
                    "credential.helper=!gh auth git-credential",
                    "push",
                    "origin",
                    f"{tag_object}:refs/tags/{tag}",
                ],
                env=env,
                capture_output=True,
                text=True,
            )
            # Reconcile a dropped push response only against this exact verified
            # object. Never delete, update or force an existing remote tag.
            remote_object, remote_source = docs.tag_identity(tag)
            if (remote_object, remote_source) != (tag_object, sha):
                raise ValueError(
                    "remote signed tag does not match the exact created object"
                )
            if pushed.returncode and remote_object != tag_object:
                raise ValueError("immutable signed tag push failed")
            return {
                "tag": tag,
                "source_sha": sha,
                "tag_object_sha": tag_object,
                "fingerprint": fingerprint,
            }
        finally:
            # Ephemeral runner keyring; no private artifact/cache is uploaded.
            subprocess.run(
                ["gpgconf", "--homedir", str(home), "--kill", "all"],
                env=env,
                capture_output=True,
            )
            if created:
                git(root, "tag", "--delete", tag)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-publication-gates", action="store_true")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--sha")
    parser.add_argument("--version")
    parser.add_argument("--date")
    args = parser.parse_args()
    try:
        if args.check_publication_gates:
            publication_gates()
            print("npm/PyPI retain Mo's required-reviewer gates")
            return 0
        if any(
            value is None for value in (args.root, args.sha, args.version, args.date)
        ):
            raise ValueError("signing requires root, exact SHA, version and date")
        result = sign(args.root.resolve(), args.sha, args.version, args.date)
        print(json.dumps(result))
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with Path(summary).open("a", encoding="utf-8") as handle:
                handle.write(
                    f"\nDedicated-key signed `{result['tag']}` on `{result['source_sha']}`; "
                    f"GitHub verified tag object `{result['tag_object_sha']}`.\n\n"
                    "Release builds from this source. npm and PyPI each await Mo's environment approval. "
                    "Approval authorizes publication; it is not a cryptographic signature.\n"
                )
    except (
        ValueError,
        OSError,
        KeyError,
        TypeError,
        subprocess.CalledProcessError,
    ) as exc:
        detail = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        print(f"release signing stopped: {detail}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

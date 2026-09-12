"""Cut a release: bump the manifest, tag it, and publish it on GitHub.

HACS reads the version from `manifest.json` of the downloaded tag, so the tag
and the manifest have to agree or an install reports the wrong version forever.
This bumps one and derives the other.

Versions are semver, because that is what HACS sorts releases by and what
`v0.1.0-rc.1` means to GitHub. A version with a pre-release component is
published as a GitHub pre-release, which HACS only offers to users who asked to
see them.

    mise run release 0.1.0-rc.1
    mise run release 0.1.0 --dry-run
"""

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Final, NoReturn

MANIFEST: Final = Path("custom_components") / "nisc_smarthub" / "manifest.json"
RELEASE_BRANCH: Final = "main"
# https://semver.org, the suggested pattern with its named groups dropped.
SEMVER: Final = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)


def git(*args: str) -> str:
    """Run a read-only git command and return its trimmed output."""
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def run(command: list[str], *, dry_run: bool) -> None:
    """Run a command that changes the world, or print it under --dry-run."""
    if dry_run:
        print("would run:", " ".join(command))
        return
    subprocess.run(command, check=True)


def refuse(message: str) -> NoReturn:
    """Abort the release."""
    raise SystemExit(f"refusing to release: {message}")


def check(version: str) -> None:
    """Refuse every state a release must not be cut from."""
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    if branch != RELEASE_BRANCH:
        refuse(f"the checkout is on {branch}, not {RELEASE_BRANCH}")

    if git("status", "--porcelain"):
        refuse("the working tree has uncommitted changes")

    tag = f"v{version}"
    if git("tag", "--list", tag):
        refuse(f"{tag} already exists")


def bump(version: str, *, dry_run: bool) -> None:
    """Write the version into the manifest, keeping its formatting."""
    text = MANIFEST.read_text()
    bumped = re.sub(r'("version": ")[^"]*(")', rf"\g<1>{version}\g<2>", text, count=1)
    if bumped == text:
        refuse(f"{MANIFEST} already says {json.loads(text)['version']}")
    if dry_run:
        print(f"would write {MANIFEST} version {version}")
        return
    MANIFEST.write_text(bumped)


def main() -> int:
    """Cut the release named on the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="the semver version to release, without a v")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run every check and print what would change",
    )
    arguments = parser.parse_args()
    version = arguments.version
    dry_run = arguments.dry_run
    tag = f"v{version}"

    parsed = SEMVER.fullmatch(version)
    if parsed is None:
        refuse(f"{version!r} is not a semver version, such as 0.1.0 or 0.1.0-rc.1")
    prerelease = parsed.group(4) is not None

    check(version)
    bump(version, dry_run=dry_run)
    run(["git", "commit", "-m", f"Release {tag}", "--", str(MANIFEST)], dry_run=dry_run)
    run(["git", "tag", "-a", tag, "-m", tag], dry_run=dry_run)
    run(["git", "push", "origin", RELEASE_BRANCH, tag], dry_run=dry_run)
    run(
        ["gh", "release", "create", tag, "--generate-notes"]
        + (["--prerelease"] if prerelease else []),
        dry_run=dry_run,
    )
    print(f"released {tag}{' as a pre-release' if prerelease else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

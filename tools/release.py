"""Release-version bumper for ATLAS.

Keeps the version in lockstep across the two places it lives:

* ``config.py`` -> ``APP_VERSION``
* ``ATLAS.nsi`` -> ``!define ATLAS_VERSION`` AND ``!define ATLAS_VERSION_4PART``
  (NSIS wants the Windows MAJOR.MINOR.PATCH.BUILD form, build always 0)

Usage::

    python tools/release.py 2.0.5            # set version explicitly
    python tools/release.py --patch          # 2.0.4 -> 2.0.5
    python tools/release.py --minor          # 2.0.4 -> 2.1.0
    python tools/release.py --major          # 2.0.4 -> 3.0.0
    python tools/release.py --hotfix         # 2.0.6 -> 2.0.6.1
    python tools/release.py --patch --dry-run    # show diff only
    python tools/release.py --patch --commit     # also git-add + commit + tag
    python tools/release.py --patch --commit --push   # commit + tag + push

By default the script ONLY edits the files. ``--commit`` is opt-in so you
can review the diff first. ``--push`` requires ``--commit``.

If ``config.APP_VERSION`` and ``ATLAS.nsi``'s ``ATLAS_VERSION`` are out of
sync going in, the script refuses to proceed and prints both values —
fix manually then re-run.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PY = ROOT / "config.py"
ATLAS_NSI = ROOT / "ATLAS.nsi"

# 3-part (X.Y.Z) is the normal case; 4-part (X.Y.Z.W) is for hotfix
# point releases on top of a tagged patch (e.g. 2.0.6.1 fixes 2.0.6
# without burning a 2.0.7 number that would otherwise be reserved for
# the next feature drop).
SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?$")

# Capture the version string inside the quotes so a single sub() can both
# locate and replace it. The bumper rewrites only the captured group.
CONFIG_VERSION_RE = re.compile(
    r'^(APP_VERSION\s*=\s*")(\d+\.\d+\.\d+(?:\.\d+)?)(")', re.MULTILINE
)
NSI_VERSION_RE = re.compile(
    r'^(!define\s+ATLAS_VERSION\s+")(\d+\.\d+\.\d+(?:\.\d+)?)(")', re.MULTILINE
)
NSI_VERSION_4PART_RE = re.compile(
    r'^(!define\s+ATLAS_VERSION_4PART\s+")(\d+\.\d+\.\d+\.\d+)(")', re.MULTILINE
)


def _read_current_version_config() -> str:
    text = CONFIG_PY.read_text(encoding="utf-8")
    m = CONFIG_VERSION_RE.search(text)
    if not m:
        raise SystemExit(f"[!] Could not find APP_VERSION in {CONFIG_PY}")
    return m.group(2)


def _read_current_version_nsi() -> tuple[str, str]:
    text = ATLAS_NSI.read_text(encoding="utf-8")
    m = NSI_VERSION_RE.search(text)
    if not m:
        raise SystemExit(f"[!] Could not find ATLAS_VERSION in {ATLAS_NSI}")
    m4 = NSI_VERSION_4PART_RE.search(text)
    if not m4:
        raise SystemExit(
            f"[!] Could not find ATLAS_VERSION_4PART in {ATLAS_NSI}"
        )
    return m.group(2), m4.group(2)


def _parse_semver(s: str) -> tuple[int, int, int, int]:
    """Return ``(major, minor, patch, hotfix)``. ``hotfix`` is 0 when the
    input is 3-part. The 4-tuple form makes ordering comparisons across
    3-part and 4-part inputs trivial (``2.0.6`` < ``2.0.6.1`` because
    ``(2,0,6,0) < (2,0,6,1)``)."""
    m = SEMVER_RE.match(s)
    if not m:
        raise SystemExit(
            f"[!] Version must be MAJOR.MINOR.PATCH or MAJOR.MINOR.PATCH.HOTFIX "
            f"(got {s!r})"
        )
    hotfix = int(m.group(4)) if m.group(4) else 0
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), hotfix


def _bump(version: str, kind: str) -> str:
    major, minor, patch, hotfix = _parse_semver(version)
    if kind == "major":
        return f"{major + 1}.0.0"
    if kind == "minor":
        return f"{major}.{minor + 1}.0"
    if kind == "patch":
        # A patch bump always lands on the next 3-part release — it
        # never auto-increments the hotfix counter, since a hotfix
        # number is a deliberate "fix the prior release in place"
        # choice (user must pass --hotfix or an explicit version).
        return f"{major}.{minor}.{patch + 1}"
    if kind == "hotfix":
        return f"{major}.{minor}.{patch}.{hotfix + 1}"
    raise SystemExit(f"[!] Unknown bump kind {kind!r}")


def _is_four_part(version: str) -> bool:
    return version.count(".") == 3


def _to_4part(version: str) -> str:
    """Convert any accepted semver to the Windows 4-part form for NSI."""
    if _is_four_part(version):
        return version
    return f"{version}.0"


def _write(path: Path, pattern: re.Pattern, new_version: str) -> bool:
    text = path.read_text(encoding="utf-8")
    new_text, n = pattern.subn(rf"\g<1>{new_version}\g<3>", text, count=1)
    if n != 1:
        raise SystemExit(
            f"[!] Pattern {pattern.pattern!r} matched {n} times in {path} "
            f"(expected exactly 1)"
        )
    if new_text == text:
        return False
    path.write_text(new_text, encoding="utf-8")
    return True


def _run_git(args: list[str]) -> None:
    """Run a git command from the repo root, raising on non-zero exit."""
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise SystemExit(
            f"[!] git {' '.join(args)} exited {result.returncode}"
        )
    sys.stdout.write(result.stdout)


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "version", nargs="?",
        help="Explicit version (e.g. 2.0.5).",
    )
    target.add_argument("--major", action="store_true", help="Bump MAJOR.")
    target.add_argument("--minor", action="store_true", help="Bump MINOR.")
    target.add_argument("--patch", action="store_true", help="Bump PATCH.")
    target.add_argument(
        "--hotfix", action="store_true",
        help="Bump HOTFIX (4-part, e.g. 2.0.6 -> 2.0.6.1).",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Show what would change; don't write files.",
    )
    p.add_argument(
        "--commit", action="store_true",
        help="git add + commit + create vX.Y.Z tag after writing.",
    )
    p.add_argument(
        "--push", action="store_true",
        help="git push origin HEAD + the new tag (requires --commit).",
    )
    args = p.parse_args()

    if args.push and not args.commit:
        p.error("--push requires --commit")

    # Sanity: the two files must already be in lockstep before we touch them.
    cur_config = _read_current_version_config()
    cur_nsi, cur_nsi_4part = _read_current_version_nsi()
    if cur_config != cur_nsi:
        raise SystemExit(
            f"[!] config.py APP_VERSION = {cur_config!r} but ATLAS.nsi "
            f"ATLAS_VERSION = {cur_nsi!r}. Fix the mismatch first."
        )
    expected_4part = _to_4part(cur_nsi)
    if cur_nsi_4part != expected_4part:
        raise SystemExit(
            f"[!] ATLAS.nsi ATLAS_VERSION_4PART = {cur_nsi_4part!r} but "
            f"expected {expected_4part!r}. Fix manually first."
        )

    # Resolve target version.
    if args.version is not None:
        new_version = args.version.lstrip("v")
        _parse_semver(new_version)
    else:
        kind = (
            "major" if args.major else
            "minor" if args.minor else
            "hotfix" if args.hotfix else
            "patch"
        )
        new_version = _bump(cur_config, kind)

    if new_version == cur_config:
        raise SystemExit(
            f"[!] New version {new_version} matches current — nothing to do."
        )
    if tuple(_parse_semver(new_version)) <= tuple(_parse_semver(cur_config)):
        raise SystemExit(
            f"[!] New version {new_version} is not strictly newer than "
            f"current {cur_config}. Refusing to downgrade."
        )

    new_4part = _to_4part(new_version)

    print(f"Current: {cur_config}")
    print(f"New    : {new_version}")
    print(f"Files  : {CONFIG_PY.name}, {ATLAS_NSI.name}")

    if args.dry_run:
        print("[dry-run] No files written.")
        return 0

    _write(CONFIG_PY, CONFIG_VERSION_RE, new_version)
    _write(ATLAS_NSI, NSI_VERSION_RE, new_version)
    _write(ATLAS_NSI, NSI_VERSION_4PART_RE, new_4part)
    print(f"[OK] Bumped to {new_version} ({new_4part} in NSI 4-part form).")

    if args.commit:
        tag = f"v{new_version}"
        # Refuse if the tag already exists locally — would clobber history.
        existing = subprocess.run(
            ["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}"],
            cwd=ROOT, capture_output=True,
        )
        if existing.returncode == 0:
            raise SystemExit(
                f"[!] Tag {tag} already exists locally. Delete it first "
                f"(`git tag -d {tag}`) or pick a different version."
            )
        print(f"[*] git add + commit + tag {tag} ...")
        _run_git(["add", str(CONFIG_PY.relative_to(ROOT)),
                  str(ATLAS_NSI.relative_to(ROOT))])
        _run_git(["commit", "-m", f"Release {tag}"])
        _run_git(["tag", "-a", tag, "-m", f"ATLAS {tag}"])
        print(f"[OK] Committed and tagged {tag}.")

    if args.push:
        print("[*] Pushing commit and tag to origin ...")
        _run_git(["push", "origin", "HEAD"])
        _run_git(["push", "origin", f"v{new_version}"])
        print("[OK] Pushed.")
        print()
        print("Next steps:")
        print(f"  1. build.bat --clean --release         (signed setup.exe)")
        print(f"  2. Create GitHub Release at:")
        print(
            f"     https://github.com/Chees3loaf/Network-Inventory-Update/releases/new"
            f"?tag=v{new_version}"
        )
        print(f"  3. Attach dist\\ATLAS_Setup.exe, paste the SHA-256 line.")
    elif args.commit:
        print()
        print("Next steps:")
        print(f"  - `git push origin HEAD && git push origin v{new_version}`")
        print(f"  - build.bat --clean --release")
        print(f"  - Create GitHub Release with the resulting Setup.exe.")
    else:
        print()
        print("Files updated but NOT committed. Review with `git diff` then:")
        print(
            f"  python tools/release.py {new_version} --commit "
            f"# or re-run with --commit"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Regenerate requirements.lock — hash-pinned dependency lock file (F017).

Run from the project root with the target Python interpreter:

    python scripts/generate_requirements_lock.py

This produces ``requirements.lock`` next to ``requirements.txt``. Install it
with strict integrity checking via:

    pip install --require-hashes -r requirements.lock

The lock file is platform- and Python-version-specific (it contains wheel
hashes for the current interpreter). Regenerate after upgrading any package
in ``requirements.txt``.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    req_in = repo_root / "requirements.txt"
    req_out = repo_root / "requirements.lock"

    if not req_in.exists():
        print(f"ERROR: {req_in} not found", file=sys.stderr)
        return 1

    report_path = Path(tempfile.mktemp(suffix=".json"))
    try:
        proc = subprocess.run(
            [
                sys.executable, "-m", "pip", "install",
                "--dry-run", "--no-deps", "--quiet",
                "--ignore-installed",
                "--report", str(report_path),
                "-r", str(req_in),
            ],
            capture_output=True, text=True, timeout=600,
        )
        if proc.returncode != 0:
            print("pip install --dry-run failed:", file=sys.stderr)
            print(proc.stderr, file=sys.stderr)
            return proc.returncode

        data = json.loads(report_path.read_text(encoding="utf-8"))
    finally:
        try:
            report_path.unlink()
        except OSError:
            pass

    entries = []
    for item in data.get("install", []):
        md = item.get("metadata", {})
        name = md.get("name", "")
        version = md.get("version", "")
        archive = item.get("download_info", {}).get("archive_info", {}) or {}
        h = archive.get("hash", "")
        if not (name and version and h.startswith("sha256=")):
            print(f"WARN: missing hash for {name}=={version}", file=sys.stderr)
            continue
        entries.append((name, version, "sha256:" + h[len("sha256="):]))

    entries.sort(key=lambda e: e[0].lower())

    lines = [
        "# Auto-generated hash-pinned dependency lock file (F017).",
        "# Regenerate with: python scripts/generate_requirements_lock.py",
        "# Install with: pip install --require-hashes -r requirements.lock",
        "",
    ]
    for name, ver, h in entries:
        lines.append(f"{name}=={ver} \\")
        lines.append(f"    --hash={h}")

    req_out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {req_out} ({len(entries)} packages)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

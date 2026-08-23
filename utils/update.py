"""ATLAS auto-update manager.

Supports two runtime modes:

* **Dev / source tree** — when running from a checked-out repository, updates
  pull from git (signature-enforced, branch-restricted) and re-exec via
  ``os.execl``. This is the legacy code path; behaviour is unchanged.

* **Installed / frozen** — when running from the PyInstaller-built
  ``ATLAS.exe`` (per-user install at ``%LOCALAPPDATA%\\Programs\\ATLAS``), the
  updater queries the configured GitHub Releases feed, downloads the
  ``ATLAS_Setup.exe`` asset attached to the latest release, verifies its
  SHA-256 against the release notes (if a hash is published), and launches
  the installer. The installer overwrites the install directory and
  re-launches ATLAS via NSIS's ``MUI_FINISHPAGE_RUN``.

Mode is detected automatically from ``sys.frozen``. Callers don't need to
change anything — ``check_for_updates`` / ``apply_update`` / ``restart_program``
work in both modes.
"""
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from typing import List, Optional, Tuple

import config

# Suppress console windows when running as a frozen GUI app on Windows.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0) | _NO_WINDOW

# Hard cap on the startup ``git fetch``/``git status`` update probe. The
# check runs synchronously on the launch path, so an unreachable or slow
# remote must not hold the GUI hostage -- a fetch that blows this budget is
# treated as "no update available" (same as any other probe failure). Tune
# via the ATLAS_UPDATE_CHECK_TIMEOUT env var if a site needs more headroom.
try:
    _GIT_CHECK_TIMEOUT = float(os.environ.get("ATLAS_UPDATE_CHECK_TIMEOUT", "8"))
except (TypeError, ValueError):
    _GIT_CHECK_TIMEOUT = 8.0

# SemVer-ish version tag regex. Tolerates an optional leading "v" and
# pre-release suffixes ("-rc.1", "-beta.2"). Captures the numeric portion
# for comparison.
_VERSION_RE = re.compile(
    r"^v?(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?:\.(?P<hotfix>\d+))?(?:[-+].*)?$"
)


def _parse_version(s: str) -> Optional[Tuple[int, int, int, int]]:
    """Return ``(major, minor, patch, hotfix)`` for *s* or ``None`` if
    unparseable. Three-part inputs get a ``hotfix=0`` so the 4-tuple
    comparison naturally orders ``2.0.6`` before ``2.0.6.1``.
    """
    if not s:
        return None
    m = _VERSION_RE.match(s.strip())
    if not m:
        return None
    hotfix = int(m.group("hotfix")) if m.group("hotfix") else 0
    return (
        int(m.group("major")),
        int(m.group("minor")),
        int(m.group("patch")),
        hotfix,
    )


def _is_newer(latest: str, current: str) -> bool:
    """Return True when *latest* is a strictly newer version than *current*."""
    a = _parse_version(latest)
    b = _parse_version(current)
    if a is None or b is None:
        return False
    return a > b


def _running_as_frozen() -> bool:
    """True when running from a PyInstaller / py2exe frozen build."""
    return bool(getattr(sys, "frozen", False))


class Updater:
    """Secure update manager with mode-aware update plumbing.

    See module docstring for the two supported runtime modes.
    """

    ALLOWED_UPDATE_BRANCHES = ["main", "master"]

    _GPG_GOOD_STATUSES = ("G", "U")

    def __init__(
        self,
        repo_path: Optional[str] = None,
        enforce_signatures: bool = True,
        *,
        github_owner: Optional[str] = None,
        github_repo: Optional[str] = None,
        installer_asset_name: Optional[str] = None,
        current_version: Optional[str] = None,
    ):
        self.confirmation_callback = None  # For GUI integration

        # Auto-detect runtime mode. Installed builds skip every git-related
        # operation; dev builds keep the existing git pull flow.
        self.installed_mode = _running_as_frozen()

        # ----- Installed-mode config -----
        env_repo = os.environ.get("LAMIS_UPDATE_REPO", "").strip()
        if env_repo and "/" in env_repo:
            env_owner, env_name = env_repo.split("/", 1)
        else:
            env_owner = env_name = ""
        self.github_owner = github_owner or env_owner or getattr(config, "GITHUB_OWNER", "")
        self.github_repo = github_repo or env_name or getattr(config, "GITHUB_REPO", "")
        self.installer_asset_name = installer_asset_name or getattr(
            config, "INSTALLER_ASSET_NAME", "ATLAS_Setup.exe"
        )
        self.current_version = current_version or getattr(config, "APP_VERSION", "0.0.0")

        # State produced by check_for_updates() (installed mode).
        self._latest_release: Optional[dict] = None
        self._installer_path: Optional[str] = None

        # ----- Dev-mode config -----
        env_opt_out = os.environ.get("LAMIS_ALLOW_UNSIGNED_UPDATES", "").strip() in (
            "1", "true", "yes",
        )
        self.enforce_signatures = enforce_signatures and not env_opt_out
        if not self.enforce_signatures and not self.installed_mode:
            logging.warning(
                "[UPDATE] GPG signature enforcement DISABLED — updates may be "
                "applied from unsigned/untrusted commits. Set "
                "LAMIS_ALLOW_UNSIGNED_UPDATES=0 (or unset it) and pass "
                "enforce_signatures=True to re-enable."
            )

        # Dev mode requires a working git tree. In installed mode the
        # caller's repo_path argument is ignored entirely.
        self.repo_path = repo_path or ""
        if not self.installed_mode:
            if not self.repo_path or not os.path.exists(
                os.path.join(self.repo_path, ".git")
            ):
                raise FileNotFoundError(
                    f"Repository not found at {self.repo_path!r}. "
                    f"Pass a valid git working tree, or run from the "
                    f"installed build (ATLAS.exe)."
                )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_confirmation_callback(self, callback):
        """Set callback function for user confirmation dialog (for GUI integration)."""
        self.confirmation_callback = callback

    def check_for_updates(self) -> bool:
        """Return True when a newer version is available."""
        if self.installed_mode:
            return self._check_for_updates_installed()
        return self._check_for_updates_git()

    def apply_update(self) -> Tuple[bool, str]:
        """Apply updates with full security checks and user confirmation."""
        if self.installed_mode:
            return self._apply_update_installed()
        return self._apply_update_git()

    def restart_program(self) -> None:
        """Restart the program after updates are applied.

        In installed mode the NSIS installer auto-launches the freshly
        installed ATLAS.exe via ``MUI_FINISHPAGE_RUN``, so the only thing
        this needs to do is exit the current process cleanly.
        """
        if self.installed_mode:
            logging.info(
                "Installed-mode update applied — exiting so the installer's "
                "post-install hook can launch the new ATLAS.exe."
            )
            os._exit(0)
            return
        argv = self._safe_restart_argv()
        logging.info(
            "Restarting ATLAS (argv0=%s, extra_args=%d)",
            os.path.basename(argv[0]) if argv else "<none>",
            max(0, len(argv) - 1),
        )
        try:
            os.execl(sys.executable, sys.executable, *argv)
        except Exception as e:
            logging.error(f"Error while restarting the program: {e}")

    # ------------------------------------------------------------------
    # Installed-mode: GitHub Releases path
    # ------------------------------------------------------------------

    def _github_releases_url(self) -> str:
        return (
            f"https://api.github.com/repos/{self.github_owner}/"
            f"{self.github_repo}/releases/latest"
        )

    def _fetch_latest_release(self) -> Optional[dict]:
        """Query GitHub's Releases API for the latest published release."""
        if not self.github_owner or not self.github_repo:
            logging.warning("[UPDATE] GitHub owner/repo not configured")
            return None
        url = self._github_releases_url()
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"ATLAS-Updater/{self.current_version}",
            },
        )
        try:
            with urllib.request.urlopen(
                req, timeout=getattr(config, "UPDATE_HTTP_TIMEOUT_S", 15)
            ) as resp:
                data = resp.read()
            return json.loads(data.decode("utf-8"))
        except urllib.error.HTTPError as e:
            logging.warning(f"[UPDATE] GitHub releases API returned HTTP {e.code}")
        except urllib.error.URLError as e:
            logging.warning(f"[UPDATE] Could not reach GitHub: {e.reason}")
        except (json.JSONDecodeError, ValueError) as e:
            logging.warning(f"[UPDATE] Invalid release JSON from GitHub: {e}")
        return None

    @staticmethod
    def _extract_sha256_hint(release: dict) -> Optional[str]:
        """Pull a SHA-256 hash from the release notes if the maintainer
        published one. Looks for a 64-hex token next to the installer name
        in the body — tolerant of `**ATLAS_Setup.exe** sha256: <hash>` or
        plain `<hash>  ATLAS_Setup.exe` formats."""
        body = release.get("body") or ""
        hex_re = re.compile(r"\b([0-9a-fA-F]{64})\b")
        for line in body.splitlines():
            if "ATLAS_Setup.exe" not in line:
                continue
            m = hex_re.search(line)
            if m:
                return m.group(1).lower()
        # Final fallback: first 64-hex token anywhere in the body.
        m = hex_re.search(body)
        return m.group(1).lower() if m else None

    def _check_for_updates_installed(self) -> bool:
        release = self._fetch_latest_release()
        if release is None:
            return False
        tag = release.get("tag_name") or release.get("name") or ""
        if not _is_newer(tag, self.current_version):
            logging.info(
                f"[UPDATE] No update available (installed {self.current_version}, "
                f"latest {tag or 'unknown'})"
            )
            return False
        logging.info(
            f"[UPDATE] New release available: {tag} "
            f"(installed {self.current_version})"
        )
        self._latest_release = release
        return True

    def _download_installer(self, release: dict) -> Tuple[Optional[str], Optional[str]]:
        """Download the installer asset attached to *release*.

        Returns ``(path, error)`` — ``path`` is set on success, ``error`` on
        failure. Verifies SHA-256 if the release notes publish one.
        """
        assets = release.get("assets") or []
        asset = next(
            (a for a in assets if a.get("name") == self.installer_asset_name),
            None,
        )
        if asset is None:
            return None, (
                f"Installer asset '{self.installer_asset_name}' missing from "
                f"release {release.get('tag_name')!r}"
            )

        url = asset.get("browser_download_url")
        size = asset.get("size") or 0
        if not url:
            return None, "Installer asset has no download URL"

        # Save to a unique tmpfile inside the user's TEMP dir.
        version = release.get("tag_name") or "update"
        safe_version = re.sub(r"[^A-Za-z0-9._-]+", "_", version)
        fd, tmp_path = tempfile.mkstemp(
            prefix=f"ATLAS_Setup_{safe_version}_",
            suffix=".exe",
        )
        os.close(fd)

        req = urllib.request.Request(
            url,
            headers={"User-Agent": f"ATLAS-Updater/{self.current_version}"},
        )
        try:
            with urllib.request.urlopen(
                req, timeout=getattr(config, "UPDATE_HTTP_TIMEOUT_S", 15)
            ) as resp, open(tmp_path, "wb") as out:
                hasher = hashlib.sha256()
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    hasher.update(chunk)
            actual_size = os.path.getsize(tmp_path)
            if size and actual_size != size:
                logging.warning(
                    f"[UPDATE] Downloaded size mismatch ({actual_size} != {size}) "
                    f"— proceeding but flag for review"
                )
            expected = self._extract_sha256_hint(release)
            if expected:
                got = hasher.hexdigest()
                if got.lower() != expected.lower():
                    try: os.unlink(tmp_path)
                    except OSError: pass
                    return None, (
                        f"SHA-256 mismatch on downloaded installer.\n"
                        f"  expected: {expected}\n"
                        f"  got:      {got}\n"
                        f"Aborting update."
                    )
                logging.info(f"[UPDATE] Installer SHA-256 verified ({got})")
            else:
                logging.warning(
                    "[UPDATE] No SHA-256 published in release notes — proceeding "
                    "without hash verification. Add a `sha256:` line beside the "
                    "asset name in the release body to enable verification."
                )
            return tmp_path, None
        except (urllib.error.URLError, OSError) as e:
            try: os.unlink(tmp_path)
            except OSError: pass
            return None, f"Failed to download installer: {e}"

    def _apply_update_installed(self) -> Tuple[bool, str]:
        release = self._latest_release or self._fetch_latest_release()
        if release is None:
            return False, "Could not retrieve release information from GitHub."
        tag = release.get("tag_name") or release.get("name") or "(unknown)"
        if not _is_newer(tag, self.current_version):
            return False, f"Already up to date ({self.current_version})."

        installer_path, err = self._download_installer(release)
        if err:
            return False, err
        self._installer_path = installer_path

        notes_body = (release.get("body") or "").strip()
        confirmation_message = (
            "UPDATE AVAILABLE\n\n"
            f"Installed version: {self.current_version}\n"
            f"New version:       {tag}\n\n"
            f"{notes_body[:600]}\n\n"
            "Apply this update now? ATLAS will close, and the installer "
            "will start automatically once ATLAS has fully exited. The "
            "new version re-launches when the install completes."
        )

        if self.confirmation_callback:
            approved = self.confirmation_callback(confirmation_message)
        else:
            print(confirmation_message)
            approved = input("Apply update? (yes/no): ").strip().lower() in ("yes", "y")

        if not approved:
            try: os.unlink(installer_path)
            except OSError: pass
            return False, "Update cancelled by operator."

        # Operator complaint: previously the installer's NSIS UI could
        # pop up WHILE the ATLAS main window was still visible (the
        # post-confirmation "ATLAS will close" messagebox hadn't been
        # dismissed yet, and Popen returned immediately, so the user saw
        # two windows fighting). The fix: don't launch the installer
        # ourselves. Instead spawn a tiny PowerShell watcher that polls
        # for our PID to vanish and only then runs the installer. The
        # watcher is detached and hidden, so the user sees nothing until
        # ATLAS is fully gone — at which point the NSIS UI appears.
        watcher_launched = self._spawn_post_exit_installer_watcher(installer_path)
        if not watcher_launched:
            # Fallback: the watcher couldn't be spawned (PowerShell
            # missing / blocked by policy). Best effort — direct
            # detached launch like before. The race may briefly show the
            # NSIS window over ATLAS, but the update still happens.
            try:
                subprocess.Popen(
                    [installer_path],
                    cwd=os.path.dirname(installer_path),
                    close_fds=True,
                    creationflags=_DETACHED,
                )
            except OSError as e:
                return False, f"Failed to launch installer: {e}"

        return True, (
            f"Installer {os.path.basename(installer_path)} is queued — "
            f"it will start as soon as ATLAS exits."
        )

    @staticmethod
    def _spawn_post_exit_installer_watcher(installer_path: str) -> bool:
        """Spawn a hidden ``cmd.exe`` process that sleeps briefly, then
        launches *installer_path*.

        Returns ``True`` on successful spawn (no guarantee about the
        downstream installer launch -- that happens after we've already
        called ``os._exit``). Returns ``False`` if the spawn itself
        failed.

        Why ``cmd.exe`` and not PowerShell anymore: the previous
        implementation used ``powershell.exe -Command <inline script>``
        to wait for ATLAS's PID to vanish and then launch the
        installer. That stopped working in the field -- corporate
        Defender / EDR configurations silently kill ``powershell.exe
        -Command`` invocations (AMSI flags them), so ``Popen``
        returned success but the watcher process died milliseconds
        later, before writing even one diagnostic line. ``cmd.exe`` +
        ``start`` is the standard Windows fire-and-forget idiom and
        is almost never AV-flagged.

        Mechanism:
          1. ``echo`` a "watcher start" line to the diagnostic log so
             we can confirm in the field whether the watcher actually
             ran (the absence of this line proved AV was killing the
             old PowerShell variant).
          2. ``ping 127.0.0.1 -n 4 >nul`` -- ~3 second delay. ATLAS
             exits in milliseconds, so a fixed delay is plenty in
             place of the previous PID-poll loop.
          3. ``start "" "<installer>"`` -- detached GUI launch via
             ShellExecute. The empty ``""`` first arg is the window
             title (mandatory for ``start`` when the second arg is
             quoted).
          4. ``echo`` a "launch issued" line so the log reflects how
             far the watcher got.

        Diagnostics live at ``%APPDATA%\\ATLAS\\logs\\update_watcher
        .log`` so the operator (and Help → Open Logs Folder) can
        reach them without having to know ``%TEMP%``.
        """
        # Watcher log lives next to ATLAS's own run logs.
        try:
            app_data = os.environ.get("APPDATA") or os.path.expanduser("~")
            log_dir = os.path.join(app_data, "ATLAS", "logs")
            os.makedirs(log_dir, exist_ok=True)
            log_path = os.path.join(log_dir, "update_watcher.log")
        except OSError:
            # Last-resort: write next to the installer so we still
            # get SOMETHING if APPDATA is unwritable.
            log_path = installer_path + ".watcher.log"

        # Build the cmd.exe one-liner. Quoting rules:
        #   * Each ``cmd`` redirection (``>>``) needs its output path
        #     in quotes if it contains spaces.
        #   * ``start "" "..."`` -- the empty ``""`` is the window
        #     title (cmd's ``start`` builtin requires it when the
        #     program path is quoted).
        #   * ``&`` chains commands sequentially regardless of exit
        #     code (vs ``&&`` which short-circuits on failure).
        # We deliberately tolerate failures in each step (log write
        # to read-only profile, ping unavailable, etc.) -- the
        # installer launch itself is the only step that has to
        # succeed.
        cmd_line = (
            f'echo [%date% %time%] watcher start: installer="{installer_path}"'
            f' >> "{log_path}" 2>&1'
            f' & ping -n 4 127.0.0.1 >nul'
            f' & echo [%date% %time%] launching installer'
            f' >> "{log_path}" 2>&1'
            f' & start "" "{installer_path}"'
            f' & echo [%date% %time%] start issued'
            f' >> "{log_path}" 2>&1'
        )
        try:
            subprocess.Popen(
                ["cmd.exe", "/c", cmd_line],
                close_fds=True,
                creationflags=_DETACHED,
            )
            return True
        except (OSError, FileNotFoundError) as e:
            logging.warning(
                f"[UPDATE] Could not spawn cmd.exe installer watcher: {e}. "
                f"Falling back to direct launch (NSIS UI may briefly "
                f"overlap ATLAS)."
            )
            return False

    # ------------------------------------------------------------------
    # Dev-mode: git pull path (legacy behaviour, unchanged)
    # ------------------------------------------------------------------

    def _get_current_branch(self) -> Optional[str]:
        try:
            result = subprocess.run(
                ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                check=True,
                creationflags=_NO_WINDOW,
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError:
            return None

    def _validate_branch(self) -> Tuple[bool, str]:
        current_branch = self._get_current_branch()
        if not current_branch:
            return False, "Could not determine current branch"
        if current_branch not in self.ALLOWED_UPDATE_BRANCHES:
            return False, (
                f"Updates only allowed on branches: "
                f"{', '.join(self.ALLOWED_UPDATE_BRANCHES)}. "
                f"Current branch: {current_branch}"
            )
        return True, f"On allowed branch: {current_branch}"

    def _verify_commit_signatures(self, commit_range: str) -> Tuple[bool, List[str]]:
        try:
            result = subprocess.run(
                ['git', 'log', '--format=%H %G?', commit_range],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                check=False,
                creationflags=_NO_WINDOW,
            )
            if result.returncode != 0:
                err = result.stderr.strip() or "git log returned non-zero"
                logging.warning(f"[UPDATE] Could not list commits for signature check: {err}")
                return False, [f"Could not enumerate incoming commits: {err}"]

            stdout = result.stdout.strip()
            if not stdout:
                return True, []

            messages: List[str] = []
            all_good = True
            for line in stdout.split('\n'):
                line = line.strip()
                if not line:
                    continue
                parts = line.split()
                commit = parts[0][:8]
                status = parts[-1] if len(parts) > 1 else '?'
                if status in self._GPG_GOOD_STATUSES:
                    continue
                all_good = False
                if   status == 'N': messages.append(f"Commit {commit}: UNSIGNED")
                elif status == 'B': messages.append(f"Commit {commit}: BAD signature")
                elif status == 'X': messages.append(f"Commit {commit}: signature EXPIRED")
                elif status == 'Y': messages.append(f"Commit {commit}: signing key EXPIRED")
                elif status == 'R': messages.append(f"Commit {commit}: signing key REVOKED")
                elif status == 'E': messages.append(f"Commit {commit}: signature unverifiable (missing key)")
                else:               messages.append(f"Commit {commit}: unknown signature status '{status}'")
            return all_good, messages

        except FileNotFoundError:
            logging.error("[UPDATE] git executable not found")
            return False, ["git executable not found on PATH"]
        except Exception as e:
            logging.warning(f"[UPDATE] Signature verification failed: {e}")
            return False, [f"Signature verification error: {e}"]

    def _get_diff_summary(self) -> str:
        try:
            result = subprocess.run(
                ['git', 'diff', '--stat', 'HEAD...@{u}'],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                check=False,
                creationflags=_NO_WINDOW,
            )
            if result.stdout:
                return "Files to be changed:\n" + result.stdout[:500]
            return "No file changes detected"
        except Exception as e:
            logging.debug(f"Could not generate diff summary: {e}")
            return "Could not generate change summary"

    def _check_for_updates_git(self) -> bool:
        logging.info("Checking for updates (git mode)...")
        try:
            subprocess.run(
                ['git', 'fetch'],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                check=True,
                creationflags=_NO_WINDOW,
                timeout=_GIT_CHECK_TIMEOUT,
            )
            result = subprocess.run(
                ['git', 'status'],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                check=True,
                creationflags=_NO_WINDOW,
                timeout=_GIT_CHECK_TIMEOUT,
            )
            return (
                "Your branch is behind" in result.stdout
                or "can be fast-forwarded" in result.stdout
            )
        except subprocess.TimeoutExpired:
            logging.warning(
                "Update check exceeded %.0fs budget (slow/unreachable remote);"
                " skipping so startup isn't blocked.", _GIT_CHECK_TIMEOUT
            )
            return False
        except subprocess.CalledProcessError as e:
            logging.error(f"Git command failed: {e.stderr}")
            return False
        except Exception as e:
            logging.error(f"Error while checking for updates: {e}")
            return False

    def _apply_update_git(self) -> Tuple[bool, str]:
        logging.info("Preparing to apply updates (git mode)...")

        branch_valid, branch_msg = self._validate_branch()
        if not branch_valid:
            logging.error(f"Branch validation failed: {branch_msg}")
            return False, branch_msg

        is_signed, sig_messages = self._verify_commit_signatures('HEAD...@{u}')
        for msg in sig_messages:
            logging.warning(f"[UPDATE] Signature issue: {msg}")
        if not is_signed and self.enforce_signatures:
            joined = "\n  - ".join(sig_messages) if sig_messages else "(no details)"
            err = (
                "Update aborted: commit signature verification failed.\n"
                f"  - {joined}\n"
                "Resolve by importing the maintainer's GPG public key, or set "
                "LAMIS_ALLOW_UNSIGNED_UPDATES=1 to override (NOT recommended)."
            )
            logging.error(f"[UPDATE] {err}")
            return False, err

        diff_summary = self._get_diff_summary()
        if is_signed:
            sig_label = "All commits signed and verified"
        elif self.enforce_signatures:
            sig_label = "Signature verification failed"
        else:
            sig_label = "Signature enforcement DISABLED (operator override)"

        confirmation_message = (
            "UPDATE CONFIRMATION REQUIRED\n\n"
            f"Branch: {self._get_current_branch()}\n"
            f"Signature Status: {sig_label}\n\n"
            f"{diff_summary}\n\n"
        )
        if sig_messages:
            confirmation_message += "Signature notes:\n"
            for w in sig_messages:
                confirmation_message += f"  - {w}\n"
            confirmation_message += "\n"
        confirmation_message += "Do you want to apply these changes and restart ATLAS?"

        if self.confirmation_callback:
            approved = self.confirmation_callback(confirmation_message)
        else:
            print(confirmation_message)
            approved = input("Apply update? (yes/no): ").strip().lower() in ("yes", "y")

        if not approved:
            logging.info("Update cancelled by user")
            return False, "Update cancelled"

        logging.info("Applying updates...")
        try:
            result = subprocess.run(
                ['git', 'pull'],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                check=True,
                creationflags=_NO_WINDOW,
            )
            logging.info(f"Updates applied: {result.stdout}")
            return True, "Updates applied successfully. Restart ATLAS to complete."
        except subprocess.CalledProcessError as e:
            logging.error(f"Failed to apply updates: {e.stderr}")
            return False, f"Failed to apply updates: {e.stderr}"
        except Exception as e:
            logging.error(f"Error while applying updates: {e}")
            return False, f"Update error: {e}"

    # ------------------------------------------------------------------
    # Restart helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_restart_argv() -> list:
        """Return a sanitized argv for re-exec.

        F016 hardening: ``sys.argv`` may contain credentials or attacker-controlled
        flags (e.g. ``--password=...`` from a launcher). Re-executing with the
        original argv would echo those values into the new process and into any
        process listing. We keep only ``sys.argv[0]`` (the script path) and drop
        all user-supplied arguments. Callers needing arguments preserved must
        explicitly opt in via the ``LAMIS_RESTART_ARGS`` environment variable.
        """
        script = sys.argv[0] if sys.argv else ""
        extra = os.environ.get("LAMIS_RESTART_ARGS", "").strip()
        argv = [script] if script else []
        if extra:
            safe = []
            for tok in extra.split():
                if all(c.isalnum() or c in "-_./=" for c in tok) and len(tok) <= 64:
                    safe.append(tok)
            argv.extend(safe)
        return argv


if __name__ == "__main__":
    repo = None if _running_as_frozen() else os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))
    )
    updater = Updater(repo_path=repo)
    if updater.check_for_updates():
        success, message = updater.apply_update()
        print(message)
        if success and not updater.installed_mode:
            input("Press Enter to restart ATLAS...")
            updater.restart_program()
    else:
        logging.info("No updates available.")

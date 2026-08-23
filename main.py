import logging
import os
import sys


def _maybe_dispatch_to_tds() -> None:
    """If ``--tds-mode`` is on the command line, hand off to TDS_v6.2.py.

    ATLAS used to ship a sibling ``TDS.exe`` so the GUI could spawn the
    diagnostics tool as a subprocess. The same binary now handles both
    roles: when ATLAS sees ``--tds-mode`` at startup, it strips the flag
    and ``runpy``-executes the bundled TDS source instead of starting the
    GUI. This keeps the installer down to a single visible executable
    without forcing a refactor of the 17k-line TDS script.
    """
    if "--tds-mode" not in sys.argv:
        return
    # Strip the dispatch flag so TDS's argparse never sees it.
    sys.argv = [a for a in sys.argv if a != "--tds-mode"]
    # Locate the TDS source. Frozen builds extract data files under
    # _MEIPASS; dev builds use the source tree relative to this file.
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    tds_src = os.path.join(base, "scripts", "TDS", "TDS_v6.2.py")
    if not os.path.isfile(tds_src):
        # Fall back to the project-root path so dev runs work even when
        # cwd has wandered.
        proj_root = os.path.dirname(os.path.abspath(__file__))
        tds_src = os.path.join(proj_root, "scripts", "TDS", "TDS_v6.2.py")
    if not os.path.isfile(tds_src):
        sys.stderr.write(
            "[ATLAS] --tds-mode requested but scripts/TDS/TDS_v6.2.py was "
            "not found next to the executable.\n"
        )
        sys.exit(2)
    import runpy
    try:
        runpy.run_path(tds_src, run_name="__main__")
    except SystemExit:
        # TDS exits the process via sys.exit() in several error paths;
        # propagate the exit code through cleanly.
        raise
    sys.exit(0)


def _relaunch_as_admin() -> bool:
    """On Windows, relaunch ATLAS elevated (UAC) when not already Administrator.

    Some operations need an elevated token -- notably the Nokia PSI network
    audit's neighbor walk, which adds temporary Windows /32 routes. Relaunching
    at startup means the operator gets one UAC prompt up front instead of a
    mid-run "Run as Administrator" failure.

    Returns True if this process should keep running (already elevated,
    non-Windows, or the user declined UAC -> run unelevated with admin-gated
    features limited). Returns False if an elevated instance was launched and
    THIS (unelevated) instance should exit.
    """
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        if ctypes.windll.shell32.IsUserAnAdmin():
            return True
    except Exception:
        return True  # can't determine -> don't block startup
    try:
        import ctypes
        import subprocess
        frozen = getattr(sys, "frozen", False)
        exe = sys.executable
        if not frozen:
            # Relaunch through the windowless interpreter (pythonw.exe) so the
            # elevated instance doesn't pop a console window. Falls back to the
            # current interpreter if pythonw isn't alongside it. The frozen build
            # is already a windowed exe (console=False), so it needs no swap.
            _pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
            if os.path.isfile(_pyw):
                exe = _pyw
        args = sys.argv[1:] if frozen else [os.path.abspath(sys.argv[0])] + sys.argv[1:]
        params = subprocess.list2cmdline(args)
        workdir = os.path.dirname(sys.executable if frozen else os.path.abspath(sys.argv[0]))
        # ShellExecuteW "runas" triggers the UAC prompt; SW_SHOWNORMAL = 1.
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, workdir, 1)
        if int(rc) > 32:
            return False  # elevated instance launched -> exit this one
        # rc <= 32: user declined UAC or it failed -> carry on unelevated.
    except Exception:
        pass
    return True


# Dispatch BEFORE any heavy imports (tkinter, gui, paramiko, ...) so the
# TDS subprocess doesn't pay the GUI startup tax.
_maybe_dispatch_to_tds()

# Relaunch elevated (UAC) for the GUI path -- after the TDS dispatch (the
# --tds-mode child inherits the parent's token, so it never reaches here) and
# before the heavy imports (a relaunch exits cheaply). If the user declines
# UAC, ATLAS still starts unelevated; only admin-gated features are limited.
if not _relaunch_as_admin():
    sys.exit(0)


from tkinter import Tk, Label
from PIL import Image, ImageTk
from gui.gui4_0 import InventoryGUI
import script_interface
from utils.update import Updater
from script_interface import CommandTracker
import config
from utils.helpers import (
    get_database_path,
    set_host_key_prompt,
    default_tk_host_key_prompt,
    cleanup_stale_lamis_tempfiles,
    scrub_known_hosts,
    get_known_hosts_path,
)
from utils.logging_setup import configure_atlas_logging

# --- Host key cleanup on exit/crash ---
import atexit
import signal
import threading
import shutil

def _delete_known_hosts():
    try:
        kh_path = get_known_hosts_path()
        if kh_path.exists():
            kh_path.unlink()
            logging.info(f"[HOSTKEY] Deleted known_hosts at exit: {kh_path}")
    except Exception as e:
        logging.warning(f"[HOSTKEY] Could not delete known_hosts: {e}")

def _register_known_hosts_cleanup():
    # Register for normal exit
    atexit.register(_delete_known_hosts)
    # Register for signals (crash/interrupt)
    def _signal_handler(signum, frame):
        _delete_known_hosts()
        # Re-raise default handler
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGABRT):
        try:
            signal.signal(sig, _signal_handler)
        except Exception:
            pass

_register_known_hosts_cleanup()

# Configure logging before any work starts. The helper is idempotent and is
# shared with gui/gui4_0.py's development entry point.
_log_file = str(configure_atlas_logging())

# Suppress PIL debug logs
logging.getLogger("PIL").setLevel(config.PIL_LOG_LEVEL)

# Suppress paramiko internal debug logs (kex handshake, cipher negotiation, etc.)
logging.getLogger("paramiko").setLevel(logging.WARNING)

# Serial diagnostics: when config.SERIAL_DEBUG is True, raise the
# atlas.serial logger to DEBUG so utils.serial_helpers writes a
# per-chunk byte transcript. Independent of LOG_LEVEL so a field
# operator can turn it on without flooding the rest of the log.
if getattr(config, "SERIAL_DEBUG", False):
    try:
        from utils.serial_helpers import enable_serial_debug
        enable_serial_debug(True)
        logging.info("[SERIAL] DEBUG transcript enabled via config.SERIAL_DEBUG")
    except Exception as _serial_dbg_err:
        logging.debug(f"Could not enable serial debug: {_serial_dbg_err}")

# F023: sweep stale temp files left behind by prior crashed runs (best-effort).
try:
    cleanup_stale_lamis_tempfiles(max_age_hours=24)
except Exception as _cleanup_exc:  # pragma: no cover - defensive
    logging.debug("Startup tempfile cleanup skipped: %s", _cleanup_exc)

# Drop any malformed entries from known_hosts at startup. A single corrupt
# line (truncated base64 etc.) makes paramiko's HostKeys.load raise,
# which in turn breaks every subsequent save_host_keys call — and a
# save-failure during a successful SSH session bubbles up as a spurious
# identification error. Idempotent on clean files.
try:
    scrub_known_hosts()
except Exception as _scrub_exc:  # pragma: no cover - defensive
    logging.debug("Startup known_hosts scrub skipped: %s", _scrub_exc)


class LoadingScreen:
    def __init__(self, root):
        self.root = root
        self.root.title("Loading ATLAS")
        self.root.geometry("800x600")
        self.root.overrideredirect(True)  # Remove window decorations

        # Center the window
        x = (self.root.winfo_screenwidth() / 2) - 400
        y = (self.root.winfo_screenheight() / 2) - 300
        self.root.geometry(f'+{int(x)}+{int(y)}')

        try:
            logo_path = os.path.join(
                getattr(sys, '_MEIPASS', os.path.dirname(__file__)),
                "ATLAS Logo.png"
            )
            logo = Image.open(logo_path).resize((800, 600), Image.LANCZOS)
            self.logo = ImageTk.PhotoImage(logo)
            self.logo_label = Label(self.root, image=self.logo)
            self.logo_label.pack(pady=20)
        except Exception as e:
            logging.error(f"Failed to load logo: {e}")
            Label(self.root, text="Automated Toolkit for Lightriver Asset & Systems (ATLAS)", font=("Arial", 16)).pack(pady=20)

        self.status_label = Label(self.root, text="Loading...", font=("Arial", 12))
        self.status_label.pack(pady=10)
        self.root.update_idletasks()

    def update_status(self, message):
        self.status_label.config(text=message)
        self.root.update_idletasks()

    def close(self):
        self.root.quit()
        self.root.destroy()


def show_loading_screen():
    loading_root = Tk()
    return LoadingScreen(loading_root), loading_root


def check_updates(loading_screen):
    """Probe for updates and surface availability on the loading screen.

    In dev mode the Updater needs a path to the working tree; in
    installed (frozen) mode it auto-detects and queries the GitHub
    Releases feed instead — no repo path needed."""
    try:
        if getattr(sys, "frozen", False):
            updater = Updater()
        else:
            updater = Updater(os.path.dirname(__file__))
        update_available = updater.check_for_updates()
    except Exception:
        update_available = False
    loading_screen.update_status("Update Available" if update_available else "No Updates")
    loading_screen.close()
    start_gui(update_available)


def start_gui(update_available):
    root = Tk()
    set_host_key_prompt(default_tk_host_key_prompt)
    command_tracker = script_interface.get_tracker()
    # Use the process-wide singleton cache so the path-fix applied in
    # InventoryGUI.__init__ is visible to scripts launched via
    # script_interface.select_script() (which calls get_cache()).
    db_cache = script_interface.get_cache()

    InventoryGUI(root, update_available, command_tracker, db_cache)
    root.mainloop()


def main():
    logging.info("Starting Automated Toolkit for Lightriver Asset & Systems (ATLAS)")
    logging.info(f"Log file: {_log_file}")
    loading_screen, loading_root = show_loading_screen()
    loading_root.after(100, lambda: check_updates(loading_screen))
    loading_root.mainloop()


if __name__ == "__main__":
    main()

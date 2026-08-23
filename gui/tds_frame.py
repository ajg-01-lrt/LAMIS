"""TDS diagnostics frame — widgets and logic for the TDS mode."""
import ipaddress
import os
import re
import sys
import logging
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, messagebox

import config
from utils.helpers import ensure_host_key_known, friendly_error
from utils.credentials import get_default_credential_for_vendor


def _resolve_ciena_default() -> tuple:
    """Pull the Ciena 6500 / RLS default credentials from the same Fernet
    source every other ATLAS component uses (``credentials_config.json``).

    Returns ``(None, None)`` when the encrypted store is unreachable or
    defaults have been disabled via ``LAMIS_DISABLE_DEFAULT_CREDS`` so the
    caller falls straight through to the operator prompt instead of
    attempting an unauthenticated login.
    """
    pair = get_default_credential_for_vendor("ciena")
    if pair:
        return pair
    return (None, None)

# Markers in the subprocess output that mean the device rejected the login.
# Keep narrow so a benign "Permission" word in some other context doesn't
# trigger a needless re-prompt.
_AUTH_FAILURE_RE = re.compile(
    r"(?i)\b("
    r"authentication failed|"
    r"permission denied|"
    r"login (?:failed|incorrect)|"
    r"invalid (?:credentials|username|password|login)|"
    r"access denied|"
    r"bad (?:username|password)"
    r")\b"
)


class TDSFrame(ttk.Frame):
    """Tkinter frame for the TDS (Diagnostics) mode.

    Owns all TDS configuration widgets and the ``run_tds`` worker.  Uses
    *controller* only to access ``controller.root`` (for ``root.after``) and
    ``controller.output_screen`` (to append log lines).
    """

    def __init__(self, parent: ttk.Frame, controller) -> None:
        super().__init__(parent)
        self.controller = controller
        self._build()

    # ------------------------------------------------------------------
    # Frame construction
    # ------------------------------------------------------------------

    def _build(self) -> None:
        config_frame = ttk.LabelFrame(self, text="TDS Diagnostics")
        config_frame.pack(fill=tk.X, pady=5)

        # Row 0 — target device
        tk.Label(config_frame, text="IP Address:").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        self.tds_ip_entry = tk.Entry(config_frame, width=25)
        self.tds_ip_entry.grid(row=0, column=1, padx=5, pady=5)

        tk.Label(config_frame, text="Platform:").grid(row=0, column=2, sticky="e", padx=5, pady=5)
        self.tds_platform_var = tk.StringVar(value="rls")
        self.tds_platform_combo = ttk.Combobox(
            config_frame, textvariable=self.tds_platform_var,
            values=["rls", "6500"], width=10, state="readonly",
        )
        self.tds_platform_combo.grid(row=0, column=3, padx=5, pady=5)

        # Row 1 — output filename
        tk.Label(config_frame, text="File Name:").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        self.tds_filename_entry = tk.Entry(config_frame, width=25)
        self.tds_filename_entry.grid(row=1, column=1, padx=5, pady=5)

        # Row 2 — credential hint (so operators know what login is being tried)
        # Pulls the username out of the same Fernet store every other ATLAS
        # component reads from; only the username is shown, never the secret.
        u, _ = _resolve_ciena_default()
        if u:
            hint_text = f"Login: {u} (Ciena default — prompts on failure)"
        else:
            hint_text = "Login: prompt on connect (defaults disabled or unavailable)"
        hint = tk.Label(
            config_frame, text=hint_text,
            anchor="w", foreground="#555555",
        )
        hint.grid(row=2, column=0, columnspan=4, sticky="w", padx=5, pady=(2, 5))

        # Run controls
        tds_control_frame = ttk.Frame(self)
        tds_control_frame.pack(fill=tk.X, pady=10)

        self.tds_run_button = tk.Button(tds_control_frame, text="Run Diagnostics", command=self.run_tds)
        self.tds_run_button.pack(side=tk.RIGHT, padx=5)

        self.tds_status_label = tk.Label(tds_control_frame, text="Status: Ready", anchor="w")
        self.tds_status_label.pack(side=tk.RIGHT, padx=10)

    # ------------------------------------------------------------------
    # Credential helpers
    # ------------------------------------------------------------------

    def _prompt_user_for_credentials(self):
        """Show a credential dialog on the main thread and return (user, pw)
        or ``None`` if cancelled. Caller is responsible for being on the
        Tk main thread (the worker should marshal via ``root.after``)."""
        try:
            from utils.credentials import prompt_for_credentials_gui
            return prompt_for_credentials_gui(parent_window=self.controller.root)
        except Exception as exc:
            logging.exception("TDS credential prompt failed")
            messagebox.showerror(
                "Credential Prompt Error",
                f"Could not display the credential prompt:\n{friendly_error(exc)}",
            )
            return None

    def _looks_like_auth_failure(self, output: str) -> bool:
        return bool(output) and bool(_AUTH_FAILURE_RE.search(output))

    # ------------------------------------------------------------------
    # TDS worker
    # ------------------------------------------------------------------

    def run_tds(self) -> None:
        """Validate inputs and launch the TDS script in a background thread.

        Uses the Ciena factory default from the encrypted credential store
        (looked up by vendor key, never hardcoded) for the first attempt
        and re-prompts the operator on the main thread if the device rejects
        it. Retries the subprocess once with the supplied credentials before
        giving up.
        """
        ip = self.tds_ip_entry.get().strip()
        platform = (self.tds_platform_var.get() or "rls").strip().lower()
        file_name = self.tds_filename_entry.get().strip()

        if not ip:
            messagebox.showerror("Input Error", "Please enter a device IP address.")
            return
        # Validate: must be a valid IP or a safe hostname (alphanumeric, dots, dashes only)
        _ip_valid = False
        try:
            ipaddress.ip_address(ip)
            _ip_valid = True
        except ValueError:
            _ip_valid = bool(re.match(r'^[A-Za-z0-9][A-Za-z0-9.\-]*$', ip))
        if not _ip_valid:
            messagebox.showerror("Input Error", "Invalid IP address or hostname format.")
            return
        if platform not in ("6500", "rls"):
            messagebox.showerror("Input Error", "Platform must be either 6500 or rls.")
            return
        if not file_name:
            messagebox.showerror("Input Error", "Please enter a file name.")
            return

        # F009: pre-verify the device's SSH host key in the GUI thread (where
        # the Tk prompt can run) before launching the TDS subprocess. The
        # subprocess uses RejectPolicy and will refuse if the key isn't in
        # known_hosts, so this step is what makes that strict mode usable.
        if not ensure_host_key_known(ip):
            messagebox.showerror(
                "Host Key Verification Failed",
                f"Could not verify the SSH host key for {ip}. "
                "TDS will not be launched.",
            )
            return

        # TDS now runs through the same binary as ATLAS — frozen builds
        # self-spawn ATLAS.exe with --tds-mode, dev runs use the Python
        # interpreter against TDS_v6.2.py directly.
        if getattr(sys, "frozen", False):
            tds_command_prefix = [sys.executable, "--tds-mode"]
            tds_cwd = os.path.dirname(sys.executable)
        else:
            tds_script_path = os.path.normpath(
                os.path.join(os.path.dirname(__file__), "..", "scripts", "TDS", "TDS_v6.2.py")
            )
            if not os.path.isfile(tds_script_path):
                messagebox.showerror("TDS Error", f"TDS script not found:\n{tds_script_path}")
                return
            tds_command_prefix = [sys.executable, tds_script_path]
            tds_cwd = os.path.dirname(tds_script_path)

        self.tds_status_label.config(text="Status: Running...")
        self.tds_run_button.config(state=tk.DISABLED)
        out = self.controller.output_screen
        activity = getattr(self.controller, "log_activity", logging.info)
        activity(
            f"[TDS] Starting diagnostics at {ip} (platform={platform})."
        )

        root = self.controller.root

        # Source the default credentials from the encrypted store on every
        # run so a one-time edit to credentials_config.json takes effect
        # without restarting ATLAS.
        default_user, default_pass = _resolve_ciena_default()

        # Background worker — runs the subprocess once with defaults; if the
        # subprocess fails with an auth-style error, marshals back to the
        # main thread to prompt the user and retries once.
        def _invoke(username: str, password: str):
            command = tds_command_prefix + [
                "--non-interactive",
                "--host", ip,
                "--platform", platform,
                "--username", username,
                "--file-name", file_name,
                "--read-password-stdin",
            ]
            if platform == "rls":
                command.extend(["--validate", "--walk-mode"])

            return subprocess.run(
                command,
                input=password,
                capture_output=True,
                text=True,
                cwd=tds_cwd,
                timeout=config.TDS_TIMEOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

        def _combined(result) -> str:
            buf = result.stdout or ""
            if result.stderr:
                buf += ("\n" if buf else "") + result.stderr
            return buf

        def _ask_user_creds_blocking() -> "tuple|None":
            """Marshal a credential prompt to the main thread and wait for
            the answer. Returns (user, pw) or None if cancelled."""
            result_holder: dict = {'value': None}
            ready = threading.Event()

            def _on_main():
                try:
                    out.insert(
                        tk.END,
                        f"[{ip}] Default credentials failed — please enter credentials.\n",
                    )
                    out.see(tk.END)
                    result_holder['value'] = self._prompt_user_for_credentials()
                finally:
                    ready.set()

            root.after(0, _on_main)
            ready.wait()
            return result_holder['value']

        def _worker() -> None:
            try:
                # First attempt: Ciena default credentials from the Fernet store.
                # If the store is unreachable / defaults are disabled, jump
                # straight to the operator prompt.
                if default_user and default_pass:
                    result = _invoke(default_user, default_pass)
                    combined = _combined(result)
                else:
                    result = None
                    combined = ""

                # If we never tried (no default available) OR the device
                # rejected the login, re-prompt and retry once.
                need_prompt = (
                    result is None
                    or (result.returncode != 0
                        and self._looks_like_auth_failure(combined))
                )
                if need_prompt:
                    logging.info(
                        f"[TDS] Default credentials rejected for {ip}; "
                        f"prompting user for credentials"
                    )
                    answer = _ask_user_creds_blocking()
                    if answer and answer[0] and answer[1]:
                        user2, pass2 = answer
                        result = _invoke(user2, pass2)
                        combined = _combined(result)
                    elif answer is None:
                        # User cancelled — fall through with the original result
                        # so the existing error path reports cleanly.
                        logging.info(f"[TDS] Operator cancelled credential prompt for {ip}")

                def on_complete() -> None:
                    self.tds_run_button.config(state=tk.NORMAL)
                    self.tds_status_label.config(text="Status: Ready")
                    if combined.strip():
                        out.insert(tk.END, combined + "\n")
                        out.see(tk.END)
                    if result is None:
                        logging.warning(
                            "[TDS] Cancelled for %s; no credentials were provided.",
                            ip,
                        )
                        # No default available AND operator cancelled the prompt —
                        # nothing was attempted.
                        messagebox.showwarning(
                            "TDS Cancelled",
                            "No credentials were provided; TDS did not run.",
                        )
                    elif result.returncode == 0:
                        logging.info("[TDS] Diagnostics completed for %s.", ip)
                        messagebox.showinfo("TDS Complete", "TDS diagnostics completed successfully.")
                    else:
                        logging.error(
                            "[TDS] Diagnostics failed for %s with exit code %s.",
                            ip,
                            result.returncode,
                        )
                        messagebox.showerror(
                            "TDS Error",
                            f"TDS script exited with code {result.returncode}.",
                        )

                root.after(0, on_complete)

            except subprocess.TimeoutExpired:
                def on_timeout() -> None:
                    self.tds_run_button.config(state=tk.NORMAL)
                    self.tds_status_label.config(text="Status: Timeout")
                    messagebox.showerror("TDS Timeout", "TDS diagnostics timed out.")
                root.after(0, on_timeout)

            except Exception as exc:
                def on_error() -> None:
                    self.tds_run_button.config(state=tk.NORMAL)
                    self.tds_status_label.config(text="Status: Error")
                    messagebox.showerror(
                        "TDS Error",
                        f"Failed to run TDS script:\n{friendly_error(exc)}",
                    )
                root.after(0, on_error)

        threading.Thread(target=_worker, daemon=True).start()

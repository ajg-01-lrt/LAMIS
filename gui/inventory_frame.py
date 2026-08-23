"""Inventory scanning frame — widgets and UI logic for the Inventory mode."""
from __future__ import annotations

import ipaddress
import os
import logging
import re
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    from serial.tools import list_ports
except Exception:  # pragma: no cover - safe fallback when pyserial extras are unavailable
    list_ports = None

import config
from utils.helpers import (
    UploadValidationError,
    friendly_error,
    scrub_password_widget,
    validate_uploaded_file,
)


class InventoryFrame(ttk.Frame):
    """Tkinter frame for the Inventory (network scan) mode.

    Owns all pod/IP selection widgets, run/pause/abort controls, progress bar,
    and append-mode report selection.  Calls back into *controller* (an
    ``InventoryGUI`` instance) for cross-cutting operations such as
    ``run_script``, ``pause_program``, and ``abort_program``.
    """

    def __init__(self, parent: ttk.Frame, controller) -> None:
        super().__init__(parent)
        self.controller = controller
        self.inventory_report_path: str | None = None
        # Customer/Project/PO/SO auto-extracted from the uploaded report file.
        # Used to pre-fill the run-context popup so the user doesn't have to
        # retype values that already exist in the file they just selected.
        self.uploaded_metadata: dict[str, str] = {}
        self._build()

    def _get_available_serial_ports(self) -> list[str]:
        """Return sorted COM port device names available on the current system."""
        if list_ports is None:
            return []

        try:
            ports = [port.device for port in list_ports.comports()]
            return sorted(ports)
        except Exception as exc:
            logging.debug(f"Unable to enumerate serial ports: {exc}")
            return []

    def _refresh_serial_ports(self) -> None:
        """Refresh Serial Port dropdown options while preserving user selection when possible."""
        current = self.serial_port_var.get().strip()
        ports = self._get_available_serial_ports()
        if not ports:
            ports = ["COM1"]

        self.serial_port_combobox["values"] = ports
        if current in ports:
            self.serial_port_var.set(current)
        else:
            self.serial_port_var.set(ports[0])

    # ------------------------------------------------------------------
    # Frame construction
    # ------------------------------------------------------------------

    def _build(self) -> None:
        # --- Connection type row ---
        connection_frame = ttk.LabelFrame(self, text="Connection Type")
        connection_frame.pack(fill=tk.X, pady=5)
        self.connection_type = tk.StringVar(value="Network")
        tk.Radiobutton(
            connection_frame, text="Network",
            variable=self.connection_type, value="Network",
            command=self._on_connection_type_change,
        ).pack(side=tk.LEFT, padx=10)
        tk.Radiobutton(
            connection_frame, text="LAN",
            variable=self.connection_type, value="LAN",
            command=self._on_connection_type_change,
        ).pack(side=tk.LEFT, padx=10)
        tk.Radiobutton(
            connection_frame, text="Serial",
            variable=self.connection_type, value="Serial",
            command=self._on_connection_type_change,
        ).pack(side=tk.LEFT, padx=10)

        self.lan_script_options = ["Nokia PSS", "Nokia PSI", "Ciena 6500", "Ciena RLS"]
        self.serial_script_options = ["Nokia SAR", "Nokia IXR", "Nokia PSI", "Ciena RLS", "Ciena Waveserver 5"]

        self.manual_connection_frame = ttk.LabelFrame(self, text="Direct Connection")

        script_row = ttk.Frame(self.manual_connection_frame)
        script_row.pack(fill=tk.X, padx=10, pady=5)
        tk.Label(script_row, text="Script:").pack(side=tk.LEFT, padx=(0, 5))
        self.manual_script_var = tk.StringVar(value=self.lan_script_options[0])
        self.manual_script_combobox = ttk.Combobox(
            script_row,
            textvariable=self.manual_script_var,
            state="readonly",
            values=self.lan_script_options,
            width=24,
        )
        self.manual_script_combobox.pack(side=tk.LEFT, padx=5)

        self.lan_details_frame = ttk.Frame(self.manual_connection_frame)
        self.lan_details_frame.pack(fill=tk.X, padx=10, pady=5)
        tk.Label(self.lan_details_frame, text="IP:").pack(side=tk.LEFT, padx=(0, 5))
        _octet_vcmd = (self.register(self._validate_octet), "%P")
        self._lan_octet_entries: list[tk.Entry] = []
        for i in range(4):
            octet_entry = tk.Entry(
                self.lan_details_frame, width=4, justify="center",
                validate="key", validatecommand=_octet_vcmd,
            )
            octet_entry.pack(side=tk.LEFT, padx=(2, 0))
            self._lan_octet_entries.append(octet_entry)
            # Bind Tab/dot to jump to next octet
            if i < 3:
                next_idx = i + 1
                octet_entry.bind("<period>", lambda e, ni=next_idx: self._focus_octet(ni))
                octet_entry.bind("<Tab>", lambda e, ni=next_idx: self._focus_octet(ni))
            if i > 0:
                prev_idx = i - 1
                octet_entry.bind("<BackSpace>", lambda e, pi=prev_idx, oe=octet_entry: self._octet_backspace(e, pi, oe))
            if i < 3:
                tk.Label(self.lan_details_frame, text=".").pack(side=tk.LEFT)

        self.serial_details_frame = ttk.Frame(self.manual_connection_frame)
        self.serial_details_frame.pack(fill=tk.X, padx=10, pady=5)
        tk.Label(self.serial_details_frame, text="Serial Port:").pack(side=tk.LEFT, padx=(0, 5))
        self.serial_port_var = tk.StringVar(value="COM1")
        self.serial_port_combobox = ttk.Combobox(
            self.serial_details_frame,
            textvariable=self.serial_port_var,
            width=12,
        )
        self.serial_port_combobox.pack(side=tk.LEFT, padx=5)
        tk.Button(
            self.serial_details_frame,
            text="Refresh",
            command=self._refresh_serial_ports,
        ).pack(side=tk.LEFT, padx=(0, 5))
        tk.Label(self.serial_details_frame, text="Baud Rate:").pack(side=tk.LEFT, padx=(10, 5))
        self.serial_baud_var = tk.StringVar(value="9600")
        self.serial_baud_combobox = ttk.Combobox(
            self.serial_details_frame,
            textvariable=self.serial_baud_var,
            state="readonly",
            values=["9600", "19200", "38400", "57600", "115200"],
            width=10,
        )
        self.serial_baud_combobox.pack(side=tk.LEFT, padx=5)

        # --- Optional existing report (append mode) ---
        self.report_frame = ttk.LabelFrame(self, text="Device Report (Optional)")
        self.report_frame.pack(fill=tk.X, pady=5)
        self.inventory_file_label = tk.Label(
            self.report_frame,
            text="No report selected (new workbook will be created)",
            foreground="gray",
        )
        self.inventory_file_label.pack(side=tk.LEFT, padx=10, pady=5)
        tk.Button(self.report_frame, text="Browse", command=self.upload_inventory_report).pack(side=tk.LEFT, padx=5)
        tk.Button(self.report_frame, text="Clear", command=self.clear_inventory_report_upload).pack(side=tk.LEFT, padx=5)

        # --- Pod / IP selection (two columns) ---
        self.middle_frame = ttk.Frame(self)
        self.middle_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=20)

        left_middle_frame = ttk.Frame(self.middle_frame)
        left_middle_frame.pack(side=tk.LEFT, expand=True, padx=10, pady=20)

        right_middle_frame = ttk.Frame(self.middle_frame)
        right_middle_frame.pack(side=tk.RIGHT, expand=True, padx=10, pady=20)

        # Pods 1..13 map to 172.21.1xx.x; "Lab" switches to 10.9.x.x and asks the
        # operator for the third octet as well.
        pod_options = [f"Pod {n}" for n in range(1, config.POD_COUNT + 1)]
        pod_options.append(config.LAB_LABEL)

        # Pod 1 (left column)
        pod_frame_1 = ttk.LabelFrame(left_middle_frame, text="Pod Selection 1")
        pod_frame_1.pack(fill=tk.X, padx=10, pady=5)
        tk.Label(pod_frame_1, text="Pod:").pack(side=tk.LEFT, padx=5)
        self.pod_var_1 = tk.StringVar()
        self.pod_combobox_1 = ttk.Combobox(
            pod_frame_1, textvariable=self.pod_var_1, values=pod_options, state="readonly", width=10
        )
        self.pod_combobox_1.pack(side=tk.LEFT, padx=5)
        self.pod_combobox_1.set("Pod 1")

        ip_frame_1 = ttk.LabelFrame(left_middle_frame, text="IP Selection 1")
        ip_frame_1.pack(fill=tk.X, padx=10, pady=5)
        ip_container_1 = ttk.Frame(ip_frame_1)
        ip_container_1.pack()
        self.start_ip_label_1 = tk.Label(ip_container_1, text="")
        self.start_ip_label_1.pack(side=tk.LEFT)
        # Lab-only third octet: a pod fixes the /24, the lab does not.
        self.start_oct3_var_1 = tk.StringVar()
        self.start_oct3_entry_1 = tk.Entry(
            ip_container_1, width=4, justify="center", textvariable=self.start_oct3_var_1
        )
        self.start_dot_label_1 = tk.Label(ip_container_1, text=".")
        self.start_ip_entry_1 = tk.Entry(ip_container_1, width=3, justify="center")
        self.start_ip_entry_1.pack(side=tk.LEFT)
        self.end_ip_label_1 = tk.Label(ip_container_1, text="")
        self.end_ip_label_1.pack(side=tk.LEFT)
        self.end_oct3_var_1 = tk.StringVar()
        self.end_oct3_entry_1 = tk.Entry(
            ip_container_1, width=4, justify="center", textvariable=self.end_oct3_var_1
        )
        self.end_dot_label_1 = tk.Label(ip_container_1, text=".")
        self.end_ip_entry_1 = tk.Entry(ip_container_1, width=3, justify="center")
        self.end_ip_entry_1.pack(side=tk.LEFT)
        self.pod_var_1.trace_add("write", lambda *_: self._refresh_selector(1))

        # Pod 2 (right column)
        pod_frame_2 = ttk.LabelFrame(right_middle_frame, text="Pod Selection 2")
        pod_frame_2.pack(fill=tk.X, padx=10, pady=5)
        tk.Label(pod_frame_2, text="Pod:").pack(side=tk.LEFT, padx=5)
        self.pod_var_2 = tk.StringVar()
        self.pod_combobox_2 = ttk.Combobox(
            pod_frame_2, textvariable=self.pod_var_2, values=pod_options, state="readonly", width=10
        )
        self.pod_combobox_2.pack(side=tk.LEFT, padx=5)
        self.pod_combobox_2.set("Pod 1")

        ip_frame_2 = ttk.LabelFrame(right_middle_frame, text="IP Selection 2")
        ip_frame_2.pack(fill=tk.X, padx=10, pady=5)
        ip_container_2 = ttk.Frame(ip_frame_2)
        ip_container_2.pack()
        self.start_ip_label_2 = tk.Label(ip_container_2, text="")
        self.start_ip_label_2.pack(side=tk.LEFT)
        self.start_oct3_var_2 = tk.StringVar()
        self.start_oct3_entry_2 = tk.Entry(
            ip_container_2, width=4, justify="center", textvariable=self.start_oct3_var_2
        )
        self.start_dot_label_2 = tk.Label(ip_container_2, text=".")
        self.start_ip_entry_2 = tk.Entry(ip_container_2, width=3, justify="center")
        self.start_ip_entry_2.pack(side=tk.LEFT)
        self.end_ip_label_2 = tk.Label(ip_container_2, text="")
        self.end_ip_label_2.pack(side=tk.LEFT)
        self.end_oct3_var_2 = tk.StringVar()
        self.end_oct3_entry_2 = tk.Entry(
            ip_container_2, width=4, justify="center", textvariable=self.end_oct3_var_2
        )
        self.end_dot_label_2 = tk.Label(ip_container_2, text=".")
        self.end_ip_entry_2 = tk.Entry(ip_container_2, width=3, justify="center")
        self.end_ip_entry_2.pack(side=tk.LEFT)
        self.pod_var_2.trace_add("write", lambda *_: self._refresh_selector(2))

        # Set the initial label text and Lab-field visibility for both selectors.
        self._refresh_selector(1)
        self._refresh_selector(2)

        # --- Action buttons + status ---
        self.control_frame = ttk.Frame(self)
        self.control_frame.pack(fill=tk.X, pady=10)

        self.run_button = tk.Button(self.control_frame, text="Run", command=self.controller.run_script)
        self.run_button.pack(side=tk.RIGHT, padx=5)
        self.pause_button = tk.Button(
            self.control_frame, text="Pause",
            command=self.controller.pause_program, state=tk.DISABLED,
        )
        self.pause_button.pack(side=tk.RIGHT, padx=5)
        self.abort_button = tk.Button(
            self.control_frame, text="Abort",
            command=self.controller.abort_program, state=tk.DISABLED,
        )
        self.abort_button.pack(side=tk.RIGHT, padx=5)

        self.status_label = tk.Label(self.control_frame, text="Status: Ready", anchor="w")
        self.status_label.pack(side=tk.RIGHT, padx=10)

        # --- Progress bar ---
        self.progress_frame = ttk.Frame(self)
        self.progress_frame.pack(fill=tk.X, padx=10, pady=(0, 4))
        self.progress_bar = ttk.Progressbar(
            self.progress_frame, orient="horizontal", mode="determinate", maximum=100
        )
        self.progress_bar.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        self.progress_label = tk.Label(self.progress_frame, text="", width=28, anchor="w")
        self.progress_label.pack(side=tk.LEFT)

        # Spacer that expands to push controls to the bottom
        self.spacer_frame = ttk.Frame(self)

        self._refresh_serial_ports()
        self._on_connection_type_change()

    # ------------------------------------------------------------------
    # Public API used by InventoryGUI
    # ------------------------------------------------------------------

    def update_status(self, message: str) -> None:
        """Update the status label and flush pending UI events."""
        self.status_label.config(text=message)
        self.controller.root.update_idletasks()

    def set_run_controls(self, running: bool) -> None:
        """Enable or disable run/pause/abort buttons based on *running* state."""
        self.run_button.config(state=tk.DISABLED if running else tk.NORMAL)
        pa_state = tk.NORMAL if running else tk.DISABLED
        self.pause_button.config(state=pa_state)
        self.abort_button.config(state=pa_state)

    def update_progress(self, current: int, total: int, label: str) -> None:
        """Advance the progress bar to *current*/*total* and set the label text."""
        if total > 0:
            self.progress_bar["value"] = (current / total) * 100
        self.progress_label.config(text=label)

    def reset_progress(self) -> None:
        """Reset progress bar to zero and clear the label."""
        self.progress_bar["value"] = 0
        self.progress_label.config(text="")

    def upload_inventory_report(self) -> None:
        """Prompt for an existing report file to enable append mode."""
        file_path = filedialog.askopenfilename(
            title="Select Existing Device Report",
            filetypes=[("Excel files", "*.xlsx *.xls"), ("All files", "*.*")],
        )
        if not file_path:
            return

        try:
            resolved = validate_uploaded_file(file_path, allowed_kinds=("xlsx", "xls"))
        except UploadValidationError as e:
            messagebox.showerror("Invalid Report File", str(e))
            logging.warning(f"Inventory report upload rejected: {e}")
            return

        file_path = str(resolved)
        self.inventory_report_path = file_path
        file_name = os.path.basename(file_path)

        # Auto-extract Customer/Project/PO/SO so the run-context popup can
        # pre-fill them. Same logic as the packing slip "Upload File" path.
        try:
            from utils.workbook_metadata import extract_workbook_metadata
            self.uploaded_metadata = extract_workbook_metadata(file_path)
        except Exception as e:
            logging.debug(f"Inventory report metadata extraction failed: {e}")
            self.uploaded_metadata = {}

        meta_summary = ", ".join(
            f"{k.title()}={v}" for k, v in self.uploaded_metadata.items() if v
        )
        self.inventory_file_label.config(
            text=f"Using existing report: {file_name}", foreground="green"
        )
        activity = getattr(self.controller, "log_activity", logging.info)
        activity(f"[INVENTORY] Append report selected: {file_path}")
        if meta_summary:
            activity(f"[INVENTORY] Metadata pre-filled: {meta_summary}")

    def clear_inventory_report_upload(self) -> None:
        """Clear the append-mode report selection."""
        self.inventory_report_path = None
        self.uploaded_metadata = {}
        self.inventory_file_label.config(
            text="No report selected (new workbook will be created)", foreground="gray"
        )
        activity = getattr(self.controller, "log_activity", logging.info)
        activity(
            "[INVENTORY] Append mode disabled; a new report will be created."
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_octet(value: str) -> bool:
        """Allow only up to 3 digits (0-9) per octet field."""
        return value == "" or (value.isdigit() and len(value) <= 3)

    def _focus_octet(self, index: int) -> str:
        """Move focus to the octet entry at *index* and return 'break' to suppress default binding."""
        self._lan_octet_entries[index].focus_set()
        return "break"

    def _octet_backspace(self, event: tk.Event, prev_index: int, current_entry: tk.Entry) -> None:
        """Jump to the previous octet only when the current box is already empty."""
        if current_entry.get() == "" and current_entry.index(tk.INSERT) == 0:
            self._lan_octet_entries[prev_index].focus_set()
            return "break"

    @property
    def lan_ip(self) -> str:
        """Return the LAN IP address assembled from the four octet entries."""
        return ".".join(e.get() for e in self._lan_octet_entries)

    @staticmethod
    def _subnet_prefix_for(pod_choice: str, lab_octet: str) -> str:
        """Return the first three octets for a pod/Lab selection.

        Pods 1..13 resolve to 172.21.<POD_THIRD_OCTET_BASE + N>. "Lab" resolves to
        10.9.<lab_octet>, and yields the bare "10.9" prefix until the operator
        supplies a valid third octet so the label can still be drawn.
        """
        choice = (pod_choice or "").strip()

        if choice == config.LAB_LABEL:
            octet = (lab_octet or "").strip()
            if octet.isdigit() and 0 <= int(octet) <= 255:
                return f"{config.LAB_NETWORK_PREFIX}.{int(octet)}"
            return config.LAB_NETWORK_PREFIX

        match = re.fullmatch(r"Pod\s*(\d+)", choice)
        if match:
            pod_number = int(match.group(1))
            if 1 <= pod_number <= config.POD_COUNT:
                return f"{config.POD_NETWORK_PREFIX}.{config.POD_THIRD_OCTET_BASE + pod_number}"

        # Tolerate a raw third octet typed directly (older saved input).
        if choice.isdigit() and config.POD_MIN <= int(choice) <= config.POD_MAX:
            return f"{config.POD_NETWORK_PREFIX}.{int(choice)}"

        return config.POD_NETWORK_PREFIX

    def _selector_widgets(self, which: int) -> dict:
        """Widget/variable bundle for selector 1 or 2, so logic is written once."""
        if which == 1:
            return {
                "pod": self.pod_var_1,
                "start_label": self.start_ip_label_1,
                "end_label": self.end_ip_label_1,
                "start_oct3": self.start_oct3_var_1,
                "end_oct3": self.end_oct3_var_1,
                "start_oct3_entry": self.start_oct3_entry_1,
                "end_oct3_entry": self.end_oct3_entry_1,
                "start_dot": self.start_dot_label_1,
                "end_dot": self.end_dot_label_1,
                "start_entry": self.start_ip_entry_1,
                "end_entry": self.end_ip_entry_1,
            }
        return {
            "pod": self.pod_var_2,
            "start_label": self.start_ip_label_2,
            "end_label": self.end_ip_label_2,
            "start_oct3": self.start_oct3_var_2,
            "end_oct3": self.end_oct3_var_2,
            "start_oct3_entry": self.start_oct3_entry_2,
            "end_oct3_entry": self.end_oct3_entry_2,
            "start_dot": self.start_dot_label_2,
            "end_dot": self.end_dot_label_2,
            "start_entry": self.start_ip_entry_2,
            "end_entry": self.end_ip_entry_2,
        }

    def is_lab_selection(self, which: int) -> bool:
        """True when selector *which* is set to the Lab range rather than a pod."""
        return self._selector_widgets(which)["pod"].get().strip() == config.LAB_LABEL

    def _refresh_selector(self, which: int) -> None:
        """Update a selector's IP label prefixes and show/hide its Lab third octet.

        The third-octet entry and its dot are packed with ``before=`` the fourth-octet
        entry so the row keeps the order "label oct3 . oct4" no matter how many times
        the operator toggles between a pod and the Lab.
        """
        w = self._selector_widgets(which)
        is_lab = self.is_lab_selection(which)

        for oct3_entry, dot, fourth in (
            (w["start_oct3_entry"], w["start_dot"], w["start_entry"]),
            (w["end_oct3_entry"], w["end_dot"], w["end_entry"]),
        ):
            if is_lab:
                dot.pack(side=tk.LEFT, before=fourth)
                oct3_entry.pack(side=tk.LEFT, before=dot)
            else:
                oct3_entry.pack_forget()
                dot.pack_forget()

        prefix = config.LAB_NETWORK_PREFIX if is_lab else self._subnet_prefix_for(w["pod"].get(), "")
        w["start_label"].config(text=f"Start IP: {prefix}.")
        w["end_label"].config(text=f"End IP: {prefix}.")

    @staticmethod
    def _octet(value: str) -> int | None:
        """Parse a single dotted-quad octet, or None if it is not 0-255."""
        text = (value or "").strip()
        if not text.isdigit():
            return None
        number = int(text)
        return number if 0 <= number <= 255 else None

    def resolve_range(self, which: int) -> tuple[str | None, str | None, str | None]:
        """Resolve selector *which* to (start_ip, end_ip, error).

        Returns ``(None, None, None)`` when the selector is blank, so selector 2 stays
        optional. A pod contributes a fixed /24 and only the fourth octet is read; the
        Lab reads a third octet for each end, which means a Lab range may legitimately
        span more than one /24.
        """
        w = self._selector_widgets(which)
        is_lab = self.is_lab_selection(which)
        label = f"IP Selection {which}"

        start_host = w["start_entry"].get().strip()
        end_host = w["end_entry"].get().strip()
        start_oct3 = w["start_oct3"].get().strip() if is_lab else ""
        end_oct3 = w["end_oct3"].get().strip() if is_lab else ""

        if not any((start_host, end_host, start_oct3, end_oct3)):
            return None, None, None

        if not start_host or not end_host:
            return None, None, f"{label}: enter both a start and an end address."

        host_start, host_end = self._octet(start_host), self._octet(end_host)
        if host_start is None or host_end is None:
            return None, None, f"{label}: the last octet must be a number from 0 to 255."

        if is_lab:
            third_start, third_end = self._octet(start_oct3), self._octet(end_oct3)
            if third_start is None or third_end is None:
                return None, None, (
                    f"{label}: enter the third octet for both the start and end "
                    f"{config.LAB_NETWORK_PREFIX}.x.x addresses (0-255)."
                )
            start_ip = f"{config.LAB_NETWORK_PREFIX}.{third_start}.{host_start}"
            end_ip = f"{config.LAB_NETWORK_PREFIX}.{third_end}.{host_end}"
        else:
            prefix = self._subnet_prefix_for(w["pod"].get(), "")
            if prefix.count(".") != 2:
                return None, None, f"Pod Selection {which}: choose a pod or the Lab range."
            start_ip = f"{prefix}.{host_start}"
            end_ip = f"{prefix}.{host_end}"

        if int(ipaddress.IPv4Address(start_ip)) > int(ipaddress.IPv4Address(end_ip)):
            return None, None, f"{label}: Start IP must be less than or equal to End IP."

        return start_ip, end_ip, None

    def _on_connection_type_change(self) -> None:
        mode = self.connection_type.get()

        # Hide all mode-dependent sections and bottom controls first.
        self.report_frame.pack_forget()
        self.middle_frame.pack_forget()
        self.manual_connection_frame.pack_forget()
        self.spacer_frame.pack_forget()
        self.control_frame.pack_forget()
        self.progress_frame.pack_forget()

        if mode == "Network":
            self.report_frame.pack(fill=tk.X, pady=5)
            self.middle_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=20)
        else:
            self.manual_connection_frame.pack(fill=tk.X, pady=5)

            if mode == "Serial":
                self.manual_script_combobox["values"] = self.serial_script_options
                if self.manual_script_var.get() not in self.serial_script_options:
                    self.manual_script_var.set(self.serial_script_options[0])
                self.lan_details_frame.pack_forget()
                self.serial_details_frame.pack(fill=tk.X, padx=10, pady=5)
            else:  # LAN
                self.manual_script_combobox["values"] = self.lan_script_options
                if self.manual_script_var.get() not in self.lan_script_options:
                    self.manual_script_var.set(self.lan_script_options[0])
                self.serial_details_frame.pack_forget()
                self.lan_details_frame.pack(fill=tk.X, padx=10, pady=5)

            # Show file upload for LAN and Serial too
            self.report_frame.pack(fill=tk.X, pady=5)

        # Spacer pushes controls to the very bottom
        self.spacer_frame.pack(fill=tk.BOTH, expand=True)
        self.control_frame.pack(fill=tk.X, pady=10)
        self.progress_frame.pack(fill=tk.X, padx=10, pady=(0, 4))

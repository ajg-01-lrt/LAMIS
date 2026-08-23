"""Source-level guards on the user-visible progress breadcrumbs.

The operator complaint that prompted these: during a LAN scan, the
output panel stayed blank for long stretches. Queue breadcrumbs now
flow through ``InventoryGUI.log_activity`` so the same event reaches
both the rotating file and the queue-backed GUI logging handler.

These tests pin the breadcrumbs at each phase boundary so a future
refactor can't silently drop them and reintroduce the "is it
working?" gap.
"""
from __future__ import annotations
import inspect
import unittest

from gui.gui4_0 import InventoryGUI


class TestLanInventoryHasPhaseAnnouncements(unittest.TestCase):
    """``run_inventory_worker`` (LAN multi-IP) must narrate each
    phase so the operator can see scan progress on the output panel,
    not just the progress bar."""

    def setUp(self):
        self.src = inspect.getsource(InventoryGUI.run_inventory_worker)

    def test_announces_lan_inventory_start(self):
        self.assertIn("Starting LAN inventory", self.src)

    def test_announces_phase_1_ping(self):
        # Phase 1 banner — operators see the scan move from "Pinging"
        # to "Scanning" to "Building" so they know how far along we
        # are at a glance.
        self.assertIn("Phase 1/3", self.src)
        self.assertIn("Pinging", self.src)

    def test_announces_ping_summary(self):
        # After ping completes, summarize before moving to the scan
        # phase — "X reachable, Y unreachable" is the key signal.
        self.assertIn("Ping complete", self.src)
        self.assertIn("reachable", self.src)
        self.assertIn("unreachable", self.src)

    def test_announces_phase_2_scan(self):
        self.assertIn("Phase 2/3", self.src)
        self.assertIn("Scanning", self.src)

    def test_announces_per_device_completion(self):
        # Each scan-pool future completes with a "[ip] Scan complete
        # (count/total)" line — drives the output panel at the same
        # rate the progress bar ticks.
        self.assertIn("Scan complete", self.src)

    def test_announces_phase_3_build(self):
        # Building the workbook can take multiple seconds for large
        # device counts. Announce before kicking it off.
        self.assertIn("Phase 3/3", self.src)
        self.assertIn("Building report", self.src)


class TestDirectConnectionHasStartAnnouncement(unittest.TestCase):
    """Single-device LAN / Serial (Direct Connection) paths must say
    "Starting <mode> inventory for <target_id>…" so the panel isn't
    blank while the script connects."""

    def test_announces_direct_connect_start(self):
        src = inspect.getsource(InventoryGUI.run_inventory_worker)
        self.assertIn("Starting", src)
        # Cover both LAN and Serial variants of the Direct
        # Connection panel — the worker uses ``connection_mode`` in
        # the message so the operator sees which path is running.
        self.assertIn("connection_mode", src)


class TestPerDeviceIdentifyAndExecuteBreadcrumbs(unittest.TestCase):
    """``_process_single_device`` walks every reachable device through
    Identify → Select → Execute. Without per-phase log lines on the
    queue, the operator sees nothing in the panel until the device
    finishes (or fails) seconds later."""

    def setUp(self):
        self.src = inspect.getsource(InventoryGUI._process_single_device)

    def test_announces_identify_start(self):
        self.assertIn("Identifying device type", self.src)

    def test_announces_identify_result(self):
        # Operators want to confirm the device family before commands
        # fire. Surfacing the identified type catches mis-detected
        # devices early.
        self.assertIn("Identified as", self.src)

    def test_announces_execute_start(self):
        # Phase C — actual command execution. The command count is
        # the most useful signal ("running 6 command(s)…") because
        # the per-script logs go to the file, not the panel.
        self.assertIn("Running", self.src)
        self.assertIn("command(s)", self.src)


class TestDirectConnectionExecuteBreadcrumb(unittest.TestCase):
    """The Direct-Connection path goes through ``process_task_queue``
    rather than ``_process_single_device``. Same idea — surface
    "Connecting and running N command(s)" so the panel isn't blank."""

    def test_process_task_queue_announces_connect(self):
        src = inspect.getsource(InventoryGUI.process_task_queue)
        self.assertIn("Connecting and running", src)


class TestExportWorkerAnnouncesBuild(unittest.TestCase):
    """The Excel build can take multiple seconds on large device
    counts. Without an announcement, the operator stares at a panel
    that suddenly stops scrolling after the scan finishes."""

    def test_export_worker_announces_build(self):
        src = inspect.getsource(InventoryGUI.run_export_worker)
        self.assertIn("Building inventory workbook", src)


if __name__ == "__main__":
    unittest.main()

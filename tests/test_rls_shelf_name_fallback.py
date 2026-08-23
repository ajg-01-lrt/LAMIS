"""Regression tests for the Ciena RLS shelf-name fallback chain.

Field report: an RLS R4 chassis with no TID in its CLI prompt and no
``ui-name`` set produced a workbook tab named ``Shelf 1`` -- and every
other un-named RLS on the queue collapsed onto the same tab, so all
but the last device's data was lost. Operators expected the script to
fall back to the *device type + serial* form (``6500-R4_NNTMRT...``)
when nothing else identifies the chassis, so each one keeps its own
tab even before the operator sets a hostname.

The fix extends the priority chain in ``Script.extract_shelf_detail``:

  1. ``hostname`` (TID extracted from CLI prompt)        -- best case
  2. ``node-name`` field from ``show shelf`` output      -- NEW
  3. ``ui-name`` (operator-set shelf alias)
  4. ``<shelf-type>_<serial>``                           -- NEW
  5. ``Shelf <n>``
  6. ``Unknown``

Tier 2 catches RLS firmware builds that print a ``node-name`` line
even when the prompt is bare (early boot, partial provisioning).
Tier 4 is the field's "we have hardware identity but no name"
fallback -- the operator can match the workbook tab against the
physical chassis serial sticker.
"""
from __future__ import annotations
import unittest

from scripts.Ciena_RLS import Script


def _make_script() -> Script:
    """Build a Script instance without the DB / network plumbing -- we
    only need the parser methods (``extract_shelf_detail``,
    ``_resolve_tid``, ``_strip_rls_noise``) which don't touch the DB."""
    s = Script.__new__(Script)
    s.system_tid = ""  # so _resolve_tid sees a clean slate
    return s


def _shelf_block(**fields) -> str:
    """Build a ``show shelf`` block populated with the given fields."""
    lines = ["shelf:"]
    for k, v in fields.items():
        if v is None:
            continue
        lines.append(f"  {k}    : {v}")
    return "\n".join(lines) + "\n"


class TestRLSShelfNameFallbackChain(unittest.TestCase):

    def test_uses_hostname_from_prompt_when_present(self):
        # The normal case: device's CLI prompt carries a FQDN.
        s = _make_script()
        output = (
            "usuqo4-l8o1.bb.net.apple.com# show shelf\n"
            + _shelf_block(
                name="1",
                **{"shelf-type": "6500-R4", "serial-number": "NNTMRT1PJFW7",
                   "pec": "NTK803FA"},
            )
            + "usuqo4-l8o1.bb.net.apple.com#"
        )
        df = s.extract_shelf_detail(output, ip="10.0.0.1")
        self.assertEqual(
            df.iloc[0]["System Name"], "usuqo4-l8o1.bb.net.apple.com",
        )

    def test_falls_back_to_node_name_field_when_prompt_has_no_tid(self):
        # Some RLS firmware emits ``node-name`` inside the shelf block
        # even when the CLI prompt is just ``#``. Honor it before the
        # operator-set ui-name (which is often blank).
        s = _make_script()
        output = (
            "# show shelf\n"
            + _shelf_block(
                name="1",
                **{"shelf-type": "6500-R4", "serial-number": "NNTMRT1PJFW7",
                   "node-name": "usuqo4-l8o1.bb.net.apple.com"},
            )
            + "#"
        )
        df = s.extract_shelf_detail(output, ip="10.0.0.1")
        self.assertEqual(
            df.iloc[0]["System Name"], "usuqo4-l8o1.bb.net.apple.com",
        )

    def test_falls_back_to_ui_name_when_no_tid_and_no_node_name(self):
        s = _make_script()
        output = (
            "# show shelf\n"
            + _shelf_block(
                name="1",
                **{"shelf-type": "6500-R4", "serial-number": "NNTMRT1PJFW7",
                   "ui-name": "Tile-A-1"},
            )
            + "#"
        )
        df = s.extract_shelf_detail(output, ip="10.0.0.1")
        self.assertEqual(df.iloc[0]["System Name"], "Tile-A-1")

    def test_falls_back_to_device_type_and_serial_when_no_name_set(self):
        # This is the bug Wen-Lan hit: no hostname, no node-name, no
        # ui-name. Old code returned ``Shelf 1`` -- the new code uses
        # the concrete ``<shelf-type>_<serial>`` form so the operator
        # can still match the workbook tab to the physical chassis.
        s = _make_script()
        output = (
            "# show shelf\n"
            + _shelf_block(
                name="1",
                **{"shelf-type": "6500-R4", "serial-number": "NNTMRT1PJFW7"},
            )
            + "#"
        )
        df = s.extract_shelf_detail(output, ip="10.0.0.1")
        self.assertEqual(
            df.iloc[0]["System Name"], "6500-R4_NNTMRT1PJFW7",
        )

    def test_ui_name_that_is_just_device_model_falls_through(self):
        # Field report (the operator's literal capture):
        #   shelf:
        #     name        : 0
        #     ui-name     : 6500-R4
        #     shelf-type  : 6500-R4 8-Slot
        #     c-type      : 6500-R4 8-Slot Shelf Assembly
        #     serial-number : NNTMRT1RD88T
        # ``ui-name = 6500-R4`` is a default echo of the device model
        # (it's a substring of both shelf-type and c-type), NOT an
        # operator alias. Must fall through to ``6500-R4_<serial>``
        # so each chassis still gets its own workbook tab.
        s = _make_script()
        output = (
            "# show shelf\n"
            + _shelf_block(
                name="0",
                **{
                    "ui-name": "6500-R4",
                    "shelf-type": "6500-R4 8-Slot",
                    "c-type": "6500-R4 8-Slot Shelf Assembly",
                    "serial-number": "NNTMRT1RD88T",
                },
            )
            + "#"
        )
        df = s.extract_shelf_detail(output, ip="10.0.0.1")
        self.assertEqual(
            df.iloc[0]["System Name"], "6500-R4_NNTMRT1RD88T",
        )

    def test_shelf_number_zero_is_treated_as_no_shelf_id(self):
        # ``name: 0`` is not a useful identifier (every un-provisioned
        # shelf would collapse onto ``Shelf 0``). Should fall through
        # to ``Unknown`` when we have nothing else.
        s = _make_script()
        output = "# show shelf\n" + _shelf_block(name="0") + "#"
        df = s.extract_shelf_detail(output, ip="10.0.0.1")
        # No model/serial either -- last-resort ``Unknown``.
        self.assertEqual(df.iloc[0]["System Name"], "Unknown")

    def test_extracts_hostname_from_trailing_prompt_with_no_whitespace(self):
        # Regex regression: ``_extract_hostname`` used to require
        # whitespace AFTER the ``#``, so a trailing prompt at end of
        # buffer (``hostname#`` with no trailing newline) failed to
        # match. With the leading echo line ``hostname# show shelf``
        # stripped by the serial capture, this meant TID = empty and
        # the workbook tab was named ``Shelf 0`` instead of the host.
        s = _make_script()
        # Output with ONLY the trailing prompt -- no echoed command
        # line at the top.
        output = (
            _shelf_block(
                name="0",
                **{"shelf-type": "6500-R4", "serial-number": "X"},
            )
            + "uselp1-l8r2.bb.net.apple.com#"
        )
        df = s.extract_shelf_detail(output, ip="10.0.0.1")
        self.assertEqual(
            df.iloc[0]["System Name"], "uselp1-l8r2.bb.net.apple.com",
        )

    def test_falls_back_to_shelf_number_when_no_type_and_no_serial(self):
        # Truly anonymous device but we still have a shelf index --
        # this is rare (the device always knows its shelf type) but
        # the legacy code path is retained for completeness.
        s = _make_script()
        output = "# show shelf\n" + _shelf_block(name="1") + "#"
        df = s.extract_shelf_detail(output, ip="10.0.0.1")
        self.assertEqual(df.iloc[0]["System Name"], "Shelf 1")

    def test_falls_back_to_unknown_only_when_we_have_literally_nothing(self):
        s = _make_script()
        output = "# show shelf\n" + _shelf_block() + "#"
        df = s.extract_shelf_detail(output, ip="10.0.0.1")
        self.assertEqual(df.iloc[0]["System Name"], "Unknown")

    def test_node_name_does_not_override_hostname_from_prompt(self):
        # If both are present, prompt-TID wins -- it's the most
        # authoritative (matches what the operator sees on screen).
        s = _make_script()
        output = (
            "usuqo4-l8o1.bb.net.apple.com# show shelf\n"
            + _shelf_block(
                name="1",
                **{"shelf-type": "6500-R4", "serial-number": "X",
                   "node-name": "old-stale-name.example.com"},
            )
            + "usuqo4-l8o1.bb.net.apple.com#"
        )
        df = s.extract_shelf_detail(output, ip="10.0.0.1")
        self.assertEqual(
            df.iloc[0]["System Name"], "usuqo4-l8o1.bb.net.apple.com",
        )


if __name__ == "__main__":
    unittest.main()

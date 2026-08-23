"""Regression tests for Nokia 7705 SAR ``show port detail`` parsing.

Bug fixed here: when a populated SFP port follows one or more empty ports
on the same MDA, the ``show port detail | match ...`` filter keeps an
``Interface`` line for EVERY port (populated or not) but only emits
Serial/Model/Part/Optical Compliance lines for ports with an SFP. The
parser used to look back from each ``Optical Compliance`` line and take
the FIRST ``Interface`` it found in the 5-line window — which is the
PREVIOUS (empty) port's interface. The SFP then got reported against
the wrong port number (e.g. ``1/1/4`` instead of ``1/1/5`` on a 7705
SAR-8 a6-eth-10G card where xcme ports 1-4 are empty and xgige ports
5-6 carry the LR optics).

The fix iterates the look-back window backward so the CLOSEST
``Interface`` line above the ``Optical Compliance`` wins — that's
always the current port.
"""
import textwrap
import unittest
from unittest.mock import MagicMock

from scripts.Nokia_SAR import Script as SARScript


def _make_script():
    s = SARScript.__new__(SARScript)
    s.db_cache = MagicMock()
    s.db_cache.lookup_part.return_value = "SFP+  10GE LR - LC ROHS6/6 0/70C"
    return s


# Real-world filtered output from CHEM002_7705 (the screenshot the user
# reported). Ports 1/1/1-1/1/4 are empty xcme ports — they only emit an
# Interface line. Ports 1/1/5, 1/1/6, 1/2/5, 1/2/6 carry the LR optics
# and emit the full Serial / Model / Part / Optical Compliance block.
CHEM002_7705_SHOW_PORT_DETAIL = textwrap.dedent("""
    A:CHEM002_7705# show port detail | match "(Optical Compliance.+)|(Serial.+)|(Model.+)|(Part.+)|(Interface +: [0-9/]+)" expression
    Interface          : 1/1/1
    Interface          : 1/1/2
    Interface          : 1/1/3
    Interface          : 1/1/4
    Interface          : 1/1/5
    Serial Number      : NK2535Q2537
    Model Number       : 3HE04823AAAA01  NOK  IPU3ANKEAA
    Part Number        : 3HE04823AAAA01
    Optical Compliance : 10GBASE-LR
    Interface          : 1/1/6
    Serial Number      : NK2535Q2551
    Model Number       : 3HE04823AAAA01  NOK  IPU3ANKEAA
    Part Number        : 3HE04823AAAA01
    Optical Compliance : 10GBASE-LR
    Interface          : 1/2/1
    Interface          : 1/2/2
    Interface          : 1/2/3
    Interface          : 1/2/4
    Interface          : 1/2/5
    Serial Number      : NK2531Q0286
    Model Number       : 3HE04823AAAA01  NOK  IPU3ANKEAA
    Part Number        : 3HE04823AAAA01
    Optical Compliance : 10GBASE-LR
    Interface          : 1/2/6
    Serial Number      : NK2531Q0280
    Model Number       : 3HE04823AAAA01  NOK  IPU3ANKEAA
    Part Number        : 3HE04823AAAA01
    Optical Compliance : 10GBASE-LR
    A:CHEM002_7705#
""").strip()


class TestPortDetailLookbackPicksClosestInterface(unittest.TestCase):
    """Reproduce the CHEM002_7705 misattribution: 1/1/5 was reported as
    1/1/4 because empty ports 1-4 each emitted an Interface line, and
    the parser picked the EARLIEST Interface in the 5-line look-back
    window instead of the closest one."""

    def _parse(self, output):
        s = _make_script()
        captured = {}
        s.extract_port_detail(
            output,
            cache_callback=lambda df, key: captured.setdefault(key, df),
            ip="10.9.102.26",
        )
        return captured.get("port_data")

    def test_sfp_after_empty_ports_keeps_correct_port_number(self):
        df = self._parse(CHEM002_7705_SHOW_PORT_DETAIL)
        self.assertIsNotNone(df)
        ports = sorted(df["Name"].tolist())
        # The 4 LR optics live on ports 5 and 6 of each MDA, NOT on
        # port 4 of each MDA (which was the bug's incorrect output).
        self.assertEqual(ports, ["1/1/5", "1/1/6", "1/2/5", "1/2/6"])
        self.assertNotIn("1/1/4", ports)
        self.assertNotIn("1/2/4", ports)

    def test_optic_descriptions_track_their_correct_port(self):
        """Beyond just the port number, the per-port serial must
        attribute to the right interface. Cross-checking the serial
        ensures we didn't accidentally re-assign the SAME row's data
        to a different cell."""
        df = self._parse(CHEM002_7705_SHOW_PORT_DETAIL)
        row_by_port = {row["Name"]: row for _, row in df.iterrows()}
        self.assertEqual(row_by_port["1/1/5"]["Serial Number"], "NK2535Q2537")
        self.assertEqual(row_by_port["1/1/6"]["Serial Number"], "NK2535Q2551")
        self.assertEqual(row_by_port["1/2/5"]["Serial Number"], "NK2531Q0286")
        self.assertEqual(row_by_port["1/2/6"]["Serial Number"], "NK2531Q0280")

    def test_part_number_canonicalized_to_first_token(self):
        """Sanity: Model Number captures only the first whitespace token
        (the SKU), not the vendor / option suffixes."""
        df = self._parse(CHEM002_7705_SHOW_PORT_DETAIL)
        # 10-char canonical (parser truncates to [:10]) of 3HE04823AAAA01.
        self.assertEqual(set(df["Part Number"].tolist()), {"3HE04823AA"})


class TestPortDetailNoFalsePositiveOnDenselyPackedOutput(unittest.TestCase):
    """When EVERY port carries an SFP (no empty ports interleaved),
    the look-back window has exactly one Interface per port and the
    new backward-iterating logic must still pick it. This protects
    against an over-fitting fix that only handles the empty-ports
    case."""

    DENSE_OUTPUT = textwrap.dedent("""
        A:Densely_Populated# show port detail
        Interface          : 1/1/1
        Serial Number      : AAA111
        Model Number       : 3HE04823AAAA01
        Part Number        : 3HE04823AAAA01
        Optical Compliance : 10GBASE-LR
        Interface          : 1/1/2
        Serial Number      : BBB222
        Model Number       : 3HE04823AAAA01
        Part Number        : 3HE04823AAAA01
        Optical Compliance : 10GBASE-LR
    """).strip()

    def test_dense_population_still_reports_each_port_once(self):
        s = _make_script()
        captured = {}
        s.extract_port_detail(
            self.DENSE_OUTPUT,
            cache_callback=lambda df, key: captured.setdefault(key, df),
            ip="10.9.0.1",
        )
        df = captured.get("port_data")
        ports = sorted(df["Name"].tolist())
        self.assertEqual(ports, ["1/1/1", "1/1/2"])
        row_by_port = {row["Name"]: row for _, row in df.iterrows()}
        self.assertEqual(row_by_port["1/1/1"]["Serial Number"], "AAA111")
        self.assertEqual(row_by_port["1/1/2"]["Serial Number"], "BBB222")


if __name__ == "__main__":
    unittest.main()

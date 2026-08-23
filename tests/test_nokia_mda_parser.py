"""Regression tests for Nokia 7705 SAR-8 ``show mda detail`` parsing.

Bug fixed here: the parser used to extract the slot number by
searching the per-MDA block BODY for a line matching ``MDA   : N``
(``re.search(r'MDA\\s*:\\s*(\\d+)', block)``). Most cards' specific-data
sections include that exact line, but the 32-port T1/E1 ASAP card
(``a32-chds1v2``) formats its detail section differently and the
expected line is either absent or written as ``Mda   : N`` (lowercase).
The case-sensitive regex returned ``None`` and the slot's ``if not
mda_m: continue`` skipped the entire entry — so OSTD002_7705 (and
similar SAR-8 v2 chassis with an a32-chds1v2 in slot 5) lost MDA 5
from the inventory report.

The fix pulls the slot number from the BLOCK HEADER (``MDA 1/5
detail``) instead, which is always present and always parses the same
way regardless of card type.
"""
import textwrap
import unittest
from unittest.mock import MagicMock

from scripts.Nokia_SAR import Script as SARScript


def _make_script():
    s = SARScript.__new__(SARScript)
    s.db_cache = MagicMock()
    s.db_cache.lookup_part.return_value = "<test description>"
    return s


# Six-MDA SAR-8 v2 chassis with the a32-chds1v2 at slot 5.
# Slots 1-4 and 6 use a body that contains the "MDA  : N" line the
# previous parser depended on. Slot 5's body OMITS that line (the
# real-world bug condition for the 32-port T1/E1 ASAP card) — under
# the old parser this slot was silently dropped. The fix reads the
# slot number from the "MDA 1/5 detail" header instead.
SAR8_SHOW_MDA_DETAIL_WITH_A32_AT_SLOT_5 = textwrap.dedent("""
    A:OSTD002_7705# show mda detail
    ===============================================================================
    MDA 1/1 detail
    ===============================================================================
    Slot  Mda   Provisioned Type        Admin     Operational
                Equipped Type (if different)      State     State
    -------------------------------------------------------------------------------
    1     1     a6-eth-10G                        up        up

      Hardware Data
        MDA                            : 1
        Provisioned Type               : a6-eth-10G
        Equipped Type                  : a6-eth-10G
        Part number                    : 3HE07943AABA01
        Serial number                  : NS2543S0695
        Operational state              : up
    ===============================================================================
    MDA 1/2 detail
    ===============================================================================
    1     2     a6-eth-10G                        up        up

      Hardware Data
        MDA                            : 2
        Provisioned Type               : a6-eth-10G
        Equipped Type                  : a6-eth-10G
        Part number                    : 3HE07943AABA01
        Serial number                  : NS2543S0746
        Operational state              : up
    ===============================================================================
    MDA 1/3 detail
    ===============================================================================
    1     3     a6-em                             up        up

      Hardware Data
        MDA                            : 3
        Provisioned Type               : a6-em
        Equipped Type                  : a6-em
        Part number                    : 3HE03126AABB01
        Serial number                  : NS2546S1224
        Operational state              : up
    ===============================================================================
    MDA 1/4 detail
    ===============================================================================
    1     4     a8-c3794                          up        up

      Hardware Data
        MDA                            : 4
        Provisioned Type               : a8-c3794
        Equipped Type                  : a8-c3794
        Part number                    : 3HE12504AABA01
        Serial number                  : NS2551S0878
        Operational state              : up
    ===============================================================================
    MDA 1/5 detail
    ===============================================================================
    1     5     a32-chds1v2                       up        up

      Hardware Data
        Provisioned Type               : a32-chds1v2
        Equipped Type                  : a32-chds1v2
        Part number                    : 3HE02781AABA01
        Serial number                  : NS2523S1647
        Operational state              : up
    ===============================================================================
    MDA 1/6 detail
    ===============================================================================
    1     6     a12-sdiv3                         up        up

      Hardware Data
        MDA                            : 6
        Provisioned Type               : a12-sdiv3
        Equipped Type                  : a12-sdiv3
        Part number                    : 3HE03391ACBA01
        Serial number                  : NS2548S0989
        Operational state              : up
    A:OSTD002_7705#
""").strip()


class TestMdaDetailSlotFromHeader(unittest.TestCase):
    """Pull the slot number from the ``MDA N/M detail`` header so the
    parser doesn't depend on the body containing a particular
    case-sensitive ``MDA : N`` line."""

    def _parse(self, output):
        s = _make_script()
        return s.extract_mda_details(output, cache_callback=None, ip="10.9.102.21")

    def test_a32_chds1v2_at_slot_5_is_no_longer_dropped(self):
        """The exact OSTD002_7705 reproducer: slot 5 used to disappear
        because its block body didn't carry a matchable ``MDA : 5``
        line. Header-based extraction picks it up."""
        df = self._parse(SAR8_SHOW_MDA_DETAIL_WITH_A32_AT_SLOT_5)
        self.assertIsNotNone(df)
        slot_nums = sorted(df["Name"].tolist(), key=int)
        self.assertEqual(slot_nums, ["1", "2", "3", "4", "5", "6"])

    def test_slot_5_carries_correct_part_and_serial(self):
        df = self._parse(SAR8_SHOW_MDA_DETAIL_WITH_A32_AT_SLOT_5)
        row_by_slot = {row["Name"]: row for _, row in df.iterrows()}
        self.assertEqual(row_by_slot["5"]["Type"], "a32-chds1v2")
        # Part number is truncated to 10 chars.
        self.assertEqual(row_by_slot["5"]["Part Number"], "3HE02781AA")
        self.assertEqual(row_by_slot["5"]["Serial Number"], "NS2523S1647")

    def test_slot_6_after_dropped_slot_still_carries_correct_data(self):
        """The slot following a previously-dropped slot must not pick
        up the wrong slot number from the header."""
        df = self._parse(SAR8_SHOW_MDA_DETAIL_WITH_A32_AT_SLOT_5)
        row_by_slot = {row["Name"]: row for _, row in df.iterrows()}
        self.assertEqual(row_by_slot["6"]["Type"], "a12-sdiv3")
        self.assertEqual(row_by_slot["6"]["Part Number"], "3HE03391AC")
        self.assertEqual(row_by_slot["6"]["Serial Number"], "NS2548S0989")

    def test_empty_slot_still_skipped(self):
        """Header-based extraction must not introduce empty slots.
        An ``(empty)`` block is still dropped."""
        empty_slot_output = textwrap.dedent("""
            A:H# show mda detail
            ===============================================================================
            MDA 1/1 detail
            ===============================================================================
            1     1     a6-eth-10G                        up        up
              Hardware Data
                Provisioned Type               : a6-eth-10G
                Part number                    : 3HE07943AABA01
                Serial number                  : NS2543S0695
            ===============================================================================
            MDA 1/2 detail
            ===============================================================================
            1     2     (empty)                            -         -
              Hardware Data
                Provisioned Type               : (empty)
                Part number                    : ZZZ_should_not_appear
                Serial number                  : ZZZ_should_not_appear
            ===============================================================================
            MDA 1/3 detail
            ===============================================================================
            1     3     a8-c3794                          up        up
              Hardware Data
                Provisioned Type               : a8-c3794
                Part number                    : 3HE12504AABA01
                Serial number                  : NS2551S0878
            A:H#
        """).strip()
        df = self._parse(empty_slot_output)
        slots = sorted(df["Name"].tolist(), key=int)
        self.assertEqual(slots, ["1", "3"])
        pns = set(df["Part Number"].tolist())
        self.assertNotIn("ZZZ_should", pns)  # 10-char truncation of ZZZ_should_not_appear

    def test_part_number_canonicalized_to_first_token_and_10_chars(self):
        df = self._parse(SAR8_SHOW_MDA_DETAIL_WITH_A32_AT_SLOT_5)
        # All Nokia part numbers in this fixture are 14-char vendor
        # forms that truncate to a 10-char canonical SKU.
        self.assertTrue(all(len(pn) <= 10 for pn in df["Part Number"].tolist()))


if __name__ == "__main__":
    unittest.main()

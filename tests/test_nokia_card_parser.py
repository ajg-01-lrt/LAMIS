"""Regression tests for Nokia SAR/IXR `show card` parsing.

Bug fixed here: when Card B (or any slot) is provisioned but unequipped, the
device emits `Part number :` / `Serial number :` lines with empty values.
The old regex used `\\s*` around the colon, which matched newlines — so the
Serial Number value capture slid down to the next non-empty line in the
buffer, which is the device prompt (e.g. `*A:hostname#`). The Card B row
then carried the prompt string as its serial number.
"""

import unittest
from unittest.mock import MagicMock

import pandas as pd

from scripts.Nokia_SAR import Script as SARScript
from scripts.Nokia_IXR import Script as IXRScript


_EMPTY_CARD_B_OUTPUT = (
    '*A:IPMuxSar8-CANAL-02# show card b detail | match expression '
    '"(Slot)|(^B)|(Part number)|(Serial number)"\n'
    'Slot   Provisioned Type\n'
    'B      csmv2-10g\n'
    'Part number     :  \n'
    'Serial number   :  \n'
    '*A:IPMuxSar8-CANAL-02#'
)

_POPULATED_CARD_B_OUTPUT = (
    '*A:IPMuxSar8-CANAL-01# show card b detail | match expression '
    '"(Slot)|(^B)|(Part number)|(Serial number)"\n'
    'Slot   Provisioned Type\n'
    'B      csmv2-10g\n'
    'Part number     : 3HE02774CB\n'
    'Serial number   : NS2607S0463\n'
    '*A:IPMuxSar8-CANAL-01#'
)


def _make_script(script_cls):
    s = script_cls.__new__(script_cls)
    s.db_cache = MagicMock()
    s.db_cache.lookup_part.return_value = "Invalid part number"
    return s


class _CardParserMixin:
    script_cls = None

    def _parse(self, output):
        s = _make_script(self.script_cls)
        captured = {}

        def cache_cb(df, key):
            captured[key] = df

        s.extract_card_details(output, ['B'], 'Card B', cache_cb, 'COM3')
        return captured['Card B_data']

    def test_empty_card_does_not_capture_prompt_as_serial(self):
        df = self._parse(_EMPTY_CARD_B_OUTPUT)
        if df.empty:
            return  # acceptable — empty card simply omitted
        self.assertEqual(len(df), 1)
        row = df.iloc[0]
        self.assertNotIn('IPMuxSar8-CANAL-02', str(row['Serial Number']))
        self.assertNotIn('#', str(row['Serial Number']))
        self.assertNotIn(':', str(row['Serial Number']))
        # An unequipped card should land as empty/whitespace, not as a prompt.
        self.assertEqual(str(row['Serial Number']).strip(), '')

    def test_populated_card_parsed_correctly(self):
        df = self._parse(_POPULATED_CARD_B_OUTPUT)
        self.assertEqual(len(df), 1)
        row = df.iloc[0]
        self.assertEqual(row['Part Number'], '3HE02774CB')
        self.assertEqual(row['Serial Number'], 'NS2607S0463')
        self.assertEqual(row['Type'], 'csmv2-10g')
        self.assertEqual(row['Name'], 'Slot B')


class TestNokiaSARCardParser(_CardParserMixin, unittest.TestCase):
    script_cls = SARScript


class TestNokiaIXRCardParser(_CardParserMixin, unittest.TestCase):
    script_cls = IXRScript


if __name__ == '__main__':
    unittest.main()

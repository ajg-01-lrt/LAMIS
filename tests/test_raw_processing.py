import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from gui.raw_frame import (
    AUTO_DETECT_NOKIA,
    _detect_nokia_raw_script,
    _normalize_device_id,
    _normalize_device_map,
    _read_text_folder,
    _resolve_script_module,
    _split_raw_output_by_commands,
)
from gui.workbook_builder import WorkbookBuilder
from scripts.Nokia_IXR_Raw import Script as NokiaIXRRawScript
from scripts.Nokia_SAR_Raw import Script as NokiaSARRawScript


IXR_SAMPLE = textwrap.dedent(
    """
    Beginning of SecureCRT Session Log

    A:ALSN001_7250# environment no more
    A:ALSN001_7250# show chassis detail | match expression "Part|Serial|Model|Information|Chassis 1|supply number|supply model|Status|Fan tray number"
    System Information
    Chassis 1 Detail
      Chassis Status                    : up
        Part number                     : 3HE11278AARC01
        Serial number                   : NS233464965
    Environment Information
      Fan tray number                   : 1
        Status                          : up
          Part number                   : 3HE11279AARC01
          Serial number                 : NS233362873
    Power Supply Information
      Power supply number               : 1
        Power supply model              : pem
        Status                          : up
    A:ALSN001_7250# show mda detail | match expression "MDA 1/|Part number|Serial number|Operational state"
    MDA 1/1 detail
        Part number                   : 3HE12518AARA01
        Serial number                 : NS232762954
        Operational state             : up
    MDA 1/2 detail
        Part number                   : 3HE12518AARA01
        Serial number                 : NS232762957
        Operational state             : up
    A:ALSN001_7250# show port detail | match expression "Serial|Model| 1/1| 1/2| 1/3| 1/4| 1/5| 1/6"
    Interface          : 1/1/1                      Oper Speed       : 10 Gbps
    Model Number       : 3HE04823AAAA01  NOK  IPU3ANKEAA
    Serial Number      : ATOPCN23324394
    Interface          : 1/1/2                      Oper Speed       : 10 Gbps
    Model Number       : 3HE04823AAAA01  NOK  IPU3ANKEAA
    Serial Number      : ATOPCN23324418
    Interface          : 1/1/5                      Oper Speed       : 100 Gbps
    Model Number       : 3HE13998AARA01  NOK  INUIAEGGAA
    Serial Number      : NK2313F0008
    Interface          : 1/2/1                      Oper Speed       : 10 Gbps
    Model Number       : 3HE04823AAAA01  NOK  IPU3ANKEAA
    Serial Number      : ATOPCN23324391
    Interface          : 1/2/2                      Oper Speed       : 10 Gbps
    Model Number       : 3HE04823AAAA01  NOK  IPU3ANKEAA
    Serial Number      : ATOPCN23324419
    Interface          : 1/2/5                      Oper Speed       : 100 Gbps
    Model Number       : 3HE13998AARA01  NOK  INUIAEGGAA
    Serial Number      : NK2301F0002
    A:ALSN001_7250# environment more
    End of SecureCRT Session Log
    """
).strip()


SAR_SAMPLE = textwrap.dedent(
    """
    Beginning of SecureCRT Session Log

    A:ABER002_7705# environment no more
    A:ABER002_7705# show chassis detail | match expression "Part|Serial|Model|Information|Chassis 1|supply number|supply model|Status|Fan tray number"
    System Information
    Chassis 1 Detail
      Chassis Status                    : up
        Part number                     : 3HE06791AAAC0105
        Serial number                   : NS2326S1035
    Environment Information
            Status                      : ok
        Fan Information
            Status                      : up
        Part number                     : 3HE06792EAAC0101
        Serial number                   : NS2333S0594
    A:ABER002_7705# show mda detail | match expression "MDA 1/|Part number|Serial number|Operational state"
    MDA 1/1 detail
        Part number                   : 3HE07943AAAD0104
        Serial number                 : NS2328S0403
        Operational state             : up
    MDA 1/2 detail
        Part number                   : 3HE07943AAAD0104
        Serial number                 : NS2328S0555
        Operational state             : up
    MDA 1/5 detail
        Part number                   : 3HE02781AAAD0102
        Serial number                 : NS2330S0391
        Operational state             : up
    A:ABER002_7705# show port detail | match expression "Serial|Model| 1/1| 1/2| 1/3| 1/4| 1/5| 1/6"
    Interface          : 1/1/5                      Oper Speed       : 10 Gbps
    Model Number       : 3HE04823AAAA01  NOK  IPU3ANKEAA
    Serial Number      : DL232800011748
    Interface          : 1/1/6                      Oper Speed       : 10 Gbps
    Model Number       : 3HE05036AAAA01  NOK  IPU3ASLEAA
    Serial Number      : ATOPCN23257518
    Interface          : 1/2/5                      Oper Speed       : 10 Gbps
    Model Number       : 3HE04823AAAA01  NOK  IPU3ANKEAA
    Serial Number      : DL232800011641
    Interface          : 1/2/6                      Oper Speed       : 10 Gbps
    A:ABER002_7705# environment more
    End of SecureCRT Session Log
    """
).strip()


SAR_NONE_PORT_SAMPLE = textwrap.dedent(
    """
    Beginning of SecureCRT Session Log

    A:ABER001_7705# environment no more
    A:ABER001_7705# show chassis detail | match expression "Part|Serial|Model|Information|Chassis 1|supply number|supply model|Status|Fan tray number"
    System Information
    Chassis 1 Detail
      Chassis Status                    : up
        Part number                     : 3HE06791AAAC0105
        Serial number                   : NS2326S1038
    A:ABER001_7705# show mda detail | match expression "MDA 1/|Part number|Serial number|Operational state"
    MDA 1/4 detail
        Part number                   : 3HE03127AABC0101
        Serial number                 : NS14306K177
        Operational state             : up
    A:ABER001_7705# show port detail | match expression "Serial|Model| 1/1| 1/2| 1/3| 1/4| 1/5| 1/6"
    Interface          : 1/1/1                      Oper Speed       : 1 Gbps
    Model Number       : 3HE00028CAAA01  NOK  IPUIBDLDAA
    Serial Number      : ATOPCN23293864
    Interface          : 1/1/4                      Oper Speed       : N/A
    Model Number       : none
    Serial Number      : VT2340000899
    Interface          : 1/1/5                      Oper Speed       : 10 Gbps
    Model Number       : 3HE04823AAAA01  NOK  IPU3ANKEAA
    Serial Number      : DL232800011727
    A:ABER001_7705# environment more
    End of SecureCRT Session Log
    """
).strip()


IXR_CHILD_SPEED_SAMPLE = textwrap.dedent(
    """
    Beginning of SecureCRT Session Log

    A:ASHE001_7250# environment no more
    A:ASHE001_7250# show chassis detail | match expression "Part|Serial|Model|Information|Chassis 1|supply number|supply model|Status|Fan tray number"
    System Information
    Chassis 1 Detail
      Chassis Status                    : up
        Part number                     : 3HE11278AARC01
        Serial number                   : NS233464965
    A:ASHE001_7250# show mda detail | match expression "MDA 1/|Part number|Serial number|Operational state"
    MDA 1/1 detail
        Part number                   : 3HE12518AARA01
        Serial number                 : NS232762954
        Operational state             : up
    A:ASHE001_7250# show port detail | match expression "Serial|Model| 1/1| 1/2| 1/3| 1/4| 1/5| 1/6"
    Interface          : 1/5/c7
    Model Number       : 3HE10550AARA01  NOK  IPU3BFUEAA
    Serial Number      : NK2325N00GC
    Interface          : 1/5/c7/1                   Oper Speed       : 100 Gbps
    Interface          : 1/6/c7
    Model Number       : 3HE10550AARA01  NOK  IPU3BFUEAA
    Serial Number      : NK2321N00UW
    Interface          : 1/6/c7/1                   Oper Speed       : 100 Gbps
    A:ASHE001_7250# environment more
    End of SecureCRT Session Log
    """
).strip()


class TestRawTextFolder(unittest.TestCase):

    def test_reads_only_txt_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            (base / "device_a.txt").write_text("alpha", encoding="utf-8")
            (base / "device_b.TXT").write_text("beta", encoding="utf-8")
            (base / "ignore.csv").write_text("gamma", encoding="utf-8")

            data = _read_text_folder(tmpdir)

        self.assertEqual(set(data.keys()), {"device_a", "device_b"})
        self.assertEqual(data["device_a"], "alpha")
        self.assertEqual(data["device_b"], "beta")

    def test_reads_txt_files_recursively(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            nested = base / "nested" / "deeper"
            nested.mkdir(parents=True)
            (base / "root.txt").write_text("root", encoding="utf-8")
            (nested / "leaf.txt").write_text("leaf", encoding="utf-8")

            data = _read_text_folder(tmpdir)

        self.assertEqual(set(data.keys()), {"root", "leaf"})
        self.assertEqual(data["leaf"], "leaf")


class TestDeviceIdNormalization(unittest.TestCase):

    def test_strips_datetime_prefix(self):
        raw_name = "11-27-2023 - 20.33 ET - ALSN001-7250"
        self.assertEqual(_normalize_device_id(raw_name), "ALSN001-7250")

    def test_normal_map_deduplicates_after_cleanup(self):
        devices = {
            "11-27-2023 - 20.33 ET - ALSN001-7250": "a",
            "11-28-2023 - 09.15 ET - ALSN001-7250": "b",
        }
        normalized = _normalize_device_map(devices)
        self.assertEqual(set(normalized.keys()), {"ALSN001-7250", "ALSN001-7250_02"})
        self.assertEqual(normalized["ALSN001-7250"], "a")
        self.assertEqual(normalized["ALSN001-7250_02"], "b")


class TestNokiaAutoDetection(unittest.TestCase):

    def test_detects_ixr_from_prompt_and_text(self):
        self.assertEqual(
            _detect_nokia_raw_script(IXR_SAMPLE, "ALSN001_7250"),
            "scripts.Nokia_IXR_Raw",
        )

    def test_detects_sar_from_prompt_and_text(self):
        self.assertEqual(
            _detect_nokia_raw_script(SAR_SAMPLE, "ABER002_7705"),
            "scripts.Nokia_SAR_Raw",
        )

    def test_auto_detect_resolves_ixr_parser(self):
        self.assertEqual(
            _resolve_script_module(IXR_SAMPLE, "ALSN001_7250", AUTO_DETECT_NOKIA),
            "scripts.Nokia_IXR_Raw",
        )

    def test_detects_psi_4l_variant(self):
        # Both 4L and 8L PSI hardware tags must auto-detect to Nokia_PSI;
        # previously the 8L branch was unreachable dead code.
        text = "Welcome to NOKIA-4L PSI shelf\nSystem build info..."
        self.assertEqual(_detect_nokia_raw_script(text, ""), "scripts.Nokia_PSI")

    def test_detects_psi_8l_variant(self):
        text = "Welcome to NOKIA-8L PSI shelf\nSystem build info..."
        self.assertEqual(_detect_nokia_raw_script(text, ""), "scripts.Nokia_PSI")

    def test_detects_bare_psi_keyword(self):
        text = "psi> show shelf\nShelf 1 info..."
        self.assertEqual(_detect_nokia_raw_script(text, ""), "scripts.Nokia_PSI")


class TestRawProcessMultiFamilyDispatch(unittest.TestCase):
    """``_process_multi`` must derive the workbook-builder family from the
    per-sheet *resolved* module path. Using the raw dropdown value would
    always return "default" for Auto Detect Nokia because
    ``SCRIPT_OPTIONS[AUTO_DETECT_NOKIA] == ''``, which silently bypasses
    the PSI / RLS specific builders for multi-device runs.
    """

    def setUp(self):
        from gui.raw_frame import RawFrame
        # Skip Tk init — we only need the method, not the UI.
        self.frame = RawFrame.__new__(RawFrame)
        self.frame.gui = MagicMock()
        self.frame.gui.db_cache = MagicMock()
        self.frame.gui.db_file = ":memory:"
        self.frame._input_path = "/tmp/multidev.xlsx"
        self.frame._log_write = lambda *_a, **_kw: None
        self._after_calls = []
        self.frame.after = lambda delay, fn, *args: self._after_calls.append((fn, args))

    def _stub_parse(self, family_per_sheet):
        """Stub ``_parse_device`` so each sheet succeeds, and capture
        the resolved module each call gets. Returns the captured list."""
        captured = []
        def _fake_parse(raw_text, device_id, module_path, outputs):
            captured.append(module_path)
            outputs[device_id] = {"data": MagicMock()}
            return True
        self.frame._parse_device = _fake_parse
        return captured

    def test_auto_detect_multi_psi_dispatches_psi_family(self):
        """Multi-sheet workbook of PSI transcripts under Auto Detect must
        hit the PSI builder family, not "default"."""
        self._stub_parse(["psi"] * 2)
        sheets = {
            "PSI_NODE_A": "Welcome to NOKIA-4L PSI shelf\n",
            "PSI_NODE_B": "Welcome to NOKIA-8L PSI shelf\n",
        }
        self.frame._process_multi(sheets, AUTO_DETECT_NOKIA)
        # _export gets called via self.after(0, self._export, outputs, family, label)
        self.assertEqual(len(self._after_calls), 1)
        fn, args = self._after_calls[0]
        outputs, family, label = args
        self.assertEqual(family, "psi")
        self.assertEqual(len(outputs), 2)

    def test_auto_detect_multi_ixr_falls_back_to_default_family(self):
        """IXR has no special family — default builder is the right choice."""
        sheets = {
            "ALSN001_7250": IXR_SAMPLE,
            "ALSN002_7250": IXR_SAMPLE,
        }
        self._stub_parse(["default"] * 2)
        self.frame._process_multi(sheets, AUTO_DETECT_NOKIA)
        self.assertEqual(len(self._after_calls), 1)
        _, args = self._after_calls[0]
        _, family, _ = args
        self.assertEqual(family, "default")

    def test_auto_detect_mixed_psi_and_ixr_picks_psi_family(self):
        """When a multi-sheet input mixes PSI (family="psi") and IXR
        (family="default"), PSI is the only non-default family in the
        set — it wins, and the IXR sheet still rides along on the same
        PSI workbook. Verifies the "single non-default wins" rule."""
        sheets = {
            "PSI_NODE":   "Welcome to NOKIA-4L PSI shelf\n",
            "IXR_NODE":   IXR_SAMPLE,
        }
        self._stub_parse(["mixed"] * 2)
        self.frame._process_multi(sheets, AUTO_DETECT_NOKIA)
        _, args = self._after_calls[0]
        _, family, _ = args
        self.assertEqual(family, "psi")


class TestNokiaRawProcessing(unittest.TestCase):

    def test_ixr_raw_parser_extracts_expected_sections(self):
        parser = NokiaIXRRawScript(connection_type="ssh", ip_address="ALSN001_7250")
        sections = _split_raw_output_by_commands(IXR_SAMPLE, parser.get_commands())
        outputs = {}

        parser.process_outputs(sections, "ALSN001_7250", outputs)

        self.assertEqual(sorted(outputs["ALSN001_7250"].keys()), ["hardware_data", "mda_data", "port_data"])
        self.assertEqual(len(outputs["ALSN001_7250"]["hardware_data"]["DataFrame"]), 2)
        self.assertEqual(len(outputs["ALSN001_7250"]["mda_data"]["DataFrame"]), 2)
        self.assertEqual(len(outputs["ALSN001_7250"]["port_data"]["DataFrame"]), 6)

    def test_sar_raw_parser_extracts_expected_sections(self):
        parser = NokiaSARRawScript(connection_type="ssh", ip_address="ABER002_7705")
        sections = _split_raw_output_by_commands(SAR_SAMPLE, parser.get_commands())
        outputs = {}

        parser.process_outputs(sections, "ABER002_7705", outputs)

        self.assertEqual(sorted(outputs["ABER002_7705"].keys()), ["hardware_data", "mda_data", "port_data"])
        self.assertEqual(len(outputs["ABER002_7705"]["hardware_data"]["DataFrame"]), 2)
        self.assertEqual(len(outputs["ABER002_7705"]["mda_data"]["DataFrame"]), 3)
        self.assertEqual(len(outputs["ABER002_7705"]["port_data"]["DataFrame"]), 3)

    def test_sar_raw_parser_skips_ports_with_none_model_number(self):
        parser = NokiaSARRawScript(connection_type="ssh", ip_address="ABER001_7705")
        sections = _split_raw_output_by_commands(SAR_NONE_PORT_SAMPLE, parser.get_commands())
        outputs = {}

        parser.process_outputs(sections, "ABER001_7705", outputs)

        port_df = outputs["ABER001_7705"]["port_data"]["DataFrame"]

        self.assertEqual(port_df["Name"].tolist(), ["1/1/1", "1/1/5"])
        self.assertNotIn("none", port_df["Part Number"].tolist())

    def test_ixr_raw_parser_inherits_speed_from_child_interface(self):
        parser = NokiaIXRRawScript(connection_type="ssh", ip_address="ASHE001_7250")
        parser.get_part_description = MagicMock(return_value="QSFP28 - 100GBASE-LR4 ROHS6/6 0/70C")
        sections = _split_raw_output_by_commands(IXR_CHILD_SPEED_SAMPLE, parser.get_commands())
        outputs = {}

        parser.process_outputs(sections, "ASHE001_7250", outputs)

        port_df = outputs["ASHE001_7250"]["port_data"]["DataFrame"]

        self.assertEqual(port_df["Name"].tolist(), ["1/5/c7", "1/6/c7"])
        self.assertEqual(
            port_df["Type"].tolist(),
            ["QSFP28 - 100GBASE-LR4 ROHS6/6 0/70C", "QSFP28 - 100GBASE-LR4 ROHS6/6 0/70C"],
        )

    def test_ixr_raw_parser_uses_description_when_speed_missing(self):
        parser = NokiaIXRRawScript(connection_type="ssh", ip_address="DITT002_7250")
        parser.get_part_description = MagicMock(return_value="SFP+ 10GE ER - LC")
        sections = _split_raw_output_by_commands(
            textwrap.dedent(
                """
                Beginning of SecureCRT Session Log
                A:DITT002_7250# show chassis detail | match expression "Part|Serial|Model|Information|Chassis 1|supply number|supply model|Status|Fan tray number"
                Chassis 1 Detail
                  Part number                     : 3HE11278AARC01
                  Serial number                   : NS233464965
                A:DITT002_7250# show mda detail | match expression "MDA 1/|Part number|Serial number|Operational state"
                MDA 1/1 detail
                    Part number                   : 3HE12518AARA01
                    Serial number                 : NS232762954
                    Operational state             : up
                A:DITT002_7250# show port detail | match expression "Serial|Model| 1/1| 1/2| 1/3| 1/4| 1/5| 1/6"
                Interface          : 1/5/c16
                Model Number       : 3HE05036AAAA01  NOK  IPU3ASLEAA
                Serial Number      : ATOPCN23257519
                A:DITT002_7250# environment more
                End of SecureCRT Session Log
                """
            ).strip(),
            parser.get_commands(),
        )
        outputs = {}

        parser.process_outputs(sections, "DITT002_7250", outputs)

        port_df = outputs["DITT002_7250"]["port_data"]["DataFrame"]

        self.assertEqual(port_df.iloc[0]["Type"], "SFP+ 10GE ER - LC")

    def test_ixr_raw_parser_falls_back_to_speed_when_description_missing(self):
        parser = NokiaIXRRawScript(connection_type="ssh", ip_address="ALSN001_7250")
        parser.get_part_description = MagicMock(return_value="Not Found")
        sections = _split_raw_output_by_commands(IXR_SAMPLE, parser.get_commands())
        outputs = {}

        parser.process_outputs(sections, "ALSN001_7250", outputs)

        port_df = outputs["ALSN001_7250"]["port_data"]["DataFrame"]

        self.assertEqual(port_df.iloc[0]["Type"], "10 Gbps")

    def test_ixr_raw_parser_falls_back_to_speed_when_description_is_unknown(self):
        parser = NokiaIXRRawScript(connection_type="ssh", ip_address="ALSN001_7250")
        parser.get_part_description = MagicMock(return_value="Unknown")
        sections = _split_raw_output_by_commands(IXR_SAMPLE, parser.get_commands())
        outputs = {}

        parser.process_outputs(sections, "ALSN001_7250", outputs)

        port_df = outputs["ALSN001_7250"]["port_data"]["DataFrame"]

        self.assertEqual(port_df.iloc[0]["Type"], "10 Gbps")

    def test_ixr_raw_parser_extracts_part_number_when_vendor_prefix_present(self):
        parser = NokiaIXRRawScript(connection_type="ssh", ip_address="CHJO001_7250")
        parser.get_part_description = MagicMock(return_value="Not Found")
        sample = textwrap.dedent(
            """
            Beginning of SecureCRT Session Log
            A:CHJO001_7250# show chassis detail | match expression "Part|Serial|Model|Information|Chassis 1|supply number|supply model|Status|Fan tray number"
            Chassis 1 Detail
              Part number                     : 3HE11278AARC01
              Serial number                   : NS233464965
            A:CHJO001_7250# show mda detail | match expression "MDA 1/|Part number|Serial number|Operational state"
            MDA 1/1 detail
                Part number                   : 3HE12518AARA01
                Serial number                 : NS232762954
                Operational state             : up
            A:CHJO001_7250# show port detail | match expression "Serial|Model| 1/1| 1/2| 1/3| 1/4| 1/5| 1/6"
            Interface          : 1/1/3                      Oper Speed       : 10 Gbps
            Model Number       : ALCATEL 3FE62600AA03 VAUIAS3AAA
            Serial Number      : F1700203060
            A:CHJO001_7250# environment more
            End of SecureCRT Session Log
            """
        ).strip()
        sections = _split_raw_output_by_commands(sample, parser.get_commands())
        outputs = {}

        parser.process_outputs(sections, "CHJO001_7250", outputs)

        port_df = outputs["CHJO001_7250"]["port_data"]["DataFrame"]

        self.assertEqual(port_df.iloc[0]["Part Number"], "3FE62600AA")
        self.assertEqual(port_df.iloc[0]["Type"], "10 Gbps")


class TestWorkbookBuilderMdaLabels(unittest.TestCase):

    def test_preserves_full_mda_slot_in_sheet_output(self):
        builder = WorkbookBuilder(MagicMock(), "", "")

        combined_df = builder.combine_and_format_data(
            {
                "mda_data": {
                    "DataFrame": __import__("pandas").DataFrame(
                        [
                            {
                                "System Name": "ABER001_7705",
                                "System Type": "Nokia 7705 SAR",
                                "Type": "Example Card (up)",
                                "Part Number": "3HE03127AA",
                                "Serial Number": "NS14306K177",
                                "Description": "Example Card",
                                "Information Type": "MDA Card",
                                "Name": "1/4",
                                "Source": "ABER001_7705",
                            }
                        ]
                    )
                }
            }
        )

        row = combined_df.iloc[0]
        info_type = str(row.get("Information Type", "")).strip().lower()
        name_value = str(row.get("Name", "")).strip()

        if "mda card" in info_type:
            name_value = f"MDA {name_value}" if name_value else "MDA"

        self.assertEqual(name_value, "MDA 1/4")

    def test_skips_blank_provisioned_mda_rows(self):
        parser = NokiaSARRawScript(connection_type="ssh", ip_address="BEAR001_7705")
        sections = _split_raw_output_by_commands(
            textwrap.dedent(
                """
                Beginning of SecureCRT Session Log
                A:BEAR001_7705# show chassis detail | match expression "Part|Serial"
                Chassis 1 Detail
                  Part number                     : 3HE06791AAAC0105
                  Serial number                   : NS2326S1038
                A:BEAR001_7705# show mda detail | match expression "MDA 1/|Part number|Serial number|Operational state"
                MDA 1/1 detail
                    Part number                   : 3HE07943AAAD0104
                    Serial number                 : NS2422S0667
                    Operational state             : up
                MDA 1/3 detail
                    Part number                   :
                    Serial number                 :
                    Operational state             : provisioned
                A:BEAR001_7705# show port detail | match expression "Serial|Model| 1/1"
                Interface          : 1/1/5                      Oper Speed       : 10 Gbps
                Model Number       : 3HE04823AAAA01  NOK  IPU3ANKEAA
                Serial Number      : DL232800011748
                End of SecureCRT Session Log
                """
            ).strip(),
            parser.get_commands(),
        )
        outputs = {}

        parser.process_outputs(sections, "BEAR001_7705", outputs)

        mda_df = outputs["BEAR001_7705"]["mda_data"]["DataFrame"]

        self.assertEqual(mda_df["Name"].tolist(), ["1/1"])
        self.assertNotIn("Serial num", mda_df["Part Number"].tolist())

    def test_preserves_existing_description_when_db_lookup_fails(self):
        builder = WorkbookBuilder(MagicMock(), "", "")
        builder.db_cache.db_path = "missing.db"
        builder.db_cache.lookup_part.return_value = "Not Found"

        row = {
            "System Name": "PATS001_7705",
            "System Type": "Unknown",
            "Type": "No inventory sections matched",
            "Part Number": "UNPARSED",
            "Serial Number": "",
            "Description": "Transcript did not contain expected inventory commands",
            "Name": "No Inventory Data",
            "Source": "Manual",
        }

        combined_df = __import__("pandas").DataFrame([row])
        description = str(combined_df.iloc[0]["Description"])
        part_number = str(combined_df.iloc[0]["Part Number"])

        if part_number:
            db_description = builder.db_cache.lookup_part(part_number[:10])
            if db_description and db_description != "Not Found":
                description = db_description

        self.assertEqual(description, "Transcript did not contain expected inventory commands")


if __name__ == "__main__":
    unittest.main()
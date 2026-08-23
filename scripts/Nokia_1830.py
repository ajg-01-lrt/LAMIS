"""scripts/Nokia_1830.py — Nokia PSS inventory (selected as "Nokia PSS").

Thin variant of the shared Nokia 1830-family engine
(:mod:`scripts._nokia_1830_family`). This variant keeps the lean PSS read-out
(system name + shelf / card / interface inventory) and its Name-column
conventions.

The module file keeps its ``Nokia_1830`` name for import/back-compat (the GUI
dropdown label is "Nokia PSS"). Login (SSH auth as admin, then the getty
two-step driven on the shell channel by ``_settle_shell``; Telnet three-stage
via ``telnet_login``) and all plumbing live in the engine; this class only
declares the command list and the matching parser pipeline.
"""
from __future__ import annotations

from typing import Callable, List

import pandas as pd

from scripts._nokia_1830_family import Nokia1830FamilyScript


class Script(Nokia1830FamilyScript):
    LABEL = "PSS"

    COMMANDS = [
        "show general name",
        "show shelf inventory *",
        "show card inventory *",
        "show interface inventory *",
    ]

    def _pipeline(self, ip_address: str) -> List[Callable[[str, Callable], pd.DataFrame]]:
        return [
            lambda out, cb: self.extract_system_name(out, cb, ip_address),
            lambda out, cb: self.extract_shelf_inventory_1830(out, cb, ip_address),
            lambda out, cb: self.extract_card_inventory_1830(out, cb, ip_address),
            lambda out, cb: self.extract_interface_inventory(out, cb, ip_address),
        ]

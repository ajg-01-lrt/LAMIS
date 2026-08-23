"""scripts/Nokia_PSI.py — Nokia 1830 PSI (PSI-4L / PSI-8L) inventory.

Thin variant of the shared Nokia 1830-family engine
(:mod:`scripts._nokia_1830_family`). The PSI runs the full read-out — shelf /
card / module inventory plus software, slot, redundancy, power-feed and
topology — and uses the richer ``show general system-identification`` shelf
parser.

Login (SSH cli two-step + Telnet three-stage) and all plumbing live in the
engine; this class only declares the command list and the matching parser
pipeline. See the engine module docstring for the two-step login details.
"""
from __future__ import annotations

from typing import Callable, List

import pandas as pd

from scripts._nokia_1830_family import Nokia1830FamilyScript


class Script(Nokia1830FamilyScript):
    LABEL = "PSI"

    # LAN PSI is routed to Telnet (see gui4_0._lan_connection_types): SSH auth as
    # admin dead-ends at the alarm banner on these shelves, while the Telnet
    # getty two-step (login:cli / Username:admin / Password:admin) drives the
    # CLI. The Telnet three-stage lives in the engine (telnet_login); this
    # variant only differs from Nokia_1830 in its command list + parser pipeline.

    COMMANDS = [
        "show general name",                   # system (host) name — "System Name: <host>"
        "show general system-identification",  # canonical shelf type (PSI-4L/PSI-8L)
        "show shelf inventory *",              # shelf hardware
        "show card inventory *",               # card inventory
        "show interface inventory *",          # module / transceiver inventory
        "show software dynamic",               # software release info
        "show slot *",                         # slot programming state
        "show redundancy 1 detail",            # redundancy / clock switch
        "show pf *",                           # power feed status
        "show interface topology *",           # port connectivity
    ]

    def _pipeline(self, ip_address: str) -> List[Callable[[str, Callable], pd.DataFrame]]:
        # 'show general system-identification' carries the shelf TYPE + serial but
        # not the hostname, so we also run 'show general name' first for the
        # System Name (its row has no Part Number, so it never appears as an
        # equipment line — only feeds the sheet title / F6).
        return [
            lambda out, cb: self.extract_system_name(out, cb, ip_address),
            lambda out, cb: self.extract_shelf_detail(out, cb, ip_address),
            lambda out, cb: self.extract_shelf_inventory(out, cb, ip_address),
            lambda out, cb: self.extract_card_inventory(out, cb, ip_address),
            lambda out, cb: self.extract_module_inventory(out, cb, ip_address),
            lambda out, cb: self.extract_software_info(out, cb, ip_address),
            lambda out, cb: self.extract_slot_info(out, cb, ip_address),
            lambda out, cb: self.extract_redundancy_info(out, cb, ip_address),
            lambda out, cb: self.extract_power_info(out, cb, ip_address),
            lambda out, cb: self.extract_topology(out, cb, ip_address),
        ]

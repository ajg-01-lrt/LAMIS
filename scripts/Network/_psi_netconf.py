"""NETCONF/YANG parsers for the Nokia 1830 PSI audit (Phase 3 of the NETCONF
rebase).

Each parser takes a namespace-stripped lxml element for a top-level YANG
container (as returned by ``utils.netconf.NetconfSession.get``) and produces the
record structures the audit's Excel writer already consumes. That keeps the
discovery walk, the OAMP-LAN reachability, and the workbook code unchanged --
only per-node data collection moves from CLI scraping to NETCONF.

This starts with the Amplifiers parser (validated field-for-field against a live
ILA dump); further parsers (inventory, alarms, connections, power) follow.
"""
import base64
import datetime
import re
import struct

__all__ = [
    "parse_amplifiers", "parse_alarms", "parse_inventory", "parse_connections",
    "parse_port_power", "parse_interfaces", "parse_ospf", "parse_ospf_config",
    "parse_power", "parse_transceivers", "derive_power_filters", "neighbor_ips",
    "card_type_by_slot", "collect_node_netconf",
]

# Nokia part-number -> CLI mnemonic (the mnemonic is not a YANG leaf). Seeded
# from the 1830 PSI card inventory; extend as new cards appear. Amps still
# derive EILAL/EILA from the amp-name band as a fallback.
PART_MNEMONIC = {
    "3KC70525AAAF01": "EILAL",
    "3KC70175AAAF01": "EILA",
    "3KC72357AAAC02": "OMDWB",
    "8DG63029ABNF02": "RA5PB",
    "3KC90213AAAC01": "MFC",
    "3KC81775ACNE05": "MEC2L",
    "3KC90259AAAB01": "FAN",
    "3KC90297AAAD01": "PF",
    "3KC90395AA":     "PF",
}


# ── small YANG helpers ────────────────────────────────────────────────────────

def _local(v):
    """Strip a YANG module prefix from an enum value ('oc-x:FOO' -> 'FOO')."""
    return v.split(":", 1)[-1] if v else v


def _text(elem, path):
    """Text of the first descendant at *path* (lxml find), stripped, or None."""
    if elem is None:
        return None
    f = elem.find(path)
    return f.text.strip() if f is not None and f.text and f.text.strip() else None


def _instant(elem, path):
    """A telemetry leaf's current value: ``<path><instant>X</instant></path>``."""
    return _text(elem, f"{path}/instant")


def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _ieee32(v):
    """Decode Nokia's base64 IEEE-754 float32 leaf (e.g. power-supply current/
    voltage) to a float. Falls back to plain float() for decimal-encoded values."""
    if v is None:
        return None
    try:
        return round(struct.unpack(">f", base64.b64decode(v))[0], 4)
    except (ValueError, struct.error, base64.binascii.Error):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None


def _epoch_ns(s):
    """Nanosecond-epoch string -> 'YYYY-MM-DD HH:MM:SS' (UTC), or the raw value."""
    try:
        dt = datetime.datetime.fromtimestamp(int(s) / 1e9, datetime.timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return s


# ── Alarms ────────────────────────────────────────────────────────────────────
# system/alarms/alarm/state/{id, resource, text, time-created, severity, type-id}
_SEV = {"CRITICAL": "CR", "MAJOR": "MJ", "MINOR": "MN", "WARNING": "WN"}


def parse_alarms(system_elem):
    """Parse ``system/alarms`` into alarm rows. resource is PORT-<sh>-<slot>-<port>
    or CARD-<sh>-<name>; we also split out the card/port for the sheet."""
    rows = []
    if system_elem is None:
        return rows
    for al in system_elem.findall(".//alarms/alarm"):
        st = al.find("state")
        if st is None:
            continue
        sev = _local(_text(st, "severity"))
        resource = _text(st, "resource") or ""
        rows.append({
            "id": _text(st, "id"),
            "severity": _SEV.get(sev, sev),
            "resource": resource,
            "condition": _text(st, "type-id"),
            "text": _text(st, "text"),
            "time_created": _epoch_ns(_text(st, "time-created")),
            "kind": ("card" if resource.startswith("CARD-")
                     else "port" if resource.startswith("PORT-") else "other"),
        })
    return rows


# ── Card inventory ────────────────────────────────────────────────────────────
# components/component/state/{type, mfg-name, serial-no, part-no, clei-code,
#                             hardware-version, oper-status}

def parse_inventory(components_elem):
    """Parse ``components`` into card inventory rows (CHASSIS + CARD-*). The Nokia
    card mnemonic (EILAL/EILA/OMDWB...) is NOT a YANG leaf; ``card_type_by_slot``
    for the amp parser is derived elsewhere (amp band / part-no lookup)."""
    rows = []
    if components_elem is None:
        return rows
    for comp in components_elem.findall(".//component"):
        st = comp.find("state")
        if st is None:
            continue
        name = _text(st, "name") or _text(comp, "name") or ""
        if not (name.startswith("CARD-") or name.startswith("CHASSIS")):
            continue
        rows.append({
            "name": name,
            "slot": name.split("-", 2)[-1] if name.startswith("CARD-") else "--",
            "ctype": _local(_text(st, "type")),
            "mfg": _text(st, "mfg-name"),
            "serial": _text(st, "serial-no"),
            "part": _text(st, "part-no"),
            "clei": _text(st, "clei-code"),
            "hw": _text(st, "hardware-version"),
            "hw_name": _text(st, "hardware-name"),   # CHASSIS -> shelf model (PSI-8L)
            "oper": _local(_text(st, "oper-status")),
        })
    return rows


# ── OSC-SFP transceivers (OSC Optics sheet) ───────────────────────────────────
# components/component[TRANSCEIVER-<sh>-<slot>-OSCSFP*]/state -> the OSC optics
# that `show interface inventory *` lists (module type, serial, part).

def parse_transceivers(components_elem):
    """OSC-SFP optic rows: {loc '1/4/OSCSFP1', module, serial, part, clei}."""
    rows = []
    if components_elem is None:
        return rows
    for comp in components_elem.findall(".//component"):
        st = comp.find("state")
        name = (_text(st, "name") if st is not None else None) or _text(comp, "name") or ""
        if not (name.startswith("TRANSCEIVER-") and "OSCSFP" in name.upper()):
            continue
        loc = name.split("-", 1)[-1].replace("-", "/")   # TRANSCEIVER-1-4-OSCSFP1 -> 1/4/OSCSFP1
        clei = _text(st, "clei-code")
        rows.append({
            "loc": loc,
            "module": _text(st, "hardware-name"),
            "serial": _text(st, "serial-no"),
            "part": _text(st, "part-no"),
            "clei": clei if clei and set(clei) != {"-"} else "--",
        })
    return rows


# ── Intra-node connections (Internal Fibers) ──────────────────────────────────
# connections/connection/state/{source, dest, fiber-type}. Patch loss = source
# port output-power vs dest port input-power (from components optical-port).

def parse_connections(conn_elem):
    """Parse ``connections`` into fiber records (source -> dest).

    Intra-node connections join two local ports (LINE<->SIG). External-span
    connections use the literal ``EXTERNAL`` for the far end and carry
    ``external-peer-address`` (the adjacent NE's DCN loopback IP) plus
    ``external-peer-port`` -- these are the NETCONF equivalent of the CLI
    topology "Ext" rows and drive neighbour discovery (see neighbor_ips)."""
    rows = []
    if conn_elem is None:
        return rows
    for c in conn_elem.findall(".//connection"):
        st = c.find("state")
        if st is None:
            st = c
        src = _text(st, "source")
        dst = _text(st, "dest")
        # external-peer-address/port live in a sibling <external-link><state>,
        # not under the connection <state>.
        ext = c.find("external-link/state")
        if ext is None:
            ext = c.find("external-link/config")
        peer = _text(ext, "external-peer-address") if ext is not None else None
        peer_port = _text(ext, "external-peer-port") if ext is not None else None
        rows.append({
            "index": _text(st, "index"),
            "source": src,
            "dest": dst,
            "fiber_type": _local(_text(st, "fiber-type")),
            "external_peer": peer,
            "external_peer_port": peer_port,
            "is_external": bool(peer) or src == "EXTERNAL" or dst == "EXTERNAL",
        })
    return rows


def neighbor_ips(connections):
    """Directly-adjacent neighbour NE IPs from external-span connections, in
    first-seen order -- the NETCONF-native discovery seed set (the equivalent
    of the CLI ``show interface topology`` Ext rows). Feed these into the
    breadth-first walk: connect to each, read *its* external peers, repeat."""
    seen, ips = set(), []
    for c in connections or []:
        ip = (c.get("external_peer") or "").strip()
        if ip and ip not in seen:
            seen.add(ip)
            ips.append(ip)
    return ips


# ── Port optical power ────────────────────────────────────────────────────────
# components/component[PORT-*]/port/optical-port/state. OMDWB ports expose
# calibrated input-power/output-power; OSC ports expose the supervisory-channel
# power; amp line ports expose neither (use amp state). Keyed by port name so
# connections/OSC sheets can look up either end's power.

def parse_port_power(components_elem):
    """Return {port_name: {parent, oper, admin, port_type, in_power, out_power,
    in_supvy, out_supvy}} for every PORT-* component."""
    ports = {}
    if components_elem is None:
        return ports
    for comp in components_elem.findall(".//component"):
        name = _text(comp, "name") or ""
        if not name.startswith("PORT-"):
            continue
        cst = comp.find("state")
        op = comp.find("port/optical-port/state")
        ports[name] = {
            "name": name,
            "parent": _text(cst, "parent"),
            "oper": _local(_text(cst, "oper-status")),
            "admin": _local(_text(op, "admin-state")) if op is not None else None,
            "port_type": _local(_text(op, "optical-port-type")) if op is not None else None,
            "in_power": _num(_instant(op, "input-power")) if op is not None else None,
            "out_power": _num(_instant(op, "output-power")) if op is not None else None,
            "in_supvy": _num(_instant(op, "input-power-supervisory-channel")) if op is not None else None,
            "out_supvy": _num(_instant(op, "output-power-supervisory-channel")) if op is not None else None,
        }
    return ports


# ── Interfaces (loopback / OSCLAN / OAMP / CIT) ───────────────────────────────

def parse_interfaces(interfaces_elem):
    """Return {interface_name: {ipv4, prefix, oper}} for interfaces with an IPv4
    address (loopback, OSCLAN, OAMP, CIT, LAN)."""
    out = {}
    if interfaces_elem is None:
        return out
    for itf in interfaces_elem.findall(".//interface"):
        name = _text(itf, "name") or ""
        addr = itf.find("ip/ipv4/addresses/address")
        out[name] = {
            "name": name,
            "ipv4": (_text(addr, "ip") or _text(addr, "state/ip")) if addr is not None else None,
            "prefix": (_text(addr, "state/prefix-length")
                       or _text(addr, "config/prefix-length")) if addr is not None else None,
            "oper": _local(_text(itf, "state/oper-status")),
        }
    return out


# ── OSPF / routing (Summary + OSPF sheet) ─────────────────────────────────────

def parse_ospf(ni_elem):
    """Return {name, router_id, type} from the default network-instance."""
    if ni_elem is None:
        return {}
    ni = ni_elem.find(".//network-instance")
    st = ni.find("state") if ni is not None else None
    if st is None:
        return {}
    return {
        "name": _text(st, "name"),
        "router_id": _text(st, "router-id"),
        "type": _local(_text(st, "type")),
    }


def parse_ospf_config(ni_config_elem):
    """OSPF Area ID + DNS capability from the running-CONFIG network-instances.

    The bare <get> leaves the OSPF areas empty; the config datastore carries
    them (protocols/protocol[OSPF]/ospf/areas/area). Picks the DNS-opaque-LSA
    area (the DCN area, matching the CLI derivation) -> {area_id, dns_capable,
    opaque_lsa_cap}; falls back to the first area when none is DNS-capable."""
    out = {"area_id": None, "dns_capable": None, "opaque_lsa_cap": None}
    if ni_config_elem is None:
        return out
    first = None
    for a in ni_config_elem.findall(".//ospf/areas/area"):
        ident = _text(a, "config/identifier") or _text(a, "identifier")
        dns = (_text(a, "config/dns-opaque-lsa") or "").lower()
        if first is None:
            first = ident
        if dns == "true":
            out.update(area_id=ident, dns_capable="Yes", opaque_lsa_cap="DNS")
            return out
    if first:
        out.update(area_id=first, dns_capable="No")
    return out


# ── Power (shelf totals) + Power Filters ──────────────────────────────────────
# components/component[CARD-1-PFn]/power-supply/state/{input-current, input-voltage}
# encoded base64 IEEE float32. A down/absent feed reads ~0 V (bogus current).

def parse_power(components_elem):
    """PF voltage/current/power + shelf totals from the power-supply leaves."""
    pfs = []
    total_current = 0.0
    total_power = 0.0
    if components_elem is None:
        return {"pfs": pfs, "total_current": None, "total_power": None}
    for comp in components_elem.findall(".//component"):
        name = _text(comp, "name") or ""
        if not (name.startswith("CARD-") and "PF" in name):
            continue
        ps = comp.find("power-supply/state")
        cur = _ieee32(_text(ps, "input-current")) if ps is not None else None
        volt = _ieee32(_text(ps, "input-voltage")) if ps is not None else None
        live = (cur is not None and volt is not None and cur > 0 and volt > 1)
        power = round(volt * cur, 1) if live else None
        if live:
            total_current += cur
            total_power += power
        pfs.append({
            "name": name,
            "voltage": volt,
            "current": cur if live else None,
            "power": power,
        })
    return {
        "pfs": pfs,
        "total_current": round(total_current, 2) if total_current else None,
        "total_power": round(total_power, 1) if total_power else None,
    }


def derive_power_filters(inventory, alarms):
    """Power Filters sheet rows from CARD-1-PFn inventory + PWR alarms (a PWR
    alarm on the PF => Oper Down / FLT). No new YANG needed."""
    pwr_alarmed = {a.get("resource") for a in (alarms or []) if a.get("condition") == "PWR"}
    rows = []
    for c in inventory or []:
        name = c.get("name", "")
        if "PF" not in name:
            continue
        down = name in pwr_alarmed
        pf = name.split("-")[-1]                       # 'PF1' / 'PF4'
        digit = "".join(ch for ch in pf if ch.isdigit())
        rows.append({
            "name": name,
            "slot": f"1/4{digit}" if digit else pf,    # PF1->1/41, PF4->1/44
            "admin_state": "Up",
            "oper_state": "Down" if down else "Up",
            "qualifier": "FLT" if down else "",
        })
    return rows


# ── card-type-by-slot (mnemonic) ──────────────────────────────────────────────

def card_type_by_slot(inventory_rows, amp_rows=None):
    """Build {slot: mnemonic} from inventory part-no (PART_MNEMONIC) plus the
    amp-derived EILAL/EILA (amps know their band, which disambiguates L vs C)."""
    out = {}
    for r in inventory_rows or []:
        if r.get("slot") in (None, "--"):
            continue
        mnem = PART_MNEMONIC.get((r.get("part") or "").strip())
        if mnem:
            out[r["slot"]] = mnem
    for a in amp_rows or []:
        slot, ct = a.get("slot"), a.get("card_type")
        if slot and ct and ct not in ("--",) and slot not in out:
            out[slot] = ct
    return out


# ── Phase 4: per-node NETCONF collector ───────────────────────────────────────

def collect_node_netconf(host, username, password, port=830, timeout=120.0,
                         jump=None, connect_retries=2, source_ip=None):
    """Open ONE NETCONF session to *host*, do a single full <get>, and run every
    parser -- returning an assembled record for the Excel writer. One round-trip
    per node; namespaces stripped so parsers match by local name (a single full
    get sidesteps per-subtree namespace-filter guesswork).

    *jump* (``(seed, user, pw[, 830])``) tunnels the session through the GNE/seed
    so a DCN-only RNE is reachable from a remote PC; *connect_retries* rides out a
    brief VPN/WiFi blip; *source_ip* binds the outbound socket to a chosen local
    NIC (prefer wired/on-link over Wi-Fi). Read-only collection (the only write is
    the separate OAMP redistribute toggle)."""
    from utils.netconf import NetconfSession   # lazy: keeps parser use dep-free

    with NetconfSession(host, username, password, port=port, timeout=timeout,
                        jump=jump, connect_retries=connect_retries,
                        source_ip=source_ip) as nc:
        data = nc.get()          # full operational tree, namespaces stripped
        caps = len(nc.server_capabilities)
        # OSPF areas/DNS-capability live ONLY in the config datastore (the bare
        # <get> returns empty areas); a small get-config recovers them.
        try:
            ospf_cfg = parse_ospf_config(
                nc.get_config("running").find("network-instances"))
        except Exception:
            ospf_cfg = {}

    system = data.find("system")
    components = data.find("components")
    inv = parse_inventory(components)
    amps = parse_amplifiers(data.find("optical-amplifier"))
    ct_by_slot = card_type_by_slot(inv, amps)
    # re-run amps with the mnemonic map so card_type is authoritative
    amps = parse_amplifiers(data.find("optical-amplifier"), ct_by_slot)
    alarms = parse_alarms(system)
    connections = parse_connections(data.find("connections"))
    ospf = parse_ospf(data.find("network-instances"))
    ospf.update({k: v for k, v in (ospf_cfg or {}).items() if v})

    return {
        "host": host,
        "capabilities": caps,
        "hostname": NetconfSession.first_text(system, "hostname") if system is not None else None,
        "software_version": _text(system, "state/software-version"),
        "inventory": inv,
        "card_type_by_slot": ct_by_slot,
        "amplifiers": amps,
        "alarms": alarms,
        "connections": connections,
        "neighbors": neighbor_ips(connections),   # discovery seed set for the walk
        "transceivers": parse_transceivers(components),
        "port_power": parse_port_power(components),
        "interfaces": parse_interfaces(data.find("interfaces")),
        "ospf": ospf,
        "power": parse_power(components),
        "power_filters": derive_power_filters(inv, alarms),
    }


# ── Amplifiers ────────────────────────────────────────────────────────────────
# Nokia names amps AMPLIFIER-<shelf>-<slot>-<role>-<band>, e.g.
#   AMPLIFIER-1-2-ILA1-L  (EILAL, Line1, L-band)   AMPLIFIER-1-3-ILA2-C (EILA, Line2)
#   AMPLIFIER-1-1-RAMAN-CL (RA5PB Raman)
_AMP_NAME_RE = re.compile(
    r"AMPLIFIER-(\d+)-(\d+)-(RAMAN|ILA(\d+))(?:-([A-Za-z]+))?", re.I)
_GAIN_RANGE = {"HIGH_GAIN_RANGE": "High", "LOW_GAIN_RANGE": "Low"}
_AMP_PORT_RE = re.compile(r"-(LINE)(\d+)?(IN|OUT)$", re.I)


def _amp_port_label(*ports):
    """Amp line label from its port AID(s), tried in order. ILA line ports carry
    a digit: 'PORT-1-2-LINE1OUT' -> 'Line1'. Terminal line ports have none:
    'PORT-1-1-LINEOUT' -> 'LineOut'. Returns None when no LINE port is given
    (e.g. a pre-amp whose egress is a SIG port -> caller tries the next port)."""
    for p in ports:
        if not p:
            continue
        mm = _AMP_PORT_RE.search(p)
        if mm:
            digit, direction = mm.group(2), mm.group(3).upper()
            return f"Line{digit}" if digit else (
                "LineIn" if direction == "IN" else "LineOut")
    return None


def parse_amplifiers(oa_elem, card_type_by_slot=None):
    """Parse the ``optical-amplifier`` container into amp record dicts.

    ``card_type_by_slot`` (from the inventory parse, e.g. ``{'2': 'EILAL'}``)
    overrides the band-derived card type; without it, EDFA card type is derived
    from the amp-name band (L->EILAL, C->EILA) and Raman -> RA5PB.

    Each row: name, card_type, slot, port_label, amp_type, gain_range,
    target_gain, current_gain, actual_tilt, in_power, out_power, agc,
    gain_min, gain_max, in_spec (PASS/FAIL/'--', from the shelf's OWN
    dynamic-gain-control thresholds -- no hardcoded spec table).
    """
    rows = []
    if oa_elem is None:
        return rows
    card_type_by_slot = card_type_by_slot or {}
    for amp in oa_elem.findall(".//amplifier"):
        st = amp.find("state")
        if st is None:
            continue
        name = _text(st, "name") or _text(amp, "name") or ""
        amp_type = _text(st, "type") or ""
        m = _AMP_NAME_RE.match(name)

        # Derive slot/port from the STRUCTURAL leaves (present + consistent on
        # both ILA and terminal); the amp NAME format (ILA<n>/RAMAN) is only a
        # fallback, since terminal amps are not named ILA<n>.
        component = _text(st, "component")     # CARD-1-2  -> slot 2
        egress = _text(st, "egress-port")      # PORT-1-2-LINE1OUT
        ingress = _text(st, "ingress-port")    # PORT-1-2-LINE2IN
        cm = re.match(r"CARD-\d+-(.+)", component or "")
        slot = cm.group(1) if cm else (m.group(2) if m else None)

        is_raman = "RAMAN" in name.upper() or bool(
            m and "RAMAN" in (m.group(3) or "").upper())

        # band (for card-type fallback): amp-name suffix, else the populated
        # per-band power leaf.
        band = ((m.group(5) or "").upper() if (m and m.group(5)) else "")
        if not band:
            if _instant(st, "output-power-l-band") or _instant(st, "input-power-l-band"):
                band = "L"
            elif _instant(st, "output-power-c-band") or _instant(st, "input-power-c-band"):
                band = "C"

        if is_raman:
            port_label = "Raman"
        else:
            port_label = (_amp_port_label(egress, ingress)
                          or (f"Line{m.group(4)}" if (m and m.group(4)) else None)
                          or (m.group(3) if m else None)
                          or "--")

        # Card mnemonic: prefer the inventory-resolved value; else derive from the
        # band AND the amp PORT SHAPE, since the part-no map doesn't cover every
        # amp. ILAs (Line1/Line2 ports) run EILA/EILAL; terminals (LineIn/LineOut)
        # run IRDM32/IRDM32L -- the CLI mnemonic the telnet audit reads directly.
        card_type = card_type_by_slot.get(slot)
        if not card_type:
            terminal_amp = port_label in ("LineIn", "LineOut")
            if is_raman:
                card_type = "RA5PB"
            elif band == "L":
                card_type = "IRDM32L" if terminal_amp else "EILAL"
            elif band == "C":
                card_type = "IRDM32" if terminal_amp else "EILA"
            else:
                card_type = amp_type or "--"

        current_gain = _num(_instant(st, "actual-gain"))
        # Power leaf name varies by band: total (Raman) / c-band / l-band (EDFA).
        in_pwr = (_instant(st, "input-power-total")
                  or _instant(st, "input-power-c-band")
                  or _instant(st, "input-power-l-band"))
        out_pwr = (_instant(st, "output-power-total")
                   or _instant(st, "output-power-c-band")
                   or _instant(st, "output-power-l-band"))

        dgc = amp.find("dynamic-gain-control/state")
        gmin = _num(_text(dgc, "gain-min-alarm-threshold")) if dgc is not None else None
        gmax = _num(_text(dgc, "gain-max-alarm-threshold")) if dgc is not None else None
        in_spec = "--"
        if current_gain is not None and gmin is not None and gmax is not None:
            in_spec = "PASS" if gmin <= current_gain <= gmax else "FAIL"

        rows.append({
            "name": name,
            "card_type": card_type,
            "slot": slot,
            "port_label": port_label,
            "amp_type": amp_type,
            "gain_range": _GAIN_RANGE.get(_text(st, "gain-range"), _text(st, "gain-range")),
            "target_gain": _num(_text(st, "target-gain")),
            "current_gain": current_gain,
            "actual_tilt": _num(_instant(st, "actual-gain-tilt")),
            "in_power": _num(in_pwr),
            "out_power": _num(out_pwr),
            "agc": _local(_text(st, "amp-mode")),
            "gain_min": gmin,
            "gain_max": gmax,
            "in_spec": in_spec,
        })
    return rows

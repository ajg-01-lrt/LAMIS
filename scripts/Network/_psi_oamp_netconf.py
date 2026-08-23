"""NETCONF equivalent of the telnet ``config interface mfc 1/10/oamp routestate
<redistribute|disabled>`` used to open/close the RNE return path for the OSC
discovery walk.

Enabling redistribute adds INTERFACE-1-MFC-OAMP as a **passive** OSPFv2 interface
under area 0.0.0.0 (advertise-only, no adjacency); disabling deletes it. This
module performs those two edits over NETCONF <edit-config> instead of the fragile
telnet getty, so the routed-seed audit has no telnet dependency at all.

SAFETY: the edit-config payload is built by CLONING the device's own namespaced
XML (a sibling OSPF interface) at run time -- no namespaces or paths are
hardcoded, so it round-trips exactly what the box expects. Every change is
verified by read-back, and prepare() returns a restore token the caller must use
to revert (this module never leaves the box changed without handing back a
revert path).
"""
from __future__ import annotations

import copy

from lxml import etree

OAMP_IFACE = "INTERFACE-1-MFC-OAMP"
_OSPF_AREA = "0.0.0.0"
_NC_BASE = "urn:ietf:params:xml:ns:netconf:base:1.0"


# ── namespace-agnostic navigation (works on raw or stripped trees) ────────────

def _ln(el):
    return etree.QName(el).localname if isinstance(el.tag, str) else ""


def _child(parent, name):
    for c in parent:
        if _ln(c) == name:
            return c
    return None


def _child_text(parent, name):
    c = _child(parent, name)
    return c.text.strip() if c is not None and c.text else None


def find_ospf_v2_area(ni_data, area_id=_OSPF_AREA):
    """Locate the OSPFv2 area (default 0.0.0.0) element in a network-instances
    tree. Returns the <area> element or None. Matches by local name so it works
    on both the raw (namespaced) and ns-stripped trees."""
    if ni_data is None:
        return None
    for prot in (e for e in ni_data.iter() if _ln(e) == "protocol"):
        if _child_text(prot, "name") != "OSPF":     # skip OSPF3 / others
            continue
        ospf = _child(prot, "ospf")                  # v2 container (not ospfv3)
        areas = _child(ospf, "areas") if ospf is not None else None
        if areas is None:
            continue
        for area in areas:
            if _ln(area) == "area" and _child_text(area, "identifier") == area_id:
                return area
    return None


def _area_interfaces(area):
    return _child(area, "interfaces") if area is not None else None


def oamp_interface(area):
    """The <interface> element for the OAMP under *area*, or None."""
    ifs = _area_interfaces(area)
    if ifs is None:
        return None
    for itf in ifs:
        if _ln(itf) == "interface" and _child_text(itf, "id") == OAMP_IFACE:
            return itf
    return None


# ── edit-config payload construction (clone the device's own XML) ─────────────

def _spine(ni_data, area, target_interface):
    """Rebuild the minimal keyed path
    network-instances/network-instance/protocols/protocol/ospf/areas/area/
    interfaces/<target_interface>, cloning each container's tag + key leaves from
    the live tree so element namespaces + identityref prefix declarations are the
    device's own. Returns the <network-instances> payload element."""
    nis = next((e for e in ni_data.iter() if _ln(e) == "network-instances"), None)
    ni = next((e for e in ni_data.iter() if _ln(e) == "network-instance"), None)
    # walk up from the area: area <- areas <- ospf <- protocol <- protocols
    areas = area.getparent()
    ospf = areas.getparent()
    prot = ospf.getparent()
    protocols = prot.getparent()

    # Declare every in-scope prefix (incl. identityref prefixes like
    # oc-pol-types) on the payload root, taken from the deepest live element.
    nsmap = dict(area.nsmap)
    nsmap["nc"] = _NC_BASE

    def clone_keys(src, keys):
        el = etree.Element(src.tag, nsmap=src.nsmap)
        for c in src:
            if _ln(c) in keys:
                el.append(copy.deepcopy(c))
        return el

    p_nis = etree.Element(nis.tag, nsmap=nsmap)
    p_ni = clone_keys(ni, {"name"});           p_nis.append(p_ni)
    p_protocols = etree.SubElement(p_ni, protocols.tag)
    p_prot = clone_keys(prot, {"identifier", "name"}); p_protocols.append(p_prot)
    p_ospf = etree.SubElement(p_prot, ospf.tag)
    p_areas = etree.SubElement(p_ospf, areas.tag)
    p_area = clone_keys(area, {"identifier"});  p_areas.append(p_area)
    p_ifs = etree.SubElement(p_area, _area_interfaces(area).tag)
    p_ifs.append(target_interface)
    return p_nis


def build_enable_config(ni_data, area):
    """Return the <config> inner XML (str) that ADDS the passive OAMP OSPF
    interface, cloning a sibling interface so the structure/namespaces match the
    device exactly (id + config/id -> OAMP, passive -> true)."""
    ifs = _area_interfaces(area)
    sibling = next((c for c in ifs if _ln(c) == "interface"), None)
    if sibling is None:
        raise RuntimeError("no sibling OSPF interface to clone from")
    new_if = copy.deepcopy(sibling)
    for idc in (c for c in new_if.iter() if _ln(c) == "id"):
        idc.text = OAMP_IFACE
    cfg = _child(new_if, "config")
    if cfg is not None:
        passive = _child(cfg, "passive")
        if passive is None:                       # sibling always has it, but be safe
            ns = etree.QName(cfg).namespace
            passive = etree.SubElement(
                cfg, "{%s}passive" % ns if ns else "passive")
        passive.text = "true"
    new_if.set("{%s}operation" % _NC_BASE, "merge")
    return etree.tostring(_spine(ni_data, area, new_if)).decode()


def build_disable_config(ni_data, area):
    """Return the <config> inner XML (str) that DELETES the OAMP OSPF interface
    (keyed by <id>, netconf operation=delete)."""
    itf = oamp_interface(area)
    if itf is None:
        raise RuntimeError("OAMP OSPF interface not present to delete")
    del_if = etree.Element(itf.tag, nsmap=itf.nsmap)
    idc = _child(itf, "id")
    del_if.append(copy.deepcopy(idc))
    del_if.set("{%s}operation" % _NC_BASE, "delete")
    return etree.tostring(_spine(ni_data, area, del_if)).decode()


# ── high-level prepare / restore (used by the audit + the test tool) ──────────

def _read_area(nc, area_id=_OSPF_AREA):
    """Raw (namespaced) network-instances <data> + the OSPFv2 area element."""
    ni = nc.get_config("running", strip=False).find("{*}network-instances")
    if ni is None:
        # some servers wrap differently; fall back to a local-name search
        root = nc.get_config("running", strip=False)
        ni = next((e for e in root.iter() if _ln(e) == "network-instances"), None)
    area = find_ospf_v2_area(ni, area_id) if ni is not None else None
    return ni, area


def is_redistributing(nc):
    """True if the OAMP is already a passive OSPF interface (redistribute on)."""
    _, area = _read_area(nc)
    return oamp_interface(area) is not None


def prepare(nc):
    """Ensure OAMP redistribute is ON over NETCONF. Returns ``(ready, restore)``
    where *restore* is 'disable' only if THIS call enabled it (so teardown
    reverts only what we changed), else None. Verifies by read-back."""
    ni, area = _read_area(nc)
    if area is None:
        return False, None
    if oamp_interface(area) is not None:
        return True, None                    # already advertising -- leave it
    nc.edit_config(build_enable_config(ni, area))
    if not is_redistributing(nc):
        return False, None                   # write didn't take -> not ready
    return True, "disable"


def restore(nc, restore_state):
    """Revert what prepare() changed. ``restore_state`` == 'disable' removes the
    OAMP OSPF interface; anything else is a no-op. Verifies by read-back."""
    if restore_state != "disable":
        return True
    ni, area = _read_area(nc)
    if area is None or oamp_interface(area) is None:
        return True                          # already gone
    nc.edit_config(build_disable_config(ni, area))
    return not is_redistributing(nc)


def oamp_prefix(nc, default=24):
    """OAMP interface IPv4 prefix-length (for the PC multi-net), from config."""
    try:
        data = nc.get_config("running")
    except Exception:
        return default
    for itf in data.iter():
        if _ln(itf) == "interface" and _child_text(itf, "name") == OAMP_IFACE:
            for pl in itf.iter():
                t = (pl.text or "").strip()
                if _ln(pl) == "prefix-length" and t.isdigit():
                    return int(t)
    return default


# ── session-managed wrappers (what the audit calls) ───────────────────────────

def prepare_over_netconf(host, username, password, port=830, timeout=60.0):
    """Open a NETCONF session, ensure OAMP redistribute is ON, and read the OAMP
    subnet prefix. Returns ``(ready, restore_state, prefix)``; restore_state is
    'disable' only when THIS call enabled it (else None -> left as found)."""
    from utils.netconf import NetconfSession, NetconfError
    try:
        with NetconfSession(host, username, password, port=port, timeout=timeout) as nc:
            prefix = oamp_prefix(nc)
            ready, restore_state = prepare(nc)
            return ready, restore_state, prefix
    except NetconfError:
        return False, None, 24


def restore_over_netconf(host, username, password, restore_state,
                         port=830, timeout=60.0):
    """Open a NETCONF session and revert what prepare_over_netconf enabled."""
    from utils.netconf import NetconfSession, NetconfError
    if not restore_state:
        return True
    try:
        with NetconfSession(host, username, password, port=port, timeout=timeout) as nc:
            return restore(nc, restore_state)
    except NetconfError:
        return False


def oamp_prefix_over_netconf(host, username, password, port=830, timeout=30.0):
    """Read-only: OAMP interface IPv4 prefix-length over NETCONF (default 24 on
    any failure). Used to size the PC's temp multi-net BEFORE any config write, so
    the on-site adjacency probe can run without touching the seed."""
    from utils.netconf import NetconfSession, NetconfError
    try:
        with NetconfSession(host, username, password, port=port, timeout=timeout) as nc:
            return oamp_prefix(nc)
    except (NetconfError, Exception):
        return 24

"""Pod / Lab subnet resolution for the Inventory frame.

Pods 1..13 live on 172.21.1xx.x with the operator supplying only the fourth octet.
"Lab" switches to 10.9.x.x and the operator supplies the third octet as well, so an
incomplete Lab selection must resolve to None rather than silently scanning 10.9.0.x.
"""
from __future__ import annotations

import pytest

import config
from gui.inventory_frame import InventoryFrame

resolve = InventoryFrame._subnet_prefix_for


# ── pods ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "pod_number, expected_third_octet",
    [(1, 101), (2, 102), (7, 107), (13, 113)],
)
def test_pod_maps_to_expected_third_octet(pod_number, expected_third_octet):
    assert resolve(f"Pod {pod_number}", "") == f"172.21.{expected_third_octet}"


def test_every_pod_option_resolves_to_a_full_prefix():
    for pod_number in range(1, config.POD_COUNT + 1):
        prefix = resolve(f"Pod {pod_number}", "")
        assert prefix.count(".") == 2, f"Pod {pod_number} did not resolve to a /24 prefix"
        assert prefix.startswith(config.POD_NETWORK_PREFIX + ".")


def test_pod_count_covers_thirteen_pods():
    assert config.POD_COUNT == 13
    assert resolve("Pod 13", "") == "172.21.113"


def test_pod_number_beyond_range_does_not_resolve():
    # 14 is not a real pod; must not silently become 172.21.114
    assert resolve(f"Pod {config.POD_COUNT + 1}", "") == config.POD_NETWORK_PREFIX


def test_pod_selection_ignores_a_stray_lab_octet():
    # A leftover Lab octet must not leak into a pod range.
    assert resolve("Pod 4", "77") == "172.21.104"


def test_pod_label_whitespace_is_tolerated():
    assert resolve("  Pod 3  ", "") == "172.21.103"
    assert resolve("Pod3", "") == "172.21.103"


# ── lab ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("octet", ["0", "5", "9", "102", "255"])
def test_lab_uses_the_lab_prefix_and_operator_octet(octet):
    assert resolve(config.LAB_LABEL, octet) == f"10.9.{int(octet)}"


@pytest.mark.parametrize("octet", ["", "   ", "abc", "256", "-1", "1.2"])
def test_lab_without_a_valid_octet_is_incomplete(octet):
    # Deliberately returns the bare two-octet prefix so the label can still render;
    # subnet_prefix() turns that into None and the run is rejected.
    assert resolve(config.LAB_LABEL, octet) == config.LAB_NETWORK_PREFIX


def test_lab_and_pod_ranges_are_distinct_networks():
    assert config.LAB_NETWORK_PREFIX != config.POD_NETWORK_PREFIX
    assert resolve(config.LAB_LABEL, "101") != resolve("Pod 1", "")


# ── legacy input ────────────────────────────────────────────────────────────
def test_raw_third_octet_still_resolves():
    """A directly typed third octet (older saved input) keeps working."""
    assert resolve("105", "") == "172.21.105"


def test_raw_third_octet_outside_the_pod_span_does_not_resolve():
    assert resolve("50", "") == config.POD_NETWORK_PREFIX


def test_unknown_selection_does_not_produce_a_scannable_prefix():
    for junk in ["", "Pod", "Lab ", "???"]:
        prefix = resolve(junk, "")
        assert prefix.count(".") < 2, f"{junk!r} must not resolve to a scannable /24"


# ── octet parsing ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("text, expected", [("0", 0), ("7", 7), ("255", 255), (" 42 ", 42)])
def test_octet_accepts_valid_values(text, expected):
    assert InventoryFrame._octet(text) == expected


@pytest.mark.parametrize("text", ["", "   ", "256", "-1", "1.2", "abc", None])
def test_octet_rejects_invalid_values(text):
    assert InventoryFrame._octet(text) is None


# ── end-to-end range resolution through the real widgets ────────────────────
@pytest.fixture(scope="module")
def tk_root():
    """One Tk root for the whole module.

    Creating and destroying a root per test is flaky — repeated interpreters
    intermittently report "Tk display unavailable" — so the root is shared and only
    the frame under test is rebuilt each time.
    """
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:  # pragma: no cover - no display available
        pytest.skip("Tk display unavailable")
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def frame(tk_root):
    from unittest.mock import MagicMock
    widget = InventoryFrame(tk_root, controller=MagicMock())
    yield widget
    widget.destroy()


def _enter(frame, which, start_host, end_host, start_oct3="", end_oct3=""):
    w = frame._selector_widgets(which)
    w["start_entry"].delete(0, "end")
    w["end_entry"].delete(0, "end")
    w["start_entry"].insert(0, start_host)
    w["end_entry"].insert(0, end_host)
    w["start_oct3"].set(start_oct3)
    w["end_oct3"].set(end_oct3)


def test_pod_range_reads_only_the_fourth_octet(frame):
    frame.pod_var_1.set("Pod 3")
    _enter(frame, 1, "10", "20")
    assert frame.resolve_range(1) == ("172.21.103.10", "172.21.103.20", None)


def test_pod_range_ignores_leftover_lab_octets(frame):
    frame.pod_var_1.set(config.LAB_LABEL)
    _enter(frame, 1, "10", "20", "88", "99")
    frame.pod_var_1.set("Pod 3")
    assert frame.resolve_range(1) == ("172.21.103.10", "172.21.103.20", None)


def test_lab_range_within_one_subnet(frame):
    frame.pod_var_1.set(config.LAB_LABEL)
    _enter(frame, 1, "10", "20", "5", "5")
    assert frame.resolve_range(1) == ("10.9.5.10", "10.9.5.20", None)


def test_lab_range_may_span_subnets(frame):
    frame.pod_var_1.set(config.LAB_LABEL)
    _enter(frame, 1, "10", "20", "5", "7")
    assert frame.resolve_range(1) == ("10.9.5.10", "10.9.7.20", None)


def test_lab_without_third_octets_is_an_error(frame):
    frame.pod_var_1.set(config.LAB_LABEL)
    _enter(frame, 1, "10", "20")
    start, end, error = frame.resolve_range(1)
    assert (start, end) == (None, None)
    assert "third octet" in error


def test_reversed_range_is_rejected(frame):
    frame.pod_var_1.set(config.LAB_LABEL)
    _enter(frame, 1, "10", "20", "9", "5")
    start, end, error = frame.resolve_range(1)
    assert (start, end) == (None, None)
    assert "less than or equal" in error


def test_reversed_pod_range_is_rejected(frame):
    frame.pod_var_1.set("Pod 2")
    _enter(frame, 1, "50", "10")
    assert frame.resolve_range(1)[2] is not None


def test_blank_selector_is_not_an_error(frame):
    # Selector 2 is optional; blank must resolve to "nothing supplied", not a failure.
    assert frame.resolve_range(2) == (None, None, None)


def test_half_filled_selector_is_an_error(frame):
    _enter(frame, 2, "10", "")
    start, end, error = frame.resolve_range(2)
    assert (start, end) == (None, None)
    assert "start and an end" in error


def test_out_of_range_host_octet_is_rejected(frame):
    frame.pod_var_1.set("Pod 1")
    _enter(frame, 1, "10", "300")
    assert frame.resolve_range(1)[2] is not None


def test_lab_toggle_shows_and_hides_the_third_octet(frame):
    w = frame._selector_widgets(1)
    frame.pod_var_1.set("Pod 1")
    assert not w["start_oct3_entry"].winfo_manager()
    frame.pod_var_1.set(config.LAB_LABEL)
    assert w["start_oct3_entry"].winfo_manager()
    assert w["end_oct3_entry"].winfo_manager()
    frame.pod_var_1.set("Pod 4")
    assert not w["start_oct3_entry"].winfo_manager()


def test_labels_track_the_selection(frame):
    frame.pod_var_1.set("Pod 9")
    assert frame.start_ip_label_1.cget("text") == "Start IP: 172.21.109."
    frame.pod_var_1.set(config.LAB_LABEL)
    assert frame.start_ip_label_1.cget("text") == f"Start IP: {config.LAB_NETWORK_PREFIX}."
    assert frame.end_ip_label_1.cget("text") == f"End IP: {config.LAB_NETWORK_PREFIX}."


def test_selectors_are_independent(frame):
    frame.pod_var_1.set(config.LAB_LABEL)
    frame.pod_var_2.set("Pod 11")
    _enter(frame, 1, "1", "2", "3", "3")
    _enter(frame, 2, "4", "5")
    assert frame.resolve_range(1) == ("10.9.3.1", "10.9.3.2", None)
    assert frame.resolve_range(2) == ("172.21.111.4", "172.21.111.5", None)

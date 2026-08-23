"""Headless integration contracts for the RLS R4.0 configuration UI."""

from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from gui import rls_r4_0_config_frame as module
from utils.rls_config.common import (
    DEFAULT_R40_TARGET_BUILD_SCHEMA,
    SUPPORTED_SOFTWARE_RELEASE,
)
from utils.rls_config.r4_0_generator import (
    R40_CL_ROADM_RLA12_LRU12_1DEG_SRA6,
    R40_PROVIDER_CATALOG,
    R40_R2_CL_DLE_S1_SRA4,
    SUPPORTED_RELEASE,
    provider_profiles_for_role,
)


_ROOT = Path(__file__).resolve().parents[1]


def _source(relative_path: str) -> str:
    return (_ROOT / relative_path).read_text(encoding="utf-8")


def _tree(relative_path: str) -> ast.Module:
    return ast.parse(_source(relative_path), filename=relative_path)


def _class(tree: ast.Module, name: str) -> ast.ClassDef:
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == name
    )


def _method(class_node: ast.ClassDef, name: str) -> ast.FunctionDef:
    return next(
        node
        for node in class_node.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _from_imports(tree: ast.Module) -> set[tuple[str, str]]:
    return {
        (node.module or "", alias.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }


def _assigns_self_attribute(
    method: ast.FunctionDef,
    attribute: str,
    constructor: str,
) -> bool:
    for node in ast.walk(method):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        if _call_name(node.value) != constructor:
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
                and target.attr == attribute
            ):
                return True
    return False


class _Variable:
    def __init__(self, value: object = "") -> None:
        self.value = value

    def get(self) -> object:
        return self.value

    def set(self, value: object) -> None:
        self.value = value


def test_gui4_0_wires_provision_mode_through_wrapper() -> None:
    tree = _tree("gui/gui4_0.py")
    assert ("gui.provisioning_frame", "ProvisioningFrame") in _from_imports(tree)

    setup_gui = _method(_class(tree, "InventoryGUI"), "setup_gui")
    assert _assigns_self_attribute(
        setup_gui,
        attribute="provision_frame",
        constructor="ProvisioningFrame",
    )


def test_integrated_review_filters_project_incompatible_provider_options() -> None:
    roadm_profiles = provider_profiles_for_role("roadm_a")
    assert roadm_profiles
    allowed_id = roadm_profiles[0].provider_id

    assert module._provider_profiles_for_route_seed(
        "roadm_a",
        {
            "provider_resolution": {
                "review_provider_ids": [],
            }
        },
    ) == ()
    assert module._provider_profiles_for_route_seed(
        "roadm_a",
        {
            "provider_resolution": {
                "review_provider_ids": [allowed_id],
            }
        },
    ) == (roadm_profiles[0],)
    assert module._provider_profiles_for_route_seed(
        "roadm_a",
        {"provider_resolution": {}},
    ) == roadm_profiles


def test_sole_route_compatible_provider_populates_review_dropdown() -> None:
    profiles = provider_profiles_for_role("ila")
    assert {profile.supports_raman for profile in profiles} == {False, True}
    candidate = next(
        profile for profile in profiles if not profile.supports_raman
    )
    route_seed = {
        "provider_resolution": {
            "status": "unique_candidate",
            "provider_id": candidate.provider_id,
            "review_provider_ids": [candidate.provider_id],
            "preselect_allowed": False,
            "reason_codes": [
                "UNIQUE_ROUTE_SCOPE_COMPATIBLE_PROVIDER",
                "INSUFFICIENT_PROVIDER_IDENTITY_EVIDENCE",
            ],
        }
    }

    compatible_profiles = module._provider_profiles_for_route_seed(
        "ila",
        route_seed,
    )
    selected, evidence_class = module._initial_route_provider_choice(
        compatible_profiles,
        route_seed,
    )

    assert selected is candidate
    assert evidence_class == "route_candidate"
    assert module._provider_profiles_for_route_seed(
        "ila",
        route_seed,
    ) == (candidate,)
    context = module._provider_resolution_context_text(route_seed)
    assert "sole route-compatible review candidate populated" in context
    assert "direct hardware identity remains unproved" in context


def test_direct_hardware_provider_preselection_remains_distinct() -> None:
    profiles = provider_profiles_for_role("ila")
    candidate = profiles[0]

    selected, evidence_class = module._initial_route_provider_choice(
        profiles,
        {
            "provider_resolution": {
                "status": "exact_match",
                "provider_id": candidate.provider_id,
                "review_provider_ids": [candidate.provider_id],
                "preselect_allowed": True,
                "reason_codes": ["EXACT_DIRECT_DISCRIMINATORS_MATCH"],
            }
        },
    )

    assert selected is candidate
    assert evidence_class == "direct_hardware"
    context = module._provider_resolution_context_text(
        {
            "provider_resolution": {
                "status": "exact_match",
                "provider_id": candidate.provider_id,
                "review_provider_ids": [candidate.provider_id],
                "preselect_allowed": True,
            },
        }
    )
    assert "direct-hardware-qualified suggestion populated" in context


@pytest.mark.parametrize("raman_status", ["pending", "invalid", "accepted"])
def test_initial_provider_choice_fails_closed_for_unresolved_or_unsupported_raman(
    raman_status: str,
) -> None:
    profiles = provider_profiles_for_role("ila")
    candidate = profiles[0]
    assert candidate.supports_raman is False

    assert module._initial_route_provider_choice(
        profiles,
        {
            "provider_resolution": {
                "status": "unique_candidate",
                "provider_id": candidate.provider_id,
                "review_provider_ids": [candidate.provider_id],
                "preselect_allowed": False,
                "raman_callout_review_status": raman_status,
            },
        },
    ) == (None, "")


@pytest.mark.parametrize(
    "resolution",
    [
        {
            "status": "conflict",
            "provider_id": "candidate",
            "review_provider_ids": ["candidate"],
        },
        {
            "status": "ambiguous",
            "provider_id": "candidate",
            "review_provider_ids": ["candidate"],
        },
        {
            "status": "unsupported_sra",
            "provider_id": "candidate",
            "review_provider_ids": ["candidate"],
        },
        {
            "status": "unique_candidate",
            "provider_id": "",
            "review_provider_ids": ["candidate"],
        },
        {
            "status": "unique_candidate",
            "provider_id": "candidate",
            "review_provider_ids": [],
        },
        {
            "status": "unique_candidate",
            "provider_id": "candidate",
            "review_provider_ids": ["candidate", "other"],
        },
        {
            "status": "unique_candidate",
            "provider_id": "candidate",
            "review_provider_ids": ["other"],
        },
        {
            "status": "unique_candidate",
            "provider_id": "candidate",
            "review_provider_ids": ["candidate"],
            "reason_codes": ["R40_SRA_PROVIDER_UNAVAILABLE"],
        },
    ],
)
def test_initial_provider_choice_rejects_unsafe_or_unresolved_resolution(
    resolution: dict[str, object],
) -> None:
    catalog_profile = provider_profiles_for_role("ila")[0]
    profiles = (catalog_profile,)
    normalized = {
        key: (
            catalog_profile.provider_id
            if value == "candidate"
            else [
                (
                    catalog_profile.provider_id
                    if item == "candidate"
                    else item
                )
                for item in value
            ]
            if isinstance(value, list)
            else value
        )
        for key, value in resolution.items()
    }

    assert module._initial_route_provider_choice(
        profiles,
        {"provider_resolution": normalized},
    ) == (None, "")


def test_ambiguous_provider_options_remain_in_dropdown_without_selection() -> None:
    profiles = provider_profiles_for_role("roadm")
    assert len(profiles) > 1
    provider_ids = [profile.provider_id for profile in profiles]
    route_seed = {
        "provider_resolution": {
            "status": "ambiguous",
            "provider_id": "",
            "review_provider_ids": provider_ids,
            "preselect_allowed": False,
            "reason_codes": ["MULTIPLE_COMPATIBLE_PROVIDERS"],
        }
    }

    assert module._provider_profiles_for_route_seed(
        "roadm",
        route_seed,
    ) == profiles
    assert module._initial_route_provider_choice(
        profiles,
        route_seed,
    ) == (None, "")


def test_wrapper_exposes_route_builder_as_the_only_offline_rls_mode() -> None:
    tree = _tree("gui/provisioning_frame.py")
    imports = _from_imports(tree)
    assert ("gui.provision_frame", "ProvisionFrame") in imports
    assert ("gui.rls_route_frame", "RlsRouteFrame") in imports
    assert all("rls_config_frame" not in imported for imported, _name in imports)

    build = _method(_class(tree, "ProvisioningFrame"), "_build")
    assert _assigns_self_attribute(build, "live_child", "ProvisionFrame")
    assert _assigns_self_attribute(build, "rls_route_child", "RlsRouteFrame")

    wrapper = importlib.import_module("gui.provisioning_frame")
    route = importlib.import_module("gui.rls_route_frame")
    assert wrapper.RlsRouteFrame is route.RlsRouteFrame
    assert not hasattr(wrapper, "RlsConfigFrame")


def test_r40_editor_has_no_retired_release_dependencies() -> None:
    source = inspect.getsource(module)
    assert "R4.2" not in source
    assert "protected_dci" not in source
    assert "legacy_generator" not in source
    assert "export_artifact" not in source
    assert "rls_config.generator" not in source
    assert "rls_config.common" in source
    assert "Workbook Fields" in source
    assert "legacy_workbook_contract_text" in source
    assert "preselect one audited provider" in source
    assert "legacy B5" in source


def test_exact_editor_replaces_manual_confirmation_tab_with_control_notice() -> None:
    build_source = inspect.getsource(module.RlsR40ConfigFrame._build)
    class_source = inspect.getsource(module.RlsR40ConfigFrame)

    assert "_confirm_tab" not in class_source
    assert "_build_confirmations" not in class_source
    assert "ttk.Checkbutton" not in class_source
    assert "Provider-specific deployment controls are included" in build_source
    assert "They are not verified observations" in build_source


def test_active_release_is_one_canonical_constant() -> None:
    assert SUPPORTED_SOFTWARE_RELEASE == SUPPORTED_RELEASE == "RLS R4.0"
    assert {
        provider_id
        for provider_id, profile in R40_PROVIDER_CATALOG.items()
        if profile.supports_raman
    } == {
        R40_CL_ROADM_RLA12_LRU12_1DEG_SRA6,
        R40_R2_CL_DLE_S1_SRA4,
    }
    assert sum(
        not profile.supports_raman
        for profile in R40_PROVIDER_CATALOG.values()
    ) == 4


@pytest.mark.parametrize(
    ("role", "expected_sra_states"),
    [
        ("add_drop_a", (False,)),
        ("add_drop_z", (False,)),
        ("add_drop", (False,)),
        ("roadm_a", (False, False, True)),
        ("roadm_z", (False, False, True)),
        ("roadm", (False, False, True)),
        ("ila", (False, True)),
    ],
)
def test_editor_provider_choices_are_role_scoped(
    role: str,
    expected_sra_states: tuple[bool, ...],
) -> None:
    profiles = provider_profiles_for_role(role)
    assert tuple(
        sorted(profile.supports_raman for profile in profiles)
    ) == expected_sra_states
    assert all(role in profile.role_profiles for profile in profiles)


def test_editor_requires_explicit_provider_selection() -> None:
    subject = SimpleNamespace(_selected_profile=lambda: None)
    with pytest.raises(ValueError, match="Explicitly choose"):
        module.RlsR40ConfigFrame._build_request(subject)


def test_provider_fixed_link_names_replace_repeated_route_circuit_ids() -> None:
    def line_widgets(value: str) -> module._LineWidgets:
        return module._LineWidgets(
            **{
                field_name: _Variable(value if field_name == "link_name" else "")
                for field_name in module._LineWidgets.__dataclass_fields__
            }
        )

    provider = next(
        profile
        for profile in R40_PROVIDER_CATALOG.values()
        if profile.application == "ila_dle_cl"
    )
    first = line_widgets("BDJW7353")
    second = line_widgets("BDJW7353")
    subject = SimpleNamespace(
        _line_1=first,
        _line_2=second,
        _route_lines_by_side={
            "A": {"link_name": "BDJW7353", "circuit_id": "BDJW7353"},
            "Z": {"link_name": "BDJW7353", "circuit_id": "BDJW7353"},
        },
    )

    changed = module.RlsR40ConfigFrame._apply_provider_link_name_defaults(
        subject,
        provider,
    )

    assert changed == 2
    assert first.link_name.get() == "PFG-1-2-LINEOUT"
    assert second.link_name.get() == "PFG-2-1-LINEOUT"

    first.link_name.set("OPERATOR-LOCAL-LINK")
    module.RlsR40ConfigFrame._apply_provider_link_name_defaults(
        subject,
        provider,
    )
    assert first.link_name.get() == "OPERATOR-LOCAL-LINK"


def test_one_degree_provider_hides_phantom_second_degree() -> None:
    class _Notebook:
        def __init__(self) -> None:
            self.hidden: object | None = None
            self.state = ""

        def hide(self, tab: object) -> None:
            self.hidden = tab

        def tab(self, tab: object, **kwargs) -> None:
            assert tab is self.hidden
            self.state = str(kwargs.get("state", ""))

    class _Button:
        def __init__(self) -> None:
            self.state = ""

        def config(self, **kwargs) -> None:
            self.state = str(kwargs["state"])

    provider = next(
        profile
        for profile in R40_PROVIDER_CATALOG.values()
        if profile.application == "roadm_rla12_cl_lru12_core"
    )
    notebook = _Notebook()
    button = _Button()
    tab = object()
    subject = SimpleNamespace(
        _notebook=notebook,
        _line2_tab=tab,
        _swap_direction_button=button,
        _profiles=(provider,),
    )

    module.RlsR40ConfigFrame._set_line_cardinality(subject, provider)

    assert notebook.hidden is tab
    assert button.state == "disabled"


def test_editor_requires_explicit_fixed_direction_route_side() -> None:
    subject = SimpleNamespace(
        _selected_profile=lambda: SimpleNamespace(
            line_semantics="bidirectional_degree"
        ),
        _line_1_route_side_var=_Variable(""),
    )

    with pytest.raises(ValueError, match="first fixed physical degree 1"):
        module.RlsR40ConfigFrame._build_request(subject)


def test_request_keeps_automatic_control_observations_false() -> None:
    provider = next(
        profile
        for profile in provider_profiles_for_role("roadm_a")
        if len(profile.line_outputs) == 1
    )
    line = module.R40LinePath(
        link_name="LM1-LINEOUT",
        neighbor_node="USQTN1-L8I2",
        neighbor_line_mux_pfg="LM1",
        neighbor_line_demux_pfg="LD1",
        fiber_type="LEAF",
        expected_loss_db=14.55,
    )
    subject = SimpleNamespace(
        _selected_profile=lambda: provider,
        _role_profile="roadm_a",
        _line_1_route_side_var=_Variable(module._ROUTE_SIDE_LABELS["Z"]),
        _target_build_var=_Variable("R4.0.0"),
        _shelf_name_var=_Variable("USELP1-L8R2"),
        _shelf_label_var=_Variable("El Paso, TX"),
        _site_name_var=_Variable("El Paso, TX"),
        _site_id_var=_Variable("1"),
        _site_description_var=_Variable("ELP1-SAT4"),
        _site_address_var=_Variable(""),
        _member_name_var=_Variable("USELP1-L8R2"),
        _hostname_var=_Variable("USELP1-L8R2"),
        _frame_id_var=_Variable("FRAME-1"),
        _bay_var=_Variable("1"),
        _physical_shelf_var=_Variable("1"),
        _loopback_var=_Variable("10.6.22.129"),
        _ospf_area_var=_Variable("10.6.8.0"),
        _line_1=object(),
        _line_2=object(),
        _required_int=lambda raw, _label: int(raw),
        _management_value=lambda _profile: module.ManagementInterface(),
        _line_value=lambda _widgets, _number: line,
    )

    request = module.RlsR40ConfigFrame._build_request(subject)

    assert request.installed_inventory_confirmed is False
    assert request.planner_runtime_mop_confirmed is False
    assert request.target_build_confirmed is False
    assert request.greenfield_fibers_disconnected_confirmed is False
    assert request.calibration_feature_inactive_confirmed is False
    assert request.cfim_unused_ports_terminated_confirmed is False


@pytest.mark.parametrize(
    ("management", "expected_colan_state"),
    (
        (
            module.ManagementInterface(
                enabled=True,
                name="colan-x",
                routing_mode=module.ROUTING_OSPF_GNE,
                ip_address="192.0.2.2",
                prefix_length=30,
            ),
            module._COLAN_REVIEW_CONFIGURED,
        ),
        (
            module.ManagementInterface(
                enabled=False,
                name="",
                routing_mode=module.ROUTING_OSPF_OSC_ONLY,
                ip_address="",
                prefix_length=None,
                gateway="",
            ),
            module._COLAN_REVIEW_DEFERRED,
        ),
    ),
    ids=("configured-colan", "deferred-colan"),
)
def test_stored_request_with_legacy_confirmations_and_colan_state_loads(
    management: module.ManagementInterface,
    expected_colan_state: str,
) -> None:
    provider = next(
        profile
        for profile in provider_profiles_for_role("roadm_a")
        if len(profile.line_outputs) == 1
    )
    line = module.R40LinePath(
        link_name="LM1-LINEOUT",
        neighbor_node="USQTN1-L8I2",
        neighbor_line_mux_pfg="LM1",
        neighbor_line_demux_pfg="LD1",
        fiber_type="LEAF",
        expected_loss_db=14.55,
    )
    request = module.R40ExactRequest(
        provider_id=provider.provider_id,
        profile="roadm_a",
        software_release=module.SUPPORTED_RELEASE,
        target_software_build="R4.0.0",
        chassis_family=provider.chassis_family,
        chassis_pec=provider.chassis_pec,
        hardware_profile=provider.hardware_profile,
        shelf_name="USELP1-L8R2",
        site_name="El Paso, TX",
        member_name="USELP1-L8R2",
        hostname="USELP1-L8R2",
        frame_identification_code="FRAME-1",
        loopback_ip="10.6.22.129",
        ospf_area="10.6.8.0",
        management=management,
        line_1=line,
        line_2=None,
        line_1_route_side="Z",
        installed_inventory_confirmed=True,
        planner_runtime_mop_confirmed=True,
        target_build_confirmed=True,
        greenfield_fibers_disconnected_confirmed=True,
        calibration_feature_inactive_confirmed=True,
        cfim_unused_ports_terminated_confirmed=True,
    )

    def line_widgets() -> module._LineWidgets:
        return module._LineWidgets(
            **{
                field_name: _Variable()
                for field_name in module._LineWidgets.__dataclass_fields__
            }
        )

    variable_names = (
        "_provider_var",
        "_line_1_route_side_var",
        "_target_build_var",
        "_shelf_name_var",
        "_shelf_label_var",
        "_site_name_var",
        "_site_id_var",
        "_site_description_var",
        "_site_address_var",
        "_member_name_var",
        "_hostname_var",
        "_frame_id_var",
        "_bay_var",
        "_physical_shelf_var",
        "_loopback_var",
        "_ospf_area_var",
        "_colan_state_var",
        "_routing_var",
        "_management_name_var",
        "_management_ip_var",
        "_prefix_var",
        "_gateway_var",
        "_ospf_metric_var",
        "_static_metric_var",
        "_status_var",
    )
    subject = SimpleNamespace(
        **{name: _Variable() for name in variable_names},
        _profiles=(provider,),
        _loading=False,
        _line_1=line_widgets(),
        _line_2=line_widgets(),
        _refresh_provider=lambda: None,
        _invalidate=lambda: None,
    )
    subject._load_line = (
        lambda widgets, value: module.RlsR40ConfigFrame._load_line(
            widgets,
            value,
        )
    )
    subject._load_line_seed = (
        lambda widgets, raw: module.RlsR40ConfigFrame._load_line_seed(
            widgets,
            raw,
        )
    )

    module.RlsR40ConfigFrame.load_request(subject, request)

    assert subject._provider_var.get() == provider.display_name
    assert subject._status_var.get() == (
        "Loaded stored exact request — validate before applying."
    )
    assert subject._colan_state_var.get() == expected_colan_state
    assert subject._management_name_var.get() == management.name
    assert subject._management_ip_var.get() == management.ip_address
    assert not hasattr(subject, "_inventory_confirmed_var")


def test_route_side_seeds_map_to_fixed_directions_without_copying() -> None:
    source = {
        "A": {
            "expected_loss_db": None,
            "represented_by_route_span": False,
        },
        "Z": {
            "expected_loss_db": 14.55,
            "represented_by_route_span": True,
        },
    }

    line_1, line_2 = module._route_line_seeds_for_direction(source, "Z")

    assert line_1 == source["Z"]
    assert line_2 == source["A"]
    assert line_1 is not source["Z"]
    assert line_2 is not source["A"]
    assert source["A"]["expected_loss_db"] is None


def test_exact_editor_summarizes_prepopulation_without_customer_values() -> None:
    summary = module._route_seed_prepopulation_summary(
        {
            "prepopulation": {
                "route_reviewed_fields": (
                    "shelf_name",
                    "loopback_ip",
                    "Z.expected_loss_db",
                ),
                "controlled_derivations": ("hostname_from_tid",),
                "controlled_defaults": ("bay_number_zero",),
                "policy_exclusions": ("customer_managed_ntp_omitted",),
                "manual_fields": (
                    "exact_provider_and_installed_bom",
                    "exact_target_build",
                ),
            }
        }
    )

    assert "3 diagram/reviewed route fields" in summary
    assert "1 controlled derivation" in summary
    assert "1 editable workflow default" in summary
    assert "1 deployment-policy exclusion" in summary
    assert "2 engineering/safety groups" in summary
    assert "shelf_name" not in summary


def test_exact_editor_summarizes_peer_pfg_suggestion_without_values() -> None:
    summary = module._route_seed_prepopulation_summary(
        {
            "prepopulation": {
                "route_reviewed_fields": (),
                "controlled_derivations": (
                    "Z_neighbor_pfg_from_audited_peer_role_fallback",
                ),
                "controlled_defaults": (),
                "policy_exclusions": (),
                "manual_fields": (
                    "prepopulated_remote_pfg_review",
                ),
                "peer_pfg_suggestions": (
                    "Z:audited_peer_role_fallback",
                ),
            },
        }
    )

    assert "1 adjacent-peer fixed PFG suggestion" in summary
    assert "operator review remains required" in summary
    assert "PFG-2-to-1" not in summary
    assert "USQTN1-L8I2" not in summary


def test_line_seed_hydrates_peer_pfg_suggestion() -> None:
    widgets = module._LineWidgets(
        **{
            field_name: _Variable()
            for field_name in module._LineWidgets.__dataclass_fields__
        }
    )

    module.RlsR40ConfigFrame._load_line_seed(
        widgets,
        {
            "link_name": "BDJW7353",
            "neighbor_node": "USQTN1-L8I2",
            "neighbor_line_mux_pfg": "PFG-2-to-1",
            "neighbor_line_demux_pfg": "PFG-1-to-2",
            "fiber_type": "LEAF",
            "expected_loss_db": 14.55,
        },
    )

    assert widgets.neighbor_mux_pfg.get() == "PFG-2-to-1"
    assert widgets.neighbor_demux_pfg.get() == "PFG-1-to-2"


def test_passive_span_context_is_display_only_and_complete() -> None:
    text = module._route_span_context_text(
        {
            "represented_by_route_span": True,
            "distance_km": 62.47,
            "circuit_id": "BDJW7353",
            "fiber_start": 14,
            "fiber_end": 15,
            "source_fiber_label": "LEAF",
            "outbound_flow": "A→Z",
            "inbound_flow": "Z→A",
            "inbound_peer_reviewed": True,
            "inbound_peer_link_name": "Z-LOCAL-LINK",
            "inbound_peer_loss_db": 13.75,
            "inbound_peer_fiber_type": "LEAF",
            "neighbor_pfg_source": "audited_peer_role_fallback",
        }
    )

    assert "circuit BDJW7353" in text
    assert "distance 62.47 km" in text
    assert "source fibers 14-15" in text
    assert "diagram fiber label LEAF" in text
    assert "A→Z transmit / Z→A receive" in text
    assert "Z→A facing-shelf egress review" in text
    assert "loss 13.75 dB" in text
    assert "peer link Z-LOCAL-LINK" in text
    assert "neighbor PFG suggestion from audited peer role convention" in text
    assert "operator review required" in text
    assert "documentation context" in text
    assert "independent engineering" in module._route_span_context_text(
        {"represented_by_route_span": False}
    )


def test_route_header_band_display_is_context_only() -> None:
    assert module._display_optical_band("c+l") == "C+L"
    assert module._display_optical_band("integrated_c+l") == "Integrated C+L"
    assert module._display_optical_band("unknown") == ""


def test_reviewed_hardware_context_does_not_claim_provider_compatibility() -> None:
    text = module._reviewed_hardware_context_text(
        {
            "reviewed_hardware": {
                "route_role": "roadm_a",
                "chassis": "R4 600mm",
                "shelf_variant": "R4 600mm",
                "shelf_band": "c+l",
                "topology": "cdc",
                "module_inventory": (
                    {
                        "pec": "NTK852AA",
                        "role": "RLA32",
                        "slot": 1,
                        "subslot": None,
                    },
                ),
                "line_endpoints": (
                    {
                        "adjacency": "following",
                        "slot": 1,
                        "line_out_port": 63,
                    },
                ),
                "power_label": "AC",
            }
        }
    )

    assert "diagram chassis: R4 600mm" in text
    assert "shelf band: c+l" in text
    assert "NTK852AA (RLA32) @ 1" in text
    assert "following: line-out 63 @ slot 1" in text
    assert "do not select or authorize the BOM" in text


def test_prepopulation_summary_discloses_direct_fixed_direction_mapping() -> None:
    summary = module._route_seed_prepopulation_summary(
        {
            "line_1_route_side": "Z",
            "line_semantics": "bidirectional_degree",
            "prepopulation": {
                "route_reviewed_fields": (),
                "controlled_derivations": (),
                "controlled_defaults": (),
                "policy_exclusions": (),
                "manual_fields": (),
            },
        }
    )

    assert "mapped fixed physical degree 1 to route Z-side" in summary
    assert "verify that mapping" in summary


def test_prepopulation_summary_discloses_audited_direction_fallback() -> None:
    summary = module._route_seed_prepopulation_summary(
        {
            "line_1_route_side": "Z",
            "line_semantics": "bidirectional_degree",
            "direction_resolution": {"status": "controlled_fallback"},
            "prepopulation": {
                "route_reviewed_fields": (),
                "controlled_derivations": (),
                "controlled_defaults": (),
                "policy_exclusions": (),
                "manual_fields": (),
            },
        }
    )

    assert "provider/role defaults" in summary
    assert "physical degree 1 to route Z-side" in summary
    assert "non-executable mapping" in summary
    assert "Direct local port evidence" not in summary


def test_line_titles_separate_rla_degrees_from_traffic_propagation() -> None:
    assert module._line_record_title("bidirectional_degree", 1, "Z") == (
        "Degree 1 (Z-facing; A→Z TX / Z→A RX)"
    )
    assert module._line_record_title("bidirectional_degree", 2, "A") == (
        "Degree 2 (A-facing; Z→A TX / A→Z RX)"
    )
    assert module._line_record_title(
        "unidirectional_amplifier_path",
        1,
        "Z",
    ) == "A→Z amplifier path (line-out Z-side)"
    assert module._line_record_title(
        "unidirectional_amplifier_path",
        2,
        "A",
    ) == "Z→A amplifier path (line-out A-side)"


def test_direction_assignment_remaps_seed_and_preserves_later_edits() -> None:
    def line_widgets() -> module._LineWidgets:
        return module._LineWidgets(
            **{
                field_name: _Variable()
                for field_name in module._LineWidgets.__dataclass_fields__
            }
        )

    first = line_widgets()
    second = line_widgets()
    subject = SimpleNamespace(
        _line_1_route_side_var=_Variable(module._ROUTE_SIDE_LABELS["Z"]),
        _route_lines_by_side={
            "A": {
                "link_name": "",
                "expected_loss_db": None,
                "represented_by_route_span": False,
            },
            "Z": {
                "link_name": "CUSTOMER-SPAN",
                "expected_loss_db": 14.55,
                "represented_by_route_span": True,
            },
        },
        _seed_direction_side="",
        _line_1=first,
        _line_2=second,
        _loading=False,
        _refresh_direction_mapping=lambda: None,
        _invalidate=lambda: None,
        controller=None,
    )
    subject._load_line_seed = (
        lambda widgets, raw: module.RlsR40ConfigFrame._load_line_seed(
            widgets, raw
        )
    )
    subject._swap_line_widget_values = (
        lambda: module.RlsR40ConfigFrame._swap_line_widget_values(subject)
    )

    module.RlsR40ConfigFrame._on_route_side_selected(subject)

    assert first.link_name.get() == "CUSTOMER-SPAN"
    assert first.expected_loss.get() == "14.55"
    assert second.expected_loss.get() == ""

    first.link_name.set("OPERATOR-CORRECTED")
    subject._line_1_route_side_var.set(module._ROUTE_SIDE_LABELS["A"])
    module.RlsR40ConfigFrame._on_route_side_selected(subject)

    assert first.expected_loss.get() == ""
    assert second.expected_loss.get() == "14.55"
    assert second.link_name.get() == "OPERATOR-CORRECTED"


def test_manual_provider_selection_hydrates_retained_terminal_direction() -> None:
    def line_widgets() -> module._LineWidgets:
        return module._LineWidgets(
            **{
                field_name: _Variable()
                for field_name in module._LineWidgets.__dataclass_fields__
            }
        )

    class _Notebook:
        def __init__(self) -> None:
            self.selected: object | None = None

        def select(self, tab: object) -> None:
            self.selected = tab

    provider = next(
        profile
        for profile in R40_PROVIDER_CATALOG.values()
        if profile.application == "cdc_roadm_rla32_c"
    )
    direct_endpoint = {
        "adjacency": "following",
        "slot": 1,
        "line_out_port": 53,
        "evidence": [
            {
                "field": "line_endpoints.0.adjacency",
                "normalized_value": "following",
                "confidence": 0.99,
                "method": "inferred",
            },
            {
                "field": "line_endpoints.0.slot",
                "normalized_value": 1,
                "confidence": 0.99,
                "method": "vision",
            },
            {
                "field": "line_endpoints.0.line_out_port",
                "normalized_value": 53,
                "confidence": 0.99,
                "method": "vision",
            },
        ],
    }
    first = line_widgets()
    second = line_widgets()
    line1_tab = object()
    line2_tab = object()
    notebook = _Notebook()
    subject = SimpleNamespace(
        _route_seed={
            "reviewed_hardware": {
                "line_endpoints": (direct_endpoint,),
            },
        },
        _role_profile="roadm_a",
        _route_lines_by_side={
            "A": {
                "represented_by_route_span": False,
                "expected_loss_db": None,
            },
            "Z": {
                "represented_by_route_span": True,
                "link_name": "BDJW7353",
                "neighbor_node": "USQTN1-L8I2",
                "fiber_type": "LEAF",
                "expected_loss_db": 14.55,
            },
        },
        _line_1_route_side_var=_Variable(),
        _seed_direction_side="",
        _line_1=first,
        _line_2=second,
        _loading=False,
        _line1_tab=line1_tab,
        _line2_tab=line2_tab,
        _notebook=notebook,
        _provider_hint=_Variable("Selected provider."),
        _refresh_direction_mapping=lambda: None,
        _invalidate=lambda: None,
        controller=None,
    )
    subject._load_line_seed = (
        lambda widgets, raw: module.RlsR40ConfigFrame._load_line_seed(
            widgets, raw
        )
    )
    subject._swap_line_widget_values = (
        lambda: module.RlsR40ConfigFrame._swap_line_widget_values(subject)
    )
    subject._on_route_side_selected = (
        lambda **kwargs: (
            module.RlsR40ConfigFrame._on_route_side_selected(
                subject,
                **kwargs,
            )
        )
    )
    subject._route_side_for_direction = (
        lambda number: module.RlsR40ConfigFrame._route_side_for_direction(
            subject,
            number,
        )
    )
    subject._route_side_is_represented = (
        lambda side: module.RlsR40ConfigFrame._route_side_is_represented(
            subject,
            side,
        )
    )

    applied = (
        module.RlsR40ConfigFrame
        ._apply_selected_provider_direction_suggestion(subject, provider)
    )

    assert applied is True
    assert subject._line_1_route_side_var.get() == (
        module._ROUTE_SIDE_LABELS["Z"]
    )
    assert first.link_name.get() == "BDJW7353"
    assert first.neighbor_node.get() == "USQTN1-L8I2"
    assert first.fiber_type.get() == "LEAF"
    assert first.expected_loss.get() == "14.55"
    assert second.expected_loss.get() == ""
    assert notebook.selected is line1_tab
    assert "populated the represented route degree" in (
        subject._provider_hint.get()
    )


def test_manual_provider_selection_does_not_fallback_over_low_confidence_endpoint() -> None:
    provider = next(
        profile
        for profile in R40_PROVIDER_CATALOG.values()
        if profile.application == "cdc_roadm_rla32_c"
    )
    resolution = module._selected_provider_direction_resolution(
        {
            "reviewed_hardware": {
                "line_endpoints": (
                    {
                        "adjacency": "following",
                        "slot": 1,
                        "line_out_port": 53,
                        "evidence": [
                            {
                                "field": "line_endpoints.0.adjacency",
                                "normalized_value": "following",
                                "confidence": 0.99,
                                "method": "inferred",
                            },
                            {
                                "field": "line_endpoints.0.slot",
                                "normalized_value": 1,
                                "confidence": 0.5,
                                "method": "vision",
                            },
                            {
                                "field": "line_endpoints.0.line_out_port",
                                "normalized_value": 53,
                                "confidence": 0.5,
                                "method": "vision",
                            },
                        ],
                    },
                ),
            },
        },
        "roadm_a",
        provider,
    )

    assert resolution.status == "missing_evidence"
    assert resolution.line_1_route_side == ""
    assert "AUDITED_PROVIDER_ROLE_DIRECTION_FALLBACK" not in (
        resolution.reason_codes
    )


def test_provider_change_never_overwrites_operator_direction_assignment() -> None:
    provider = provider_profiles_for_role("ila")[0]
    subject = SimpleNamespace(
        _line_1_route_side_var=_Variable(module._ROUTE_SIDE_LABELS["A"]),
        _direction_assignment_provenance="operator",
    )

    applied = (
        module.RlsR40ConfigFrame
        ._apply_selected_provider_direction_suggestion(
            subject,
            provider,
            replace_atlas=True,
        )
    )

    assert applied is False
    assert subject._line_1_route_side_var.get() == (
        module._ROUTE_SIDE_LABELS["A"]
    )
    assert subject._direction_assignment_provenance == "operator"


def test_slotless_terminal_endpoint_shows_route_side_seed_before_direction_mapping() -> None:
    """The real terminal's port 53 is ambiguous, but its Z span is not."""

    class _Notebook:
        def __init__(self) -> None:
            self.selected: object | None = None
            self.labels: dict[object, str] = {}

        def select(self, tab: object) -> None:
            self.selected = tab

        def tab(self, tab: object, *, text: str) -> None:
            self.labels[tab] = text

    def line_widgets() -> module._LineWidgets:
        return module._LineWidgets(
            **{
                field_name: _Variable()
                for field_name in module._LineWidgets.__dataclass_fields__
            }
        )

    provider = next(
        profile
        for profile in R40_PROVIDER_CATALOG.values()
        if profile.application == "cdc_roadm_rla32_c"
    )
    slotless_endpoint = {
        "adjacency": "following",
        "line_out_port": 53,
        "evidence": [
            {
                "field": "line_endpoints.0.adjacency",
                "normalized_value": "following",
                "confidence": 0.99,
                "method": "inferred",
            },
            {
                "field": "line_endpoints.0.line_out_port",
                "normalized_value": 53,
                "confidence": 0.99,
                "method": "vision",
            },
        ],
    }
    route_seed = {
        "shelf_name": "USELP1-L8R2",
        "shelf_label": "El Paso",
        "site_name": "El Paso",
        "site_id": 0,
        "site_description": "ELP1-SAT4",
        "site_address": "",
        "member_name": "USELP1-L8R2",
        "hostname": "USELP1-L8R2",
        "frame_identification_code": "",
        "bay_number": 0,
        "physical_shelf": 0,
        "loopback_ip": "10.6.22.129",
        "ospf_area": "10.6.8.0",
        "diagram_optical_band": "c+l",
        "line_1_route_side": "",
        "reviewed_hardware": {
            "line_endpoints": (slotless_endpoint,),
        },
    }
    lines_by_side = {
        "A": {
            "represented_by_route_span": False,
            "link_name": "",
            "neighbor_node": "",
            "fiber_type": "",
            "expected_loss_db": None,
        },
        "Z": {
            "represented_by_route_span": True,
            "link_name": "BDJW7353",
            "neighbor_node": "USQTN1-L8I2",
            "fiber_type": "LEAF",
            "expected_loss_db": 14.55,
        },
    }
    route_seed["lines_by_side"] = lines_by_side

    # Port 53 exists in both fixed provider directions when no slot is shown.
    # ATLAS must not invent the fixed mapping.
    resolution = module._selected_provider_direction_resolution(
        route_seed,
        "roadm_a",
        provider,
    )
    assert resolution.status == "ambiguous"
    assert resolution.line_1_route_side == ""

    first = line_widgets()
    second = line_widgets()
    line1_tab = object()
    line2_tab = object()
    notebook = _Notebook()
    subject = SimpleNamespace(
        _route_seed=route_seed,
        _role_profile="roadm_a",
        _route_lines_by_side=lines_by_side,
        _line_1=first,
        _line_2=second,
        _seed_direction_side="",
        _target_build_var=_Variable(),
        _shelf_name_var=_Variable(),
        _shelf_label_var=_Variable(),
        _site_name_var=_Variable(),
        _site_id_var=_Variable(),
        _site_description_var=_Variable(),
        _site_address_var=_Variable(),
        _member_name_var=_Variable(),
        _hostname_var=_Variable(),
        _frame_id_var=_Variable(),
        _bay_var=_Variable(),
        _physical_shelf_var=_Variable(),
        _loopback_var=_Variable(),
        _ospf_area_var=_Variable(),
        _diagram_band_display=_Variable(),
        _line_1_route_side_var=_Variable(),
        _line1_tab=line1_tab,
        _line2_tab=line2_tab,
        _notebook=notebook,
        _line_context_hints={1: _Variable(), 2: _Variable()},
        _line_mapping_hints={1: _Variable(), 2: _Variable()},
        _provider_hint=_Variable("Selected provider."),
        _loading=False,
        _selected_profile=lambda: None,
        _invalidate=lambda: None,
        controller=None,
    )
    subject._load_line_seed = (
        lambda widgets, raw: module.RlsR40ConfigFrame._load_line_seed(
            widgets,
            raw,
        )
    )
    subject._swap_line_widget_values = (
        lambda: module.RlsR40ConfigFrame._swap_line_widget_values(subject)
    )
    subject._refresh_direction_mapping = (
        lambda: module.RlsR40ConfigFrame._refresh_direction_mapping(subject)
    )

    module.RlsR40ConfigFrame._load_route_seed(subject)
    module.RlsR40ConfigFrame._refresh_direction_mapping(subject)

    assert subject._target_build_var.get() == DEFAULT_R40_TARGET_BUILD_SCHEMA
    assert subject._line_1_route_side_var.get() == ""
    assert subject._seed_direction_side == "Z"
    assert first.link_name.get() == "BDJW7353"
    assert first.neighbor_node.get() == "USQTN1-L8I2"
    assert first.fiber_type.get() == "LEAF"
    assert first.expected_loss.get() == "14.55"
    assert second.link_name.get() == ""
    assert second.expected_loss.get() == ""
    assert notebook.selected is line1_tab
    assert notebook.labels[line1_tab] == (
        "Route Z-facing degree data — A→Z TX / Z→A RX — degree unassigned"
    )
    assert notebook.labels[line2_tab] == (
        "Route A-facing degree data — Z→A TX / A→Z RX — degree unassigned"
    )
    assert "not yet assigned to fixed provider Degree 1 or Degree 2" in (
        subject._line_mapping_hints[1].get()
    )
    assert "carries both flows" in subject._line_mapping_hints[1].get()

    applied = (
        module.RlsR40ConfigFrame
        ._apply_selected_provider_direction_suggestion(subject, provider)
    )
    assert applied is False
    assert subject._line_1_route_side_var.get() == ""
    assert "first fixed local line-output remains unassigned" in (
        subject._provider_hint.get()
    )
    assert "AMBIGUOUS_LINE_OUTPUT_MATCH" in subject._provider_hint.get()

    # Manually selecting the matching fixed direction preserves an operator
    # correction made while the route-side staging data was visible.
    first.link_name.set("BDJW7353-CORRECTED")
    subject._line_1_route_side_var.set(module._ROUTE_SIDE_LABELS["Z"])
    module.RlsR40ConfigFrame._on_route_side_selected(subject)
    assert first.link_name.get() == "BDJW7353-CORRECTED"
    assert second.link_name.get() == ""

    # Changing that mapping swaps the independent route-side values rather
    # than reloading them or copying the represented span into both degrees.
    subject._line_1_route_side_var.set(module._ROUTE_SIDE_LABELS["A"])
    module.RlsR40ConfigFrame._on_route_side_selected(subject)
    assert first.link_name.get() == ""
    assert second.link_name.get() == "BDJW7353-CORRECTED"


def test_terminal_mapping_selects_direction_tab_that_contains_route_seed() -> None:
    class _Notebook:
        def __init__(self) -> None:
            self.selected: object | None = None

        def select(self, tab: object) -> None:
            self.selected = tab

    line1_tab = object()
    line2_tab = object()
    notebook = _Notebook()
    subject = SimpleNamespace(
        _line_1_route_side_var=_Variable(
            module._ROUTE_SIDE_LABELS["A"]
        ),
        _route_lines_by_side={
            "A": {"represented_by_route_span": False},
            "Z": {"represented_by_route_span": True},
        },
        _line1_tab=line1_tab,
        _line2_tab=line2_tab,
        _notebook=notebook,
    )

    selected = (
        module.RlsR40ConfigFrame._select_represented_direction_tab(subject)
    )

    assert selected == 2
    assert notebook.selected is line2_tab


def test_resolved_route_direction_hydrates_both_lines_on_open() -> None:
    def line_widgets() -> module._LineWidgets:
        return module._LineWidgets(
            **{
                field_name: _Variable()
                for field_name in module._LineWidgets.__dataclass_fields__
            }
        )

    first = line_widgets()
    second = line_widgets()
    subject = SimpleNamespace(
        _route_seed={
            "shelf_name": "USELP1-L8R2",
            "shelf_label": "El Paso",
            "site_name": "El Paso",
            "site_id": 0,
            "site_description": "ELP1-SAT4",
            "site_address": "",
            "member_name": "USELP1-L8R2",
            "hostname": "USELP1-L8R2",
            "frame_identification_code": "",
            "bay_number": 0,
            "physical_shelf": 0,
            "loopback_ip": "10.6.22.129",
            "ospf_area": "10.6.8.0",
            "diagram_optical_band": "c+l",
            "line_1_route_side": "Z",
            "direction_resolution": {"status": "controlled_fallback"},
        },
        _route_lines_by_side={
            "A": {
                "represented_by_route_span": False,
                "neighbor_node": "",
            },
            "Z": {
                "represented_by_route_span": True,
                "link_name": "BDJW7353",
                "neighbor_node": "USQTN1-L8I2",
                "fiber_type": "LEAF",
                "expected_loss_db": 14.55,
            },
        },
        _line_1=first,
        _line_2=second,
        _seed_direction_side="",
        _target_build_var=_Variable(),
        _shelf_name_var=_Variable(),
        _shelf_label_var=_Variable(),
        _site_name_var=_Variable(),
        _site_id_var=_Variable(),
        _site_description_var=_Variable(),
        _site_address_var=_Variable(),
        _member_name_var=_Variable(),
        _hostname_var=_Variable(),
        _frame_id_var=_Variable(),
        _bay_var=_Variable(),
        _physical_shelf_var=_Variable(),
        _loopback_var=_Variable(),
        _ospf_area_var=_Variable(),
        _diagram_band_display=_Variable(),
        _line_1_route_side_var=_Variable(),
    )
    subject._load_line_seed = (
        lambda widgets, raw: module.RlsR40ConfigFrame._load_line_seed(
            widgets, raw
        )
    )

    module.RlsR40ConfigFrame._load_route_seed(subject)

    assert subject._target_build_var.get() == DEFAULT_R40_TARGET_BUILD_SCHEMA
    assert subject._line_1_route_side_var.get() == (
        module._ROUTE_SIDE_LABELS["Z"]
    )
    assert subject._seed_direction_side == "Z"
    assert first.neighbor_node.get() == "USQTN1-L8I2"
    assert first.link_name.get() == "BDJW7353"
    assert first.fiber_type.get() == "LEAF"
    assert first.expected_loss.get() == "14.55"
    assert second.neighbor_node.get() == ""
    assert second.expected_loss.get() == ""


def test_unrepresented_degree_reports_independent_engineering_requirement() -> None:
    blank = lambda value="": _Variable(value)
    widgets = module._LineWidgets(
        link_name=blank(),
        neighbor_node=blank(),
        neighbor_mux_pfg=blank(),
        neighbor_demux_pfg=blank(),
        fiber_type=blank("NDSF"),
        expected_loss=blank(),
        input_patch_loss=blank("0.5"),
        output_patch_loss=blank("0.5"),
        repair_margin=blank("2"),
        high_loss_threshold=blank("3"),
        ospcfib=blank(),
    )
    subject = SimpleNamespace(
        _route_side_for_direction=lambda _number: "A",
        _route_side_is_represented=lambda _side: False,
        _line_semantics="bidirectional_degree",
        _required_float=module.RlsR40ConfigFrame._required_float,
    )

    with pytest.raises(ValueError, match="not represented") as caught:
        module.RlsR40ConfigFrame._line_value(subject, widgets, 1)

    assert "independently engineered" in str(caught.value)
    assert "Physical degree 1" in str(caught.value)
    assert "will not copy" in str(caught.value)


@pytest.mark.parametrize(
    ("semantics", "expected_label"),
    [
        ("bidirectional_degree", "Physical degree 1"),
        ("unidirectional_amplifier_path", "Amplifier path 1"),
    ],
)
def test_missing_loss_uses_hardware_record_semantics(
    semantics: str,
    expected_label: str,
) -> None:
    blank = lambda value="": _Variable(value)
    widgets = module._LineWidgets(
        link_name=blank("ROUTE-LINK"),
        neighbor_node=blank("NEIGHBOR"),
        neighbor_mux_pfg=blank("MUX-PFG"),
        neighbor_demux_pfg=blank("DEMUX-PFG"),
        fiber_type=blank("NDSF"),
        expected_loss=blank(),
        input_patch_loss=blank("0.5"),
        output_patch_loss=blank("0.5"),
        repair_margin=blank("2"),
        high_loss_threshold=blank("3"),
        ospcfib=blank(),
    )
    subject = SimpleNamespace(
        _route_side_for_direction=lambda _number: "Z",
        _route_side_is_represented=lambda _side: True,
        _line_semantics=semantics,
        _required_float=module.RlsR40ConfigFrame._required_float,
    )

    with pytest.raises(ValueError, match="expected loss is missing") as caught:
        module.RlsR40ConfigFrame._line_value(subject, widgets, 1)

    assert expected_label in str(caught.value)
    assert "Direction 1" not in str(caught.value)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("42", 42),
        (" 3 ", 3),
    ],
)
def test_required_integer_parser(raw: str, expected: int) -> None:
    assert module.RlsR40ConfigFrame._required_int(raw, "Field") == expected


@pytest.mark.parametrize("raw", ["", "3.5", "three"])
def test_required_integer_parser_fails_closed(raw: str) -> None:
    with pytest.raises(ValueError, match="must be a whole number"):
        module.RlsR40ConfigFrame._required_int(raw, "Field")


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("add_drop_a", module.COLAN_TERMINAL_OPTIONAL),
        ("roadm_z", module.COLAN_TERMINAL_OPTIONAL),
        ("ila", module.COLAN_PROHIBITED),
    ],
)
def test_exact_editor_colan_policy_follows_route_role(
    role: str, expected: str
) -> None:
    assert module._role_colan_policy(role) == expected


def test_planning_preview_identifies_only_unresolved_cli_gates() -> None:
    terminal = next(
        profile
        for profile in provider_profiles_for_role("roadm_a")
        if len(profile.line_outputs) == 1
    )

    blockers = module._planning_preview_blockers(
        terminal,
        "roadm_a",
        "RLS R4.0",
        module._COLAN_REVIEW_DEFERRED,
    )

    assert {issue.code for issue in blockers} == {"TARGET_BUILD_REQUIRED"}
    assert module._planning_preview_blockers(
        terminal,
        "roadm_a",
        "R4.0.0-123",
        module._COLAN_REVIEW_DEFERRED,
    ) == ()
    assert module._planning_preview_blockers(
        terminal,
        "roadm_a",
        "R4.0.0-123",
        module._COLAN_REVIEW_CONFIGURED,
    ) == ()

    ila = next(iter(provider_profiles_for_role("ila")))
    assert {
        issue.code
        for issue in module._planning_preview_blockers(
            ila,
            "ila",
            "R4.0.0-123",
            module._COLAN_REVIEW_NOT_APPLICABLE,
        )
    } == set()


def test_planning_preview_is_watermarked_non_cli_and_uses_no_fake_colan() -> None:
    provider = next(
        profile
        for profile in provider_profiles_for_role("roadm_a")
        if len(profile.line_outputs) == 1
    )
    blockers = module._planning_preview_blockers(
        provider,
        "roadm_a",
        "",
        module._COLAN_REVIEW_DEFERRED,
    )
    report = module._render_planning_preview(
        provider,
        "roadm_a",
        {
            "shelf_name": "USELP1-L8R2",
            "site_name": "El Paso, TX",
            "loopback_ip": "10.6.22.129",
            "ospf_area": "10.6.8.0",
            "target_software_build": "",
            "frame_identification_code": "",
            "colan_review_state": module._COLAN_REVIEW_DEFERRED,
            # Stale draft values must not appear while COLAN is deferred.
            "routing_model": "draft-routing",
            "management_name": "colan-x",
            "management_ip": "192.0.2.25",
            "management_prefix": "24",
            "line_1": {
                "route_side": "Z",
                "link_name": "LM1-LINEOUT",
                "neighbor_node": "USQTN1-L8I2",
                "neighbor_line_mux_pfg": "PFG-1",
                "neighbor_line_demux_pfg": "PFG-2",
                "fiber_type": "LEAF",
                "expected_loss_db": "14.55",
            },
        },
        blockers,
    )

    assert report.count("PLANNING PREVIEW — NOT CLI — DO NOT DEPLOY") == 2
    assert "No CLI, exact request payload, or deployable artifact" in report
    assert "USELP1-L8R2" in report
    assert "USQTN1-L8I2" in report
    assert "LEAF" in report
    assert "14.55" in report
    assert report.count(module._PENDING_DISPLAY) >= 4
    assert "colan-x" not in report
    assert "192.0.2.25" not in report
    assert not any(
        line.startswith(("set ", "batch", "validate", "commit", "quit"))
        for line in report.splitlines()
    )


def test_preview_with_pending_gates_never_runs_strict_generation() -> None:
    provider = next(
        profile
        for profile in provider_profiles_for_role("roadm_a")
        if len(profile.line_outputs) == 1
    )
    calls: list[object] = []
    subject = SimpleNamespace(
        _selected_profile=lambda: provider,
        _role_profile="roadm_a",
        _target_build_var=_Variable(""),
        _frame_id_var=_Variable(""),
        _colan_state_var=_Variable(module._COLAN_REVIEW_DEFERRED),
        _show_planning_preview=lambda selected, blockers: calls.append(
            ("planning", selected, tuple(blockers))
        ),
        _generate_current=lambda: calls.append("strict"),
        controller=None,
    )

    module.RlsR40ConfigFrame._preview(subject)

    assert len(calls) == 1
    assert calls[0][0] == "planning"
    assert calls[0][1] is provider
    assert {issue.code for issue in calls[0][2]} == {"TARGET_BUILD_REQUIRED"}


@pytest.mark.parametrize(
    "colan_state",
    (
        module._COLAN_REVIEW_DEFERRED,
        module._COLAN_REVIEW_CONFIGURED,
    ),
)
def test_preview_with_optional_or_configured_colan_uses_strict_path(
    colan_state: str,
) -> None:
    provider = next(
        profile
        for profile in provider_profiles_for_role("roadm_a")
        if len(profile.line_outputs) == 1
    )
    calls: list[str] = []
    subject = SimpleNamespace(
        _selected_profile=lambda: provider,
        _role_profile="roadm_a",
        _target_build_var=_Variable("R4.0.0-123"),
        _frame_id_var=_Variable("FRAME-1"),
        _colan_state_var=_Variable(colan_state),
        _show_planning_preview=lambda *_args: calls.append("planning"),
        _generate_current=lambda: calls.append("strict"),
        controller=None,
    )

    module.RlsR40ConfigFrame._preview(subject)

    assert calls == ["strict"]


def test_planning_surface_disables_apply_and_copy_without_artifact() -> None:
    class _Control:
        def __init__(self) -> None:
            self.options: dict[str, object] = {}

        def config(self, **kwargs: object) -> None:
            self.options.update(kwargs)

    class _Notebook:
        def __init__(self) -> None:
            self.selected: object | None = None

        def select(self, tab: object) -> None:
            self.selected = tab

    provider = next(
        profile
        for profile in provider_profiles_for_role("roadm_a")
        if len(profile.line_outputs) == 1
    )
    apply_button = _Control()
    copy_button = _Control()
    chooser = _Control()
    notebook = _Notebook()
    preview_tab = object()
    shown: list[str] = []
    subject = SimpleNamespace(
        _artifact=object(),
        _planning_preview_text=None,
        _role_profile="roadm_a",
        _planning_fields=lambda _profile: {
            "colan_review_state": module._COLAN_REVIEW_DEFERRED,
            "line_1": {},
        },
        _apply_button=apply_button,
        _copy_button=copy_button,
        _preview_chooser=chooser,
        _preview_type_var=_Variable(),
        _set_preview_text=lambda text: shown.append(text),
        _notebook=notebook,
        _preview_tab=preview_tab,
        _status_var=_Variable(),
        controller=None,
    )
    blockers = module._planning_preview_blockers(
        provider,
        "roadm_a",
        "",
        module._COLAN_REVIEW_DEFERRED,
    )

    module.RlsR40ConfigFrame._show_planning_preview(
        subject,
        provider,
        blockers,
    )

    assert subject._artifact is None
    assert apply_button.options["state"] == module.tk.DISABLED
    assert copy_button.options["state"] == module.tk.DISABLED
    assert chooser.options["values"] == (module._PLANNING_PREVIEW_TYPE,)
    assert subject._preview_type_var.get() == module._PLANNING_PREVIEW_TYPE
    assert shown and shown[-1] == subject._planning_preview_text
    assert notebook.selected is preview_tab
    assert "Planning review only" in subject._status_var.get()


def test_deferred_terminal_colan_builds_canonical_blank_management_value() -> None:
    provider = next(
        profile
        for profile in provider_profiles_for_role("roadm_a")
        if len(profile.line_outputs) == 1
    )
    subject = SimpleNamespace(
        _colan_state_var=_Variable(module._COLAN_REVIEW_DEFERRED),
    )

    management = module.RlsR40ConfigFrame._management_value(subject, provider)

    assert management == module.ManagementInterface(
        enabled=False,
        name="",
        routing_mode=module.ROUTING_OSPF_OSC_ONLY,
        ip_address="",
        prefix_length=None,
        gateway="",
        ospf_metric=10,
        static_metric=1500,
    )


def test_deferred_terminal_colan_never_reuses_hidden_control_values() -> None:
    class _UnexpectedRead:
        @staticmethod
        def get() -> object:
            raise AssertionError("deferred COLAN must not read hidden values")

    provider = next(
        profile
        for profile in provider_profiles_for_role("roadm_a")
        if len(profile.line_outputs) == 1
    )
    subject = SimpleNamespace(
        _colan_state_var=_Variable(module._COLAN_REVIEW_DEFERRED),
        _routing_var=_UnexpectedRead(),
        _management_name_var=_UnexpectedRead(),
        _management_ip_var=_UnexpectedRead(),
        _prefix_var=_UnexpectedRead(),
        _gateway_var=_UnexpectedRead(),
        _ospf_metric_var=_UnexpectedRead(),
        _static_metric_var=_UnexpectedRead(),
    )

    management = module.RlsR40ConfigFrame._management_value(subject, provider)

    assert management.enabled is False
    assert management.routing_mode == module.ROUTING_OSPF_OSC_ONLY
    assert management.name == ""
    assert management.ip_address == ""
    assert management.prefix_length is None
    assert management.gateway == ""


def test_terminal_editor_defaults_customer_colan_fields_blank() -> None:
    source = inspect.getsource(module.RlsR40ConfigFrame._build_oam)

    assert "_COLAN_REVIEW_DEFERRED" in source
    assert "self._management_name_var = self._new_string()" in source
    assert "self._prefix_var = self._new_string()" in source
    assert 'self._new_string("colan-x")' not in source
    assert 'self._new_string("24")' not in source


def test_apply_regenerates_and_invokes_route_callback() -> None:
    calls: list[tuple[object, object]] = []
    request = SimpleNamespace(
        provider_id="r40-provider",
        shelf_name="RLS-01",
    )
    artifact = SimpleNamespace(request=request)
    subject = SimpleNamespace(
        _generate_current=lambda: artifact,
        _on_apply=lambda request_value, artifact_value: calls.append(
            (request_value, artifact_value)
        ),
        _dirty=True,
        _status_var=_Variable(),
        controller=None,
    )

    module.RlsR40ConfigFrame._apply_reviewed(subject)

    assert calls == [(request, artifact)]
    assert subject._dirty is False
    assert subject._status_var.get() == (
        "Reviewed exact R4.0 request applied to this shelf."
    )


def test_apply_does_not_invoke_callback_when_generation_is_blocked() -> None:
    calls: list[object] = []
    subject = SimpleNamespace(
        _generate_current=lambda: None,
        _on_apply=lambda *_args: calls.append(object()),
    )

    module.RlsR40ConfigFrame._apply_reviewed(subject)

    assert calls == []


def test_dirty_state_is_exposed_for_close_confirmation() -> None:
    assert module.RlsR40ConfigFrame.has_unapplied_changes(
        SimpleNamespace(_dirty=True)
    )
    assert not module.RlsR40ConfigFrame.has_unapplied_changes(
        SimpleNamespace(_dirty=False)
    )

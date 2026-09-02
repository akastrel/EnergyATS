from ats_core import Phase, Snapshot
from policy_controller import PolicyATSController


def snap(**kw):
    base = dict(
        grid_ready=True,
        house_grid=True,
        house_generator=False,
        generator_a_running=False,
        generator_b_running=False,
        emergency_stop=False,
        garage_temperature=20.0,
        remote_a=False,
        remote_b=False,
        grid_disconnected=False,
        source_generator=False,
        ats_enabled=False,
        session_active=False,
        session_mode="none",
    )
    base.update(kw)
    return Snapshot(**base)


def action_triplets(actions):
    return [(a.kind, a.target, a.value) for a in actions]


def test_primary_generator_b_is_used_for_new_session():
    PolicyATSController.configure_runtime(
        {"primary_generator": "B", "generator_a_enabled": True, "generator_b_enabled": True}
    )
    c = PolicyATSController()
    actions = c._begin_session_and_start(0, snap(), "manual", "A")
    assert c.active_generator == "B"
    assert ("switch_on", "switch.generator_b_remote_start", None) in action_triplets(actions)


def test_disabled_primary_falls_back_to_enabled_generator():
    PolicyATSController.configure_runtime(
        {"primary_generator": "A", "generator_a_enabled": False, "generator_b_enabled": True}
    )
    c = PolicyATSController()
    c._begin_session_and_start(0, snap(), "manual", "A")
    assert c.active_generator == "B"


def test_global_manual_return_interrupts_starting_and_isolates_generator_bus():
    PolicyATSController.configure_runtime(
        {"primary_generator": "A", "generator_a_enabled": True, "generator_b_enabled": True}
    )
    c = PolicyATSController()
    c.phase = Phase.STARTING
    c.active_generator = "B"
    c.session_mode = "manual"
    actions = c._handle_manual_return(
        10,
        snap(
            house_grid=False,
            house_generator=False,
            remote_b=True,
            grid_disconnected=True,
            source_generator=False,
            session_active=True,
            session_mode="manual",
        ),
    )
    assert c.phase == Phase.RETURN_SELECT_GRID
    triplets = action_triplets(actions)
    assert ("switch_off", "switch.generator_a_remote_start", None) in triplets
    assert ("switch_off", "switch.generator_b_remote_start", None) in triplets
    assert ("switch_off", "switch.use_generator_as_power_source", None) in triplets


def test_global_manual_return_requires_grid_ready():
    c = PolicyATSController()
    c.phase = Phase.STARTING
    actions = c._handle_manual_return(10, snap(grid_ready=False, house_grid=False))
    assert c.phase == Phase.STARTING
    assert any(a.kind == "notify_warning" for a in actions)

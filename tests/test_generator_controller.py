from __future__ import annotations

from dataclasses import replace

from domain import GeneratorSlot
from generator_controller import (
    ChokeStrategy,
    GeneratorActionKind,
    GeneratorController,
    GeneratorObservation,
    GeneratorPhase,
    GeneratorProfile,
)


def profile(**changes) -> GeneratorProfile:
    base = GeneratorProfile(
        slot=GeneratorSlot.A,
        display_name="Test Generator",
        model="Test Model",
        choke_strategy=ChokeStrategy.ALWAYS,
        choke_move_seconds=0.5,
        cold_start_choke_hold_seconds=1.0,
        start_timeout_seconds=4.0,
        stop_timeout_seconds=2.0,
        cooldown_seconds=3.0,
        warmup_warm_seconds=2.0,
        warmup_cool_seconds=2.0,
        warmup_cold_seconds=2.0,
        warmup_very_cold_seconds=2.0,
    )
    return replace(base, **changes)


def observed(**changes) -> GeneratorObservation:
    base = GeneratorObservation(
        running=False,
        remote_on=False,
        load_connected=False,
        emergency_stop=False,
        ambient_temperature_external=20.0,
    )
    return replace(base, **changes)


def action_kinds(actions):
    return [action.kind for action in actions]


def test_complete_managed_start_warmup_cooldown_and_stop():
    controller = GeneratorController(profile())
    controller.step(0.0, observed(), desired_running=False)
    assert controller.phase == GeneratorPhase.IDLE

    actions = controller.step(1.0, observed(), desired_running=True)
    assert action_kinds(actions) == [GeneratorActionKind.CHOKE_TO_COLD_START]
    assert controller.start_temperature_source == "ambient_temperature_external"

    actions = controller.step(1.5, observed(), desired_running=True)
    assert action_kinds(actions) == [GeneratorActionKind.REMOTE_ON]
    assert controller.phase == GeneratorPhase.WAITING_FOR_RUNNING

    running = observed(running=True, remote_on=True)
    assert controller.step(2.0, running, desired_running=True) == []
    assert controller.phase == GeneratorPhase.HOLDING_COLD_START_CHOKE

    actions = controller.step(3.0, running, desired_running=True)
    assert action_kinds(actions) == [GeneratorActionKind.CHOKE_TO_RUN]
    assert controller.phase == GeneratorPhase.WARMING_UP

    controller.step(5.0, running, desired_running=True)
    assert controller.phase == GeneratorPhase.READY_FOR_LOAD
    assert controller.status(running).ready_for_load is True

    loaded = replace(running, load_connected=True)
    controller.step(6.0, loaded, desired_running=False)
    assert controller.phase == GeneratorPhase.WAITING_FOR_LOAD_RELEASE

    controller.step(7.0, running, desired_running=False)
    assert controller.phase == GeneratorPhase.COOLING_DOWN
    actions = controller.step(10.0, running, desired_running=False)
    assert action_kinds(actions) == [GeneratorActionKind.REMOTE_OFF]

    controller.step(11.0, observed(), desired_running=False)
    assert controller.phase == GeneratorPhase.IDLE


def test_temperature_strategy_uses_external_ambient_temperature():
    controller = GeneratorController(
        profile(
            choke_strategy=ChokeStrategy.TEMPERATURE,
            choke_temperature=10.0,
        )
    )
    controller.step(0.0, observed(), desired_running=False)

    actions = controller.step(1.0, observed(ambient_temperature_external=18.0), True)
    assert action_kinds(actions) == [GeneratorActionKind.CHOKE_TO_RUN]
    assert controller.start_temperature == 18.0
    assert controller.start_temperature_source == "ambient_temperature_external"


def test_unknown_temperature_uses_conservative_cold_start():
    controller = GeneratorController(
        profile(choke_strategy=ChokeStrategy.TEMPERATURE)
    )
    controller.step(0.0, observed(), False)
    actions = controller.step(
        1.0,
        observed(ambient_temperature_external=None),
        True,
    )
    assert action_kinds(actions) == [GeneratorActionKind.CHOKE_TO_COLD_START]
    assert controller.start_temperature_source == "conservative_fallback"


def test_external_start_is_observed_but_never_captured():
    controller = GeneratorController(profile())
    external = observed(running=True, remote_on=False)

    assert controller.step(0.0, external, desired_running=False) == []
    assert controller.phase == GeneratorPhase.EXTERNAL_RUNNING
    assert controller.status(external).externally_started is True

    # Даже появившееся позднее намерение Supervisor не даёт ownership.
    assert controller.step(1.0, external, desired_running=True) == []
    assert controller.phase == GeneratorPhase.EXTERNAL_RUNNING

    controller.step(
        2.0,
        observed(),
        desired_running=False,
        actions_allowed=False,
    )
    assert controller.phase == GeneratorPhase.IDLE


def test_external_remote_request_is_observed_before_engine_is_running():
    controller = GeneratorController(profile())
    external_request = observed(running=False, remote_on=True)

    assert controller.step(0.0, external_request, desired_running=False) == []
    assert controller.phase == GeneratorPhase.EXTERNAL_RUNNING
    assert controller.status(external_request).externally_started is True

    controller.step(
        1.0,
        external_request,
        desired_running=False,
        actions_allowed=False,
    )
    assert controller.phase == GeneratorPhase.EXTERNAL_RUNNING

    controller.step(
        2.0,
        observed(),
        desired_running=False,
        actions_allowed=False,
    )
    assert controller.phase == GeneratorPhase.IDLE


def test_restart_restores_ownership_only_for_stable_managed_session():
    running = observed(running=True, remote_on=True, load_connected=True)

    managed = GeneratorController(profile())
    managed.step(
        0.0,
        running,
        desired_running=True,
        stable_managed_session=True,
    )
    assert managed.phase == GeneratorPhase.READY_FOR_LOAD

    unowned = GeneratorController(profile())
    unowned.step(
        0.0,
        running,
        desired_running=True,
        stable_managed_session=False,
    )
    assert unowned.phase == GeneratorPhase.EXTERNAL_RUNNING


def test_cancelled_start_waits_for_remote_off_confirmation():
    controller = GeneratorController(profile())
    controller.step(0.0, observed(), False)
    controller.step(1.0, observed(), True)
    controller.step(1.5, observed(remote_on=True), True)

    actions = controller.step(2.0, observed(remote_on=True), False)
    assert action_kinds(actions) == [
        GeneratorActionKind.REMOTE_OFF,
        GeneratorActionKind.CHOKE_TO_RUN,
    ]
    assert controller.phase == GeneratorPhase.WAITING_FOR_STOP

    controller.step(2.5, observed(remote_on=True), False)
    assert controller.phase == GeneratorPhase.WAITING_FOR_STOP
    controller.step(3.0, observed(remote_on=False), False)
    assert controller.phase == GeneratorPhase.IDLE


def test_generator_is_never_stopped_while_load_is_connected():
    controller = GeneratorController(profile())
    running = observed(running=True, remote_on=True, load_connected=True)
    controller.step(
        0.0,
        running,
        desired_running=True,
        stable_managed_session=True,
    )

    for now in (1.0, 10.0, 100.0):
        actions = controller.step(now, running, desired_running=False)
        assert GeneratorActionKind.REMOTE_OFF not in action_kinds(actions)
    assert controller.phase == GeneratorPhase.WAITING_FOR_LOAD_RELEASE


def test_temporary_unavailable_state_freezes_instead_of_losing_phase():
    controller = GeneratorController(profile())
    running = observed(running=True, remote_on=True)
    controller.step(
        0.0,
        running,
        desired_running=True,
        stable_managed_session=True,
    )
    assert controller.phase == GeneratorPhase.READY_FOR_LOAD

    unavailable = observed(running=None)
    assert controller.step(1.0, unavailable, desired_running=True) == []
    assert controller.phase == GeneratorPhase.READY_FOR_LOAD

    controller.step(2.0, running, desired_running=True)
    assert controller.phase == GeneratorPhase.READY_FOR_LOAD


def test_start_timeout_opens_choke_and_removes_remote():
    controller = GeneratorController(profile())
    controller.step(0.0, observed(), False)
    controller.step(1.0, observed(), True)
    controller.step(1.5, observed(remote_on=True), True)

    actions = controller.step(5.6, observed(remote_on=True), True)

    assert action_kinds(actions) == [
        GeneratorActionKind.REMOTE_OFF,
        GeneratorActionKind.CHOKE_TO_RUN,
    ]
    assert controller.phase == GeneratorPhase.FAULT


def test_temperature_equal_to_threshold_uses_run_position():
    controller = GeneratorController(
        profile(
            choke_strategy=ChokeStrategy.TEMPERATURE,
            choke_temperature=10.0,
        )
    )
    controller.step(0.0, observed(), False)

    actions = controller.step(
        1.0,
        observed(ambient_temperature_external=10.0),
        True,
    )

    assert action_kinds(actions) == [GeneratorActionKind.CHOKE_TO_RUN]


def test_warmup_table_has_explicit_temperature_boundaries():
    configured = profile(
        warm_temperature=10.0,
        cool_temperature=-5.0,
        cold_temperature=-10.0,
        warmup_warm_seconds=30.0,
        warmup_cool_seconds=60.0,
        warmup_cold_seconds=180.0,
        warmup_very_cold_seconds=300.0,
    )

    assert configured.warmup_seconds(10.0) == 30.0
    assert configured.warmup_seconds(-4.9) == 60.0
    assert configured.warmup_seconds(-5.0) == 180.0
    assert configured.warmup_seconds(-10.0) == 300.0
    assert configured.warmup_seconds(None) == 300.0


def test_ready_requires_both_running_and_managed_remote():
    controller = GeneratorController(profile())
    running = observed(running=True, remote_on=True)
    controller.step(
        0.0,
        running,
        desired_running=True,
        stable_managed_session=True,
    )
    assert controller.status(running).ready_for_load is True

    remote_lost = observed(running=True, remote_on=False)
    assert controller.status(remote_lost).ready_for_load is False


def test_already_stopped_unloaded_generator_needs_no_stop_command():
    controller = GeneratorController(profile())
    running = observed(running=True, remote_on=True, load_connected=False)
    controller.step(
        0.0,
        running,
        desired_running=True,
        stable_managed_session=True,
    )

    actions = controller.step(1.0, observed(), desired_running=False)

    assert actions == []
    assert controller.phase == GeneratorPhase.IDLE


def test_running_off_without_remote_off_is_not_accepted_as_local_stop():
    controller = GeneratorController(profile())
    running = observed(running=True, remote_on=True, load_connected=False)
    controller.step(
        0.0,
        running,
        desired_running=True,
        stable_managed_session=True,
    )
    controller.step(1.0, running, desired_running=False)
    assert controller.phase == GeneratorPhase.COOLING_DOWN

    actions = controller.step(
        2.0,
        observed(running=False, remote_on=True, load_connected=False),
        desired_running=False,
    )

    assert controller.phase == GeneratorPhase.FAULT
    assert action_kinds(actions) == [
        GeneratorActionKind.REMOTE_OFF,
        GeneratorActionKind.CHOKE_TO_RUN,
    ]

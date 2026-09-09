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
    return replace(
        GeneratorObservation(
            running=False,
            remote_on=False,
            load_connected=False,
            emergency_stop=False,
            ambient_temperature_external=20.0,
        ),
        **changes,
    )


def kinds(actions):
    return [action.kind for action in actions]


def test_complete_managed_start_warmup_cooldown_and_stop():
    controller = GeneratorController(profile())
    controller.step(0.0, observed(), False)
    assert controller.phase == GeneratorPhase.IDLE

    assert kinds(controller.step(1.0, observed(), True)) == [
        GeneratorActionKind.CHOKE_TO_COLD_START
    ]
    assert kinds(controller.step(1.5, observed(), True)) == [
        GeneratorActionKind.REMOTE_ON
    ]

    running = observed(running=True, remote_on=True)
    controller.step(2.0, running, True)
    assert controller.phase == GeneratorPhase.HOLDING_COLD_START_CHOKE
    assert kinds(controller.step(3.0, running, True)) == [
        GeneratorActionKind.CHOKE_TO_RUN
    ]
    controller.step(5.0, running, True)
    assert controller.status(running).ready_for_load is True

    controller.step(6.0, replace(running, load_connected=True), False)
    assert controller.phase == GeneratorPhase.WAITING_FOR_LOAD_RELEASE
    controller.step(7.0, running, False)
    assert controller.phase == GeneratorPhase.COOLING_DOWN
    assert kinds(controller.step(10.0, running, False)) == [
        GeneratorActionKind.REMOTE_OFF
    ]
    controller.step(11.0, observed(), False)
    assert controller.phase == GeneratorPhase.IDLE


def test_temperature_strategy_uses_run_position_when_warm():
    controller = GeneratorController(
        profile(choke_strategy=ChokeStrategy.TEMPERATURE, choke_temperature=10.0)
    )
    controller.step(0.0, observed(), False)
    actions = controller.step(1.0, observed(ambient_temperature_external=18.0), True)
    assert kinds(actions) == [GeneratorActionKind.CHOKE_TO_RUN]
    assert controller.start_temperature == 18.0


def test_unknown_temperature_uses_conservative_cold_start():
    controller = GeneratorController(profile(choke_strategy=ChokeStrategy.TEMPERATURE))
    controller.step(0.0, observed(), False)
    assert kinds(controller.step(
        1.0, observed(ambient_temperature_external=None), True
    )) == [GeneratorActionKind.CHOKE_TO_COLD_START]


def test_temperature_and_warmup_boundaries_are_explicit():
    configured = profile(
        choke_strategy=ChokeStrategy.TEMPERATURE,
        choke_temperature=10.0,
        warm_temperature=10.0,
        cool_temperature=-5.0,
        cold_temperature=-10.0,
        warmup_warm_seconds=30.0,
        warmup_cool_seconds=60.0,
        warmup_cold_seconds=180.0,
        warmup_very_cold_seconds=300.0,
    )
    assert configured.should_use_choke(10.0) is False
    assert configured.should_use_choke(9.9) is True
    assert configured.warmup_seconds(10.0) == 30.0
    assert configured.warmup_seconds(-4.9) == 60.0
    assert configured.warmup_seconds(-5.0) == 180.0
    assert configured.warmup_seconds(-10.0) == 300.0
    assert configured.warmup_seconds(None) == 300.0


def test_external_running_is_observed_but_never_captured():
    controller = GeneratorController(profile())
    external = observed(running=True, remote_on=False)
    assert controller.step(0.0, external, False) == []
    assert controller.phase == GeneratorPhase.EXTERNAL_RUNNING
    assert controller.step(1.0, external, True) == []
    assert controller.phase == GeneratorPhase.EXTERNAL_RUNNING
    controller.step(2.0, observed(), False, actions_allowed=False)
    assert controller.phase == GeneratorPhase.IDLE


def test_external_remote_request_is_observed_before_running():
    controller = GeneratorController(profile())
    external = observed(remote_on=True)
    controller.step(0.0, external, False)
    assert controller.phase == GeneratorPhase.EXTERNAL_RUNNING
    assert controller.step(1.0, external, True) == []


def test_restart_restores_only_stable_managed_generator():
    running = observed(running=True, remote_on=True, load_connected=True)

    managed = GeneratorController(profile())
    managed.step(0.0, running, True, stable_managed_session=True)
    assert managed.phase == GeneratorPhase.READY_FOR_LOAD

    external = GeneratorController(profile())
    external.step(0.0, running, True, stable_managed_session=False)
    assert external.phase == GeneratorPhase.EXTERNAL_RUNNING

    uncertain = GeneratorController(profile())
    uncertain.step(0.0, observed(remote_on=True), True)
    assert uncertain.phase == GeneratorPhase.RECOVERY_REQUIRED


def test_cancelled_start_removes_remote_and_opens_choke():
    controller = GeneratorController(profile())
    controller.step(0.0, observed(), False)
    controller.step(1.0, observed(), True)
    controller.step(1.5, observed(remote_on=True), True)
    assert kinds(controller.step(2.0, observed(remote_on=True), False)) == [
        GeneratorActionKind.REMOTE_OFF,
        GeneratorActionKind.CHOKE_TO_RUN,
    ]


def test_load_must_be_released_before_managed_stop():
    controller = GeneratorController(profile())
    running = observed(running=True, remote_on=True, load_connected=True)
    controller.step(0.0, running, True, stable_managed_session=True)
    for now in (1.0, 10.0, 100.0):
        assert GeneratorActionKind.REMOTE_OFF not in kinds(
            controller.step(now, running, False)
        )
    assert controller.phase == GeneratorPhase.WAITING_FOR_LOAD_RELEASE


def test_authorized_shutdown_cools_then_removes_remote():
    controller = GeneratorController(profile())
    running = observed(running=True, remote_on=True, load_connected=False)
    controller.step(0.0, running, True, stable_managed_session=True)

    actions, error = controller.step_authorized_shutdown(1.0, running, authorized=True)
    assert error is None and actions == []
    assert controller.phase == GeneratorPhase.COOLING_DOWN

    actions, error = controller.step_authorized_shutdown(4.0, running, authorized=True)
    assert error is None
    assert kinds(actions) == [GeneratorActionKind.REMOTE_OFF]

    actions, error = controller.step_authorized_shutdown(5.0, observed(), authorized=True)
    assert error is None and actions == []
    assert controller.phase == GeneratorPhase.IDLE


def test_authorized_shutdown_rejects_loaded_generator():
    controller = GeneratorController(profile())
    actions, error = controller.step_authorized_shutdown(
        1.0,
        observed(running=True, remote_on=True, load_connected=True),
        authorized=True,
    )
    assert actions == []
    assert error is not None


def test_unauthorized_external_shutdown_never_emits_command():
    controller = GeneratorController(profile())
    actions, error = controller.step_authorized_shutdown(
        1.0,
        observed(running=True, remote_on=True, load_connected=False),
        authorized=False,
    )
    assert actions == []
    assert error is not None
    assert controller.phase == GeneratorPhase.EXTERNAL_RUNNING


def test_start_timeout_enters_fault_and_requests_safe_outputs():
    controller = GeneratorController(profile())
    controller.step(0.0, observed(), False)
    controller.step(1.0, observed(), True)
    controller.step(1.5, observed(remote_on=True), True)
    actions = controller.step(5.6, observed(remote_on=True), True)
    assert kinds(actions) == [
        GeneratorActionKind.REMOTE_OFF,
        GeneratorActionKind.CHOKE_TO_RUN,
    ]
    assert controller.phase == GeneratorPhase.FAULT


def test_unknown_state_freezes_current_phase():
    controller = GeneratorController(profile())
    running = observed(running=True, remote_on=True)
    controller.step(0.0, running, True, stable_managed_session=True)
    assert controller.phase == GeneratorPhase.READY_FOR_LOAD
    assert controller.step(1.0, observed(running=None), True) == []
    assert controller.phase == GeneratorPhase.READY_FOR_LOAD

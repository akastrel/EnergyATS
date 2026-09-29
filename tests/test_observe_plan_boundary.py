from __future__ import annotations

from dataclasses import replace

from domain import GeneratorSlot, PowerSource
from generator_controller import (
    GeneratorActionKind,
    GeneratorController,
    GeneratorObservation,
    GeneratorPhase,
    default_generator_profiles,
)
from power_transfer import (
    PowerTransferController,
    PowerTransferObservation,
    TransferActionKind,
    TransferPhase,
)


def generator_observation(**changes) -> GeneratorObservation:
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


def transfer_observation(**changes) -> PowerTransferObservation:
    return replace(
        PowerTransferObservation(
            grid_ready=True,
            house_on_grid=True,
            house_on_generator=False,
            grid_connected=True,
            generator_selected=False,
            emergency_stop=False,
        ),
        **changes,
    )


def test_generator_observe_does_not_plan_start_action():
    controller = GeneratorController(default_generator_profiles()[GeneratorSlot.A])
    stopped = generator_observation()

    assert controller.observe(0.0, stopped) is None
    assert controller.phase == GeneratorPhase.IDLE

    assert controller.plan(1.0, stopped, desired_running=False) == []
    actions = controller.plan(1.0, stopped, desired_running=True)

    assert [action.kind for action in actions] == [
        GeneratorActionKind.CHOKE_TO_COLD_START
    ]
    assert controller.phase == GeneratorPhase.PREPARING


def test_generator_observe_can_latch_physical_fault_without_action():
    controller = GeneratorController(default_generator_profiles()[GeneratorSlot.A])

    assert controller.observe(
        0.0,
        generator_observation(emergency_stop=True),
    ) is None
    assert controller.phase == GeneratorPhase.FAULT
    assert controller.plan(
        1.0,
        generator_observation(emergency_stop=True),
        desired_running=True,
    ) == []


def test_transfer_observe_settles_feedback_before_plan_selects_next_step():
    controller = PowerTransferController(confirmation_timeout=10.0)
    grid = transfer_observation()

    assert controller.observe(0.0, grid) is None
    assert controller.phase == TransferPhase.STABLE_GRID

    actions = controller.plan(
        1.0,
        grid,
        PowerSource.GENERATOR,
        desired_generator_ready=True,
    )
    assert [action.kind for action in actions] == [
        TransferActionKind.DISCONNECT_GRID
    ]

    grid_off = transfer_observation(
        house_on_grid=False,
        grid_connected=False,
    )
    assert controller.observe(2.0, grid_off) is None
    assert controller.phase == TransferPhase.STABLE_ISOLATED

    actions = controller.plan(
        2.0,
        grid_off,
        PowerSource.GENERATOR,
        desired_generator_ready=True,
    )
    assert [action.kind for action in actions] == [
        TransferActionKind.SELECT_GENERATOR
    ]


def test_transfer_observe_never_starts_new_transfer_by_itself():
    controller = PowerTransferController()
    grid = transfer_observation()

    controller.observe(0.0, grid)
    controller.observe(1.0, grid)

    assert controller.phase == TransferPhase.STABLE_GRID
    assert controller.deadline is None

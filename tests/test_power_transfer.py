from dataclasses import replace

from domain import GeneratorSlot, PowerPath, PowerSource
from power_transfer import (
    PowerTransferController,
    PowerTransferObservation,
    TransferActionKind,
    TransferPhase,
)


def observed(
    *,
    grid_ready=True,
    house_on_grid=True,
    house_on_generator=False,
    grid_connected=True,
    generator_selected=False,
    active_generator=None,
    emergency_stop=False,
):
    return PowerTransferObservation(
        grid_ready=grid_ready,
        house_on_grid=house_on_grid,
        house_on_generator=house_on_generator,
        grid_connected=grid_connected,
        generator_selected=generator_selected,
        active_generator=active_generator,
        emergency_stop=emergency_stop,
    )


def kinds(actions):
    return [action.kind for action in actions]


def test_initial_grid_topology_is_recognized():
    controller = PowerTransferController()

    actions = controller.step(
        0.0,
        observed(),
        None,
        desired_generator_ready=False,
    )

    assert actions == []
    assert controller.phase == TransferPhase.STABLE_GRID_PATH
    assert controller.status().actual_path == PowerPath.GRID
    assert controller.status().actual_source == PowerSource.GRID


def test_grid_path_without_physical_grid_is_ups_only():
    controller = PowerTransferController()
    no_grid = observed(
        grid_ready=False,
        house_on_grid=False,
        grid_connected=True,
    )

    controller.step(
        0.0,
        no_grid,
        None,
        desired_generator_ready=False,
    )

    assert controller.status().actual_path == PowerPath.GRID
    assert controller.status().actual_source == PowerSource.UPS_ONLY


def test_deliberately_disconnected_grid_is_isolated_not_battery_path():
    controller = PowerTransferController()
    isolated = observed(
        grid_ready=True,
        house_on_grid=False,
        grid_connected=False,
    )

    controller.step(
        0.0,
        isolated,
        None,
        desired_generator_ready=False,
    )

    assert controller.phase == TransferPhase.STABLE_ISOLATED
    assert controller.status().actual_path == PowerPath.ISOLATED
    assert controller.status().actual_source == PowerSource.UPS_ONLY


def test_hold_does_not_reconnect_manually_disabled_grid():
    controller = PowerTransferController()
    isolated = observed(
        grid_ready=True,
        house_on_grid=False,
        grid_connected=False,
    )
    controller.step(0.0, isolated, None, desired_generator_ready=False)

    actions = controller.step(
        1.0,
        isolated,
        None,
        desired_generator_ready=False,
    )

    assert actions == []
    assert controller.status().actual_path == PowerPath.ISOLATED


def test_transfer_grid_to_generator_is_break_before_make():
    controller = PowerTransferController(confirmation_timeout=10.0)
    controller.step(0.0, observed(), None, desired_generator_ready=False)

    actions = controller.step(
        1.0,
        observed(),
        PowerSource.GENERATOR_A,
        desired_generator_ready=True,
    )
    assert kinds(actions) == [TransferActionKind.DISCONNECT_GRID]
    assert controller.phase == TransferPhase.DISCONNECTING_GRID

    grid_off = observed(grid_connected=False, house_on_grid=False)
    actions = controller.step(
        2.0,
        grid_off,
        PowerSource.GENERATOR_A,
        desired_generator_ready=True,
    )
    assert kinds(actions) == [TransferActionKind.SELECT_GENERATOR]
    assert controller.phase == TransferPhase.SELECTING_GENERATOR

    generator_on = replace(
        grid_off,
        generator_selected=True,
        house_on_generator=True,
        active_generator=GeneratorSlot.A,
    )
    actions = controller.step(
        3.0,
        generator_on,
        PowerSource.GENERATOR_A,
        desired_generator_ready=True,
    )
    assert actions == []
    assert controller.phase == TransferPhase.STABLE_GENERATOR
    assert controller.status().actual_source == PowerSource.GENERATOR_A


def test_transfer_waits_until_generator_ready():
    controller = PowerTransferController()
    controller.step(0.0, observed(), None, desired_generator_ready=False)

    actions = controller.step(
        1.0,
        observed(),
        PowerSource.GENERATOR_A,
        desired_generator_ready=False,
    )

    assert actions == []
    assert controller.status().actual_path == PowerPath.GRID


def test_tpc_does_not_reject_external_generator_bus_owner():
    controller = PowerTransferController()
    on_b = observed(
        grid_ready=False,
        house_on_grid=False,
        house_on_generator=True,
        grid_connected=False,
        generator_selected=True,
        active_generator=GeneratorSlot.B,
    )

    controller.step(
        0.0,
        on_b,
        PowerSource.GENERATOR_A,
        desired_generator_ready=True,
    )

    # Выбор A/B принадлежит аппаратной FIFO-схеме, не TPC.
    assert controller.phase == TransferPhase.STABLE_GENERATOR
    assert controller.status().actual_source == PowerSource.GENERATOR_B
    assert controller.status().recovery_required is False


def test_generator_bus_with_unknown_owner_is_still_valid_generator_path():
    controller = PowerTransferController()
    generator = observed(
        grid_ready=False,
        house_on_grid=False,
        house_on_generator=True,
        grid_connected=False,
        generator_selected=True,
        active_generator=None,
    )

    controller.step(
        0.0,
        generator,
        None,
        desired_generator_ready=False,
    )

    assert controller.phase == TransferPhase.STABLE_GENERATOR
    assert controller.status().actual_source == PowerSource.GENERATOR


def test_return_generator_to_grid_is_break_before_make():
    controller = PowerTransferController(confirmation_timeout=10.0)
    on_generator = observed(
        grid_ready=True,
        house_on_grid=False,
        house_on_generator=True,
        grid_connected=False,
        generator_selected=True,
        active_generator=GeneratorSlot.A,
    )
    controller.step(
        0.0,
        on_generator,
        None,
        desired_generator_ready=False,
    )

    actions = controller.step(
        1.0,
        on_generator,
        PowerSource.GRID,
        desired_generator_ready=False,
    )
    assert kinds(actions) == [TransferActionKind.DESELECT_GENERATOR]

    generator_off = replace(
        on_generator,
        generator_selected=False,
        house_on_generator=False,
        active_generator=None,
    )
    actions = controller.step(
        2.0,
        generator_off,
        PowerSource.GRID,
        desired_generator_ready=False,
    )
    assert kinds(actions) == [TransferActionKind.CONNECT_GRID]

    on_grid = replace(
        generator_off,
        grid_connected=True,
        house_on_grid=True,
    )
    actions = controller.step(
        3.0,
        on_grid,
        PowerSource.GRID,
        desired_generator_ready=False,
    )
    assert actions == []
    assert controller.status().actual_source == PowerSource.GRID
    assert controller.status().actual_path == PowerPath.GRID


def test_return_to_grid_path_without_grid_voltage_results_in_ups_only():
    controller = PowerTransferController(confirmation_timeout=10.0)
    on_generator = observed(
        grid_ready=False,
        house_on_grid=False,
        house_on_generator=True,
        grid_connected=False,
        generator_selected=True,
        active_generator=GeneratorSlot.A,
    )
    controller.step(0.0, on_generator, None, desired_generator_ready=False)

    assert kinds(
        controller.step(
            1.0,
            on_generator,
            PowerSource.GRID,
            desired_generator_ready=False,
        )
    ) == [TransferActionKind.DESELECT_GENERATOR]

    generator_off = replace(
        on_generator,
        generator_selected=False,
        house_on_generator=False,
        active_generator=None,
    )
    assert kinds(
        controller.step(
            2.0,
            generator_off,
            PowerSource.GRID,
            desired_generator_ready=False,
        )
    ) == [TransferActionKind.CONNECT_GRID]

    grid_path_without_voltage = replace(generator_off, grid_connected=True)
    controller.step(
        3.0,
        grid_path_without_voltage,
        PowerSource.GRID,
        desired_generator_ready=False,
    )

    assert controller.status().actual_path == PowerPath.GRID
    assert controller.status().actual_source == PowerSource.UPS_ONLY


def test_explicit_ups_only_target_isolates_grid():
    controller = PowerTransferController(confirmation_timeout=10.0)
    controller.step(0.0, observed(), None, desired_generator_ready=False)

    actions = controller.step(
        1.0,
        observed(),
        PowerSource.UPS_ONLY,
        desired_generator_ready=False,
    )
    assert kinds(actions) == [TransferActionKind.DISCONNECT_GRID]

    isolated = observed(
        grid_ready=True,
        house_on_grid=False,
        grid_connected=False,
    )
    controller.step(
        2.0,
        isolated,
        PowerSource.UPS_ONLY,
        desired_generator_ready=False,
    )

    assert controller.phase == TransferPhase.STABLE_ISOLATED
    assert controller.status().actual_source == PowerSource.UPS_ONLY


def test_simultaneous_confirmation_of_grid_and_generator_requires_recovery():
    controller = PowerTransferController()
    controller.step(0.0, observed(), None, desired_generator_ready=False)

    overlap = observed(
        house_on_grid=True,
        house_on_generator=True,
        grid_connected=True,
        generator_selected=True,
        active_generator=GeneratorSlot.A,
    )
    actions = controller.step(
        1.0,
        overlap,
        None,
        desired_generator_ready=False,
    )

    assert actions == []
    assert controller.phase == TransferPhase.RECOVERY_REQUIRED


def test_confirmation_timeout_requires_recovery():
    controller = PowerTransferController(confirmation_timeout=2.0)
    controller.step(0.0, observed(), None, desired_generator_ready=False)
    controller.step(
        1.0,
        observed(),
        PowerSource.GENERATOR_A,
        desired_generator_ready=True,
    )

    # На 3.1 с подтверждения grid-off всё ещё нет.
    controller.step(
        3.1,
        observed(),
        PowerSource.GENERATOR_A,
        desired_generator_ready=True,
    )

    assert controller.phase == TransferPhase.RECOVERY_REQUIRED
    assert controller.status().fault is not None


def test_recovery_never_selects_generator():
    controller = PowerTransferController()
    controller.begin_recovery_to_grid_path()
    on_generator = observed(
        grid_ready=True,
        house_on_grid=False,
        house_on_generator=True,
        grid_connected=False,
        generator_selected=True,
        active_generator=GeneratorSlot.A,
    )

    actions, error = controller.step_recovery_to_grid_path(1.0, on_generator)

    assert error is None
    assert kinds(actions) == [TransferActionKind.DESELECT_GENERATOR]
    assert TransferActionKind.SELECT_GENERATOR not in kinds(actions)

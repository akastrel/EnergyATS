from dataclasses import replace

from domain import PowerPath, PowerSource
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
    emergency_stop=False,
):
    return PowerTransferObservation(
        grid_ready=grid_ready,
        house_on_grid=house_on_grid,
        house_on_generator=house_on_generator,
        grid_connected=grid_connected,
        generator_selected=generator_selected,
        emergency_stop=emergency_stop,
    )


def kinds(actions):
    return [action.kind for action in actions]


def test_initial_grid_topology_is_recognized():
    controller = PowerTransferController()
    assert controller.step(0.0, observed(), None, desired_generator_ready=False) == []
    assert controller.phase == TransferPhase.STABLE_GRID
    assert controller.status().actual_path == PowerPath.GRID
    assert controller.status().actual_source == PowerSource.GRID


def test_grid_path_without_physical_grid_is_ups_only():
    controller = PowerTransferController()
    no_grid = observed(grid_ready=False, house_on_grid=False, grid_connected=True)
    controller.step(0.0, no_grid, None, desired_generator_ready=False)
    assert controller.status().actual_path == PowerPath.GRID
    assert controller.status().actual_source == PowerSource.UPS_ONLY


def test_deliberately_disconnected_grid_is_isolated():
    controller = PowerTransferController()
    isolated = observed(grid_ready=True, house_on_grid=False, grid_connected=False)
    controller.step(0.0, isolated, None, desired_generator_ready=False)
    assert controller.phase == TransferPhase.STABLE_ISOLATED
    assert controller.status().actual_path == PowerPath.ISOLATED
    assert controller.status().actual_source == PowerSource.UPS_ONLY


def test_hold_does_not_reconnect_manually_disabled_grid():
    controller = PowerTransferController()
    isolated = observed(grid_ready=True, house_on_grid=False, grid_connected=False)
    controller.step(0.0, isolated, None, desired_generator_ready=False)
    assert controller.step(1.0, isolated, None, desired_generator_ready=False) == []
    assert controller.status().actual_path == PowerPath.ISOLATED


def test_transfer_grid_to_generator_is_break_before_make():
    controller = PowerTransferController(confirmation_timeout=10.0)
    controller.step(0.0, observed(), None, desired_generator_ready=False)

    actions = controller.step(
        1.0, observed(), PowerSource.GENERATOR, desired_generator_ready=True
    )
    assert kinds(actions) == [TransferActionKind.DISCONNECT_GRID]

    grid_off = observed(grid_connected=False, house_on_grid=False)
    actions = controller.step(
        2.0, grid_off, PowerSource.GENERATOR, desired_generator_ready=True
    )
    assert kinds(actions) == [TransferActionKind.SELECT_GENERATOR]

    generator_on = replace(
        grid_off,
        generator_selected=True,
        house_on_generator=True,
    )
    assert controller.step(
        3.0,
        generator_on,
        PowerSource.GENERATOR,
        desired_generator_ready=True,
    ) == []
    assert controller.phase == TransferPhase.STABLE_GENERATOR
    assert controller.status().actual_source == PowerSource.GENERATOR


def test_transfer_waits_until_generator_ready():
    controller = PowerTransferController()
    controller.step(0.0, observed(), None, desired_generator_ready=False)
    assert controller.step(
        1.0, observed(), PowerSource.GENERATOR, desired_generator_ready=False
    ) == []
    assert controller.status().actual_path == PowerPath.GRID


def test_tpc_accepts_generator_path_without_knowing_bus_owner():
    controller = PowerTransferController()
    generator = observed(
        grid_ready=False,
        house_on_grid=False,
        house_on_generator=True,
        grid_connected=False,
        generator_selected=True,
    )
    controller.step(0.0, generator, None, desired_generator_ready=False)
    assert controller.phase == TransferPhase.STABLE_GENERATOR
    assert controller.status().actual_source == PowerSource.GENERATOR
    assert controller.status().recovery_required is False


def test_return_generator_to_grid_is_break_before_make():
    controller = PowerTransferController(confirmation_timeout=10.0)
    on_generator = observed(
        grid_ready=True,
        house_on_grid=False,
        house_on_generator=True,
        grid_connected=False,
        generator_selected=True,
    )
    controller.step(0.0, on_generator, None, desired_generator_ready=False)

    actions = controller.step(
        1.0, on_generator, PowerSource.GRID, desired_generator_ready=False
    )
    assert kinds(actions) == [TransferActionKind.DESELECT_GENERATOR]

    generator_off = replace(
        on_generator,
        generator_selected=False,
        house_on_generator=False,
    )
    actions = controller.step(
        2.0, generator_off, PowerSource.GRID, desired_generator_ready=False
    )
    assert kinds(actions) == [TransferActionKind.CONNECT_GRID]

    on_grid = replace(generator_off, grid_connected=True, house_on_grid=True)
    assert controller.step(
        3.0, on_grid, PowerSource.GRID, desired_generator_ready=False
    ) == []
    assert controller.status().actual_source == PowerSource.GRID


def test_return_to_grid_path_without_grid_voltage_results_in_ups_only():
    controller = PowerTransferController(confirmation_timeout=10.0)
    on_generator = observed(
        grid_ready=False,
        house_on_grid=False,
        house_on_generator=True,
        grid_connected=False,
        generator_selected=True,
    )
    controller.step(0.0, on_generator, None, desired_generator_ready=False)
    assert kinds(controller.step(
        1.0, on_generator, PowerSource.GRID, desired_generator_ready=False
    )) == [TransferActionKind.DESELECT_GENERATOR]

    generator_off = replace(
        on_generator,
        generator_selected=False,
        house_on_generator=False,
    )
    assert kinds(controller.step(
        2.0, generator_off, PowerSource.GRID, desired_generator_ready=False
    )) == [TransferActionKind.CONNECT_GRID]

    controller.step(
        3.0,
        replace(generator_off, grid_connected=True),
        PowerSource.GRID,
        desired_generator_ready=False,
    )
    assert controller.status().actual_path == PowerPath.GRID
    assert controller.status().actual_source == PowerSource.UPS_ONLY


def test_explicit_ups_only_target_isolates_grid():
    controller = PowerTransferController(confirmation_timeout=10.0)
    controller.step(0.0, observed(), None, desired_generator_ready=False)
    assert kinds(controller.step(
        1.0, observed(), PowerSource.UPS_ONLY, desired_generator_ready=False
    )) == [TransferActionKind.DISCONNECT_GRID]

    isolated = observed(grid_ready=True, house_on_grid=False, grid_connected=False)
    controller.step(
        2.0, isolated, PowerSource.UPS_ONLY, desired_generator_ready=False
    )
    assert controller.phase == TransferPhase.STABLE_ISOLATED
    assert controller.status().actual_source == PowerSource.UPS_ONLY


def test_simultaneous_grid_and_generator_confirmation_requires_recovery():
    controller = PowerTransferController()
    controller.step(0.0, observed(), None, desired_generator_ready=False)
    overlap = observed(
        house_on_grid=True,
        house_on_generator=True,
        grid_connected=True,
        generator_selected=True,
    )
    assert controller.step(
        1.0, overlap, None, desired_generator_ready=False
    ) == []
    assert controller.phase == TransferPhase.RECOVERY_REQUIRED


def test_confirmation_timeout_requires_recovery():
    controller = PowerTransferController(confirmation_timeout=2.0)
    controller.step(0.0, observed(), None, desired_generator_ready=False)
    controller.step(
        1.0, observed(), PowerSource.GENERATOR, desired_generator_ready=True
    )
    controller.step(
        3.1, observed(), PowerSource.GENERATOR, desired_generator_ready=True
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
    )
    actions, error = controller.step_recovery_to_grid_path(1.0, on_generator)
    assert error is None
    assert kinds(actions) == [TransferActionKind.DESELECT_GENERATOR]
    assert TransferActionKind.SELECT_GENERATOR not in kinds(actions)

from __future__ import annotations

from dataclasses import replace

from domain import GeneratorSlot, PowerPath, PowerSource
from power_transfer import (
    PowerTransferController,
    PowerTransferObservation,
    TransferActionKind,
    TransferPhase,
)


def observed(**changes) -> PowerTransferObservation:
    base = PowerTransferObservation(
        grid_ready=True,
        house_on_grid=True,
        house_on_generator=False,
        grid_connected=True,
        generator_selected=False,
        active_generator=None,
        emergency_stop=False,
    )
    return replace(base, **changes)


def kinds(actions):
    return [action.kind for action in actions]


def test_break_before_make_to_generator_and_back_to_grid():
    controller = PowerTransferController(confirmation_timeout=10.0)
    controller.step(0.0, observed(), PowerSource.GRID, desired_generator_ready=False)
    assert controller.phase == TransferPhase.STABLE_GRID_PATH

    # READY — обязательное условие даже для начала переключения.
    assert (
        controller.step(
            1.0,
            observed(),
            PowerSource.GENERATOR_A,
            desired_generator_ready=False,
        )
        == []
    )

    actions = controller.step(
        2.0,
        observed(),
        PowerSource.GENERATOR_A,
        desired_generator_ready=True,
    )
    assert kinds(actions) == [TransferActionKind.DISCONNECT_GRID]

    grid_off = observed(grid_connected=False, house_on_grid=False)
    actions = controller.step(
        3.0,
        grid_off,
        PowerSource.GENERATOR_A,
        desired_generator_ready=True,
    )
    assert kinds(actions) == [TransferActionKind.SELECT_GENERATOR]

    on_generator = replace(
        grid_off,
        generator_selected=True,
        house_on_generator=True,
        active_generator=GeneratorSlot.A,
    )
    controller.step(
        4.0,
        on_generator,
        PowerSource.GENERATOR_A,
        desired_generator_ready=True,
    )
    assert controller.status().actual_source == PowerSource.GENERATOR_A

    actions = controller.step(
        5.0,
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
        6.0,
        generator_off,
        PowerSource.GRID,
        desired_generator_ready=False,
    )
    assert kinds(actions) == [TransferActionKind.CONNECT_GRID]

    controller.step(
        7.0,
        observed(),
        PowerSource.GRID,
        desired_generator_ready=False,
    )
    assert controller.phase == TransferPhase.STABLE_GRID_PATH


def test_return_without_grid_selects_battery_path():
    controller = PowerTransferController(confirmation_timeout=10.0)
    on_generator = observed(
        grid_ready=False,
        house_on_grid=False,
        house_on_generator=True,
        grid_connected=False,
        generator_selected=True,
        active_generator=GeneratorSlot.A,
    )
    controller.step(
        0.0,
        on_generator,
        PowerSource.GENERATOR_A,
        desired_generator_ready=True,
    )

    actions = controller.step(
        1.0,
        on_generator,
        PowerSource.BATTERY,
        desired_generator_ready=False,
    )
    assert kinds(actions) == [TransferActionKind.DESELECT_GENERATOR]

    disconnected = replace(
        on_generator,
        generator_selected=False,
        house_on_generator=False,
        active_generator=None,
    )
    actions = controller.step(
        2.0,
        disconnected,
        PowerSource.BATTERY,
        desired_generator_ready=False,
    )
    assert actions == []
    assert controller.phase == TransferPhase.STABLE_BATTERY_PATH
    assert controller.status().actual_source == PowerSource.BATTERY
    assert controller.status().actual_path == PowerPath.BATTERY


def test_transfer_timeout_requires_recovery_without_guessing():
    controller = PowerTransferController(confirmation_timeout=2.0)
    controller.step(0.0, observed(), PowerSource.GRID, desired_generator_ready=False)
    controller.step(
        1.0,
        observed(),
        PowerSource.GENERATOR_A,
        desired_generator_ready=True,
    )
    controller.step(
        3.1,
        observed(),
        PowerSource.GENERATOR_A,
        desired_generator_ready=True,
    )
    assert controller.phase == TransferPhase.RECOVERY_REQUIRED
    assert controller.status().recovery_required is True


def test_external_physical_change_is_observed_without_a_command():
    controller = PowerTransferController()
    controller.step(0.0, observed(), PowerSource.GRID, desired_generator_ready=False)
    external = observed(
        house_on_grid=False,
        house_on_generator=True,
        grid_connected=False,
        generator_selected=True,
        active_generator=GeneratorSlot.B,
    )
    actions = controller.step(
        1.0,
        external,
        PowerSource.GRID,
        desired_generator_ready=False,
        actions_allowed=False,
    )
    assert actions == []
    assert controller.status().actual_source == PowerSource.GENERATOR_B


def test_spontaneous_generator_selection_requires_recovery_when_managed():
    controller = PowerTransferController()
    controller.step(0.0, observed(), PowerSource.GRID, desired_generator_ready=False)
    spontaneous = observed(
        house_on_grid=False,
        house_on_generator=True,
        grid_connected=False,
        generator_selected=True,
        active_generator=GeneratorSlot.B,
    )

    actions = controller.step(
        1.0,
        spontaneous,
        PowerSource.GRID,
        desired_generator_ready=False,
        actions_allowed=True,
    )

    assert actions == []
    assert controller.phase == TransferPhase.RECOVERY_REQUIRED
    assert "до команды" in (controller.status().fault or "")


def test_temporary_unavailable_state_does_not_erase_transition():
    controller = PowerTransferController(confirmation_timeout=10.0)
    controller.step(0.0, observed(), PowerSource.GRID, desired_generator_ready=False)
    controller.step(
        1.0,
        observed(),
        PowerSource.GENERATOR_A,
        desired_generator_ready=True,
    )
    assert controller.phase == TransferPhase.DISCONNECTING_GRID

    unavailable = observed(house_on_grid=None)
    assert (
        controller.step(
            2.0,
            unavailable,
            PowerSource.GENERATOR_A,
            desired_generator_ready=True,
        )
        == []
    )
    assert controller.phase == TransferPhase.DISCONNECTING_GRID


def test_confirmation_on_timeout_boundary_is_accepted():
    controller = PowerTransferController(confirmation_timeout=2.0)
    controller.step(0.0, observed(), PowerSource.GRID, desired_generator_ready=False)
    controller.step(
        1.0,
        observed(),
        PowerSource.GENERATOR_A,
        desired_generator_ready=True,
    )

    actions = controller.step(
        3.0,
        observed(grid_connected=False, house_on_grid=False),
        PowerSource.GENERATOR_A,
        desired_generator_ready=True,
    )

    assert kinds(actions) == [TransferActionKind.SELECT_GENERATOR]
    assert controller.phase == TransferPhase.SELECTING_GENERATOR


def test_wrong_generator_on_shared_bus_requires_recovery():
    controller = PowerTransferController()
    on_b = observed(
        house_on_grid=False,
        house_on_generator=True,
        grid_connected=False,
        generator_selected=True,
        active_generator=GeneratorSlot.B,
    )
    controller.step(
        0.0,
        on_b,
        PowerSource.GENERATOR_B,
        desired_generator_ready=True,
    )

    controller.step(
        1.0,
        on_b,
        PowerSource.GENERATOR_A,
        desired_generator_ready=True,
    )

    assert controller.phase == TransferPhase.RECOVERY_REQUIRED
    assert "не тот генератор" in (controller.status().fault or "")


def test_overlapping_grid_and_generator_inputs_require_recovery():
    controller = PowerTransferController()
    controller.step(0.0, observed(), PowerSource.GRID, desired_generator_ready=False)

    actions = controller.step(
        1.0,
        observed(grid_connected=True, generator_selected=True),
        PowerSource.GRID,
        desired_generator_ready=False,
    )

    assert actions == []
    assert controller.phase == TransferPhase.RECOVERY_REQUIRED


def test_generator_confirmation_requires_the_requested_running_unit():
    controller = PowerTransferController(confirmation_timeout=10.0)
    controller.step(0.0, observed(), PowerSource.GRID, desired_generator_ready=False)
    controller.step(
        1.0,
        observed(),
        PowerSource.GENERATOR_A,
        desired_generator_ready=True,
    )
    grid_off = observed(grid_connected=False, house_on_grid=False)
    controller.step(
        2.0,
        grid_off,
        PowerSource.GENERATOR_A,
        desired_generator_ready=True,
    )

    wrong_source = replace(
        grid_off,
        generator_selected=True,
        house_on_generator=True,
        active_generator=GeneratorSlot.B,
    )
    controller.step(
        3.0,
        wrong_source,
        PowerSource.GENERATOR_A,
        desired_generator_ready=True,
    )

    assert controller.phase == TransferPhase.RECOVERY_REQUIRED


def test_lost_house_power_feedback_requires_recovery_after_timeout():
    controller = PowerTransferController(confirmation_timeout=2.0)
    on_a = observed(
        house_on_grid=False,
        house_on_generator=True,
        grid_connected=False,
        generator_selected=True,
        active_generator=GeneratorSlot.A,
    )
    controller.step(
        0.0,
        on_a,
        PowerSource.GENERATOR_A,
        desired_generator_ready=True,
    )

    missing_feedback = replace(on_a, house_on_generator=False)
    controller.step(
        1.0,
        missing_feedback,
        PowerSource.GENERATOR_A,
        desired_generator_ready=True,
    )
    assert controller.phase == TransferPhase.STABLE_GENERATOR

    controller.step(
        3.0,
        missing_feedback,
        PowerSource.GENERATOR_A,
        desired_generator_ready=True,
    )

    assert controller.phase == TransferPhase.RECOVERY_REQUIRED


def test_failed_generator_can_still_be_isolated_with_missing_power_feedback():
    controller = PowerTransferController(confirmation_timeout=2.0)
    on_a = observed(
        house_on_grid=False,
        house_on_generator=True,
        grid_connected=False,
        generator_selected=True,
        active_generator=GeneratorSlot.A,
    )
    controller.step(
        0.0,
        on_a,
        PowerSource.GENERATOR_A,
        desired_generator_ready=True,
    )
    failed = replace(
        on_a,
        house_on_generator=False,
        active_generator=None,
    )

    actions = controller.step(
        1.0,
        failed,
        PowerSource.BATTERY,
        desired_generator_ready=False,
    )

    assert kinds(actions) == [TransferActionKind.DESELECT_GENERATOR]


def test_battery_source_is_distinguished_on_grid_and_battery_paths():
    grid_path = observed(
        grid_ready=False,
        house_on_grid=False,
        grid_connected=True,
    )
    controller = PowerTransferController()
    controller.step(
        0.0,
        grid_path,
        PowerSource.BATTERY,
        desired_generator_ready=False,
        actions_allowed=False,
    )
    assert controller.status().actual_source == PowerSource.BATTERY
    assert controller.status().actual_path == PowerPath.GRID

    battery_path = replace(grid_path, grid_connected=False)
    controller = PowerTransferController()
    controller.step(
        0.0,
        battery_path,
        PowerSource.BATTERY,
        desired_generator_ready=False,
        actions_allowed=False,
    )
    assert controller.status().actual_source == PowerSource.BATTERY
    assert controller.status().actual_path == PowerPath.BATTERY


def test_explicit_battery_target_disconnects_available_grid_and_can_return():
    controller = PowerTransferController(confirmation_timeout=10.0)
    controller.step(0.0, observed(), PowerSource.GRID, desired_generator_ready=False)

    actions = controller.step(
        1.0,
        observed(),
        PowerSource.BATTERY,
        desired_generator_ready=False,
    )
    assert kinds(actions) == [TransferActionKind.DISCONNECT_GRID]

    battery_path = observed(grid_connected=False, house_on_grid=False)
    actions = controller.step(
        2.0,
        battery_path,
        PowerSource.BATTERY,
        desired_generator_ready=False,
    )
    assert actions == []
    assert controller.phase == TransferPhase.STABLE_BATTERY_PATH
    assert controller.status().actual_path == PowerPath.BATTERY

    actions = controller.step(
        3.0,
        battery_path,
        PowerSource.GRID,
        desired_generator_ready=False,
    )
    assert kinds(actions) == [TransferActionKind.CONNECT_GRID]

    controller.step(
        4.0,
        observed(),
        PowerSource.GRID,
        desired_generator_ready=False,
    )
    assert controller.phase == TransferPhase.STABLE_GRID_PATH
    assert controller.status().actual_source == PowerSource.GRID
    assert controller.status().actual_path == PowerPath.GRID


def test_generator_is_selected_directly_from_battery_path():
    battery_path = observed(grid_connected=False, house_on_grid=False)
    controller = PowerTransferController()
    controller.step(
        0.0,
        battery_path,
        PowerSource.BATTERY,
        desired_generator_ready=False,
    )

    actions = controller.step(
        1.0,
        battery_path,
        PowerSource.GENERATOR_A,
        desired_generator_ready=True,
    )
    assert kinds(actions) == [TransferActionKind.SELECT_GENERATOR]


def test_changed_target_finishes_grid_disconnect_before_reconnecting_grid():
    controller = PowerTransferController()
    controller.step(0.0, observed(), PowerSource.GRID, desired_generator_ready=False)
    controller.step(
        1.0,
        observed(),
        PowerSource.GENERATOR_A,
        desired_generator_ready=True,
    )

    grid_disconnected = observed(grid_connected=False, house_on_grid=False)
    actions = controller.step(
        2.0,
        grid_disconnected,
        PowerSource.GRID,
        desired_generator_ready=False,
    )
    assert kinds(actions) == [TransferActionKind.CONNECT_GRID]
    assert controller.phase == TransferPhase.CONNECTING_GRID


def test_changed_target_after_select_confirmation_deselects_generator():
    controller = PowerTransferController()
    controller.step(0.0, observed(), PowerSource.GRID, desired_generator_ready=False)
    controller.step(
        1.0,
        observed(),
        PowerSource.GENERATOR_A,
        desired_generator_ready=True,
    )
    grid_disconnected = observed(grid_connected=False, house_on_grid=False)
    controller.step(
        2.0,
        grid_disconnected,
        PowerSource.GENERATOR_A,
        desired_generator_ready=True,
    )

    selected = replace(
        grid_disconnected,
        generator_selected=True,
        house_on_generator=True,
        active_generator=GeneratorSlot.A,
    )
    actions = controller.step(
        3.0,
        selected,
        PowerSource.BATTERY,
        desired_generator_ready=False,
    )
    assert kinds(actions) == [TransferActionKind.DESELECT_GENERATOR]
    assert controller.phase == TransferPhase.DISCONNECTING_GENERATOR


def test_changed_target_waits_for_grid_connect_before_disconnect_command():
    battery_path = observed(grid_connected=False, house_on_grid=False)
    controller = PowerTransferController()
    controller.step(
        0.0,
        battery_path,
        PowerSource.BATTERY,
        desired_generator_ready=False,
    )
    controller.step(
        1.0,
        battery_path,
        PowerSource.GRID,
        desired_generator_ready=False,
    )

    actions = controller.step(
        2.0,
        battery_path,
        PowerSource.BATTERY,
        desired_generator_ready=False,
    )
    assert actions == []
    assert controller.phase == TransferPhase.CONNECTING_GRID

    actions = controller.step(
        3.0,
        observed(),
        PowerSource.BATTERY,
        desired_generator_ready=False,
    )
    assert kinds(actions) == [TransferActionKind.DISCONNECT_GRID]

"""Физический event journal: входы/feedback без повторения каждого control tick."""

from __future__ import annotations

from domain import GeneratorSlot, PowerPath, PowerSource
from generator_bus import GeneratorBusOwner
from physical_event_log import PhysicalEventTracker
from user_messages import user_message


NAMES = {
    GeneratorSlot.A: "Elemax",
    GeneratorSlot.B: "Вепрь",
}


def observe(
    tracker: PhysicalEventTracker,
    *,
    grid_ready=True,
    automatic=True,
    path=PowerPath.GRID,
    source=PowerSource.GRID,
    emergency=False,
    a_running=False,
    a_remote=False,
    b_running=False,
    b_remote=False,
    owner=GeneratorBusOwner.NONE,
    managed=frozenset(),
):
    return tracker.observe(
        grid_ready=grid_ready,
        automatic_transfer_enabled=automatic,
        power_path=path,
        power_source=source,
        emergency_stop=emergency,
        generators={
            GeneratorSlot.A: (a_running, a_remote),
            GeneratorSlot.B: (b_running, b_remote),
        },
        bus_owner=owner,
        generator_names=NAMES,
        managed_slots=managed,
    )


def messages(events):
    return [event.message for event in events]


def test_first_snapshot_is_baseline_without_false_events():
    tracker = PhysicalEventTracker()
    assert observe(tracker) == ()


def test_grid_loss_and_later_isolation_are_distinct_physical_facts():
    """Видим сначала outage/UPS, затем отдельное подтверждение снятия Grid path."""
    tracker = PhysicalEventTracker()
    observe(tracker)

    lost = observe(
        tracker,
        grid_ready=False,
        path=PowerPath.GRID,
        source=PowerSource.UPS_ONLY,
    )
    assert user_message("grid_input_off") in messages(lost)
    assert user_message("power_source_ups_grid_path") in messages(lost)
    assert user_message("power_path_isolated_from_grid") not in messages(lost)

    isolated = observe(
        tracker,
        grid_ready=False,
        path=PowerPath.ISOLATED,
        source=PowerSource.UPS_ONLY,
    )
    assert user_message("power_path_isolated_from_grid") in messages(isolated)
    assert user_message("power_source_ups_isolated") not in messages(isolated)


def test_managed_running_transition_is_marked_as_energyats_start():
    tracker = PhysicalEventTracker()
    observe(tracker, a_remote=True, managed=frozenset({GeneratorSlot.A}))

    events = observe(
        tracker,
        a_remote=True,
        a_running=True,
        owner=GeneratorBusOwner.A,
        managed=frozenset({GeneratorSlot.A}),
    )
    assert user_message("generator_running_managed", generator="Elemax") in messages(events)
    assert "Генератор Elemax запущен. Запуск управляется АВР." in messages(events)


def test_managed_stop_uses_natural_generator_message():
    tracker = PhysicalEventTracker()
    observe(
        tracker,
        a_remote=True,
        a_running=True,
        owner=GeneratorBusOwner.A,
        managed=frozenset({GeneratorSlot.A}),
    )

    events = observe(
        tracker,
        a_remote=False,
        a_running=False,
        owner=GeneratorBusOwner.NONE,
        managed=frozenset({GeneratorSlot.A}),
    )
    assert "Генератор Elemax остановлен." in messages(events)


def test_unowned_running_transition_is_marked_external():
    tracker = PhysicalEventTracker()
    observe(tracker)

    events = observe(
        tracker,
        b_running=True,
        b_remote=False,
        owner=GeneratorBusOwner.B,
    )
    assert user_message("generator_running_external", generator="Вепрь") in messages(events)


def test_external_stop_is_visible_too():
    tracker = PhysicalEventTracker()
    observe(tracker, b_running=True, owner=GeneratorBusOwner.B)

    events = observe(tracker, b_running=False, owner=GeneratorBusOwner.NONE)
    assert user_message("generator_stopped_external", generator="Вепрь") in messages(events)


def test_generator_supply_and_grid_restore_create_physical_timeline():
    tracker = PhysicalEventTracker()
    observe(
        tracker,
        grid_ready=False,
        path=PowerPath.ISOLATED,
        source=PowerSource.UPS_ONLY,
        a_running=True,
        a_remote=True,
        owner=GeneratorBusOwner.A,
        managed=frozenset({GeneratorSlot.A}),
    )

    generator = observe(
        tracker,
        grid_ready=False,
        path=PowerPath.GENERATOR,
        source=PowerSource.GENERATOR,
        a_running=True,
        a_remote=True,
        owner=GeneratorBusOwner.A,
        managed=frozenset({GeneratorSlot.A}),
    )
    assert user_message("power_path_generator") in messages(generator)
    assert user_message("power_source_generator") in messages(generator)

    restored = observe(
        tracker,
        grid_ready=True,
        path=PowerPath.GRID,
        source=PowerSource.GRID,
        a_running=True,
        a_remote=True,
        owner=GeneratorBusOwner.A,
        managed=frozenset({GeneratorSlot.A}),
    )
    assert user_message("grid_input_on") in messages(restored)
    assert user_message("power_path_grid") in messages(restored)
    assert user_message("power_source_grid") in messages(restored)


def test_emergency_stop_and_automatic_transfer_toggle_are_visible():
    tracker = PhysicalEventTracker()
    observe(tracker)

    events = observe(tracker, emergency=True, automatic=False)
    assert user_message("emergency_stop_on") in messages(events)
    assert user_message("automatic_transfer_off") in messages(events)


def test_remote_state_confirmation_is_visible_separately_from_running():
    tracker = PhysicalEventTracker()
    observe(tracker)

    events = observe(
        tracker,
        a_remote=True,
        managed=frozenset({GeneratorSlot.A}),
    )
    assert user_message("generator_remote_on", generator="Elemax") in messages(events)
    assert user_message("generator_running_managed", generator="Elemax") not in messages(events)


def test_bus_owner_change_names_real_generators():
    tracker = PhysicalEventTracker()
    observe(tracker, a_running=True, owner=GeneratorBusOwner.A)

    events = observe(
        tracker,
        a_running=False,
        b_running=True,
        owner=GeneratorBusOwner.B,
    )
    assert user_message(
        "bus_owner_changed",
        old="Elemax",
        new="Вепрь",
    ) in messages(events)


def test_reset_makes_next_snapshot_new_baseline():
    tracker = PhysicalEventTracker()
    observe(tracker)
    tracker.reset()

    assert (
        observe(
            tracker,
            grid_ready=False,
            path=PowerPath.ISOLATED,
            source=PowerSource.UPS_ONLY,
        )
        == ()
    )

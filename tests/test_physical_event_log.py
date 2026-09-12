"""Физический event journal: входы/feedback без повторения каждого control tick."""

from __future__ import annotations

from domain import GeneratorSlot, PowerPath, PowerSource
from generator_bus import GeneratorBusOwner
from physical_event_log import PhysicalEventTracker


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


def test_grid_loss_and_isolation_are_separate_observed_events():
    """Отдельно видим причину outage и подтверждённое снятие Grid path."""
    tracker = PhysicalEventTracker()
    observe(tracker)

    lost = observe(
        tracker,
        grid_ready=False,
        path=PowerPath.GRID,
        source=PowerSource.UPS_ONLY,
    )
    assert "Входная сеть пропала (Grid Input Ready = OFF)." in messages(lost)
    assert "Подтверждено: силовые вводы сняты; дом в режиме UPS_ONLY." not in messages(lost)

    isolated = observe(
        tracker,
        grid_ready=False,
        path=PowerPath.ISOLATED,
        source=PowerSource.UPS_ONLY,
    )
    assert (
        "Подача входной сети в дом отключена; силовой ввод изолирован."
        in messages(isolated)
    )


def test_managed_running_transition_is_marked_managed():
    tracker = PhysicalEventTracker()
    observe(tracker, a_remote=True, managed=frozenset({GeneratorSlot.A}))

    events = observe(
        tracker,
        a_remote=True,
        a_running=True,
        owner=GeneratorBusOwner.A,
        managed=frozenset({GeneratorSlot.A}),
    )
    assert (
        "Elemax: RUNNING = ON — двигатель запущен (managed EnergyATS)."
        in messages(events)
    )


def test_unowned_running_transition_is_marked_external():
    tracker = PhysicalEventTracker()
    observe(tracker)

    events = observe(
        tracker,
        b_running=True,
        b_remote=False,
        owner=GeneratorBusOwner.B,
    )
    assert (
        "Вепрь: RUNNING = ON — двигатель запущен (внешний/неуправляемый запуск)."
        in messages(events)
    )


def test_external_stop_is_visible_too():
    tracker = PhysicalEventTracker()
    observe(tracker, b_running=True, owner=GeneratorBusOwner.B)

    events = observe(tracker, b_running=False, owner=GeneratorBusOwner.NONE)
    assert (
        "Вепрь: RUNNING = OFF — двигатель остановлен (внешняя/неуправляемая остановка)."
        in messages(events)
    )


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
    assert "Генераторная ветвь дома подключена (Generator path подтверждён)." in messages(generator)
    assert "Подтверждено: дом питается от генераторной шины." in messages(generator)

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
    assert "Входная сеть восстановлена (Grid Input Ready = ON)." in messages(restored)
    assert "Сетевая ветвь дома подключена (Grid path подтверждён)." in messages(restored)
    assert "Подтверждено: дом питается от входной сети Grid." in messages(restored)


def test_emergency_stop_and_automatic_transfer_toggle_are_visible():
    tracker = PhysicalEventTracker()
    observe(tracker)

    events = observe(tracker, emergency=True, automatic=False)
    assert "Generators Emergency Stop активирован." in messages(events)
    assert "Автоматический переход на резерв отключён." in messages(events)


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

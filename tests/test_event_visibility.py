"""MAIN/DETAIL — независимое измерение пользовательского журнала EnergyATS."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from domain import (
    EventVisibility,
    GeneratorSlot,
    PowerPath,
    PowerSource,
    SessionReason,
    SupervisorEvent,
)
from energy_supervisor import EnergySupervisor
from exercise_scheduler import ExerciseConfig, ExerciseScheduler
from generator_bus import GeneratorBusOwner
from ha_adapter import (
    ENERGY_ATS_LOG_ENTITY,
    HomeAssistantAdapter,
)
from load_manager import (
    LoadAction,
    LoadActionKind,
    LoadGroup,
    LoadManager,
    LoadManagerConfig,
    LoadManagerObservation,
)
from physical_event_log import PhysicalEventTracker
from ups_run import BatteryObservation, UPSRun, UPSRunConfig, UPSRunObservation
from user_messages import user_event, user_message


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    async def call_service(self, domain, service, *, service_data=None):
        self.calls.append((domain, service, service_data or {}))

    def get_state(self, entity_id):
        return None

    def has_entity(self, entity_id):
        return False


def _observe(
    tracker: PhysicalEventTracker,
    *,
    grid_ready=True,
    path=PowerPath.GRID,
    source=PowerSource.GRID,
    emergency=False,
    a_running=False,
    a_remote=False,
    owner=GeneratorBusOwner.NONE,
):
    return tracker.observe(
        grid_ready=grid_ready,
        automatic_transfer_enabled=True,
        power_path=path,
        power_source=source,
        emergency_stop=emergency,
        generators={
            GeneratorSlot.A: (a_running, a_remote),
            GeneratorSlot.B: (False, False),
        },
        bus_owner=owner,
        generator_names={
            GeneratorSlot.A: "Elemax",
            GeneratorSlot.B: "Вепрь",
        },
        managed_slots=frozenset({GeneratorSlot.A}),
    )


def _load_observation(
    *,
    now=0.0,
    owner=GeneratorSlot.A,
    nominal=5000.0,
    maximum=6500.0,
    meter=True,
    power=1000.0,
    sample=1,
    g1=True,
    g2=True,
    on_generator=True,
    on_grid=False,
    desired=False,
    ready=False,
):
    return LoadManagerObservation(
        now=now,
        house_on_generator=on_generator,
        house_on_grid=on_grid,
        desired_generator_supply=desired,
        managed_generator_ready=ready,
        power_transition_in_progress=False,
        bus_owner=owner,
        nominal_power=nominal,
        maximum_power=maximum,
        meter_ready=meter,
        generator_power=power,
        power_sample_id=sample,
        groups={LoadGroup.G1: g1, LoadGroup.G2: g2},
        generator_name="Elemax",
        actions_enabled=True,
    )


def test_supervisor_event_is_main_by_default():
    event = SupervisorEvent("info", "обычное событие")
    assert event.visibility == EventVisibility.MAIN


def test_user_event_can_explicitly_be_detail():
    event = user_event(
        "generator_remote_on",
        visibility=EventVisibility.DETAIL,
        generator="Elemax",
    )
    assert event.visibility == EventVisibility.DETAIL


def test_supervisor_requests_are_detail_but_result_stays_main():
    supervisor = EnergySupervisor()

    supervisor.request_manual_start()
    supervisor.request_manual_stop()
    supervisor.request_recovery_reset()
    requested = supervisor.take_events()
    assert requested
    assert all(event.visibility == EventVisibility.DETAIL for event in requested)

    supervisor.begin_recovery_reset()
    started = supervisor.take_events()
    assert len(started) == 1
    assert started[0].visibility == EventVisibility.DETAIL

    supervisor.complete_recovery_reset()
    completed = supervisor.take_events()
    assert len(completed) == 1
    assert completed[0].visibility == EventVisibility.MAIN


def test_physical_confirmations_are_detail_while_major_facts_are_main():
    tracker = PhysicalEventTracker()
    _observe(tracker)

    events = _observe(
        tracker,
        grid_ready=False,
        path=PowerPath.ISOLATED,
        source=PowerSource.UPS_ONLY,
        emergency=True,
        a_running=True,
        a_remote=True,
        owner=GeneratorBusOwner.A,
    )

    by_message = {event.message: event.visibility for event in events}
    assert any(
        visibility == EventVisibility.DETAIL
        for message, visibility in by_message.items()
        if "Подтверждено" in message or "REMOTE" in message or "шины" in message
    )
    assert any(event.visibility == EventVisibility.MAIN for event in events)


def test_confirmed_house_supply_from_grid_is_main():
    tracker = PhysicalEventTracker()
    _observe(
        tracker,
        grid_ready=False,
        path=PowerPath.GENERATOR,
        source=PowerSource.GENERATOR,
    )

    events = _observe(
        tracker,
        grid_ready=True,
        path=PowerPath.GRID,
        source=PowerSource.GRID,
    )

    target = [
        event
        for event in events
        if event.message == user_message("power_source_grid")
    ]
    assert len(target) == 1
    assert target[0].visibility == EventVisibility.MAIN


def test_exercise_scheduler_progress_notifications_are_detail():
    scheduler = ExerciseScheduler(
        {
            GeneratorSlot.A: ExerciseConfig(True, 30, "12:00", 10, 3),
            GeneratorSlot.B: ExerciseConfig(False, 30, "12:00", 10, 3),
        }
    )
    event = scheduler.confirm_warning(
        GeneratorSlot.A,
        "2026-09-12",
        "Elemax",
        datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc),
    )
    assert event.visibility == EventVisibility.DETAIL


def test_ups_target_soc_detail_does_not_duplicate_main_outcome():
    ups_run = UPSRun(
        UPSRunConfig(
            delayed_start_enabled=True,
            charge_cycle_enabled=True,
            start_soc=40,
            target_soc=80,
            min_ttg_before_start=60,
            max_start_delay=3600,
            telemetry_stale_time=300,
        )
    )
    decision = ups_run.step(
        UPSRunObservation(
            now=100,
            grid_ready=False,
            automatic_transfer_enabled=True,
            core_delay_elapsed=True,
            battery=BatteryObservation(
                soc=80,
                ttg_minutes=None,
                discharging=False,
                ready=True,
                sample_id=1,
            ),
            session_reason=SessionReason.GRID_OUTAGE,
            session_active=True,
            session_on_generator=True,
            session_cycle_owned=True,
        )
    )
    target = [event for event in decision.events if "достигнут целевой" in event.message]
    assert len(target) == 1
    assert target[0].visibility == EventVisibility.DETAIL
    assert decision.request_cycle_stop is True


def test_load_manager_degrade_has_one_clean_main_and_technical_detail():
    manager = LoadManager(LoadManagerConfig(enabled=True))
    first = manager.step(_load_observation(owner=None))

    main = [event for event in first.events if event.visibility == EventVisibility.MAIN]
    detail = [event for event in first.events if event.visibility == EventVisibility.DETAIL]
    assert [event.message for event in main] == [
        "Автоматическое управление некритичными нагрузками временно недоступно. "
        "Основное управление резервным питанием продолжает работу."
    ]
    assert detail
    assert first.notifications == (main[0].message,)
    assert not any(
        word in main[0].message
        for word in ("bus owner", "sample", "LOAD_SHEDDING", "core ATS")
    )

    second = manager.step(_load_observation(now=1, nominal=None, maximum=None, sample=2))
    assert not any(
        event.visibility == EventVisibility.MAIN for event in second.events
    )
    assert any(
        event.visibility == EventVisibility.DETAIL for event in second.events
    )
    assert second.notifications == ()


def test_load_manager_recovery_returns_to_main_once_after_stabilization():
    """Recovery-сообщение означает завершённое новое measurement window, а не первый sample."""
    manager = LoadManager(LoadManagerConfig(enabled=True))
    manager.step(_load_observation(owner=None))

    started = manager.step(_load_observation(now=1, sample=2))
    assert not any(
        event.visibility == EventVisibility.MAIN
        and event.message
        == "Автоматическое управление некритичными нагрузками восстановлено."
        for event in started.events
    )

    middle = manager.step(_load_observation(now=5, sample=3))
    assert not any(
        event.visibility == EventVisibility.MAIN
        and event.message
        == "Автоматическое управление некритичными нагрузками восстановлено."
        for event in middle.events
    )

    recovered = manager.step(_load_observation(now=11, sample=4))
    assert sum(
        event.visibility == EventVisibility.MAIN
        and event.message
        == "Автоматическое управление некритичными нагрузками восстановлено."
        for event in recovered.events
    ) == 1


def test_load_manager_user_message_uses_ground_floor_name_not_basement():
    manager = LoadManager(LoadManagerConfig(enabled=True))
    actions: list[LoadAction] = []
    manager._queue(
        _load_observation(),
        actions,
        LoadGroup.G2,
        False,
        "nominal_overload",
        "detail",
    )
    event, notification = manager.report_execution_failure(
        actions[0],
        "test failure",
    )

    assert "цокольного этажа" in event.message
    assert "подвал" not in event.message.lower()
    assert notification == event.message
    assert "turn_off" not in event.message


def test_repeated_pretransfer_problem_does_not_repeat_main_warning():
    manager = LoadManager(LoadManagerConfig(enabled=True))
    first = manager.step(
        _load_observation(
            on_generator=False,
            on_grid=True,
            desired=True,
            ready=True,
            g1=None,
            g2=False,
        )
    )
    second = manager.step(
        _load_observation(
            now=1,
            on_generator=False,
            on_grid=True,
            desired=True,
            ready=True,
            g1=None,
            g2=False,
            sample=2,
        )
    )

    assert sum(
        event.visibility == EventVisibility.MAIN for event in first.events
    ) == 1
    assert not any(
        event.visibility == EventVisibility.MAIN for event in second.events
    )


@pytest.mark.asyncio
async def test_adapter_logs_every_event_but_publishes_only_main_to_ha_logbook(caplog):
    fake = FakeClient()
    logger = logging.getLogger("test.complete-app-log")
    adapter = HomeAssistantAdapter(fake, armed=True, logger=logger)

    with caplog.at_level(logging.INFO, logger=logger.name):
        await adapter.publish_events(
            (
                SupervisorEvent("info", "Основное"),
                SupervisorEvent(
                    "info",
                    "Подробное",
                    EventVisibility.DETAIL,
                ),
            )
        )

    logbook_calls = [call for call in fake.calls if call[:2] == ("logbook", "log")]
    assert [(call[2]["message"], call[2]["entity_id"]) for call in logbook_calls] == [
        ("Основное", ENERGY_ATS_LOG_ENTITY),
    ]
    assert "Основное" in caplog.messages
    assert "Подробное" in caplog.messages

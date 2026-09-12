"""MAIN/DETAIL — независимое измерение пользовательского журнала EnergyATS."""

from __future__ import annotations

import pytest

from domain import EventVisibility, GeneratorSlot, PowerPath, PowerSource, SupervisorEvent
from energy_supervisor import EnergySupervisor
from generator_bus import GeneratorBusOwner
from ha_adapter import (
    ENERGY_ATS_DETAIL_LOG_ENTITY,
    ENERGY_ATS_LOG_ENTITY,
    HomeAssistantAdapter,
)
from physical_event_log import PhysicalEventTracker
from user_messages import user_event


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


@pytest.mark.asyncio
async def test_adapter_routes_main_and_detail_to_separate_logbook_entities():
    fake = FakeClient()
    adapter = HomeAssistantAdapter(fake, armed=True)

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
    routed = {
        call[2]["message"]: call[2]["entity_id"]
        for call in logbook_calls
    }
    assert routed == {
        "Основное": ENERGY_ATS_LOG_ENTITY,
        "Подробное": ENERGY_ATS_DETAIL_LOG_ENTITY,
    }

"""Общие fake-объекты для тестов границы Energy ATS и Home Assistant."""

from __future__ import annotations

import json
from pathlib import Path

from domain import GeneratorSlot, PowerSource, SessionReason, Transaction
from energy_supervisor import EnergySupervisor, GeneratorSession, SupervisorPhase
from ha_adapter import ENTITIES
from main import EnergySupervisorApp


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.states: dict[str, str] = {}

    async def call_service(self, domain, service, *, service_data=None):
        self.calls.append((domain, service, service_data or {}))

    def get_state(self, entity_id):
        return self.states.get(entity_id)

    def has_entity(self, entity_id):
        return entity_id in self.states


class PhysicalFakeClient(FakeClient):
    """Имитировать только физическую обратную связь силовых switch-команд."""

    def __init__(self, journal_path: Path | None = None) -> None:
        super().__init__()
        self.journal_path = journal_path
        self.pending_seen_before_hardware: list[list[dict[str, str]]] = []

    async def call_service(self, domain, service, *, service_data=None):
        data = service_data or {}
        entity_id = data.get("entity_id")
        if domain in {"switch", "button"} and self.journal_path is not None:
            journal = json.loads(self.journal_path.read_text(encoding="utf-8"))
            self.pending_seen_before_hardware.append(journal["pending_actions"])

        await super().call_service(domain, service, service_data=data)
        if domain != "switch" or not isinstance(entity_id, str):
            return

        self.states[entity_id] = "on" if service == "turn_on" else "off"
        if entity_id == ENTITIES["grid_power"] and service == "turn_off":
            self.states[ENTITIES["house_grid"]] = "off"
        elif entity_id == ENTITIES["grid_power"] and service == "turn_on":
            if self.states[ENTITIES["grid_ready"]] == "on":
                self.states[ENTITIES["house_grid"]] = "on"
        elif entity_id == ENTITIES["source_generator"] and service == "turn_on":
            self.states[ENTITIES["house_generator"]] = "on"
        elif entity_id == ENTITIES["source_generator"] and service == "turn_off":
            self.states[ENTITIES["house_generator"]] = "off"


def attach_fake_client(app: EnergySupervisorApp, fake: FakeClient) -> None:
    app.client = fake
    app.adapter.client = fake


def populated_states() -> dict[str, str]:
    return {
        ENTITIES["automatic_transfer"]: "off",
        ENTITIES["grid_ready"]: "on",
        ENTITIES["house_grid"]: "on",
        ENTITIES["house_generator"]: "off",
        ENTITIES["generator_a_running"]: "off",
        ENTITIES["generator_b_running"]: "off",
        ENTITIES["generator_a_remote"]: "off",
        ENTITIES["generator_b_remote"]: "off",
        ENTITIES["emergency_stop"]: "off",
        ENTITIES["ambient_temperature_external"]: "7.5",
        ENTITIES["grid_power"]: "on",
        ENTITIES["source_generator"]: "off",
        ENTITIES["generator_a_choke_cold_start"]: "unknown",
        ENTITIES["generator_a_choke_run"]: "unknown",
        ENTITIES["generator_b_choke_cold_start"]: "unknown",
        ENTITIES["generator_b_choke_run"]: "unknown",
    }


def saved_supervisor_payload(
    *,
    phase: SupervisorPhase,
    transaction_complete: bool,
) -> dict:
    supervisor = EnergySupervisor()
    supervisor.phase = phase
    supervisor.session = GeneratorSession.begin(
        reason=SessionReason.MANUAL_GENERATOR_START,
        generator=GeneratorSlot.A,
        now=1.0,
        grid_was_unavailable=False,
    )
    supervisor.desired_source = PowerSource.GENERATOR_A
    supervisor.desired_generators[GeneratorSlot.A] = True
    supervisor.transaction = Transaction.begin(
        "enter_generator",
        "A",
        1.0,
        "transfer_to_generator",
    )
    if transaction_complete:
        supervisor.transaction.complete(2.0, "stable")
    return {
        "journal_schema_version": 1,
        "app_version": "0.3.0",
        "supervisor": supervisor.to_dict(),
        "pending_actions": [],
    }

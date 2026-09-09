from __future__ import annotations

import pytest

from domain import GeneratorSlot, SessionReason
from energy_supervisor import GeneratorSession, SupervisorPhase
from ha_adapter import ENTITIES, ENERGY_ATS_STATUS_ENTITY
from main import DEFAULT_OPTIONS, EnergySupervisorApp, _seconds_left


class StatusFakeClient:
    def __init__(self, states: dict[str, str]) -> None:
        self.states = states
        self.published: list[tuple[str, str, dict]] = []

    def get_state(self, entity_id: str) -> str | None:
        return self.states.get(entity_id)

    def has_entity(self, entity_id: str) -> bool:
        return entity_id in self.states

    async def call_service(self, domain, service, *, service_data=None):
        return None

    async def set_state(self, entity_id, state, *, attributes=None):
        self.published.append((entity_id, state, attributes or {}))


def grid_states() -> dict[str, str]:
    return {
        ENTITIES["automatic_transfer"]: "on",
        ENTITIES["test_mode"]: "off",
        ENTITIES["grid_ready"]: "on",
        ENTITIES["house_grid"]: "on",
        ENTITIES["house_generator"]: "off",
        ENTITIES["generator_a_running"]: "off",
        ENTITIES["generator_b_running"]: "off",
        ENTITIES["generator_a_remote"]: "off",
        ENTITIES["generator_b_remote"]: "off",
        ENTITIES["generator_a_name"]: "Elemax",
        ENTITIES["generator_b_name"]: "Вепрь",
        ENTITIES["generator_a_model"]: "SH7600EX 6.5 / 5.6 кВт",
        ENTITIES["generator_b_model"]: "АПБ 6-230 ВХ-БСГ 6.0 / 5.5 кВт",
        ENTITIES["primary_generator"]: "Elemax",
        ENTITIES["emergency_stop"]: "off",
        ENTITIES["ambient_temperature_external"]: "7.5",
        ENTITIES["grid_power"]: "on",
        ENTITIES["source_generator"]: "off",
        ENTITIES["generator_a_choke_cold_start"]: "unknown",
        ENTITIES["generator_a_choke_run"]: "unknown",
        ENTITIES["generator_b_choke_cold_start"]: "unknown",
        ENTITIES["generator_b_choke_run"]: "unknown",
    }


def app_with_fake(tmp_path):
    app = EnergySupervisorApp(
        {**DEFAULT_OPTIONS, "armed": True, "state_file": str(tmp_path / "state.json")},
        token="test",
    )
    fake = StatusFakeClient(grid_states())
    app.client = fake
    app.adapter.client = fake
    return app, fake


def normal_observation(app: EnergySupervisorApp, now: float):
    hardware = app.adapter.snapshot()
    app._sync_generator_configuration(hardware)
    hardware = app._apply_bus_model(hardware)
    app._refresh_component_views(now, hardware)
    observation = app._supervisor_observation(hardware)
    app.supervisor.step(now, observation)
    return app._supervisor_observation(hardware)


def test_status_payload_exposes_current_v04_contract(tmp_path):
    app, _ = app_with_fake(tmp_path)
    payload = app._status_payload(100.0, normal_observation(app, 100.0))

    assert payload["state"] == "Питание от основной сети"
    attrs = payload["attributes"]
    assert attrs["source"] == "grid"
    assert attrs["phase"] == "normal"
    assert attrs["generator"] is None
    assert attrs["managed_generator"] is None
    assert attrs["bus_owner"] in {"none", "unknown"}
    assert attrs["generator_a_run_context"] == "none"
    assert attrs["generator_b_run_context"] == "none"
    assert attrs["primary_generator"] == "Elemax"
    assert attrs["fallback_used"] is False
    assert attrs["armed"] is True
    assert "schema_version" not in attrs
    assert "primary_generator_slot" not in attrs
    assert "bus_owner_slot" not in attrs


def test_status_reports_ups_only_when_grid_is_intentionally_isolated(tmp_path):
    app, fake = app_with_fake(tmp_path)
    fake.states[ENTITIES["grid_power"]] = "off"
    fake.states[ENTITIES["house_grid"]] = "off"
    payload = app._status_payload(100.0, normal_observation(app, 100.0))
    assert payload["state"] == "В доме работает только UPS линия"
    assert payload["attributes"]["source"] == "ups_only"


@pytest.mark.asyncio
async def test_status_publication_is_deduplicated(tmp_path):
    app, fake = app_with_fake(tmp_path)
    observation = normal_observation(app, 100.0)
    await app._publish_status(100.0, observation)
    await app._publish_status(100.0, observation)
    assert len(fake.published) == 1
    entity_id, state, attributes = fake.published[0]
    assert entity_id == ENERGY_ATS_STATUS_ENTITY
    assert state == "Питание от основной сети"
    assert attributes["source"] == "grid"


def test_grid_restore_remaining_time_uses_only_current_supervisor_phase(tmp_path):
    app, _ = app_with_fake(tmp_path)
    observation = normal_observation(app, 100.0)
    app.supervisor.session = GeneratorSession.begin(
        SessionReason.GRID_OUTAGE,
        GeneratorSlot.A,
        grid_was_unavailable=True,
    )
    app.supervisor.phase = SupervisorPhase.ON_GENERATOR
    app.supervisor.grid_ready_since = 90.0

    assert app._remaining_seconds(100.0, observation) == 50


def test_seconds_left_rounds_up_and_never_negative():
    assert _seconds_left(3.01) == 4
    assert _seconds_left(3.0) == 3
    assert _seconds_left(0.01) == 1
    assert _seconds_left(-5.0) == 0

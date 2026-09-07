from __future__ import annotations

import pytest

from domain import GeneratorSlot
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


def _grid_states() -> dict[str, str]:
    return {
        ENTITIES["automatic_transfer"]: "on",
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


def _app_with_fake(tmp_path):
    app = EnergySupervisorApp(
        {
            **DEFAULT_OPTIONS,
            "armed": True,
            "state_file": str(tmp_path / "state.json"),
        },
        token="test",
    )
    fake = StatusFakeClient(_grid_states())
    app.client = fake
    app.adapter.client = fake
    return app, fake


def _normal_grid_observation(app: EnergySupervisorApp, now: float):
    hardware = app.adapter.snapshot()
    app._sync_generator_configuration(hardware)
    app._refresh_component_views(now, hardware)
    observation = app._supervisor_observation(hardware)
    app.supervisor.step(now, observation)
    return app._supervisor_observation(hardware)


def test_status_payload_uses_confirmed_source_and_public_contract(tmp_path):
    app, _ = _app_with_fake(tmp_path)
    observation = _normal_grid_observation(app, 100.0)

    payload = app._status_payload(100.0, observation)

    assert payload["state"] == "Питание от основной сети"
    assert payload["attributes"] == {
        "friendly_name": "Energy ATS Status",
        "icon": "mdi:transfer-switch",
        "source": "grid",
        "phase": "normal",
        "generator": None,
        "generator_slot": None,
        "remaining_seconds": None,
        "session_reason": None,
        "armed": True,
        "schema_version": 1,
    }


def test_status_reports_manual_battery_path_when_grid_is_available(tmp_path):
    app, fake = _app_with_fake(tmp_path)
    fake.states[ENTITIES["grid_power"]] = "off"
    fake.states[ENTITIES["house_grid"]] = "off"
    observation = _normal_grid_observation(app, 100.0)

    payload = app._status_payload(100.0, observation)

    assert payload["state"] == (
        "Grid доступна · Grid path отключён · питание от аккумуляторов МАП"
    )
    assert payload["attributes"]["source"] == "battery"


@pytest.mark.asyncio
async def test_status_publication_is_deduplicated(tmp_path):
    app, fake = _app_with_fake(tmp_path)
    observation = _normal_grid_observation(app, 100.0)

    await app._publish_status(100.0, observation)
    await app._publish_status(100.0, observation)

    assert len(fake.published) == 1
    entity_id, state, attributes = fake.published[0]
    assert entity_id == ENERGY_ATS_STATUS_ENTITY
    assert state == "Питание от основной сети"
    assert attributes["source"] == "grid"


def test_seconds_left_rounds_up_and_never_becomes_negative():
    assert _seconds_left(3.01) == 4
    assert _seconds_left(3.0) == 3
    assert _seconds_left(0.01) == 1
    assert _seconds_left(-5.0) == 0

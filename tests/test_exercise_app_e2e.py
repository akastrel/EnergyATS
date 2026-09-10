from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from domain import GeneratorSlot
from exercise_scheduler import ExerciseResult
from generator_bus import GeneratorRunContext
from ha_adapter import ENTITIES
from main import DEFAULT_OPTIONS, EnergySupervisorApp


class ExercisePhysicalFake:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.state_writes: list[tuple[str, str, dict]] = []
        self.states = {
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
            ENTITIES["ambient_temperature_external"]: "20",
            ENTITIES["grid_power"]: "on",
            ENTITIES["source_generator"]: "off",
            ENTITIES["generator_a_choke_cold_start"]: "unknown",
            ENTITIES["generator_a_choke_run"]: "unknown",
            ENTITIES["generator_b_choke_cold_start"]: "unknown",
            ENTITIES["generator_b_choke_run"]: "unknown",
            "group.family": "not_home",
        }

    def get_state(self, entity_id):
        return self.states.get(entity_id)

    def has_entity(self, entity_id):
        return entity_id in self.states

    async def call_service(self, domain, service, *, service_data=None):
        data = service_data or {}
        self.calls.append((domain, service, data))
        entity_id = data.get("entity_id")
        if domain != "switch" or not isinstance(entity_id, str):
            return

        self.states[entity_id] = "on" if service == "turn_on" else "off"
        if entity_id == ENTITIES["generator_a_remote"]:
            self.states[ENTITIES["generator_a_running"]] = (
                "on" if service == "turn_on" else "off"
            )
        elif entity_id == ENTITIES["generator_b_remote"]:
            self.states[ENTITIES["generator_b_running"]] = (
                "on" if service == "turn_on" else "off"
            )

    async def set_state(self, entity_id, state, *, attributes=None):
        self.state_writes.append((entity_id, state, attributes or {}))


def make_app(tmp_path: Path) -> tuple[EnergySupervisorApp, ExercisePhysicalFake]:
    app = EnergySupervisorApp(
        {
            **DEFAULT_OPTIONS,
            "armed": True,
            "state_file": str(tmp_path / "state.json"),
            "family_presence_entity": "group.family",
            "generator_a_exercise_enabled": True,
            "generator_a_exercise_interval_days": 1,
            "generator_a_exercise_start_time": "15:00",
            "generator_a_exercise_run_minutes": 1,
            "generator_a_exercise_presence_grace_days": 7,
            "generator_b_exercise_enabled": False,
        },
        token="test",
    )
    app.local_time_zone = timezone.utc
    fake = ExercisePhysicalFake()
    app.client = fake
    app.adapter.client = fake
    return app, fake


@pytest.mark.asyncio
async def test_scheduled_exercise_starts_runs_stops_without_touching_house_source(tmp_path):
    app, fake = make_app(tmp_path)
    start = datetime(2026, 3, 10, 15, 0, tzinfo=timezone.utc)
    app.exercise_scheduler.states[GeneratorSlot.A].initial_reference_time = (
        start - timedelta(days=2)
    ).isoformat()

    await app._tick(start.timestamp())
    await app._tick((start + timedelta(seconds=2)).timestamp())
    await app._tick((start + timedelta(seconds=3)).timestamp())

    assert fake.states[ENTITIES["generator_a_running"]] == "on"
    assert (
        app.generator_bus.status().run_contexts[GeneratorSlot.A]
        == GeneratorRunContext.TEST_RUN
    )
    assert fake.states[ENTITIES["house_grid"]] == "on"
    assert fake.states[ENTITIES["house_generator"]] == "off"

    # 60 seconds of confirmed RUNNING, then the ordinary GC cooldown.
    await app._tick((start + timedelta(seconds=64)).timestamp())
    await app._tick((start + timedelta(seconds=125)).timestamp())
    await app._tick((start + timedelta(seconds=126)).timestamp())

    assert fake.states[ENTITIES["generator_a_running"]] == "off"
    assert fake.states[ENTITIES["generator_a_remote"]] == "off"
    assert app.exercise_scheduler.active_attempt is None
    assert (
        app.exercise_scheduler.states[GeneratorSlot.A].last_result
        == ExerciseResult.SUCCESS.value
    )
    assert app.exercise_scheduler.history[-1]["result"] == ExerciseResult.SUCCESS.value

    power_entities = {ENTITIES["grid_power"], ENTITIES["source_generator"]}
    assert not any(
        domain == "switch" and data.get("entity_id") in power_entities
        for domain, _service, data in fake.calls
    )

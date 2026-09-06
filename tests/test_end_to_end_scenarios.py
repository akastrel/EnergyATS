"""Сквозные сценарии Energy ATS от сигналов HA до команд оборудованию."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1] / "energy_ats" / "app"
sys.path.insert(0, str(APP_DIR))

from domain import GeneratorSlot
from energy_supervisor import SupervisorPhase
from generator_controller import GeneratorPhase
from ha_adapter import ENTITIES
from main import DEFAULT_OPTIONS, EnergySupervisorApp
from test_app_adapter import (
    PhysicalFakeClient,
    attach_fake_client,
    populated_states,
)


@pytest.mark.asyncio
async def test_generator_stall_during_warmup_never_transfers_or_starts_backup(
    tmp_path,
):
    journal = tmp_path / "state.json"
    app = EnergySupervisorApp(
        {
            **DEFAULT_OPTIONS,
            "armed": True,
            "state_file": str(journal),
        },
        token="test",
    )
    fake = PhysicalFakeClient(journal)
    fake.states = populated_states()
    fake.states[ENTITIES["ambient_temperature_external"]] = "20"
    attach_fake_client(app, fake)

    await app._tick(0.0)
    app.supervisor.request_manual_start()
    await app._tick(1.0)   # заслонка -> cold
    await app._tick(2.0)   # REMOTE ON
    fake.states[ENTITIES["generator_a_running"]] = "on"
    await app._tick(3.0)   # RUNNING подтверждён
    await app._tick(13.0)  # заслонка -> run, начинается прогрев

    assert app.generator_controllers[GeneratorSlot.A].phase == GeneratorPhase.WARMING_UP
    fake.states[ENTITIES["generator_a_running"]] = "off"
    await app._tick(14.0)  # GC фиксирует остановку во время прогрева
    await app._tick(15.0)  # ES начинает изоляцию отказавшего источника
    await app._tick(16.0)  # безопасный Grid path подтверждён

    hardware_calls = [
        (domain, service, data["entity_id"])
        for domain, service, data in fake.calls
        if domain in {"switch", "button"} and "entity_id" in data
    ]
    assert ("switch", "turn_on", ENTITIES["source_generator"]) not in hardware_calls
    assert ("switch", "turn_off", ENTITIES["grid_power"]) not in hardware_calls
    assert ("switch", "turn_on", ENTITIES["generator_b_remote"]) not in hardware_calls
    assert hardware_calls[-2:] == [
        ("switch", "turn_off", ENTITIES["generator_a_remote"]),
        ("button", "press", ENTITIES["generator_a_choke_run"]),
    ]
    assert fake.states[ENTITIES["grid_power"]] == "on"
    assert fake.states[ENTITIES["house_grid"]] == "on"
    assert app.generator_controllers[GeneratorSlot.A].phase == GeneratorPhase.FAULT
    assert app.supervisor.phase == SupervisorPhase.RECOVERY_REQUIRED
    assert app.supervisor.transaction is not None
    assert app.supervisor.transaction.kind == "isolate_failed_source"
    assert app.supervisor.desired_generators == {
        GeneratorSlot.A: False,
        GeneratorSlot.B: False,
    }


@pytest.mark.asyncio
async def test_generator_stall_under_load_returns_to_grid_without_starting_backup(
    tmp_path,
):
    journal = tmp_path / "state.json"
    app = EnergySupervisorApp(
        {
            **DEFAULT_OPTIONS,
            "armed": True,
            "state_file": str(journal),
        },
        token="test",
    )
    fake = PhysicalFakeClient(journal)
    fake.states = populated_states()
    fake.states[ENTITIES["ambient_temperature_external"]] = "20"
    attach_fake_client(app, fake)

    await app._tick(0.0)
    app.supervisor.request_manual_start()
    await app._tick(1.0)
    await app._tick(2.0)
    fake.states[ENTITIES["generator_a_running"]] = "on"
    await app._tick(3.0)
    await app._tick(13.0)
    await app._tick(43.0)
    await app._tick(44.0)  # Grid OFF
    await app._tick(45.0)  # generator selector ON
    await app._tick(46.0)
    await app._tick(47.0)  # питание дома от генератора подтверждено

    assert app.supervisor.phase == SupervisorPhase.ON_GENERATOR
    assert fake.states[ENTITIES["house_generator"]] == "on"
    fake.calls.clear()

    fake.states[ENTITIES["generator_a_running"]] = "off"
    await app._tick(48.0)  # снять генераторную шину и зафиксировать fault
    await app._tick(49.0)  # подключить Grid
    await app._tick(50.0)  # подтвердить Grid path
    await app._tick(51.0)  # потребовать recovery после подтверждённой изоляции

    hardware_calls = [
        (domain, service, data["entity_id"])
        for domain, service, data in fake.calls
        if domain in {"switch", "button"} and "entity_id" in data
    ]
    assert hardware_calls == [
        ("switch", "turn_off", ENTITIES["source_generator"]),
        ("switch", "turn_off", ENTITIES["generator_a_remote"]),
        ("button", "press", ENTITIES["generator_a_choke_run"]),
        ("switch", "turn_on", ENTITIES["grid_power"]),
    ]
    assert ("switch", "turn_on", ENTITIES["generator_b_remote"]) not in hardware_calls
    assert fake.states[ENTITIES["house_generator"]] == "off"
    assert fake.states[ENTITIES["grid_power"]] == "on"
    assert fake.states[ENTITIES["house_grid"]] == "on"
    assert app.generator_controllers[GeneratorSlot.A].phase == GeneratorPhase.FAULT
    assert app.supervisor.phase == SupervisorPhase.RECOVERY_REQUIRED
    assert app.supervisor.transaction is not None
    assert app.supervisor.transaction.kind == "isolate_failed_source"
    assert app.supervisor.desired_generators == {
        GeneratorSlot.A: False,
        GeneratorSlot.B: False,
    }

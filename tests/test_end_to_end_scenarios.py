"""Ключевые сквозные сценарии EnergyATS v0.4 от HA-сигналов до команд железу."""

from __future__ import annotations

from pathlib import Path

import pytest

from domain import GeneratorSlot, PowerPath, PowerSource
from energy_supervisor import SupervisorPhase
from generator_controller import GeneratorPhase
from ha_adapter import ENTITIES
from main import DEFAULT_OPTIONS, EnergySupervisorApp
from test_app_adapter import PhysicalFakeClient, attach_fake_client, populated_states


def make_app(tmp_path: Path) -> tuple[EnergySupervisorApp, PhysicalFakeClient]:
    journal = tmp_path / "state.json"
    app = EnergySupervisorApp(
        {**DEFAULT_OPTIONS, "armed": True, "state_file": str(journal)},
        token="test",
    )
    fake = PhysicalFakeClient(journal)
    fake.states = populated_states()
    fake.states[ENTITIES["ambient_temperature_external"]] = "20"
    attach_fake_client(app, fake)
    return app, fake


@pytest.mark.asyncio
async def test_manual_start_transfers_to_primary_after_ready(tmp_path):
    app, fake = make_app(tmp_path)
    await app._tick(0.0)
    app.supervisor.request_manual_start()
    await app._tick(1.0)  # choke
    await app._tick(2.0)  # remote on
    fake.states[ENTITIES["generator_a_running"]] = "on"
    await app._tick(3.0)
    await app._tick(13.0)
    await app._tick(43.0)  # ready after warmup
    await app._tick(44.0)  # grid off
    await app._tick(45.0)  # generator selector on
    await app._tick(46.0)
    await app._tick(47.0)

    assert app.supervisor.phase == SupervisorPhase.ON_GENERATOR
    assert fake.states[ENTITIES["grid_power"]] == "off"
    assert fake.states[ENTITIES["source_generator"]] == "on"
    assert fake.states[ENTITIES["house_generator"]] == "on"
    assert app.generator_bus.status().owner_slot == GeneratorSlot.A


@pytest.mark.asyncio
async def test_idle_manual_grid_disconnect_is_ups_only_and_not_reverted(tmp_path):
    app, fake = make_app(tmp_path)
    await app._tick(0.0)
    fake.calls.clear()

    # Grid физически есть, но пользователь запретил её подачу в дом.
    fake.states[ENTITIES["grid_power"]] = "off"
    fake.states[ENTITIES["house_grid"]] = "off"
    await app._tick(1.0)

    assert fake.states[ENTITIES["grid_power"]] == "off"
    assert app.supervisor.desired_source is None
    assert app.power_transfer.status().actual_path == PowerPath.ISOLATED
    assert app.power_transfer.status().actual_source == PowerSource.UPS_ONLY
    assert not any(
        domain == "switch"
        and service == "turn_on"
        and data.get("entity_id") == ENTITIES["grid_power"]
        for domain, service, data in fake.calls
    )


@pytest.mark.asyncio
async def test_two_running_generators_are_not_an_interlock_fault(tmp_path):
    app, fake = make_app(tmp_path)
    await app._tick(0.0)

    # A появился первым и физически владеет общей генераторной шиной.
    fake.states[ENTITIES["generator_a_running"]] = "on"
    await app._tick(1.0)
    assert app.generator_bus.status().owner_slot == GeneratorSlot.A

    # B запускается внешне позднее. Это штатно: owner остаётся A.
    fake.states[ENTITIES["generator_b_running"]] = "on"
    await app._tick(2.0)

    assert app.generator_bus.status().owner_slot == GeneratorSlot.A
    assert app.supervisor.phase != SupervisorPhase.RECOVERY_REQUIRED
    assert fake.states[ENTITIES["generator_b_remote"]] == "off"


@pytest.mark.asyncio
async def test_bus_owner_moves_to_second_running_generator_when_first_stops(tmp_path):
    app, fake = make_app(tmp_path)
    await app._tick(0.0)
    fake.states[ENTITIES["generator_a_running"]] = "on"
    await app._tick(1.0)
    fake.states[ENTITIES["generator_b_running"]] = "on"
    await app._tick(2.0)
    assert app.generator_bus.status().owner_slot == GeneratorSlot.A

    fake.states[ENTITIES["generator_a_running"]] = "off"
    await app._tick(3.0)

    assert app.generator_bus.status().owner_slot == GeneratorSlot.B


@pytest.mark.asyncio
async def test_missing_required_generator_state_never_emits_hardware_commands(tmp_path):
    app, fake = make_app(tmp_path)
    await app._tick(0.0)
    fake.calls.clear()

    fake.states[ENTITIES["generator_a_running"]] = "unknown"
    app.supervisor.request_manual_start()
    await app._tick(1.0)

    assert app.supervisor.session is None
    assert not any(domain in {"switch", "button"} for domain, _service, _data in fake.calls)


@pytest.mark.asyncio
async def test_manual_stop_without_grid_restores_grid_side_before_engine_stop(tmp_path):
    app, fake = make_app(tmp_path)
    fake.states[ENTITIES["grid_ready"]] = "off"
    fake.states[ENTITIES["house_grid"]] = "off"
    await app._tick(0.0)
    app.supervisor.request_manual_start()
    await app._tick(1.0)
    await app._tick(2.0)
    fake.states[ENTITIES["generator_a_running"]] = "on"
    await app._tick(3.0)
    await app._tick(13.0)
    await app._tick(43.0)
    await app._tick(44.0)
    await app._tick(45.0)
    await app._tick(46.0)
    await app._tick(47.0)
    assert app.supervisor.phase == SupervisorPhase.ON_GENERATOR

    fake.calls.clear()
    app.supervisor.request_manual_stop()
    await app._tick(48.0)
    await app._tick(49.0)
    await app._tick(50.0)

    calls = [
        (domain, service, data.get("entity_id"))
        for domain, service, data in fake.calls
        if domain in {"switch", "button"}
    ]
    assert calls[0] == ("switch", "turn_off", ENTITIES["source_generator"])
    assert ("switch", "turn_on", ENTITIES["grid_power"]) in calls
    assert app.power_transfer.status().actual_source == PowerSource.UPS_ONLY
    assert app.generator_controllers[GeneratorSlot.A].phase != GeneratorPhase.IDLE

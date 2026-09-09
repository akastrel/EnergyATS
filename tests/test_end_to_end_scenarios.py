"""Сквозные сценарии EnergyATS v0.4: HA-сигналы -> FSM -> команды железу."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from domain import GeneratorSlot, PowerPath, PowerSource
from energy_supervisor import SupervisorPhase
from generator_bus import GeneratorRunContext
from generator_controller import GeneratorPhase
from ha_adapter import ENTITIES
from main import DEFAULT_OPTIONS, EnergySupervisorApp
from test_app_adapter import PhysicalFakeClient, attach_fake_client, populated_states


def make_app(
    tmp_path: Path,
    **option_overrides,
) -> tuple[EnergySupervisorApp, PhysicalFakeClient]:
    journal = tmp_path / "state.json"
    app = EnergySupervisorApp(
        {
            **DEFAULT_OPTIONS,
            "armed": True,
            "state_file": str(journal),
            **option_overrides,
        },
        token="test",
    )
    fake = PhysicalFakeClient(journal)
    fake.states = populated_states()
    fake.states[ENTITIES["ambient_temperature_external"]] = "20"
    fake.states[ENTITIES["test_mode"]] = "off"
    attach_fake_client(app, fake)
    return app, fake


def accelerate_generators(app: EnergySupervisorApp) -> None:
    """Ускорить таймеры, сохранив последовательность FSM."""

    for controller in app.generator_controllers.values():
        controller.profile = replace(
            controller.profile,
            choke_move_seconds=0.0,
            cold_start_choke_hold_seconds=0.0,
            start_timeout_seconds=2.0,
            stop_timeout_seconds=2.0,
            cooldown_seconds=0.0,
            warmup_warm_seconds=0.0,
            warmup_cool_seconds=0.0,
            warmup_cold_seconds=0.0,
            warmup_very_cold_seconds=0.0,
        )


def set_grid_outage(fake: PhysicalFakeClient, *, automatic: bool = True) -> None:
    fake.states[ENTITIES["automatic_transfer"]] = "on" if automatic else "off"
    fake.states[ENTITIES["grid_ready"]] = "off"
    fake.states[ENTITIES["house_grid"]] = "off"
    fake.states[ENTITIES["test_mode"]] = "off"


async def drive_primary_a_to_house(
    app: EnergySupervisorApp,
    fake: PhysicalFakeClient,
    *,
    start_now: float = 0.0,
) -> float:
    set_grid_outage(fake)
    now = start_now
    for _ in range(40):
        await app._tick(now)
        if fake.states[ENTITIES["generator_a_remote"]] == "on":
            fake.states[ENTITIES["generator_a_running"]] = "on"
        if (
            app.supervisor.phase == SupervisorPhase.ON_GENERATOR
            and app.supervisor.session is not None
            and app.supervisor.session.generator == GeneratorSlot.A
        ):
            return now + 1.0
        now += 1.0
    raise AssertionError("PRIMARY A не дошёл до устойчивого питания дома")


def switch_calls(fake: PhysicalFakeClient) -> list[tuple[str, str]]:
    return [
        (service, data.get("entity_id"))
        for domain, service, data in fake.calls
        if domain == "switch"
    ]


@pytest.mark.asyncio
async def test_manual_start_transfers_to_primary_after_ready(tmp_path):
    app, fake = make_app(tmp_path)
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
    assert fake.states[ENTITIES["grid_power"]] == "off"
    assert fake.states[ENTITIES["source_generator"]] == "on"
    assert fake.states[ENTITIES["house_generator"]] == "on"
    assert app.generator_bus.status().owner_slot == GeneratorSlot.A
    assert app.power_transfer.status().actual_source == PowerSource.GENERATOR


@pytest.mark.asyncio
async def test_idle_manual_grid_disconnect_is_ups_only_and_not_reverted(tmp_path):
    app, fake = make_app(tmp_path)
    await app._tick(0.0)
    fake.calls.clear()

    fake.states[ENTITIES["grid_power"]] = "off"
    fake.states[ENTITIES["house_grid"]] = "off"
    await app._tick(1.0)

    assert app.supervisor.desired_source is None
    assert app.power_transfer.status().actual_path == PowerPath.ISOLATED
    assert app.power_transfer.status().actual_source == PowerSource.UPS_ONLY
    assert ("turn_on", ENTITIES["grid_power"]) not in switch_calls(fake)


@pytest.mark.asyncio
async def test_two_running_generators_are_not_an_interlock_fault(tmp_path):
    app, fake = make_app(tmp_path)
    await app._tick(0.0)

    fake.states[ENTITIES["generator_a_running"]] = "on"
    await app._tick(1.0)
    fake.states[ENTITIES["generator_b_running"]] = "on"
    await app._tick(2.0)

    assert app.generator_bus.status().owner_slot == GeneratorSlot.A
    assert app.supervisor.phase != SupervisorPhase.RECOVERY_REQUIRED


@pytest.mark.asyncio
async def test_bus_owner_moves_to_second_running_generator_when_first_stops(tmp_path):
    app, fake = make_app(tmp_path)
    await app._tick(0.0)
    fake.states[ENTITIES["generator_a_running"]] = "on"
    await app._tick(1.0)
    fake.states[ENTITIES["generator_b_running"]] = "on"
    await app._tick(2.0)

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
    assert not any(
        domain in {"switch", "button"}
        for domain, _service, _data in fake.calls
    )


@pytest.mark.asyncio
async def test_manual_stop_without_grid_removes_house_load_before_engine_stop(tmp_path):
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

    calls = switch_calls(fake)
    assert calls[0] == ("turn_off", ENTITIES["source_generator"])
    assert ("turn_on", ENTITIES["grid_power"]) in calls
    assert app.power_transfer.status().actual_source == PowerSource.UPS_ONLY
    assert app.generator_controllers[GeneratorSlot.A].phase != GeneratorPhase.IDLE


@pytest.mark.asyncio
async def test_outage_primary_failure_falls_back_to_secondary_and_powers_house(tmp_path):
    app, fake = make_app(
        tmp_path,
        grid_failure_delay=0,
        grid_restore_stable_time=0,
    )
    accelerate_generators(app)
    set_grid_outage(fake)

    now = 0.0
    for _ in range(50):
        await app._tick(now)
        if fake.states[ENTITIES["generator_b_remote"]] == "on":
            fake.states[ENTITIES["generator_b_running"]] = "on"
        if (
            app.supervisor.phase == SupervisorPhase.ON_GENERATOR
            and app.supervisor.session is not None
            and app.supervisor.session.generator == GeneratorSlot.B
        ):
            break
        now += 1.0
    else:
        raise AssertionError("fallback SECONDARY не довёл дом до генераторного питания")

    assert app.supervisor.session is not None
    assert app.supervisor.session.fallback_used is True
    assert fake.states[ENTITIES["generator_a_remote"]] == "off"
    assert fake.states[ENTITIES["generator_b_remote"]] == "on"
    assert fake.states[ENTITIES["house_generator"]] == "on"
    assert app.generator_bus.status().owner_slot == GeneratorSlot.B
    assert switch_calls(fake).count(
        ("turn_on", ENTITIES["generator_b_remote"])
    ) == 1


@pytest.mark.asyncio
async def test_managed_a_dies_external_b_takes_bus_without_becoming_managed(tmp_path):
    app, fake = make_app(
        tmp_path,
        grid_failure_delay=0,
        grid_restore_stable_time=60,
    )
    accelerate_generators(app)
    now = await drive_primary_a_to_house(app, fake)

    fake.states[ENTITIES["generator_b_remote"]] = "on"
    fake.states[ENTITIES["generator_b_running"]] = "on"
    await app._tick(now)
    now += 1.0

    assert app.generator_bus.status().owner_slot == GeneratorSlot.A
    assert (
        app.generator_bus.status().run_contexts[GeneratorSlot.B]
        == GeneratorRunContext.OUTAGE_RELATED
    )

    fake.calls.clear()
    fake.states[ENTITIES["generator_a_running"]] = "off"
    await app._tick(now)

    assert app.supervisor.phase == SupervisorPhase.ON_GENERATOR
    assert app.generator_bus.status().owner_slot == GeneratorSlot.B
    assert app.supervisor.session is not None
    assert app.supervisor.session.generator == GeneratorSlot.A
    assert fake.states[ENTITIES["generator_b_remote"]] == "on"
    assert not any(
        entity_id == ENTITIES["generator_b_remote"]
        for _service, entity_id in switch_calls(fake)
    )


@pytest.mark.asyncio
async def test_stable_grid_returns_house_and_stops_all_outage_related_generators(tmp_path):
    app, fake = make_app(
        tmp_path,
        grid_failure_delay=0,
        grid_restore_stable_time=0,
    )
    accelerate_generators(app)
    now = await drive_primary_a_to_house(app, fake)

    fake.states[ENTITIES["generator_b_remote"]] = "on"
    fake.states[ENTITIES["generator_b_running"]] = "on"
    await app._tick(now)
    now += 1.0

    assert (
        app.generator_bus.status().run_contexts[GeneratorSlot.A]
        == GeneratorRunContext.OUTAGE_RELATED
    )
    assert (
        app.generator_bus.status().run_contexts[GeneratorSlot.B]
        == GeneratorRunContext.OUTAGE_RELATED
    )

    fake.calls.clear()
    fake.states[ENTITIES["grid_ready"]] = "on"
    for _ in range(30):
        await app._tick(now)
        for remote_key, running_key in (
            ("generator_a_remote", "generator_a_running"),
            ("generator_b_remote", "generator_b_running"),
        ):
            if fake.states[ENTITIES[remote_key]] == "off":
                fake.states[ENTITIES[running_key]] = "off"
        if (
            app.supervisor.session is None
            and fake.states[ENTITIES["house_grid"]] == "on"
            and fake.states[ENTITIES["generator_a_running"]] == "off"
            and fake.states[ENTITIES["generator_b_running"]] == "off"
        ):
            break
        now += 1.0
    else:
        raise AssertionError("outage-related генераторы не были полностью остановлены")

    calls = switch_calls(fake)
    deselect = calls.index(("turn_off", ENTITIES["source_generator"]))
    grid_on = calls.index(("turn_on", ENTITIES["grid_power"]))
    a_stop = calls.index(("turn_off", ENTITIES["generator_a_remote"]))
    b_stop = calls.index(("turn_off", ENTITIES["generator_b_remote"]))
    assert deselect < grid_on < a_stop
    assert deselect < grid_on < b_stop


@pytest.mark.asyncio
async def test_test_run_survives_grid_restore(tmp_path):
    app, fake = make_app(
        tmp_path,
        grid_failure_delay=0,
        grid_restore_stable_time=0,
    )
    set_grid_outage(fake, automatic=False)
    await app._tick(0.0)

    fake.states[ENTITIES["test_mode"]] = "on"
    fake.states[ENTITIES["generator_b_remote"]] = "on"
    fake.states[ENTITIES["generator_b_running"]] = "on"
    await app._tick(1.0)
    assert (
        app.generator_bus.status().run_contexts[GeneratorSlot.B]
        == GeneratorRunContext.TEST_RUN
    )

    fake.states[ENTITIES["test_mode"]] = "off"
    fake.calls.clear()
    fake.states[ENTITIES["grid_ready"]] = "on"
    fake.states[ENTITIES["house_grid"]] = "on"
    for now in (2.0, 3.0, 4.0, 5.0):
        await app._tick(now)

    assert fake.states[ENTITIES["generator_b_running"]] == "on"
    assert fake.states[ENTITIES["generator_b_remote"]] == "on"
    assert ("turn_off", ENTITIES["generator_b_remote"]) not in switch_calls(fake)

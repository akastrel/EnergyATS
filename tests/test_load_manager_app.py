"""Интеграционные сценарии Load Manager через полный EnergySupervisorApp.

Каждый тест содержит короткое человеко-читаемое описание защищаемого поведения.
Чистая policy/FSM отдельно покрыта в test_load_manager.py.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from domain import GeneratorSlot
from energy_supervisor import SupervisorPhase
from ha_adapter import ENTITIES
from load_manager import LoadGroup, LoadManagerPhase
from main import DEFAULT_OPTIONS, EnergySupervisorApp
from test_app_adapter import PhysicalFakeClient, attach_fake_client, populated_states


def make_app(tmp_path: Path, **overrides) -> tuple[EnergySupervisorApp, PhysicalFakeClient]:
    journal = tmp_path / "state.json"
    app = EnergySupervisorApp(
        {
            **DEFAULT_OPTIONS,
            "armed": True,
            "state_file": str(journal),
            **overrides,
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


def add_load_entities(
    fake: PhysicalFakeClient,
    *,
    g1: str = "on",
    g2: str = "on",
    meter: str = "on",
    power: str = "1000",
) -> None:
    fake.states.update(
        {
            ENTITIES["generator_a_nominal_power"]: "5600",
            ENTITIES["generator_a_maximum_power"]: "6500",
            ENTITIES["generator_b_nominal_power"]: "5500",
            ENTITIES["generator_b_maximum_power"]: "6000",
            ENTITIES["generator_meter_status"]: meter,
            ENTITIES["generator_power"]: power,
            ENTITIES["load_g1"]: g1,
            ENTITIES["load_g2"]: g2,
        }
    )


def set_outage(fake: PhysicalFakeClient) -> None:
    fake.states[ENTITIES["automatic_transfer"]] = "on"
    fake.states[ENTITIES["grid_ready"]] = "off"
    fake.states[ENTITIES["house_grid"]] = "off"


def switch_calls(fake: PhysicalFakeClient) -> list[tuple[str, str]]:
    return [
        (service, data.get("entity_id"))
        for domain, service, data in fake.calls
        if domain == "switch"
    ]


async def drive_managed_a_to_house(
    app: EnergySupervisorApp,
    fake: PhysicalFakeClient,
    *,
    start: float = 0.0,
) -> float:
    set_outage(fake)
    now = start
    for _ in range(50):
        await app._tick(now)
        if fake.states[ENTITIES["generator_a_remote"]] == "on":
            fake.states[ENTITIES["generator_a_running"]] = "on"
        if app.supervisor.phase == SupervisorPhase.ON_GENERATOR:
            return now + 1.0
        now += 1.0
    raise AssertionError("managed Generator A не дошёл до питания дома")


def set_external_a_on_bus(fake: PhysicalFakeClient, power: float) -> None:
    fake.states[ENTITIES["automatic_transfer"]] = "off"
    fake.states[ENTITIES["grid_ready"]] = "off"
    fake.states[ENTITIES["grid_power"]] = "off"
    fake.states[ENTITIES["house_grid"]] = "off"
    fake.states[ENTITIES["source_generator"]] = "on"
    fake.states[ENTITIES["house_generator"]] = "on"
    fake.states[ENTITIES["generator_a_remote"]] = "on"
    fake.states[ENTITIES["generator_a_running"]] = "on"
    fake.states[ENTITIES["generator_power"]] = str(power)


@pytest.mark.asyncio
async def test_app_load_manager_disabled_does_not_touch_loads_or_block_transfer(tmp_path):
    """При выключенном Load Manager обычный ATS должен работать как раньше даже при отсутствии meter/Load Manager entities. G1/G2 не получают никаких команд и generator transfer не задерживается."""

    app, fake = make_app(
        tmp_path,
        grid_failure_delay=0,
        load_management_enabled=False,
    )
    accelerate_generators(app)
    await drive_managed_a_to_house(app, fake)

    assert app.supervisor.phase == SupervisorPhase.ON_GENERATOR
    assert app.load_manager.phase == LoadManagerPhase.DISABLED
    assert not any(
        entity in {ENTITIES["load_g1"], ENTITIES["load_g2"]}
        for _service, entity in switch_calls(fake)
    )


@pytest.mark.asyncio
async def test_app_pretransfer_sheds_both_groups_before_generator_contactor(tmp_path):
    """После прогрева Generator A Load Manager должен снять обе некритичные группы до подключения дома к generator bus. Только после подтверждения этих OFF TPC получает право выбрать генераторную ветвь."""

    app, fake = make_app(
        tmp_path,
        grid_failure_delay=0,
        load_management_enabled=True,
        load_measurement_stabilization_time=1,
    )
    add_load_entities(fake)
    accelerate_generators(app)
    await drive_managed_a_to_house(app, fake)

    calls = switch_calls(fake)
    g1_off = calls.index(("turn_off", ENTITIES["load_g1"]))
    g2_off = calls.index(("turn_off", ENTITIES["load_g2"]))
    generator_on = calls.index(("turn_on", ENTITIES["source_generator"]))
    assert g1_off < generator_on
    assert g2_off < generator_on
    assert fake.states[ENTITIES["load_g1"]] == "off"
    assert fake.states[ENTITIES["load_g2"]] == "off"
    assert app.load_manager.shed_by_energy_ats[LoadGroup.G1] is True
    assert app.load_manager.shed_by_energy_ats[LoadGroup.G2] is True


@pytest.mark.asyncio
async def test_app_invalid_power_metadata_degrades_only_load_manager(tmp_path):
    """Ошибочные паспортные limits лишают смысла power-based admission, но не сам резервный источник. Дом всё равно должен перейти на генератор после pre-transfer LOAD_SHEDDING, а системный Supervisor не должен уходить в Recovery."""

    app, fake = make_app(
        tmp_path,
        grid_failure_delay=0,
        load_management_enabled=True,
        load_measurement_stabilization_time=1,
    )
    add_load_entities(fake)
    fake.states[ENTITIES["generator_a_nominal_power"]] = "unknown"
    accelerate_generators(app)
    now = await drive_managed_a_to_house(app, fake)
    await app._tick(now)

    assert app.supervisor.phase == SupervisorPhase.ON_GENERATOR
    assert fake.states[ENTITIES["house_generator"]] == "on"
    assert app.load_manager.phase == LoadManagerPhase.DEGRADED
    assert "Nominal/Maximum" in (app.load_manager.degraded_reason or "")


@pytest.mark.asyncio
async def test_app_meter_failure_during_generator_supply_is_local_degraded(tmp_path):
    """Если счётчик генераторной шины пропал уже во время питания дома, Load Manager прекращает power-based решения, но не меняет текущие G1/G2. Работа генератора и core ATS продолжаются без RECOVERY_REQUIRED."""

    app, fake = make_app(
        tmp_path,
        load_management_enabled=True,
        load_measurement_stabilization_time=1,
    )
    add_load_entities(fake, power="3000")
    set_external_a_on_bus(fake, 3000)

    await app._tick(0.0)
    fake.states[ENTITIES["generator_power"]] = "3001"
    await app._tick(1.0)
    assert app.load_manager.phase == LoadManagerPhase.STABLE

    fake.calls.clear()
    fake.states[ENTITIES["generator_meter_status"]] = "off"
    await app._tick(2.0)

    assert app.load_manager.phase == LoadManagerPhase.DEGRADED
    assert app.supervisor.phase != SupervisorPhase.RECOVERY_REQUIRED
    assert fake.states[ENTITIES["load_g1"]] == "on"
    assert fake.states[ENTITIES["load_g2"]] == "on"
    assert not any(
        entity in {ENTITIES["load_g1"], ENTITIES["load_g2"]}
        for _service, entity in switch_calls(fake)
    )


@pytest.mark.asyncio
async def test_app_continuous_nominal_overload_sheds_g2_then_g1(tmp_path):
    """Load Manager должен защищать генератор не только при startup: перегрузка, возникшая позже, снимает G2 первой. Если после нового измерительного окна P всё ещё выше nominal, следующим отдельным шагом снимается G1."""

    app, fake = make_app(
        tmp_path,
        load_management_enabled=True,
        load_measurement_stabilization_time=1,
        nominal_overload_time=0,
        maximum_overload_confirmation_time=0,
    )
    add_load_entities(fake, power="5700")
    set_external_a_on_bus(fake, 5700)

    await app._tick(0.0)
    fake.states[ENTITIES["generator_power"]] = "5701"
    await app._tick(1.0)
    fake.states[ENTITIES["generator_power"]] = "5702"
    await app._tick(2.0)

    fake.states[ENTITIES["generator_power"]] = "5703"
    await app._tick(3.0)
    fake.states[ENTITIES["generator_power"]] = "5704"
    await app._tick(4.0)
    fake.states[ENTITIES["generator_power"]] = "5705"
    await app._tick(5.0)

    calls = switch_calls(fake)
    assert ("turn_off", ENTITIES["load_g2"]) in calls
    assert ("turn_off", ENTITIES["load_g1"]) in calls
    assert calls.index(("turn_off", ENTITIES["load_g2"])) < calls.index(
        ("turn_off", ENTITIES["load_g1"])
    )
    assert app.supervisor.phase != SupervisorPhase.RECOVERY_REQUIRED


@pytest.mark.asyncio
async def test_app_grid_return_restores_only_owned_loads_after_grid_path(tmp_path):
    """После outage сначала должен физически завершиться возврат дома на Grid, и лишь затем Load Manager возвращает собственные отключения. Восстановление идёт G1→G2 и уже не зависит от generator power meter."""

    app, fake = make_app(
        tmp_path,
        grid_failure_delay=0,
        grid_restore_stable_time=0,
        load_management_enabled=True,
        load_measurement_stabilization_time=1,
    )
    add_load_entities(fake, meter="off")
    accelerate_generators(app)
    now = await drive_managed_a_to_house(app, fake)
    assert app.load_manager.shed_by_energy_ats[LoadGroup.G1] is True
    assert app.load_manager.shed_by_energy_ats[LoadGroup.G2] is True

    fake.calls.clear()
    fake.states[ENTITIES["grid_ready"]] = "on"
    for _ in range(30):
        await app._tick(now)
        if fake.states[ENTITIES["generator_a_remote"]] == "off":
            fake.states[ENTITIES["generator_a_running"]] = "off"
        if (
            fake.states[ENTITIES["house_grid"]] == "on"
            and fake.states[ENTITIES["load_g1"]] == "on"
            and fake.states[ENTITIES["load_g2"]] == "on"
            and app.load_manager.shed_by_energy_ats[LoadGroup.G1] is False
            and app.load_manager.shed_by_energy_ats[LoadGroup.G2] is False
        ):
            break
        now += 1.0
    else:
        raise AssertionError("Grid/load restore не завершился и не был подтверждён")

    calls = switch_calls(fake)
    grid_on = calls.index(("turn_on", ENTITIES["grid_power"]))
    g1_on = calls.index(("turn_on", ENTITIES["load_g1"]))
    g2_on = calls.index(("turn_on", ENTITIES["load_g2"]))
    assert grid_on < g1_on < g2_on
    assert app.load_manager.shed_by_energy_ats[LoadGroup.G1] is False
    assert app.load_manager.shed_by_energy_ats[LoadGroup.G2] is False


@pytest.mark.asyncio
async def test_app_status_exposes_load_manager_diagnostics(tmp_path):
    """Оператор должен видеть состояние Load Manager через существующий sensor.energy_ats_status. Status содержит phase, measured power, active limits и ownership обеих групп без создания отдельной скрытой state model."""

    app, fake = make_app(
        tmp_path,
        load_management_enabled=True,
        load_measurement_stabilization_time=1,
    )
    add_load_entities(fake, power="2500")
    set_external_a_on_bus(fake, 2500)
    await app._tick(0.0)
    fake.states[ENTITIES["generator_power"]] = "2501"
    await app._tick(1.0)

    hardware = app._apply_bus_model(app.adapter.snapshot())
    app._refresh_component_views(2.0, hardware)
    payload = app._status_payload(2.0, app._supervisor_observation(hardware))
    attrs = payload["attributes"]
    assert attrs["load_management_enabled"] is True
    assert attrs["load_manager_phase"] == "stable"
    assert attrs["generator_power"] == 2501.0
    assert attrs["active_generator_nominal_power"] == 5600.0
    assert attrs["active_generator_maximum_power"] == 6500.0
    assert attrs["load_g1_state"] == "on"
    assert attrs["load_g2_state"] == "on"

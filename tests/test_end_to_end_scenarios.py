"""Сквозные сценарии Energy ATS от сигналов HA до команд оборудованию."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1] / "energy_ats" / "app"
sys.path.insert(0, str(APP_DIR))

from domain import GeneratorSlot, PowerPath, PowerSource, SessionReason
from energy_supervisor import GeneratorSession, SupervisorPhase
from generator_controller import GeneratorPhase
from ha_adapter import ENTITIES
from main import DEFAULT_OPTIONS, EnergySupervisorApp
from state_store import StateStore
from test_app_adapter import (
    PhysicalFakeClient,
    attach_fake_client,
    populated_states,
    saved_supervisor_payload,
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


@pytest.mark.asyncio
async def test_idle_app_does_not_reconnect_manually_disabled_grid(tmp_path):
    app = EnergySupervisorApp(
        {
            **DEFAULT_OPTIONS,
            "armed": True,
            "state_file": str(tmp_path / "state.json"),
        },
        token="test",
    )
    fake = PhysicalFakeClient()
    fake.states = populated_states()
    attach_fake_client(app, fake)
    await app._tick(0.0)
    fake.calls.clear()

    # Grid перед контактором доступна, но пользователь вручную выбрал
    # Battery path.
    fake.states[ENTITIES["grid_power"]] = "off"
    fake.states[ENTITIES["house_grid"]] = "off"
    await app._tick(1.0)

    assert fake.states[ENTITIES["grid_power"]] == "off"
    assert not any(
        domain == "switch"
        and service == "turn_on"
        and data.get("entity_id") == ENTITIES["grid_power"]
        for domain, service, data in fake.calls
    )
    assert app.supervisor.desired_source is None
    assert app.power_transfer.status().actual_path == PowerPath.BATTERY


@pytest.mark.asyncio
async def test_stable_managed_session_survives_app_restart(tmp_path):
    journal = tmp_path / "state.json"
    StateStore(journal).save(
        saved_supervisor_payload(
            phase=SupervisorPhase.ON_GENERATOR,
            transaction_complete=True,
        )
    )
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
    fake.states.update(
        {
            ENTITIES["grid_power"]: "off",
            ENTITIES["house_grid"]: "off",
            ENTITIES["source_generator"]: "on",
            ENTITIES["house_generator"]: "on",
            ENTITIES["generator_a_remote"]: "on",
            ENTITIES["generator_a_running"]: "on",
        }
    )
    attach_fake_client(app, fake)

    await app._tick(3.0)

    assert app.supervisor.phase == SupervisorPhase.ON_GENERATOR
    assert (
        app.generator_controllers[GeneratorSlot.A].phase
        == GeneratorPhase.READY_FOR_LOAD
    )
    hardware_calls = [
        call for call in fake.calls if call[0] in {"switch", "button"}
    ]
    assert hardware_calls == []


@pytest.mark.asyncio
async def test_external_power_change_after_reconnect_is_never_reasserted(tmp_path):
    journal = tmp_path / "state.json"
    StateStore(journal).save(
        saved_supervisor_payload(
            phase=SupervisorPhase.ON_GENERATOR,
            transaction_complete=True,
        )
    )
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
    fake.states.update(
        {
            ENTITIES["grid_power"]: "off",
            ENTITIES["house_grid"]: "off",
            ENTITIES["source_generator"]: "on",
            ENTITIES["house_generator"]: "on",
            ENTITIES["generator_a_remote"]: "on",
            ENTITIES["generator_a_running"]: "on",
        }
    )
    attach_fake_client(app, fake)
    await app._tick(3.0)

    # Пока HA был недоступен, человек вернул дом на Grid, но оставил
    # управляемый двигатель работать. Старое намерение нельзя применить снова.
    fake.states.update(
        {
            ENTITIES["grid_power"]: "on",
            ENTITIES["house_grid"]: "on",
            ENTITIES["source_generator"]: "off",
            ENTITIES["house_generator"]: "off",
        }
    )
    fake.calls.clear()
    await app._tick(4.0)

    assert app.supervisor.phase == SupervisorPhase.RECOVERY_REQUIRED
    hardware_calls = [
        call for call in fake.calls if call[0] in {"switch", "button"}
    ]
    assert hardware_calls == []


@pytest.mark.asyncio
async def test_disarmed_app_does_not_issue_hardware_commands(tmp_path):
    app = EnergySupervisorApp(
        {
            **DEFAULT_OPTIONS,
            "armed": False,
            "state_file": str(tmp_path / "state.json"),
        },
        token="test",
    )
    fake = PhysicalFakeClient(tmp_path / "state.json")
    fake.states = populated_states()
    attach_fake_client(app, fake)

    await app._tick(0.0)

    assert not any(
        domain in {"switch", "button"}
        for domain, _service, _data in fake.calls
    )


@pytest.mark.asyncio
async def test_app_journals_pending_command_before_hardware_call(tmp_path):
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

    assert fake.pending_seen_before_hardware[0] == [
        {
            "controller": "generator_controller",
            "generator": "A",
            "action": "choke_to_cold_start",
        }
    ]
    saved_after_call = json.loads(journal.read_text(encoding="utf-8"))
    assert saved_after_call["pending_actions"] == []


@pytest.mark.asyncio
async def test_manual_stop_during_start_aborts_without_touching_power_selector(
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
    attach_fake_client(app, fake)

    await app._tick(0.0)
    app.supervisor.request_manual_start()
    await app._tick(1.0)
    await app._tick(2.0)
    app.supervisor.request_manual_stop()
    await app._tick(3.0)

    hardware_calls = [
        (domain, service, data["entity_id"])
        for domain, service, data in fake.calls
        if domain in {"switch", "button"} and "entity_id" in data
    ]
    assert hardware_calls == [
        ("button", "press", ENTITIES["generator_a_choke_cold_start"]),
        ("switch", "turn_on", ENTITIES["generator_a_remote"]),
        ("switch", "turn_off", ENTITIES["generator_a_remote"]),
        ("button", "press", ENTITIES["generator_a_choke_run"]),
    ]
    assert fake.states[ENTITIES["source_generator"]] == "off"


@pytest.mark.asyncio
async def test_recovery_reset_succeeds_only_from_safe_normal_topology(tmp_path):
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
    attach_fake_client(app, fake)
    await app._tick(0.0)

    app.supervisor.require_recovery("test")
    app.supervisor.request_recovery_reset()
    await app._tick(1.0)

    assert app.supervisor.phase == SupervisorPhase.NORMAL
    assert all(
        controller.phase == GeneratorPhase.IDLE
        for controller in app.generator_controllers.values()
    )


@pytest.mark.asyncio
async def test_recovery_reset_connects_grid_path_from_battery_path(tmp_path):
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
    fake.states.update(
        {
            ENTITIES["grid_ready"]: "off",
            ENTITIES["grid_power"]: "off",
            ENTITIES["house_grid"]: "off",
        }
    )
    attach_fake_client(app, fake)
    await app._tick(0.0)

    app.supervisor.require_recovery("test")
    app.supervisor.request_recovery_reset()
    await app._tick(1.0)

    assert app.supervisor.phase == SupervisorPhase.RECOVERY_REQUIRED
    assert fake.states[ENTITIES["grid_power"]] == "on"

    await app._tick(2.0)

    assert app.supervisor.phase == SupervisorPhase.NORMAL
    assert app.power_transfer.status().actual_path == PowerPath.GRID


@pytest.mark.asyncio
async def test_recovery_reset_returns_from_generator_then_stops_it(tmp_path):
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
    attach_fake_client(app, fake)
    await app._tick(0.0)

    app.supervisor.session = GeneratorSession.begin(
        reason=SessionReason.MANUAL_GENERATOR_START,
        generator=GeneratorSlot.A,
        now=0.0,
        grid_was_unavailable=False,
    )
    app.supervisor.desired_generators[GeneratorSlot.A] = True
    app.supervisor.require_recovery("test")
    fake.states.update(
        {
            ENTITIES["grid_power"]: "off",
            ENTITIES["house_grid"]: "off",
            ENTITIES["source_generator"]: "on",
            ENTITIES["house_generator"]: "on",
            ENTITIES["generator_a_running"]: "on",
            ENTITIES["generator_a_remote"]: "on",
        }
    )

    app.supervisor.request_recovery_reset()
    await app._tick(1.0)    # снять генераторную шину
    await app._tick(2.0)    # подключить Grid path
    await app._tick(3.0)    # начать cooldown
    await app._tick(303.0)  # снять REMOTE
    fake.states[ENTITIES["generator_a_running"]] = "off"
    await app._tick(304.0)  # подтвердить остановку и завершить reset

    assert app.supervisor.phase == SupervisorPhase.NORMAL
    assert app.supervisor.session is None
    hardware_calls = [
        (domain, service, data.get("entity_id"))
        for domain, service, data in fake.calls
        if domain in {"switch", "button"}
    ]
    assert hardware_calls[-4:] == [
        ("switch", "turn_off", ENTITIES["source_generator"]),
        ("switch", "turn_on", ENTITIES["grid_power"]),
        ("button", "press", ENTITIES["generator_a_choke_run"]),
        ("switch", "turn_off", ENTITIES["generator_a_remote"]),
    ]


@pytest.mark.asyncio
async def test_complete_manual_session_obeys_controller_boundaries(tmp_path):
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
    await app._tick(13.0)  # заслонка -> run
    await app._tick(43.0)  # прогрев завершён
    await app._tick(44.0)  # Grid OFF
    await app._tick(45.0)  # selector -> Generator
    await app._tick(46.0)  # подтверждение selector
    await app._tick(47.0)  # Supervisor подтверждает питание дома

    app.supervisor.request_manual_stop()
    await app._tick(48.0)  # selector -> normal
    await app._tick(49.0)  # Grid power -> ON
    await app._tick(50.0)  # подтверждение Grid path
    await app._tick(51.0)  # начинается cooldown
    await app._tick(110.0)
    assert fake.states[ENTITIES["generator_a_remote"]] == "on"
    await app._tick(111.0)  # cooldown окончен, REMOTE OFF

    fake.states[ENTITIES["generator_a_running"]] = "off"
    await app._tick(112.0)
    await app._tick(113.0)
    assert app.supervisor.session is None
    hardware_calls = [
        (domain, service, data["entity_id"])
        for domain, service, data in fake.calls
        if domain in {"switch", "button"} and "entity_id" in data
    ]
    assert hardware_calls == [
        ("button", "press", ENTITIES["generator_a_choke_cold_start"]),
        ("switch", "turn_on", ENTITIES["generator_a_remote"]),
        ("button", "press", ENTITIES["generator_a_choke_run"]),
        ("switch", "turn_off", ENTITIES["grid_power"]),
        ("switch", "turn_on", ENTITIES["source_generator"]),
        ("switch", "turn_off", ENTITIES["source_generator"]),
        ("switch", "turn_on", ENTITIES["grid_power"]),
        ("switch", "turn_off", ENTITIES["generator_a_remote"]),
    ]


@pytest.mark.asyncio
async def test_manual_stop_without_grid_restores_grid_relay_before_engine_stop(
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
    fake.states[ENTITIES["grid_ready"]] = "off"
    fake.states[ENTITIES["house_grid"]] = "off"
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
    await app._tick(44.0)
    await app._tick(45.0)
    await app._tick(46.0)
    await app._tick(47.0)

    assert fake.states[ENTITIES["grid_power"]] == "off"
    assert fake.states[ENTITIES["source_generator"]] == "on"
    fake.calls.clear()

    app.supervisor.request_manual_stop()
    await app._tick(48.0)
    await app._tick(49.0)
    await app._tick(50.0)
    await app._tick(51.0)
    await app._tick(52.0)

    hardware_calls = [
        (domain, service, data["entity_id"])
        for domain, service, data in fake.calls
        if domain in {"switch", "button"} and "entity_id" in data
    ]
    assert hardware_calls[:2] == [
        ("switch", "turn_off", ENTITIES["source_generator"]),
        ("switch", "turn_on", ENTITIES["grid_power"]),
    ]
    assert fake.states[ENTITIES["grid_power"]] == "on"
    assert fake.states[ENTITIES["house_grid"]] == "off"
    assert fake.states[ENTITIES["generator_a_remote"]] == "on"
    assert app.power_transfer.status().actual_source == PowerSource.BATTERY
    assert app.power_transfer.status().actual_path == PowerPath.GRID
    assert app.supervisor.phase == SupervisorPhase.STOPPING_GENERATOR

    await app._tick(112.0)
    assert fake.states[ENTITIES["generator_a_remote"]] == "off"
    hardware_calls = [
        (domain, service, data["entity_id"])
        for domain, service, data in fake.calls
        if domain in {"switch", "button"} and "entity_id" in data
    ]
    assert hardware_calls[-1] == (
        "switch",
        "turn_off",
        ENTITIES["generator_a_remote"],
    )
    fake.states[ENTITIES["generator_a_running"]] = "off"
    await app._tick(113.0)
    await app._tick(114.0)
    assert app.supervisor.session is None


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_grid", ["on", None, "unknown", "unavailable"])
async def test_app_starts_when_ha_data_is_ready_without_fixed_delay(
    tmp_path, monkeypatch, initial_grid
):
    import asyncio

    app = EnergySupervisorApp(
        {**DEFAULT_OPTIONS, "armed": True,
         "state_file": str(tmp_path / "state.json")},
        token="test",
    )
    fake = PhysicalFakeClient()
    fake.states = populated_states()
    if initial_grid is None:
        fake.states.pop(ENTITIES["grid_ready"])
    else:
        fake.states[ENTITIES["grid_ready"]] = initial_grid
    fake.connected = asyncio.Event()
    attach_fake_client(app, fake)
    readiness_waits = []
    ticks = []

    async def connect():
        fake.connected.set()

    async def close():
        fake.connected.clear()

    async def wait(seconds):
        if app.stop_event.is_set():
            return True
        # Любая пауза до connect — регрессия удалённой startup_delay.
        assert fake.connected.is_set()
        assert not app.commands_ready
        assert seconds == 1.0
        assert not fake.calls
        readiness_waits.append(seconds)
        fake.states[ENTITIES["grid_ready"]] = "on"
        return False

    original_tick = app._tick

    async def tick(now):
        assert app.commands_ready
        assert fake.states[ENTITIES["grid_ready"]] == "on"
        await original_tick(now)
        ticks.append(app.supervisor.phase)
        app.request_stop()

    monkeypatch.setattr(fake, "connect", connect, raising=False)
    monkeypatch.setattr(fake, "close", close, raising=False)
    monkeypatch.setattr(app, "_stop_requested_within", wait)
    monkeypatch.setattr(app, "_tick", tick)

    await app.run()

    assert readiness_waits == ([] if initial_grid == "on" else [1.0])
    assert ticks == [SupervisorPhase.NORMAL]
    assert not [call for call in fake.calls if call[0] in {"switch", "button"}]

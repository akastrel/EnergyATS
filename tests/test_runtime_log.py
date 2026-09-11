"""Человеко-читаемое runtime-состояние EnergyATS."""

from __future__ import annotations

import logging
from dataclasses import replace
from types import SimpleNamespace

from domain import GeneratorSlot, PowerPath, PowerSource
from energy_supervisor import SupervisorObservation
from exercise_scheduler import ExerciseConfig, ExerciseScheduler
from generator_bus import GeneratorBusTracker
from generator_controller import (
    GeneratorController,
    GeneratorPhase,
    GeneratorStatus,
    default_generator_profiles,
)
from load_manager import LoadManager, LoadManagerConfig
from main import EnergySupervisorApp
from outage_power_policy import OutagePowerPolicy
from power_transfer import PowerTransferStatus, TransferPhase


def generator_status(slot: GeneratorSlot, phase: GeneratorPhase) -> GeneratorStatus:
    running = phase != GeneratorPhase.IDLE
    return GeneratorStatus(
        slot=slot,
        display_name=slot.value,
        phase=phase,
        running=running,
        remote_on=running,
        ready_for_load=phase == GeneratorPhase.READY_FOR_LOAD,
        fault=None,
    )


def observation(
    *,
    grid_ready=True,
    source=PowerSource.GRID,
    path=PowerPath.GRID,
    phase=TransferPhase.STABLE_GRID,
    target=PowerSource.GRID,
    a_phase=GeneratorPhase.IDLE,
    b_phase=GeneratorPhase.IDLE,
):
    return SupervisorObservation(
        grid_ready=grid_ready,
        automatic_transfer_enabled=True,
        emergency_stop=False,
        power=PowerTransferStatus(
            phase=phase,
            actual_source=source,
            actual_path=path,
            target_source=target,
            transition_in_progress=phase in {
                TransferPhase.DISCONNECTING_GRID,
                TransferPhase.SELECTING_GENERATOR,
                TransferPhase.DISCONNECTING_GENERATOR,
                TransferPhase.CONNECTING_GRID,
            },
            recovery_required=phase == TransferPhase.RECOVERY_REQUIRED,
            fault=None,
        ),
        generators={
            GeneratorSlot.A: generator_status(GeneratorSlot.A, a_phase),
            GeneratorSlot.B: generator_status(GeneratorSlot.B, b_phase),
        },
    )


def app_for_log() -> EnergySupervisorApp:
    app = object.__new__(EnergySupervisorApp)
    profiles = default_generator_profiles()
    profiles[GeneratorSlot.A] = replace(
        profiles[GeneratorSlot.A], display_name="Elemax"
    )
    profiles[GeneratorSlot.B] = replace(
        profiles[GeneratorSlot.B], display_name="Вепрь"
    )
    app.generator_controllers = {
        slot: GeneratorController(profile)
        for slot, profile in profiles.items()
    }
    app.exercise_scheduler = ExerciseScheduler(
        {
            GeneratorSlot.A: ExerciseConfig(False, 30, "15:00", 10, 7),
            GeneratorSlot.B: ExerciseConfig(False, 45, "15:00", 10, 14),
        }
    )
    app.load_manager = LoadManager(LoadManagerConfig(enabled=False))
    app.outage_power_policy = OutagePowerPolicy()
    app.armed = True
    app._last_runtime_signature = None
    app.log = logging.getLogger("test_runtime_log")
    app.generator_bus = GeneratorBusTracker()
    app.supervisor = SimpleNamespace(
        config=SimpleNamespace(primary_generator=GeneratorSlot.A),
        session=None,
        status_text=lambda _observation: "Питание от основной сети",
    )
    return app


def test_stable_grid_log_names_generators_and_bus(caplog):
    app = app_for_log()
    with caplog.at_level(logging.INFO, logger="test_runtime_log"):
        app._log_runtime_if_changed(observation())

    assert caplog.messages == [
        "Состояние: Питание от основной сети; Grid=ON; AVR=ON; power=Grid; "
        "bus=unknown; Elemax: остановлен; Вепрь: остановлен; primary=Elemax."
    ]


def test_ups_only_is_not_reported_as_battery_path(caplog):
    app = app_for_log()
    app.supervisor.status_text = lambda _observation: "В доме работает только UPS линия"
    obs = observation(
        grid_ready=False,
        source=PowerSource.UPS_ONLY,
        path=PowerPath.ISOLATED,
        phase=TransferPhase.STABLE_ISOLATED,
        target=None,
    )
    with caplog.at_level(logging.INFO, logger="test_runtime_log"):
        app._log_runtime_if_changed(obs)
    assert "power=UPS only" in caplog.messages[0]
    assert "Battery" not in caplog.messages[0]


def test_generator_owner_is_named_and_reported_under_load(caplog):
    app = app_for_log()
    app.generator_bus.update(
        {GeneratorSlot.A: True, GeneratorSlot.B: False},
        grid_ready=False,
        test_mode=False,
        managed_slot=GeneratorSlot.A,
        managed_outage=True,
    )
    app.supervisor.status_text = lambda _observation: "Питание от генератора"
    obs = observation(
        grid_ready=False,
        source=PowerSource.GENERATOR,
        path=PowerPath.GENERATOR,
        phase=TransferPhase.STABLE_GENERATOR,
        target=PowerSource.GENERATOR,
        a_phase=GeneratorPhase.READY_FOR_LOAD,
    )
    with caplog.at_level(logging.INFO, logger="test_runtime_log"):
        app._log_runtime_if_changed(obs)

    assert "power=Generator Elemax" in caplog.messages[0]
    assert "bus=Elemax" in caplog.messages[0]
    assert "Elemax: под нагрузкой" in caplog.messages[0]


def test_grid_change_changes_visible_signature(caplog):
    app = app_for_log()
    with caplog.at_level(logging.INFO, logger="test_runtime_log"):
        app._log_runtime_if_changed(observation(grid_ready=False))
        app._log_runtime_if_changed(observation(grid_ready=True))
    assert len(caplog.messages) == 2
    assert "Grid=OFF" in caplog.messages[0]
    assert "Grid=ON" in caplog.messages[1]

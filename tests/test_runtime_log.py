"""Человеко-читаемое runtime-представление АВР."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from domain import GeneratorSlot, PowerPath, PowerSource
from energy_supervisor import SupervisorObservation, SupervisorPhase
from exercise_scheduler import ExerciseConfig, ExerciseScheduler
from generator_bus import GeneratorBusTracker
from generator_controller import (
    GeneratorController,
    GeneratorPhase,
    GeneratorStatus,
    default_generator_profiles,
)
from load_manager import LoadManager, LoadManagerConfig
from operator_status import _runtime_signature
from ups_run import UPSRun
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


def runtime_context():
    profiles = default_generator_profiles()
    profiles[GeneratorSlot.A] = replace(
        profiles[GeneratorSlot.A], display_name="Elemax"
    )
    profiles[GeneratorSlot.B] = replace(
        profiles[GeneratorSlot.B], display_name="Вепрь"
    )
    controllers = {
        slot: GeneratorController(profile)
        for slot, profile in profiles.items()
    }
    exercise = ExerciseScheduler(
        {
            GeneratorSlot.A: ExerciseConfig(False, 30, "15:00", 10, 7),
            GeneratorSlot.B: ExerciseConfig(False, 45, "15:00", 10, 14),
        }
    )
    load_manager = LoadManager(LoadManagerConfig(enabled=False))
    ups_run = UPSRun()
    bus = GeneratorBusTracker()
    supervisor = SimpleNamespace(
        phase=SupervisorPhase.NORMAL,
        config=SimpleNamespace(primary_generator=GeneratorSlot.A),
        session=None,
        status_text=lambda _observation: "Питание от основной сети",
    )
    return supervisor, bus, controllers, exercise, load_manager, ups_run


def runtime_message(obs: SupervisorObservation, *, context=None) -> str:
    if context is None:
        context = runtime_context()
    supervisor, bus, controllers, exercise, load_manager, ups_run = context
    signature = _runtime_signature(
        armed=True,
        observation=obs,
        supervisor=supervisor,
        bus_status=bus.status(),
        generator_controllers=controllers,
        load_manager=load_manager,
        ups_run=ups_run,
        exercise_scheduler=exercise,
    )
    return "; ".join(signature) + "."


def test_stable_grid_log_names_generators_and_bus():
    assert runtime_message(observation()) == (
        "Состояние: Питание от основной сети; Grid=ON; AVR=ON; power=Grid; "
        "bus=unknown; Elemax: остановлен; Вепрь: остановлен; primary=Elemax."
    )


def test_ups_only_is_not_reported_as_battery_path():
    context = runtime_context()
    supervisor = context[0]
    supervisor.status_text = lambda _observation: "В доме работает только UPS линия"
    message = runtime_message(
        observation(
            grid_ready=False,
            source=PowerSource.UPS_ONLY,
            path=PowerPath.ISOLATED,
            phase=TransferPhase.STABLE_ISOLATED,
            target=None,
        ),
        context=context,
    )
    assert "power=UPS only" in message
    assert "Battery" not in message


def test_generator_owner_is_named_and_reported_under_load():
    context = runtime_context()
    supervisor, bus, *_ = context
    bus.update(
        {GeneratorSlot.A: True, GeneratorSlot.B: False},
        grid_ready=False,
        test_mode=False,
        managed_slot=GeneratorSlot.A,
        managed_outage=True,
    )
    supervisor.status_text = lambda _observation: "Питание от генератора"
    message = runtime_message(
        observation(
            grid_ready=False,
            source=PowerSource.GENERATOR,
            path=PowerPath.GENERATOR,
            phase=TransferPhase.STABLE_GENERATOR,
            target=PowerSource.GENERATOR,
            a_phase=GeneratorPhase.READY_FOR_LOAD,
        ),
        context=context,
    )

    assert "power=Generator Elemax" in message
    assert "bus=Elemax" in message
    assert "Elemax: под нагрузкой" in message


def test_grid_change_changes_visible_signature():
    context = runtime_context()
    off = runtime_message(observation(grid_ready=False), context=context)
    on = runtime_message(observation(grid_ready=True), context=context)
    assert off != on
    assert "Grid=OFF" in off
    assert "Grid=ON" in on

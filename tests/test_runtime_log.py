"""Человеко-читаемое представление runtime-состояния Energy ATS."""

from __future__ import annotations

import logging
from dataclasses import replace
from types import SimpleNamespace

from domain import GeneratorSlot, PowerPath, PowerSource
from energy_supervisor import SupervisorObservation
from generator_controller import GeneratorPhase, GeneratorStatus, default_generator_profiles
from main import EnergySupervisorApp
from power_transfer import PowerTransferStatus, TransferPhase


def _generator_status(slot: GeneratorSlot, phase: GeneratorPhase) -> GeneratorStatus:
    return GeneratorStatus(
        slot=slot,
        display_name=slot.value,
        phase=phase,
        running=phase != GeneratorPhase.IDLE,
        remote_on=phase != GeneratorPhase.IDLE,
        ready_for_load=phase == GeneratorPhase.READY_FOR_LOAD,
        externally_started=phase == GeneratorPhase.EXTERNAL_RUNNING,
        fault=None,
        start_temperature=None,
        start_temperature_source=None,
    )


def _observation(
    *,
    grid_ready: bool = True,
    source: PowerSource = PowerSource.GRID,
    path: PowerPath = PowerPath.GRID,
    transfer_phase: TransferPhase = TransferPhase.STABLE_GRID_PATH,
    target_source: PowerSource | None = PowerSource.GRID,
    generator_a_phase: GeneratorPhase = GeneratorPhase.IDLE,
    generator_b_phase: GeneratorPhase = GeneratorPhase.IDLE,
) -> SupervisorObservation:
    return SupervisorObservation(
        grid_ready=grid_ready,
        automatic_transfer_enabled=True,
        emergency_stop=False,
        power=PowerTransferStatus(
            phase=transfer_phase,
            actual_source=source,
            actual_path=path,
            target_source=target_source,
            transition_in_progress=transfer_phase
            in {
                TransferPhase.DISCONNECTING_GRID,
                TransferPhase.SELECTING_GENERATOR,
                TransferPhase.DISCONNECTING_GENERATOR,
                TransferPhase.CONNECTING_GRID,
            },
            recovery_required=transfer_phase == TransferPhase.RECOVERY_REQUIRED,
            fault=None,
        ),
        generators={
            GeneratorSlot.A: _generator_status(GeneratorSlot.A, generator_a_phase),
            GeneratorSlot.B: _generator_status(GeneratorSlot.B, generator_b_phase),
        },
    )


def _app() -> EnergySupervisorApp:
    app = object.__new__(EnergySupervisorApp)
    profiles = default_generator_profiles()
    profiles[GeneratorSlot.A] = replace(
        profiles[GeneratorSlot.A], display_name="Elemax"
    )
    profiles[GeneratorSlot.B] = replace(
        profiles[GeneratorSlot.B], display_name="Вепрь"
    )
    app.profiles = profiles
    app.armed = True
    app._last_runtime_signature = None
    app.log = logging.getLogger("test_runtime_log")
    app.supervisor = SimpleNamespace(
        config=SimpleNamespace(primary_generator=GeneratorSlot.A),
        session=None,
        status_text=lambda observation: "Питание от основной сети",
    )
    return app


def test_stable_grid_log_uses_names_and_hides_transfer(caplog) -> None:
    app = _app()
    observation = _observation()

    with caplog.at_level(logging.INFO, logger="test_runtime_log"):
        app._log_runtime_if_changed(observation)

    assert caplog.messages == [
        "Состояние: Питание от основной сети; Grid=ON; AVR=ON; power=Grid; "
        "Elemax: остановлен; Вепрь: остановлен; primary=Elemax."
    ]


def test_generator_transfer_names_target_generator(caplog) -> None:
    app = _app()
    observation = _observation(
        grid_ready=False,
        source=PowerSource.BATTERY,
        path=PowerPath.BATTERY,
        transfer_phase=TransferPhase.SELECTING_GENERATOR,
        target_source=PowerSource.GENERATOR_A,
        generator_a_phase=GeneratorPhase.READY_FOR_LOAD,
    )
    app.supervisor.status_text = lambda observation: "Переключение на генератор"

    with caplog.at_level(logging.INFO, logger="test_runtime_log"):
        app._log_runtime_if_changed(observation)

    assert caplog.messages == [
        "Состояние: Переключение на генератор; Grid=OFF; AVR=ON; power=Battery; "
        "transfer=connecting Elemax; Elemax: готов; Вепрь: остановлен; "
        "primary=Elemax."
    ]


def test_generator_under_load_is_reported_as_loaded(caplog) -> None:
    app = _app()
    observation = _observation(
        grid_ready=False,
        source=PowerSource.GENERATOR_A,
        path=PowerPath.GENERATOR,
        transfer_phase=TransferPhase.STABLE_GENERATOR,
        target_source=PowerSource.GENERATOR_A,
        generator_a_phase=GeneratorPhase.READY_FOR_LOAD,
    )
    app.supervisor.status_text = lambda observation: "Питание от генератора"

    with caplog.at_level(logging.INFO, logger="test_runtime_log"):
        app._log_runtime_if_changed(observation)

    assert caplog.messages == [
        "Состояние: Питание от генератора; Grid=OFF; AVR=ON; "
        "power=Generator Elemax; Elemax: под нагрузкой; Вепрь: остановлен; "
        "primary=Elemax."
    ]


def test_grid_change_is_part_of_visible_signature(caplog) -> None:
    app = _app()

    with caplog.at_level(logging.INFO, logger="test_runtime_log"):
        app._log_runtime_if_changed(_observation(grid_ready=False))
        app._log_runtime_if_changed(_observation(grid_ready=True))

    assert len(caplog.messages) == 2
    assert "Grid=OFF" in caplog.messages[0]
    assert "Grid=ON" in caplog.messages[1]

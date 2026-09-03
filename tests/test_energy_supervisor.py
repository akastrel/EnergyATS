from __future__ import annotations

from dataclasses import replace

import pytest

from domain import GeneratorSlot, PowerSource, Transaction, TransactionStatus
from energy_supervisor import (
    EnergySupervisor,
    SupervisorConfig,
    SupervisorObservation,
    SupervisorPhase,
)
from generator_controller import GeneratorPhase, GeneratorStatus
from power_transfer import PowerTransferStatus, TransferPhase


def generator_status(
    slot: GeneratorSlot,
    *,
    phase: GeneratorPhase = GeneratorPhase.IDLE,
    running: bool | None = False,
    remote_on: bool | None = False,
    ready: bool = False,
    external: bool = False,
    fault: str | None = None,
) -> GeneratorStatus:
    return GeneratorStatus(
        slot=slot,
        display_name="Elemax" if slot == GeneratorSlot.A else "Вепрь",
        phase=phase,
        running=running,
        remote_on=remote_on,
        ready_for_load=ready,
        externally_started=external,
        fault=fault,
        start_temperature=None,
        start_temperature_source=None,
    )


def power_status(source: PowerSource) -> PowerTransferStatus:
    if source == PowerSource.GRID:
        phase = TransferPhase.STABLE_GRID
    elif source == PowerSource.BATTERY:
        phase = TransferPhase.STABLE_BATTERY
    else:
        phase = TransferPhase.STABLE_GENERATOR
    return PowerTransferStatus(
        phase=phase,
        actual_source=source,
        target_source=source,
        transition_in_progress=False,
        recovery_required=False,
        fault=None,
    )


def observation(
    *,
    grid_ready: bool = True,
    source: PowerSource = PowerSource.GRID,
    automatic: bool = False,
    a: GeneratorStatus | None = None,
    b: GeneratorStatus | None = None,
    power_inputs_known: bool = True,
) -> SupervisorObservation:
    return SupervisorObservation(
        grid_ready=grid_ready,
        automatic_transfer_enabled=automatic,
        emergency_stop=False,
        power=power_status(source),
        generators={
            GeneratorSlot.A: a or generator_status(GeneratorSlot.A),
            GeneratorSlot.B: b or generator_status(GeneratorSlot.B),
        },
        power_inputs_known=power_inputs_known,
    )


def ready_a() -> GeneratorStatus:
    return generator_status(
        GeneratorSlot.A,
        phase=GeneratorPhase.READY_FOR_LOAD,
        running=True,
        remote_on=True,
        ready=True,
    )


def enter_manual_session(
    supervisor: EnergySupervisor,
    *,
    grid_ready: bool,
    initial_source: PowerSource,
) -> None:
    supervisor.step(
        0.0,
        observation(grid_ready=grid_ready, source=initial_source),
    )
    supervisor.request_manual_start()
    supervisor.step(
        1.0,
        observation(grid_ready=grid_ready, source=initial_source),
    )
    supervisor.step(
        2.0,
        observation(
            grid_ready=grid_ready,
            source=initial_source,
            a=ready_a(),
        ),
    )
    supervisor.step(
        3.0,
        observation(
            grid_ready=grid_ready,
            source=PowerSource.GENERATOR_A,
            a=ready_a(),
        ),
    )
    assert supervisor.phase == SupervisorPhase.ON_GENERATOR


def test_manual_start_with_grid_present_stays_on_generator_until_manual_stop():
    supervisor = EnergySupervisor()
    enter_manual_session(
        supervisor,
        grid_ready=True,
        initial_source=PowerSource.GRID,
    )

    decision = supervisor.step(
        1000.0,
        observation(
            grid_ready=True,
            source=PowerSource.GENERATOR_A,
            a=ready_a(),
        ),
    )
    assert supervisor.phase == SupervisorPhase.ON_GENERATOR
    assert decision.desired_source == PowerSource.GENERATOR_A


def test_repeated_manual_start_does_not_create_a_second_session():
    supervisor = EnergySupervisor()
    supervisor.step(0.0, observation())
    supervisor.request_manual_start()
    supervisor.step(1.0, observation())
    assert supervisor.session is not None
    original_session_id = supervisor.session.session_id

    supervisor.request_manual_start()
    decision = supervisor.step(2.0, observation())

    assert supervisor.session.session_id == original_session_id
    assert any("сессия уже активна" in event.message for event in decision.events)


def test_primary_and_enabled_flags_are_policy_not_transfer_logic():
    supervisor = EnergySupervisor(
        SupervisorConfig(
            primary_generator=GeneratorSlot.B,
            generator_b_enabled=False,
        )
    )
    supervisor.step(0.0, observation())
    supervisor.request_manual_start()
    decision = supervisor.step(1.0, observation())

    assert supervisor.session is not None
    assert supervisor.session.generator == GeneratorSlot.A
    assert decision.desired_generators == {
        GeneratorSlot.A: True,
        GeneratorSlot.B: False,
    }


def test_manual_start_is_rejected_while_emergency_stop_is_active():
    supervisor = EnergySupervisor()
    emergency = replace(observation(), emergency_stop=True)
    supervisor.step(0.0, emergency)
    supervisor.request_manual_start()
    decision = supervisor.step(1.0, emergency)

    assert supervisor.session is None
    assert supervisor.phase == SupervisorPhase.RECOVERY_REQUIRED
    assert decision.actions_allowed is False


def test_manual_outage_session_returns_house_to_grid_but_leaves_engine_running():
    supervisor = EnergySupervisor(
        SupervisorConfig(grid_restore_stable_time=60.0)
    )
    enter_manual_session(
        supervisor,
        grid_ready=False,
        initial_source=PowerSource.BATTERY,
    )

    supervisor.step(
        10.0,
        observation(
            grid_ready=True,
            source=PowerSource.GENERATOR_A,
            a=ready_a(),
        ),
    )
    decision = supervisor.step(
        70.0,
        observation(
            grid_ready=True,
            source=PowerSource.GENERATOR_A,
            a=ready_a(),
        ),
    )
    assert supervisor.phase == SupervisorPhase.RETURNING_TO_NORMAL
    assert decision.desired_source == PowerSource.GRID

    decision = supervisor.step(
        71.0,
        observation(grid_ready=True, source=PowerSource.GRID, a=ready_a()),
    )
    assert supervisor.phase == SupervisorPhase.MANUAL_GENERATOR_IDLE
    assert decision.desired_generators[GeneratorSlot.A] is True
    assert decision.desired_source == PowerSource.GRID


def test_manual_stop_without_grid_targets_battery_without_warning():
    supervisor = EnergySupervisor()
    enter_manual_session(
        supervisor,
        grid_ready=False,
        initial_source=PowerSource.BATTERY,
    )

    supervisor.request_manual_stop()
    decision = supervisor.step(
        4.0,
        observation(
            grid_ready=False,
            source=PowerSource.GENERATOR_A,
            a=ready_a(),
        ),
    )
    assert supervisor.phase == SupervisorPhase.RETURNING_TO_NORMAL
    assert decision.desired_source == PowerSource.BATTERY
    assert not any("невозмож" in event.message.lower() for event in decision.events)

    decision = supervisor.step(
        5.0,
        observation(grid_ready=False, source=PowerSource.BATTERY, a=ready_a()),
    )
    assert supervisor.phase == SupervisorPhase.STOPPING_GENERATOR
    assert decision.desired_generators[GeneratorSlot.A] is False


def test_manual_stop_without_grid_suppresses_automatic_restart():
    supervisor = EnergySupervisor(SupervisorConfig(grid_failure_delay=0.0))
    enter_manual_session(
        supervisor,
        grid_ready=False,
        initial_source=PowerSource.BATTERY,
    )
    supervisor.request_manual_stop()
    supervisor.step(
        4.0,
        observation(
            grid_ready=False,
            source=PowerSource.GENERATOR_A,
            automatic=True,
            a=ready_a(),
        ),
    )
    supervisor.step(
        5.0,
        observation(
            grid_ready=False,
            source=PowerSource.BATTERY,
            automatic=True,
            a=ready_a(),
        ),
    )
    supervisor.step(
        6.0,
        observation(
            grid_ready=False,
            source=PowerSource.BATTERY,
            automatic=True,
        ),
    )
    assert supervisor.session is None
    assert supervisor.automatic_start_suppressed_until_grid is True

    # Ручное решение не должно забываться после restart App.
    supervisor = EnergySupervisor.from_dict(
        supervisor.to_dict(),
        SupervisorConfig(grid_failure_delay=0.0),
    )
    supervisor.step(
        100.0,
        observation(
            grid_ready=False,
            source=PowerSource.BATTERY,
            automatic=True,
        ),
    )
    assert supervisor.session is None

    supervisor.request_manual_start()
    supervisor.step(
        101.0,
        observation(
            grid_ready=False,
            source=PowerSource.BATTERY,
            automatic=True,
        ),
    )
    assert supervisor.session is not None
    assert supervisor.automatic_start_suppressed_until_grid is False


def test_external_start_only_notifies_and_disables_all_commands():
    supervisor = EnergySupervisor()
    external = generator_status(
        GeneratorSlot.A,
        phase=GeneratorPhase.EXTERNAL_RUNNING,
        running=True,
        remote_on=False,
        external=True,
    )
    decision = supervisor.step(0.0, observation(a=external))

    assert supervisor.phase == SupervisorPhase.EXTERNAL_RUNNING
    assert decision.actions_allowed is False
    assert decision.desired_generators == {
        GeneratorSlot.A: False,
        GeneratorSlot.B: False,
    }
    assert any("только наблюдает" in event.message for event in decision.events)


def test_generator_failure_is_isolated_without_automatic_fallback():
    supervisor = EnergySupervisor()
    enter_manual_session(
        supervisor,
        grid_ready=False,
        initial_source=PowerSource.BATTERY,
    )
    failed = generator_status(
        GeneratorSlot.A,
        phase=GeneratorPhase.FAULT,
        running=False,
        remote_on=True,
        fault="нет RUNNING",
    )

    decision = supervisor.step(
        4.0,
        observation(
            grid_ready=False,
            source=PowerSource.GENERATOR_A,
            a=failed,
        ),
    )
    assert supervisor.phase == SupervisorPhase.ISOLATING_FAILED_SOURCE
    assert decision.desired_source == PowerSource.BATTERY
    assert decision.desired_generators[GeneratorSlot.A] is False
    assert decision.desired_generators[GeneratorSlot.B] is False
    assert supervisor.transaction is not None
    assert supervisor.transaction.kind == "isolate_failed_source"
    assert supervisor.transaction.status == TransactionStatus.IN_PROGRESS

    supervisor.step(
        5.0,
        observation(grid_ready=False, source=PowerSource.BATTERY, a=failed),
    )
    assert supervisor.phase == SupervisorPhase.RECOVERY_REQUIRED
    assert supervisor.transaction.status == TransactionStatus.RECOVERY_REQUIRED
    assert supervisor.transaction.last_confirmed_step == "failed_source_isolated"


def test_readiness_loss_during_transfer_immediately_targets_safe_power_path():
    supervisor = EnergySupervisor()
    supervisor.step(0.0, observation())
    supervisor.request_manual_start()
    supervisor.step(1.0, observation())
    supervisor.step(2.0, observation(a=ready_a()))
    assert supervisor.phase == SupervisorPhase.TRANSFERRING_TO_GENERATOR

    not_ready = generator_status(
        GeneratorSlot.A,
        phase=GeneratorPhase.READY_FOR_LOAD,
        running=True,
        remote_on=False,
        ready=False,
    )
    decision = supervisor.step(3.0, observation(a=not_ready))

    assert supervisor.phase == SupervisorPhase.ISOLATING_FAILED_SOURCE
    assert decision.desired_source == PowerSource.GRID
    assert decision.desired_generators[GeneratorSlot.A] is False


def test_connection_loss_during_fault_isolation_cannot_be_mistaken_for_stable():
    supervisor = EnergySupervisor()
    enter_manual_session(
        supervisor,
        grid_ready=False,
        initial_source=PowerSource.BATTERY,
    )
    failed = generator_status(
        GeneratorSlot.A,
        phase=GeneratorPhase.FAULT,
        running=False,
        remote_on=True,
        fault="нет RUNNING",
    )
    supervisor.step(
        4.0,
        observation(
            grid_ready=False,
            source=PowerSource.GENERATOR_A,
            a=failed,
        ),
    )

    supervisor.mark_connection_lost(4.5)

    assert supervisor.phase == SupervisorPhase.RECOVERY_REQUIRED
    assert supervisor.transaction is not None
    assert supervisor.transaction.status == TransactionStatus.RECOVERY_REQUIRED


def test_second_generator_during_managed_session_is_isolated_not_adopted():
    supervisor = EnergySupervisor()
    enter_manual_session(
        supervisor,
        grid_ready=False,
        initial_source=PowerSource.BATTERY,
    )
    external_b = generator_status(
        GeneratorSlot.B,
        phase=GeneratorPhase.EXTERNAL_RUNNING,
        running=True,
        remote_on=False,
        external=True,
    )

    decision = supervisor.step(
        4.0,
        observation(
            grid_ready=False,
            source=PowerSource.GENERATOR_A,
            a=ready_a(),
            b=external_b,
        ),
    )

    assert supervisor.phase == SupervisorPhase.ISOLATING_FAILED_SOURCE
    assert decision.desired_source == PowerSource.BATTERY
    assert decision.desired_generators == {
        GeneratorSlot.A: False,
        GeneratorSlot.B: False,
    }
    assert any("взаимная блокировка" in event.message for event in decision.events)


def test_connection_loss_blocks_only_an_in_progress_transaction():
    starting = EnergySupervisor()
    starting.step(0.0, observation())
    starting.request_manual_start()
    starting.step(1.0, observation())
    assert starting.transaction.status == TransactionStatus.IN_PROGRESS
    starting.mark_connection_lost(2.0)
    assert starting.phase == SupervisorPhase.RECOVERY_REQUIRED

    stable = EnergySupervisor()
    enter_manual_session(
        stable,
        grid_ready=True,
        initial_source=PowerSource.GRID,
    )
    assert stable.transaction.status == TransactionStatus.COMPLETED
    stable.mark_connection_lost(4.0)
    assert stable.phase == SupervisorPhase.ON_GENERATOR


def test_idle_manual_generator_creates_warning_but_is_not_stopped():
    supervisor = EnergySupervisor(
        SupervisorConfig(
            grid_restore_stable_time=1.0,
            manual_idle_warning_seconds=10.0,
        )
    )
    enter_manual_session(
        supervisor,
        grid_ready=False,
        initial_source=PowerSource.BATTERY,
    )
    supervisor.step(
        10.0,
        observation(
            grid_ready=True,
            source=PowerSource.GENERATOR_A,
            a=ready_a(),
        ),
    )
    supervisor.step(
        11.0,
        observation(
            grid_ready=True,
            source=PowerSource.GENERATOR_A,
            a=ready_a(),
        ),
    )
    supervisor.step(
        12.0,
        observation(grid_ready=True, source=PowerSource.GRID, a=ready_a()),
    )
    decision = supervisor.step(
        22.0,
        observation(grid_ready=True, source=PowerSource.GRID, a=ready_a()),
    )

    assert supervisor.phase == SupervisorPhase.MANUAL_GENERATOR_IDLE
    assert decision.desired_generators[GeneratorSlot.A] is True
    assert any("без нагрузки" in event.message for event in decision.events)


def test_local_stop_of_idle_manual_generator_finishes_session_without_recovery():
    supervisor = EnergySupervisor(
        SupervisorConfig(grid_restore_stable_time=1.0)
    )
    enter_manual_session(
        supervisor,
        grid_ready=False,
        initial_source=PowerSource.BATTERY,
    )
    supervisor.step(
        10.0,
        observation(
            grid_ready=True,
            source=PowerSource.GENERATOR_A,
            a=ready_a(),
        ),
    )
    supervisor.step(
        11.0,
        observation(
            grid_ready=True,
            source=PowerSource.GENERATOR_A,
            a=ready_a(),
        ),
    )
    supervisor.step(
        12.0,
        observation(grid_ready=True, source=PowerSource.GRID, a=ready_a()),
    )
    assert supervisor.phase == SupervisorPhase.MANUAL_GENERATOR_IDLE

    decision = supervisor.step(
        13.0,
        observation(grid_ready=True, source=PowerSource.GRID),
    )

    assert supervisor.phase == SupervisorPhase.NORMAL
    assert supervisor.session is None
    assert decision.actions_allowed is True
    assert any("остановлен локально" in event.message for event in decision.events)


def test_grid_failure_during_automatic_cooldown_reuses_running_generator():
    supervisor = EnergySupervisor(
        SupervisorConfig(grid_failure_delay=0.0, grid_restore_stable_time=1.0)
    )
    # Автоматическая outage-сессия.
    supervisor.step(
        0.0,
        observation(
            grid_ready=False,
            source=PowerSource.BATTERY,
            automatic=True,
        ),
    )
    supervisor.step(
        0.1,
        observation(
            grid_ready=False,
            source=PowerSource.BATTERY,
            automatic=True,
        ),
    )
    supervisor.step(
        1.0,
        observation(
            grid_ready=False,
            source=PowerSource.BATTERY,
            automatic=True,
            a=ready_a(),
        ),
    )
    supervisor.step(
        2.0,
        observation(
            grid_ready=False,
            source=PowerSource.GENERATOR_A,
            automatic=True,
            a=ready_a(),
        ),
    )
    supervisor.step(
        3.0,
        observation(
            grid_ready=True,
            source=PowerSource.GENERATOR_A,
            automatic=True,
            a=ready_a(),
        ),
    )
    supervisor.step(
        4.0,
        observation(
            grid_ready=True,
            source=PowerSource.GENERATOR_A,
            automatic=True,
            a=ready_a(),
        ),
    )
    supervisor.step(
        5.0,
        observation(
            grid_ready=True,
            source=PowerSource.GRID,
            automatic=True,
            a=ready_a(),
        ),
    )
    assert supervisor.phase == SupervisorPhase.STOPPING_GENERATOR

    decision = supervisor.step(
        6.0,
        observation(
            grid_ready=False,
            source=PowerSource.BATTERY,
            automatic=True,
            a=ready_a(),
        ),
    )
    assert supervisor.phase == SupervisorPhase.TRANSFERRING_TO_GENERATOR
    assert decision.desired_generators[GeneratorSlot.A] is True
    assert decision.desired_source == PowerSource.GENERATOR_A


def test_stable_grid_return_during_start_avoids_unnecessary_transfer():
    supervisor = EnergySupervisor(
        SupervisorConfig(
            grid_failure_delay=0.0,
            grid_restore_stable_time=2.0,
        )
    )
    supervisor.step(
        0.0,
        observation(
            grid_ready=False,
            source=PowerSource.BATTERY,
            automatic=True,
        ),
    )
    supervisor.step(
        0.1,
        observation(
            grid_ready=False,
            source=PowerSource.BATTERY,
            automatic=True,
        ),
    )
    warming = generator_status(
        GeneratorSlot.A,
        phase=GeneratorPhase.WARMING_UP,
        running=True,
        remote_on=True,
        ready=False,
    )
    supervisor.step(
        1.0,
        observation(
            grid_ready=True,
            source=PowerSource.GRID,
            automatic=True,
            a=warming,
        ),
    )
    supervisor.step(
        3.0,
        observation(
            grid_ready=True,
            source=PowerSource.GRID,
            automatic=True,
            a=warming,
        ),
    )

    decision = supervisor.step(
        4.0,
        observation(
            grid_ready=True,
            source=PowerSource.GRID,
            automatic=True,
            a=ready_a(),
        ),
    )

    assert supervisor.phase == SupervisorPhase.RETURNING_TO_NORMAL
    assert decision.desired_source == PowerSource.GRID


def test_external_session_never_moves_power_path_after_engine_stops():
    supervisor = EnergySupervisor()
    external = generator_status(
        GeneratorSlot.B,
        phase=GeneratorPhase.EXTERNAL_RUNNING,
        running=True,
        remote_on=False,
        external=True,
    )
    supervisor.step(
        0.0,
        observation(source=PowerSource.GENERATOR_B, b=external),
    )

    decision = supervisor.step(
        1.0,
        observation(source=PowerSource.GENERATOR_B),
    )
    assert supervisor.phase == SupervisorPhase.EXTERNAL_RUNNING
    assert decision.actions_allowed is False
    assert decision.desired_source == PowerSource.GENERATOR_B

    supervisor.request_manual_start()
    decision = supervisor.step(
        2.0,
        observation(source=PowerSource.GENERATOR_B),
    )
    assert supervisor.session is None
    assert any("внешний сеанс" in event.message.lower() for event in decision.events)

    supervisor.step(3.0, observation(source=PowerSource.GRID))
    assert supervisor.phase == SupervisorPhase.NORMAL


def test_missing_physical_data_rejects_manual_command_without_starting_later():
    supervisor = EnergySupervisor()
    supervisor.step(0.0, observation())
    supervisor.request_manual_start()
    missing = generator_status(
        GeneratorSlot.A,
        running=None,
        remote_on=None,
    )

    decision = supervisor.step(1.0, observation(a=missing))

    assert supervisor.session is None
    assert decision.actions_allowed is False
    assert any("отсутствуют" in event.message for event in decision.events)

    # Кнопка не должна неожиданно сработать после восстановления датчиков.
    supervisor.step(2.0, observation())
    assert supervisor.session is None


def test_disabling_automatic_transfer_restarts_grid_failure_delay():
    supervisor = EnergySupervisor(SupervisorConfig(grid_failure_delay=5.0))
    supervisor.step(
        0.0,
        observation(
            grid_ready=False,
            source=PowerSource.BATTERY,
            automatic=True,
        ),
    )
    supervisor.step(
        4.0,
        observation(
            grid_ready=False,
            source=PowerSource.BATTERY,
            automatic=False,
        ),
    )
    supervisor.step(
        10.0,
        observation(
            grid_ready=False,
            source=PowerSource.BATTERY,
            automatic=True,
        ),
    )
    assert supervisor.session is None

    supervisor.step(
        15.0,
        observation(
            grid_ready=False,
            source=PowerSource.BATTERY,
            automatic=True,
        ),
    )
    assert supervisor.session is not None


def test_restore_rejects_failed_or_unknown_journal_schema():
    transaction = Transaction.begin("test", "A", 1.0, "physical_step")
    transaction.fail(2.0, "failed")
    supervisor = EnergySupervisor()
    data = supervisor.to_dict()
    data["session"] = {
        "session_id": "session",
        "reason": "manual_backup",
        "generator": "A",
        "started_at": 0.0,
        "grid_was_unavailable": False,
    }
    data["transaction"] = transaction.to_dict()

    restored = EnergySupervisor.from_dict(data)
    restored.step(3.0, observation())
    assert restored.phase == SupervisorPhase.RECOVERY_REQUIRED

    data["schema_version"] = 999
    with pytest.raises(ValueError, match="версия журнала"):
        EnergySupervisor.from_dict(data)


def test_restart_never_reasserts_saved_source_over_changed_hardware():
    supervisor = EnergySupervisor()
    enter_manual_session(
        supervisor,
        grid_ready=True,
        initial_source=PowerSource.GRID,
    )
    restored = EnergySupervisor.from_dict(supervisor.to_dict())

    decision = restored.step(
        10.0,
        observation(
            grid_ready=True,
            source=PowerSource.GRID,
            a=ready_a(),
        ),
    )

    assert restored.phase == SupervisorPhase.RECOVERY_REQUIRED
    assert decision.actions_allowed is False
    assert decision.desired_source == PowerSource.GENERATOR_A

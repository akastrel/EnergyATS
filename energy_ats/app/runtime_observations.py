"""Чистые преобразования runtime facts в observation DTO отдельных подсистем.

Модуль не хранит state и не принимает решений. Он является application-layer
boundary между общим physical/runtime snapshot и узкими входными структурами
Supervisor, Exercise, UPS Run и Load Manager.
"""

from __future__ import annotations

from datetime import datetime
from typing import Mapping

from domain import GeneratorSlot, PowerPath, PowerSource
from energy_supervisor import (
    EnergySupervisor,
    SupervisorDecision,
    SupervisorObservation,
    SupervisorPhase,
)
from exercise_scheduler import (
    ExerciseGeneratorObservation,
    ExerciseObservation,
)
from generator_bus import GeneratorBusStatus
from generator_controller import GeneratorStatus
from ha_adapter import HardwareSnapshot
from load_manager import LoadManagerObservation
from power_transfer import PowerTransferStatus
from ups_run import UPSRunObservation


def build_supervisor_observation(
    *,
    hardware: HardwareSnapshot,
    armed: bool,
    power_status: PowerTransferStatus,
    generator_statuses: Mapping[GeneratorSlot, GeneratorStatus],
    bus_status: GeneratorBusStatus,
) -> SupervisorObservation:
    return SupervisorObservation(
        grid_ready=hardware.grid_ready,
        automatic_transfer_enabled=(
            hardware.automatic_transfer_enabled and armed
        ),
        emergency_stop=hardware.emergency_stop,
        power=power_status,
        generators=dict(generator_statuses),
        power_inputs_known=hardware.power_transfer.required_states_known,
        bus=bus_status,
        grid_input_state=hardware.grid_input_state,
    )


def build_ups_run_observation(
    *,
    now: float,
    hardware: HardwareSnapshot,
    supervisor_observation: SupervisorObservation,
    supervisor: EnergySupervisor,
    bus_status: GeneratorBusStatus,
) -> UPSRunObservation:
    session = supervisor.session
    core_delay_elapsed = bool(
        supervisor.grid_failed_since is not None
        and now - supervisor.grid_failed_since
        >= supervisor.config.grid_failure_delay
    )
    session_on_generator = bool(
        session is not None
        and supervisor.phase == SupervisorPhase.ON_GENERATOR
        and supervisor_observation.power.actual_path == PowerPath.GENERATOR
        and bus_status.owner_slot == session.generator
    )
    return UPSRunObservation(
        now=now,
        grid_ready=hardware.grid_ready,
        automatic_transfer_enabled=(
            supervisor_observation.automatic_transfer_enabled
        ),
        core_delay_elapsed=core_delay_elapsed,
        battery=hardware.battery,
        session_reason=session.reason if session is not None else None,
        session_active=session is not None,
        session_on_generator=session_on_generator,
        session_cycle_owned=bool(session is not None and session.cycle_owned),
        session_manual_override=bool(
            session is not None and session.manual_override
        ),
        manual_start_pending=supervisor.manual_start_pending,
        grid_stable=bool(
            hardware.grid_ready is True
            and (
                supervisor.config.grid_restore_stable_time == 0
                or (
                    supervisor.grid_ready_since is not None
                    and now - supervisor.grid_ready_since
                    >= supervisor.config.grid_restore_stable_time
                )
            )
        ),
        grid_supply_restored=(
            supervisor_observation.power.actual_source == PowerSource.GRID
            and not supervisor_observation.power.transition_in_progress
        ),
    )


def build_exercise_observation(
    *,
    now: float,
    local_now: datetime,
    hardware: HardwareSnapshot,
    supervisor_observation: SupervisorObservation,
    supervisor: EnergySupervisor,
    armed: bool,
    generator_names: Mapping[GeneratorSlot, str],
) -> ExerciseObservation:
    power = supervisor_observation.power
    policy_busy = (
        supervisor.session is not None
        or supervisor.phase
        not in {SupervisorPhase.NORMAL, SupervisorPhase.WAITING_FOR_DATA}
        or supervisor.has_pending_session_request
        or supervisor.recovery_reset_in_progress
    )
    return ExerciseObservation(
        now=now,
        local_now=local_now,
        grid_ready=hardware.grid_ready,
        grid_path_stable=(
            power.actual_path == PowerPath.GRID
            and not power.transition_in_progress
            and hardware.power_transfer.generator_selected is False
            and hardware.power_transfer.house_on_generator is False
        ),
        family_present=hardware.family_present,
        emergency_stop=hardware.emergency_stop,
        required_states_known=supervisor_observation.required_states_known,
        power_transition_in_progress=power.transition_in_progress,
        policy_busy=policy_busy,
        actions_enabled=armed,
        generators={
            slot: ExerciseGeneratorObservation(
                running=status.running,
                remote_on=status.remote_on,
                fault=status.fault,
            )
            for slot, status in supervisor_observation.generators.items()
        },
        generator_names=dict(generator_names),
    )


def build_load_manager_observation(
    *,
    now: float,
    hardware: HardwareSnapshot,
    decision: SupervisorDecision,
    supervisor: EnergySupervisor,
    power_status: PowerTransferStatus,
    bus_status: GeneratorBusStatus,
    generator_statuses: Mapping[GeneratorSlot, GeneratorStatus],
    generator_names: Mapping[GeneratorSlot, str],
    actions_enabled: bool,
) -> LoadManagerObservation:
    session_slot = (
        supervisor.session.generator if supervisor.session is not None else None
    )
    limit_slot = (
        bus_status.owner_slot
        if hardware.power_transfer.house_on_generator is True
        else session_slot
    )
    metadata = (
        hardware.generator_metadata.get(limit_slot)
        if limit_slot is not None
        else None
    )
    managed_ready = bool(
        session_slot is not None
        and generator_statuses[session_slot].ready_for_load
    )
    load = hardware.load_management
    return LoadManagerObservation(
        now=now,
        house_on_generator=hardware.power_transfer.house_on_generator,
        house_on_grid=hardware.power_transfer.house_on_grid,
        desired_generator_supply=(
            decision.desired_source == PowerSource.GENERATOR
            and supervisor.session is not None
        ),
        managed_generator_ready=managed_ready,
        power_transition_in_progress=power_status.transition_in_progress,
        bus_owner=bus_status.owner_slot,
        nominal_power=metadata.nominal_power if metadata is not None else None,
        maximum_power=metadata.maximum_power if metadata is not None else None,
        meter_ready=load.meter_ready,
        generator_power=load.generator_power,
        power_sample_id=load.power_sample_id,
        groups=load.groups,
        generator_name=(
            generator_names[limit_slot] if limit_slot is not None else None
        ),
        actions_enabled=actions_enabled,
    )

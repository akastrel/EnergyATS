from __future__ import annotations

from domain import GeneratorSlot, PowerPath, PowerSource, SessionReason
from energy_supervisor import (
    EnergySupervisor,
    GeneratorSession,
    SupervisorConfig,
    SupervisorObservation,
    SupervisorPhase,
)
from generator_bus import GeneratorBusOwner, GeneratorBusStatus, GeneratorRunContext
from generator_controller import GeneratorPhase, GeneratorStatus
from power_transfer import PowerTransferStatus, TransferPhase


def _stopped(slot: GeneratorSlot) -> GeneratorStatus:
    return GeneratorStatus(
        slot=slot,
        display_name="Elemax" if slot == GeneratorSlot.A else "Вепрь",
        phase=GeneratorPhase.IDLE,
        running=False,
        remote_on=False,
        ready_for_load=False,
        fault=None,
    )


def test_manual_stop_during_finishing_cycle_does_not_start_new_cycle_wait() -> None:
    """Проверяет REQ-BEH-14/REQ-CYCLE-07: manual stop во время уже завершающегося charge cycle снимает cycle ownership, поэтому после остановки не создаётся новый automatic post-cycle wait."""
    supervisor = EnergySupervisor(SupervisorConfig(grid_restore_stable_time=60.0))
    supervisor.initialized = True
    supervisor.phase = SupervisorPhase.RETURNING_TO_UPS
    supervisor.session = GeneratorSession.begin(
        SessionReason.GRID_OUTAGE,
        GeneratorSlot.A,
        grid_was_unavailable=True,
    )
    supervisor.session.cycle_owned = True
    supervisor.session.stop_requested = True
    supervisor.desired_source = PowerSource.UPS_ONLY
    supervisor.desired_generators[GeneratorSlot.A] = True
    supervisor.request_manual_stop()

    decision = supervisor.step(
        20.0,
        SupervisorObservation(
            grid_ready=False,
            automatic_transfer_enabled=True,
            emergency_stop=False,
            power=PowerTransferStatus(
                phase=TransferPhase.STABLE_ISOLATED,
                actual_source=PowerSource.UPS_ONLY,
                actual_path=PowerPath.ISOLATED,
                target_source=None,
                transition_in_progress=False,
                recovery_required=False,
                fault=None,
            ),
            generators={
                GeneratorSlot.A: _stopped(GeneratorSlot.A),
                GeneratorSlot.B: _stopped(GeneratorSlot.B),
            },
            power_inputs_known=True,
            bus=GeneratorBusStatus(
                owner=GeneratorBusOwner.NONE,
                run_contexts={
                    GeneratorSlot.A: GeneratorRunContext.NONE,
                    GeneratorSlot.B: GeneratorRunContext.NONE,
                },
            ),
        ),
    )

    assert supervisor.session is None
    assert supervisor.phase == SupervisorPhase.NORMAL
    assert decision.begin_post_cycle_wait is False

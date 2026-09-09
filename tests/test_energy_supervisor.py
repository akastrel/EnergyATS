import pytest

from domain import (
    GeneratorSlot,
    PowerPath,
    PowerSource,
    SessionReason,
    Transaction,
)
from energy_supervisor import (
    EnergySupervisor,
    GeneratorSession,
    SupervisorConfig,
    SupervisorObservation,
    SupervisorPhase,
)
from generator_bus import (
    GeneratorBusOwner,
    GeneratorBusStatus,
    GeneratorRunContext,
)
from generator_controller import GeneratorPhase, GeneratorStatus
from power_transfer import PowerTransferStatus, TransferPhase


def generator_status(
    slot: GeneratorSlot,
    *,
    phase=GeneratorPhase.IDLE,
    running=False,
    remote_on=False,
    ready=False,
    external=False,
    fault=None,
):
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


def ready(slot: GeneratorSlot):
    return generator_status(
        slot,
        phase=GeneratorPhase.READY_FOR_LOAD,
        running=True,
        remote_on=True,
        ready=True,
    )


def power_status(
    source=PowerSource.GRID,
    *,
    path=None,
    transition=False,
    recovery=False,
    fault=None,
):
    actual_path = path or PowerPath.for_source(source)
    if recovery:
        phase = TransferPhase.RECOVERY_REQUIRED
    elif actual_path == PowerPath.GRID:
        phase = TransferPhase.STABLE_GRID_PATH
    elif actual_path == PowerPath.ISOLATED:
        phase = TransferPhase.STABLE_ISOLATED
    elif actual_path == PowerPath.GENERATOR:
        phase = TransferPhase.STABLE_GENERATOR
    else:
        phase = TransferPhase.WAITING_FOR_DATA
    return PowerTransferStatus(
        phase=phase,
        actual_source=source,
        actual_path=actual_path,
        target_source=None,
        transition_in_progress=transition,
        recovery_required=recovery,
        fault=fault,
    )


def bus(
    owner=GeneratorBusOwner.NONE,
    *,
    a_context=GeneratorRunContext.NONE,
    b_context=GeneratorRunContext.NONE,
):
    return GeneratorBusStatus(
        owner=owner,
        run_contexts={
            GeneratorSlot.A: a_context,
            GeneratorSlot.B: b_context,
        },
    )


def observation(
    *,
    grid_ready=True,
    automatic=False,
    source=PowerSource.GRID,
    path=None,
    a=None,
    b=None,
    bus_status=None,
    transition=False,
    recovery=False,
    emergency=False,
    power_inputs_known=True,
):
    return SupervisorObservation(
        grid_ready=grid_ready,
        automatic_transfer_enabled=automatic,
        emergency_stop=emergency,
        power=power_status(
            source,
            path=path,
            transition=transition,
            recovery=recovery,
            fault="transfer fault" if recovery else None,
        ),
        generators={
            GeneratorSlot.A: a or generator_status(GeneratorSlot.A),
            GeneratorSlot.B: b or generator_status(GeneratorSlot.B),
        },
        power_inputs_known=power_inputs_known,
        bus=bus_status or bus(),
    )


def start_manual(supervisor: EnergySupervisor, *, grid_ready=True):
    supervisor.step(
        0.0,
        observation(
            grid_ready=grid_ready,
            source=PowerSource.GRID if grid_ready else PowerSource.UPS_ONLY,
            path=PowerPath.GRID,
        ),
    )
    supervisor.request_manual_start()
    return supervisor.step(
        1.0,
        observation(
            grid_ready=grid_ready,
            source=PowerSource.GRID if grid_ready else PowerSource.UPS_ONLY,
            path=PowerPath.GRID,
        ),
    )


def enter_on_generator(
    supervisor: EnergySupervisor,
    *,
    grid_ready=False,
    session_reason=SessionReason.MANUAL_GENERATOR_START,
):
    start_manual(supervisor, grid_ready=grid_ready)
    assert supervisor.session is not None
    supervisor.session.reason = session_reason
    supervisor.session.grid_was_unavailable = not grid_ready

    supervisor.step(
        2.0,
        observation(
            grid_ready=grid_ready,
            source=PowerSource.UPS_ONLY if not grid_ready else PowerSource.GRID,
            path=PowerPath.GRID,
            a=ready(GeneratorSlot.A),
            bus_status=bus(GeneratorBusOwner.A),
        ),
    )
    assert supervisor.phase == SupervisorPhase.TRANSFERRING_TO_GENERATOR

    supervisor.step(
        3.0,
        observation(
            grid_ready=grid_ready,
            source=PowerSource.GENERATOR_A,
            path=PowerPath.GENERATOR,
            a=ready(GeneratorSlot.A),
            bus_status=bus(GeneratorBusOwner.A),
        ),
    )
    assert supervisor.phase == SupervisorPhase.ON_GENERATOR


def test_manual_start_uses_primary_generator():
    supervisor = EnergySupervisor(
        SupervisorConfig(primary_generator=GeneratorSlot.B)
    )

    supervisor.step(0.0, observation())
    supervisor.request_manual_start()
    decision = supervisor.step(1.0, observation())

    assert supervisor.session is not None
    assert supervisor.session.generator == GeneratorSlot.B
    assert decision.desired_generators[GeneratorSlot.B] is True
    assert decision.desired_generators[GeneratorSlot.A] is False


def test_disabled_primary_blocks_new_session():
    supervisor = EnergySupervisor(
        SupervisorConfig(
            primary_generator=GeneratorSlot.B,
            generator_b_enabled=False,
        )
    )
    supervisor.step(0.0, observation())
    supervisor.request_manual_start()

    supervisor.step(1.0, observation())

    assert supervisor.session is None


def test_grid_outage_waits_delay_then_starts_primary():
    supervisor = EnergySupervisor(
        SupervisorConfig(grid_failure_delay=5.0)
    )
    outage = observation(
        grid_ready=False,
        automatic=True,
        source=PowerSource.UPS_ONLY,
        path=PowerPath.GRID,
    )

    supervisor.step(0.0, outage)
    assert supervisor.phase == SupervisorPhase.GRID_FAILURE_DELAY
    assert supervisor.session is None

    supervisor.step(4.9, outage)
    assert supervisor.session is None

    decision = supervisor.step(5.0, outage)
    assert supervisor.session is not None
    assert supervisor.session.reason == SessionReason.GRID_OUTAGE
    assert supervisor.session.generator == GeneratorSlot.A
    assert decision.desired_generators[GeneratorSlot.A] is True


def test_deliberate_grid_disconnect_does_not_start_outage_session():
    supervisor = EnergySupervisor(
        SupervisorConfig(grid_failure_delay=0.0)
    )
    # Grid physically ready; isolated main source is an external/manual fact.
    isolated = observation(
        grid_ready=True,
        automatic=True,
        source=PowerSource.UPS_ONLY,
        path=PowerPath.ISOLATED,
    )

    supervisor.step(0.0, isolated)
    supervisor.step(100.0, isolated)

    assert supervisor.session is None
    assert supervisor.desired_source is None


def test_second_running_generator_is_not_interlock_fault():
    supervisor = EnergySupervisor()
    enter_on_generator(supervisor, grid_ready=False)

    second_external = generator_status(
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
            path=PowerPath.GENERATOR,
            a=ready(GeneratorSlot.A),
            b=second_external,
            bus_status=bus(
                GeneratorBusOwner.A,
                a_context=GeneratorRunContext.MANAGED_OUTAGE,
                b_context=GeneratorRunContext.EXTERNAL_OUTAGE,
            ),
        ),
    )

    assert supervisor.phase == SupervisorPhase.ON_GENERATOR
    assert decision.actions_allowed is True
    assert not any("взаимн" in event.message.lower() for event in decision.events)


def test_primary_failure_falls_back_once_to_stopped_secondary():
    supervisor = EnergySupervisor()
    start_manual(supervisor, grid_ready=False)

    failed_a = generator_status(
        GeneratorSlot.A,
        phase=GeneratorPhase.FAULT,
        running=False,
        remote_on=True,
        fault="не запустился",
    )
    decision = supervisor.step(
        2.0,
        observation(
            grid_ready=False,
            source=PowerSource.UPS_ONLY,
            path=PowerPath.GRID,
            a=failed_a,
        ),
    )

    assert supervisor.session is not None
    assert supervisor.session.generator == GeneratorSlot.B
    assert supervisor.session.fallback_used is True
    assert supervisor.phase == SupervisorPhase.STARTING_GENERATOR
    assert decision.desired_generators[GeneratorSlot.B] is True
    assert decision.desired_generators[GeneratorSlot.A] is False


def test_secondary_failure_after_fallback_requires_recovery_without_ping_pong():
    supervisor = EnergySupervisor()
    start_manual(supervisor, grid_ready=False)

    failed_a = generator_status(
        GeneratorSlot.A,
        phase=GeneratorPhase.FAULT,
        remote_on=True,
        fault="primary failed",
    )
    supervisor.step(
        2.0,
        observation(
            grid_ready=False,
            source=PowerSource.UPS_ONLY,
            path=PowerPath.GRID,
            a=failed_a,
        ),
    )
    assert supervisor.session is not None
    assert supervisor.session.generator == GeneratorSlot.B

    failed_b = generator_status(
        GeneratorSlot.B,
        phase=GeneratorPhase.FAULT,
        remote_on=True,
        fault="secondary failed",
    )
    decision = supervisor.step(
        3.0,
        observation(
            grid_ready=False,
            source=PowerSource.UPS_ONLY,
            path=PowerPath.GRID,
            a=failed_a,
            b=failed_b,
        ),
    )

    assert supervisor.phase == SupervisorPhase.RECOVERY_REQUIRED
    assert decision.desired_generators[GeneratorSlot.A] is False
    assert decision.desired_generators[GeneratorSlot.B] is False


def test_running_external_secondary_is_not_captured_on_primary_failure():
    supervisor = EnergySupervisor()
    enter_on_generator(supervisor, grid_ready=False)

    failed_a = generator_status(
        GeneratorSlot.A,
        phase=GeneratorPhase.FAULT,
        running=False,
        remote_on=True,
        fault="primary stopped",
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
            source=PowerSource.GENERATOR_B,
            path=PowerPath.GENERATOR,
            a=failed_a,
            b=external_b,
            bus_status=bus(
                GeneratorBusOwner.B,
                a_context=GeneratorRunContext.NONE,
                b_context=GeneratorRunContext.EXTERNAL_OUTAGE,
            ),
        ),
    )

    assert supervisor.phase == SupervisorPhase.ON_EXTERNAL_GENERATOR
    assert supervisor.session is not None
    assert supervisor.session.generator == GeneratorSlot.A
    assert supervisor.session.external_takeover == GeneratorSlot.B
    assert decision.desired_generators[GeneratorSlot.B] is False


def test_stable_grid_starts_return_from_outage_session():
    supervisor = EnergySupervisor(
        SupervisorConfig(grid_restore_stable_time=60.0)
    )
    enter_on_generator(supervisor, grid_ready=False)

    running_on_grid_return = observation(
        grid_ready=True,
        source=PowerSource.GENERATOR_A,
        path=PowerPath.GENERATOR,
        a=ready(GeneratorSlot.A),
        bus_status=bus(
            GeneratorBusOwner.A,
            a_context=GeneratorRunContext.MANAGED_OUTAGE,
        ),
    )
    supervisor.step(10.0, running_on_grid_return)
    assert supervisor.phase == SupervisorPhase.ON_GENERATOR

    decision = supervisor.step(70.0, running_on_grid_return)
    assert supervisor.phase == SupervisorPhase.RETURNING_TO_GRID_OR_UPS
    assert decision.desired_source == PowerSource.GRID


def test_stable_grid_stops_all_outage_related_runs_after_grid_path_confirmed():
    supervisor = EnergySupervisor(
        SupervisorConfig(grid_restore_stable_time=10.0)
    )
    a = generator_status(
        GeneratorSlot.A,
        phase=GeneratorPhase.EXTERNAL_RUNNING,
        running=True,
        remote_on=False,
        external=True,
    )
    b = generator_status(
        GeneratorSlot.B,
        phase=GeneratorPhase.EXTERNAL_RUNNING,
        running=True,
        remote_on=False,
        external=True,
    )
    status = bus(
        GeneratorBusOwner.A,
        a_context=GeneratorRunContext.EXTERNAL_OUTAGE,
        b_context=GeneratorRunContext.EXTERNAL_OUTAGE,
    )

    # Grid appears; start the stability timer.
    supervisor.step(
        0.0,
        observation(
            grid_ready=True,
            source=PowerSource.GRID,
            path=PowerPath.GRID,
            a=a,
            b=b,
            bus_status=status,
        ),
    )
    decision = supervisor.step(
        10.0,
        observation(
            grid_ready=True,
            source=PowerSource.GRID,
            path=PowerPath.GRID,
            a=a,
            b=b,
            bus_status=status,
        ),
    )

    assert decision.stop_outage_generators == frozenset(
        {GeneratorSlot.A, GeneratorSlot.B}
    )


def test_test_run_is_not_stopped_by_stable_grid():
    supervisor = EnergySupervisor(
        SupervisorConfig(grid_restore_stable_time=0.0)
    )
    test_a = generator_status(
        GeneratorSlot.A,
        phase=GeneratorPhase.EXTERNAL_RUNNING,
        running=True,
        remote_on=False,
        external=True,
    )
    decision = supervisor.step(
        0.0,
        observation(
            grid_ready=True,
            source=PowerSource.GRID,
            path=PowerPath.GRID,
            a=test_a,
            bus_status=bus(
                GeneratorBusOwner.A,
                a_context=GeneratorRunContext.TEST_RUN,
            ),
        ),
    )

    assert decision.stop_outage_generators == frozenset()


def test_missing_required_physical_data_disallows_actions():
    supervisor = EnergySupervisor()
    supervisor.step(0.0, observation())
    supervisor.request_manual_start()
    missing_a = generator_status(
        GeneratorSlot.A,
        running=None,
        remote_on=None,
    )

    decision = supervisor.step(1.0, observation(a=missing_a))

    assert supervisor.session is None
    assert decision.actions_allowed is False


def test_transfer_controller_recovery_propagates_to_supervisor():
    supervisor = EnergySupervisor()
    supervisor.step(0.0, observation())

    decision = supervisor.step(
        1.0,
        observation(
            recovery=True,
            source=PowerSource.UNKNOWN,
            path=PowerPath.UNKNOWN,
        ),
    )

    assert supervisor.phase == SupervisorPhase.RECOVERY_REQUIRED
    assert decision.actions_allowed is False


def test_persistence_schema_round_trip_v4():
    supervisor = EnergySupervisor()
    start_manual(supervisor, grid_ready=False)
    assert supervisor.session is not None
    supervisor.session.fallback_used = True

    data = supervisor.to_dict()
    restored = EnergySupervisor.from_dict(data, SupervisorConfig())

    assert data["schema_version"] == 4
    assert restored.session is not None
    assert restored.session.generator == GeneratorSlot.A
    assert restored.session.fallback_used is True


def test_old_supervisor_schema_is_rejected():
    supervisor = EnergySupervisor()
    data = supervisor.to_dict()
    data["schema_version"] = 3

    with pytest.raises(ValueError, match="Неподдерживаемая"):
        EnergySupervisor.from_dict(data, SupervisorConfig())


def test_restart_restores_stable_managed_owner_when_physics_matches():
    supervisor = EnergySupervisor()
    supervisor.session = GeneratorSession.begin(
        reason=SessionReason.GRID_OUTAGE,
        generator=GeneratorSlot.A,
        now=0.0,
        grid_was_unavailable=True,
    )
    supervisor.phase = SupervisorPhase.ON_GENERATOR
    supervisor.desired_source = PowerSource.GENERATOR_A
    supervisor.desired_generators[GeneratorSlot.A] = True
    supervisor.transaction = Transaction.begin(
        "enter_generator",
        GeneratorSlot.A.value,
        0.0,
        "transfer_to_generator",
    )
    supervisor.transaction.complete(1.0, "stable")

    restored = EnergySupervisor.from_dict(
        supervisor.to_dict(),
        SupervisorConfig(),
    )
    decision = restored.step(
        2.0,
        observation(
            grid_ready=False,
            source=PowerSource.GENERATOR_A,
            path=PowerPath.GENERATOR,
            a=ready(GeneratorSlot.A),
            bus_status=bus(
                GeneratorBusOwner.A,
                a_context=GeneratorRunContext.MANAGED_OUTAGE,
            ),
        ),
    )

    assert restored.phase == SupervisorPhase.ON_GENERATOR
    assert decision.actions_allowed is True


def test_connection_loss_during_in_progress_transaction_requires_recovery():
    supervisor = EnergySupervisor()
    start_manual(supervisor, grid_ready=True)
    assert supervisor.transaction is not None

    supervisor.mark_connection_lost(2.0)

    assert supervisor.phase == SupervisorPhase.RECOVERY_REQUIRED

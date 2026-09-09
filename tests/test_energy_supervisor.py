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
    fault=None,
):
    return GeneratorStatus(
        slot=slot,
        display_name="Elemax" if slot == GeneratorSlot.A else "Вепрь",
        phase=phase,
        running=running,
        remote_on=remote_on,
        ready_for_load=ready,
        fault=fault,
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
):
    actual_path = path or PowerPath.for_source(source)
    phase = (
        TransferPhase.RECOVERY_REQUIRED
        if recovery
        else TransferPhase.STABLE_GRID
        if actual_path == PowerPath.GRID
        else TransferPhase.STABLE_ISOLATED
        if actual_path == PowerPath.ISOLATED
        else TransferPhase.STABLE_GENERATOR
        if actual_path == PowerPath.GENERATOR
        else TransferPhase.WAITING_FOR_DATA
    )
    return PowerTransferStatus(
        phase=phase,
        actual_source=source,
        actual_path=actual_path,
        target_source=None,
        transition_in_progress=transition,
        recovery_required=recovery,
        fault="transfer fault" if recovery else None,
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
        ),
        generators={
            GeneratorSlot.A: a or generator_status(GeneratorSlot.A),
            GeneratorSlot.B: b or generator_status(GeneratorSlot.B),
        },
        power_inputs_known=power_inputs_known,
        bus=bus_status or bus(),
    )


def initialize(supervisor: EnergySupervisor, *, grid_ready=True) -> None:
    supervisor.step(
        0.0,
        observation(
            grid_ready=grid_ready,
            source=PowerSource.GRID if grid_ready else PowerSource.UPS_ONLY,
            path=PowerPath.GRID,
        ),
    )


def start_manual(supervisor: EnergySupervisor, *, grid_ready=True):
    initialize(supervisor, grid_ready=grid_ready)
    supervisor.request_manual_start()
    return supervisor.step(
        1.0,
        observation(
            grid_ready=grid_ready,
            source=PowerSource.GRID if grid_ready else PowerSource.UPS_ONLY,
            path=PowerPath.GRID,
        ),
    )


def stable_session(
    *,
    reason=SessionReason.GRID_OUTAGE,
    generator=GeneratorSlot.A,
    grid_was_unavailable=True,
) -> EnergySupervisor:
    supervisor = EnergySupervisor()
    supervisor.initialized = True
    supervisor.session = GeneratorSession.begin(
        reason=reason,
        generator=generator,
        now=0.0,
        grid_was_unavailable=grid_was_unavailable,
    )
    supervisor.phase = SupervisorPhase.ON_GENERATOR
    supervisor.desired_source = PowerSource.GENERATOR
    supervisor.desired_generators[generator] = True
    supervisor.transaction = Transaction.begin(
        "enter_generator", generator.value, 0.0, "stable"
    )
    supervisor.transaction.complete(0.0, "stable")
    return supervisor


def test_manual_start_uses_primary_generator():
    supervisor = EnergySupervisor(
        SupervisorConfig(primary_generator=GeneratorSlot.B)
    )
    initialize(supervisor)
    supervisor.request_manual_start()
    decision = supervisor.step(1.0, observation())

    assert supervisor.session is not None
    assert supervisor.session.generator == GeneratorSlot.B
    assert decision.desired_generators == {
        GeneratorSlot.A: False,
        GeneratorSlot.B: True,
    }


def test_disabled_primary_blocks_new_session():
    supervisor = EnergySupervisor(
        SupervisorConfig(primary_generator=GeneratorSlot.B, generator_b_enabled=False)
    )
    initialize(supervisor)
    supervisor.request_manual_start()
    supervisor.step(1.0, observation())
    assert supervisor.session is None


def test_grid_outage_waits_delay_then_starts_primary():
    supervisor = EnergySupervisor(SupervisorConfig(grid_failure_delay=5.0))
    outage = observation(
        grid_ready=False,
        automatic=True,
        source=PowerSource.UPS_ONLY,
        path=PowerPath.GRID,
    )

    supervisor.step(0.0, outage)
    assert supervisor.phase == SupervisorPhase.GRID_FAILURE_DELAY
    supervisor.step(4.9, outage)
    assert supervisor.session is None

    decision = supervisor.step(5.0, outage)
    assert supervisor.session is not None
    assert supervisor.session.reason == SessionReason.GRID_OUTAGE
    assert decision.desired_generators[GeneratorSlot.A] is True


def test_intentional_grid_disconnect_does_not_start_outage_session():
    supervisor = EnergySupervisor(SupervisorConfig(grid_failure_delay=0.0))
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


def test_two_running_generators_are_not_an_interlock_fault():
    supervisor = stable_session()
    decision = supervisor.step(
        1.0,
        observation(
            grid_ready=False,
            source=PowerSource.GENERATOR,
            path=PowerPath.GENERATOR,
            a=ready(GeneratorSlot.A),
            b=generator_status(
                GeneratorSlot.B,
                phase=GeneratorPhase.EXTERNAL_RUNNING,
                running=True,
                remote_on=False,
            ),
            bus_status=bus(
                GeneratorBusOwner.A,
                a_context=GeneratorRunContext.MANAGED_OUTAGE,
                b_context=GeneratorRunContext.EXTERNAL_OUTAGE,
            ),
        ),
    )
    assert supervisor.phase == SupervisorPhase.ON_GENERATOR
    assert decision.actions_allowed is True


def test_primary_failure_falls_back_once_to_stopped_secondary():
    supervisor = EnergySupervisor()
    start_manual(supervisor, grid_ready=False)
    failed_a = generator_status(
        GeneratorSlot.A,
        phase=GeneratorPhase.FAULT,
        remote_on=True,
        fault="primary failed",
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
    assert decision.desired_generators == {
        GeneratorSlot.A: False,
        GeneratorSlot.B: True,
    }


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
    assert decision.desired_generators == {
        GeneratorSlot.A: False,
        GeneratorSlot.B: False,
    }


def test_external_secondary_takeover_does_not_change_managed_generator():
    supervisor = stable_session()
    decision = supervisor.step(
        1.0,
        observation(
            grid_ready=False,
            source=PowerSource.GENERATOR,
            path=PowerPath.GENERATOR,
            a=generator_status(
                GeneratorSlot.A,
                phase=GeneratorPhase.FAULT,
                running=False,
                remote_on=True,
                fault="A stopped",
            ),
            b=generator_status(
                GeneratorSlot.B,
                phase=GeneratorPhase.EXTERNAL_RUNNING,
                running=True,
                remote_on=False,
            ),
            bus_status=bus(
                GeneratorBusOwner.B,
                b_context=GeneratorRunContext.EXTERNAL_OUTAGE,
            ),
        ),
    )
    assert supervisor.phase == SupervisorPhase.ON_EXTERNAL_GENERATOR
    assert supervisor.session is not None
    assert supervisor.session.generator == GeneratorSlot.A
    assert decision.desired_generators[GeneratorSlot.B] is False


def test_stable_grid_starts_return_from_outage_session():
    supervisor = stable_session()
    supervisor.config = SupervisorConfig(grid_restore_stable_time=60.0)
    returned_grid = observation(
        grid_ready=True,
        source=PowerSource.GENERATOR,
        path=PowerPath.GENERATOR,
        a=ready(GeneratorSlot.A),
        bus_status=bus(
            GeneratorBusOwner.A,
            a_context=GeneratorRunContext.MANAGED_OUTAGE,
        ),
    )
    supervisor.step(10.0, returned_grid)
    assert supervisor.phase == SupervisorPhase.ON_GENERATOR
    decision = supervisor.step(70.0, returned_grid)
    assert supervisor.phase == SupervisorPhase.RETURNING_TO_GRID
    assert decision.desired_source == PowerSource.GRID


def test_grid_return_stops_all_outage_related_runs_after_house_is_on_grid():
    supervisor = stable_session()
    supervisor.phase = SupervisorPhase.RETURNING_TO_GRID
    status = bus(
        GeneratorBusOwner.A,
        a_context=GeneratorRunContext.MANAGED_OUTAGE,
        b_context=GeneratorRunContext.EXTERNAL_OUTAGE,
    )
    decision = supervisor.step(
        1.0,
        observation(
            grid_ready=True,
            source=PowerSource.GRID,
            path=PowerPath.GRID,
            a=ready(GeneratorSlot.A),
            b=generator_status(
                GeneratorSlot.B,
                phase=GeneratorPhase.EXTERNAL_RUNNING,
                running=True,
                remote_on=True,
            ),
            bus_status=status,
        ),
    )
    assert supervisor.phase == SupervisorPhase.STOPPING_GENERATORS
    assert decision.stop_outage_generators == frozenset(
        {GeneratorSlot.A, GeneratorSlot.B}
    )


def test_test_run_is_never_part_of_outage_cleanup():
    supervisor = EnergySupervisor(SupervisorConfig(grid_restore_stable_time=0.0))
    decision = supervisor.step(
        0.0,
        observation(
            grid_ready=True,
            source=PowerSource.GRID,
            path=PowerPath.GRID,
            a=generator_status(
                GeneratorSlot.A,
                phase=GeneratorPhase.EXTERNAL_RUNNING,
                running=True,
                remote_on=True,
            ),
            bus_status=bus(
                GeneratorBusOwner.A,
                a_context=GeneratorRunContext.TEST_RUN,
            ),
        ),
    )
    assert decision.stop_outage_generators == frozenset()


def test_manual_stop_during_outage_does_not_stop_external_secondary():
    supervisor = stable_session()
    supervisor.request_manual_stop()
    decision = supervisor.step(
        1.0,
        observation(
            grid_ready=False,
            source=PowerSource.GENERATOR,
            path=PowerPath.GENERATOR,
            a=ready(GeneratorSlot.A),
            b=generator_status(
                GeneratorSlot.B,
                phase=GeneratorPhase.EXTERNAL_RUNNING,
                running=True,
                remote_on=True,
            ),
            bus_status=bus(
                GeneratorBusOwner.A,
                a_context=GeneratorRunContext.MANAGED_OUTAGE,
                b_context=GeneratorRunContext.EXTERNAL_OUTAGE,
            ),
        ),
    )
    assert supervisor.phase == SupervisorPhase.RETURNING_TO_GRID
    assert decision.stop_outage_generators == frozenset()


def test_missing_required_physical_data_disallows_actions():
    supervisor = EnergySupervisor()
    initialize(supervisor)
    supervisor.request_manual_start()
    decision = supervisor.step(
        1.0,
        observation(
            a=generator_status(
                GeneratorSlot.A,
                running=None,
                remote_on=None,
            )
        ),
    )
    assert supervisor.session is None
    assert decision.actions_allowed is False


def test_transfer_recovery_propagates_to_supervisor():
    supervisor = EnergySupervisor()
    initialize(supervisor)
    decision = supervisor.step(
        1.0,
        observation(
            source=PowerSource.UNKNOWN,
            path=PowerPath.UNKNOWN,
            recovery=True,
        ),
    )
    assert supervisor.phase == SupervisorPhase.RECOVERY_REQUIRED
    assert decision.actions_allowed is False


def test_supervisor_round_trip_persists_only_current_model():
    supervisor = EnergySupervisor()
    start_manual(supervisor, grid_ready=False)
    assert supervisor.session is not None
    supervisor.session.fallback_used = True

    restored = EnergySupervisor.from_dict(supervisor.to_dict(), SupervisorConfig())
    assert restored.session is not None
    assert restored.session.generator == GeneratorSlot.A
    assert restored.session.fallback_used is True
    assert "schema_version" not in supervisor.to_dict()


def test_restart_accepts_matching_stable_managed_session():
    supervisor = stable_session()
    supervisor.initialized = False
    restored = EnergySupervisor.from_dict(supervisor.to_dict(), SupervisorConfig())
    restored.initialized = False
    restored.step(
        10.0,
        observation(
            grid_ready=False,
            source=PowerSource.GENERATOR,
            path=PowerPath.GENERATOR,
            a=ready(GeneratorSlot.A),
            bus_status=bus(
                GeneratorBusOwner.A,
                a_context=GeneratorRunContext.MANAGED_OUTAGE,
            ),
        ),
    )
    assert restored.phase == SupervisorPhase.ON_GENERATOR
    assert restored.recovery_reason is None


def test_connection_loss_during_transaction_requires_recovery():
    supervisor = EnergySupervisor()
    start_manual(supervisor)
    assert supervisor.transaction is not None
    supervisor.mark_connection_lost(2.0)
    assert supervisor.phase == SupervisorPhase.RECOVERY_REQUIRED
    assert supervisor.recovery_reason is not None

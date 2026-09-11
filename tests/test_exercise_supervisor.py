from __future__ import annotations

from domain import GeneratorSlot, PowerPath, PowerSource, SessionReason
from energy_supervisor import (
    EnergySupervisor,
    ExerciseDirective,
    SupervisorConfig,
    SupervisorObservation,
    SupervisorPhase,
)
from generator_bus import (
    GeneratorBusOwner,
    GeneratorBusStatus,
    GeneratorBusTracker,
    GeneratorRunContext,
)
from generator_controller import GeneratorPhase, GeneratorStatus
from power_transfer import PowerTransferStatus, TransferPhase


def generator_status(
    slot: GeneratorSlot,
    *,
    running: bool,
    remote: bool,
    ready: bool = False,
    fault: str | None = None,
) -> GeneratorStatus:
    return GeneratorStatus(
        slot=slot,
        display_name="Elemax" if slot == GeneratorSlot.A else "Вепрь",
        phase=(GeneratorPhase.READY_FOR_LOAD if ready else GeneratorPhase.IDLE),
        running=running,
        remote_on=remote,
        ready_for_load=ready,
        fault=fault,
    )


def observation(
    *,
    grid: bool,
    automatic: bool = True,
    a_running: bool = False,
    a_remote: bool = False,
    a_ready: bool = False,
    b_running: bool = False,
    b_remote: bool = False,
    owner: GeneratorBusOwner = GeneratorBusOwner.NONE,
) -> SupervisorObservation:
    source = PowerSource.GRID if grid else PowerSource.UPS_ONLY
    path = PowerPath.GRID if grid else PowerPath.ISOLATED
    phase = TransferPhase.STABLE_GRID if grid else TransferPhase.STABLE_ISOLATED
    return SupervisorObservation(
        grid_ready=grid,
        automatic_transfer_enabled=automatic,
        emergency_stop=False,
        power=PowerTransferStatus(
            phase=phase,
            actual_source=source,
            actual_path=path,
            target_source=source,
            transition_in_progress=False,
            recovery_required=False,
            fault=None,
        ),
        generators={
            GeneratorSlot.A: generator_status(
                GeneratorSlot.A,
                running=a_running,
                remote=a_remote,
                ready=a_ready,
            ),
            GeneratorSlot.B: generator_status(
                GeneratorSlot.B,
                running=b_running,
                remote=b_remote,
            ),
        },
        bus=GeneratorBusStatus(
            owner=owner,
            run_contexts={
                GeneratorSlot.A: (
                    GeneratorRunContext.TEST_RUN
                    if a_running
                    else GeneratorRunContext.NONE
                ),
                GeneratorSlot.B: (
                    GeneratorRunContext.OTHER
                    if b_running
                    else GeneratorRunContext.NONE
                ),
            },
        ),
    )


def test_outage_adopts_running_exercise_generator_even_when_primary_is_other_slot():
    """Проверяет, что после outage delay уже RUNNING Exercise-generator принимается в outage-session вместо cold start настроенного PRIMARY."""
    supervisor = EnergySupervisor(
        SupervisorConfig(
            grid_failure_delay=5,
            primary_generator=GeneratorSlot.B,
        )
    )
    o = observation(
        grid=False,
        a_running=True,
        a_remote=True,
        a_ready=True,
        owner=GeneratorBusOwner.A,
    )

    first = supervisor.step(
        0,
        o,
        exercise_owned_slot=GeneratorSlot.A,
        exercise_desired_running=True,
    )
    assert supervisor.phase == SupervisorPhase.GRID_FAILURE_DELAY
    assert first.desired_generators[GeneratorSlot.A] is True
    assert first.desired_generators[GeneratorSlot.B] is False

    second = supervisor.step(
        5,
        o,
        exercise_owned_slot=GeneratorSlot.A,
        exercise_desired_running=True,
    )
    assert supervisor.session is not None
    assert supervisor.session.generator == GeneratorSlot.A
    assert supervisor.session.grid_was_unavailable is True
    assert supervisor.phase == SupervisorPhase.STARTING_GENERATOR
    assert second.desired_generators[GeneratorSlot.A] is True
    assert second.desired_generators[GeneratorSlot.B] is False


def test_unstarted_exercise_is_cancelled_before_outage_primary_start():
    """Проверяет REQ-BEH-03: реальный outage отменяет ещё не начавшийся физически Exercise и продолжает обычный запуск PRIMARY, не позволяя maintenance-run стартовать во время outage."""
    supervisor = EnergySupervisor(
        SupervisorConfig(
            grid_failure_delay=1,
            primary_generator=GeneratorSlot.B,
        )
    )
    o = observation(grid=False)

    supervisor.step(
        0,
        o,
        exercise_owned_slot=GeneratorSlot.A,
        exercise_desired_running=True,
    )
    decision = supervisor.step(
        2,
        o,
        exercise_owned_slot=GeneratorSlot.A,
        exercise_desired_running=True,
    )

    assert supervisor.session is not None
    assert supervisor.session.reason == SessionReason.GRID_OUTAGE
    assert supervisor.session.generator == GeneratorSlot.B
    assert supervisor.phase == SupervisorPhase.STARTING_GENERATOR
    assert decision.exercise_directive == ExerciseDirective.CANCEL_UNSTARTED
    assert decision.exercise_slot == GeneratorSlot.A
    assert decision.desired_generators[GeneratorSlot.A] is False
    assert decision.desired_generators[GeneratorSlot.B] is True


def test_exercise_does_not_create_outage_session_when_automatic_transfer_is_disabled():
    """Проверяет, что отключённый automatic transfer не превращает RUNNING Exercise в outage managed-session."""
    supervisor = EnergySupervisor(
        SupervisorConfig(grid_failure_delay=0, primary_generator=GeneratorSlot.B)
    )
    o = observation(
        grid=False,
        automatic=False,
        a_running=True,
        a_remote=True,
        a_ready=True,
        owner=GeneratorBusOwner.A,
    )

    decision = supervisor.step(
        0,
        o,
        exercise_owned_slot=GeneratorSlot.A,
        exercise_desired_running=True,
    )

    assert supervisor.session is None
    assert supervisor.phase == SupervisorPhase.NORMAL
    assert decision.desired_generators[GeneratorSlot.A] is True
    assert decision.desired_generators[GeneratorSlot.B] is False


def test_foreign_running_generator_still_prevents_exercise_adoption():
    """Проверяет, что наличие отдельного внешнего RUNNING generator не позволяет молча принять Exercise-generator в managed outage-session."""
    supervisor = EnergySupervisor(
        SupervisorConfig(grid_failure_delay=0, primary_generator=GeneratorSlot.B)
    )
    o = observation(
        grid=False,
        a_running=True,
        a_remote=True,
        a_ready=True,
        b_running=True,
        b_remote=False,
        owner=GeneratorBusOwner.A,
    )

    supervisor.step(
        0,
        o,
        exercise_owned_slot=GeneratorSlot.A,
        exercise_desired_running=True,
    )

    assert supervisor.session is None
    assert supervisor.phase == SupervisorPhase.EXTERNAL_RUNNING


def test_generator_bus_marks_internal_scheduled_run_as_test_run_even_during_grid_outage():
    """Проверяет, что Scheduled Exercise сохраняет run-context TEST_RUN даже если Grid исчезла после начала тестового запуска."""
    tracker = GeneratorBusTracker()
    tracker.update(
        {GeneratorSlot.A: False, GeneratorSlot.B: False},
        grid_ready=True,
        test_mode=False,
    )
    status = tracker.update(
        {GeneratorSlot.A: True, GeneratorSlot.B: False},
        grid_ready=False,
        test_mode=False,
        internal_test_slots=frozenset({GeneratorSlot.A}),
    )

    assert status.run_contexts[GeneratorSlot.A] == GeneratorRunContext.TEST_RUN


def test_generator_bus_restores_running_internal_test_context_without_reclassification():
    """Проверяет, что restart Tracker не переклассифицирует продолжающийся internal TEST_RUN как outage-related только из-за текущего Grid state."""
    tracker = GeneratorBusTracker()
    tracker.update(
        {GeneratorSlot.A: True, GeneratorSlot.B: False},
        grid_ready=True,
        test_mode=False,
        internal_test_slots=frozenset({GeneratorSlot.A}),
    )
    restored = GeneratorBusTracker.from_dict(tracker.to_dict())

    status = restored.update(
        {GeneratorSlot.A: True, GeneratorSlot.B: False},
        grid_ready=False,
        test_mode=False,
        internal_test_slots=frozenset({GeneratorSlot.A}),
    )

    assert status.run_contexts[GeneratorSlot.A] == GeneratorRunContext.TEST_RUN

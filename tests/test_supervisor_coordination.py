from __future__ import annotations

from datetime import datetime, timezone

from domain import GeneratorSlot, PowerPath, PowerSource, SessionReason
from energy_supervisor import (
    EnergySupervisor,
    ExerciseDirective,
    GeneratorSession,
    SupervisorConfig,
    SupervisorObservation,
    SupervisorPhase,
)
from exercise_scheduler import (
    ExerciseAttempt,
    ExerciseAttemptPhase,
    ExerciseConfig,
    ExerciseGeneratorObservation,
    ExerciseObservation,
    ExerciseResult,
    ExerciseScheduler,
)
from generator_bus import GeneratorBusOwner, GeneratorBusStatus, GeneratorRunContext
from generator_controller import GeneratorPhase, GeneratorStatus
from power_transfer import PowerTransferStatus, TransferPhase


def _generator(
    slot: GeneratorSlot,
    *,
    running: bool = False,
    remote_on: bool = False,
    ready: bool = False,
    phase: GeneratorPhase | None = None,
    fault: str | None = None,
) -> GeneratorStatus:
    if phase is None:
        phase = GeneratorPhase.READY_FOR_LOAD if ready else GeneratorPhase.IDLE
    return GeneratorStatus(
        slot=slot,
        display_name="Elemax" if slot == GeneratorSlot.A else "Вепрь",
        phase=phase,
        running=running,
        remote_on=remote_on,
        ready_for_load=ready,
        fault=fault,
    )


def _bus(owner: GeneratorBusOwner = GeneratorBusOwner.NONE) -> GeneratorBusStatus:
    return GeneratorBusStatus(
        owner=owner,
        run_contexts={
            GeneratorSlot.A: GeneratorRunContext.NONE,
            GeneratorSlot.B: GeneratorRunContext.NONE,
        },
    )


def _power(
    source: PowerSource,
    path: PowerPath,
    *,
    transition: bool = False,
) -> PowerTransferStatus:
    phase = (
        TransferPhase.STABLE_GRID
        if path == PowerPath.GRID
        else TransferPhase.STABLE_GENERATOR
        if path == PowerPath.GENERATOR
        else TransferPhase.STABLE_ISOLATED
        if path == PowerPath.ISOLATED
        else TransferPhase.WAITING_FOR_DATA
    )
    return PowerTransferStatus(
        phase=phase,
        actual_source=source,
        actual_path=path,
        target_source=None,
        transition_in_progress=transition,
        recovery_required=False,
        fault=None,
    )


def _observation(
    *,
    grid_ready: bool,
    automatic: bool = True,
    source: PowerSource | None = None,
    path: PowerPath | None = None,
    a: GeneratorStatus | None = None,
    b: GeneratorStatus | None = None,
    owner: GeneratorBusOwner = GeneratorBusOwner.NONE,
    emergency: bool = False,
) -> SupervisorObservation:
    if source is None:
        source = PowerSource.GRID if grid_ready else PowerSource.UPS_ONLY
    if path is None:
        path = PowerPath.GRID
    return SupervisorObservation(
        grid_ready=grid_ready,
        automatic_transfer_enabled=automatic,
        emergency_stop=emergency,
        power=_power(source, path),
        generators={
            GeneratorSlot.A: a or _generator(GeneratorSlot.A),
            GeneratorSlot.B: b or _generator(GeneratorSlot.B),
        },
        power_inputs_known=True,
        bus=_bus(owner),
    )


def _initialize(supervisor: EnergySupervisor) -> None:
    supervisor.step(0.0, _observation(grid_ready=True, automatic=False))


def _cycle_supervisor(*, phase: SupervisorPhase = SupervisorPhase.ON_GENERATOR) -> EnergySupervisor:
    supervisor = EnergySupervisor(SupervisorConfig(grid_restore_stable_time=0.0))
    supervisor.initialized = True
    supervisor.phase = phase
    supervisor.session = GeneratorSession.begin(
        SessionReason.GRID_OUTAGE,
        GeneratorSlot.A,
        grid_was_unavailable=True,
    )
    supervisor.session.cycle_owned = True
    supervisor.desired_source = (
        PowerSource.UPS_ONLY
        if phase == SupervisorPhase.RETURNING_TO_UPS
        else PowerSource.GENERATOR
    )
    supervisor.desired_generators[GeneratorSlot.A] = True
    return supervisor


def test_req_beh_04_outage_adopts_running_exercise_without_cycle_claim() -> None:
    """Проверяет, что реальный outage использует уже RUNNING Exercise-generator без stop/start и не ошибочно присваивает ему ownership Charge Cycling."""
    supervisor = EnergySupervisor(SupervisorConfig(grid_failure_delay=0.0))
    running_a = _generator(
        GeneratorSlot.A,
        running=True,
        remote_on=True,
        ready=True,
    )

    decision = supervisor.step(
        10.0,
        _observation(
            grid_ready=False,
            a=running_a,
            owner=GeneratorBusOwner.A,
        ),
        exercise_owned_slot=GeneratorSlot.A,
        exercise_desired_running=True,
        outage_delay_already_satisfied=True,
        claim_new_outage_session=True,
    )

    assert supervisor.session is not None
    assert supervisor.session.reason == SessionReason.GRID_OUTAGE
    assert supervisor.session.generator == GeneratorSlot.A
    assert supervisor.session.cycle_owned is False
    assert decision.exercise_directive == ExerciseDirective.HANDOFF_TO_OUTAGE
    assert decision.exercise_slot == GeneratorSlot.A


def test_req_cycle_04_fresh_automatic_outage_can_be_cycle_owned() -> None:
    """Проверяет, что новая обычная automatic outage-session может стать cycle-owned, если generator не принадлежит другому сценарию."""
    supervisor = EnergySupervisor(SupervisorConfig(grid_failure_delay=0.0))

    supervisor.step(
        10.0,
        _observation(grid_ready=False),
        outage_delay_already_satisfied=True,
        claim_new_outage_session=True,
    )

    assert supervisor.session is not None
    assert supervisor.session.reason == SessionReason.GRID_OUTAGE
    assert supervisor.session.cycle_owned is True


def test_req_beh_06_manual_preempts_unstarted_exercise() -> None:
    """Проверяет, что manual reserve request отменяет ещё не начавшийся Exercise и создаёт обычную ручную managed-session без запуска maintenance-generator."""
    supervisor = EnergySupervisor(SupervisorConfig(primary_generator=GeneratorSlot.A))
    _initialize(supervisor)
    supervisor.request_manual_start()

    decision = supervisor.step(
        1.0,
        _observation(grid_ready=True, automatic=False),
        exercise_owned_slot=GeneratorSlot.B,
        exercise_desired_running=True,
    )

    assert supervisor.session is not None
    assert supervisor.session.reason == SessionReason.MANUAL_GENERATOR_START
    assert supervisor.session.generator == GeneratorSlot.A
    assert decision.exercise_directive == ExerciseDirective.CANCEL_UNSTARTED
    assert decision.exercise_slot == GeneratorSlot.B
    assert decision.desired_generators[GeneratorSlot.B] is False


def test_req_beh_07_manual_adopts_running_exercise_without_restart() -> None:
    """Проверяет, что manual reserve request принимает уже RUNNING исправный Exercise-generator и не требует его остановки с новым холодным запуском."""
    supervisor = EnergySupervisor()
    _initialize(supervisor)
    supervisor.request_manual_start()
    running_b = _generator(
        GeneratorSlot.B,
        running=True,
        remote_on=True,
        ready=True,
    )

    decision = supervisor.step(
        1.0,
        _observation(
            grid_ready=True,
            automatic=False,
            b=running_b,
            owner=GeneratorBusOwner.B,
        ),
        exercise_owned_slot=GeneratorSlot.B,
        exercise_desired_running=True,
    )

    assert supervisor.session is not None
    assert supervisor.session.reason == SessionReason.MANUAL_GENERATOR_START
    assert supervisor.session.generator == GeneratorSlot.B
    assert decision.exercise_directive == ExerciseDirective.HANDOFF_TO_MANUAL
    assert decision.desired_generators[GeneratorSlot.B] is True


def test_req_beh_03_outage_cancels_unstarted_exercise_before_ups_wait() -> None:
    """Проверяет, что outage не позволяет логически начатому, но физически ещё не стартовавшему Exercise завести generator во время намеренного UPS-only ожидания."""
    supervisor = EnergySupervisor(SupervisorConfig(grid_failure_delay=0.0))

    decision = supervisor.step(
        10.0,
        _observation(grid_ready=False),
        exercise_owned_slot=GeneratorSlot.B,
        exercise_desired_running=True,
        outage_delay_already_satisfied=True,
        defer_automatic_start=True,
    )

    assert supervisor.session is None
    assert supervisor.phase == SupervisorPhase.GRID_FAILURE_DELAY
    assert decision.exercise_directive == ExerciseDirective.CANCEL_UNSTARTED
    assert decision.desired_generators[GeneratorSlot.B] is False


def test_req_beh_05_ambiguous_exercise_outage_requires_recovery() -> None:
    """Проверяет, что outage во время неоднозначной Exercise start-phase не создаёт duplicate REMOTE или новый managed start, а переводит систему в Recovery."""
    supervisor = EnergySupervisor(SupervisorConfig(grid_failure_delay=0.0))
    starting_a = _generator(
        GeneratorSlot.A,
        running=False,
        remote_on=True,
        phase=GeneratorPhase.WAITING_FOR_RUNNING,
    )

    decision = supervisor.step(
        10.0,
        _observation(grid_ready=False, a=starting_a),
        exercise_owned_slot=GeneratorSlot.A,
        exercise_desired_running=True,
        outage_delay_already_satisfied=True,
    )

    assert supervisor.session is None
    assert supervisor.phase == SupervisorPhase.RECOVERY_REQUIRED
    assert decision.exercise_directive == ExerciseDirective.FAIL_ACTIVE
    assert not any(decision.desired_generators.values())


def test_req_beh_08_recovery_preserves_exercise_shutdown_responsibility() -> None:
    """Проверяет, что системный Recovery запрещает продолжать Exercise, но Supervisor явно поручает Scheduler сохранить обязанность безопасной остановки auto-run."""
    supervisor = EnergySupervisor()
    supervisor.initialized = True
    supervisor.phase = SupervisorPhase.RECOVERY_REQUIRED

    decision = supervisor.step(
        10.0,
        _observation(grid_ready=True, automatic=False),
        exercise_owned_slot=GeneratorSlot.A,
        exercise_desired_running=True,
    )

    assert decision.exercise_directive == ExerciseDirective.FAIL_ACTIVE
    assert decision.exercise_slot == GeneratorSlot.A
    assert decision.actions_allowed is False


def test_req_beh_11_target_soc_requests_supervisor_controlled_cycle_stop() -> None:
    """Проверяет, что Target SoC переводит cycle-owned session в Generator -> UPS_ONLY через Supervisor, а не выполняет аппаратную остановку внутри UPS Run."""
    supervisor = _cycle_supervisor()

    decision = supervisor.step(
        10.0,
        _observation(
            grid_ready=False,
            source=PowerSource.GENERATOR,
            path=PowerPath.GENERATOR,
            a=_generator(
                GeneratorSlot.A,
                running=True,
                remote_on=True,
                ready=True,
            ),
            owner=GeneratorBusOwner.A,
        ),
        request_cycle_stop=True,
    )

    assert supervisor.phase == SupervisorPhase.RETURNING_TO_UPS
    assert decision.desired_source == PowerSource.UPS_ONLY
    assert decision.desired_generators[GeneratorSlot.A] is True
    assert decision.begin_post_cycle_wait is False


def test_req_cycle_07_completed_cycle_begins_next_ups_wait() -> None:
    """Проверяет, что после подтверждённого Generator -> UPS_ONLY и остановки cycle-owned generator начинается новый UPS interval без повторного core delay."""
    supervisor = _cycle_supervisor(phase=SupervisorPhase.RETURNING_TO_UPS)

    decision = supervisor.step(
        20.0,
        _observation(
            grid_ready=False,
            source=PowerSource.UPS_ONLY,
            path=PowerPath.ISOLATED,
        ),
    )

    assert supervisor.session is None
    assert supervisor.phase == SupervisorPhase.NORMAL
    assert decision.begin_post_cycle_wait is True


def test_req_beh_12_stable_grid_wins_over_target_soc_return_to_ups() -> None:
    """Проверяет, что при одновременном Target SoC и устойчивом восстановлении Grid Supervisor возвращает дом на Grid, а не начинает промежуточный UPS-only цикл."""
    supervisor = _cycle_supervisor()

    decision = supervisor.step(
        30.0,
        _observation(
            grid_ready=True,
            source=PowerSource.GENERATOR,
            path=PowerPath.GENERATOR,
            a=_generator(
                GeneratorSlot.A,
                running=True,
                remote_on=True,
                ready=True,
            ),
            owner=GeneratorBusOwner.A,
        ),
        request_cycle_stop=True,
    )

    assert supervisor.phase == SupervisorPhase.RETURNING_TO_GRID
    assert decision.desired_source == PowerSource.GRID
    assert decision.begin_post_cycle_wait is False


def test_req_beh_13_manual_override_wins_over_same_tick_target_soc() -> None:
    """Проверяет, что manual request в том же tick сильнее Target SoC: cycle ownership снимается и automatic stop не начинается."""
    supervisor = _cycle_supervisor()
    supervisor.request_manual_start()

    decision = supervisor.step(
        10.0,
        _observation(
            grid_ready=False,
            source=PowerSource.GENERATOR,
            path=PowerPath.GENERATOR,
            a=_generator(
                GeneratorSlot.A,
                running=True,
                remote_on=True,
                ready=True,
            ),
            owner=GeneratorBusOwner.A,
        ),
        request_cycle_stop=True,
    )

    assert supervisor.session is not None
    assert supervisor.session.manual_override is True
    assert supervisor.session.cycle_owned is False
    assert supervisor.phase == SupervisorPhase.ON_GENERATOR
    assert decision.desired_source == PowerSource.GENERATOR


def test_scheduler_manual_handoff_is_neutral_interruption() -> None:
    """Проверяет, что передача RUNNING Exercise ручной managed-session фиксируется как нейтральное INTERRUPTED_BY_MANUAL, а не как FAILED."""
    configs = {
        GeneratorSlot.A: ExerciseConfig(False, 30, "15:00", 10, 7),
        GeneratorSlot.B: ExerciseConfig(False, 45, "15:00", 10, 14),
    }
    scheduler = ExerciseScheduler(configs)
    scheduler.active_attempt = ExerciseAttempt(
        slot=GeneratorSlot.A,
        scheduled_time="2026-09-11T15:00:00+03:00",
        forced=False,
        phase=ExerciseAttemptPhase.RUNNING,
        actual_start_time="2026-09-11T15:00:00+03:00",
        started_at=100.0,
        run_until=700.0,
    )
    observation = ExerciseObservation(
        now=160.0,
        local_now=datetime(2026, 9, 11, 15, 1, tzinfo=timezone.utc),
        grid_ready=True,
        grid_path_stable=True,
        family_present=False,
        emergency_stop=False,
        required_states_known=True,
        power_transition_in_progress=False,
        policy_busy=True,
        actions_enabled=True,
        generators={
            GeneratorSlot.A: ExerciseGeneratorObservation(True, True, None),
            GeneratorSlot.B: ExerciseGeneratorObservation(False, False, None),
        },
        generator_names={GeneratorSlot.A: "Elemax", GeneratorSlot.B: "Вепрь"},
    )

    scheduler.handoff_to_manual(GeneratorSlot.A, observation)

    assert scheduler.active_attempt is None
    assert scheduler.states[GeneratorSlot.A].last_result == ExerciseResult.INTERRUPTED_BY_MANUAL.value
    assert scheduler.states[GeneratorSlot.A].last_failure_reason is None

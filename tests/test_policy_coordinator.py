from __future__ import annotations

from types import SimpleNamespace

from domain import GeneratorSlot, SessionReason, SupervisorEvent
from energy_supervisor import SupervisorDecision, SupervisorPhase
from exercise_scheduler import ExerciseDecision
from outage_power_policy import OutagePowerDecision
from policy_coordinator import PolicyCoordinator


_UNCHANGED = object()


def _decision() -> SupervisorDecision:
    return SupervisorDecision(
        desired_source=None,
        desired_generators={GeneratorSlot.A: False, GeneratorSlot.B: False},
        actions_allowed=True,
        stable_managed_generator=None,
        stop_outage_generators=frozenset(),
        events=(),
    )


class SupervisorStub:
    def __init__(self) -> None:
        self.session = None
        self.phase = SupervisorPhase.NORMAL
        self.next_session = _UNCHANGED
        self.next_phase = None
        self.cycle_stop_requested = False
        self.cycle_owned_marked = False
        self.step_kwargs = None

    def request_cycle_stop(self) -> None:
        self.cycle_stop_requested = True

    def step(self, _now, _observation, **kwargs):
        self.step_kwargs = kwargs
        if self.next_session is not _UNCHANGED:
            self.session = self.next_session
        if self.next_phase is not None:
            self.phase = self.next_phase
        return _decision()

    def mark_session_cycle_owned(self) -> bool:
        self.cycle_owned_marked = True
        if self.session is not None:
            self.session.cycle_owned = True
        return True


class ExerciseStub:
    def __init__(self, owned_slot=None) -> None:
        self.owned_slot = owned_slot
        self.authorized_shutdown_slot = None
        self.handoff_calls = []
        self.cancel_calls = []
        self.fail_calls = []

    def handoff_to_outage(self, slot, _observation):
        self.handoff_calls.append(slot)
        self.owned_slot = None
        return (SupervisorEvent("warning", "handoff"),)

    def cancel_unstarted(self, _observation, reason):
        self.cancel_calls.append(reason)
        self.owned_slot = None
        return (SupervisorEvent("info", "cancel"),)

    def fail_active(self, _observation, reason):
        self.fail_calls.append(reason)
        self.authorized_shutdown_slot = self.owned_slot
        return (SupervisorEvent("critical", "failed"),)


class OutagePolicyStub:
    def __init__(self) -> None:
        self.post_cycle_waits = []

    def begin_post_cycle_wait(self, now):
        self.post_cycle_waits.append(now)


def _exercise_decision(slot=None, *, desired=False) -> ExerciseDecision:
    return ExerciseDecision(
        owned_slot=slot,
        desired_running=desired,
        authorized_shutdown_slot=None,
        warnings=(),
        events=(),
    )


def _coordinator(supervisor, exercise, outage):
    return PolicyCoordinator(supervisor, exercise, outage)


def test_outage_adopts_exercise_without_charge_cycle_claim():
    """Реальный outage принимает уже запущенный Exercise-generator, но не делает эту session cycling-owned. Ownership передаётся только после создания outage-session на том же slot."""
    supervisor = SupervisorStub()
    supervisor.next_session = SimpleNamespace(
        reason=SessionReason.GRID_OUTAGE,
        generator=GeneratorSlot.A,
        cycle_owned=False,
    )
    exercise = ExerciseStub(GeneratorSlot.A)
    outage = OutagePolicyStub()

    result = _coordinator(supervisor, exercise, outage).step(
        10,
        object(),
        object(),
        _exercise_decision(GeneratorSlot.A, desired=True),
        OutagePowerDecision(claim_new_outage_session=True),
        grid_ready=False,
    )

    assert exercise.handoff_calls == [GeneratorSlot.A]
    assert supervisor.cycle_owned_marked is False
    assert exercise.owned_slot is None
    assert [event.message for event in result.events] == ["handoff"]


def test_fresh_automatic_outage_can_be_claimed_by_charge_cycling():
    """Charge Cycling захватывает только новую обычную outage-session, когда нет другого policy-owner генератора."""
    supervisor = SupervisorStub()
    supervisor.next_session = SimpleNamespace(
        reason=SessionReason.GRID_OUTAGE,
        generator=GeneratorSlot.A,
        cycle_owned=False,
    )
    exercise = ExerciseStub()
    outage = OutagePolicyStub()

    _coordinator(supervisor, exercise, outage).step(
        10,
        object(),
        object(),
        _exercise_decision(),
        OutagePowerDecision(claim_new_outage_session=True),
        grid_ready=False,
    )

    assert supervisor.cycle_owned_marked is True
    assert supervisor.session.cycle_owned is True


def test_manual_session_preempts_unstarted_exercise():
    """Пользовательская managed-session имеет приоритет над ещё не стартовавшим Exercise; Scheduler освобождает ownership без команды остановки двигателя."""
    supervisor = SupervisorStub()
    supervisor.next_session = SimpleNamespace(
        reason=SessionReason.MANUAL_GENERATOR_START,
        generator=GeneratorSlot.A,
    )
    exercise = ExerciseStub(GeneratorSlot.B)
    outage = OutagePolicyStub()

    _coordinator(supervisor, exercise, outage).step(
        10,
        object(),
        object(),
        _exercise_decision(GeneratorSlot.B, desired=True),
        OutagePowerDecision(),
        grid_ready=True,
    )

    assert exercise.cancel_calls == ["начата пользовательская managed-сессия"]
    assert exercise.owned_slot is None


def test_recovery_keeps_exercise_shutdown_ownership():
    """RECOVERY запрещает продолжать Exercise, но автоматически запущенный двигатель не остаётся бесхозным: Scheduler становится явным shutdown-owner."""
    supervisor = SupervisorStub()
    supervisor.next_phase = SupervisorPhase.RECOVERY_REQUIRED
    exercise = ExerciseStub(GeneratorSlot.A)
    outage = OutagePolicyStub()

    result = _coordinator(supervisor, exercise, outage).step(
        10,
        object(),
        object(),
        _exercise_decision(GeneratorSlot.A, desired=True),
        OutagePowerDecision(),
        grid_ready=True,
    )

    assert len(exercise.fail_calls) == 1
    assert result.exercise_shutdown_slot == GeneratorSlot.A


def test_completed_charge_cycle_begins_next_ups_wait_without_core_delay():
    """После штатного RETURNING_TO_UPS и исчезновения session начинается следующий battery interval; повторный grid_failure_delay не требуется."""
    supervisor = SupervisorStub()
    supervisor.phase = SupervisorPhase.RETURNING_TO_UPS
    supervisor.session = SimpleNamespace(
        reason=SessionReason.GRID_OUTAGE,
        generator=GeneratorSlot.A,
    )
    supervisor.next_session = None
    supervisor.next_phase = SupervisorPhase.NORMAL
    exercise = ExerciseStub()
    outage = OutagePolicyStub()

    _coordinator(supervisor, exercise, outage).step(
        123,
        object(),
        object(),
        _exercise_decision(),
        OutagePowerDecision(),
        grid_ready=False,
    )

    assert outage.post_cycle_waits == [123]


def test_outage_policy_request_is_forwarded_to_supervisor_before_step():
    """Target SoC stop не выполняет hardware сам: policy только передаёт запрос Supervisor, который остаётся владельцем силовой последовательности."""
    supervisor = SupervisorStub()
    exercise = ExerciseStub()
    outage = OutagePolicyStub()

    _coordinator(supervisor, exercise, outage).step(
        10,
        object(),
        object(),
        _exercise_decision(),
        OutagePowerDecision(request_cycle_stop=True),
        grid_ready=False,
    )

    assert supervisor.cycle_stop_requested is True
    assert supervisor.step_kwargs is not None

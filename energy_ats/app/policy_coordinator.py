"""Арбитраж high-level policies EnergyATS.

Модуль не управляет Home Assistant и не выполняет аппаратные команды. Его задача —
в одном месте описать пересечения maintenance/outage policies вокруг общей
GeneratorSession и ownership генератора.

Приоритеты intentionally простые:

1. safety / RECOVERY остаётся абсолютным gate внутри EnergySupervisor;
2. ручные команды обрабатываются EnergySupervisor раньше автоматических policy;
3. реальный outage может принять уже работающий Exercise-generator;
4. Exercise используется только пока нет более приоритетной managed-session;
5. Load Manager сюда не входит: это downstream transfer/admission policy, а не
   владелец generator-run.

Так новые policies не должны вызывать друг друга попарно. Их пересечения
добавляются и тестируются здесь как явные ownership transitions.
"""

from __future__ import annotations

from dataclasses import dataclass

from domain import GeneratorSlot, SessionReason, SupervisorEvent
from energy_supervisor import (
    EnergySupervisor,
    SupervisorDecision,
    SupervisorObservation,
    SupervisorPhase,
)
from exercise_scheduler import (
    ExerciseDecision,
    ExerciseObservation,
    ExerciseScheduler,
)
from outage_power_policy import OutagePowerDecision, OutagePowerPolicy


@dataclass(frozen=True)
class PolicyCoordinationResult:
    """Результат одного arbitration pass перед аппаратными контроллерами."""

    supervisor_decision: SupervisorDecision
    events: tuple[SupervisorEvent, ...]
    exercise_shutdown_slot: GeneratorSlot | None


class PolicyCoordinator:
    """Связывает Exercise, OutagePowerPolicy и EnergySupervisor.

    Конкретные policy сохраняют собственные FSM/state. Coordinator содержит
    только правила приоритета и передачи ownership между ними.
    """

    def __init__(
        self,
        supervisor: EnergySupervisor,
        exercise_scheduler: ExerciseScheduler,
        outage_power_policy: OutagePowerPolicy,
    ) -> None:
        self.supervisor = supervisor
        self.exercise_scheduler = exercise_scheduler
        self.outage_power_policy = outage_power_policy

    def step(
        self,
        now: float,
        supervisor_observation: SupervisorObservation,
        exercise_observation: ExerciseObservation,
        exercise_decision: ExerciseDecision,
        outage_decision: OutagePowerDecision,
        *,
        grid_ready: bool | None,
    ) -> PolicyCoordinationResult:
        """Разрешить policy-конфликты и выполнить один шаг Supervisor.

        Важно: сами ExerciseScheduler и OutagePowerPolicy уже были step() до
        этого вызова. Здесь выполняются только меж-policy transitions.
        """

        events = [*exercise_decision.events, *outage_decision.events]
        session_before = self.supervisor.session
        cycle_return_before = (
            self.supervisor.phase == SupervisorPhase.RETURNING_TO_UPS
        )

        if outage_decision.request_cycle_stop:
            self.supervisor.request_cycle_stop()

        supervisor_decision = self.supervisor.step(
            now,
            supervisor_observation,
            exercise_owned_slot=exercise_decision.owned_slot,
            exercise_desired_running=exercise_decision.desired_running,
            defer_automatic_start=outage_decision.defer_automatic_start,
            outage_delay_already_satisfied=(
                outage_decision.outage_delay_already_satisfied
            ),
            restore_grid_after_cycle=outage_decision.restore_grid_after_cycle,
        )

        # Charge Cycling может владеть только новой обычной automatic outage
        # session. Уже работающий Exercise-generator не должен быть захвачен
        # cycling policy до явного Exercise -> Outage handoff.
        if (
            session_before is None
            and self.supervisor.session is not None
            and self.supervisor.session.reason == SessionReason.GRID_OUTAGE
            and outage_decision.claim_new_outage_session
            and exercise_decision.owned_slot is None
        ):
            self.supervisor.mark_session_cycle_owned()

        # После штатного cycle stop следующий battery interval начинается без
        # повторного core grid_failure_delay.
        if (
            cycle_return_before
            and self.supervisor.session is None
            and grid_ready is False
        ):
            self.outage_power_policy.begin_post_cycle_wait(now)

        # Outage имеет приоритет над maintenance. Если Supervisor действительно
        # создал outage-session на том же уже работающем generator, ownership
        # передаётся явно; до этого Scheduler остаётся ответственным за stop.
        if (
            self.exercise_scheduler.owned_slot is not None
            and self.supervisor.session is not None
            and self.supervisor.session.reason == SessionReason.GRID_OUTAGE
            and self.supervisor.session.generator == self.exercise_scheduler.owned_slot
            and grid_ready is False
        ):
            events.extend(
                self.exercise_scheduler.handoff_to_outage(
                    self.supervisor.session.generator,
                    exercise_observation,
                )
            )

        # Manual managed-session сильнее ещё не начавшегося Exercise. Если
        # двигатель уже фактически стартовал, cancel_unstarted() ничего не
        # меняет — Scheduler продолжает владеть им до безопасного решения.
        if (
            self.exercise_scheduler.owned_slot is not None
            and self.supervisor.session is not None
            and self.supervisor.session.reason != SessionReason.GRID_OUTAGE
        ):
            events.extend(
                self.exercise_scheduler.cancel_unstarted(
                    exercise_observation,
                    "начата пользовательская managed-сессия",
                )
            )

        # RECOVERY запрещает обычное продолжение Exercise, но не разрешает
        # потерять автоматически запущенный двигатель. Scheduler переводит
        # attempt в STOPPING и остаётся явным shutdown-owner.
        if (
            self.supervisor.phase == SupervisorPhase.RECOVERY_REQUIRED
            and self.exercise_scheduler.owned_slot is not None
        ):
            events.extend(
                self.exercise_scheduler.fail_active(
                    exercise_observation,
                    "EnergyATS перешёл в RECOVERY_REQUIRED во время пробного запуска.",
                )
            )

        return PolicyCoordinationResult(
            supervisor_decision=supervisor_decision,
            events=tuple((*supervisor_decision.events, *events)),
            exercise_shutdown_slot=self.exercise_scheduler.authorized_shutdown_slot,
        )

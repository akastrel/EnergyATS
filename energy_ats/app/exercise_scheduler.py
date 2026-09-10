"""Плановый пробный запуск генераторов без зависимостей от Home Assistant."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import Enum
from typing import Any, Mapping

from domain import GeneratorSlot, SupervisorEvent

SLOTS = (GeneratorSlot.A, GeneratorSlot.B)
_HISTORY_LIMIT = 50
_WARNING_MIN_LEAD = timedelta(minutes=60)
# Scheduler опрашивается по tick, поэтому warning запрашиваем на минуту раньше:
# даже последний tick этой минуты всё ещё гарантирует не менее 60 минут lead time.
_WARNING_DISPATCH_LEAD = timedelta(minutes=61)


class ExerciseAttemptPhase(str, Enum):
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"


class ExerciseResult(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    DEFERRED = "deferred"
    INTERRUPTED_BY_OUTAGE = "interrupted_by_outage"


@dataclass(frozen=True)
class ExerciseConfig:
    enabled: bool
    interval_days: int
    start_time: str
    run_minutes: int
    presence_grace_days: int

    @property
    def local_start_time(self) -> time:
        try:
            hour_s, minute_s = self.start_time.split(":", 1)
            hour, minute = int(hour_s), int(minute_s)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError(
                f"Некорректное exercise_start_time: {self.start_time!r}"
            ) from exc
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"Некорректное exercise_start_time: {self.start_time!r}")
        return time(hour, minute)

    def validate(self) -> None:
        _ = self.local_start_time
        if self.interval_days < 1:
            raise ValueError("exercise_interval_days должен быть >= 1")
        if self.run_minutes < 1:
            raise ValueError("exercise_run_minutes должен быть >= 1")
        if self.presence_grace_days < 0:
            raise ValueError("exercise_presence_grace_days должен быть >= 0")


@dataclass(frozen=True)
class ExerciseGeneratorObservation:
    running: bool | None
    remote_on: bool | None
    fault: str | None


@dataclass(frozen=True)
class ExerciseObservation:
    now: float
    local_now: datetime
    grid_ready: bool | None
    grid_path_stable: bool
    family_present: bool | None
    emergency_stop: bool | None
    required_states_known: bool
    power_transition_in_progress: bool
    policy_busy: bool
    actions_enabled: bool
    generators: Mapping[GeneratorSlot, ExerciseGeneratorObservation]
    generator_names: Mapping[GeneratorSlot, str]


@dataclass(frozen=True)
class ExerciseWarning:
    slot: GeneratorSlot
    window_date: str
    message: str


@dataclass(frozen=True)
class ExerciseDecision:
    owned_slot: GeneratorSlot | None
    desired_running: bool
    authorized_shutdown_slot: GeneratorSlot | None
    warnings: tuple[ExerciseWarning, ...]
    events: tuple[SupervisorEvent, ...]


@dataclass
class ExerciseSlotState:
    initial_reference_time: str | None = None
    last_qualifying_run: str | None = None
    last_window_date: str | None = None
    warning_sent_for_date: str | None = None
    warning_sent_at: str | None = None
    last_result: str | None = None
    last_failure_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "initial_reference_time": self.initial_reference_time,
            "last_qualifying_run": self.last_qualifying_run,
            "last_window_date": self.last_window_date,
            "warning_sent_for_date": self.warning_sent_for_date,
            "warning_sent_at": self.warning_sent_at,
            "last_result": self.last_result,
            "last_failure_reason": self.last_failure_reason,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ExerciseSlotState":
        result = data.get("last_result")
        if result is not None:
            ExerciseResult(str(result))
        return cls(
            initial_reference_time=_optional_str(data.get("initial_reference_time")),
            last_qualifying_run=_optional_str(data.get("last_qualifying_run")),
            last_window_date=_optional_str(data.get("last_window_date")),
            warning_sent_for_date=_optional_str(data.get("warning_sent_for_date")),
            warning_sent_at=_optional_str(data.get("warning_sent_at")),
            last_result=_optional_str(result),
            last_failure_reason=_optional_str(data.get("last_failure_reason")),
        )


@dataclass
class ExerciseAttempt:
    slot: GeneratorSlot
    scheduled_time: str
    forced: bool
    phase: ExerciseAttemptPhase = ExerciseAttemptPhase.STARTING
    actual_start_time: str | None = None
    started_at: float | None = None
    run_until: float | None = None
    result: ExerciseResult | None = None
    failure_reason: str | None = None
    failure_event_emitted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot": self.slot.value,
            "scheduled_time": self.scheduled_time,
            "forced": self.forced,
            "phase": self.phase.value,
            "actual_start_time": self.actual_start_time,
            "started_at": self.started_at,
            "run_until": self.run_until,
            "result": self.result.value if self.result else None,
            "failure_reason": self.failure_reason,
            "failure_event_emitted": self.failure_event_emitted,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ExerciseAttempt":
        return cls(
            slot=GeneratorSlot(str(data["slot"])),
            scheduled_time=str(data["scheduled_time"]),
            forced=_strict_bool(data["forced"], "exercise.attempt.forced"),
            phase=ExerciseAttemptPhase(str(data["phase"])),
            actual_start_time=_optional_str(data.get("actual_start_time")),
            started_at=_optional_float(data.get("started_at")),
            run_until=_optional_float(data.get("run_until")),
            result=(
                ExerciseResult(str(data["result"]))
                if data.get("result") is not None
                else None
            ),
            failure_reason=_optional_str(data.get("failure_reason")),
            failure_event_emitted=_strict_bool(
                data.get(
                    "failure_event_emitted",
                    data.get("failure_notified", False),
                ),
                "exercise.attempt.failure_event_emitted",
            ),
        )


class ExerciseScheduler:
    """Хранит график/history и ownership одного активного exercise."""

    def __init__(self, configs: Mapping[GeneratorSlot, ExerciseConfig]) -> None:
        self.configs = dict(configs)
        for slot in SLOTS:
            if slot not in self.configs:
                raise ValueError(
                    f"Отсутствует exercise config для Generator {slot.value}"
                )
            self.configs[slot].validate()

        self.states = {slot: ExerciseSlotState() for slot in SLOTS}
        self.active_attempt: ExerciseAttempt | None = None
        self.history: list[dict[str, Any]] = []

        # Эти три поля намеренно не persist-ятся. Для обычного observed run после
        # restart непрерывность доказывается заново. Active exercise имеет свой
        # persisted started_at/run_until.
        self._run_started_at: dict[GeneratorSlot, float | None] = {
            slot: None for slot in SLOTS
        }
        self._run_faulted = {slot: False for slot in SLOTS}
        self._run_qualified = {slot: False for slot in SLOTS}

    @property
    def any_enabled(self) -> bool:
        return any(config.enabled for config in self.configs.values())

    @property
    def owned_slot(self) -> GeneratorSlot | None:
        return self.active_attempt.slot if self.active_attempt else None

    @property
    def authorized_shutdown_slot(self) -> GeneratorSlot | None:
        attempt = self.active_attempt
        if attempt is not None and attempt.phase == ExerciseAttemptPhase.STOPPING:
            return attempt.slot
        return None

    @property
    def internal_test_slots(self) -> frozenset[GeneratorSlot]:
        return (
            frozenset({self.active_attempt.slot})
            if self.active_attempt is not None
            else frozenset()
        )

    def owns(self, slot: GeneratorSlot) -> bool:
        return self.active_attempt is not None and self.active_attempt.slot == slot

    def step(self, o: ExerciseObservation) -> ExerciseDecision:
        events: list[SupervisorEvent] = []
        warnings: list[ExerciseWarning] = []

        self._initialize_references(o.local_now)
        self._track_qualifying_runs(o)

        if self.active_attempt is not None:
            events.extend(self._step_active(o))
        else:
            warnings.extend(self._due_warnings(o))
            events.extend(self._maybe_start(o))

        attempt = self.active_attempt
        desired_running = bool(
            attempt is not None and attempt.phase != ExerciseAttemptPhase.STOPPING
        )
        return ExerciseDecision(
            owned_slot=attempt.slot if attempt else None,
            desired_running=desired_running,
            authorized_shutdown_slot=self.authorized_shutdown_slot,
            warnings=tuple(warnings),
            events=tuple(events),
        )

    def confirm_warning(
        self,
        slot: GeneratorSlot,
        window_date: str,
        generator_name: str,
        sent_at: datetime,
    ) -> SupervisorEvent:
        date.fromisoformat(window_date)
        if sent_at.tzinfo is None:
            raise ValueError("Время отправки exercise-warning должно содержать timezone")
        state = self.states[slot]
        state.warning_sent_for_date = window_date
        state.warning_sent_at = sent_at.isoformat()
        return SupervisorEvent(
            "info",
            f"Предупреждение о пробном запуске {generator_name} "
            "успешно отправлено заранее.",
        )

    def fail_active(
        self,
        o: ExerciseObservation,
        reason: str,
    ) -> tuple[SupervisorEvent, ...]:
        """Завершить собственный auto-run как FAILED, сохранив обязанность stop."""
        return tuple(self._fail_active(o, reason))

    def handoff_to_outage(
        self,
        slot: GeneratorSlot,
        o: ExerciseObservation,
    ) -> tuple[SupervisorEvent, ...]:
        attempt = self.active_attempt
        if attempt is None or attempt.slot != slot:
            return ()

        self._finish_attempt(o, ExerciseResult.INTERRUPTED_BY_OUTAGE, None)
        return (
            SupervisorEvent(
                "warning",
                f"Пробный запуск {o.generator_names[slot]} передан outage-сессии; "
                "дальнейшая работа и остановка принадлежат АВР.",
            ),
        )

    def cancel_unstarted(
        self,
        o: ExerciseObservation,
        reason: str,
    ) -> tuple[SupervisorEvent, ...]:
        attempt = self.active_attempt
        if attempt is None or attempt.started_at is not None:
            return ()
        status = o.generators[attempt.slot]
        if status.running is True or status.remote_on is True:
            return ()

        slot = attempt.slot
        self._record_deferred(
            slot,
            attempt.scheduled_time,
            attempt.forced,
            reason,
        )
        self.active_attempt = None
        return (
            SupervisorEvent(
                "info",
                f"Пробный запуск {o.generator_names[slot]} отложен: {reason}",
            ),
        )

    def status_attributes(
        self,
        local_now: datetime,
        now: float,
    ) -> dict[str, Any]:
        attrs: dict[str, Any] = {}
        for slot in SLOTS:
            prefix = f"generator_{slot.value.lower()}_exercise"
            state = self.states[slot]
            next_due = self._next_due(slot)
            forced_date = self._forced_date(slot)
            attrs.update(
                {
                    f"{prefix}_enabled": self.configs[slot].enabled,
                    f"{prefix}_initial_reference": state.initial_reference_time,
                    f"{prefix}_last_qualifying_run": state.last_qualifying_run,
                    f"{prefix}_next_due": (
                        next_due.isoformat() if next_due else None
                    ),
                    f"{prefix}_overdue": self._is_due(slot, local_now),
                    f"{prefix}_forced_date": (
                        forced_date.isoformat() if forced_date else None
                    ),
                    f"{prefix}_warning_sent_at": state.warning_sent_at,
                    f"{prefix}_active": self.owns(slot),
                    f"{prefix}_run_minutes": self.configs[slot].run_minutes,
                    f"{prefix}_last_result": state.last_result,
                    f"{prefix}_last_failure_reason": state.last_failure_reason,
                }
            )

        attrs["exercise_active_generator_slot"] = (
            self.owned_slot.value if self.owned_slot else None
        )
        attrs["exercise_remaining_seconds"] = self.remaining_seconds(now)
        return attrs

    def remaining_seconds(self, now: float) -> int | None:
        attempt = self.active_attempt
        if (
            attempt is None
            or attempt.phase != ExerciseAttemptPhase.RUNNING
            or attempt.run_until is None
        ):
            return None
        return max(0, int((attempt.run_until - now) + 0.999999))

    def to_dict(self) -> dict[str, Any]:
        return {
            "slots": {
                slot.value: self.states[slot].to_dict()
                for slot in SLOTS
            },
            "active_attempt": (
                self.active_attempt.to_dict() if self.active_attempt else None
            ),
            "history": list(self.history[-_HISTORY_LIMIT:]),
        }

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
        configs: Mapping[GeneratorSlot, ExerciseConfig],
    ) -> "ExerciseScheduler":
        scheduler = cls(configs)
        slots = data.get("slots")
        if not isinstance(slots, Mapping):
            raise ValueError("Некорректное состояние exercise_scheduler.slots")

        scheduler.states = {
            slot: ExerciseSlotState.from_dict(
                _mapping(slots.get(slot.value), f"exercise slot {slot.value}")
            )
            for slot in SLOTS
        }

        active = data.get("active_attempt")
        scheduler.active_attempt = (
            ExerciseAttempt.from_dict(active)
            if isinstance(active, Mapping)
            else None
        )

        history = data.get("history", [])
        if not isinstance(history, list) or not all(
            isinstance(item, dict) for item in history
        ):
            raise ValueError("Некорректное состояние exercise_scheduler.history")
        scheduler.history = list(history[-_HISTORY_LIMIT:])
        return scheduler

    # Internal state -------------------------------------------------

    def _initialize_references(self, local_now: datetime) -> None:
        value = local_now.isoformat()
        for slot in SLOTS:
            if self.states[slot].initial_reference_time is None:
                self.states[slot].initial_reference_time = value

    def _track_qualifying_runs(self, o: ExerciseObservation) -> None:
        for slot in SLOTS:
            status = o.generators[slot]
            if status.running is not True:
                self._run_started_at[slot] = None
                self._run_faulted[slot] = False
                self._run_qualified[slot] = False
                continue

            if self._run_started_at[slot] is None:
                attempt = self.active_attempt
                self._run_started_at[slot] = (
                    attempt.started_at
                    if attempt is not None
                    and attempt.slot == slot
                    and attempt.started_at is not None
                    else o.now
                )

            if status.fault is not None:
                self._run_faulted[slot] = True

            if self._run_qualified[slot] or self._run_faulted[slot]:
                continue

            started_at = self._run_started_at[slot]
            assert started_at is not None
            required = self.configs[slot].run_minutes * 60
            if o.now - started_at >= required:
                self.states[slot].last_qualifying_run = o.local_now.isoformat()
                self._run_qualified[slot] = True

    def _step_active(self, o: ExerciseObservation) -> list[SupervisorEvent]:
        assert self.active_attempt is not None
        attempt = self.active_attempt
        status = o.generators[attempt.slot]
        events: list[SupervisorEvent] = []

        if status.fault is not None:
            events.extend(self._fail_active(o, status.fault))
            attempt = self.active_attempt
            if attempt is None:
                return events

        if attempt.phase == ExerciseAttemptPhase.STARTING:
            if status.running is True and status.remote_on is True:
                attempt.phase = ExerciseAttemptPhase.RUNNING
                attempt.started_at = o.now
                attempt.actual_start_time = o.local_now.isoformat()
                attempt.run_until = (
                    o.now + self.configs[attempt.slot].run_minutes * 60
                )
                events.append(
                    SupervisorEvent(
                        "info",
                        f"Пробный запуск {o.generator_names[attempt.slot]} подтверждён; "
                        f"контрольная работа "
                        f"{self.configs[attempt.slot].run_minutes} мин.",
                    )
                )
            return events

        if attempt.phase == ExerciseAttemptPhase.RUNNING:
            if status.running is not True or status.remote_on is not True:
                return events + self._fail_active(
                    o,
                    f"{o.generator_names[attempt.slot]} неожиданно потерял "
                    "RUNNING/REMOTE до завершения пробного запуска.",
                )
            if attempt.run_until is not None and o.now >= attempt.run_until:
                attempt.phase = ExerciseAttemptPhase.STOPPING
                events.append(
                    SupervisorEvent(
                        "info",
                        f"Пробный запуск {o.generator_names[attempt.slot]} выдержал "
                        "требуемое время; начинаем штатную остановку.",
                    )
                )
            return events

        if attempt.phase == ExerciseAttemptPhase.STOPPING:
            if status.running is False and status.remote_on is False:
                result = attempt.result or ExerciseResult.SUCCESS
                reason = attempt.failure_reason
                slot = attempt.slot
                self._finish_attempt(o, result, reason)
                if result == ExerciseResult.SUCCESS:
                    events.append(
                        SupervisorEvent(
                            "info",
                            f"Пробный запуск {o.generator_names[slot]} "
                            "успешно завершён.",
                        )
                    )
            return events

        return events

    def _fail_active(
        self,
        o: ExerciseObservation,
        reason: str,
    ) -> list[SupervisorEvent]:
        attempt = self.active_attempt
        if attempt is None:
            return []

        if attempt.failure_reason is None:
            attempt.failure_reason = reason
            attempt.result = ExerciseResult.FAILED
            attempt.phase = ExerciseAttemptPhase.STOPPING

        if attempt.failure_event_emitted:
            return []
        attempt.failure_event_emitted = True
        return [
            SupervisorEvent(
                "critical",
                f"Пробный запуск генератора "
                f"{o.generator_names[attempt.slot]} завершился ошибкой: {reason}",
            )
        ]

    def _finish_attempt(
        self,
        o: ExerciseObservation,
        result: ExerciseResult,
        reason: str | None,
    ) -> None:
        assert self.active_attempt is not None
        attempt = self.active_attempt
        actual_run_seconds = 0
        if attempt.started_at is not None:
            actual_run_seconds = max(0, int(o.now - attempt.started_at))

        self.history.append(
            {
                "generator": attempt.slot.value,
                "scheduled_time": attempt.scheduled_time,
                "actual_start_time": attempt.actual_start_time,
                "actual_end_time": o.local_now.isoformat(),
                "result": result.value,
                "required_run_minutes": self.configs[attempt.slot].run_minutes,
                "actual_run_seconds": actual_run_seconds,
                "forced_after_presence_grace": attempt.forced,
                "failure_reason": reason,
            }
        )
        self.history = self.history[-_HISTORY_LIMIT:]

        state = self.states[attempt.slot]
        state.last_result = result.value
        state.last_failure_reason = (
            reason if result == ExerciseResult.FAILED else None
        )
        self.active_attempt = None

    # Scheduling -----------------------------------------------------

    def _due_warnings(self, o: ExerciseObservation) -> list[ExerciseWarning]:
        warnings: list[ExerciseWarning] = []
        for slot in SLOTS:
            config = self.configs[slot]
            if not config.enabled or not self._is_due(slot, o.local_now):
                continue

            # Для старта около полуночи warning может относиться к окну завтра.
            for window_date in (
                o.local_now.date(),
                o.local_now.date() + timedelta(days=1),
            ):
                forced_date = self._forced_date(slot)
                if forced_date is None or window_date < forced_date:
                    continue

                window = datetime.combine(
                    window_date,
                    config.local_start_time,
                    tzinfo=o.local_now.tzinfo,
                )
                warning_at = window - _WARNING_DISPATCH_LEAD
                if not _same_minute(o.local_now, warning_at):
                    continue
                if (
                    self.states[slot].warning_sent_for_date
                    == window_date.isoformat()
                ):
                    continue

                day_word = (
                    "сегодня"
                    if window_date == o.local_now.date()
                    else "завтра"
                )
                warnings.append(
                    ExerciseWarning(
                        slot=slot,
                        window_date=window_date.isoformat(),
                        message=(
                            f"Пробный запуск генератора {o.generator_names[slot]} "
                            f"состоится {day_word} в {config.start_time}. "
                            "Это регулярный тестовый пуск для проверки "
                            "работоспособности генератора. После автоматического "
                            f"запуска генератор будет автоматически остановлен через "
                            f"{config.run_minutes} минут."
                        ),
                    )
                )
        return warnings

    def _maybe_start(self, o: ExerciseObservation) -> list[SupervisorEvent]:
        events: list[SupervisorEvent] = []
        for slot in SLOTS:
            config = self.configs[slot]
            if not config.enabled or not self._is_due(slot, o.local_now):
                continue
            if not _at_start_window(o.local_now, config.local_start_time):
                continue

            state = self.states[slot]
            today = o.local_now.date().isoformat()
            if state.last_window_date == today:
                continue
            state.last_window_date = today

            scheduled = datetime.combine(
                o.local_now.date(),
                config.local_start_time,
                tzinfo=o.local_now.tzinfo,
            )
            forced_date = self._forced_date(slot)
            forced = (
                forced_date is not None
                and o.local_now.date() >= forced_date
            )

            reason = self._start_blocker(slot, o, forced, scheduled)
            if reason is not None:
                self._record_deferred(
                    slot,
                    scheduled.isoformat(),
                    forced,
                    reason,
                )
                events.append(
                    SupervisorEvent(
                        "info",
                        f"Пробный запуск {o.generator_names[slot]} "
                        f"отложен: {reason}",
                    )
                )
                continue

            self.active_attempt = ExerciseAttempt(
                slot=slot,
                scheduled_time=scheduled.isoformat(),
                forced=forced,
            )
            events.append(
                SupervisorEvent(
                    "info",
                    f"Начат плановый пробный запуск генератора "
                    f"{o.generator_names[slot]}.",
                )
            )
            # Цикл продолжается, чтобы второй slot с тем же окном получил
            # DEFERRED вместо молчаливого пропуска.

        return events

    def _start_blocker(
        self,
        slot: GeneratorSlot,
        o: ExerciseObservation,
        forced: bool,
        scheduled: datetime,
    ) -> str | None:
        if not o.actions_enabled:
            return "EnergyATS находится в DISARMED режиме"
        if not o.required_states_known:
            return "неизвестны обязательные физические состояния"
        if o.emergency_stop is not False:
            return "активен или неизвестен Generators Emergency Stop"
        if o.grid_ready is not True or not o.grid_path_stable:
            return "нет подтверждённой штатной Grid"
        if o.power_transition_in_progress or o.policy_busy:
            return "EnergyATS выполняет другую операцию"
        if self.active_attempt is not None and self.active_attempt.slot != slot:
            return "в это же окно уже запущен exercise другого генератора"

        target = o.generators[slot]
        if target.running is not False or target.remote_on is not False:
            return "тестируемый генератор уже RUNNING или REMOTE ON"

        if forced:
            state = self.states[slot]
            if state.warning_sent_for_date != scheduled.date().isoformat():
                return "не было подтверждённого предупреждения за 60 минут"
            if state.warning_sent_at is None:
                return "неизвестно фактическое время предупреждения"
            try:
                warning_sent_at = datetime.fromisoformat(state.warning_sent_at)
            except ValueError:
                return "некорректно сохранено время предупреждения"
            if warning_sent_at.tzinfo is None:
                return "время предупреждения не содержит timezone"
            if warning_sent_at > scheduled - _WARNING_MIN_LEAD:
                return "предупреждение было отправлено менее чем за 60 минут"
        elif o.family_present is not False:
            return "отсутствие семьи дома не подтверждено"

        return None

    def _record_deferred(
        self,
        slot: GeneratorSlot,
        scheduled: str,
        forced: bool,
        reason: str,
    ) -> None:
        self.history.append(
            {
                "generator": slot.value,
                "scheduled_time": scheduled,
                "actual_start_time": None,
                "actual_end_time": None,
                "result": ExerciseResult.DEFERRED.value,
                "required_run_minutes": self.configs[slot].run_minutes,
                "actual_run_seconds": 0,
                "forced_after_presence_grace": forced,
                "failure_reason": reason,
            }
        )
        self.history = self.history[-_HISTORY_LIMIT:]

    def _reference(self, slot: GeneratorSlot) -> datetime | None:
        state = self.states[slot]
        value = state.last_qualifying_run or state.initial_reference_time
        return datetime.fromisoformat(value) if value else None

    def _next_due(self, slot: GeneratorSlot) -> datetime | None:
        reference = self._reference(slot)
        if reference is None:
            return None
        return reference + timedelta(days=self.configs[slot].interval_days)

    def _is_due(self, slot: GeneratorSlot, local_now: datetime) -> bool:
        due = self._next_due(slot)
        return due is not None and local_now >= due

    def _forced_date(self, slot: GeneratorSlot) -> date | None:
        due = self._next_due(slot)
        if due is None:
            return None
        return due.date() + timedelta(
            days=self.configs[slot].presence_grace_days
        )


def _same_minute(left: datetime, right: datetime) -> bool:
    return left.replace(second=0, microsecond=0) == right.replace(
        second=0,
        microsecond=0,
    )


def _at_start_window(local_now: datetime, start: time) -> bool:
    return local_now.hour == start.hour and local_now.minute == start.minute


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _strict_bool(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{name} должен быть boolean")
    return value


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"Некорректное состояние {name}")
    return value

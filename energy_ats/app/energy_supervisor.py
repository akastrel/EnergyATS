"""Policy EnergyATS поверх GC, TPC и наблюдаемой генераторной шины."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from domain import GeneratorSlot, PowerPath, PowerSource, SessionReason, SupervisorEvent
from generator_bus import GeneratorBusOwner, GeneratorBusStatus
from generator_controller import GeneratorPhase, GeneratorStatus
from power_transfer import PowerTransferStatus


class SupervisorPhase(str, Enum):
    WAITING_FOR_DATA = "waiting_for_data"
    NORMAL = "normal"
    GRID_FAILURE_DELAY = "grid_failure_delay"
    STARTING_GENERATOR = "starting_generator"
    ON_GENERATOR = "on_generator"
    RETURNING_TO_GRID = "returning_to_grid"
    RETURNING_TO_UPS = "returning_to_ups"
    EXTERNAL_RUNNING = "external_running"
    RECOVERY_REQUIRED = "recovery_required"


class SessionControlMode(str, Enum):
    """Кто/что сейчас определяет завершение managed-session.

    Раньше это кодировалось двумя независимыми boolean `cycle_owned` и
    `manual_override`, что допускало противоречивую комбинацию True/True.
    Одно enum-state делает ownership явным и исключает невозможные состояния.
    """

    STANDARD = "standard"
    CHARGE_CYCLE = "charge_cycle"
    MANUAL_OVERRIDE = "manual_override"
    MANUAL_STOP = "manual_stop"


_TRANSIENT_PHASES = {
    SupervisorPhase.STARTING_GENERATOR,
    SupervisorPhase.RETURNING_TO_GRID,
    SupervisorPhase.RETURNING_TO_UPS,
}


@dataclass(frozen=True)
class SupervisorConfig:
    grid_failure_delay: float = 5.0
    grid_restore_stable_time: float = 60.0
    primary_generator: GeneratorSlot = GeneratorSlot.A
    generator_a_enabled: bool = True
    generator_b_enabled: bool = True

    def generator_enabled(self, slot: GeneratorSlot) -> bool:
        return self.generator_a_enabled if slot == GeneratorSlot.A else self.generator_b_enabled


@dataclass
class GeneratorSession:
    reason: SessionReason
    generator: GeneratorSlot
    grid_was_unavailable: bool
    stop_requested: bool = False
    fallback_used: bool = False
    external_takeover_observed: bool = False
    control_mode: SessionControlMode = SessionControlMode.STANDARD

    @property
    def cycle_owned(self) -> bool:
        """Compatibility/readability view: session принадлежит Charge Cycling."""
        return self.control_mode == SessionControlMode.CHARGE_CYCLE

    @property
    def manual_override(self) -> bool:
        """Compatibility/readability view для прежнего status contract."""
        return self.control_mode == SessionControlMode.MANUAL_OVERRIDE

    @classmethod
    def begin(
        cls,
        reason: SessionReason,
        generator: GeneratorSlot,
        grid_was_unavailable: bool,
    ) -> "GeneratorSession":
        return cls(reason, generator, grid_was_unavailable)

    def to_dict(self) -> dict[str, object]:
        return {
            "reason": self.reason.value,
            "generator": self.generator.value,
            "grid_was_unavailable": self.grid_was_unavailable,
            "stop_requested": self.stop_requested,
            "fallback_used": self.fallback_used,
            "external_takeover_observed": self.external_takeover_observed,
            "control_mode": self.control_mode.value,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "GeneratorSession":
        reason = SessionReason(str(data["reason"]))
        stop_requested = _strict_bool(
            data.get("stop_requested", False), "session.stop_requested"
        )

        # 0.7.0 persisted two booleans. PR20 keeps one narrow migration path so
        # an update during an active outage-session remains restart-safe.
        if data.get("control_mode") is not None:
            control_mode = SessionControlMode(str(data["control_mode"]))
        else:
            cycle_owned = _strict_bool(
                data.get("cycle_owned", False), "session.cycle_owned"
            )
            manual_override = _strict_bool(
                data.get("manual_override", False), "session.manual_override"
            )
            if cycle_owned and manual_override:
                raise ValueError(
                    "session не может одновременно принадлежать Charge Cycling "
                    "и manual override"
                )
            if manual_override:
                control_mode = SessionControlMode.MANUAL_OVERRIDE
            elif cycle_owned:
                control_mode = SessionControlMode.CHARGE_CYCLE
            elif reason == SessionReason.GRID_OUTAGE and stop_requested:
                control_mode = SessionControlMode.MANUAL_STOP
            else:
                control_mode = SessionControlMode.STANDARD

        if reason != SessionReason.GRID_OUTAGE and control_mode != SessionControlMode.STANDARD:
            raise ValueError(
                "Специальный session control mode допустим только для GRID_OUTAGE"
            )

        return cls(
            reason,
            GeneratorSlot(str(data["generator"])),
            _strict_bool(data["grid_was_unavailable"], "session.grid_was_unavailable"),
            stop_requested,
            _strict_bool(data.get("fallback_used", False), "session.fallback_used"),
            _strict_bool(
                data.get("external_takeover_observed", False),
                "session.external_takeover_observed",
            ),
            control_mode,
        )


@dataclass(frozen=True)
class SupervisorObservation:
    grid_ready: bool | None
    automatic_transfer_enabled: bool
    emergency_stop: bool | None
    power: PowerTransferStatus
    generators: Mapping[GeneratorSlot, GeneratorStatus]
    power_inputs_known: bool = True
    bus: GeneratorBusStatus | None = None

    @property
    def required_states_known(self) -> bool:
        return (
            self.grid_ready is not None
            and self.emergency_stop is not None
            and self.power_inputs_known
            and all(
                status.running is not None and status.remote_on is not None
                for status in self.generators.values()
            )
        )


@dataclass(frozen=True)
class SupervisorDecision:
    desired_source: PowerSource | None
    desired_generators: Mapping[GeneratorSlot, bool]
    actions_allowed: bool
    stable_managed_generator: GeneratorSlot | None
    stop_outage_generators: frozenset[GeneratorSlot]
    events: tuple[SupervisorEvent, ...]


class EnergySupervisor:
    """Хранит только policy-state; физические переходы выполняют GC/TPC."""

    def __init__(self, config: SupervisorConfig | None = None) -> None:
        self.config = config or SupervisorConfig()
        self.phase = SupervisorPhase.WAITING_FOR_DATA
        self.session: GeneratorSession | None = None
        self.desired_source: PowerSource | None = None
        self.desired_generators = _stopped_generators()
        self.grid_failed_since: float | None = None
        self.grid_ready_since: float | None = None
        self.automatic_start_suppressed_until_grid = False
        self.recovery_reason: str | None = None
        self.initialized = False

        self._manual_start_requested = False
        self._manual_stop_requested = False
        self._cycle_stop_requested = False
        self._recovery_reset_requested = False
        self._recovery_reset_active = False
        self._events: list[SupervisorEvent] = []
        self._stop_outage_generators: set[GeneratorSlot] = set()

    # Requests / recovery --------------------------------------------

    def request_manual_start(self) -> None:
        self._manual_start_requested = True

    def request_manual_stop(self) -> None:
        self._manual_stop_requested = True

    def request_cycle_stop(self) -> None:
        """Завершить только принадлежащую Charge Cycling outage-session."""
        self._cycle_stop_requested = True

    def mark_session_cycle_owned(self) -> bool:
        """Передать новую automatic outage-session под управление cycling policy."""
        if (
            self.session is None
            or self.session.reason != SessionReason.GRID_OUTAGE
            or self.session.control_mode != SessionControlMode.STANDARD
        ):
            return False
        self.session.control_mode = SessionControlMode.CHARGE_CYCLE
        return True

    def request_recovery_reset(self) -> None:
        self._recovery_reset_requested = True

    @property
    def has_pending_session_request(self) -> bool:
        """Maintenance не должна обгонять пользовательскую команду."""
        return self._manual_start_requested or self._manual_stop_requested

    @property
    def manual_start_pending(self) -> bool:
        return self._manual_start_requested

    def consume_recovery_reset_request(self) -> bool:
        requested = self._recovery_reset_requested
        self._recovery_reset_requested = False
        return requested

    @property
    def recovery_reset_in_progress(self) -> bool:
        return self._recovery_reset_active

    def begin_recovery_reset(self) -> None:
        self._recovery_reset_active = True
        self.desired_source = PowerSource.GRID
        self.desired_generators = _stopped_generators()
        self._event("info", "Начато безопасное восстановление Energy ATS.")

    def reject_recovery_reset(self, message: str) -> None:
        self._event("warning", message)

    def report_recovery_reset_not_needed(self) -> None:
        self._event("info", "Сброс не требуется: аварийная блокировка не активна.")

    def fail_recovery_reset(self, reason: str) -> None:
        self._recovery_reset_active = False
        self.recovery_reason = reason
        self._event("warning", f"Восстановление не завершено: {reason}")

    def complete_recovery_reset(self) -> None:
        self._recovery_reset_active = False
        self.phase = SupervisorPhase.NORMAL
        self.session = None
        self.recovery_reason = None
        self.desired_source = None
        self.desired_generators = _stopped_generators()
        self.grid_failed_since = None
        self.grid_ready_since = None
        self._cycle_stop_requested = False
        self._stop_outage_generators.clear()
        self._event("info", "Восстановление завершено; управление снова разрешено.")

    def require_recovery(self, reason: str) -> None:
        self._require_recovery(reason)

    def mark_connection_lost(self) -> None:
        if self.phase in _TRANSIENT_PHASES:
            self._require_recovery(
                "Связь потеряна во время незавершённой физической операции."
            )

    def manages_stable_generator(self, slot: GeneratorSlot) -> bool:
        return (
            self.phase == SupervisorPhase.ON_GENERATOR
            and self.session is not None
            and self.session.generator == slot
        )

    def take_events(self) -> tuple[SupervisorEvent, ...]:
        events = tuple(self._events)
        self._events.clear()
        return events

    # Main policy -----------------------------------------------------

    def step(
        self,
        now: float,
        o: SupervisorObservation,
        *,
        exercise_owned_slot: GeneratorSlot | None = None,
        exercise_desired_running: bool = False,
        defer_automatic_start: bool = False,
        outage_delay_already_satisfied: bool = False,
        restore_grid_after_cycle: bool = False,
    ) -> SupervisorDecision:
        self._stop_outage_generators.clear()
        if not self.initialized:
            self._initialize(o)
            self.initialized = True
        self._update_grid_timer(now, o.grid_ready)

        if not o.required_states_known:
            self._discard_requests()
            return self._decision(o, exercise_owned_slot, exercise_desired_running)
        if self.phase == SupervisorPhase.RECOVERY_REQUIRED:
            self._discard_requests()
            return self._decision(o, exercise_owned_slot, False)
        if o.emergency_stop is True:
            self._discard_requests()
            self._require_recovery("Активен Generators Emergency Stop.")
            return self._decision(o, exercise_owned_slot, False)
        if o.power.recovery_required:
            self._discard_requests()
            self._require_recovery(
                o.power.fault or "Power Transfer требует восстановления."
            )
            return self._decision(o, exercise_owned_slot, False)

        if self._manual_start_requested:
            self._manual_start_requested = False
            self._manual_start(o)
        if self._manual_stop_requested:
            self._manual_stop_requested = False
            self._manual_stop(o)
        if self._cycle_stop_requested:
            self._cycle_stop_requested = False
            self._cycle_stop(o)

        if self.session is None:
            self._without_session(
                now,
                o,
                exercise_owned_slot,
                defer_automatic_start=defer_automatic_start,
                outage_delay_already_satisfied=outage_delay_already_satisfied,
                restore_grid_after_cycle=restore_grid_after_cycle,
            )
        else:
            self._with_session(now, o)
        return self._decision(o, exercise_owned_slot, exercise_desired_running)

    def _initialize(self, o: SupervisorObservation) -> None:
        if self.phase == SupervisorPhase.RECOVERY_REQUIRED:
            return
        if o.power.recovery_required:
            self._require_recovery(o.power.fault or "Неоднозначная силовая топология.")
            return

        if self.session is None:
            if self.phase in _TRANSIENT_PHASES:
                self._require_recovery(
                    "После restart обнаружена незавершённая операция без managed-сессии."
                )
            else:
                self._finish_session()
            return

        if self.phase != SupervisorPhase.ON_GENERATOR or not self._restored_session_matches(o):
            self._require_recovery(
                "После restart сохранённая managed-сессия не совпадает "
                "с устойчивой физической схемой."
            )
            return

        self.desired_source = PowerSource.GENERATOR
        self.desired_generators = _stopped_generators()
        if self._owner(o) == self.session.generator:
            self.desired_generators[self.session.generator] = True

    def _without_session(
        self,
        now: float,
        o: SupervisorObservation,
        exercise_owned_slot: GeneratorSlot | None,
        *,
        defer_automatic_start: bool,
        outage_delay_already_satisfied: bool,
        restore_grid_after_cycle: bool,
    ) -> None:
        self.desired_generators = _stopped_generators()

        if restore_grid_after_cycle and self._grid_stable(now):
            self.desired_source = PowerSource.GRID
            self.phase = SupervisorPhase.RETURNING_TO_GRID
            return

        outage_slots = self._outage_slots(o)
        if outage_slots and self._grid_stable(now):
            self.desired_source = PowerSource.GRID
            self.phase = SupervisorPhase.RETURNING_TO_GRID
            if self._grid_path(o):
                self._stop_outage_generators.update(outage_slots)
                if self._all_stopped(o, outage_slots):
                    self._finish_session()
            return

        self.desired_source = None
        if o.grid_ready is True:
            self.automatic_start_suppressed_until_grid = False
            self.phase = SupervisorPhase.NORMAL
            return
        if self.automatic_start_suppressed_until_grid:
            self.phase = SupervisorPhase.NORMAL
            return
        if o.grid_ready is not False or not o.automatic_transfer_enabled:
            self.phase = SupervisorPhase.NORMAL
            return

        active = self._active_slots(o)
        foreign_active = tuple(
            slot for slot in active if slot != exercise_owned_slot
        )
        if foreign_active:
            self.phase = SupervisorPhase.EXTERNAL_RUNNING
            return

        delay_elapsed = outage_delay_already_satisfied
        if not delay_elapsed:
            if self.grid_failed_since is None:
                self.grid_failed_since = now
                self.phase = SupervisorPhase.GRID_FAILURE_DELAY
                return
            delay_elapsed = now - self.grid_failed_since >= self.config.grid_failure_delay

        if not delay_elapsed:
            self.phase = SupervisorPhase.GRID_FAILURE_DELAY
            return

        # Уже запущенный Scheduler-ом generator можно явно принять в outage
        # независимо от delayed-start policy: дополнительного пуска здесь нет.
        if exercise_owned_slot is not None:
            exercise_status = o.generators[exercise_owned_slot]
            if (
                exercise_status.running is True
                and exercise_status.remote_on is True
                and not _generator_failed(exercise_status)
                and self.config.generator_enabled(exercise_owned_slot)
            ):
                self._begin_session_for_slot(
                    o,
                    SessionReason.GRID_OUTAGE,
                    exercise_owned_slot,
                    allow_active=True,
                )
            else:
                self.phase = SupervisorPhase.GRID_FAILURE_DELAY
            return

        if defer_automatic_start:
            self.phase = SupervisorPhase.GRID_FAILURE_DELAY
            return

        self._begin_session(o, SessionReason.GRID_OUTAGE)

    def _with_session(self, now: float, o: SupervisorObservation) -> None:
        assert self.session is not None

        if self.phase == SupervisorPhase.RETURNING_TO_GRID:
            self._returning(o)
            return

        if self.phase == SupervisorPhase.RETURNING_TO_UPS:
            if self._grid_stable(now):
                self.session.stop_requested = False
                self.phase = SupervisorPhase.RETURNING_TO_GRID
                self.desired_source = PowerSource.GRID
                return
            self._returning_to_ups(o)
            return

        if (
            self.session.grid_was_unavailable
            and not self.session.stop_requested
            and self._grid_stable(now)
        ):
            self.phase = SupervisorPhase.RETURNING_TO_GRID
            self.desired_source = PowerSource.GRID
            return

        if self.phase == SupervisorPhase.STARTING_GENERATOR:
            self._starting(o)
        elif self.phase == SupervisorPhase.ON_GENERATOR:
            self._on_generator(o)
        else:
            self._require_recovery(
                f"Неподдерживаемая фаза managed-сессии: {self.phase.value}."
            )

    def _starting(self, o: SupervisorObservation) -> None:
        assert self.session is not None
        slot = self.session.generator
        generator = o.generators[slot]
        self.desired_generators[slot] = True
        self.desired_source = (
            PowerSource.UPS_ONLY
            if self.session.grid_was_unavailable
            else PowerSource.GRID
            if o.power.actual_source == PowerSource.GRID
            else PowerSource.UPS_ONLY
        )

        if _generator_failed(generator):
            self._managed_failure(o, generator.fault or "Ошибка запуска генератора.")
            return
        if not generator.ready_for_load:
            return

        self.desired_source = PowerSource.GENERATOR
        if o.power.transition_in_progress or o.power.actual_path != PowerPath.GENERATOR:
            return

        owner = self._owner(o)
        if owner is None:
            self._require_recovery(
                "Дом подключён к генераторной шине, но её physical owner неизвестен."
            )
            return

        self.phase = SupervisorPhase.ON_GENERATOR
        if owner == slot:
            self._event(
                "warning",
                f"Дом переведён на резервное питание от {generator.display_name}.",
            )
        else:
            self.session.external_takeover_observed = True
            self.desired_generators[slot] = generator.running is True
            self._event(
                "warning",
                f"Генераторную шину удерживает внешний "
                f"{o.generators[owner].display_name}; EnergyATS не принимает его под управление.",
            )

    def _on_generator(self, o: SupervisorObservation) -> None:
        assert self.session is not None
        slot = self.session.generator
        managed = o.generators[slot]
        owner = self._owner(o)
        self.desired_source = PowerSource.GENERATOR

        if owner == slot:
            self.desired_generators[slot] = True
            if managed.ready_for_load and not _generator_failed(managed):
                return
            self._managed_failure(
                o,
                managed.fault
                or f"{managed.display_name} неожиданно остановился/потерял готовность.",
            )
            return

        if owner is not None and o.generators[owner].running is True:
            self.session.external_takeover_observed = True
            self.desired_generators[slot] = (
                managed.running is True and managed.remote_on is True
            )
            return

        if o.bus is not None and o.bus.owner == GeneratorBusOwner.UNKNOWN:
            self._require_recovery("Владелец работающей генераторной шины неизвестен.")
            return

        if self.session.external_takeover_observed:
            self._require_recovery(
                "Внешний генератор, принявший генераторную шину после отказа "
                "управляемого генератора, остановился. Автоматический повторный "
                "запуск запрещён."
            )
            return

        self._managed_failure(o, "Потерян генераторный источник.")

    def _managed_failure(self, o: SupervisorObservation, reason: str) -> None:
        assert self.session is not None
        failed = self.session.generator
        other = _other_slot(failed)
        other_status = o.generators[other]
        self.desired_generators[failed] = False

        if self.session.fallback_used:
            self._require_recovery(f"Отказ SECONDARY после fallback: {reason}")
            return

        if other_status.running is True or other_status.remote_on is True:
            self.session.external_takeover_observed = True
            self.desired_source = PowerSource.GENERATOR
            self.phase = SupervisorPhase.ON_GENERATOR
            self._event(
                "critical",
                f"Отказ {o.generators[failed].display_name}: {reason} "
                f"Работающий {other_status.display_name} остаётся внешним.",
            )
            return

        if not self.config.generator_enabled(other) or _generator_failed(other_status):
            self._require_recovery(
                f"Отказ {o.generators[failed].display_name}; SECONDARY недоступен."
            )
            return

        self.session.generator = other
        self.session.fallback_used = True
        self.desired_source = PowerSource.UPS_ONLY
        self.desired_generators[other] = True
        self.phase = SupervisorPhase.STARTING_GENERATOR
        self._event(
            "critical",
            f"Отказ {o.generators[failed].display_name}: {reason} "
            f"Выполняется единственный fallback на {other_status.display_name}.",
        )

    def _returning(self, o: SupervisorObservation) -> None:
        assert self.session is not None
        self.desired_source = PowerSource.GRID

        if (
            self.session.grid_was_unavailable
            and not self.session.stop_requested
            and o.grid_ready is False
        ):
            self._cancel_return(o)
            return
        if not self._grid_path(o):
            return

        self.desired_generators = _stopped_generators()
        outage_slots = (
            self._outage_slots(o)
            if self.session.grid_was_unavailable and not self.session.stop_requested
            else frozenset()
        )
        self._stop_outage_generators.update(outage_slots)

        managed = o.generators[self.session.generator]
        if (
            managed.running is False
            and managed.remote_on is False
            and self._all_stopped(o, outage_slots)
        ):
            self._finish_session()

    def _returning_to_ups(self, o: SupervisorObservation) -> None:
        """Cycle stop: сначала снять дом с generator bus, потом остановить engine."""
        assert self.session is not None
        slot = self.session.generator
        self.desired_source = PowerSource.UPS_ONLY
        managed = o.generators[slot]
        if _generator_failed(managed):
            self._require_recovery(
                managed.fault or "Ошибка генератора при завершении charge cycle."
            )
            return

        if o.power.transition_in_progress or o.power.actual_path != PowerPath.ISOLATED:
            self.desired_generators[slot] = True
            return

        self.desired_generators[slot] = False
        if managed.running is False and managed.remote_on is False:
            self._event(
                "info",
                "Цикл подзаряда завершён; дом остаётся на UPS до следующей необходимости запуска.",
            )
            self._finish_session(preserve_grid_failure_timer=True)

    def _cancel_return(self, o: SupervisorObservation) -> None:
        assert self.session is not None
        owner = self._owner(o)
        if owner is not None and o.generators[owner].running is True:
            self.desired_source = PowerSource.GENERATOR
            self.desired_generators[self.session.generator] = (
                owner == self.session.generator
            )
            self.phase = SupervisorPhase.ON_GENERATOR
            self._event(
                "warning",
                "Grid снова пропала во время возврата; "
                "сохраняем доступный генераторный источник.",
            )
            return

        managed = o.generators[self.session.generator]
        self.desired_generators[self.session.generator] = True
        self.desired_source = (
            PowerSource.GENERATOR if managed.ready_for_load else PowerSource.UPS_ONLY
        )
        self.phase = (
            SupervisorPhase.ON_GENERATOR
            if managed.ready_for_load
            else SupervisorPhase.STARTING_GENERATOR
        )

    # Session commands ------------------------------------------------

    def _manual_start(self, o: SupervisorObservation) -> None:
        if self.session is not None:
            if self.session.reason == SessionReason.GRID_OUTAGE:
                self.session.control_mode = SessionControlMode.MANUAL_OVERRIDE
                self.session.stop_requested = False
                if self.phase == SupervisorPhase.RETURNING_TO_UPS:
                    # Двигатель мог уже штатно остановиться. Возобновление
                    # проходит через обычный startup, а не через failure/fallback.
                    self.phase = SupervisorPhase.STARTING_GENERATOR
                    self.desired_source = PowerSource.UPS_ONLY
                    self.desired_generators[self.session.generator] = True
                self._event(
                    "info",
                    "Ручная команда приняла активную outage-сессию под управление пользователя; "
                    "автоматическая остановка по Target SoC отменена.",
                )
            else:
                self._event("info", "Ручная команда запуска: сессия уже активна.")
            return
        active = self._active_slots(o)
        if active:
            names = ", ".join(o.generators[slot].display_name for slot in active)
            self._event(
                "warning",
                f"Managed-запуск отклонён: уже работает внешний генератор ({names}).",
            )
            return
        self._begin_session(o, SessionReason.MANUAL_GENERATOR_START)
        if self.session is not None:
            self.automatic_start_suppressed_until_grid = False

    def _manual_stop(self, o: SupervisorObservation) -> None:
        if self.session is None:
            self._event("info", "Управляемая генераторная сессия не активна.")
            return
        self.session.stop_requested = True
        if self.session.reason == SessionReason.GRID_OUTAGE:
            self.session.control_mode = SessionControlMode.MANUAL_STOP
        if o.grid_ready is False:
            self.automatic_start_suppressed_until_grid = True
        self.desired_source = PowerSource.GRID
        self.phase = SupervisorPhase.RETURNING_TO_GRID

    def _cycle_stop(self, o: SupervisorObservation) -> None:
        if (
            self.session is None
            or self.session.reason != SessionReason.GRID_OUTAGE
            or self.session.control_mode != SessionControlMode.CHARGE_CYCLE
            or self.phase != SupervisorPhase.ON_GENERATOR
        ):
            return
        self.session.stop_requested = True
        self.desired_source = PowerSource.UPS_ONLY
        self.phase = SupervisorPhase.RETURNING_TO_UPS
        self._event("info", "Target SoC достигнут; начинаем возврат на питание только от UPS.")

    def _begin_session(self, o: SupervisorObservation, reason: SessionReason) -> None:
        if self._active_slots(o):
            return
        self._begin_session_for_slot(
            o,
            reason,
            self.config.primary_generator,
            allow_active=False,
        )

    def _begin_session_for_slot(
        self,
        o: SupervisorObservation,
        reason: SessionReason,
        slot: GeneratorSlot,
        *,
        allow_active: bool,
    ) -> None:
        if self._active_slots(o) and not allow_active:
            return

        status = o.generators[slot]
        if not self.config.generator_enabled(slot) or _generator_failed(status):
            self._event(
                "warning",
                f"Generator {status.display_name} недоступен; новая managed-сессия не начата.",
            )
            return

        self.session = GeneratorSession.begin(
            reason,
            slot,
            grid_was_unavailable=o.grid_ready is False,
        )
        self.desired_generators = {
            GeneratorSlot.A: slot == GeneratorSlot.A,
            GeneratorSlot.B: slot == GeneratorSlot.B,
        }
        self.desired_source = (
            PowerSource.UPS_ONLY if o.grid_ready is False else o.power.actual_source
        )
        self.phase = SupervisorPhase.STARTING_GENERATOR
        self._event(
            "info",
            f"Начата сессия {reason.value}; используется {status.display_name}.",
        )

    def _finish_session(self, *, preserve_grid_failure_timer: bool = False) -> None:
        grid_failed_since = self.grid_failed_since
        self.session = None
        self.desired_source = None
        self.desired_generators = _stopped_generators()
        self.phase = SupervisorPhase.NORMAL
        self.grid_failed_since = (
            grid_failed_since if preserve_grid_failure_timer else None
        )
        self._cycle_stop_requested = False
        self._stop_outage_generators.clear()

    # Derived state / persistence ------------------------------------

    def _decision(
        self,
        o: SupervisorObservation,
        exercise_owned_slot: GeneratorSlot | None = None,
        exercise_desired_running: bool = False,
    ) -> SupervisorDecision:
        owner = self._owner(o)
        stable_managed = (
            self.session.generator
            if (
                self.session is not None
                and self.phase == SupervisorPhase.ON_GENERATOR
                and owner == self.session.generator
            )
            else None
        )
        desired = dict(self.desired_generators)
        if (
            self.session is None
            and self.phase != SupervisorPhase.RECOVERY_REQUIRED
            and exercise_owned_slot is not None
            and exercise_desired_running
        ):
            desired[exercise_owned_slot] = True

        return SupervisorDecision(
            desired_source=self.desired_source,
            desired_generators=desired,
            actions_allowed=(
                o.required_states_known
                and self.phase != SupervisorPhase.RECOVERY_REQUIRED
            ),
            stable_managed_generator=stable_managed,
            stop_outage_generators=frozenset(self._stop_outage_generators),
            events=self.take_events(),
        )

    def status_text(self, o: SupervisorObservation) -> str:
        if self.phase == SupervisorPhase.WAITING_FOR_DATA:
            return "Ожидание данных"
        if self.phase == SupervisorPhase.GRID_FAILURE_DELAY:
            return "Ожидание запуска генератора"
        if self.phase == SupervisorPhase.STARTING_GENERATOR:
            return (
                "Переключение на генератор"
                if self.desired_source == PowerSource.GENERATOR
                else "Запуск генератора"
            )
        if self.phase == SupervisorPhase.RETURNING_TO_GRID:
            return "Возврат на основную сеть"
        if self.phase == SupervisorPhase.RETURNING_TO_UPS:
            return "Переход на питание только от UPS"
        if self.phase == SupervisorPhase.EXTERNAL_RUNNING:
            return "Обнаружен внешний запуск"
        if self.phase == SupervisorPhase.RECOVERY_REQUIRED:
            return "Требуется восстановление"
        if self.phase == SupervisorPhase.ON_GENERATOR:
            if (
                self.session is not None
                and self._owner(o) not in {None, self.session.generator}
            ):
                return "Питание от внешнего генератора"
            return "Питание от генератора"
        return {
            PowerSource.GRID: "Питание от основной сети",
            PowerSource.GENERATOR: "Питание от генератора",
            PowerSource.UPS_ONLY: "В доме работает только UPS линия",
            PowerSource.NO_POWER: "Питание отсутствует",
        }.get(o.power.actual_source, "Состояние питания неизвестно")

    def to_dict(self) -> dict[str, object]:
        return {
            "phase": self.phase.value,
            "session": self.session.to_dict() if self.session else None,
            "automatic_start_suppressed_until_grid": (
                self.automatic_start_suppressed_until_grid
            ),
            "recovery_reason": self.recovery_reason,
        }

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
        config: SupervisorConfig,
    ) -> "EnergySupervisor":
        supervisor = cls(config)
        supervisor.phase = SupervisorPhase(str(data["phase"]))
        session = data.get("session")
        supervisor.session = (
            GeneratorSession.from_dict(session)
            if isinstance(session, Mapping)
            else None
        )
        supervisor.automatic_start_suppressed_until_grid = _strict_bool(
            data.get("automatic_start_suppressed_until_grid", False),
            "automatic_start_suppressed_until_grid",
        )
        reason = data.get("recovery_reason")
        supervisor.recovery_reason = str(reason) if reason is not None else None
        return supervisor

    def _restored_session_matches(self, o: SupervisorObservation) -> bool:
        assert self.session is not None
        return (
            not o.power.transition_in_progress
            and o.power.actual_path == PowerPath.GENERATOR
            and self._owner(o) is not None
        )

    def _owner(self, o: SupervisorObservation) -> GeneratorSlot | None:
        return o.bus.owner_slot if o.bus is not None else None

    def _outage_slots(self, o: SupervisorObservation) -> frozenset[GeneratorSlot]:
        if o.bus is not None:
            return o.bus.outage_related_slots
        if self.session is not None and self.session.grid_was_unavailable:
            return frozenset({self.session.generator})
        return frozenset()

    @staticmethod
    def _active_slots(o: SupervisorObservation) -> tuple[GeneratorSlot, ...]:
        return tuple(
            slot
            for slot, status in o.generators.items()
            if status.running is True or status.remote_on is True
        )

    @staticmethod
    def _grid_path(o: SupervisorObservation) -> bool:
        return (
            o.power.actual_path == PowerPath.GRID
            and not o.power.transition_in_progress
        )

    @staticmethod
    def _all_stopped(
        o: SupervisorObservation,
        slots: frozenset[GeneratorSlot] | set[GeneratorSlot],
    ) -> bool:
        return all(
            o.generators[slot].running is False
            and o.generators[slot].remote_on is False
            for slot in slots
        )

    def _grid_stable(self, now: float) -> bool:
        return (
            self.grid_ready_since is not None
            and now - self.grid_ready_since >= self.config.grid_restore_stable_time
        )

    def _update_grid_timer(self, now: float, grid_ready: bool | None) -> None:
        if grid_ready is True:
            if self.grid_ready_since is None:
                self.grid_ready_since = now
            self.grid_failed_since = None
        elif grid_ready is False:
            self.grid_ready_since = None
        else:
            self.grid_ready_since = None
            self.grid_failed_since = None

    def _require_recovery(self, reason: str) -> None:
        if self.phase == SupervisorPhase.RECOVERY_REQUIRED:
            return
        self.phase = SupervisorPhase.RECOVERY_REQUIRED
        self.recovery_reason = reason
        self.desired_generators = _stopped_generators()
        self._stop_outage_generators.clear()
        self._recovery_reset_active = False
        self._event(
            "critical",
            f"Energy ATS остановил автоматическое управление. Причина: {reason}",
        )

    def _discard_requests(self) -> None:
        self._manual_start_requested = False
        self._manual_stop_requested = False
        self._cycle_stop_requested = False

    def _event(self, level: str, message: str) -> None:
        self._events.append(SupervisorEvent(level, message))


def _generator_failed(status: GeneratorStatus) -> bool:
    return status.fault is not None or status.phase == GeneratorPhase.FAULT


def _stopped_generators() -> dict[GeneratorSlot, bool]:
    return {GeneratorSlot.A: False, GeneratorSlot.B: False}


def _other_slot(slot: GeneratorSlot) -> GeneratorSlot:
    return GeneratorSlot.B if slot == GeneratorSlot.A else GeneratorSlot.A


def _strict_bool(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{name} должен быть boolean")
    return value

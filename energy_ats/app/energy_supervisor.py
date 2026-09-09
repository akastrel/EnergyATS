"""Политика EnergyATS поверх реальной физической схемы.

EnergySupervisor не управляет реле напрямую. Он решает:

* нужен ли дому Grid, генераторная шина или изоляция основных источников;
* какой двигатель принадлежит текущей managed-сессии;
* когда разрешён единственный fallback PRIMARY -> SECONDARY;
* когда после восстановления Grid разрешена остановка outage-related генераторов.

Физический выбор Generator A/B на общей генераторной шине Supervisor не делает.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Mapping
from uuid import uuid4

from domain import (
    GeneratorSlot,
    PowerPath,
    PowerSource,
    SessionReason,
    SupervisorEvent,
    Transaction,
    TransactionStatus,
)
from generator_bus import GeneratorBusOwner, GeneratorBusStatus
from generator_controller import GeneratorPhase, GeneratorStatus
from power_transfer import PowerTransferStatus


class SupervisorPhase(str, Enum):
    WAITING_FOR_DATA = "waiting_for_data"
    NORMAL = "normal"
    GRID_FAILURE_DELAY = "grid_failure_delay"
    STARTING_GENERATOR = "starting_generator"
    TRANSFERRING_TO_GENERATOR = "transferring_to_generator"
    ON_GENERATOR = "on_generator"
    ON_EXTERNAL_GENERATOR = "on_external_generator"
    RETURNING_TO_GRID = "returning_to_grid"
    STOPPING_GENERATORS = "stopping_generators"
    EXTERNAL_RUNNING = "external_running"
    RECOVERY_REQUIRED = "recovery_required"


@dataclass(frozen=True)
class SupervisorConfig:
    grid_failure_delay: float = 5.0
    grid_restore_stable_time: float = 60.0
    primary_generator: GeneratorSlot = GeneratorSlot.A
    generator_a_enabled: bool = True
    generator_b_enabled: bool = True

    def generator_enabled(self, slot: GeneratorSlot) -> bool:
        return (
            self.generator_a_enabled
            if slot == GeneratorSlot.A
            else self.generator_b_enabled
        )


@dataclass
class GeneratorSession:
    session_id: str
    reason: SessionReason
    generator: GeneratorSlot
    started_at: float
    grid_was_unavailable: bool
    stop_requested: bool = False
    fallback_used: bool = False

    @classmethod
    def begin(
        cls,
        reason: SessionReason,
        generator: GeneratorSlot,
        now: float,
        grid_was_unavailable: bool,
    ) -> "GeneratorSession":
        return cls(
            session_id=uuid4().hex,
            reason=reason,
            generator=generator,
            started_at=now,
            grid_was_unavailable=grid_was_unavailable,
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["reason"] = self.reason.value
        data["generator"] = self.generator.value
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "GeneratorSession":
        return cls(
            session_id=str(data["session_id"]),
            reason=SessionReason(str(data["reason"])),
            generator=GeneratorSlot(str(data["generator"])),
            started_at=float(data["started_at"]),
            grid_was_unavailable=_strict_bool(
                data["grid_was_unavailable"],
                "session.grid_was_unavailable",
            ),
            stop_requested=_strict_bool(
                data.get("stop_requested", False),
                "session.stop_requested",
            ),
            fallback_used=_strict_bool(
                data.get("fallback_used", False),
                "session.fallback_used",
            ),
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
        generators_known = all(
            status.running is not None and status.remote_on is not None
            for status in self.generators.values()
        )
        return (
            self.grid_ready is not None
            and self.emergency_stop is not None
            and self.power_inputs_known
            and generators_known
        )


@dataclass(frozen=True)
class SupervisorDecision:
    desired_source: PowerSource | None
    desired_generators: Mapping[GeneratorSlot, bool]
    actions_allowed: bool
    stable_managed_generator: GeneratorSlot | None
    stop_outage_generators: frozenset[GeneratorSlot]
    events: tuple[SupervisorEvent, ...]
    status_text: str


class EnergySupervisor:
    """Верхнеуровневая политика источников и ownership сессий."""

    def __init__(self, config: SupervisorConfig | None = None) -> None:
        self.config = config or SupervisorConfig()
        self.phase = SupervisorPhase.WAITING_FOR_DATA
        self.session: GeneratorSession | None = None
        self.transaction: Transaction | None = None
        self.desired_source: PowerSource | None = None
        self.desired_generators: dict[GeneratorSlot, bool] = _stopped_generators()
        self.grid_failed_since: float | None = None
        self.grid_ready_since: float | None = None
        self.automatic_start_suppressed_until_grid = False
        self.recovery_reason: str | None = None
        self.initialized = False
        self._manual_start_requested = False
        self._manual_stop_requested = False
        self._recovery_reset_requested = False
        self._events: list[SupervisorEvent] = []
        self._stop_outage_generators: set[GeneratorSlot] = set()

    def request_manual_start(self) -> None:
        self._manual_start_requested = True

    def request_manual_stop(self) -> None:
        self._manual_stop_requested = True

    def request_recovery_reset(self) -> None:
        self._recovery_reset_requested = True

    def consume_recovery_reset_request(self) -> bool:
        requested = self._recovery_reset_requested
        self._recovery_reset_requested = False
        return requested

    @property
    def recovery_reset_in_progress(self) -> bool:
        return (
            self.phase == SupervisorPhase.RECOVERY_REQUIRED
            and self.transaction is not None
            and self.transaction.kind == "recovery_reset"
            and self.transaction.status == TransactionStatus.IN_PROGRESS
        )

    def begin_recovery_reset(self, now: float) -> None:
        if self.recovery_reset_in_progress:
            self._event("info", "Восстановление уже выполняется.")
            return
        self.transaction = Transaction.begin(
            "recovery_reset", PowerPath.GRID.value, now, "restore_grid_path"
        )
        self.desired_source = PowerSource.GRID
        self.desired_generators = _stopped_generators()
        self._event("info", "Начато безопасное восстановление Energy ATS.")

    def advance_recovery_reset(self, now: float, step: str, *, confirmed: str) -> None:
        if self.recovery_reset_in_progress and self.transaction is not None:
            self.transaction.advance(step, now, confirmed=confirmed)

    def reject_recovery_reset(self, message: str) -> None:
        self._event("warning", message)

    def report_recovery_reset_not_needed(self) -> None:
        self._event("info", "Сброс не требуется: аварийная блокировка не активна.")

    def fail_recovery_reset(self, now: float, reason: str) -> None:
        if self.transaction is not None and self.transaction.kind == "recovery_reset":
            self.transaction.require_recovery(now, reason)
        self.recovery_reason = reason
        self._event("warning", f"Восстановление не завершено: {reason}")

    def complete_recovery_reset(self, now: float) -> None:
        if self.transaction is not None and self.transaction.kind == "recovery_reset":
            self.transaction.complete(now, "Безопасное состояние подтверждено.")
        self.phase = SupervisorPhase.NORMAL
        self.session = None
        self.transaction = None
        self.recovery_reason = None
        self.desired_source = None
        self.desired_generators = _stopped_generators()
        self._stop_outage_generators.clear()
        self._event("info", "Восстановление завершено; управление снова разрешено.")

    def mark_connection_lost(self, now: float) -> None:
        if self.transaction is None or self.transaction.status != TransactionStatus.IN_PROGRESS:
            return
        reason = "Связь потеряна во время незавершённой физической транзакции."
        self.transaction.interrupt(now, "Потеряна связь с Home Assistant.")
        self.transaction.require_recovery(now, reason)
        self.phase = SupervisorPhase.RECOVERY_REQUIRED
        self.recovery_reason = reason

    def require_recovery(self, reason: str) -> None:
        self._require_recovery(reason)

    def manages_stable_generator(self, slot: GeneratorSlot) -> bool:
        return (
            self.session is not None
            and self.session.generator == slot
            and self.phase == SupervisorPhase.ON_GENERATOR
        )

    def step(self, now: float, observation: SupervisorObservation) -> SupervisorDecision:
        self._stop_outage_generators.clear()
        if not self.initialized:
            self._initialize(observation)
            self.initialized = True
        self._update_grid_timers(now, observation.grid_ready)

        if not observation.required_states_known:
            self._discard_manual_requests()
            return self._decision(observation)
        if self.phase == SupervisorPhase.RECOVERY_REQUIRED:
            self._discard_manual_requests()
            return self._decision(observation)
        if observation.emergency_stop is True:
            self._discard_manual_requests()
            self._require_recovery("Активен Generators Emergency Stop.")
            return self._decision(observation)
        if observation.power.recovery_required:
            self._discard_manual_requests()
            self._require_recovery(
                observation.power.fault or "Power Transfer требует восстановления."
            )
            return self._decision(observation)

        if self._manual_start_requested:
            self._manual_start_requested = False
            self._handle_manual_start(now, observation)
        if self._manual_stop_requested:
            self._manual_stop_requested = False
            self._handle_manual_stop(now, observation)

        if self.session is None:
            self._step_without_session(now, observation)
        else:
            self._step_session(now, observation)
        return self._decision(observation)

    def _initialize(self, observation: SupervisorObservation) -> None:
        if self.phase == SupervisorPhase.RECOVERY_REQUIRED:
            return
        if observation.power.recovery_required:
            self._require_recovery(
                observation.power.fault or "Неоднозначная силовая топология."
            )
            return
        if self.session is None:
            self.desired_source = None
            self.phase = SupervisorPhase.NORMAL
            return
        if self.transaction is None:
            self._require_recovery("У сохранённой сессии отсутствует запись транзакции.")
            return
        if self.transaction.status != TransactionStatus.COMPLETED:
            self.transaction.require_recovery(
                self.transaction.updated_at,
                "После restart обнаружена незавершённая транзакция.",
            )
            self._require_recovery(self.transaction.message)
            return
        if not self._restored_session_matches_power(observation):
            self._require_recovery(
                "Фактический источник после restart не совпадает с сохранённой устойчивой сессией."
            )

    def _step_without_session(self, now: float, observation: SupervisorObservation) -> None:
        self.desired_generators = _stopped_generators()
        outage_slots = self._outage_related_slots(observation)
        if outage_slots and self._grid_stable(now):
            self.desired_source = PowerSource.GRID
            self.phase = SupervisorPhase.RETURNING_TO_GRID
            if self._grid_path_confirmed(observation):
                self._stop_outage_generators.update(outage_slots)
                if self._all_slots_stopped(observation, outage_slots):
                    self.phase = SupervisorPhase.NORMAL
                    self.desired_source = None
            return

        self.desired_source = None
        if observation.grid_ready is True:
            self.automatic_start_suppressed_until_grid = False
            self.grid_failed_since = None
            self.phase = SupervisorPhase.NORMAL
            return
        if self.automatic_start_suppressed_until_grid:
            self.phase = SupervisorPhase.NORMAL
            self.grid_failed_since = None
            return
        if observation.grid_ready is not False or not observation.automatic_transfer_enabled:
            if not observation.automatic_transfer_enabled:
                self.grid_failed_since = None
            self.phase = SupervisorPhase.NORMAL
            return
        if self._active_slots(observation):
            self.phase = SupervisorPhase.EXTERNAL_RUNNING
            return
        if self.grid_failed_since is None:
            self.grid_failed_since = now
            self.phase = SupervisorPhase.GRID_FAILURE_DELAY
            return
        if now - self.grid_failed_since >= self.config.grid_failure_delay:
            self._begin_session(now, observation, SessionReason.GRID_OUTAGE)

    def _step_session(self, now: float, observation: SupervisorObservation) -> None:
        assert self.session is not None
        slot = self.session.generator
        generator = observation.generators[slot]

        if (
            self.session.grid_was_unavailable
            and not self.session.stop_requested
            and self._grid_stable(now)
            and self.phase
            not in {SupervisorPhase.RETURNING_TO_GRID, SupervisorPhase.STOPPING_GENERATORS}
        ):
            self._begin_return_to_grid(now)
            return

        if self.phase == SupervisorPhase.STARTING_GENERATOR:
            self._step_starting_generator(now, observation, slot, generator)
        elif self.phase == SupervisorPhase.TRANSFERRING_TO_GENERATOR:
            self._step_transfer_to_generator(now, observation, slot, generator)
        elif self.phase == SupervisorPhase.ON_GENERATOR:
            self._step_on_generator(now, observation, slot, generator)
        elif self.phase == SupervisorPhase.ON_EXTERNAL_GENERATOR:
            self._step_on_external_generator(now, observation, slot, generator)
        elif self.phase == SupervisorPhase.RETURNING_TO_GRID:
            self._step_returning(now, observation)
        elif self.phase == SupervisorPhase.STOPPING_GENERATORS:
            self._step_stopping(now, observation)
        else:
            self._require_recovery(
                f"Неподдерживаемая фаза managed-сессии: {self.phase.value}."
            )

    def _step_starting_generator(
        self,
        now: float,
        observation: SupervisorObservation,
        slot: GeneratorSlot,
        generator: GeneratorStatus,
    ) -> None:
        assert self.session is not None
        self.desired_source = (
            PowerSource.UPS_ONLY
            if self.session.grid_was_unavailable
            else PowerSource.GRID
            if observation.power.actual_source == PowerSource.GRID
            else PowerSource.UPS_ONLY
        )
        if generator.fault is not None or generator.phase in {
            GeneratorPhase.FAULT,
            GeneratorPhase.RECOVERY_REQUIRED,
        }:
            self._managed_generator_failed(
                now,
                observation,
                generator.fault or f"{generator.display_name}: ошибка запуска.",
            )
            return
        if generator.ready_for_load:
            self.desired_source = PowerSource.GENERATOR
            self.phase = SupervisorPhase.TRANSFERRING_TO_GENERATOR
            self._advance_transaction(now, "transfer_to_generator", "generator_ready")

    def _step_transfer_to_generator(
        self,
        now: float,
        observation: SupervisorObservation,
        slot: GeneratorSlot,
        generator: GeneratorStatus,
    ) -> None:
        self.desired_source = PowerSource.GENERATOR
        if generator.fault is not None or not generator.ready_for_load:
            self._managed_generator_failed(
                now,
                observation,
                generator.fault
                or f"{generator.display_name} потерял готовность во время переключения.",
            )
            return
        if (
            observation.power.transition_in_progress
            or observation.power.actual_path != PowerPath.GENERATOR
        ):
            return

        owner = self._bus_owner_slot(observation)
        if owner is None:
            self._require_recovery(
                "Дом подключён к генераторной шине, но её физический owner неизвестен."
            )
            return
        if owner == slot:
            self.phase = SupervisorPhase.ON_GENERATOR
            self._complete_transaction(now, "Дом питается от managed-генератора.")
            self._event(
                "warning",
                f"Дом переведён на резервное питание от {generator.display_name}.",
            )
            return

        self.phase = SupervisorPhase.ON_EXTERNAL_GENERATOR
        self._complete_transaction(
            now,
            "Генераторная шина подключена; физический owner внешний.",
        )
        self._event(
            "warning",
            f"Генераторная шина принадлежит внешнему "
            f"{observation.generators[owner].display_name}; EnergyATS не принимает его под управление.",
        )

    def _step_on_generator(
        self,
        now: float,
        observation: SupervisorObservation,
        slot: GeneratorSlot,
        generator: GeneratorStatus,
    ) -> None:
        self.desired_source = PowerSource.GENERATOR
        self.desired_generators[slot] = True
        owner = self._bus_owner_slot(observation)

        if owner is not None and owner != slot:
            self.phase = SupervisorPhase.ON_EXTERNAL_GENERATOR
            self._event(
                "warning",
                f"Общая генераторная шина перешла к внешнему "
                f"{observation.generators[owner].display_name}.",
            )
            return
        if generator.running is True and generator.ready_for_load:
            if owner is None:
                self._require_recovery(
                    "Managed-генератор работает, но owner генераторной шины неизвестен."
                )
            return
        self._managed_generator_failed(
            now,
            observation,
            generator.fault
            or f"{generator.display_name} неожиданно остановился/потерял готовность.",
        )

    def _step_on_external_generator(
        self,
        now: float,
        observation: SupervisorObservation,
        managed_slot: GeneratorSlot,
        managed_generator: GeneratorStatus,
    ) -> None:
        self.desired_source = PowerSource.GENERATOR
        self.desired_generators[managed_slot] = managed_generator.running is True
        owner = self._bus_owner_slot(observation)

        if owner == managed_slot and managed_generator.ready_for_load:
            self.phase = SupervisorPhase.ON_GENERATOR
            return
        if owner is None:
            if observation.bus is not None and observation.bus.owner == GeneratorBusOwner.UNKNOWN:
                self._require_recovery("Владелец работающей генераторной шины неизвестен.")
                return
            if not observation.power.transition_in_progress and not managed_generator.ready_for_load:
                self._managed_generator_failed(
                    now,
                    observation,
                    "Потерян генераторный источник после внешнего takeover.",
                )

    def _managed_generator_failed(
        self,
        now: float,
        observation: SupervisorObservation,
        reason: str,
    ) -> None:
        assert self.session is not None
        failed_slot = self.session.generator
        self.desired_generators[failed_slot] = False
        other = _other_slot(failed_slot)
        other_status = observation.generators[other]

        if self.session.fallback_used:
            self._require_recovery(
                f"Отказ SECONDARY после автоматического fallback: {reason}"
            )
            return

        if other_status.running is True or other_status.remote_on is True:
            self.desired_source = PowerSource.GENERATOR
            self.phase = SupervisorPhase.ON_EXTERNAL_GENERATOR
            self._event(
                "critical",
                f"Отказ {observation.generators[failed_slot].display_name}: {reason} "
                f"Работающий {other_status.display_name} остаётся внешним; "
                "его REMOTE EnergyATS не захватывает.",
            )
            return
        if not self.config.generator_enabled(other):
            self._require_recovery(
                f"Отказ {observation.generators[failed_slot].display_name}; "
                "SECONDARY запрещён политикой EnergyATS."
            )
            return
        if other_status.fault is not None or other_status.phase in {
            GeneratorPhase.FAULT,
            GeneratorPhase.RECOVERY_REQUIRED,
        }:
            self._require_recovery(
                f"Отказ {observation.generators[failed_slot].display_name}; "
                f"SECONDARY {other_status.display_name} недоступен."
            )
            return

        self.session.generator = other
        self.session.fallback_used = True
        self.desired_source = PowerSource.UPS_ONLY
        self.desired_generators[other] = True
        self.phase = SupervisorPhase.STARTING_GENERATOR
        self.transaction = Transaction.begin(
            "fallback_generator", other.value, now, "start_secondary"
        )
        self._event(
            "critical",
            f"Отказ {observation.generators[failed_slot].display_name}: {reason} "
            f"Выполняется единственный автоматический fallback на {other_status.display_name}.",
        )

    def _begin_return_to_grid(self, now: float) -> None:
        self.desired_source = PowerSource.GRID
        self.phase = SupervisorPhase.RETURNING_TO_GRID
        self.transaction = Transaction.begin(
            "return_to_grid", PowerSource.GRID.value, now, "isolate_generator"
        )

    def _step_returning(self, now: float, observation: SupervisorObservation) -> None:
        assert self.session is not None
        self.desired_source = PowerSource.GRID
        if (
            self.session.grid_was_unavailable
            and not self.session.stop_requested
            and observation.grid_ready is False
        ):
            self._cancel_return_after_grid_failure(now, observation)
            return
        if not self._grid_path_confirmed(observation):
            return

        self._complete_transaction(now, "Grid path подтверждён.")
        self.desired_generators = _stopped_generators()
        if self.session.grid_was_unavailable and not self.session.stop_requested:
            self._stop_outage_generators.update(
                self._outage_related_slots(observation)
            )
        self.phase = SupervisorPhase.STOPPING_GENERATORS
        self.transaction = Transaction.begin(
            "stop_generators", "session", now, "cooldown"
        )

    def _step_stopping(self, now: float, observation: SupervisorObservation) -> None:
        assert self.session is not None
        self.desired_source = PowerSource.GRID
        self.desired_generators = _stopped_generators()

        outage_slots: frozenset[GeneratorSlot] = frozenset()
        if self.session.grid_was_unavailable and not self.session.stop_requested:
            outage_slots = self._outage_related_slots(observation)
            self._stop_outage_generators.update(outage_slots)
            if observation.grid_ready is False:
                self._cancel_return_after_grid_failure(now, observation)
                return

        managed = observation.generators[self.session.generator]
        managed_stopped = managed.running is False and managed.remote_on is False
        if managed_stopped and self._all_slots_stopped(observation, outage_slots):
            self._complete_transaction(now, "Требуемые генераторы остановлены.")
            self._finish_session()

    def _cancel_return_after_grid_failure(
        self,
        now: float,
        observation: SupervisorObservation,
    ) -> None:
        assert self.session is not None
        owner = self._bus_owner_slot(observation)
        if owner is not None and observation.generators[owner].running is True:
            self.desired_source = PowerSource.GENERATOR
            if owner == self.session.generator:
                self.desired_generators[owner] = True
                self.phase = SupervisorPhase.ON_GENERATOR
            else:
                self.phase = SupervisorPhase.ON_EXTERNAL_GENERATOR
            self.transaction = Transaction.begin(
                "resume_after_grid_failure",
                PowerSource.GENERATOR.value,
                now,
                "transfer_to_generator",
            )
            self._event(
                "warning",
                "Grid снова пропала во время возврата; сохраняем/возвращаем доступный генераторный источник.",
            )
            return

        managed = observation.generators[self.session.generator]
        self.desired_source = PowerSource.UPS_ONLY
        self.desired_generators[self.session.generator] = True
        if managed.ready_for_load:
            self.desired_source = PowerSource.GENERATOR
            self.phase = SupervisorPhase.TRANSFERRING_TO_GENERATOR
        else:
            self.phase = SupervisorPhase.STARTING_GENERATOR
        self.transaction = Transaction.begin(
            "resume_after_grid_failure",
            self.session.generator.value,
            now,
            "resume_generator",
        )

    def _handle_manual_start(
        self,
        now: float,
        observation: SupervisorObservation,
    ) -> None:
        if self.session is not None:
            self._event("info", "Ручная команда запуска: сессия уже активна.")
            return
        active = self._active_slots(observation)
        if active:
            names = ", ".join(
                observation.generators[slot].display_name for slot in active
            )
            self._event(
                "warning",
                f"Ручной managed-запуск отклонён: уже работает внешний генератор ({names}).",
            )
            return
        self._begin_session(now, observation, SessionReason.MANUAL_GENERATOR_START)
        if self.session is not None:
            self.automatic_start_suppressed_until_grid = False

    def _handle_manual_stop(
        self,
        now: float,
        observation: SupervisorObservation,
    ) -> None:
        if self.session is None:
            self._event("info", "Управляемая генераторная сессия не активна.")
            return
        self.session.stop_requested = True
        if observation.grid_ready is False:
            self.automatic_start_suppressed_until_grid = True
        self._begin_return_to_grid(now)

    def _begin_session(
        self,
        now: float,
        observation: SupervisorObservation,
        reason: SessionReason,
    ) -> None:
        if self._active_slots(observation):
            self._event(
                "warning",
                "Новая managed-сессия не создана: генератор уже работает внешне.",
            )
            return

        slot = self.config.primary_generator
        if not self.config.generator_enabled(slot):
            self._event("warning", "PRIMARY запрещён политикой EnergyATS.")
            return

        status = observation.generators[slot]
        fallback_used = False
        if status.fault is not None or status.phase in {
            GeneratorPhase.FAULT,
            GeneratorPhase.RECOVERY_REQUIRED,
        }:
            other = _other_slot(slot)
            other_status = observation.generators[other]
            if (
                self.config.generator_enabled(other)
                and other_status.fault is None
                and other_status.running is not True
                and other_status.remote_on is not True
            ):
                slot = other
                fallback_used = True
            else:
                self._event("warning", "Нет доступного генератора для новой сессии.")
                return

        self.session = GeneratorSession.begin(
            reason=reason,
            generator=slot,
            now=now,
            grid_was_unavailable=observation.grid_ready is False,
        )
        self.session.fallback_used = fallback_used
        self.desired_generators = {
            GeneratorSlot.A: slot == GeneratorSlot.A,
            GeneratorSlot.B: slot == GeneratorSlot.B,
        }
        self.desired_source = (
            PowerSource.UPS_ONLY
            if observation.grid_ready is False
            else observation.power.actual_source
        )
        self.phase = SupervisorPhase.STARTING_GENERATOR
        self.transaction = Transaction.begin(
            "enter_generator", slot.value, now, "start_generator"
        )
        self._event(
            "info",
            f"Начата сессия {reason.value}; запрошен запуск "
            f"{observation.generators[slot].display_name}.",
        )

    def _finish_session(self) -> None:
        self.session = None
        self.desired_generators = _stopped_generators()
        self.desired_source = None
        self.phase = SupervisorPhase.NORMAL
        self.transaction = None
        self.grid_failed_since = None
        self._stop_outage_generators.clear()

    def _decision(self, observation: SupervisorObservation) -> SupervisorDecision:
        events = tuple(self._events)
        self._events.clear()
        managed = (
            self.session.generator
            if self.session is not None and self.phase == SupervisorPhase.ON_GENERATOR
            else None
        )
        return SupervisorDecision(
            desired_source=self.desired_source,
            desired_generators=dict(self.desired_generators),
            actions_allowed=(
                observation.required_states_known
                and self.phase != SupervisorPhase.RECOVERY_REQUIRED
            ),
            stable_managed_generator=managed,
            stop_outage_generators=frozenset(self._stop_outage_generators),
            events=events,
            status_text=self.status_text(observation),
        )

    def status_text(self, observation: SupervisorObservation) -> str:
        texts = {
            SupervisorPhase.WAITING_FOR_DATA: "Ожидание данных",
            SupervisorPhase.GRID_FAILURE_DELAY: "Ожидание запуска генератора",
            SupervisorPhase.STARTING_GENERATOR: "Запуск генератора",
            SupervisorPhase.TRANSFERRING_TO_GENERATOR: "Переключение на генератор",
            SupervisorPhase.ON_GENERATOR: "Питание от генератора",
            SupervisorPhase.ON_EXTERNAL_GENERATOR: "Питание от внешнего генератора",
            SupervisorPhase.RETURNING_TO_GRID: "Возврат на основную сеть",
            SupervisorPhase.STOPPING_GENERATORS: "Остановка генераторов",
            SupervisorPhase.RECOVERY_REQUIRED: "Требуется восстановление",
            SupervisorPhase.EXTERNAL_RUNNING: "Обнаружен внешний запуск",
        }
        if self.phase in texts:
            return texts[self.phase]
        if observation.power.actual_source == PowerSource.GRID:
            return "Питание от основной сети"
        if observation.power.actual_source == PowerSource.UPS_ONLY:
            return "В доме работает только UPS линия"
        if observation.power.actual_source == PowerSource.GENERATOR:
            return "Питание от генератора"
        if observation.power.actual_source == PowerSource.NO_POWER:
            return "Питание отсутствует"
        return "Состояние питания неизвестно"

    def _bus_owner_slot(
        self,
        observation: SupervisorObservation,
    ) -> GeneratorSlot | None:
        return observation.bus.owner_slot if observation.bus is not None else None

    def _outage_related_slots(
        self,
        observation: SupervisorObservation,
    ) -> frozenset[GeneratorSlot]:
        if observation.bus is not None:
            return observation.bus.outage_related_slots
        if self.session is not None and self.session.grid_was_unavailable:
            return frozenset({self.session.generator})
        return frozenset()

    @staticmethod
    def _active_slots(
        observation: SupervisorObservation,
    ) -> tuple[GeneratorSlot, ...]:
        return tuple(
            slot
            for slot, status in observation.generators.items()
            if status.running is True or status.remote_on is True
        )

    def _grid_stable(self, now: float) -> bool:
        return (
            self.grid_ready_since is not None
            and now - self.grid_ready_since >= self.config.grid_restore_stable_time
        )

    @staticmethod
    def _grid_path_confirmed(observation: SupervisorObservation) -> bool:
        return (
            observation.power.actual_path == PowerPath.GRID
            and not observation.power.transition_in_progress
        )

    @staticmethod
    def _all_slots_stopped(
        observation: SupervisorObservation,
        slots: frozenset[GeneratorSlot] | set[GeneratorSlot],
    ) -> bool:
        return all(
            observation.generators[slot].running is False
            and observation.generators[slot].remote_on is False
            for slot in slots
        )

    def _restored_session_matches_power(
        self,
        observation: SupervisorObservation,
    ) -> bool:
        assert self.session is not None
        if observation.power.transition_in_progress:
            return False
        if self.phase == SupervisorPhase.ON_GENERATOR:
            return (
                observation.power.actual_path == PowerPath.GENERATOR
                and self._bus_owner_slot(observation) == self.session.generator
            )
        if self.phase == SupervisorPhase.ON_EXTERNAL_GENERATOR:
            return (
                observation.power.actual_path == PowerPath.GENERATOR
                and self._bus_owner_slot(observation) is not None
            )
        return False

    def _update_grid_timers(self, now: float, grid_ready: bool | None) -> None:
        if grid_ready is True:
            if self.grid_ready_since is None:
                self.grid_ready_since = now
            self.grid_failed_since = None
        elif grid_ready is False:
            self.grid_ready_since = None
        else:
            self.grid_ready_since = None
            self.grid_failed_since = None

    def _advance_transaction(self, now: float, step: str, confirmed: str) -> None:
        if self.transaction is not None:
            self.transaction.advance(step, now, confirmed=confirmed)

    def _complete_transaction(self, now: float, message: str) -> None:
        if self.transaction is not None:
            self.transaction.complete(now, message)

    def _require_recovery(self, reason: str) -> None:
        if self.phase == SupervisorPhase.RECOVERY_REQUIRED:
            return
        self.phase = SupervisorPhase.RECOVERY_REQUIRED
        self.recovery_reason = reason
        self.desired_generators = _stopped_generators()
        self._stop_outage_generators.clear()
        self._event(
            "critical",
            f"Energy ATS остановил автоматическое управление. Причина: {reason}",
        )

    def _discard_manual_requests(self) -> None:
        self._manual_start_requested = False
        self._manual_stop_requested = False

    def _event(self, level: str, message: str) -> None:
        self._events.append(SupervisorEvent(level=level, message=message))

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase.value,
            "session": self.session.to_dict() if self.session is not None else None,
            "transaction": (
                self.transaction.to_dict() if self.transaction is not None else None
            ),
            "desired_source": (
                self.desired_source.value if self.desired_source is not None else None
            ),
            "desired_generators": {
                slot.value: value for slot, value in self.desired_generators.items()
            },
            "grid_failed_since": self.grid_failed_since,
            "grid_ready_since": self.grid_ready_since,
            "automatic_start_suppressed_until_grid": (
                self.automatic_start_suppressed_until_grid
            ),
            "recovery_reason": self.recovery_reason,
            "initialized": self.initialized,
        }

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
        config: SupervisorConfig,
    ) -> "EnergySupervisor":
        supervisor = cls(config)
        supervisor.phase = SupervisorPhase(str(data["phase"]))

        session_data = data.get("session")
        supervisor.session = (
            GeneratorSession.from_dict(session_data)
            if isinstance(session_data, Mapping)
            else None
        )
        transaction_data = data.get("transaction")
        supervisor.transaction = (
            Transaction.from_dict(dict(transaction_data))
            if isinstance(transaction_data, Mapping)
            else None
        )
        source = data.get("desired_source")
        supervisor.desired_source = (
            PowerSource(str(source)) if source is not None else None
        )

        desired = data.get("desired_generators")
        if not isinstance(desired, Mapping):
            raise ValueError("desired_generators отсутствует")
        supervisor.desired_generators = {
            GeneratorSlot.A: _strict_bool(
                desired[GeneratorSlot.A.value], "desired A"
            ),
            GeneratorSlot.B: _strict_bool(
                desired[GeneratorSlot.B.value], "desired B"
            ),
        }
        supervisor.grid_failed_since = _optional_float(data.get("grid_failed_since"))
        supervisor.grid_ready_since = _optional_float(data.get("grid_ready_since"))
        supervisor.automatic_start_suppressed_until_grid = _strict_bool(
            data.get("automatic_start_suppressed_until_grid", False),
            "automatic_start_suppressed_until_grid",
        )
        recovery = data.get("recovery_reason")
        supervisor.recovery_reason = str(recovery) if recovery is not None else None
        supervisor.initialized = _strict_bool(
            data.get("initialized", False), "initialized"
        )
        return supervisor


def _stopped_generators() -> dict[GeneratorSlot, bool]:
    return {GeneratorSlot.A: False, GeneratorSlot.B: False}


def _other_slot(slot: GeneratorSlot) -> GeneratorSlot:
    return GeneratorSlot.B if slot == GeneratorSlot.A else GeneratorSlot.A


def _strict_bool(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{name} должен быть boolean")
    return value


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)

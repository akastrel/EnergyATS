"""Политика EnergyATS поверх физической схемы.

Supervisor выбирает требуемый источник дома и managed-генератор. Контакторами
и двигателями он напрямую не управляет.
"""

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
    TRANSFERRING_TO_GENERATOR = "transferring_to_generator"
    ON_GENERATOR = "on_generator"
    ON_EXTERNAL_GENERATOR = "on_external_generator"
    RETURNING_TO_GRID = "returning_to_grid"
    STOPPING_GENERATORS = "stopping_generators"
    EXTERNAL_RUNNING = "external_running"
    RECOVERY_REQUIRED = "recovery_required"


_TRANSIENT_SESSION_PHASES = {
    SupervisorPhase.STARTING_GENERATOR,
    SupervisorPhase.TRANSFERRING_TO_GENERATOR,
    SupervisorPhase.RETURNING_TO_GRID,
    SupervisorPhase.STOPPING_GENERATORS,
}
_STABLE_SESSION_PHASES = {
    SupervisorPhase.ON_GENERATOR,
    SupervisorPhase.ON_EXTERNAL_GENERATOR,
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
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "GeneratorSession":
        return cls(
            reason=SessionReason(str(data["reason"])),
            generator=GeneratorSlot(str(data["generator"])),
            grid_was_unavailable=_strict_bool(
                data["grid_was_unavailable"], "session.grid_was_unavailable"
            ),
            stop_requested=_strict_bool(
                data.get("stop_requested", False), "session.stop_requested"
            ),
            fallback_used=_strict_bool(
                data.get("fallback_used", False), "session.fallback_used"
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
        return (
            self.grid_ready is not None
            and self.emergency_stop is not None
            and self.power_inputs_known
            and all(
                item.running is not None and item.remote_on is not None
                for item in self.generators.values()
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
    status_text: str


class EnergySupervisor:
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
        self._recovery_reset_requested = False
        self._recovery_reset_active = False
        self._events: list[SupervisorEvent] = []
        self._stop_outage_generators: set[GeneratorSlot] = set()

    # ------------------------------------------------------------------
    # Requests / recovery
    # ------------------------------------------------------------------

    def request_manual_start(self) -> None:
        self._manual_start_requested = True

    def request_manual_stop(self) -> None:
        self._manual_stop_requested = True

    def request_recovery_reset(self) -> None:
        self._recovery_reset_requested = True

    def consume_recovery_reset_request(self) -> bool:
        value = self._recovery_reset_requested
        self._recovery_reset_requested = False
        return value

    @property
    def recovery_reset_in_progress(self) -> bool:
        return self._recovery_reset_active

    def begin_recovery_reset(self) -> None:
        if self._recovery_reset_active:
            self._event("info", "Восстановление уже выполняется.")
            return
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
        self._stop_outage_generators.clear()
        self._event("info", "Восстановление завершено; управление снова разрешено.")

    def mark_connection_lost(self) -> None:
        if self.phase in _TRANSIENT_SESSION_PHASES:
            self._require_recovery(
                "Связь потеряна во время незавершённой физической операции."
            )

    def require_recovery(self, reason: str) -> None:
        self._require_recovery(reason)

    def manages_stable_generator(self, slot: GeneratorSlot) -> bool:
        return (
            self.phase == SupervisorPhase.ON_GENERATOR
            and self.session is not None
            and self.session.generator == slot
        )

    # ------------------------------------------------------------------
    # Main policy step
    # ------------------------------------------------------------------

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
            self._handle_manual_start(observation, SessionReason.MANUAL_GENERATOR_START)
        if self._manual_stop_requested:
            self._manual_stop_requested = False
            self._handle_manual_stop(observation)

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
            if self.phase in _TRANSIENT_SESSION_PHASES:
                self._require_recovery(
                    "После restart обнаружена незавершённая операция без managed-сессии."
                )
            else:
                self.phase = SupervisorPhase.NORMAL
                self.desired_source = None
                self.desired_generators = _stopped_generators()
            return

        if self.phase not in _STABLE_SESSION_PHASES:
            self._require_recovery(
                "После restart обнаружена незавершённая managed-операция."
            )
            return
        if not self._restored_session_matches_power(observation):
            self._require_recovery(
                "Фактический источник после restart не совпадает с сохранённой устойчивой сессией."
            )
            return

        self.desired_source = PowerSource.GENERATOR
        self.desired_generators = _stopped_generators()
        if self.phase == SupervisorPhase.ON_GENERATOR:
            self.desired_generators[self.session.generator] = True

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
            self.grid_failed_since = None
            self.phase = SupervisorPhase.NORMAL
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
            self._begin_session(observation, SessionReason.GRID_OUTAGE)

    def _step_session(self, now: float, observation: SupervisorObservation) -> None:
        assert self.session is not None
        slot = self.session.generator
        generator = observation.generators[slot]

        if (
            self.session.grid_was_unavailable
            and not self.session.stop_requested
            and self._grid_stable(now)
            and self.phase not in {
                SupervisorPhase.RETURNING_TO_GRID,
                SupervisorPhase.STOPPING_GENERATORS,
            }
        ):
            self._begin_return_to_grid()
            return

        if self.phase == SupervisorPhase.STARTING_GENERATOR:
            self._step_starting_generator(observation, generator)
        elif self.phase == SupervisorPhase.TRANSFERRING_TO_GENERATOR:
            self._step_transfer_to_generator(observation, slot, generator)
        elif self.phase == SupervisorPhase.ON_GENERATOR:
            self._step_on_generator(observation, slot, generator)
        elif self.phase == SupervisorPhase.ON_EXTERNAL_GENERATOR:
            self._step_on_external_generator(observation, slot, generator)
        elif self.phase == SupervisorPhase.RETURNING_TO_GRID:
            self._step_returning(observation)
        elif self.phase == SupervisorPhase.STOPPING_GENERATORS:
            self._step_stopping(observation)
        else:
            self._require_recovery(
                f"Неподдерживаемая фаза managed-сессии: {self.phase.value}."
            )

    def _step_starting_generator(
        self,
        observation: SupervisorObservation,
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
        if _generator_failed(generator):
            self._managed_generator_failed(
                observation,
                generator.fault or f"{generator.display_name}: ошибка запуска.",
            )
        elif generator.ready_for_load:
            self.desired_source = PowerSource.GENERATOR
            self.phase = SupervisorPhase.TRANSFERRING_TO_GENERATOR

    def _step_transfer_to_generator(
        self,
        observation: SupervisorObservation,
        slot: GeneratorSlot,
        generator: GeneratorStatus,
    ) -> None:
        self.desired_source = PowerSource.GENERATOR
        if _generator_failed(generator) or not generator.ready_for_load:
            self._managed_generator_failed(
                observation,
                generator.fault
                or f"{generator.display_name} потерял готовность во время переключения.",
            )
            return
        if observation.power.transition_in_progress or observation.power.actual_path != PowerPath.GENERATOR:
            return

        owner = self._bus_owner_slot(observation)
        if owner is None:
            self._require_recovery(
                "Дом подключён к генераторной шине, но её физический owner неизвестен."
            )
            return
        if owner == slot:
            self.phase = SupervisorPhase.ON_GENERATOR
            self._event(
                "warning", f"Дом переведён на резервное питание от {generator.display_name}."
            )
        else:
            self.phase = SupervisorPhase.ON_EXTERNAL_GENERATOR
            self._event(
                "warning",
                f"Генераторная шина принадлежит внешнему "
                f"{observation.generators[owner].display_name}; EnergyATS не принимает его под управление.",
            )

    def _step_on_generator(
        self,
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
            observation,
            generator.fault
            or f"{generator.display_name} неожиданно остановился/потерял готовность.",
        )

    def _step_on_external_generator(
        self,
        observation: SupervisorObservation,
        managed_slot: GeneratorSlot,
        managed_generator: GeneratorStatus,
    ) -> None:
        self.desired_source = PowerSource.GENERATOR
        self.desired_generators[managed_slot] = managed_generator.running is True
        owner = self._bus_owner_slot(observation)

        if owner == managed_slot and managed_generator.ready_for_load:
            self.phase = SupervisorPhase.ON_GENERATOR
        elif owner is None and observation.bus is not None and observation.bus.owner == GeneratorBusOwner.UNKNOWN:
            self._require_recovery("Владелец работающей генераторной шины неизвестен.")
        elif owner is None and not observation.power.transition_in_progress and not managed_generator.ready_for_load:
            self._managed_generator_failed(
                observation, "Потерян генераторный источник после внешнего takeover."
            )

    def _managed_generator_failed(
        self,
        observation: SupervisorObservation,
        reason: str,
    ) -> None:
        assert self.session is not None
        failed = self.session.generator
        self.desired_generators[failed] = False
        other = _other_slot(failed)
        other_status = observation.generators[other]

        if self.session.fallback_used:
            self._require_recovery(f"Отказ SECONDARY после fallback: {reason}")
            return
        if other_status.running is True or other_status.remote_on is True:
            self.desired_source = PowerSource.GENERATOR
            self.phase = SupervisorPhase.ON_EXTERNAL_GENERATOR
            self._event(
                "critical",
                f"Отказ {observation.generators[failed].display_name}: {reason} "
                f"Работающий {other_status.display_name} остаётся внешним.",
            )
            return
        if not self.config.generator_enabled(other):
            self._require_recovery(
                f"Отказ {observation.generators[failed].display_name}; SECONDARY запрещён политикой."
            )
            return
        if _generator_failed(other_status):
            self._require_recovery(
                f"Отказ {observation.generators[failed].display_name}; "
                f"SECONDARY {other_status.display_name} недоступен."
            )
            return

        self.session.generator = other
        self.session.fallback_used = True
        self.desired_source = PowerSource.UPS_ONLY
        self.desired_generators[other] = True
        self.phase = SupervisorPhase.STARTING_GENERATOR
        self._event(
            "critical",
            f"Отказ {observation.generators[failed].display_name}: {reason} "
            f"Выполняется единственный fallback на {other_status.display_name}.",
        )

    def _begin_return_to_grid(self) -> None:
        self.desired_source = PowerSource.GRID
        self.phase = SupervisorPhase.RETURNING_TO_GRID

    def _step_returning(self, observation: SupervisorObservation) -> None:
        assert self.session is not None
        self.desired_source = PowerSource.GRID
        if (
            self.session.grid_was_unavailable
            and not self.session.stop_requested
            and observation.grid_ready is False
        ):
            self._cancel_return_after_grid_failure(observation)
            return
        if not self._grid_path_confirmed(observation):
            return

        self.desired_generators = _stopped_generators()
        if self.session.grid_was_unavailable and not self.session.stop_requested:
            self._stop_outage_generators.update(self._outage_related_slots(observation))
        self.phase = SupervisorPhase.STOPPING_GENERATORS

    def _step_stopping(self, observation: SupervisorObservation) -> None:
        assert self.session is not None
        self.desired_source = PowerSource.GRID
        self.desired_generators = _stopped_generators()

        outage_slots: frozenset[GeneratorSlot] = frozenset()
        if self.session.grid_was_unavailable and not self.session.stop_requested:
            outage_slots = self._outage_related_slots(observation)
            self._stop_outage_generators.update(outage_slots)
            if observation.grid_ready is False:
                self._cancel_return_after_grid_failure(observation)
                return

        managed = observation.generators[self.session.generator]
        if (
            managed.running is False
            and managed.remote_on is False
            and self._all_slots_stopped(observation, outage_slots)
        ):
            self._finish_session()

    def _cancel_return_after_grid_failure(self, observation: SupervisorObservation) -> None:
        assert self.session is not None
        owner = self._bus_owner_slot(observation)
        if owner is not None and observation.generators[owner].running is True:
            self.desired_source = PowerSource.GENERATOR
            if owner == self.session.generator:
                self.desired_generators[owner] = True
                self.phase = SupervisorPhase.ON_GENERATOR
            else:
                self.phase = SupervisorPhase.ON_EXTERNAL_GENERATOR
            self._event(
                "warning",
                "Grid снова пропала во время возврата; сохраняем доступный генераторный источник.",
            )
            return

        managed = observation.generators[self.session.generator]
        self.desired_generators[self.session.generator] = True
        self.desired_source = (
            PowerSource.GENERATOR if managed.ready_for_load else PowerSource.UPS_ONLY
        )
        self.phase = (
            SupervisorPhase.TRANSFERRING_TO_GENERATOR
            if managed.ready_for_load
            else SupervisorPhase.STARTING_GENERATOR
        )

    # ------------------------------------------------------------------
    # User commands / session lifecycle
    # ------------------------------------------------------------------

    def _handle_manual_start(
        self,
        observation: SupervisorObservation,
        reason: SessionReason,
    ) -> None:
        if self.session is not None:
            self._event("info", "Ручная команда запуска: сессия уже активна.")
            return
        active = self._active_slots(observation)
        if active:
            names = ", ".join(observation.generators[slot].display_name for slot in active)
            self._event(
                "warning",
                f"Managed-запуск отклонён: уже работает внешний генератор ({names}).",
            )
            return
        self._begin_session(observation, reason)
        if self.session is not None:
            self.automatic_start_suppressed_until_grid = False

    def _handle_manual_stop(self, observation: SupervisorObservation) -> None:
        if self.session is None:
            self._event("info", "Управляемая генераторная сессия не активна.")
            return
        self.session.stop_requested = True
        if observation.grid_ready is False:
            self.automatic_start_suppressed_until_grid = True
        self._begin_return_to_grid()

    def _begin_session(
        self,
        observation: SupervisorObservation,
        reason: SessionReason,
    ) -> None:
        if self._active_slots(observation):
            self._event("warning", "Новая managed-сессия не создана: генератор уже работает внешне.")
            return

        slot = self.config.primary_generator
        if not self.config.generator_enabled(slot):
            self._event("warning", "PRIMARY запрещён политикой EnergyATS.")
            return

        status = observation.generators[slot]
        fallback_used = False
        if _generator_failed(status):
            other = _other_slot(slot)
            other_status = observation.generators[other]
            if (
                self.config.generator_enabled(other)
                and not _generator_failed(other_status)
                and other_status.running is not True
                and other_status.remote_on is not True
            ):
                slot = other
                fallback_used = True
            else:
                self._event("warning", "Нет доступного генератора для новой сессии.")
                return

        self.session = GeneratorSession.begin(
            reason,
            slot,
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
        self.grid_failed_since = None
        self._stop_outage_generators.clear()

    # ------------------------------------------------------------------
    # Derived state / persistence
    # ------------------------------------------------------------------

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
        fixed = {
            SupervisorPhase.WAITING_FOR_DATA: "Ожидание данных",
            SupervisorPhase.GRID_FAILURE_DELAY: "Ожидание запуска генератора",
            SupervisorPhase.STARTING_GENERATOR: "Запуск генератора",
            SupervisorPhase.TRANSFERRING_TO_GENERATOR: "Переключение на генератор",
            SupervisorPhase.ON_GENERATOR: "Питание от генератора",
            SupervisorPhase.ON_EXTERNAL_GENERATOR: "Питание от внешнего генератора",
            SupervisorPhase.RETURNING_TO_GRID: "Возврат на основную сеть",
            SupervisorPhase.STOPPING_GENERATORS: "Остановка генераторов",
            SupervisorPhase.EXTERNAL_RUNNING: "Обнаружен внешний запуск",
            SupervisorPhase.RECOVERY_REQUIRED: "Требуется восстановление",
        }
        if self.phase in fixed:
            return fixed[self.phase]
        return {
            PowerSource.GRID: "Питание от основной сети",
            PowerSource.GENERATOR: "Питание от генератора",
            PowerSource.UPS_ONLY: "В доме работает только UPS линия",
            PowerSource.NO_POWER: "Питание отсутствует",
        }.get(observation.power.actual_source, "Состояние питания неизвестно")

    def to_dict(self) -> dict[str, object]:
        return {
            "phase": self.phase.value,
            "session": self.session.to_dict() if self.session is not None else None,
            "automatic_start_suppressed_until_grid": self.automatic_start_suppressed_until_grid,
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
            GeneratorSession.from_dict(session) if isinstance(session, Mapping) else None
        )
        supervisor.automatic_start_suppressed_until_grid = _strict_bool(
            data.get("automatic_start_suppressed_until_grid", False),
            "automatic_start_suppressed_until_grid",
        )
        reason = data.get("recovery_reason")
        supervisor.recovery_reason = str(reason) if reason is not None else None
        # Timers and desired outputs are intentionally reconstructed from fresh
        # physical observations after restart.
        return supervisor

    def _bus_owner_slot(self, observation: SupervisorObservation) -> GeneratorSlot | None:
        return observation.bus.owner_slot if observation.bus is not None else None

    def _outage_related_slots(self, observation: SupervisorObservation) -> frozenset[GeneratorSlot]:
        if observation.bus is not None:
            return observation.bus.outage_related_slots
        if self.session is not None and self.session.grid_was_unavailable:
            return frozenset({self.session.generator})
        return frozenset()

    @staticmethod
    def _active_slots(observation: SupervisorObservation) -> tuple[GeneratorSlot, ...]:
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

    def _restored_session_matches_power(self, observation: SupervisorObservation) -> bool:
        assert self.session is not None
        if observation.power.transition_in_progress:
            return False
        owner = self._bus_owner_slot(observation)
        if self.phase == SupervisorPhase.ON_GENERATOR:
            return observation.power.actual_path == PowerPath.GENERATOR and owner == self.session.generator
        if self.phase == SupervisorPhase.ON_EXTERNAL_GENERATOR:
            return observation.power.actual_path == PowerPath.GENERATOR and owner is not None
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

    def _discard_manual_requests(self) -> None:
        self._manual_start_requested = False
        self._manual_stop_requested = False

    def _event(self, level: str, message: str) -> None:
        self._events.append(SupervisorEvent(level, message))


def _generator_failed(status: GeneratorStatus) -> bool:
    return status.fault is not None or status.phase in {
        GeneratorPhase.FAULT,
        GeneratorPhase.RECOVERY_REQUIRED,
    }


def _stopped_generators() -> dict[GeneratorSlot, bool]:
    return {GeneratorSlot.A: False, GeneratorSlot.B: False}


def _other_slot(slot: GeneratorSlot) -> GeneratorSlot:
    return GeneratorSlot.B if slot == GeneratorSlot.A else GeneratorSlot.A


def _strict_bool(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{name} должен быть boolean")
    return value

"""Верхний уровень: зачем сейчас нужен тот или иной источник энергии.

EnergySupervisor не управляет реле и заслонкой. Он хранит длительную сессию,
создаёт транзакции перехода и задаёт двум нижним контроллерам только цели:

* какой источник должен питать дом;
* какой генератор должен продолжать работать.
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
from generator_controller import GeneratorPhase, GeneratorStatus
from power_transfer import PowerTransferStatus


class SupervisorPhase(str, Enum):
    WAITING_FOR_DATA = "waiting_for_data"
    NORMAL = "normal"
    GRID_FAILURE_DELAY = "grid_failure_delay"
    STARTING_GENERATOR = "starting_generator"
    TRANSFERRING_TO_GENERATOR = "transferring_to_generator"
    ON_GENERATOR = "on_generator"
    RETURNING_TO_GRID_OR_BATTERY = "returning_to_grid_or_battery"
    MANUAL_GENERATOR_IDLE = "manual_generator_idle"
    STOPPING_GENERATOR = "stopping_generator"
    ISOLATING_FAILED_SOURCE = "isolating_failed_source"
    EXTERNAL_RUNNING = "external_running"
    RECOVERY_REQUIRED = "recovery_required"


@dataclass(frozen=True)
class SupervisorConfig:
    grid_failure_delay: float = 5.0
    grid_restore_stable_time: float = 60.0
    manual_idle_warning_seconds: float = 600.0
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
        )


@dataclass(frozen=True)
class SupervisorObservation:
    grid_ready: bool | None
    automatic_transfer_enabled: bool
    emergency_stop: bool | None
    power: PowerTransferStatus
    generators: Mapping[GeneratorSlot, GeneratorStatus]
    power_inputs_known: bool = True

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
    desired_source: PowerSource
    desired_generators: Mapping[GeneratorSlot, bool]
    actions_allowed: bool
    stable_managed_generator: GeneratorSlot | None
    events: tuple[SupervisorEvent, ...]
    status_text: str


class EnergySupervisor:
    """Политика источников и владелец пользовательской сессии."""

    def __init__(self, config: SupervisorConfig | None = None) -> None:
        self.config = config or SupervisorConfig()
        self.phase = SupervisorPhase.WAITING_FOR_DATA
        self.session: GeneratorSession | None = None
        self.transaction: Transaction | None = None
        self.desired_source = PowerSource.UNKNOWN
        self.desired_generators: dict[GeneratorSlot, bool] = {
            GeneratorSlot.A: False,
            GeneratorSlot.B: False,
        }
        self.grid_failed_since: float | None = None
        self.grid_ready_since: float | None = None
        self.generator_idle_since: float | None = None
        self.idle_warning_sent = False
        self.automatic_start_suppressed_until_grid = False
        self.recovery_reason: str | None = None
        self.initialized = False
        self._manual_start_requested = False
        self._manual_stop_requested = False
        self._recovery_reset_requested = False
        self._events: list[SupervisorEvent] = []

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

    def mark_connection_lost(self, now: float) -> None:
        """Отметить только действительно прерванную физическую операцию."""
        if self.transaction is None:
            return
        if self.transaction.status != TransactionStatus.IN_PROGRESS:
            return
        self.transaction.interrupt(now, "Потеряна связь с Home Assistant.")
        self.transaction.require_recovery(
            now,
            "Связь потеряна во время незавершённой физической транзакции.",
        )
        self.phase = SupervisorPhase.RECOVERY_REQUIRED
        self.recovery_reason = self.transaction.message

    def require_recovery(self, reason: str) -> None:
        """Явно заблокировать автоматику, например при повреждённом журнале."""
        self._require_recovery(reason)

    def manages_stable_generator(self, slot: GeneratorSlot) -> bool:
        """Можно ли после restart признать работающий двигатель своим."""
        return (
            self.session is not None
            and self.session.generator == slot
            and self.phase
            in {
                SupervisorPhase.ON_GENERATOR,
                SupervisorPhase.MANUAL_GENERATOR_IDLE,
            }
        )

    def recovery_reset_result(self, succeeded: bool, message: str) -> None:
        if succeeded:
            self.phase = SupervisorPhase.NORMAL
            self.session = None
            self.transaction = None
            self.recovery_reason = None
            self.desired_generators = {
                GeneratorSlot.A: False,
                GeneratorSlot.B: False,
            }
            self._event("info", message)
        else:
            self._event("warning", message)

    def step(self, now: float, observation: SupervisorObservation) -> SupervisorDecision:
        if not self.initialized:
            self._initialize(observation)
            self.initialized = True

        self._update_grid_timers(now, observation.grid_ready)

        if not observation.required_states_known:
            command_waiting = (
                self._manual_start_requested or self._manual_stop_requested
            )
            self._manual_start_requested = False
            self._manual_stop_requested = False
            if command_waiting:
                self._event(
                    "warning",
                    "Ручная команда отклонена: отсутствуют обязательные "
                    "физические данные.",
                )
            return self._decision(observation)

        if self.phase == SupervisorPhase.RECOVERY_REQUIRED:
            if self._manual_start_requested or self._manual_stop_requested:
                self._event(
                    "warning",
                    "Ручная команда отклонена до завершения recovery.",
                )
            self._manual_start_requested = False
            self._manual_stop_requested = False
            return self._decision(observation)

        if observation.emergency_stop is True:
            self._manual_start_requested = False
            self._manual_stop_requested = False
            self._require_recovery("Активен Generators Emergency Stop.")
            return self._decision(observation)

        if observation.power.recovery_required:
            self._manual_start_requested = False
            self._manual_stop_requested = False
            self._require_recovery(
                observation.power.fault or "Power Transfer требует восстановления."
            )
            return self._decision(observation)

        # Fault GC, которым не владеет текущая сессия, является аварией
        # нижнего контроллера по ES-16. Без сессии это относится к обоим GC;
        # во время сессии — к невыбранному агрегату. Неисправность выбранного
        # генератора ниже обрабатывается отдельно по ES-15 с изоляцией шины.
        managed_slot = self.session.generator if self.session is not None else None
        unsafe_generators = [
            status
            for slot, status in observation.generators.items()
            if slot != managed_slot
            and status.phase
            in {GeneratorPhase.FAULT, GeneratorPhase.RECOVERY_REQUIRED}
        ]
        if unsafe_generators:
            self._manual_start_requested = False
            self._manual_stop_requested = False
            names = ", ".join(
                status.display_name for status in unsafe_generators
            )
            self._require_recovery(
                f"Контроллер генератора требует проверки: {names}."
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
        if self.session is not None:
            # Восстановленная незавершённая транзакция никогда не продолжается
            # автоматически. Завершённая сессия в steady state восстанавливается.
            if self.transaction is None:
                self._require_recovery(
                    "У сохранённой сессии отсутствует запись транзакции."
                )
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
                    "Фактический источник после restart не совпадает с "
                    "сохранённой устойчивой сессией. Оборудование оставлено "
                    "без изменений."
                )
            return
        self.desired_source = observation.power.actual_source
        self.phase = SupervisorPhase.NORMAL

    def _step_without_session(
        self, now: float, observation: SupervisorObservation
    ) -> None:
        external = self._external_generators(observation)
        if external:
            if self.phase != SupervisorPhase.EXTERNAL_RUNNING:
                names = ", ".join(
                    observation.generators[slot].display_name for slot in external
                )
                self._event(
                    "warning",
                    f"Обнаружен внешний запуск ({names}). Supervisor только наблюдает.",
                )
            self.phase = SupervisorPhase.EXTERNAL_RUNNING
            self.desired_generators = {
                GeneratorSlot.A: False,
                GeneratorSlot.B: False,
            }
            # Внешний запуск только наблюдаем; силовую цель не меняем.
            return

        if self.phase == SupervisorPhase.EXTERNAL_RUNNING:
            if (
                observation.power.actual_path
                not in {PowerPath.GRID, PowerPath.BATTERY}
                or observation.power.transition_in_progress
            ):
                # Внешний сеанс остаётся внешним и после остановки двигателя,
                # пока человек сам не вернёт силовую схему в Grid path или
                # Battery path.
                # Никаких команд контакторам здесь не формируем.
                self.desired_generators = {
                    GeneratorSlot.A: False,
                    GeneratorSlot.B: False,
                }
                return
            if observation.grid_ready is False:
                self.automatic_start_suppressed_until_grid = True
                self.grid_failed_since = None
            self._event("info", "Внешне запущенный генератор остановлен.")

        self.phase = (
            SupervisorPhase.GRID_FAILURE_DELAY
            if self.phase == SupervisorPhase.GRID_FAILURE_DELAY
            else SupervisorPhase.NORMAL
        )
        self.desired_source = self._safe_source_for_grid_state(
            observation.grid_ready
        )

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
                # После повторного включения АВР выдержка начинается заново,
                # а не использует старое время отсутствия Grid.
                self.grid_failed_since = None
            self.phase = SupervisorPhase.NORMAL
            return

        if self.grid_failed_since is None:
            self.grid_failed_since = now
            self.phase = SupervisorPhase.GRID_FAILURE_DELAY
            return

        if now - self.grid_failed_since >= self.config.grid_failure_delay:
            self._begin_session(now, observation, SessionReason.GRID_OUTAGE)

    def _step_session(
        self, now: float, observation: SupervisorObservation
    ) -> None:
        assert self.session is not None
        slot = self.session.generator
        generator = observation.generators[slot]
        other_slot = (
            GeneratorSlot.B if slot == GeneratorSlot.A else GeneratorSlot.A
        )
        other_generator = observation.generators[other_slot]

        if self.phase == SupervisorPhase.ISOLATING_FAILED_SOURCE:
            # Безопасная цель следует за доступностью Grid даже если сеть
            # изменилась уже после обнаружения отказа.
            self.desired_source = self._safe_source_for_grid_state(
                observation.grid_ready
            )
            if self._safe_power_path_confirmed(observation):
                if self.transaction is not None:
                    self.transaction.advance(
                        "recovery_required",
                        now,
                        confirmed="failed_source_isolated",
                    )
                    self.transaction.require_recovery(
                        now,
                        "Неисправный источник изолирован; требуется проверка человеком.",
                    )
                self.phase = SupervisorPhase.RECOVERY_REQUIRED
                self.recovery_reason = (
                    "Неисправный источник изолирован. Автоматический fallback отключён."
                )
            return

        if (
            self.phase == SupervisorPhase.MANUAL_GENERATOR_IDLE
            and generator.running is False
            and generator.remote_on is False
        ):
            self._event(
                "info",
                f"{generator.display_name} остановлен локально; "
                "ручная сессия завершена.",
            )
            self._finish_session(observation)
            return

        if (
            other_generator.running is True
            or other_generator.remote_on is True
        ):
            self._handle_generator_fault(
                now,
                observation,
                f"Во время управляемой сессии {generator.display_name} "
                f"обнаружен активный {other_generator.display_name}.",
                event_title="Нарушена взаимная блокировка генераторов",
            )
            return

        if generator.fault is not None:
            self._handle_generator_fault(now, observation, generator.fault)
            return

        if self.phase == SupervisorPhase.STARTING_GENERATOR:
            # Автоматический запуск больше не ведёт к генератору, как только
            # Grid снова появилась. До 60-секундного подтверждения удерживаем
            # Battery path, после чего возвращаем дом на Grid path.
            if (
                self.session.reason == SessionReason.GRID_OUTAGE
                and observation.grid_ready is True
            ):
                self.desired_source = PowerSource.BATTERY
                if self._should_return_after_grid_restore(now, observation):
                    self._begin_return_to_safe_source(now, observation)
                return

            # Пока генератор не готов, при доступной Grid дом остаётся на Grid
            # path, а при её отсутствии — на Battery path.
            self.desired_source = self._safe_source_for_grid_state(
                observation.grid_ready
            )
            if generator.externally_started:
                self._require_recovery(
                    f"{generator.display_name} запущен внешне во время управляемой сессии."
                )
                return
            if generator.ready_for_load:
                if self._should_return_after_grid_restore(now, observation):
                    self._begin_return_to_safe_source(now, observation)
                else:
                    self.desired_source = PowerSource.for_generator(slot)
                    self.phase = SupervisorPhase.TRANSFERRING_TO_GENERATOR
                    self._advance_transaction(now, "transfer_to_generator", "generator_ready")
            return

        if self.phase == SupervisorPhase.TRANSFERRING_TO_GENERATOR:
            if (
                self.session.reason == SessionReason.GRID_OUTAGE
                and observation.grid_ready is True
            ):
                # Если TPC уже начал движение, цель BATTERY безопасно отменит
                # ввод генератора и доведёт текущий шаг до конца.
                self.desired_source = PowerSource.BATTERY
                if self._should_return_after_grid_restore(now, observation):
                    self._begin_return_to_safe_source(now, observation)
                return
            if self._should_return_after_grid_restore(now, observation):
                self._begin_return_to_safe_source(now, observation)
                return
            if not generator.ready_for_load:
                self._handle_generator_fault(
                    now,
                    observation,
                    f"{generator.display_name} потерял готовность во время "
                    "силового переключения.",
                )
                return
            if (
                observation.power.actual_source == PowerSource.for_generator(slot)
                and not observation.power.transition_in_progress
            ):
                self.phase = SupervisorPhase.ON_GENERATOR
                self._complete_transaction(now, "Дом питается от генератора.")
                self._event(
                    "warning",
                    f"Дом переведён на резервное питание от {generator.display_name}.",
                )
            return

        if self.phase == SupervisorPhase.ON_GENERATOR:
            if generator.running is not True or not generator.ready_for_load:
                self._handle_generator_fault(
                    now,
                    observation,
                    generator.fault
                    or f"{generator.display_name} потерял готовность под нагрузкой.",
                )
                return
            expected_source = PowerSource.for_generator(slot)
            if (
                not observation.power.transition_in_progress
                and observation.power.actual_source != expected_source
            ):
                self._require_recovery(
                    "Положение силовой схемы изменилось вне транзакции "
                    f"управляемой сессии {generator.display_name}."
                )
                return
            if self._should_return_after_grid_restore(now, observation):
                self._begin_return_to_safe_source(now, observation)
            return

        if self.phase == SupervisorPhase.RETURNING_TO_GRID_OR_BATTERY:
            if (
                observation.grid_ready is False
                and not self.session.stop_requested
                and self.session.grid_was_unavailable
            ):
                self._cancel_automatic_return(now, observation, generator)
                return
            if not self._safe_power_path_confirmed(observation):
                return
            self._complete_transaction(
                now,
                "Целевой Grid path или Battery path подтверждён.",
            )
            if self.session.stop_requested or self.session.reason != SessionReason.MANUAL_BACKUP:
                self.desired_generators[slot] = False
                self.phase = SupervisorPhase.STOPPING_GENERATOR
                self.transaction = Transaction.begin(
                    "stop_generator", slot.value, now, "cooldown"
                )
            else:
                # Ручное намерение RUN сохраняется отдельно от источника дома.
                self.phase = SupervisorPhase.MANUAL_GENERATOR_IDLE
                self.generator_idle_since = now
                self.idle_warning_sent = False
                self._event(
                    "warning",
                    f"Дом возвращён на Grid, но {generator.display_name} оставлен "
                    "работать по ручной команде.",
                )
            return

        if self.phase == SupervisorPhase.MANUAL_GENERATOR_IDLE:
            if (
                not observation.power.transition_in_progress
                and observation.power.actual_source
                not in {PowerSource.GRID, PowerSource.BATTERY}
            ):
                self._require_recovery(
                    "Положение силовой схемы изменилось вне транзакции "
                    "ручной сессии."
                )
                return
            self.desired_source = self._safe_source_for_grid_state(
                observation.grid_ready
            )
            if observation.grid_ready is False and generator.ready_for_load:
                self.desired_source = PowerSource.for_generator(slot)
                self.phase = SupervisorPhase.TRANSFERRING_TO_GENERATOR
                self.transaction = Transaction.begin(
                    "restore_manual_backup",
                    self.desired_source.value,
                    now,
                    "transfer_to_generator",
                )
                self.generator_idle_since = None
                self.idle_warning_sent = False
                return
            self._maybe_warn_about_idle_generator(now, generator)
            return

        if self.phase == SupervisorPhase.STOPPING_GENERATOR:
            self.desired_source = self._safe_source_for_grid_state(
                observation.grid_ready
            )
            if (
                observation.grid_ready is False
                and not self.session.stop_requested
                and generator.ready_for_load
            ):
                self.desired_generators[slot] = True
                self.desired_source = PowerSource.for_generator(slot)
                self.phase = SupervisorPhase.TRANSFERRING_TO_GENERATOR
                self.transaction = Transaction.begin(
                    "resume_generator_after_grid_failure",
                    self.desired_source.value,
                    now,
                    "transfer_to_generator",
                )
                self._event(
                    "warning",
                    "Grid снова пропала во время cooldown; возвращаем дом на генератор.",
                )
                return
            if generator.running is False and generator.phase == GeneratorPhase.IDLE:
                self._complete_transaction(now, "Генератор остановлен.")
                self._finish_session(observation)

    def _handle_manual_start(
        self, now: float, observation: SupervisorObservation
    ) -> None:
        if self.phase == SupervisorPhase.RECOVERY_REQUIRED:
            self._event(
                "warning",
                "Ручной ввод резерва невозможен до завершения recovery.",
            )
            return
        if self.phase == SupervisorPhase.EXTERNAL_RUNNING:
            self._event(
                "warning",
                "Ручной ввод резерва отклонён: внешний сеанс ещё не завершён.",
            )
            return
        if self.session is not None:
            self._event(
                "info",
                "Ручная команда запуска проигнорирована: сессия уже активна.",
            )
            return
        external = self._external_generators(observation)
        if external:
            names = ", ".join(observation.generators[s].display_name for s in external)
            self._event(
                "warning",
                f"Ручной ввод резерва отклонён: обнаружен внешний запуск ({names}).",
            )
            return
        self._begin_session(now, observation, SessionReason.MANUAL_BACKUP)
        if self.session is not None:
            self.automatic_start_suppressed_until_grid = False

    def _handle_manual_stop(
        self, now: float, observation: SupervisorObservation
    ) -> None:
        if self.session is None:
            if (
                self.phase == SupervisorPhase.EXTERNAL_RUNNING
                or self._external_generators(observation)
            ):
                self._event(
                    "info",
                    "Внешний сеанс не изменён: Supervisor им не управляет.",
                )
            else:
                self._event("info", "Управляемая генераторная сессия не активна.")
            return
        self.session.stop_requested = True
        if observation.grid_ready is False:
            self.automatic_start_suppressed_until_grid = True
        self._begin_return_to_safe_source(now, observation)

    def _begin_session(
        self,
        now: float,
        observation: SupervisorObservation,
        reason: SessionReason,
    ) -> None:
        slot = self._choose_generator(observation)
        if slot is None:
            self._event("warning", "Нет доступного генератора для новой сессии.")
            return
        self.session = GeneratorSession.begin(
            reason=reason,
            generator=slot,
            now=now,
            grid_was_unavailable=observation.grid_ready is False,
        )
        self.desired_generators[slot] = True
        other = GeneratorSlot.B if slot == GeneratorSlot.A else GeneratorSlot.A
        self.desired_generators[other] = False
        self.desired_source = self._safe_source_for_grid_state(
            observation.grid_ready
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

    def _begin_return_to_safe_source(
        self, now: float, observation: SupervisorObservation
    ) -> None:
        self.desired_source = self._safe_source_for_grid_state(
            observation.grid_ready
        )
        self.phase = SupervisorPhase.RETURNING_TO_GRID_OR_BATTERY
        self.transaction = Transaction.begin(
            "return_to_grid_or_battery",
            self.desired_source.value,
            now,
            "isolate_generator",
        )

    def _handle_generator_fault(
        self,
        now: float,
        observation: SupervisorObservation,
        reason: str,
        *,
        event_title: str = "Отказ генератора",
    ) -> None:
        assert self.session is not None
        slot = self.session.generator
        self.desired_source = self._safe_source_for_grid_state(
            observation.grid_ready
        )
        self.desired_generators[slot] = False
        self.phase = SupervisorPhase.ISOLATING_FAILED_SOURCE
        # Это новая физическая транзакция, а не просто финальный статус
        # предыдущего запуска. До подтверждённой изоляции она остаётся
        # IN_PROGRESS, поэтому потеря связи гарантированно блокирует автоматику.
        self.transaction = Transaction.begin(
            "isolate_failed_source", slot.value, now, "isolate_generator"
        )
        self.transaction.note(now, reason)
        self._event(
            "critical",
            f"{event_title}: {reason} Источник будет изолирован; "
            "автоматический запуск второго генератора отключён.",
        )

    def _maybe_warn_about_idle_generator(
        self, now: float, generator: GeneratorStatus
    ) -> None:
        if self.generator_idle_since is None:
            self.generator_idle_since = now
        if self.idle_warning_sent:
            return
        if now - self.generator_idle_since < self.config.manual_idle_warning_seconds:
            return
        self.idle_warning_sent = True
        self._event(
            "warning",
            f"{generator.display_name} продолжительное время работает без нагрузки. "
            "Выполните stop_generator, если генератор больше не нужен.",
        )

    def _cancel_automatic_return(
        self,
        now: float,
        observation: SupervisorObservation,
        generator: GeneratorStatus,
    ) -> None:
        """Корректно пережить повторный провал Grid во время возврата.

        Начатую силовую операцию не разворачиваем посередине. Если контакторы
        уже снимают генераторную шину, сначала подтверждаем Battery path, а на
        следующем шаге снова вводим готовый генератор.
        """
        slot = self.session.generator
        generator_source = PowerSource.for_generator(slot)
        if (
            observation.power.actual_source == generator_source
            and not observation.power.transition_in_progress
        ):
            self.desired_source = generator_source
            self.phase = SupervisorPhase.ON_GENERATOR
            self._complete_transaction(now, "Возврат отменён: Grid снова пропала.")
            return

        self.desired_source = PowerSource.BATTERY
        if not self._safe_power_path_confirmed(observation):
            return

        if not generator.ready_for_load:
            self._handle_generator_fault(
                now,
                observation,
                f"{generator.display_name} не готов после повторного провала Grid.",
            )
            return

        self.desired_source = generator_source
        self.phase = SupervisorPhase.TRANSFERRING_TO_GENERATOR
        self.transaction = Transaction.begin(
            "resume_generator_after_grid_failure",
            generator_source.value,
            now,
            "transfer_to_generator",
        )
        self._event(
            "warning",
            "Grid снова пропала во время возврата; повторно вводим генератор.",
        )

    def _finish_session(self, observation: SupervisorObservation) -> None:
        self.session = None
        self.desired_generators = {
            GeneratorSlot.A: False,
            GeneratorSlot.B: False,
        }
        self.desired_source = self._safe_source_for_grid_state(
            observation.grid_ready
        )
        self.phase = SupervisorPhase.NORMAL
        self.generator_idle_since = None
        self.idle_warning_sent = False
        self.grid_failed_since = None

    def _should_return_after_grid_restore(
        self, now: float, observation: SupervisorObservation
    ) -> bool:
        if self.session is None or not self.session.grid_was_unavailable:
            return False
        return (
            observation.grid_ready is True
            and self.grid_ready_since is not None
            and now - self.grid_ready_since >= self.config.grid_restore_stable_time
        )

    def _safe_power_path_confirmed(
        self, observation: SupervisorObservation
    ) -> bool:
        expected = self._safe_source_for_grid_state(observation.grid_ready)
        expected_path = PowerPath.for_source(expected)
        return (
            observation.power.actual_source == expected
            and observation.power.actual_path == expected_path
            and not observation.power.transition_in_progress
        )

    def _restored_session_matches_power(
        self, observation: SupervisorObservation
    ) -> bool:
        """Проверить steady-state ownership, не формируя физических команд."""
        assert self.session is not None
        if observation.power.transition_in_progress:
            return False
        if self.phase == SupervisorPhase.ON_GENERATOR:
            expected = PowerSource.for_generator(self.session.generator)
            return (
                observation.power.actual_source == expected
                and observation.power.actual_path == PowerPath.GENERATOR
            )
        if self.phase == SupervisorPhase.MANUAL_GENERATOR_IDLE:
            expected_path = PowerPath.for_source(observation.power.actual_source)
            return (
                observation.power.actual_source
                in {PowerSource.GRID, PowerSource.BATTERY}
                and observation.power.actual_path == expected_path
            )
        return False

    def _choose_generator(
        self, observation: SupervisorObservation
    ) -> GeneratorSlot | None:
        # Запуск второго агрегата запрещён, пока у любого генератора физически
        # активны RUNNING или REMOTE. Это независимая от выбора A/B блокировка.
        if any(
            status.running is True or status.remote_on is True
            for status in observation.generators.values()
        ):
            return None
        primary = self.config.primary_generator
        other = GeneratorSlot.B if primary == GeneratorSlot.A else GeneratorSlot.A
        for slot in (primary, other):
            status = observation.generators[slot]
            if not self.config.generator_enabled(slot):
                continue
            if status.externally_started or status.fault is not None:
                continue
            return slot
        return None

    def _external_generators(
        self, observation: SupervisorObservation
    ) -> list[GeneratorSlot]:
        return [
            slot
            for slot, status in observation.generators.items()
            if status.externally_started
        ]

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

    @staticmethod
    def _safe_source_for_grid_state(grid_ready: bool | None) -> PowerSource:
        if grid_ready is True:
            return PowerSource.GRID
        if grid_ready is False:
            return PowerSource.BATTERY
        return PowerSource.UNKNOWN

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
        self._event("critical", f"Требуется восстановление управления: {reason}")

    def _event(self, level: str, message: str, entity_id: str | None = None) -> None:
        self._events.append(SupervisorEvent(level, message, entity_id))

    def _decision(self, observation: SupervisorObservation) -> SupervisorDecision:
        events = tuple(self._events)
        self._events.clear()
        stable_managed = None
        if self.session is not None and self.phase in {
            SupervisorPhase.ON_GENERATOR,
            SupervisorPhase.MANUAL_GENERATOR_IDLE,
        }:
            stable_managed = self.session.generator
        return SupervisorDecision(
            desired_source=self.desired_source,
            desired_generators=dict(self.desired_generators),
            # Внешний/локальный запуск только отображается. Supervisor не
            # захватывает ни двигатель, ни силовую схему такого сеанса.
            actions_allowed=self.phase
            not in {
                SupervisorPhase.RECOVERY_REQUIRED,
                SupervisorPhase.EXTERNAL_RUNNING,
            }
            and observation.required_states_known,
            stable_managed_generator=stable_managed,
            events=events,
            status_text=self.status_text(observation),
        )

    def status_text(self, observation: SupervisorObservation) -> str:
        if self.phase == SupervisorPhase.RECOVERY_REQUIRED:
            return "Управление прервано — требуется восстановление"
        if not observation.required_states_known:
            return "Ожидание обязательных физических данных"
        if self.phase == SupervisorPhase.WAITING_FOR_DATA:
            return "Ожидание обязательных данных"
        if self.phase == SupervisorPhase.EXTERNAL_RUNNING:
            names = ", ".join(
                observation.generators[s].display_name
                for s in self._external_generators(observation)
            )
            if names:
                return f"Внешний запуск: {names}"
            return "Внешний запуск завершён — ожидается ручное возвращение схемы"
        if self.phase == SupervisorPhase.GRID_FAILURE_DELAY:
            return "Основная сеть OFF — выдержка перед запуском"
        if self.session is None:
            if observation.grid_ready is True:
                return "Питание от основной сети"
            if observation.grid_ready is False:
                if self.automatic_start_suppressed_until_grid:
                    return "Основная сеть OFF — автозапуск подавлен, UPS от МАП"
                return "Основная сеть OFF — UPS от МАП"
            return "Состояние основной сети неизвестно"

        generator_status = observation.generators[self.session.generator]
        generator = generator_status.display_name
        if (
            self.phase == SupervisorPhase.STARTING_GENERATOR
            and generator_status.phase == GeneratorPhase.WARMING_UP
        ):
            return f"Прогрев: {generator}"
        statuses = {
            SupervisorPhase.STARTING_GENERATOR: f"Запускается: {generator}",
            SupervisorPhase.TRANSFERRING_TO_GENERATOR: f"Переключение на {generator}",
            SupervisorPhase.ON_GENERATOR: f"Питание от генератора: {generator}",
            SupervisorPhase.RETURNING_TO_GRID_OR_BATTERY: (
                "Возврат на Grid path / Battery path"
            ),
            SupervisorPhase.MANUAL_GENERATOR_IDLE: f"{generator} работает без нагрузки",
            SupervisorPhase.STOPPING_GENERATOR: f"Охлаждение / остановка: {generator}",
            SupervisorPhase.ISOLATING_FAILED_SOURCE: "Изоляция отказавшего генератора",
        }
        return statuses.get(self.phase, self.phase.value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "phase": self.phase.value,
            "session": self.session.to_dict() if self.session else None,
            "transaction": self.transaction.to_dict() if self.transaction else None,
            "desired_source": self.desired_source.value,
            "desired_generators": {
                slot.value: desired
                for slot, desired in self.desired_generators.items()
            },
            "grid_failed_since": self.grid_failed_since,
            "grid_ready_since": self.grid_ready_since,
            "generator_idle_since": self.generator_idle_since,
            "idle_warning_sent": self.idle_warning_sent,
            "automatic_start_suppressed_until_grid": (
                self.automatic_start_suppressed_until_grid
            ),
            "recovery_reason": self.recovery_reason,
        }

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
        config: SupervisorConfig | None = None,
    ) -> "EnergySupervisor":
        schema_version = data.get("schema_version")
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise ValueError("Некорректная версия журнала Energy Supervisor")
        if schema_version != 1:
            raise ValueError("Неподдерживаемая версия журнала Energy Supervisor")
        supervisor = cls(config)
        saved_phase = str(data["phase"])
        # 0.3.0 называла этот же переход "returning_to_normal". Это известное
        # старое имя, поэтому журнал можно безопасно прочитать без угадывания
        # физического состояния; окончательная проверка всё равно выполняется
        # по обратным связям после запуска.
        if saved_phase == "returning_to_normal":
            saved_phase = SupervisorPhase.RETURNING_TO_GRID_OR_BATTERY.value
        supervisor.phase = SupervisorPhase(saved_phase)
        session = data.get("session")
        supervisor.session = (
            GeneratorSession.from_dict(session) if isinstance(session, Mapping) else None
        )
        transaction = data.get("transaction")
        supervisor.transaction = (
            Transaction.from_dict(dict(transaction))
            if isinstance(transaction, Mapping)
            else None
        )
        supervisor.desired_source = PowerSource(str(data["desired_source"]))
        desired = data.get("desired_generators") or {}
        if not isinstance(desired, Mapping):
            raise ValueError("desired_generators должен быть JSON object")
        supervisor.desired_generators = {
            GeneratorSlot.A: _strict_bool(
                desired.get("A", False),
                "desired_generators.A",
            ),
            GeneratorSlot.B: _strict_bool(
                desired.get("B", False),
                "desired_generators.B",
            ),
        }
        supervisor.grid_failed_since = _optional_float(data.get("grid_failed_since"))
        supervisor.grid_ready_since = _optional_float(data.get("grid_ready_since"))
        supervisor.generator_idle_since = _optional_float(
            data.get("generator_idle_since")
        )
        supervisor.idle_warning_sent = _strict_bool(
            data.get("idle_warning_sent", False),
            "idle_warning_sent",
        )
        supervisor.automatic_start_suppressed_until_grid = _strict_bool(
            data.get("automatic_start_suppressed_until_grid", False),
            "automatic_start_suppressed_until_grid",
        )
        recovery = data.get("recovery_reason")
        supervisor.recovery_reason = str(recovery) if recovery else None
        supervisor.initialized = False
        return supervisor


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _strict_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} должен быть JSON boolean")
    return value

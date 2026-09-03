"""Безопасное переключение силовой схемы Grid / Generator / МАП.

Этот модуль не решает, какой источник выгоднее и какой генератор запускать. Он
только приводит силовую схему к уже выбранной цели, всегда выполняя
break-before-make и ожидая физическое подтверждение каждого шага.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from domain import GeneratorSlot, PowerSource, Transaction


class TransferPhase(str, Enum):
    WAITING_FOR_DATA = "waiting_for_data"
    STABLE_GRID = "stable_grid"
    STABLE_BATTERY = "stable_battery"
    STABLE_GENERATOR = "stable_generator"
    DISCONNECTING_GRID = "disconnecting_grid"
    SELECTING_GENERATOR = "selecting_generator"
    DISCONNECTING_GENERATOR = "disconnecting_generator"
    CONNECTING_NORMAL_PATH = "connecting_normal_path"
    RECOVERY_REQUIRED = "recovery_required"


class TransferActionKind(str, Enum):
    CONNECT_GRID = "connect_grid"
    DISCONNECT_GRID = "disconnect_grid"
    SELECT_GENERATOR = "select_generator"
    DESELECT_GENERATOR = "deselect_generator"


@dataclass(frozen=True)
class TransferAction:
    kind: TransferActionKind
    message: str


@dataclass(frozen=True)
class PowerTransferObservation:
    grid_ready: bool | None
    house_on_grid: bool | None
    house_on_generator: bool | None
    grid_connected: bool | None
    generator_selected: bool | None
    active_generator: GeneratorSlot | None
    emergency_stop: bool | None

    @property
    def required_states_known(self) -> bool:
        return all(
            value is not None
            for value in (
                self.grid_ready,
                self.house_on_grid,
                self.house_on_generator,
                self.grid_connected,
                self.generator_selected,
                self.emergency_stop,
            )
        )


@dataclass(frozen=True)
class PowerTransferStatus:
    phase: TransferPhase
    actual_source: PowerSource
    target_source: PowerSource
    transition_in_progress: bool
    recovery_required: bool
    fault: str | None


class PowerTransferController:
    """Подтверждаемый автомат силовых контакторов."""

    _TRANSITION_PHASES = {
        TransferPhase.DISCONNECTING_GRID,
        TransferPhase.SELECTING_GENERATOR,
        TransferPhase.DISCONNECTING_GENERATOR,
        TransferPhase.CONNECTING_NORMAL_PATH,
    }

    def __init__(self, confirmation_timeout: float = 60.0) -> None:
        self.confirmation_timeout = confirmation_timeout
        self.phase = TransferPhase.WAITING_FOR_DATA
        self.actual_source = PowerSource.UNKNOWN
        self.target_source = PowerSource.UNKNOWN
        self.deadline: float | None = None
        self.feedback_lost_since: float | None = None
        self.fault: str | None = None
        self.transaction: Transaction | None = None
        self.initialized = False

    @property
    def transition_in_progress(self) -> bool:
        return self.phase in self._TRANSITION_PHASES

    def status(self) -> PowerTransferStatus:
        return PowerTransferStatus(
            phase=self.phase,
            actual_source=self.actual_source,
            target_source=self.target_source,
            transition_in_progress=self.transition_in_progress,
            recovery_required=self.phase == TransferPhase.RECOVERY_REQUIRED,
            fault=self.fault,
        )

    def mark_interrupted(self, now: float, reason: str) -> None:
        if self.transaction is not None and self.transition_in_progress:
            self.transaction.interrupt(now, reason)
            self.transaction.require_recovery(now, reason)
            self._require_recovery(reason)

    def request_recovery_reset(self, observation: PowerTransferObservation) -> bool:
        """Снять блокировку, только если физическая топология уже однозначна."""
        inferred = self._infer_stable_source(observation)
        if inferred == PowerSource.UNKNOWN:
            return False
        self._set_stable(inferred)
        self.fault = None
        self.transaction = None
        self.initialized = True
        return True

    def step(
        self,
        now: float,
        observation: PowerTransferObservation,
        desired_source: PowerSource,
        *,
        desired_generator_ready: bool,
        actions_allowed: bool = True,
    ) -> list[TransferAction]:
        if not observation.required_states_known:
            # После инициализации сохраняем последнюю подтверждённую фазу.
            # Потеря одного датчика не даёт права ни продолжать переход, ни
            # забывать уже подтверждённую физическую картину.
            if not self.initialized:
                self.phase = TransferPhase.WAITING_FOR_DATA
                self.actual_source = PowerSource.UNKNOWN
            return []

        if not self.initialized:
            inferred = self._infer_stable_source(observation)
            self.initialized = True
            if inferred == PowerSource.UNKNOWN:
                self._require_recovery(
                    "После запуска Power Transfer физическая топология неоднозначна."
                )
                return []
            self._set_stable(inferred)

        # Между нашими транзакциями положение мог изменить человек или другая
        # автоматика. В steady state доверяем физической обратной связи, а не
        # старому значению в памяти процесса. Само наблюдение команд не создаёт.
        self.target_source = desired_source

        if self._unsafe_overlap(observation):
            self._require_recovery(
                "Одновременно обнаружены несовместимые положения Grid и "
                "генераторного ввода."
            )
            return []

        if not self.transition_in_progress:
            inferred = self._infer_stable_source(observation)
            if inferred != PowerSource.UNKNOWN:
                self.feedback_lost_since = None
                if inferred != self.actual_source:
                    self._set_stable(inferred)
            elif (
                self.phase == TransferPhase.STABLE_GENERATOR
                and desired_source.generator is None
            ):
                # Если двигатель/обратная связь генератора уже пропали, всё
                # равно разрешаем только безопасное действие — изоляцию шины.
                self.feedback_lost_since = None
            else:
                if self.feedback_lost_since is None:
                    self.feedback_lost_since = now
                if (
                    actions_allowed
                    and now - self.feedback_lost_since
                    >= self.confirmation_timeout
                ):
                    self._require_recovery(
                        "Устойчивая силовая топология потеряла физическое "
                        "подтверждение."
                    )
                return []

        if not actions_allowed or self.phase == TransferPhase.RECOVERY_REQUIRED:
            return []

        if self._deadline_reached(now):
            self._require_recovery(
                f"Не получено подтверждение силового шага {self.phase.value} "
                f"за {int(self.confirmation_timeout)} с."
            )
            return []

        if desired_source.generator is not None:
            if observation.emergency_stop is True or not desired_generator_ready:
                # До подключения генератора просто ждём READY. Если он уже был
                # выбран, следующий Supervisor tick задаст безопасный normal target.
                return []
            return self._towards_generator(now, observation, desired_source)

        return self._towards_normal(now, observation, desired_source)

    def _towards_generator(
        self,
        now: float,
        observation: PowerTransferObservation,
        desired_source: PowerSource,
    ) -> list[TransferAction]:
        if self.phase == TransferPhase.STABLE_GENERATOR:
            # Общая генераторная шина физически одна. Slot уточняется по RUNNING.
            if observation.active_generator != desired_source.generator:
                self._require_recovery(
                    "На общей генераторной шине обнаружен не тот генератор, "
                    "который запросил Supervisor."
                )
                return []
            self.actual_source = desired_source
            return []

        if self.phase in {
            TransferPhase.STABLE_GRID,
            TransferPhase.STABLE_BATTERY,
        }:
            self.transaction = Transaction.begin(
                "transfer_to_generator", desired_source.value, now, "disconnect_grid"
            )
            self.phase = TransferPhase.DISCONNECTING_GRID
            self.deadline = now + self.confirmation_timeout
            return [TransferAction(
                TransferActionKind.DISCONNECT_GRID,
                "Перед вводом генератора отключаем Grid от силового переключателя.",
            )]

        if self.phase == TransferPhase.DISCONNECTING_GRID:
            if observation.grid_connected is False and observation.house_on_grid is False:
                if (
                    observation.generator_selected is not False
                    or observation.house_on_generator is not False
                ):
                    self._require_recovery(
                        "Генераторный ввод изменился до команды его выбора."
                    )
                    return []
                assert self.transaction is not None
                self.transaction.advance(
                    "select_generator", now, confirmed="grid_disconnected"
                )
                self.phase = TransferPhase.SELECTING_GENERATOR
                self.deadline = now + self.confirmation_timeout
                return [TransferAction(
                    TransferActionKind.SELECT_GENERATOR,
                    "Grid подтверждённо отключён; выбираем генераторную шину.",
                )]
            return []

        if self.phase == TransferPhase.SELECTING_GENERATOR:
            if (
                observation.generator_selected is True
                and observation.house_on_generator is True
            ):
                if (
                    observation.grid_connected is not False
                    or observation.house_on_grid is not False
                    or observation.active_generator != desired_source.generator
                ):
                    self._require_recovery(
                        "Генераторный ввод включён без полного безопасного "
                        "подтверждения выбранного агрегата."
                    )
                    return []
                self._complete_transition(now, desired_source)
            return []

        # Сначала заканчиваем уже начатое безопасное возвращение в normal path.
        return []

    def _towards_normal(
        self,
        now: float,
        observation: PowerTransferObservation,
        desired_source: PowerSource,
    ) -> list[TransferAction]:
        if desired_source == PowerSource.GRID and observation.grid_ready is not True:
            return []

        if self.phase in {
            TransferPhase.STABLE_GRID,
            TransferPhase.STABLE_BATTERY,
        }:
            inferred = self._infer_stable_source(observation)
            if inferred == desired_source:
                self._set_stable(inferred)
            elif inferred in {PowerSource.GRID, PowerSource.BATTERY}:
                # Grid появился/исчез без движения контакторов: это не силовая
                # транзакция, меняется только фактический источник МАП.
                self._set_stable(inferred)
            return []

        if self.phase in {
            TransferPhase.STABLE_GENERATOR,
            TransferPhase.DISCONNECTING_GRID,
            TransferPhase.SELECTING_GENERATOR,
        }:
            self.transaction = Transaction.begin(
                "return_to_normal", desired_source.value, now, "deselect_generator"
            )
            self.phase = TransferPhase.DISCONNECTING_GENERATOR
            self.deadline = now + self.confirmation_timeout
            return [TransferAction(
                TransferActionKind.DESELECT_GENERATOR,
                "Отключаем дом от генераторной шины до подключения normal path.",
            )]

        if self.phase == TransferPhase.DISCONNECTING_GENERATOR:
            if (
                observation.grid_connected is True
                and observation.generator_selected is True
            ):
                self._require_recovery(
                    "Grid подключён до изоляции генераторного ввода."
                )
                return []
            if (
                observation.generator_selected is False
                and observation.house_on_generator is False
            ):
                assert self.transaction is not None
                self.transaction.advance(
                    "connect_normal_path", now, confirmed="generator_disconnected"
                )
                self.phase = TransferPhase.CONNECTING_NORMAL_PATH
                self.deadline = now + self.confirmation_timeout
                return [TransferAction(
                    TransferActionKind.CONNECT_GRID,
                    "Генераторная шина изолирована; подключаем normal path Grid/МАП.",
                )]
            return []

        if self.phase == TransferPhase.CONNECTING_NORMAL_PATH:
            if self._normal_target_confirmed(observation, desired_source):
                self._complete_transition(now, desired_source)
            return []

        return []

    def _normal_target_confirmed(
        self,
        observation: PowerTransferObservation,
        desired_source: PowerSource,
    ) -> bool:
        if observation.grid_connected is not True:
            return False
        if observation.generator_selected is not False:
            return False
        if observation.house_on_generator is not False:
            return False
        if desired_source == PowerSource.GRID:
            return observation.grid_ready is True and observation.house_on_grid is True
        return observation.grid_ready is False and observation.house_on_grid is False

    def _infer_stable_source(
        self, observation: PowerTransferObservation
    ) -> PowerSource:
        if not observation.required_states_known:
            return PowerSource.UNKNOWN
        if observation.house_on_grid is True and observation.house_on_generator is True:
            return PowerSource.UNKNOWN

        if (
            observation.generator_selected is True
            and observation.grid_connected is False
            and observation.house_on_generator is True
            and observation.house_on_grid is False
            and observation.active_generator is not None
        ):
            return PowerSource.for_generator(observation.active_generator)

        if (
            observation.generator_selected is False
            and observation.grid_connected is True
            and observation.house_on_generator is False
        ):
            if observation.grid_ready is True and observation.house_on_grid is True:
                return PowerSource.GRID
            if observation.grid_ready is False and observation.house_on_grid is False:
                return PowerSource.BATTERY

        return PowerSource.UNKNOWN

    @staticmethod
    def _unsafe_overlap(observation: PowerTransferObservation) -> bool:
        return (
            observation.house_on_grid is True
            and observation.house_on_generator is True
        ) or (
            observation.grid_connected is True
            and observation.generator_selected is True
        )

    def _complete_transition(self, now: float, source: PowerSource) -> None:
        if self.transaction is not None:
            self.transaction.complete(now, f"Источник {source.value} подтверждён.")
        self._set_stable(source)

    def _set_stable(self, source: PowerSource) -> None:
        self.actual_source = source
        self.target_source = source
        self.deadline = None
        self.feedback_lost_since = None
        if source == PowerSource.GRID:
            self.phase = TransferPhase.STABLE_GRID
        elif source == PowerSource.BATTERY:
            self.phase = TransferPhase.STABLE_BATTERY
        else:
            self.phase = TransferPhase.STABLE_GENERATOR

    def _require_recovery(self, reason: str) -> None:
        self.phase = TransferPhase.RECOVERY_REQUIRED
        self.actual_source = PowerSource.UNKNOWN
        self.deadline = None
        self.feedback_lost_since = None
        self.fault = reason

    def _deadline_reached(self, now: float) -> bool:
        return (
            self.transition_in_progress
            and self.deadline is not None
            # Подтверждение, пришедшее ровно на границе timeout, ещё принимаем.
            # Если состояния не изменились, следующий tick зафиксирует timeout.
            and now > self.deadline
        )

"""Управление основными контакторами Grid / общей генераторной шины.

TPC ничего не знает о Generator A/B: аппаратная схема сама выбирает владельца
генераторной шины. Каждый вызов выдаёт не более одной силовой команды и всегда
ждёт физического подтверждения предыдущего шага.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from domain import PowerPath, PowerSource


class TransferPhase(str, Enum):
    WAITING_FOR_DATA = "waiting_for_data"
    STABLE_GRID = "stable_grid"
    STABLE_ISOLATED = "stable_isolated"
    STABLE_GENERATOR = "stable_generator"
    DISCONNECTING_GRID = "disconnecting_grid"
    SELECTING_GENERATOR = "selecting_generator"
    DISCONNECTING_GENERATOR = "disconnecting_generator"
    CONNECTING_GRID = "connecting_grid"
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
    actual_path: PowerPath
    target_source: PowerSource | None
    transition_in_progress: bool
    recovery_required: bool
    fault: str | None


_TRANSITIONS = {
    TransferPhase.DISCONNECTING_GRID,
    TransferPhase.SELECTING_GENERATOR,
    TransferPhase.DISCONNECTING_GENERATOR,
    TransferPhase.CONNECTING_GRID,
}


class PowerTransferController:
    """Подтверждаемый break-before-make автомат основных контакторов."""

    def __init__(self, confirmation_timeout: float = 60.0) -> None:
        self.confirmation_timeout = confirmation_timeout
        self.phase = TransferPhase.WAITING_FOR_DATA
        self.actual_source = PowerSource.UNKNOWN
        self.actual_path = PowerPath.UNKNOWN
        self.target_source: PowerSource | None = None
        self.deadline: float | None = None
        self.feedback_lost_since: float | None = None
        self.fault: str | None = None
        self.initialized = False

    @property
    def transition_in_progress(self) -> bool:
        return self.phase in _TRANSITIONS

    def status(self) -> PowerTransferStatus:
        return PowerTransferStatus(
            phase=self.phase,
            actual_source=self.actual_source,
            actual_path=self.actual_path,
            target_source=self.target_source,
            transition_in_progress=self.transition_in_progress,
            recovery_required=self.phase == TransferPhase.RECOVERY_REQUIRED,
            fault=self.fault,
        )

    def mark_interrupted(self, _now: float, reason: str) -> None:
        if self.transition_in_progress:
            self._require_recovery(reason)

    # Normal operation -------------------------------------------------

    def step(
        self,
        now: float,
        observation: PowerTransferObservation,
        desired_source: PowerSource | None,
        *,
        desired_generator_ready: bool,
        actions_allowed: bool = True,
    ) -> list[TransferAction]:
        if not observation.required_states_known:
            if not self.initialized:
                self._set_unknown(TransferPhase.WAITING_FOR_DATA)
            return []

        if self._unsafe_overlap(observation):
            self._require_recovery(
                "Одновременно обнаружены несовместимые сетевой и генераторный вводы."
            )
            return []

        if not self.initialized:
            self.initialized = True
            topology = self._infer_topology(observation)
            if topology is None:
                self._require_recovery(
                    "После запуска Power Transfer физическая топология неоднозначна."
                )
                return []
            self._set_stable(*topology)

        self.target_source = desired_source
        if self.phase == TransferPhase.RECOVERY_REQUIRED:
            self._observe_only(observation)
            return []

        if self.transition_in_progress:
            if self._timed_out(now):
                self._require_recovery(
                    f"Не получено подтверждение силового шага {self.phase.value} "
                    f"за {int(self.confirmation_timeout)} с."
                )
                return []
            if not self._settle_transition(observation):
                return []

        topology = self._infer_topology(observation)
        if topology is None:
            if self.feedback_lost_since is None:
                self.feedback_lost_since = now
            elif now - self.feedback_lost_since >= self.confirmation_timeout:
                self._require_recovery(
                    "Устойчивая силовая топология потеряла подтверждение."
                )
            return []

        self.feedback_lost_since = None
        self._set_stable(*topology)
        if desired_source is None or not actions_allowed:
            return []
        return self._drive(now, observation, desired_source, desired_generator_ready)

    def _drive(
        self,
        now: float,
        observation: PowerTransferObservation,
        desired: PowerSource,
        generator_ready: bool,
    ) -> list[TransferAction]:
        if desired == PowerSource.GENERATOR:
            if observation.emergency_stop is not False or not generator_ready:
                return []
            if self.actual_path == PowerPath.GRID:
                return self._begin(now, TransferActionKind.DISCONNECT_GRID)
            if self.actual_path == PowerPath.ISOLATED:
                return self._begin(now, TransferActionKind.SELECT_GENERATOR)
            if self.actual_path == PowerPath.GENERATOR and observation.grid_connected is True:
                return self._begin(now, TransferActionKind.DISCONNECT_GRID)
            return []

        if desired in {PowerSource.UPS_ONLY, PowerSource.NO_POWER}:
            if self.actual_path == PowerPath.GRID:
                return self._begin(now, TransferActionKind.DISCONNECT_GRID)
            if self.actual_path == PowerPath.GENERATOR:
                return self._begin(now, TransferActionKind.DESELECT_GENERATOR)
            return []

        if desired == PowerSource.GRID:
            if self.actual_path == PowerPath.GENERATOR:
                return self._begin(now, TransferActionKind.DESELECT_GENERATOR)
            if self.actual_path == PowerPath.ISOLATED:
                return self._begin(now, TransferActionKind.CONNECT_GRID)
        return []

    def _settle_transition(self, observation: PowerTransferObservation) -> bool:
        """Закончить текущий физический шаг; следующий выбирается заново."""

        if self.phase == TransferPhase.DISCONNECTING_GRID:
            confirmed = (
                observation.grid_connected is False
                and observation.house_on_grid is False
            )
        elif self.phase == TransferPhase.SELECTING_GENERATOR:
            if observation.grid_connected is not False or observation.house_on_grid is not False:
                return False
            confirmed = (
                observation.generator_selected is True
                and observation.house_on_generator is True
            )
        elif self.phase == TransferPhase.DISCONNECTING_GENERATOR:
            confirmed = (
                observation.generator_selected is False
                and observation.house_on_generator is False
            )
        else:  # CONNECTING_GRID
            if observation.generator_selected is not False or observation.house_on_generator is not False:
                return False
            topology = self._infer_topology(observation)
            if topology is None or topology[0] != PowerPath.GRID:
                return False
            self._set_stable(*topology)
            return True

        if not confirmed:
            return False

        topology = self._infer_topology(observation)
        if topology is not None:
            self._set_stable(*topology)
        else:
            # После снятия одной ветви допустимо короткое неподтверждённое
            # промежуточное состояние. Оно будет проверяться общим timeout.
            self.deadline = None
        return True

    # Recovery ---------------------------------------------------------

    def begin_recovery_to_grid_path(self) -> None:
        self._set_unknown(TransferPhase.RECOVERY_REQUIRED)
        self.target_source = PowerSource.GRID
        self.fault = None
        self.initialized = True

    def recovery_blocker(self, observation: PowerTransferObservation) -> str | None:
        if not observation.required_states_known:
            return "Неизвестны обязательные состояния основных контакторов."
        if observation.emergency_stop is not False:
            return "Активен Generators Emergency Stop."
        if self._unsafe_overlap(observation):
            return "Одновременно обнаружены признаки сетевой и генераторной ветвей."
        return None

    def step_recovery_to_grid_path(
        self,
        now: float,
        observation: PowerTransferObservation,
    ) -> tuple[list[TransferAction], str | None]:
        blocker = self.recovery_blocker(observation)
        if blocker is not None:
            return [], blocker

        if self.transition_in_progress:
            if self._timed_out(now):
                reason = (
                    "Не получено подтверждение шага восстановления "
                    f"{self.phase.value} за {int(self.confirmation_timeout)} с."
                )
                self._require_recovery(reason)
                return [], reason
            if not self._settle_transition(observation):
                return [], None

        topology = self._infer_topology(observation)
        if topology is not None:
            self._set_stable(*topology)
            if topology[0] == PowerPath.GRID:
                return [], None

        actions = self._drive(
            now,
            observation,
            PowerSource.GRID,
            generator_ready=False,
        )
        return actions, None

    def request_recovery_reset(self, observation: PowerTransferObservation) -> bool:
        topology = self._infer_topology(observation)
        if topology is None or topology[0] != PowerPath.GRID:
            return False
        self._set_stable(*topology)
        self.target_source = topology[1]
        self.fault = None
        self.initialized = True
        return True

    # Physical interpretation -----------------------------------------

    def _infer_topology(
        self,
        o: PowerTransferObservation,
    ) -> tuple[PowerPath, PowerSource] | None:
        if not o.required_states_known or self._unsafe_overlap(o):
            return None

        if o.generator_selected is True:
            if o.house_on_grid is True or o.house_on_generator is not True:
                return None
            return PowerPath.GENERATOR, PowerSource.GENERATOR
        if o.house_on_generator is True:
            return None

        if o.grid_connected is True:
            if o.grid_ready is True and o.house_on_grid is True:
                return PowerPath.GRID, PowerSource.GRID
            if o.grid_ready is False and o.house_on_grid is False:
                return PowerPath.GRID, PowerSource.UPS_ONLY
            return None

        if o.house_on_grid is False:
            return PowerPath.ISOLATED, PowerSource.UPS_ONLY
        return None

    @staticmethod
    def _unsafe_overlap(o: PowerTransferObservation) -> bool:
        return o.house_on_grid is True and o.house_on_generator is True

    def _observe_only(self, observation: PowerTransferObservation) -> None:
        topology = self._infer_topology(observation)
        if topology is not None:
            self.actual_path, self.actual_source = topology

    def _set_stable(self, path: PowerPath, source: PowerSource) -> None:
        self.actual_path = path
        self.actual_source = source
        self.phase = {
            PowerPath.GRID: TransferPhase.STABLE_GRID,
            PowerPath.ISOLATED: TransferPhase.STABLE_ISOLATED,
            PowerPath.GENERATOR: TransferPhase.STABLE_GENERATOR,
        }.get(path, TransferPhase.WAITING_FOR_DATA)
        self.deadline = None
        self.feedback_lost_since = None
        self.fault = None

    def _set_unknown(self, phase: TransferPhase) -> None:
        self.phase = phase
        self.actual_source = PowerSource.UNKNOWN
        self.actual_path = PowerPath.UNKNOWN
        self.deadline = None
        self.feedback_lost_since = None

    def _require_recovery(self, reason: str) -> None:
        self._set_unknown(TransferPhase.RECOVERY_REQUIRED)
        self.fault = reason

    def _begin(self, now: float, kind: TransferActionKind) -> list[TransferAction]:
        phase, message = {
            TransferActionKind.DISCONNECT_GRID: (
                TransferPhase.DISCONNECTING_GRID,
                "Отключаем разрешение Grid перед изменением источника дома.",
            ),
            TransferActionKind.SELECT_GENERATOR: (
                TransferPhase.SELECTING_GENERATOR,
                "Переключаем основные контакторы на генераторную шину.",
            ),
            TransferActionKind.DESELECT_GENERATOR: (
                TransferPhase.DISCONNECTING_GENERATOR,
                "Снимаем дом с генераторной шины.",
            ),
            TransferActionKind.CONNECT_GRID: (
                TransferPhase.CONNECTING_GRID,
                "Разрешаем сетевую ветвь после снятия генераторной.",
            ),
        }[kind]
        self.phase = phase
        self.deadline = now + self.confirmation_timeout
        return [TransferAction(kind, message)]

    def _timed_out(self, now: float) -> bool:
        return self.deadline is not None and now >= self.deadline

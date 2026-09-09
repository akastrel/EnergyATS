"""Подтверждаемое управление основными контакторами Сеть / Генератор.

TPC знает только реально существующую силовую схему:

* ``switch.grid_power`` — разрешение сетевой ветви;
* ``switch.use_generator_as_power_source`` — выбор Сеть / генераторная шина.

Какой именно Generator A/B владеет общей генераторной шиной, TPC не знает и
не должен знать. Это отдельная наблюдаемая модель ``GeneratorBusTracker``.
Отдельного Battery contactor или Battery path в системе нет.
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
class PowerTopology:
    path: PowerPath
    source: PowerSource


@dataclass(frozen=True)
class PowerTransferStatus:
    phase: TransferPhase
    actual_source: PowerSource
    actual_path: PowerPath
    target_source: PowerSource | None
    transition_in_progress: bool
    recovery_required: bool
    fault: str | None
    failed_phase: TransferPhase | None = None


class PowerTransferController:
    """Автомат основных контакторов; максимум один физический шаг за tick."""

    _TRANSITION_PHASES = {
        TransferPhase.DISCONNECTING_GRID,
        TransferPhase.SELECTING_GENERATOR,
        TransferPhase.DISCONNECTING_GENERATOR,
        TransferPhase.CONNECTING_GRID,
    }

    def __init__(self, confirmation_timeout: float = 60.0) -> None:
        self.confirmation_timeout = confirmation_timeout
        self.phase = TransferPhase.WAITING_FOR_DATA
        self.actual_source = PowerSource.UNKNOWN
        self.actual_path = PowerPath.UNKNOWN
        self.target_source: PowerSource | None = None
        self.deadline: float | None = None
        self.feedback_lost_since: float | None = None
        self.fault: str | None = None
        self.failed_phase: TransferPhase | None = None
        self.initialized = False

    @property
    def transition_in_progress(self) -> bool:
        return self.phase in self._TRANSITION_PHASES

    def status(self) -> PowerTransferStatus:
        return PowerTransferStatus(
            phase=self.phase,
            actual_source=self.actual_source,
            actual_path=self.actual_path,
            target_source=self.target_source,
            transition_in_progress=self.transition_in_progress,
            recovery_required=self.phase == TransferPhase.RECOVERY_REQUIRED,
            fault=self.fault,
            failed_phase=self.failed_phase,
        )

    def mark_interrupted(self, _now: float, reason: str) -> None:
        if self.transition_in_progress:
            self._require_recovery(reason)

    # ------------------------------------------------------------------
    # Recovery: только к безопасной сетевой стороне.
    # ------------------------------------------------------------------

    def begin_recovery_to_grid_path(self) -> None:
        self.phase = TransferPhase.RECOVERY_REQUIRED
        self.actual_source = PowerSource.UNKNOWN
        self.actual_path = PowerPath.UNKNOWN
        self.target_source = PowerSource.GRID
        self.deadline = None
        self.feedback_lost_since = None
        self.fault = None
        self.failed_phase = None
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

        if self._deadline_reached(now):
            reason = (
                "Не получено подтверждение шага восстановления "
                f"{self.phase.value} за {int(self.confirmation_timeout)} с."
            )
            self._require_recovery(reason)
            return [], reason

        if self.phase == TransferPhase.DISCONNECTING_GENERATOR:
            if (
                observation.generator_selected is not False
                or observation.house_on_generator is not False
            ):
                return [], None
            if observation.grid_connected is True:
                self._begin_wait_for_grid_confirmation(now)
                return [], None
            return self._begin_connect_grid(now), None

        if self.phase == TransferPhase.CONNECTING_GRID:
            if (
                observation.generator_selected is not False
                or observation.house_on_generator is not False
            ):
                reason = "Генераторная ветвь появилась во время recovery Grid."
                self._require_recovery(reason)
                return [], reason
            topology = self._infer_stable_topology(observation)
            if topology is not None and topology.path == PowerPath.GRID:
                self._set_stable(topology)
            return [], None

        topology = self._infer_stable_topology(observation)
        if topology is not None and topology.path == PowerPath.GRID:
            self._set_stable(topology)
            return [], None

        if (
            observation.generator_selected is True
            or observation.house_on_generator is True
        ):
            return self._begin_deselect_generator(now, PowerSource.GRID), None

        if observation.grid_connected is True:
            self._begin_wait_for_grid_confirmation(now)
            return [], None

        return self._begin_connect_grid(now), None

    def request_recovery_reset(self, observation: PowerTransferObservation) -> bool:
        topology = self._infer_stable_topology(observation)
        if topology is None or topology.path != PowerPath.GRID:
            return False
        self._set_stable(topology)
        self.target_source = topology.source
        self.fault = None
        self.failed_phase = None
        self.initialized = True
        return True

    # ------------------------------------------------------------------
    # Обычный автомат.
    # ------------------------------------------------------------------

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
                self.phase = TransferPhase.WAITING_FOR_DATA
                self.actual_source = PowerSource.UNKNOWN
                self.actual_path = PowerPath.UNKNOWN
            return []

        if not self.initialized:
            topology = self._infer_stable_topology(observation)
            self.initialized = True
            if topology is None:
                self._require_recovery(
                    "После запуска Power Transfer физическая топология неоднозначна."
                )
                return []
            self._set_stable(topology)

        self.target_source = desired_source

        if self._unsafe_overlap(observation):
            self._require_recovery(
                "Одновременно обнаружены несовместимые сетевой и генераторный вводы."
            )
            return []

        if self.phase == TransferPhase.RECOVERY_REQUIRED:
            topology = self._infer_stable_topology(observation)
            if topology is not None:
                self.actual_source = topology.source
                self.actual_path = topology.path
            return []

        if self._deadline_reached(now):
            self._require_recovery(
                f"Не получено подтверждение силового шага {self.phase.value} "
                f"за {int(self.confirmation_timeout)} с."
            )
            return []

        if self.transition_in_progress:
            if not actions_allowed:
                return []
            return self._continue_transition(
                now,
                observation,
                desired_source,
                desired_generator_ready,
            )

        topology = self._infer_stable_topology(observation)
        if topology is None:
            if self.feedback_lost_since is None:
                self.feedback_lost_since = now
            if now - self.feedback_lost_since >= self.confirmation_timeout:
                self._require_recovery(
                    "Устойчивая силовая топология потеряла подтверждение."
                )
            return []

        self.feedback_lost_since = None
        self._set_stable(topology)

        if desired_source is None or not actions_allowed:
            return []
        return self._start_towards_target(
            now,
            observation,
            desired_source,
            desired_generator_ready,
        )

    def _start_towards_target(
        self,
        now: float,
        observation: PowerTransferObservation,
        desired_source: PowerSource,
        desired_generator_ready: bool,
    ) -> list[TransferAction]:
        if desired_source == PowerSource.UNKNOWN:
            return []

        if desired_source == PowerSource.GENERATOR:
            if observation.emergency_stop is True or not desired_generator_ready:
                return []
            if self.actual_path == PowerPath.GRID:
                return self._begin_disconnect_grid(now, PowerSource.GENERATOR)
            if self.actual_path == PowerPath.ISOLATED:
                return self._begin_select_generator(now)
            if self.actual_path == PowerPath.GENERATOR:
                if observation.grid_connected is True:
                    return self._begin_disconnect_grid(now, PowerSource.GENERATOR)
                return []
            return []

        if desired_source in {PowerSource.UPS_ONLY, PowerSource.NO_POWER}:
            if self.actual_path == PowerPath.GRID:
                return self._begin_disconnect_grid(now, PowerSource.UPS_ONLY)
            if self.actual_path == PowerPath.GENERATOR:
                return self._begin_deselect_generator(now, PowerSource.UPS_ONLY)
            return []

        if desired_source == PowerSource.GRID:
            if self.actual_path == PowerPath.GENERATOR:
                return self._begin_deselect_generator(now, PowerSource.GRID)
            if self.actual_path == PowerPath.ISOLATED:
                return self._begin_connect_grid(now)
        return []

    def _continue_transition(
        self,
        now: float,
        observation: PowerTransferObservation,
        desired_source: PowerSource | None,
        desired_generator_ready: bool,
    ) -> list[TransferAction]:
        target = desired_source or self.target_source
        if target is None:
            self._require_recovery("Во время силовой транзакции потеряна цель Supervisor.")
            return []

        if self.phase == TransferPhase.DISCONNECTING_GRID:
            if (
                observation.grid_connected is not False
                or observation.house_on_grid is not False
            ):
                return []
            if target == PowerSource.GENERATOR:
                if not desired_generator_ready:
                    return []
                return self._begin_select_generator(now)
            if target in {PowerSource.UPS_ONLY, PowerSource.NO_POWER}:
                self._set_stable(
                    PowerTopology(PowerPath.ISOLATED, PowerSource.UPS_ONLY)
                )
                return []
            if target == PowerSource.GRID:
                return self._begin_connect_grid(now)
            return []

        if self.phase == TransferPhase.SELECTING_GENERATOR:
            if (
                observation.grid_connected is not False
                or observation.house_on_grid is not False
            ):
                self._require_recovery(
                    "Grid появилась во время подключения генераторной ветви."
                )
                return []
            if (
                observation.generator_selected is True
                and observation.house_on_generator is True
            ):
                self._set_stable(
                    PowerTopology(PowerPath.GENERATOR, PowerSource.GENERATOR)
                )
            return []

        if self.phase == TransferPhase.DISCONNECTING_GENERATOR:
            if (
                observation.generator_selected is not False
                or observation.house_on_generator is not False
            ):
                return []
            if target == PowerSource.GRID:
                if observation.grid_connected is True:
                    self._begin_wait_for_grid_confirmation(now)
                    return []
                return self._begin_connect_grid(now)
            if target in {PowerSource.UPS_ONLY, PowerSource.NO_POWER}:
                if observation.grid_connected is True:
                    return self._begin_disconnect_grid(now, PowerSource.UPS_ONLY)
                self._set_stable(
                    PowerTopology(PowerPath.ISOLATED, PowerSource.UPS_ONLY)
                )
            return []

        if self.phase == TransferPhase.CONNECTING_GRID:
            if (
                observation.generator_selected is not False
                or observation.house_on_generator is not False
            ):
                self._require_recovery(
                    "Генераторная ветвь появилась при подключении Grid."
                )
                return []
            topology = self._infer_stable_topology(observation)
            if topology is not None and topology.path == PowerPath.GRID:
                self._set_stable(topology)
            return []

        return []

    # ------------------------------------------------------------------
    # Начало физических шагов.
    # ------------------------------------------------------------------

    def _begin_disconnect_grid(
        self,
        now: float,
        target: PowerSource,
    ) -> list[TransferAction]:
        self.phase = TransferPhase.DISCONNECTING_GRID
        self.target_source = target
        self.deadline = now + self.confirmation_timeout
        return [
            TransferAction(
                TransferActionKind.DISCONNECT_GRID,
                "Отключаем разрешение Grid перед изменением источника дома.",
            )
        ]

    def _begin_select_generator(self, now: float) -> list[TransferAction]:
        self.phase = TransferPhase.SELECTING_GENERATOR
        self.target_source = PowerSource.GENERATOR
        self.deadline = now + self.confirmation_timeout
        return [
            TransferAction(
                TransferActionKind.SELECT_GENERATOR,
                "Переключаем основные контакторы на генераторную шину.",
            )
        ]

    def _begin_deselect_generator(
        self,
        now: float,
        target: PowerSource,
    ) -> list[TransferAction]:
        self.phase = TransferPhase.DISCONNECTING_GENERATOR
        self.target_source = target
        self.deadline = now + self.confirmation_timeout
        return [
            TransferAction(
                TransferActionKind.DESELECT_GENERATOR,
                "Снимаем дом с генераторной шины.",
            )
        ]

    def _begin_connect_grid(self, now: float) -> list[TransferAction]:
        self._begin_wait_for_grid_confirmation(now)
        return [
            TransferAction(
                TransferActionKind.CONNECT_GRID,
                "Разрешаем сетевую ветвь после снятия генераторной.",
            )
        ]

    def _begin_wait_for_grid_confirmation(self, now: float) -> None:
        self.phase = TransferPhase.CONNECTING_GRID
        self.target_source = PowerSource.GRID
        self.deadline = now + self.confirmation_timeout

    # ------------------------------------------------------------------
    # Интерпретация физических обратных связей.
    # ------------------------------------------------------------------

    def _infer_stable_topology(
        self,
        observation: PowerTransferObservation,
    ) -> PowerTopology | None:
        if not observation.required_states_known or self._unsafe_overlap(observation):
            return None

        if observation.generator_selected is True:
            if observation.house_on_grid is True:
                return None
            if observation.house_on_generator is not True:
                return None
            return PowerTopology(PowerPath.GENERATOR, PowerSource.GENERATOR)

        if observation.house_on_generator is True:
            return None

        if observation.grid_connected is True:
            if observation.grid_ready is True and observation.house_on_grid is True:
                return PowerTopology(PowerPath.GRID, PowerSource.GRID)
            if observation.grid_ready is False and observation.house_on_grid is False:
                return PowerTopology(PowerPath.GRID, PowerSource.UPS_ONLY)
            return None

        if observation.house_on_grid is not False:
            return None
        return PowerTopology(PowerPath.ISOLATED, PowerSource.UPS_ONLY)

    @staticmethod
    def _unsafe_overlap(observation: PowerTransferObservation) -> bool:
        return (
            observation.house_on_grid is True
            and observation.house_on_generator is True
        )

    def _set_stable(self, topology: PowerTopology) -> None:
        self.actual_path = topology.path
        self.actual_source = topology.source
        self.deadline = None
        self.feedback_lost_since = None
        if topology.path == PowerPath.GRID:
            self.phase = TransferPhase.STABLE_GRID
        elif topology.path == PowerPath.ISOLATED:
            self.phase = TransferPhase.STABLE_ISOLATED
        elif topology.path == PowerPath.GENERATOR:
            self.phase = TransferPhase.STABLE_GENERATOR
        else:
            self.phase = TransferPhase.WAITING_FOR_DATA

    def _require_recovery(self, reason: str) -> None:
        if self.phase != TransferPhase.RECOVERY_REQUIRED:
            self.failed_phase = self.phase
        self.phase = TransferPhase.RECOVERY_REQUIRED
        self.actual_source = PowerSource.UNKNOWN
        self.actual_path = PowerPath.UNKNOWN
        self.deadline = None
        self.feedback_lost_since = None
        self.fault = reason

    def _deadline_reached(self, now: float) -> bool:
        return self.deadline is not None and now >= self.deadline

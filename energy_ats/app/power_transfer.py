"""Безопасное переключение силовой схемы Grid / Generator / МАП.

Источник энергии и положение силового переключателя — не одно и то же.
Например, при пропавшей Grid МАП питает дом от аккумуляторов, хотя ввод Grid
может оставаться подключённым. Поэтому контроллер отдельно хранит:

* ``actual_source`` — откуда сейчас должен получать энергию дом;
* ``actual_path`` — какое физическое положение подтверждено обратными связями.

Модуль не решает, какой источник нужен дому. Он только безопасно приводит
контакторы к цели Energy Supervisor, всегда соблюдая break-before-make.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from domain import GeneratorSlot, PowerPath, PowerSource, Transaction


class TransferPhase(str, Enum):
    WAITING_FOR_DATA = "waiting_for_data"
    STABLE_GRID_PATH = "stable_grid_path"
    STABLE_BATTERY_PATH = "stable_battery_path"
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
class PowerTopology:
    """Однозначно распознанные путь и фактический источник."""

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
    last_confirmed_source: PowerSource = PowerSource.UNKNOWN
    last_confirmed_path: PowerPath = PowerPath.UNKNOWN


class PowerTransferController:
    """Подтверждаемый автомат силовых контакторов."""

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
        self.transaction: Transaction | None = None
        self.commanded_generator: GeneratorSlot | None = None
        self.initialized = False
        self.failed_phase: TransferPhase | None = None
        self.last_confirmed_source = PowerSource.UNKNOWN
        self.last_confirmed_path = PowerPath.UNKNOWN

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
            last_confirmed_source=self.last_confirmed_source,
            last_confirmed_path=self.last_confirmed_path,
        )

    def mark_interrupted(self, now: float, reason: str) -> None:
        if self.transaction is not None and self.transition_in_progress:
            self.transaction.interrupt(now, reason)
            self.transaction.require_recovery(now, reason)
            self._require_recovery(reason)

    def request_recovery_reset(self, observation: PowerTransferObservation) -> bool:
        """Снять локальную блокировку только из подтверждённого Grid path."""
        topology = self._infer_stable_topology(observation)
        if topology is None or topology.path != PowerPath.GRID:
            return False
        self._set_stable(topology)
        self.target_source = topology.source
        self.fault = None
        self.failed_phase = None
        self.transaction = None
        self.initialized = True
        return True

    def begin_recovery_to_grid_path(self) -> None:
        """Начать явно разрешённую процедуру восстановления ES-18/TPC-14.

        Старая незавершённая силовая транзакция не продолжается. Дальнейшие
        шаги будут выбраны заново исключительно по физическим обратным связям.
        """
        self.phase = TransferPhase.RECOVERY_REQUIRED
        self.actual_source = PowerSource.UNKNOWN
        self.actual_path = PowerPath.UNKNOWN
        self.target_source = PowerSource.GRID
        self.deadline = None
        self.feedback_lost_since = None
        self.fault = None
        self.failed_phase = None
        self.commanded_generator = None
        self.transaction = None
        self.initialized = True

    def recovery_blocker(
        self,
        observation: PowerTransferObservation,
    ) -> str | None:
        """Объяснить, почему автоматическое восстановление сейчас опасно."""
        if not observation.required_states_known:
            return "Неизвестны обязательные состояния силовой схемы."
        if observation.emergency_stop is not False:
            return "Активен Generators Emergency Stop."

        grid_side_active = (
            observation.grid_connected is True
            or observation.house_on_grid is True
        )
        generator_side_active = (
            observation.generator_selected is True
            or observation.house_on_generator is True
        )
        if grid_side_active and generator_side_active:
            return (
                "Одновременно обнаружены признаки подключённых Grid и "
                "генераторного ввода."
            )
        return None

    def step_recovery_to_grid_path(
        self,
        now: float,
        observation: PowerTransferObservation,
    ) -> tuple[list[TransferAction], str | None]:
        """Безопасно вернуть силовую схему в Grid path.

        Метод никогда не выбирает генераторную шину. Даже в recovery сначала
        подтверждается её снятие, и только затем разрешается CONNECT_GRID.
        """
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
            self._advance_transaction(now, "generator_disconnected")
            if observation.grid_connected is True:
                self._wait_for_grid_path_confirmation(now)
                return [], None
            return self._begin_connect_grid(now), None

        if self.phase == TransferPhase.CONNECTING_GRID:
            if (
                observation.generator_selected is not False
                or observation.house_on_generator is not False
            ):
                reason = (
                    "Генераторная шина перестала быть изолированной во время "
                    "восстановления Grid path."
                )
                self._require_recovery(reason)
                return [], reason
            if observation.grid_connected is not True:
                return [], None

            topology = self._infer_stable_topology(observation)
            if topology is not None and topology.path == PowerPath.GRID:
                self._complete_transition(now, topology)
            return [], None

        topology = self._infer_stable_topology(observation)
        if topology is not None and topology.path == PowerPath.GRID:
            self._set_stable(topology)
            return [], None

        generator_side_active = (
            observation.generator_selected is True
            or observation.house_on_generator is True
        )
        if generator_side_active:
            return self._begin_deselect_generator(now, PowerSource.GRID), None

        if observation.grid_connected is True:
            self._wait_for_grid_path_confirmation(now)
            return [], None
        return self._begin_connect_grid(now), None

    def step(
        self,
        now: float,
        observation: PowerTransferObservation,
        desired_source: PowerSource | None,
        *,
        desired_generator_ready: bool,
        actions_allowed: bool = True,
    ) -> list[TransferAction]:
        """Продвинуть не более одного подтверждаемого физического шага."""
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
                "Одновременно обнаружены несовместимые положения Grid и "
                "генераторного ввода."
            )
            return []

        if self.phase == TransferPhase.RECOVERY_REQUIRED:
            # В recovery продолжаем отображать распознаваемую физическую
            # топологию, но не снимаем саму блокировку без процедуры ES-18.
            topology = self._infer_stable_topology(observation)
            if topology is not None:
                self.actual_source = topology.source
                self.actual_path = topology.path
            return []

        if not self.transition_in_progress:
            topology = self._infer_stable_topology(observation)
            if topology is not None:
                self.feedback_lost_since = None
                if (
                    topology.path != self.actual_path
                    or topology.source != self.actual_source
                ):
                    if (
                        actions_allowed
                        and topology.path == PowerPath.GENERATOR
                        and self.actual_path != PowerPath.UNKNOWN
                    ):
                        self._require_recovery(
                            "Генераторный ввод изменился до команды его выбора."
                        )
                        return []
                    self._set_stable(topology)
            elif (
                self.phase == TransferPhase.STABLE_GENERATOR
                and (desired_source is None or desired_source.generator is None)
            ):
                # Даже при пропавшем RUNNING разрешено единственное безопасное
                # действие: снять ранее подтверждённую генераторную шину.
                self.feedback_lost_since = None
            else:
                if self.feedback_lost_since is None:
                    self.feedback_lost_since = now
                if now - self.feedback_lost_since >= self.confirmation_timeout:
                    self._require_recovery(
                        "Устойчивая силовая топология потеряла физическое "
                        "подтверждение."
                    )
                return []

        if desired_source is None:
            # HOLD: безопасная устойчивая топология принята как внешний факт.
            # Незавершённая собственная транзакция без цели, напротив,
            # неоднозначна и не должна молча забываться.
            if self.transition_in_progress:
                self._require_recovery(
                    "Во время силовой транзакции потеряна цель Supervisor."
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

        if self.transition_in_progress:
            return self._continue_transition(
                now,
                observation,
                desired_source,
                desired_generator_ready,
            )

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

        target_generator = desired_source.generator
        if target_generator is not None:
            if observation.emergency_stop is True or not desired_generator_ready:
                return []
            if self.actual_path == PowerPath.GENERATOR:
                if observation.active_generator != target_generator:
                    self._require_recovery(
                        "На общей генераторной шине обнаружен не тот генератор, "
                        "который запросил Supervisor."
                    )
                else:
                    self.actual_source = desired_source
                return []
            if self.actual_path == PowerPath.GRID:
                return self._begin_disconnect_grid(now, desired_source)
            if self.actual_path == PowerPath.BATTERY:
                return self._begin_select_generator(now, target_generator)
            return []

        if desired_source == PowerSource.BATTERY:
            if self.actual_path == PowerPath.BATTERY:
                self.actual_source = PowerSource.BATTERY
                return []
            if self.actual_path == PowerPath.GRID:
                return self._begin_disconnect_grid(now, desired_source)
            if self.actual_path == PowerPath.GENERATOR:
                return self._begin_deselect_generator(now, desired_source)
            return []

        if desired_source == PowerSource.GRID:
            if self.actual_path == PowerPath.GRID:
                return []
            if self.actual_path == PowerPath.BATTERY:
                return self._begin_connect_grid(now)
            if self.actual_path == PowerPath.GENERATOR:
                return self._begin_deselect_generator(now, desired_source)

        return []

    def _continue_transition(
        self,
        now: float,
        observation: PowerTransferObservation,
        desired_source: PowerSource,
        desired_generator_ready: bool,
    ) -> list[TransferAction]:
        if self.phase == TransferPhase.DISCONNECTING_GRID:
            return self._confirm_grid_disconnected(
                now,
                observation,
                desired_source,
                desired_generator_ready,
            )
        if self.phase == TransferPhase.SELECTING_GENERATOR:
            return self._confirm_generator_selected(
                now,
                observation,
                desired_source,
                desired_generator_ready,
            )
        if self.phase == TransferPhase.DISCONNECTING_GENERATOR:
            return self._confirm_generator_disconnected(
                now,
                observation,
                desired_source,
                desired_generator_ready,
            )
        if self.phase == TransferPhase.CONNECTING_GRID:
            return self._confirm_grid_connected(now, observation, desired_source)
        return []

    def _confirm_grid_disconnected(
        self,
        now: float,
        observation: PowerTransferObservation,
        desired_source: PowerSource,
        desired_generator_ready: bool,
    ) -> list[TransferAction]:
        if (
            observation.generator_selected is not False
            or observation.house_on_generator is not False
        ):
            self._require_recovery(
                "Генераторный ввод изменился до команды его выбора."
            )
            return []
        if (
            observation.grid_connected is not False
            or observation.house_on_grid is not False
        ):
            return []

        self._advance_transaction(now, "grid_disconnected")
        return self._continue_from_isolated_bus(
            now, observation, desired_source, desired_generator_ready
        )

    def _confirm_generator_selected(
        self,
        now: float,
        observation: PowerTransferObservation,
        desired_source: PowerSource,
        desired_generator_ready: bool,
    ) -> list[TransferAction]:
        if (
            observation.generator_selected is not True
            or observation.house_on_generator is not True
        ):
            return []
        if (
            observation.grid_connected is not False
            or observation.house_on_grid is not False
            or observation.active_generator != self.commanded_generator
        ):
            self._require_recovery(
                "Генераторный ввод включён без полного безопасного "
                "подтверждения выбранного агрегата."
            )
            return []

        assert self.commanded_generator is not None
        selected_source = PowerSource.for_generator(self.commanded_generator)
        selected = PowerTopology(PowerPath.GENERATOR, selected_source)
        if desired_source == selected_source and desired_generator_ready:
            self._complete_transition(now, selected)
            return []

        # Цель изменилась после выдачи команды SELECT. Сначала подтверждаем её
        # завершение, затем отдельной командой безопасно снимаем шину.
        self._complete_transition(now, selected)
        return self._begin_deselect_generator(now, desired_source)

    def _confirm_generator_disconnected(
        self,
        now: float,
        observation: PowerTransferObservation,
        desired_source: PowerSource,
        desired_generator_ready: bool,
    ) -> list[TransferAction]:
        if (
            observation.generator_selected is not False
            or observation.house_on_generator is not False
        ):
            return []
        if (
            observation.grid_connected is not False
            or observation.house_on_grid is not False
        ):
            self._require_recovery(
                "Grid подключён до подтверждённой изоляции генераторной шины."
            )
            return []

        self._advance_transaction(now, "generator_disconnected")
        return self._continue_from_isolated_bus(
            now, observation, desired_source, desired_generator_ready
        )

    def _continue_from_isolated_bus(
        self,
        now: float,
        observation: PowerTransferObservation,
        desired_source: PowerSource,
        desired_generator_ready: bool,
    ) -> list[TransferAction]:
        """Выбрать следующий шаг только после подтверждения обоих вводов OFF.

        Проверки текущего движения и их аварийные сообщения остаются у
        вызывающего метода. Здесь применяется общая логика выбора новой цели.
        """
        target_generator = desired_source.generator
        if (
            target_generator is not None
            and desired_generator_ready
            and observation.emergency_stop is False
        ):
            return self._begin_select_generator(now, target_generator)
        if desired_source == PowerSource.GRID:
            return self._begin_connect_grid(now)

        self._complete_transition(
            now,
            PowerTopology(PowerPath.BATTERY, PowerSource.BATTERY),
        )
        return []

    def _confirm_grid_connected(
        self,
        now: float,
        observation: PowerTransferObservation,
        desired_source: PowerSource,
    ) -> list[TransferAction]:
        if (
            observation.generator_selected is not False
            or observation.house_on_generator is not False
        ):
            self._require_recovery(
                "Grid подключён до подтверждённой изоляции генераторной шины."
            )
            return []
        if observation.grid_connected is not True:
            return []

        if desired_source != PowerSource.GRID:
            # Противоположную команду разрешаем только после подтверждения,
            # что предыдущая команда подключения действительно завершилась.
            return self._begin_disconnect_grid(now, desired_source)

        topology = self._infer_stable_topology(observation)
        if topology is not None and topology.path == PowerPath.GRID:
            self._complete_transition(now, topology)
        return []

    def _begin_disconnect_grid(
        self, now: float, target: PowerSource
    ) -> list[TransferAction]:
        self._begin_or_advance_transaction(
            now,
            kind="leave_grid_path",
            target=target.value,
            step="disconnect_grid",
        )
        self.phase = TransferPhase.DISCONNECTING_GRID
        self.deadline = now + self.confirmation_timeout
        return [TransferAction(
            TransferActionKind.DISCONNECT_GRID,
            "Отключаем ввод Grid; генераторная шина подтверждённо снята.",
        )]

    def _begin_connect_grid(self, now: float) -> list[TransferAction]:
        self._begin_or_advance_transaction(
            now,
            kind="enter_grid_path",
            target=PowerSource.GRID.value,
            step="connect_grid",
        )
        self.phase = TransferPhase.CONNECTING_GRID
        self.deadline = now + self.confirmation_timeout
        return [TransferAction(
            TransferActionKind.CONNECT_GRID,
            "Генераторная шина изолирована; подключаем Grid path.",
        )]

    def _wait_for_grid_path_confirmation(self, now: float) -> None:
        """Ждать обратную связь уже включённого Grid path без лишней команды."""
        self._begin_or_advance_transaction(
            now,
            kind="recover_grid_path",
            target=PowerSource.GRID.value,
            step="confirm_grid_path",
        )
        self.phase = TransferPhase.CONNECTING_GRID
        self.deadline = now + self.confirmation_timeout

    def _begin_select_generator(
        self, now: float, generator: GeneratorSlot
    ) -> list[TransferAction]:
        target = PowerSource.for_generator(generator)
        self._begin_or_advance_transaction(
            now,
            kind="transfer_to_generator",
            target=target.value,
            step="select_generator",
        )
        self.commanded_generator = generator
        self.phase = TransferPhase.SELECTING_GENERATOR
        self.deadline = now + self.confirmation_timeout
        return [TransferAction(
            TransferActionKind.SELECT_GENERATOR,
            "Grid подтверждённо отключён; выбираем генераторную шину.",
        )]

    def _begin_deselect_generator(
        self, now: float, target: PowerSource
    ) -> list[TransferAction]:
        self._begin_or_advance_transaction(
            now,
            kind="leave_generator_path",
            target=target.value,
            step="deselect_generator",
        )
        self.phase = TransferPhase.DISCONNECTING_GENERATOR
        self.deadline = now + self.confirmation_timeout
        return [TransferAction(
            TransferActionKind.DESELECT_GENERATOR,
            "Отключаем дом от генераторной шины.",
        )]

    def _begin_or_advance_transaction(
        self,
        now: float,
        *,
        kind: str,
        target: str,
        step: str,
    ) -> None:
        if self.transaction is None or not self.transition_in_progress:
            self.transaction = Transaction.begin(kind, target, now, step)
        else:
            self.transaction.target = target
            self.transaction.advance(step, now)

    def _advance_transaction(self, now: float, confirmed: str) -> None:
        if self.transaction is not None:
            self.transaction.advance(
                self.transaction.step,
                now,
                confirmed=confirmed,
            )

    def _complete_transition(
        self, now: float, topology: PowerTopology
    ) -> None:
        if self.transaction is not None:
            self.transaction.complete(
                now,
                f"Источник {topology.source.value}, путь {topology.path.value} подтверждены.",
            )
        self._set_stable(topology)

    def _set_stable(self, topology: PowerTopology) -> None:
        self.actual_source = topology.source
        self.actual_path = topology.path
        self.last_confirmed_source = topology.source
        self.last_confirmed_path = topology.path
        self.deadline = None
        self.feedback_lost_since = None
        self.commanded_generator = None
        if topology.path == PowerPath.GRID:
            self.phase = TransferPhase.STABLE_GRID_PATH
        elif topology.path == PowerPath.BATTERY:
            self.phase = TransferPhase.STABLE_BATTERY_PATH
        elif topology.path == PowerPath.GENERATOR:
            self.phase = TransferPhase.STABLE_GENERATOR
        else:
            self.phase = TransferPhase.WAITING_FOR_DATA

    def _infer_stable_topology(
        self, observation: PowerTransferObservation
    ) -> PowerTopology | None:
        if not observation.required_states_known:
            return None
        if self._unsafe_overlap(observation):
            return None

        if (
            observation.generator_selected is True
            and observation.grid_connected is False
            and observation.house_on_generator is True
            and observation.house_on_grid is False
            and observation.active_generator is not None
        ):
            return PowerTopology(
                PowerPath.GENERATOR,
                PowerSource.for_generator(observation.active_generator),
            )

        if (
            observation.generator_selected is False
            and observation.house_on_generator is False
        ):
            if (
                observation.grid_connected is False
                and observation.house_on_grid is False
            ):
                return PowerTopology(PowerPath.BATTERY, PowerSource.BATTERY)
            if observation.grid_connected is True:
                if (
                    observation.grid_ready is True
                    and observation.house_on_grid is True
                ):
                    return PowerTopology(PowerPath.GRID, PowerSource.GRID)
                if (
                    observation.grid_ready is False
                    and observation.house_on_grid is False
                ):
                    return PowerTopology(PowerPath.GRID, PowerSource.BATTERY)

        return None

    @staticmethod
    def _unsafe_overlap(observation: PowerTransferObservation) -> bool:
        return (
            observation.house_on_grid is True
            and observation.house_on_generator is True
        ) or (
            observation.grid_connected is True
            and observation.generator_selected is True
        )

    def _require_recovery(self, reason: str) -> None:
        if self.phase != TransferPhase.RECOVERY_REQUIRED:
            self.failed_phase = self.phase
        self.phase = TransferPhase.RECOVERY_REQUIRED
        self.actual_source = PowerSource.UNKNOWN
        self.actual_path = PowerPath.UNKNOWN
        self.deadline = None
        self.feedback_lost_since = None
        self.commanded_generator = None
        self.fault = reason

    def _deadline_reached(self, now: float) -> bool:
        return (
            self.transition_in_progress
            and self.deadline is not None
            # Подтверждение ровно на границе timeout ещё принимается.
            and now > self.deadline
        )

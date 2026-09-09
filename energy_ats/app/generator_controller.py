"""Управление жизненным циклом одного бензинового генератора.

Контроллер не знает о Grid, МАП и основных контакторах дома. Он получает только
желаемое состояние RUN/STOP и физические признаки собственного двигателя.
Специфика конкретной модели хранится в ``GeneratorProfile``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from domain import GeneratorSlot


class ChokeStrategy(str, Enum):
    ALWAYS = "always"
    TEMPERATURE = "temperature"
    NEVER = "never"


class GeneratorPhase(str, Enum):
    WAITING_FOR_DATA = "waiting_for_data"
    IDLE = "idle"
    PREPARING = "preparing"
    WAITING_FOR_RUNNING = "waiting_for_running"
    HOLDING_COLD_START_CHOKE = "holding_cold_start_choke"
    WARMING_UP = "warming_up"
    READY_FOR_LOAD = "ready_for_load"
    WAITING_FOR_LOAD_RELEASE = "waiting_for_load_release"
    COOLING_DOWN = "cooling_down"
    WAITING_FOR_STOP = "waiting_for_stop"
    EXTERNAL_RUNNING = "external_running"
    FAULT = "fault"
    RECOVERY_REQUIRED = "recovery_required"


class GeneratorActionKind(str, Enum):
    REMOTE_ON = "remote_on"
    REMOTE_OFF = "remote_off"
    CHOKE_TO_COLD_START = "choke_to_cold_start"
    CHOKE_TO_RUN = "choke_to_run"


@dataclass(frozen=True)
class GeneratorAction:
    slot: GeneratorSlot
    kind: GeneratorActionKind
    message: str


@dataclass(frozen=True)
class GeneratorProfile:
    slot: GeneratorSlot
    display_name: str
    model: str
    choke_strategy: ChokeStrategy
    choke_temperature: float = 10.0
    choke_move_seconds: float = 1.0
    cold_start_choke_hold_seconds: float = 10.0
    start_timeout_seconds: float = 90.0
    stop_timeout_seconds: float = 90.0
    cooldown_seconds: float = 60.0
    warm_temperature: float = 10.0
    cool_temperature: float = -5.0
    cold_temperature: float = -10.0
    warmup_warm_seconds: float = 30.0
    warmup_cool_seconds: float = 60.0
    warmup_cold_seconds: float = 180.0
    warmup_very_cold_seconds: float = 300.0

    def should_use_choke(self, temperature: float | None) -> bool:
        if self.choke_strategy == ChokeStrategy.ALWAYS:
            return True
        if self.choke_strategy == ChokeStrategy.NEVER:
            return False
        return temperature is None or temperature < self.choke_temperature

    def warmup_seconds(self, temperature: float | None) -> float:
        if temperature is None:
            return self.warmup_very_cold_seconds
        if temperature >= self.warm_temperature:
            return self.warmup_warm_seconds
        if temperature > self.cool_temperature:
            return self.warmup_cool_seconds
        if temperature > self.cold_temperature:
            return self.warmup_cold_seconds
        return self.warmup_very_cold_seconds


def default_generator_profiles() -> dict[GeneratorSlot, GeneratorProfile]:
    """Bootstrap-профили слотов до получения имени/модели из HA."""

    return {
        GeneratorSlot.A: GeneratorProfile(
            slot=GeneratorSlot.A,
            display_name="Generator A",
            model="",
            choke_strategy=ChokeStrategy.ALWAYS,
        ),
        GeneratorSlot.B: GeneratorProfile(
            slot=GeneratorSlot.B,
            display_name="Generator B",
            model="",
            choke_strategy=ChokeStrategy.ALWAYS,
        ),
    }


@dataclass(frozen=True)
class GeneratorObservation:
    running: bool | None
    remote_on: bool | None
    load_connected: bool | None
    emergency_stop: bool | None
    ambient_temperature_external: float | None

    @property
    def required_states_known(self) -> bool:
        return (
            self.running is not None
            and self.remote_on is not None
            and self.emergency_stop is not None
        )


@dataclass(frozen=True)
class GeneratorStatus:
    slot: GeneratorSlot
    display_name: str
    phase: GeneratorPhase
    running: bool | None
    remote_on: bool | None
    ready_for_load: bool
    fault: str | None


class GeneratorController:
    """Подтверждаемый автомат одного двигателя."""

    _READY_PHASES = {
        GeneratorPhase.READY_FOR_LOAD,
        GeneratorPhase.WAITING_FOR_LOAD_RELEASE,
        GeneratorPhase.COOLING_DOWN,
    }

    def __init__(self, profile: GeneratorProfile) -> None:
        self.profile = profile
        self.phase = GeneratorPhase.WAITING_FOR_DATA
        self.deadline: float | None = None
        self.fault: str | None = None
        self.start_temperature: float | None = None
        self.choke_used = False
        self.initialized = False

    def require_recovery(self, reason: str) -> None:
        self.phase = GeneratorPhase.RECOVERY_REQUIRED
        self.deadline = None
        self.fault = reason
        self.initialized = True

    def status(self, observation: GeneratorObservation) -> GeneratorStatus:
        return GeneratorStatus(
            slot=self.profile.slot,
            display_name=self.profile.display_name,
            phase=self.phase,
            running=observation.running,
            remote_on=observation.remote_on,
            ready_for_load=(
                self.phase in self._READY_PHASES
                and observation.running is True
                and observation.remote_on is True
            ),
            fault=self.fault,
        )

    def step_authorized_shutdown(
        self,
        now: float,
        observation: GeneratorObservation,
        *,
        authorized: bool,
    ) -> tuple[list[GeneratorAction], str | None]:
        """Штатно остановить разгруженный двигатель при явном праве на это.

        Используется для managed recovery и для ограниченного требования
        остановить outage-related внешний генератор после стабильного возврата
        Grid. Сам факт внешнего RUNNING такого права не создаёт.
        """

        if not observation.required_states_known:
            return [], f"{self.profile.display_name}: неизвестны RUNNING или REMOTE."
        if observation.emergency_stop is not False:
            return [], "Активен Generators Emergency Stop."
        if observation.load_connected is not False:
            return [], (
                f"{self.profile.display_name}: нагрузка не подтверждена как отключённая."
            )

        active = observation.running is True or observation.remote_on is True
        if not active:
            self._return_to_idle()
            return [], None
        if not authorized:
            self.phase = GeneratorPhase.EXTERNAL_RUNNING
            self.deadline = None
            self.fault = None
            self.initialized = True
            return [], f"{self.profile.display_name} запущен вне управляемой сессии."

        if self.phase == GeneratorPhase.WAITING_FOR_STOP:
            if observation.running is False and observation.remote_on is False:
                self._return_to_idle()
                return [], None
            if self._deadline_reached(now):
                reason = (
                    f"{self.profile.display_name} не подтвердил остановку за "
                    f"{int(self.profile.stop_timeout_seconds)} с."
                )
                self.require_recovery(reason)
                return [], reason
            return [], None

        if self.phase == GeneratorPhase.COOLING_DOWN:
            if observation.running is False:
                if observation.remote_on is False:
                    self._return_to_idle()
                    return [], None
                return self._request_remote_off(now), None
            if self._deadline_reached(now):
                return self._request_remote_off(now), None
            return [], None

        if observation.running is True:
            self.phase = GeneratorPhase.COOLING_DOWN
            self.deadline = now + self.profile.cooldown_seconds
            self.fault = None
            self.initialized = True
            return [], None

        return self._request_remote_off(now), None

    def step(
        self,
        now: float,
        observation: GeneratorObservation,
        desired_running: bool,
        *,
        actions_allowed: bool = True,
        stable_managed_session: bool = False,
    ) -> list[GeneratorAction]:
        """Продвинуть автомат на один шаг."""

        if not observation.required_states_known:
            if not self.initialized:
                self.phase = GeneratorPhase.WAITING_FOR_DATA
            return []

        if not self.initialized:
            self._initialize_from_physical_state(
                observation,
                desired_running=desired_running,
                stable_managed_session=stable_managed_session,
            )
            self.initialized = True

        if observation.emergency_stop is True:
            self.phase = GeneratorPhase.FAULT
            self.deadline = None
            self.fault = "Активен Generators Emergency Stop"
            return []

        if self.phase == GeneratorPhase.EXTERNAL_RUNNING:
            return self._observe_external_start(observation)

        if not actions_allowed or self.phase in {
            GeneratorPhase.FAULT,
            GeneratorPhase.RECOVERY_REQUIRED,
        }:
            return []

        if self.phase == GeneratorPhase.IDLE:
            if observation.running is True or observation.remote_on is True:
                self.phase = GeneratorPhase.EXTERNAL_RUNNING
                return []
            if desired_running:
                return self._begin_start(now, observation)
            return []

        if self.phase == GeneratorPhase.PREPARING:
            if not desired_running:
                return self._abort_start(now)
            if observation.running is True:
                return self._set_fault(
                    f"{self.profile.display_name}: RUNNING появился до "
                    "управляемой команды REMOTE START."
                )
            if self._deadline_reached(now):
                self.phase = GeneratorPhase.WAITING_FOR_RUNNING
                self.deadline = now + self.profile.start_timeout_seconds
                return [
                    self._action(
                        GeneratorActionKind.REMOTE_ON,
                        f"{self.profile.display_name}: заслонка подготовлена, "
                        "подаём REMOTE START.",
                    )
                ]
            return []

        if self.phase == GeneratorPhase.WAITING_FOR_RUNNING:
            if not desired_running:
                return self._abort_start(now)
            if observation.running is True:
                if observation.remote_on is not True:
                    return self._set_fault(
                        f"{self.profile.display_name} подтвердил RUNNING без "
                        "активного управляемого REMOTE."
                    )
                return self._running_confirmed(now)
            if self._deadline_reached(now):
                return self._set_fault(
                    f"{self.profile.display_name} не подтвердил RUNNING за "
                    f"{int(self.profile.start_timeout_seconds)} с."
                )
            return []

        if self.phase == GeneratorPhase.HOLDING_COLD_START_CHOKE:
            if not desired_running:
                return self._abort_start(now)
            if observation.remote_on is not True:
                return self._set_fault(
                    f"{self.profile.display_name} потерял REMOTE после запуска."
                )
            if observation.running is not True:
                return self._set_fault(
                    f"{self.profile.display_name} заглох при закрытой заслонке."
                )
            if self._deadline_reached(now):
                self.phase = GeneratorPhase.WARMING_UP
                self.deadline = now + self.profile.warmup_seconds(
                    self.start_temperature
                )
                return [
                    self._action(
                        GeneratorActionKind.CHOKE_TO_RUN,
                        f"{self.profile.display_name}: переводим заслонку "
                        "в рабочее положение.",
                    )
                ]
            return []

        if self.phase == GeneratorPhase.WARMING_UP:
            if not desired_running:
                return self._abort_start(now)
            if observation.remote_on is not True:
                return self._set_fault(
                    f"{self.profile.display_name} потерял REMOTE во время прогрева."
                )
            if observation.running is not True:
                return self._set_fault(
                    f"{self.profile.display_name} заглох во время прогрева."
                )
            if self._deadline_reached(now):
                self.phase = GeneratorPhase.READY_FOR_LOAD
                self.deadline = None
            return []

        if self.phase == GeneratorPhase.READY_FOR_LOAD:
            if (
                not desired_running
                and observation.running is False
                and observation.remote_on is False
                and observation.load_connected is False
            ):
                self._return_to_idle()
                return []
            if observation.running is not True:
                return self._set_fault(
                    f"{self.profile.display_name} потерял RUNNING в рабочем режиме."
                )
            if observation.remote_on is not True:
                return self._set_fault(
                    f"{self.profile.display_name} потерял REMOTE в рабочем режиме."
                )
            if not desired_running:
                return self._begin_stop(now, observation)
            return []

        if self.phase == GeneratorPhase.WAITING_FOR_LOAD_RELEASE:
            if (
                observation.running is False
                and observation.remote_on is False
                and observation.load_connected is False
            ):
                self._return_to_idle()
                return []
            if observation.running is not True:
                return self._set_fault(
                    f"{self.profile.display_name} потерял RUNNING до снятия нагрузки."
                )
            if observation.remote_on is not True:
                return self._set_fault(
                    f"{self.profile.display_name} потерял REMOTE до снятия нагрузки."
                )
            if desired_running:
                self.phase = GeneratorPhase.READY_FOR_LOAD
                return []
            if observation.load_connected is False:
                return self._begin_cooldown(now)
            return []

        if self.phase == GeneratorPhase.COOLING_DOWN:
            if (
                observation.running is False
                and observation.remote_on is False
                and observation.load_connected is not True
            ):
                self._return_to_idle()
                return []
            if observation.running is not True:
                return self._set_fault(
                    f"{self.profile.display_name} потерял RUNNING во время cooldown."
                )
            if observation.remote_on is not True:
                return self._set_fault(
                    f"{self.profile.display_name} потерял REMOTE во время cooldown."
                )
            if desired_running or observation.load_connected is True:
                self.phase = GeneratorPhase.READY_FOR_LOAD
                self.deadline = None
                return []
            if self._deadline_reached(now):
                return self._request_remote_off(now)
            return []

        if self.phase == GeneratorPhase.WAITING_FOR_STOP:
            if observation.running is False and observation.remote_on is False:
                self._return_to_idle()
                return []
            if desired_running:
                return self._cancel_stop(now, observation)
            if self._deadline_reached(now):
                return self._set_fault(
                    f"{self.profile.display_name} не остановился за "
                    f"{int(self.profile.stop_timeout_seconds)} с после снятия REMOTE START."
                )
            return []

        return []

    def _initialize_from_physical_state(
        self,
        observation: GeneratorObservation,
        *,
        desired_running: bool,
        stable_managed_session: bool,
    ) -> None:
        if observation.emergency_stop is True:
            self.phase = GeneratorPhase.FAULT
            self.fault = "Активен Generators Emergency Stop"
            return
        if observation.running is True:
            if (
                desired_running
                and stable_managed_session
                and observation.remote_on is True
            ):
                self.phase = GeneratorPhase.READY_FOR_LOAD
            else:
                self.phase = GeneratorPhase.EXTERNAL_RUNNING
            return
        if observation.remote_on is True and not desired_running:
            self.phase = GeneratorPhase.EXTERNAL_RUNNING
            return
        if observation.remote_on is True or desired_running:
            self.phase = GeneratorPhase.RECOVERY_REQUIRED
            self.fault = (
                "Состояние запуска не подтверждено после restart; "
                "требуется сверка физического состояния."
            )
            return
        self._return_to_idle()

    def _begin_start(
        self,
        now: float,
        observation: GeneratorObservation,
    ) -> list[GeneratorAction]:
        self.start_temperature = observation.ambient_temperature_external
        self.choke_used = self.profile.should_use_choke(self.start_temperature)
        self.phase = GeneratorPhase.PREPARING
        self.deadline = now + self.profile.choke_move_seconds

        action = (
            GeneratorActionKind.CHOKE_TO_COLD_START
            if self.choke_used
            else GeneratorActionKind.CHOKE_TO_RUN
        )
        position = "закрыта" if self.choke_used else "открыта"
        temperature_source = (
            "ambient_temperature_external"
            if self.start_temperature is not None
            else "conservative_fallback"
        )
        return [
            self._action(
                action,
                f"{self.profile.display_name}: начало запуска; заслонка {position}, "
                f"источник температуры — {temperature_source}.",
            )
        ]

    def _running_confirmed(self, now: float) -> list[GeneratorAction]:
        if self.choke_used:
            self.phase = GeneratorPhase.HOLDING_COLD_START_CHOKE
            self.deadline = now + self.profile.cold_start_choke_hold_seconds
        else:
            self.phase = GeneratorPhase.WARMING_UP
            self.deadline = now + self.profile.warmup_seconds(self.start_temperature)
        return []

    def _begin_stop(
        self,
        now: float,
        observation: GeneratorObservation,
    ) -> list[GeneratorAction]:
        if observation.load_connected is not False:
            self.phase = GeneratorPhase.WAITING_FOR_LOAD_RELEASE
            self.deadline = None
            return []
        return self._begin_cooldown(now)

    def _begin_cooldown(self, now: float) -> list[GeneratorAction]:
        self.phase = GeneratorPhase.COOLING_DOWN
        self.deadline = now + self.profile.cooldown_seconds
        return []

    def _request_remote_off(self, now: float) -> list[GeneratorAction]:
        self.phase = GeneratorPhase.WAITING_FOR_STOP
        self.deadline = now + self.profile.stop_timeout_seconds
        return [
            self._action(
                GeneratorActionKind.REMOTE_OFF,
                f"{self.profile.display_name}: cooldown завершён, снимаем REMOTE START.",
            )
        ]

    def _abort_start(self, now: float) -> list[GeneratorAction]:
        self.phase = GeneratorPhase.WAITING_FOR_STOP
        self.deadline = now + self.profile.stop_timeout_seconds
        return [
            self._action(
                GeneratorActionKind.REMOTE_OFF,
                f"{self.profile.display_name}: запуск отменён, снимаем REMOTE START.",
            ),
            self._action(
                GeneratorActionKind.CHOKE_TO_RUN,
                f"{self.profile.display_name}: запуск отменён, открываем заслонку.",
            ),
        ]

    def _cancel_stop(
        self,
        now: float,
        observation: GeneratorObservation,
    ) -> list[GeneratorAction]:
        if observation.running is True:
            self.phase = GeneratorPhase.READY_FOR_LOAD
            self.deadline = None
        else:
            self.phase = GeneratorPhase.WAITING_FOR_RUNNING
            self.deadline = now + self.profile.start_timeout_seconds
        return [
            self._action(
                GeneratorActionKind.REMOTE_ON,
                f"{self.profile.display_name}: остановка отменена, снова подаём REMOTE START.",
            )
        ]

    def _observe_external_start(
        self,
        observation: GeneratorObservation,
    ) -> list[GeneratorAction]:
        if observation.running is False and observation.remote_on is False:
            self._return_to_idle()
        return []

    def _set_fault(self, reason: str) -> list[GeneratorAction]:
        self.phase = GeneratorPhase.FAULT
        self.deadline = None
        self.fault = reason
        return [
            self._action(GeneratorActionKind.REMOTE_OFF, reason),
            self._action(
                GeneratorActionKind.CHOKE_TO_RUN,
                f"{self.profile.display_name}: после ошибки открываем заслонку.",
            ),
        ]

    def _return_to_idle(self) -> None:
        self.phase = GeneratorPhase.IDLE
        self.deadline = None
        self.fault = None
        self.choke_used = False

    def _deadline_reached(self, now: float) -> bool:
        return self.deadline is not None and now >= self.deadline

    def _action(self, kind: GeneratorActionKind, message: str) -> GeneratorAction:
        return GeneratorAction(slot=self.profile.slot, kind=kind, message=message)

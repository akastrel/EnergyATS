"""Управление жизненным циклом одного бензинового генератора.

Контроллер намеренно не знает о Grid, МАП и силовом переключателе дома. Он
получает только желаемое состояние RUN/STOP и физические признаки собственного
двигателя. Вся специфика конкретной модели хранится в ``GeneratorProfile``.
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
    CHOKE_HOLD = "choke_hold"
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
    CHOKE_TO_COLD = "choke_to_cold"
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
    choke_hold_seconds: float = 10.0
    start_timeout_seconds: float = 90.0
    stop_timeout_seconds: float = 90.0
    cooldown_seconds: float = 300.0
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
        # Неизвестная температура трактуется консервативно: двигатель холодный.
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
    """Текущие пользовательские профили Elemax и Вепря.

    Это единственное место в Python, где универсальная логика связывается с
    конкретными двигателями. Точные модели можно вписать сюда, когда они будут
    уточнены; на алгоритм ATS эти строки не влияют.
    """

    return {
        GeneratorSlot.A: GeneratorProfile(
            slot=GeneratorSlot.A,
            display_name="Elemax",
            model="Elemax (точная модель пока не указана)",
            choke_strategy=ChokeStrategy.ALWAYS,
        ),
        GeneratorSlot.B: GeneratorProfile(
            slot=GeneratorSlot.B,
            display_name="Вепрь",
            model="Вепрь 6.5 kW (точная модель пока не указана)",
            choke_strategy=ChokeStrategy.TEMPERATURE,
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
    externally_started: bool
    fault: str | None
    start_temperature: float | None
    start_temperature_source: str | None


class GeneratorController:
    """Небольшой подтверждаемый автомат одного двигателя."""

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
        self.start_temperature_source: str | None = None
        self.choke_used = False
        self.initialized = False
        self._reset_requested = False

    def request_fault_reset(self) -> None:
        self._reset_requested = True

    def require_recovery(self, reason: str) -> None:
        self.phase = GeneratorPhase.RECOVERY_REQUIRED
        self.deadline = None
        self.fault = reason
        self.initialized = True

    def status(self, observation: GeneratorObservation) -> GeneratorStatus:
        ready = (
            self.phase in self._READY_PHASES
            and observation.running is True
            and observation.remote_on is True
        )
        return GeneratorStatus(
            slot=self.profile.slot,
            display_name=self.profile.display_name,
            phase=self.phase,
            running=observation.running,
            remote_on=observation.remote_on,
            ready_for_load=ready,
            externally_started=(
                self.phase == GeneratorPhase.EXTERNAL_RUNNING
                or (
                    (
                        observation.running is True
                        or observation.remote_on is True
                    )
                    and self.phase
                    in {GeneratorPhase.WAITING_FOR_DATA, GeneratorPhase.IDLE}
                )
            ),
            fault=self.fault,
            start_temperature=self.start_temperature,
            start_temperature_source=self.start_temperature_source,
        )

    def step(
        self,
        now: float,
        observation: GeneratorObservation,
        desired_running: bool,
        *,
        actions_allowed: bool = True,
        stable_managed_session: bool = False,
    ) -> list[GeneratorAction]:
        """Продвинуть автомат на один шаг.

        ``desired_running`` является уровневым намерением, поэтому повторные
        вызовы безопасны. ``actions_allowed=False`` используется во время
        RECOVERY_REQUIRED: физические состояния читаются, но команды запрещены.
        """

        if not observation.required_states_known:
            # Кратковременно unavailable не должно уничтожать уже известную
            # фазу. До первой полной картины показываем WAITING_FOR_DATA, а
            # затем просто замораживаем автомат без физических команд.
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

        if self._reset_requested:
            self._reset_requested = False
            if self._can_reset(observation):
                self._return_to_idle()

        if observation.emergency_stop is True:
            self.phase = GeneratorPhase.FAULT
            self.deadline = None
            self.fault = "Активен Generators Emergency Stop"
            return []

        if self.phase == GeneratorPhase.EXTERNAL_RUNNING:
            # Это только наблюдение, поэтому оно должно продолжаться даже когда
            # все управляющие команды заблокированы внешним режимом.
            return self._observe_external_start(observation)

        if not actions_allowed or self.phase == GeneratorPhase.RECOVERY_REQUIRED:
            return []

        if self.phase == GeneratorPhase.FAULT:
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
                return self._abort_start(now, observation)
            if self._deadline_reached(now):
                self.phase = GeneratorPhase.WAITING_FOR_RUNNING
                self.deadline = now + self.profile.start_timeout_seconds
                return [self._action(
                    GeneratorActionKind.REMOTE_ON,
                    f"{self.profile.display_name}: заслонка подготовлена, подаём REMOTE START.",
                )]
            return []

        if self.phase == GeneratorPhase.WAITING_FOR_RUNNING:
            if not desired_running:
                return self._abort_start(now, observation)
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

        if self.phase == GeneratorPhase.CHOKE_HOLD:
            if not desired_running:
                return self._abort_start(now, observation)
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
                self.deadline = now + self.profile.warmup_seconds(self.start_temperature)
                return [self._action(
                    GeneratorActionKind.CHOKE_TO_RUN,
                    f"{self.profile.display_name}: переводим заслонку в рабочее положение.",
                )]
            return []

        if self.phase == GeneratorPhase.WARMING_UP:
            if not desired_running:
                return self._abort_start(now, observation)
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
            ):
                # Дом уже снят с генератора, а человек успел остановить
                # двигатель раньше программного cooldown. Команд не требуется.
                self._return_to_idle()
                return []
            if observation.running is not True:
                return self._set_fault(
                    f"{self.profile.display_name} потерял RUNNING в рабочем режиме."
                )
            if not desired_running:
                return self._begin_stop(now, observation)
            return []

        if self.phase == GeneratorPhase.WAITING_FOR_LOAD_RELEASE:
            if observation.running is not True:
                self._return_to_idle()
                return []
            if desired_running:
                self.phase = GeneratorPhase.READY_FOR_LOAD
                return []
            if observation.load_connected is False:
                return self._begin_cooldown(now)
            return []

        if self.phase == GeneratorPhase.COOLING_DOWN:
            if observation.running is not True:
                self._return_to_idle()
                return []
            if desired_running or observation.load_connected is True:
                self.phase = GeneratorPhase.READY_FOR_LOAD
                self.deadline = None
                return []
            if self._deadline_reached(now):
                self.phase = GeneratorPhase.WAITING_FOR_STOP
                self.deadline = now + self.profile.stop_timeout_seconds
                return [self._action(
                    GeneratorActionKind.REMOTE_OFF,
                    f"{self.profile.display_name}: cooldown завершён, снимаем REMOTE START.",
                )]
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
        self, now: float, observation: GeneratorObservation
    ) -> list[GeneratorAction]:
        self.start_temperature = observation.ambient_temperature_external
        self.start_temperature_source = (
            "ambient_temperature_external"
            if observation.ambient_temperature_external is not None
            else "conservative_fallback"
        )
        self.choke_used = self.profile.should_use_choke(self.start_temperature)
        self.phase = GeneratorPhase.PREPARING
        self.deadline = now + self.profile.choke_move_seconds

        position = "закрыта" if self.choke_used else "открыта"
        action = (
            GeneratorActionKind.CHOKE_TO_COLD
            if self.choke_used
            else GeneratorActionKind.CHOKE_TO_RUN
        )
        return [self._action(
            action,
            f"{self.profile.display_name}: начало запуска; заслонка {position}, "
            f"источник температуры — {self.start_temperature_source}.",
        )]

    def _running_confirmed(self, now: float) -> list[GeneratorAction]:
        if self.choke_used:
            self.phase = GeneratorPhase.CHOKE_HOLD
            self.deadline = now + self.profile.choke_hold_seconds
        else:
            self.phase = GeneratorPhase.WARMING_UP
            self.deadline = now + self.profile.warmup_seconds(self.start_temperature)
        return []

    def _begin_stop(
        self, now: float, observation: GeneratorObservation
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

    def _abort_start(
        self,
        now: float,
        observation: GeneratorObservation,
    ) -> list[GeneratorAction]:
        actions = [
            self._action(
                GeneratorActionKind.REMOTE_OFF,
                f"{self.profile.display_name}: запуск отменён, снимаем REMOTE START.",
            ),
            self._action(
                GeneratorActionKind.CHOKE_TO_RUN,
                f"{self.profile.display_name}: запуск отменён, открываем заслонку.",
            ),
        ]
        # Даже остановившийся двигатель ещё не означает, что REMOTE снялся.
        # Ждём подтверждения обоих признаков, как и при штатной остановке.
        self.phase = GeneratorPhase.WAITING_FOR_STOP
        self.deadline = now + self.profile.stop_timeout_seconds
        return actions

    def _cancel_stop(
        self, now: float, observation: GeneratorObservation
    ) -> list[GeneratorAction]:
        if observation.running is True:
            self.phase = GeneratorPhase.READY_FOR_LOAD
            self.deadline = None
        else:
            self.phase = GeneratorPhase.WAITING_FOR_RUNNING
            self.deadline = now + self.profile.start_timeout_seconds
        return [self._action(
            GeneratorActionKind.REMOTE_ON,
            f"{self.profile.display_name}: остановка отменена, снова подаём REMOTE START.",
        )]

    def _observe_external_start(
        self,
        observation: GeneratorObservation,
    ) -> list[GeneratorAction]:
        if observation.running is False and observation.remote_on is False:
            self._return_to_idle()
            return []
        # Даже при запросе Supervisor внешний двигатель не захватывается.
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

    def _can_reset(self, observation: GeneratorObservation) -> bool:
        return (
            observation.emergency_stop is False
            and observation.running is False
            and observation.remote_on is False
            and observation.load_connected is not True
        )

    def _return_to_idle(self) -> None:
        self.phase = GeneratorPhase.IDLE
        self.deadline = None
        self.fault = None
        self.choke_used = False

    def _deadline_reached(self, now: float) -> bool:
        return self.deadline is not None and now >= self.deadline

    def _action(self, kind: GeneratorActionKind, message: str) -> GeneratorAction:
        return GeneratorAction(slot=self.profile.slot, kind=kind, message=message)

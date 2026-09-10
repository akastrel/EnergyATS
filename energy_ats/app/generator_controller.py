"""Жизненный цикл одного генератора: запуск, прогрев, работа и остановка."""

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
    return {
        GeneratorSlot.A: GeneratorProfile(
            GeneratorSlot.A, "Generator A", "", ChokeStrategy.ALWAYS
        ),
        GeneratorSlot.B: GeneratorProfile(
            GeneratorSlot.B, "Generator B", "", ChokeStrategy.ALWAYS
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


_READY_PHASES = {
    GeneratorPhase.READY_FOR_LOAD,
    GeneratorPhase.WAITING_FOR_LOAD_RELEASE,
    GeneratorPhase.COOLING_DOWN,
}


class GeneratorController:
    """Подтверждаемый FSM одного двигателя; ownership задаёт Supervisor."""

    def __init__(self, profile: GeneratorProfile) -> None:
        self.profile = profile
        self.phase = GeneratorPhase.WAITING_FOR_DATA
        self.deadline: float | None = None
        self.fault: str | None = None
        self.start_temperature: float | None = None
        self.choke_used = False
        self.initialized = False

    def status(self, o: GeneratorObservation) -> GeneratorStatus:
        return GeneratorStatus(
            slot=self.profile.slot,
            display_name=self.profile.display_name,
            phase=self.phase,
            running=o.running,
            remote_on=o.remote_on,
            ready_for_load=(
                self.phase in _READY_PHASES
                and o.running is True
                and o.remote_on is True
            ),
            fault=self.fault,
        )

    def reset_if_safe(self, o: GeneratorObservation) -> bool:
        """Снять локальный fault только у физически остановленного двигателя."""
        if (
            o.required_states_known
            and o.emergency_stop is False
            and o.running is False
            and o.remote_on is False
        ):
            self._idle()
            self.initialized = True
            return True
        return False

    def step_authorized_shutdown(
        self,
        now: float,
        o: GeneratorObservation,
    ) -> tuple[list[GeneratorAction], str | None]:
        """Остановить разгруженный двигатель, на который Supervisor дал право."""
        if not o.required_states_known:
            return [], f"{self.profile.display_name}: неизвестны RUNNING или REMOTE."
        if o.emergency_stop is not False:
            return [], "Активен Generators Emergency Stop."
        if o.load_connected is not False:
            return [], f"{self.profile.display_name}: нагрузка не подтверждена как отключённая."

        if o.running is False and o.remote_on is False:
            self._idle()
            self.initialized = True
            return [], None

        self.initialized = True
        if self.phase == GeneratorPhase.WAITING_FOR_STOP:
            if self._expired(now):
                reason = (
                    f"{self.profile.display_name} не подтвердил остановку за "
                    f"{int(self.profile.stop_timeout_seconds)} с."
                )
                self._latch_fault(reason)
                return [], reason
            return [], None

        if self.phase == GeneratorPhase.COOLING_DOWN:
            if o.running is False or self._expired(now):
                return self._remote_off(now), None
            return [], None

        ensure_choke_run = self._choke_position_uncertain()
        if o.running is True:
            self.phase = GeneratorPhase.COOLING_DOWN
            self.deadline = now + self.profile.cooldown_seconds
            self.fault = None
            if ensure_choke_run:
                return [
                    self._action(
                        GeneratorActionKind.CHOKE_TO_RUN,
                        f"{self.profile.display_name}: перед безопасной остановкой "
                        "приводим заслонку в рабочее положение.",
                    )
                ], None
            return [], None

        actions: list[GeneratorAction] = []
        if ensure_choke_run:
            actions.append(
                self._action(
                    GeneratorActionKind.CHOKE_TO_RUN,
                    f"{self.profile.display_name}: перед остановкой открываем заслонку.",
                )
            )
        actions.extend(self._remote_off(now))
        return actions, None

    def step(
        self,
        now: float,
        o: GeneratorObservation,
        desired_running: bool,
        *,
        actions_allowed: bool = True,
        stable_managed_session: bool = False,
    ) -> list[GeneratorAction]:
        if not o.required_states_known:
            return []

        if not self.initialized:
            self._initialize(now, o, stable_managed_session)
            self.initialized = True

        if o.emergency_stop is True:
            self._latch_fault("Активен Generators Emergency Stop")
            return []

        if self.phase == GeneratorPhase.EXTERNAL_RUNNING:
            if o.running is False and o.remote_on is False:
                self._idle()
            return []
        if self.phase == GeneratorPhase.FAULT or not actions_allowed:
            return []

        if self.phase == GeneratorPhase.IDLE:
            if o.running is True or o.remote_on is True:
                self.phase = GeneratorPhase.EXTERNAL_RUNNING
                return []
            return self._begin_start(now, o) if desired_running else []

        if self.phase == GeneratorPhase.PREPARING:
            if not desired_running:
                return self._abort_start(now)
            if o.running is True:
                return self._fault_actions(
                    f"{self.profile.display_name}: RUNNING появился до команды REMOTE START."
                )
            if self._expired(now):
                self.phase = GeneratorPhase.WAITING_FOR_RUNNING
                self.deadline = now + self.profile.start_timeout_seconds
                return [self._action(
                    GeneratorActionKind.REMOTE_ON,
                    f"{self.profile.display_name}: заслонка подготовлена, подаём REMOTE START.",
                )]
            return []

        if self.phase == GeneratorPhase.WAITING_FOR_RUNNING:
            if not desired_running:
                return self._abort_start(now)
            if o.running is True:
                if o.remote_on is not True:
                    return self._fault_actions(
                        f"{self.profile.display_name}: RUNNING без управляемого REMOTE."
                    )
                self._running_confirmed(now)
                return []
            if self._expired(now):
                return self._fault_actions(
                    f"{self.profile.display_name} не подтвердил RUNNING за "
                    f"{int(self.profile.start_timeout_seconds)} с."
                )
            return []

        if self.phase == GeneratorPhase.HOLDING_COLD_START_CHOKE:
            if not desired_running:
                return self._abort_start(now)
            failure = self._running_failure(o, "при закрытой заслонке")
            if failure:
                return self._fault_actions(failure)
            if self._expired(now):
                self.phase = GeneratorPhase.WARMING_UP
                self.deadline = now + self.profile.warmup_seconds(self.start_temperature)
                return [self._action(
                    GeneratorActionKind.CHOKE_TO_RUN,
                    f"{self.profile.display_name}: открываем заслонку после запуска.",
                )]
            return []

        if self.phase == GeneratorPhase.WARMING_UP:
            if not desired_running:
                return self._abort_start(now)
            failure = self._running_failure(o, "во время прогрева")
            if failure:
                return self._fault_actions(failure)
            if self._expired(now):
                self.phase = GeneratorPhase.READY_FOR_LOAD
                self.deadline = None
            return []

        if self.phase == GeneratorPhase.READY_FOR_LOAD:
            if not desired_running and self._stopped(o) and o.load_connected is False:
                self._idle()
                return []
            failure = self._running_failure(o, "в рабочем режиме")
            if failure:
                return self._fault_actions(failure)
            if not desired_running:
                if o.load_connected is False:
                    self._cooldown(now)
                else:
                    self.phase = GeneratorPhase.WAITING_FOR_LOAD_RELEASE
                    self.deadline = None
            return []

        if self.phase == GeneratorPhase.WAITING_FOR_LOAD_RELEASE:
            if self._stopped(o) and o.load_connected is False:
                self._idle()
                return []
            failure = self._running_failure(o, "до снятия нагрузки")
            if failure:
                return self._fault_actions(failure)
            if desired_running:
                self.phase = GeneratorPhase.READY_FOR_LOAD
            elif o.load_connected is False:
                self._cooldown(now)
            return []

        if self.phase == GeneratorPhase.COOLING_DOWN:
            if self._stopped(o) and o.load_connected is not True:
                self._idle()
                return []
            failure = self._running_failure(o, "во время cooldown")
            if failure:
                return self._fault_actions(failure)
            if desired_running or o.load_connected is True:
                self.phase = GeneratorPhase.READY_FOR_LOAD
                self.deadline = None
            elif self._expired(now):
                return self._remote_off(now)
            return []

        if self.phase == GeneratorPhase.WAITING_FOR_STOP:
            if self._stopped(o):
                self._idle()
                return []
            if desired_running:
                self.phase = (
                    GeneratorPhase.READY_FOR_LOAD
                    if o.running is True
                    else GeneratorPhase.WAITING_FOR_RUNNING
                )
                self.deadline = (
                    None
                    if o.running is True
                    else now + self.profile.start_timeout_seconds
                )
                return [self._action(
                    GeneratorActionKind.REMOTE_ON,
                    f"{self.profile.display_name}: остановка отменена, снова подаём REMOTE START.",
                )]
            if self._expired(now):
                return self._fault_actions(
                    f"{self.profile.display_name} не остановился за "
                    f"{int(self.profile.stop_timeout_seconds)} с."
                )
        return []

    # State helpers ----------------------------------------------------

    def _initialize(
        self,
        now: float,
        o: GeneratorObservation,
        stable_managed: bool,
    ) -> None:
        if o.emergency_stop is True:
            self._latch_fault("Активен Generators Emergency Stop")
        elif o.running is True:
            if stable_managed and o.remote_on is True:
                if o.load_connected is False:
                    # Stable managed session with no house load is the exercise
                    # restart case. GC's exact transient phase was not persisted,
                    # so do not pretend the choke/warmup state is known. We wait
                    # one conservative choke-hold interval and then idempotently
                    # command CHOKE_TO_RUN before continuing.
                    self.start_temperature = o.ambient_temperature_external
                    self.choke_used = True
                    self.phase = GeneratorPhase.HOLDING_COLD_START_CHOKE
                    self.deadline = now + self.profile.cold_start_choke_hold_seconds
                    self.fault = None
                else:
                    self.phase = GeneratorPhase.READY_FOR_LOAD
            else:
                self.phase = GeneratorPhase.EXTERNAL_RUNNING
        elif o.remote_on is True:
            if stable_managed:
                self._latch_fault(
                    "После restart REMOTE активен, но RUNNING не подтверждён."
                )
            else:
                self.phase = GeneratorPhase.EXTERNAL_RUNNING
        else:
            self._idle()

    def _begin_start(self, now: float, o: GeneratorObservation) -> list[GeneratorAction]:
        self.start_temperature = o.ambient_temperature_external
        self.choke_used = self.profile.should_use_choke(self.start_temperature)
        self.phase = GeneratorPhase.PREPARING
        self.deadline = now + self.profile.choke_move_seconds
        kind = (
            GeneratorActionKind.CHOKE_TO_COLD_START
            if self.choke_used
            else GeneratorActionKind.CHOKE_TO_RUN
        )
        return [self._action(
            kind,
            f"{self.profile.display_name}: начало запуска; заслонка "
            f"{'закрыта' if self.choke_used else 'открыта'}.",
        )]

    def _running_confirmed(self, now: float) -> None:
        if self.choke_used:
            self.phase = GeneratorPhase.HOLDING_COLD_START_CHOKE
            self.deadline = now + self.profile.cold_start_choke_hold_seconds
        else:
            self.phase = GeneratorPhase.WARMING_UP
            self.deadline = now + self.profile.warmup_seconds(self.start_temperature)

    def _cooldown(self, now: float) -> None:
        self.phase = GeneratorPhase.COOLING_DOWN
        self.deadline = now + self.profile.cooldown_seconds

    def _remote_off(self, now: float) -> list[GeneratorAction]:
        self.phase = GeneratorPhase.WAITING_FOR_STOP
        self.deadline = now + self.profile.stop_timeout_seconds
        return [self._action(
            GeneratorActionKind.REMOTE_OFF,
            f"{self.profile.display_name}: cooldown завершён, снимаем REMOTE START.",
        )]

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

    def _running_failure(self, o: GeneratorObservation, where: str) -> str | None:
        if o.running is not True:
            return f"{self.profile.display_name} потерял RUNNING {where}."
        if o.remote_on is not True:
            return f"{self.profile.display_name} потерял REMOTE {where}."
        return None

    def _fault_actions(self, reason: str) -> list[GeneratorAction]:
        self._latch_fault(reason)
        return [
            self._action(GeneratorActionKind.REMOTE_OFF, reason),
            self._action(
                GeneratorActionKind.CHOKE_TO_RUN,
                f"{self.profile.display_name}: после ошибки открываем заслонку.",
            ),
        ]

    def _latch_fault(self, reason: str) -> None:
        self.phase = GeneratorPhase.FAULT
        self.deadline = None
        self.fault = reason

    def _idle(self) -> None:
        self.phase = GeneratorPhase.IDLE
        self.deadline = None
        self.fault = None
        self.choke_used = False

    def _choke_position_uncertain(self) -> bool:
        return self.phase in {
            GeneratorPhase.WAITING_FOR_DATA,
            GeneratorPhase.PREPARING,
            GeneratorPhase.WAITING_FOR_RUNNING,
            GeneratorPhase.HOLDING_COLD_START_CHOKE,
            GeneratorPhase.WARMING_UP,
            GeneratorPhase.EXTERNAL_RUNNING,
            GeneratorPhase.FAULT,
        }

    @staticmethod
    def _stopped(o: GeneratorObservation) -> bool:
        return o.running is False and o.remote_on is False

    def _expired(self, now: float) -> bool:
        return self.deadline is not None and now >= self.deadline

    def _action(self, kind: GeneratorActionKind, message: str) -> GeneratorAction:
        return GeneratorAction(self.profile.slot, kind, message)

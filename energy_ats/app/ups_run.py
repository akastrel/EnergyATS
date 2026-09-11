"""UPS Run: ожидание на UPS и циклическая подзарядка при длительном outage.

Модуль намеренно не знает о Home Assistant, GC, TPC и силовых реле. Он отвечает
только на два вопроса:

* можно ли сейчас продолжать ждать на UPS вместо автоматического запуска;
* можно ли завершить принадлежащую UPS Run generator-session после достижения
  целевого SoC.

Физические действия по-прежнему выполняют EnergySupervisor, GC и TPC.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Hashable, Mapping

from domain import SessionReason, SupervisorEvent


class UPSRunState(str, Enum):
    IDLE = "idle"
    WAITING_ON_UPS = "waiting_on_ups"
    GENERATOR_REQUIRED = "generator_required"
    CHARGING = "charging"
    TARGET_REACHED = "target_reached"
    DEGRADED = "degraded"


@dataclass(frozen=True)
class UPSRunConfig:
    delayed_start_enabled: bool = False
    charge_cycle_enabled: bool = False
    start_soc: float = 40.0
    target_soc: float = 80.0
    min_ttg_before_start: float = 60.0
    max_start_delay: float = 6 * 60 * 60
    telemetry_stale_time: float = 300.0

    @property
    def valid(self) -> bool:
        values = (
            self.start_soc,
            self.target_soc,
            self.min_ttg_before_start,
            self.max_start_delay,
            self.telemetry_stale_time,
        )
        return (
            all(math.isfinite(value) for value in values)
            and 0 < self.start_soc < self.target_soc <= 100
            and self.min_ttg_before_start >= 0
            and self.max_start_delay >= 0
            and self.telemetry_stale_time > 0
        )


@dataclass(frozen=True)
class BatteryObservation:
    soc: float | None
    ttg_minutes: float | None
    discharging: bool | None
    ready: bool | None
    sample_id: Hashable | None = None
    ttg_sample_id: Hashable | None = None
    soc_updated_at: float | None = None
    ttg_updated_at: float | None = None


@dataclass(frozen=True)
class UPSRunObservation:
    now: float
    grid_ready: bool | None
    automatic_transfer_enabled: bool
    core_delay_elapsed: bool
    battery: BatteryObservation
    session_reason: SessionReason | None = None
    session_active: bool = False
    session_on_generator: bool = False
    session_cycle_owned: bool = False
    session_manual_override: bool = False
    manual_start_pending: bool = False
    grid_stable: bool = True
    grid_supply_restored: bool = True


@dataclass(frozen=True)
class UPSRunDecision:
    defer_automatic_start: bool = False
    outage_delay_already_satisfied: bool = False
    claim_new_outage_session: bool = False
    request_cycle_stop: bool = False
    restore_grid_after_cycle: bool = False
    reason: str | None = None
    events: tuple[SupervisorEvent, ...] = ()


class UPSRun:
    """Локальная stateful-стратегия длительного outage без аппаратной FSM."""

    def __init__(self, config: UPSRunConfig | None = None) -> None:
        self.config = config or UPSRunConfig()
        self.state = UPSRunState.IDLE
        self.waiting_since: float | None = None
        self.post_cycle_wait = False
        self.last_reason: str | None = None

        # SoC и TTG проверяются отдельно: обновление одного сигнала не делает
        # второй свежим. При restart используем также timestamps из HA cache.
        self._last_sample_id: Hashable | None = None
        self._last_sample_at: float | None = None
        self._last_ttg_sample_id: Hashable | None = None
        self._last_ttg_sample_at: float | None = None
        self._last_event_key: str | None = None

    @property
    def enabled(self) -> bool:
        return self.config.delayed_start_enabled or self.config.charge_cycle_enabled

    def begin_post_cycle_wait(self, now: float) -> None:
        """Начать следующий battery interval после штатного cycle stop."""
        self.post_cycle_wait = True
        self.waiting_since = now
        self.state = UPSRunState.WAITING_ON_UPS
        self.last_reason = (
            "Цикл заряда завершён; ожидаем следующей необходимости запуска."
        )
        self._last_event_key = None

    def step(self, o: UPSRunObservation) -> UPSRunDecision:
        events: list[SupervisorEvent] = []
        self._observe_sample(o.now, o.battery.sample_id)
        ttg_sample = (
            o.battery.ttg_sample_id
            if o.battery.ttg_sample_id is not None
            else o.battery.sample_id
        )
        if ttg_sample is not None and ttg_sample != self._last_ttg_sample_id:
            self._last_ttg_sample_id = ttg_sample
            self._last_ttg_sample_at = o.now

        if o.grid_ready is True:
            # Короткое появление сети не обнуляет текущий battery interval.
            # После cycle stop сетевой ввод изолирован нами: обязанность
            # вернуть его сохраняется до подтверждения Grid supply.
            restore_grid = self.post_cycle_wait and not o.session_active
            if not o.grid_stable or (restore_grid and not o.grid_supply_restored):
                return UPSRunDecision(restore_grid_after_cycle=restore_grid)
            self._reset_wait()
            self.post_cycle_wait = False
            self.state = UPSRunState.IDLE
            self.last_reason = None
            self._last_event_key = None
            return UPSRunDecision()

        if o.grid_ready is not False:
            return UPSRunDecision()

        if not self.enabled or not o.automatic_transfer_enabled:
            self._reset_wait()
            # Отключение оптимизации не снимает обязанность вернуть сетевой
            # ввод, ранее изолированный при нашем cycle stop.
            self.state = UPSRunState.IDLE
            self.last_reason = None
            return UPSRunDecision()

        if not self.config.valid:
            reason = "Некорректны настройки Delayed Start / Charge Cycling."
            self.state = UPSRunState.DEGRADED
            self.last_reason = reason
            self._emit_once(events, "invalid_config", "warning", reason)
            return UPSRunDecision(reason=reason, events=tuple(events))

        if o.session_active:
            self._reset_wait()
            self.post_cycle_wait = False
            return self._with_session(o, events)

        return self._without_session(o, events)

    def _without_session(
        self,
        o: UPSRunObservation,
        events: list[SupervisorEvent],
    ) -> UPSRunDecision:
        # Пользовательская команда всегда сильнее delayed start. Она будет
        # обработана Supervisor в этом же tick.
        if o.manual_start_pending:
            self.state = UPSRunState.GENERATOR_REQUIRED
            self.last_reason = "Пользователь запросил питание от генератора."
            return UPSRunDecision(reason=self.last_reason)

        delay_satisfied = self.waiting_since is not None
        if not delay_satisfied and not o.core_delay_elapsed:
            self.state = UPSRunState.IDLE
            self.last_reason = None
            return UPSRunDecision()

        # Начало собственного интервала ожидания происходит только после
        # обычного grid_failure_delay. При restart persisted waiting_since
        # сохраняет уже прошедшее время и сообщает Supervisor, что повторять
        # core delay не нужно.
        if self.waiting_since is None:
            self.waiting_since = o.now
            delay_satisfied = True

        claim = self.config.charge_cycle_enabled
        should_wait_on_battery = (
            self.config.delayed_start_enabled or self.post_cycle_wait
        )

        # При отключённом Delayed Start первый automatic run начинается сразу
        # после core delay. Но последующие cycling intervals всё равно обязаны
        # ждать Start SoC/TTG/max-delay, иначе generator тут же перезапустится.
        if not should_wait_on_battery:
            self.state = UPSRunState.GENERATOR_REQUIRED
            self.last_reason = "Delayed Start выключен."
            return UPSRunDecision(
                outage_delay_already_satisfied=delay_satisfied,
                claim_new_outage_session=claim,
                reason=self.last_reason,
            )

        battery_reason = self._battery_problem(o)
        if battery_reason is not None:
            self.state = UPSRunState.GENERATOR_REQUIRED
            self.last_reason = battery_reason
            self._emit_once(
                events,
                f"battery:{battery_reason}",
                "warning",
                f"Delayed Start отменён: {battery_reason} Запускаем генератор штатно.",
            )
            return UPSRunDecision(
                outage_delay_already_satisfied=delay_satisfied,
                claim_new_outage_session=claim,
                reason=battery_reason,
                events=tuple(events),
            )

        assert o.battery.soc is not None
        elapsed = max(0.0, o.now - self.waiting_since)
        reason: str | None = None

        if o.battery.soc <= self.config.start_soc:
            reason = (
                f"SoC {o.battery.soc:.1f}% достиг порога запуска "
                f"{self.config.start_soc:.1f}%."
            )
        elif (
            o.battery.discharging is True
            and o.battery.ttg_minutes is not None
            and o.battery.ttg_minutes <= self.config.min_ttg_before_start
        ):
            reason = (
                f"TTG {o.battery.ttg_minutes:.0f} мин достиг порога "
                f"{self.config.min_ttg_before_start:.0f} мин."
            )
        elif elapsed >= self.config.max_start_delay:
            reason = "Достигнута максимальная задержка запуска генератора."

        if reason is not None:
            self.state = UPSRunState.GENERATOR_REQUIRED
            self.last_reason = reason
            self._emit_once(
                events,
                f"start:{reason}",
                "info",
                f"Delayed Start завершён: {reason}",
            )
            return UPSRunDecision(
                outage_delay_already_satisfied=True,
                claim_new_outage_session=claim,
                reason=reason,
                events=tuple(events),
            )

        self.state = UPSRunState.WAITING_ON_UPS
        self.last_reason = "UPS продолжает питать критическую линию."
        return UPSRunDecision(
            defer_automatic_start=True,
            outage_delay_already_satisfied=True,
            reason=self.last_reason,
            events=tuple(events),
        )

    def _with_session(
        self,
        o: UPSRunObservation,
        events: list[SupervisorEvent],
    ) -> UPSRunDecision:
        # Ручная команда, уже ожидающая обработки Supervisor, немедленно
        # запрещает Target-SoC stop в этом tick.
        if o.manual_start_pending:
            self.state = UPSRunState.IDLE
            self.last_reason = (
                "Пользователь принял generator-session под ручное управление."
            )
            return UPSRunDecision(reason=self.last_reason)

        if o.session_reason != SessionReason.GRID_OUTAGE:
            self.state = UPSRunState.IDLE
            self.last_reason = None
            return UPSRunDecision()

        if (
            not self.config.charge_cycle_enabled
            or not o.session_cycle_owned
            or o.session_manual_override
        ):
            self.state = UPSRunState.IDLE
            self.last_reason = None
            return UPSRunDecision()

        if not o.session_on_generator:
            self.state = UPSRunState.CHARGING
            self.last_reason = "Generator-session ещё не в устойчивом режиме."
            return UPSRunDecision(reason=self.last_reason)

        soc = o.battery.soc
        if (
            not _valid_soc(soc)
            or self._telemetry_stale(o.now, o.battery.soc_updated_at)
            or o.battery.ready is not True
        ):
            reason = "SoC или готовность батареи недостоверны; cycling не завершает generator-session."
            self.state = UPSRunState.DEGRADED
            self.last_reason = reason
            self._emit_once(events, "cycle_soc_invalid", "warning", reason)
            return UPSRunDecision(reason=reason, events=tuple(events))

        assert soc is not None
        if soc >= self.config.target_soc:
            reason = (
                f"Батарея заряжена до {soc:.1f}% "
                f"(target {self.config.target_soc:.1f}%)."
            )
            self.state = UPSRunState.TARGET_REACHED
            self.last_reason = reason
            self._emit_once(
                events,
                "target_reached",
                "info",
                f"{reason} Завершаем автоматический charge cycle.",
            )
            return UPSRunDecision(
                request_cycle_stop=True,
                reason=reason,
                events=tuple(events),
            )

        self.state = UPSRunState.CHARGING
        self.last_reason = (
            f"Заряд батареи {soc:.1f}%; ожидаем target {self.config.target_soc:.1f}%."
        )
        self._last_event_key = None
        return UPSRunDecision(reason=self.last_reason)

    def _battery_problem(self, o: UPSRunObservation) -> str | None:
        b = o.battery
        if b.ready is False:
            return "UPS/Battery сообщает критическое состояние."
        if b.ready is None:
            return "состояние готовности UPS/Battery неизвестно."
        if not _valid_soc(b.soc):
            return "SoC батареи недоступен или некорректен."
        if self._telemetry_stale(o.now, b.soc_updated_at):
            return "телеметрия SoC батареи устарела."
        if b.discharging is None:
            return "неизвестно, разряжается ли батарея."
        if b.discharging:
            if not _valid_nonnegative(b.ttg_minutes):
                return "TTG батареи недоступен или некорректен при разряде."
            if self._stale(o.now, self._last_ttg_sample_at, b.ttg_updated_at):
                return "телеметрия TTG батареи устарела."
        return None

    def _observe_sample(self, now: float, sample_id: Hashable | None) -> None:
        if sample_id is None:
            return
        if sample_id != self._last_sample_id:
            self._last_sample_id = sample_id
            self._last_sample_at = now

    def _telemetry_stale(self, now: float, updated_at: float | None = None) -> bool:
        return self._stale(now, self._last_sample_at, updated_at)

    def _stale(
        self, now: float, observed_at: float | None, updated_at: float | None
    ) -> bool:
        return (
            observed_at is None
            or now - observed_at > self.config.telemetry_stale_time
            or (
                updated_at is not None
                and (
                    not math.isfinite(updated_at)
                    or now - updated_at > self.config.telemetry_stale_time
                )
            )
        )

    def _reset_wait(self) -> None:
        self.waiting_since = None

    def _emit_once(
        self,
        events: list[SupervisorEvent],
        key: str,
        level: str,
        message: str,
    ) -> None:
        if self._last_event_key == key:
            return
        self._last_event_key = key
        events.append(SupervisorEvent(level, message))

    def status_attributes(
        self, now: float, battery: BatteryObservation
    ) -> dict[str, object]:
        elapsed = (
            max(0.0, now - self.waiting_since)
            if self.waiting_since is not None
            else None
        )
        remaining = (
            max(0.0, self.config.max_start_delay - elapsed)
            if elapsed is not None
            else None
        )
        return {
            "delayed_start_enabled": self.config.delayed_start_enabled,
            "charge_cycle_enabled": self.config.charge_cycle_enabled,
            "charge_cycle_state": self.state.value,
            "delayed_start_reason": self.last_reason,
            "battery_soc": battery.soc if _valid_soc(battery.soc) else None,
            "battery_ttg_minutes": (
                battery.ttg_minutes if _valid_nonnegative(battery.ttg_minutes) else None
            ),
            "battery_discharging": battery.discharging,
            "battery_ready": battery.ready,
            "generator_start_soc": self.config.start_soc,
            "generator_target_charge_soc": self.config.target_soc,
            "delayed_start_elapsed_seconds": elapsed,
            "delayed_start_remaining_seconds": remaining,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "waiting_since": self.waiting_since,
            "post_cycle_wait": self.post_cycle_wait,
            "last_reason": self.last_reason,
        }

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
        config: UPSRunConfig,
    ) -> "UPSRun":
        ups_run = cls(config)
        ups_run.state = UPSRunState(
            str(data.get("state", UPSRunState.IDLE.value))
        )
        waiting = data.get("waiting_since")
        ups_run.waiting_since = float(waiting) if waiting is not None else None
        if ups_run.waiting_since is not None and (
            not math.isfinite(ups_run.waiting_since) or ups_run.waiting_since < 0
        ):
            raise ValueError(
                "waiting_since должен быть конечным неотрицательным временем"
            )
        post_cycle_wait = data.get("post_cycle_wait", False)
        if type(post_cycle_wait) is not bool:
            raise ValueError("post_cycle_wait должен быть boolean")
        ups_run.post_cycle_wait = post_cycle_wait
        reason = data.get("last_reason")
        ups_run.last_reason = str(reason) if reason is not None else None
        return ups_run


def _valid_soc(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and 0 <= value <= 100


def _valid_nonnegative(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value >= 0

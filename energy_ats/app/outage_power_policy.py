"""Policy задержки запуска генератора и циклической подзарядки UPS.

Модуль намеренно не знает о Home Assistant, GC, TPC и силовых реле. Он отвечает
только на два вопроса:

* можно ли сейчас продолжать ждать на UPS вместо автоматического запуска;
* можно ли завершить принадлежащую policy generator-session после достижения
  целевого SoC.

Физические действия по-прежнему выполняют EnergySupervisor, GC и TPC.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Hashable, Mapping

from domain import SessionReason, SupervisorEvent


class OutagePowerState(str, Enum):
    IDLE = "idle"
    WAITING_ON_UPS = "waiting_on_ups"
    GENERATOR_REQUIRED = "generator_required"
    CHARGING = "charging"
    TARGET_REACHED = "target_reached"
    DEGRADED = "degraded"


@dataclass(frozen=True)
class OutagePowerConfig:
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


@dataclass(frozen=True)
class OutagePowerObservation:
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


@dataclass(frozen=True)
class OutagePowerDecision:
    defer_automatic_start: bool = False
    outage_delay_already_satisfied: bool = False
    claim_new_outage_session: bool = False
    request_cycle_stop: bool = False
    reason: str | None = None
    events: tuple[SupervisorEvent, ...] = ()


class OutagePowerPolicy:
    """Небольшой stateful policy-слой без собственной аппаратной FSM."""

    def __init__(self, config: OutagePowerConfig | None = None) -> None:
        self.config = config or OutagePowerConfig()
        self.state = OutagePowerState.IDLE
        self.waiting_since: float | None = None
        self.last_reason: str | None = None

        # Freshness нужна только для delayed start. Measurement state после
        # restart намеренно не восстанавливается: первое полученное значение
        # считается новым наблюдением, а не старым накопленным доказательством.
        self._last_sample_id: Hashable | None = None
        self._last_sample_at: float | None = None
        self._last_event_key: str | None = None

    @property
    def enabled(self) -> bool:
        return self.config.delayed_start_enabled or self.config.charge_cycle_enabled

    def step(self, o: OutagePowerObservation) -> OutagePowerDecision:
        events: list[SupervisorEvent] = []
        self._observe_sample(o.now, o.battery.sample_id)

        if o.grid_ready is True:
            self._reset_wait()
            self.state = OutagePowerState.IDLE
            self.last_reason = None
            self._last_event_key = None
            return OutagePowerDecision()

        if not self.enabled or not o.automatic_transfer_enabled:
            self._reset_wait()
            self.state = OutagePowerState.IDLE
            self.last_reason = None
            return OutagePowerDecision()

        if not self.config.valid:
            reason = "Некорректны настройки Delayed Start / Charge Cycling."
            self.state = OutagePowerState.DEGRADED
            self.last_reason = reason
            self._emit_once(events, "invalid_config", "warning", reason)
            return OutagePowerDecision(reason=reason, events=tuple(events))

        if o.session_active:
            self._reset_wait()
            return self._with_session(o, events)

        return self._without_session(o, events)

    def _without_session(
        self,
        o: OutagePowerObservation,
        events: list[SupervisorEvent],
    ) -> OutagePowerDecision:
        # Пользовательская команда всегда сильнее delayed start. Она будет
        # обработана Supervisor в этом же tick.
        if o.manual_start_pending:
            self.state = OutagePowerState.GENERATOR_REQUIRED
            self.last_reason = "Пользователь запросил питание от генератора."
            return OutagePowerDecision(reason=self.last_reason)

        delay_satisfied = self.waiting_since is not None
        if not delay_satisfied and not o.core_delay_elapsed:
            self.state = OutagePowerState.IDLE
            self.last_reason = None
            return OutagePowerDecision()

        # Начало собственного интервала ожидания происходит только после
        # обычного grid_failure_delay. При restart persisted waiting_since
        # сохраняет уже прошедшее время и сообщает Supervisor, что повторять
        # core delay не нужно.
        if self.waiting_since is None:
            self.waiting_since = o.now
            delay_satisfied = True

        claim = self.config.charge_cycle_enabled

        if not self.config.delayed_start_enabled:
            self.state = OutagePowerState.GENERATOR_REQUIRED
            self.last_reason = "Delayed Start выключен."
            return OutagePowerDecision(
                outage_delay_already_satisfied=delay_satisfied,
                claim_new_outage_session=claim,
                reason=self.last_reason,
            )

        battery_reason = self._battery_problem(o)
        if battery_reason is not None:
            self.state = OutagePowerState.GENERATOR_REQUIRED
            self.last_reason = battery_reason
            self._emit_once(
                events,
                f"battery:{battery_reason}",
                "warning",
                f"Delayed Start отменён: {battery_reason} Запускаем генератор штатно.",
            )
            return OutagePowerDecision(
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
            self.state = OutagePowerState.GENERATOR_REQUIRED
            self.last_reason = reason
            self._emit_once(
                events,
                f"start:{reason}",
                "info",
                f"Delayed Start завершён: {reason}",
            )
            return OutagePowerDecision(
                outage_delay_already_satisfied=True,
                claim_new_outage_session=claim,
                reason=reason,
                events=tuple(events),
            )

        self.state = OutagePowerState.WAITING_ON_UPS
        self.last_reason = "UPS продолжает питать критическую линию."
        return OutagePowerDecision(
            defer_automatic_start=True,
            outage_delay_already_satisfied=True,
            reason=self.last_reason,
            events=tuple(events),
        )

    def _with_session(
        self,
        o: OutagePowerObservation,
        events: list[SupervisorEvent],
    ) -> OutagePowerDecision:
        if o.session_reason != SessionReason.GRID_OUTAGE:
            self.state = OutagePowerState.IDLE
            self.last_reason = None
            return OutagePowerDecision()

        if (
            not self.config.charge_cycle_enabled
            or not o.session_cycle_owned
            or o.session_manual_override
        ):
            self.state = OutagePowerState.IDLE
            self.last_reason = None
            return OutagePowerDecision()

        if not o.session_on_generator:
            self.state = OutagePowerState.CHARGING
            self.last_reason = "Generator-session ещё не в устойчивом режиме."
            return OutagePowerDecision(reason=self.last_reason)

        soc = o.battery.soc
        if not _valid_soc(soc):
            reason = "SoC батареи недоступен или некорректен; cycling не завершает generator-session."
            self.state = OutagePowerState.DEGRADED
            self.last_reason = reason
            self._emit_once(events, "cycle_soc_invalid", "warning", reason)
            return OutagePowerDecision(reason=reason, events=tuple(events))

        assert soc is not None
        if soc >= self.config.target_soc:
            reason = (
                f"Батарея заряжена до {soc:.1f}% "
                f"(target {self.config.target_soc:.1f}%)."
            )
            self.state = OutagePowerState.TARGET_REACHED
            self.last_reason = reason
            self._emit_once(
                events,
                "target_reached",
                "info",
                f"{reason} Завершаем автоматический charge cycle.",
            )
            return OutagePowerDecision(
                request_cycle_stop=True,
                reason=reason,
                events=tuple(events),
            )

        self.state = OutagePowerState.CHARGING
        self.last_reason = (
            f"Заряд батареи {soc:.1f}%; ожидаем target {self.config.target_soc:.1f}%."
        )
        self._last_event_key = None
        return OutagePowerDecision(reason=self.last_reason)

    def _battery_problem(self, o: OutagePowerObservation) -> str | None:
        b = o.battery
        if b.ready is False:
            return "UPS/Battery сообщает критическое состояние."
        if b.ready is None:
            return "состояние готовности UPS/Battery неизвестно."
        if not _valid_soc(b.soc):
            return "SoC батареи недоступен или некорректен."
        if b.discharging is None:
            return "неизвестно, разряжается ли батарея."
        if b.discharging:
            if not _valid_nonnegative(b.ttg_minutes):
                return "TTG батареи недоступен или некорректен при разряде."
            if self._telemetry_stale(o.now):
                return "телеметрия батареи устарела."
        return None

    def _observe_sample(self, now: float, sample_id: Hashable | None) -> None:
        if sample_id is None:
            return
        if sample_id != self._last_sample_id:
            self._last_sample_id = sample_id
            self._last_sample_at = now

    def _telemetry_stale(self, now: float) -> bool:
        return (
            self._last_sample_at is None
            or now - self._last_sample_at > self.config.telemetry_stale_time
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

    def status_attributes(self, now: float, battery: BatteryObservation) -> dict[str, object]:
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
            "battery_soc": battery.soc,
            "battery_ttg_minutes": battery.ttg_minutes,
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
            "last_reason": self.last_reason,
        }

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
        config: OutagePowerConfig,
    ) -> "OutagePowerPolicy":
        policy = cls(config)
        policy.state = OutagePowerState(str(data.get("state", OutagePowerState.IDLE.value)))
        waiting = data.get("waiting_since")
        policy.waiting_since = float(waiting) if waiting is not None else None
        reason = data.get("last_reason")
        policy.last_reason = str(reason) if reason is not None else None
        return policy


def _valid_soc(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and 0 <= value <= 100


def _valid_nonnegative(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value >= 0

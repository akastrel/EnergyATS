"""Policy управления некритичными нагрузками G1/G2 на generator bus."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Hashable, Mapping

from domain import EventVisibility, GeneratorSlot, SupervisorEvent


class LoadGroup(str, Enum):
    G1 = "g1"
    G2 = "g2"


_GROUP_NAMES = {
    LoadGroup.G1: "некритичные нагрузки 1-го этажа",
    LoadGroup.G2: "некритичные нагрузки цокольного этажа",
}


class LoadActionKind(str, Enum):
    TURN_ON = "turn_on"
    TURN_OFF = "turn_off"


@dataclass(frozen=True)
class LoadAction:
    group: LoadGroup
    kind: LoadActionKind
    message: str


class LoadManagerPhase(str, Enum):
    """Только крупные состояния; конкретная операция хранится отдельно."""

    DISABLED = "disabled"
    IDLE = "idle"
    LOAD_SHEDDING = "load_shedding"
    MEASURING = "measuring"
    STABLE = "stable"
    DEGRADED = "degraded"


@dataclass(frozen=True)
class LoadManagerConfig:
    enabled: bool = False
    measurement_stabilization_time: float = 10.0
    restore_margin_percent: float = 15.0
    nominal_overload_time: float = 20.0
    maximum_overload_confirmation_time: float = 4.0
    restore_retry_interval: float = 300.0

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("load_management_enabled должен быть boolean")
        for name, value in (
            ("load_measurement_stabilization_time", self.measurement_stabilization_time),
            ("nominal_overload_time", self.nominal_overload_time),
            ("maximum_overload_confirmation_time", self.maximum_overload_confirmation_time),
            ("load_restore_retry_interval", self.restore_retry_interval),
        ):
            if value < 0:
                raise ValueError(f"{name} не может быть < 0")
        if not 0 <= self.restore_margin_percent < 100:
            raise ValueError("load_restore_margin_percent должен быть в диапазоне [0, 100)")


@dataclass(frozen=True)
class LoadManagerObservation:
    now: float
    house_on_generator: bool | None
    house_on_grid: bool | None
    desired_generator_supply: bool
    managed_generator_ready: bool
    power_transition_in_progress: bool
    bus_owner: GeneratorSlot | None
    nominal_power: float | None
    maximum_power: float | None
    meter_ready: bool | None
    generator_power: float | None
    power_sample_id: Hashable | None
    groups: Mapping[LoadGroup, bool | None]
    generator_name: str | None = None
    actions_enabled: bool = True


@dataclass(frozen=True)
class LoadManagerDecision:
    actions: tuple[LoadAction, ...]
    events: tuple[SupervisorEvent, ...]
    notifications: tuple[str, ...]
    transfer_permitted: bool


@dataclass
class _PendingAction:
    group: LoadGroup
    target_on: bool
    deadline: float
    reason: str
    block_transfer: bool = False
    measurement_after: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "group": self.group.value,
            "target_on": self.target_on,
            "deadline": self.deadline,
            "reason": self.reason,
            "block_transfer": self.block_transfer,
            "measurement_after": self.measurement_after,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "_PendingAction":
        target = data.get("target_on")
        if type(target) is not bool:
            raise ValueError("load_manager.pending_action.target_on должен быть boolean")
        return cls(
            LoadGroup(str(data["group"])),
            target,
            float(data["deadline"]),
            str(data.get("reason", "")),
            bool(data.get("block_transfer", False)),
            str(data["measurement_after"])
            if data.get("measurement_after") is not None
            else None,
        )


class LoadManager:
    """Одна простая FSM + operation/data вместо отдельной фазы на каждый шаг."""

    _RESTORE_ORDER = (LoadGroup.G1, LoadGroup.G2)
    _SHED_ORDER = (LoadGroup.G2, LoadGroup.G1)

    def __init__(self, config: LoadManagerConfig | None = None) -> None:
        self.config = config or LoadManagerConfig()
        self.phase = (
            LoadManagerPhase.IDLE
            if self.config.enabled
            else LoadManagerPhase.DISABLED
        )
        self.operation: str | None = None
        self.shed_by_energy_ats = {LoadGroup.G1: False, LoadGroup.G2: False}
        self.pending_action: _PendingAction | None = None
        self.degraded_reason: str | None = None
        self.last_reason: str | None = None
        self.next_restore_retry: float | None = None

        self.last_generator_power: float | None = None
        self.active_nominal_power: float | None = None
        self.active_maximum_power: float | None = None
        self.last_group_states = {LoadGroup.G1: None, LoadGroup.G2: None}

        self._measurement_reason: str | None = None
        self._measurement_started_at: float | None = None
        self._measurement_count = 0
        self._measurement_max: float | None = None
        # Stream continuity and measurement-window state are deliberately
        # separate. The stream markers must survive completion of MEASURING so
        # STABLE can still detect a later sample gap.
        self._last_sample_id: Hashable | None = None
        self._last_new_sample_at: float | None = None
        self._stale_stream_degraded = False
        self._nominal_overload_since: float | None = None
        self._maximum_overload_since: float | None = None
        self._last_owner: GeneratorSlot | None = None
        self._blocked_groups: set[LoadGroup] = set()
        self._last_alert_key: str | None = None

    def step(self, o: LoadManagerObservation) -> LoadManagerDecision:
        actions: list[LoadAction] = []
        events: list[SupervisorEvent] = []
        notifications: list[str] = []
        self._remember(o)

        if not self.config.enabled:
            self.pending_action = None
            self._reset_runtime()
            self._set_phase(LoadManagerPhase.DISABLED)
            return self._decision(actions, events, notifications)

        self._reconcile_manual_on(o)
        if self.pending_action:
            waiting, permit = self._reconcile_pending(o, events, notifications)
            if waiting:
                return self._decision(actions, events, notifications, permit)

        if not o.actions_enabled:
            self.pending_action = None
            self._reset_runtime()
            if o.house_on_generator is True:
                error = self._dependency_error(o)
                self._set_phase(
                    LoadManagerPhase.DEGRADED if error else LoadManagerPhase.STABLE,
                    "observe_generator",
                    error,
                )
            else:
                self._set_phase(
                    LoadManagerPhase.IDLE,
                    "waiting_for_generator" if o.desired_generator_supply else None,
                )
            return self._decision(actions, events, notifications)

        if self._pretransfer_required(o):
            return self._pretransfer(o, actions, events, notifications)

        if o.house_on_grid is True and o.house_on_generator is False:
            self._reset_runtime()
            self._last_owner = None
            return self._restore_on_grid(o, actions, events, notifications)

        if o.house_on_generator is True:
            return self._on_generator(o, actions, events, notifications)

        self._last_owner = None
        self._reset_runtime()
        if o.house_on_generator is None or o.house_on_grid is None:
            self._degrade(
                "Неизвестно, от какого источника сейчас питается дом; "
                "управление некритичными нагрузками приостановлено.",
                events,
                notifications,
            )
        else:
            self._set_phase(
                LoadManagerPhase.IDLE,
                "waiting_for_generator" if o.desired_generator_supply else None,
            )
        return self._decision(actions, events, notifications)

    def report_execution_failure(
        self,
        action: LoadAction,
        error: str,
    ) -> tuple[SupervisorEvent, str]:
        pending_reason = None
        if self.pending_action and self.pending_action.group == action.group:
            pending_reason = self.pending_action.reason
            self.pending_action = None
        self._blocked_groups.add(action.group)
        reason = (
            f"Не удалось выполнить {action.kind.value} для "
            f"{self._group_debug_name(action.group)}: {error}"
        )
        self._set_phase(LoadManagerPhase.DEGRADED, "command_failed", reason)
        self.last_reason = reason
        message = self._execution_failure_message(action, pending_reason)
        return SupervisorEvent("warning", message), message

    def status_attributes(self) -> dict[str, object]:
        state = lambda value: (
            "on" if value is True else "off" if value is False else "unknown"
        )
        return {
            "load_management_enabled": self.config.enabled,
            "load_manager_phase": self.phase.value,
            "load_manager_operation": self.operation,
            "load_manager_degraded_reason": self.degraded_reason,
            "generator_power": self.last_generator_power,
            "active_generator_nominal_power": self.active_nominal_power,
            "active_generator_maximum_power": self.active_maximum_power,
            "load_g1_state": state(self.last_group_states[LoadGroup.G1]),
            "load_g1_shed_by_energy_ats": self.shed_by_energy_ats[LoadGroup.G1],
            "load_g2_state": state(self.last_group_states[LoadGroup.G2]),
            "load_g2_shed_by_energy_ats": self.shed_by_energy_ats[LoadGroup.G2],
            "load_nominal_overload_since": self._nominal_overload_since,
            "load_maximum_overload_since": self._maximum_overload_since,
            "load_next_restore_retry": self.next_restore_retry,
            "load_last_reason": self.last_reason,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "phase": self.phase.value,
            "operation": self.operation,
            "shed_by_energy_ats": {
                group.value: owned
                for group, owned in self.shed_by_energy_ats.items()
            },
            "pending_action": (
                self.pending_action.to_dict() if self.pending_action else None
            ),
            "degraded_reason": self.degraded_reason,
            "last_reason": self.last_reason,
            "next_restore_retry": self.next_restore_retry,
        }

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
        config: LoadManagerConfig,
    ) -> "LoadManager":
        manager = cls(config)
        phase = str(data.get("phase", manager.phase.value))
        old_phases = {
            "waiting_for_generator": "idle",
            "measuring_base_load": "measuring",
            "restoring_g1": "measuring",
            "measuring_after_g1": "measuring",
            "restoring_g2": "measuring",
            "measuring_after_g2": "measuring",
            "overload_control": "stable",
            "restoring_on_grid": "idle",
        }
        manager.phase = LoadManagerPhase(old_phases.get(phase, phase))
        manager.operation = (
            str(data["operation"]) if data.get("operation") is not None else None
        )

        ownership = data.get("shed_by_energy_ats", {})
        if not isinstance(ownership, Mapping):
            raise ValueError("load_manager.shed_by_energy_ats должен быть object")
        for group in cls._RESTORE_ORDER:
            owned = ownership.get(group.value, False)
            if type(owned) is not bool:
                raise ValueError(
                    f"load_manager.shed_by_energy_ats.{group.value} должен быть boolean"
                )
            manager.shed_by_energy_ats[group] = owned

        pending = data.get("pending_action")
        if pending is not None:
            if not isinstance(pending, Mapping):
                raise ValueError("load_manager.pending_action должен быть object/null")
            if (
                "block_transfer" not in pending
                and pending.get("reason") == "pretransfer"
            ):
                pending = {**pending, "block_transfer": True}
            manager.pending_action = _PendingAction.from_dict(pending)

        manager.degraded_reason = (
            str(data["degraded_reason"])
            if data.get("degraded_reason") is not None
            else None
        )
        manager.last_reason = (
            str(data["last_reason"])
            if data.get("last_reason") is not None
            else None
        )
        retry = data.get("next_restore_retry")
        manager.next_restore_retry = float(retry) if retry is not None else None
        manager._reset_measurement()
        manager._reset_sample_stream()
        manager._last_owner = None
        manager._blocked_groups.clear()
        manager._reset_overload_timers()
        return manager

    def _pretransfer(
        self,
        o,
        actions,
        events,
        notifications,
    ) -> LoadManagerDecision:
        unavailable: list[LoadGroup] = []
        for group in self._SHED_ORDER:
            state = o.groups.get(group)
            if state is None:
                unavailable.append(group)
            elif state is True and group not in self._blocked_groups:
                self._recover_from_degraded(events)
                self._set_phase(LoadManagerPhase.LOAD_SHEDDING, "pretransfer")
                self._queue(
                    o,
                    actions,
                    group,
                    False,
                    "pretransfer",
                    f"Команда отключить {self._group_debug_name(group)} "
                    "перед переходом на генератор.",
                    block_transfer=True,
                )
                return self._decision(actions, events, notifications, False)

        failed = unavailable + [
            group for group in self._SHED_ORDER if group in self._blocked_groups
        ]
        if failed:
            names = ", ".join(
                dict.fromkeys(self._group_debug_name(group) for group in failed)
            )
            self._degrade(
                "Перед переходом на генератор не удалось подтвердить отключение: "
                f"{names}. Переход на резервное питание не блокируется.",
                events,
                notifications,
                main_message=(
                    "Не удалось отключить часть некритичных нагрузок перед "
                    "переходом на генератор. Переход на резервное питание продолжится."
                ),
            )
        else:
            self._recover_from_degraded(events)
            self._set_phase(LoadManagerPhase.LOAD_SHEDDING, "pretransfer")
        return self._decision(actions, events, notifications)

    def _restore_on_grid(
        self,
        o,
        actions,
        events,
        notifications,
    ) -> LoadManagerDecision:
        self.next_restore_retry = None
        self._blocked_groups.clear()
        for group in self._RESTORE_ORDER:
            if self.shed_by_energy_ats[group] and o.groups.get(group) is True:
                self.shed_by_energy_ats[group] = False

        for group in self._RESTORE_ORDER:
            if not self.shed_by_energy_ats[group]:
                continue
            state = o.groups.get(group)
            if state is None:
                self._degrade(
                    f"{self._group_debug_name(group)} недоступны; ранее "
                    "отключённая нагрузка будет восстановлена позже.",
                    events,
                    notifications,
                    main_message=(
                        "После возврата основной сети не удалось автоматически "
                        f"восстановить {self._group_name(group)}."
                    ),
                )
                return self._decision(actions, events, notifications)
            if state is False:
                self._set_phase(LoadManagerPhase.IDLE, "grid_restore")
                self._queue(
                    o,
                    actions,
                    group,
                    True,
                    "grid_restore",
                    f"Команда восстановить {self._group_debug_name(group)} "
                    "после возврата на основную сеть.",
                )
                return self._decision(actions, events, notifications)

        self._recover_from_degraded(events)
        self._set_phase(LoadManagerPhase.IDLE)
        self._last_alert_key = None
        return self._decision(actions, events, notifications)

    def _on_generator(
        self,
        o,
        actions,
        events,
        notifications,
    ) -> LoadManagerDecision:
        error = self._dependency_error(o)
        if error:
            self._degrade(error, events, notifications)
            return self._decision(actions, events, notifications)

        # Только stale-stream деградация требует доказать появление новой
        # revision и заново пройти stabilization. Прочие локальные dependency
        # failures тоже проходят новое measurement window, но могут начать его
        # сразу после исчезновения blocker-а.
        if self.phase == LoadManagerPhase.DEGRADED:
            if self._stale_stream_degraded and not self._sample_is_new(o):
                return self._decision(actions, events, notifications)
            self._recover_from_degraded(events)
            self._last_owner = o.bus_owner
            self._start_measurement(o.now, "recovery")
            self._reset_overload_timers()
            self._accept_sample(o)
            return self._decision(actions, events, notifications)

        if o.bus_owner != self._last_owner:
            self._last_owner = o.bus_owner
            self._start_measurement(o.now, "base")
            self._reset_overload_timers()
            # Новый owner задаёт новое измерительное окно; старый timestamp
            # предыдущего owner не должен превращать этот переход в stale fault.
            self._accept_sample(o)
            return self._decision(actions, events, notifications)

        # F5 / REQ-LOAD-14/16/18/19: сначала оцениваем gap относительно
        # предыдущего наблюдения, и только потом принимаем текущий sample. Иначе
        # первый sample после gap сам стирает доказательство stale gap.
        if self._samples_stale(o.now):
            self._reset_overload_timers()
            self._stale_stream_degraded = True
            self._degrade(
                "Данные мощности генератора перестали обновляться; "
                "автоматическое управление нагрузками приостановлено.",
                events,
                notifications,
            )
            return self._decision(actions, events, notifications)

        new_sample = self._accept_sample(o)

        if self.phase == LoadManagerPhase.MEASURING:
            if not self._measurement_ready(o.now):
                return self._decision(actions, events, notifications)
            return self._finish_measurement(o, actions, events, notifications)

        if new_sample:
            decision = self._overload(o, actions, events, notifications)
            if decision:
                return decision

        if (
            self.next_restore_retry is not None
            and o.now >= self.next_restore_retry
            and any(self.shed_by_energy_ats.values())
        ):
            self._start_measurement(o.now, "retry")
            self._accept_sample(o, force=True)
            return self._decision(actions, events, notifications)

        self._set_phase(LoadManagerPhase.STABLE, "monitoring")
        return self._decision(actions, events, notifications)

    def _finish_measurement(
        self,
        o,
        actions,
        events,
        notifications,
    ) -> LoadManagerDecision:
        power = self._measurement_max
        assert power is not None
        reason = self._measurement_reason or "base"
        self._reset_measurement()
        if reason == "recovery":
            self._recover_from_degraded(events)
        nominal = self._nominal(o)

        if reason.startswith("admission:"):
            group = LoadGroup(reason.split(":", 1)[1])
            if power > nominal and o.groups.get(group) is True:
                self.next_restore_retry = o.now + self.config.restore_retry_interval
                self._queue(
                    o,
                    actions,
                    group,
                    False,
                    "admission_revert",
                    f"После возврата {self._group_debug_name(group)} нагрузка "
                    f"генератора выросла до {power:.0f} Вт выше номинального "
                    f"предела {nominal:.0f} Вт; команда повторно отключить группу.",
                    measurement_after="after_shed",
                )
                return self._decision(actions, events, notifications)

        if power > nominal:
            self._set_phase(LoadManagerPhase.STABLE, "monitoring")
            self._nominal_overload_since = o.now
            self._maximum_overload_since = (
                o.now if power > self._maximum(o) else None
            )
            return self._decision(actions, events, notifications)

        return self._maybe_restore(o, power, actions, events, notifications)

    def _maybe_restore(
        self,
        o,
        power,
        actions,
        events,
        notifications,
    ) -> LoadManagerDecision:
        candidate = next(
            (
                group
                for group in self._RESTORE_ORDER
                if self.shed_by_energy_ats[group]
            ),
            None,
        )
        if candidate is None:
            self.next_restore_retry = None
            self._set_phase(LoadManagerPhase.STABLE, "monitoring")
            return self._decision(actions, events, notifications)

        state = o.groups.get(candidate)
        if state is True:
            self.shed_by_energy_ats[candidate] = False
            return self._maybe_restore(o, power, actions, events, notifications)
        if state is None:
            self._degrade(
                f"Состояние {self._group_debug_name(candidate)} неизвестно; "
                "автоматическое восстановление нагрузки приостановлено.",
                events,
                notifications,
            )
            return self._decision(actions, events, notifications)
        if self.next_restore_retry is not None and o.now < self.next_restore_retry:
            self._set_phase(LoadManagerPhase.STABLE, "restore_wait")
            return self._decision(actions, events, notifications)

        threshold = self._nominal(o) * (
            1 - self.config.restore_margin_percent / 100
        )
        if power > threshold:
            self.next_restore_retry = o.now + self.config.restore_retry_interval
            self.last_reason = (
                f"{self._group_debug_name(candidate)} пока не восстанавливаются: "
                f"нагрузка {power:.0f} Вт выше порога восстановления "
                f"{threshold:.0f} Вт."
            )
            self._set_phase(LoadManagerPhase.STABLE, "restore_wait")
            return self._decision(actions, events, notifications)

        self._queue(
            o,
            actions,
            candidate,
            True,
            "admission",
            f"Пробуем восстановить {self._group_debug_name(candidate)}: "
            f"нагрузка генератора {power:.0f} Вт, порог восстановления "
            f"{threshold:.0f} Вт.",
            measurement_after=f"admission:{candidate.value}",
        )
        return self._decision(actions, events, notifications)

    def _overload(
        self,
        o,
        actions,
        events,
        notifications,
    ) -> LoadManagerDecision | None:
        power = self._number(o.generator_power)
        if power is None:
            return None
        nominal, maximum = self._nominal(o), self._maximum(o)
        if power <= nominal:
            self._reset_overload_timers()
            self._last_alert_key = None
            return None

        if self._nominal_overload_since is None:
            self._nominal_overload_since = o.now
        if power > maximum:
            if self._maximum_overload_since is None:
                self._maximum_overload_since = o.now
            if (
                o.now - self._maximum_overload_since
                >= self.config.maximum_overload_confirmation_time
            ):
                return self._shed_one(
                    o,
                    power,
                    True,
                    actions,
                    events,
                    notifications,
                )
            return None

        self._maximum_overload_since = None
        if (
            o.now - self._nominal_overload_since
            >= self.config.nominal_overload_time
        ):
            return self._shed_one(
                o,
                power,
                False,
                actions,
                events,
                notifications,
            )
        return None

    def _shed_one(
        self,
        o,
        power,
        maximum,
        actions,
        events,
        notifications,
    ) -> LoadManagerDecision:
        candidate = next(
            (
                group
                for group in self._SHED_ORDER
                if o.groups.get(group) is True
                and group not in self._blocked_groups
            ),
            None,
        )
        threshold = self._maximum(o) if maximum else self._nominal(o)
        if candidate is None:
            level = "critical" if maximum else "warning"
            key = f"{level}:{o.bus_owner}"
            generator = (
                f"генератора {o.generator_name}"
                if o.generator_name
                else "генератора"
            )
            message = (
                f"{'Критическая перегрузка' if maximum else 'Перегрузка'} "
                f"{generator}: нагрузка {power:.0f} Вт. Все управляемые "
                "некритичные нагрузки уже отключены."
            )
            if self._last_alert_key != key:
                self._last_alert_key = key
                self.last_reason = message
                events.append(SupervisorEvent(level, message))
                events.append(
                    SupervisorEvent(
                        level,
                        "Диагностика Load Manager: нагрузка "
                        f"{power:.0f} Вт превышает "
                        f"{'максимальный' if maximum else 'номинальный'} предел "
                        f"{threshold:.0f} Вт; доступных групп для отключения нет.",
                        EventVisibility.DETAIL,
                    )
                )
                if not maximum:
                    notifications.append(message)
            self._set_phase(LoadManagerPhase.STABLE, "overload_no_more_groups")
            return self._decision(actions, events, notifications)

        self.next_restore_retry = o.now + self.config.restore_retry_interval
        self._queue(
            o,
            actions,
            candidate,
            False,
            "maximum_overload" if maximum else "nominal_overload",
            f"Команда отключить {self._group_debug_name(candidate)}: нагрузка "
            f"генератора {power:.0f} Вт превышает "
            f"{'максимальный' if maximum else 'номинальный'} предел "
            f"{threshold:.0f} Вт.",
            measurement_after="after_shed",
        )
        self._reset_overload_timers()
        self._last_alert_key = None
        return self._decision(actions, events, notifications)

    def _queue(
        self,
        o: LoadManagerObservation,
        actions: list[LoadAction],
        group: LoadGroup,
        target_on: bool,
        reason: str,
        message: str,
        *,
        block_transfer: bool = False,
        measurement_after: str | None = None,
    ) -> None:
        self.pending_action = _PendingAction(
            group,
            target_on,
            o.now + max(2.0, self.config.measurement_stabilization_time),
            reason,
            block_transfer,
            measurement_after,
        )
        self.operation = reason
        self.last_reason = message
        actions.append(
            LoadAction(
                group,
                LoadActionKind.TURN_ON if target_on else LoadActionKind.TURN_OFF,
                message,
            )
        )

    def _reconcile_pending(
        self,
        o,
        events,
        notifications,
    ) -> tuple[bool, bool]:
        pending = self.pending_action
        assert pending is not None
        state = o.groups.get(pending.group)
        if state is pending.target_on:
            self.shed_by_energy_ats[pending.group] = not pending.target_on
            self.pending_action = None
            self._blocked_groups.discard(pending.group)
            self.degraded_reason = None
            self.last_reason = (
                f"{self._group_debug_name(pending.group)} подтверждены "
                f"{'ON' if pending.target_on else 'OFF'} ({pending.reason})."
            )
            if (
                not pending.target_on
                and pending.reason in {"nominal_overload", "maximum_overload"}
            ):
                generator = (
                    f"генератора {o.generator_name}"
                    if o.generator_name
                    else "генератора"
                )
                events.append(
                    SupervisorEvent(
                        "warning",
                        f"Из-за перегрузки {generator} отключены "
                        f"{self._group_name(pending.group)}.",
                    )
                )
            if pending.measurement_after:
                self._start_measurement(o.now, pending.measurement_after)
            return False, True
        if o.now < pending.deadline:
            return True, not pending.block_transfer

        self.pending_action = None
        self._blocked_groups.add(pending.group)
        timeout = max(2.0, self.config.measurement_stabilization_time)
        detail = (
            f"Не подтверждено {'включение' if pending.target_on else 'отключение'} "
            f"{self._group_debug_name(pending.group)} за {timeout:.0f} с."
        )
        self._degrade(
            detail,
            events,
            notifications,
            main_message=self._pending_timeout_message(pending),
        )
        return False, True

    def _start_measurement(self, now: float, reason: str) -> None:
        self.phase = LoadManagerPhase.MEASURING
        self.operation = reason
        self._measurement_reason = reason
        self._measurement_started_at = now
        self._measurement_count = 0
        self._measurement_max = None
        # Measurement window и идентичность потока — разные вещи. Не стираем
        # _last_sample_id: иначе frozen sample после stale выглядит как новый.

    def _sample_is_new(self, o: LoadManagerObservation) -> bool:
        power = self._number(o.generator_power)
        return bool(
            power is not None
            and power >= 0
            and o.power_sample_id is not None
            and o.power_sample_id != self._last_sample_id
        )

    def _accept_sample(
        self,
        o: LoadManagerObservation,
        *,
        force: bool = False,
    ) -> bool:
        power = self._number(o.generator_power)
        if power is None or power < 0 or o.power_sample_id is None:
            return False
        if not force and o.power_sample_id == self._last_sample_id:
            return False
        self._last_sample_id = o.power_sample_id
        self._last_new_sample_at = o.now
        self._measurement_count += 1
        self._measurement_max = (
            power
            if self._measurement_max is None
            else max(self._measurement_max, power)
        )
        return True

    def _measurement_ready(self, now: float) -> bool:
        return (
            self._measurement_started_at is not None
            and now - self._measurement_started_at
            >= self.config.measurement_stabilization_time
            and self._measurement_count >= 2
        )

    def _samples_stale(self, now: float) -> bool:
        reference = (
            self._last_new_sample_at
            if self._last_new_sample_at is not None
            else self._measurement_started_at
        )
        if reference is None:
            return False
        return now - reference >= max(
            5.0,
            self.config.measurement_stabilization_time * 2 + 1,
        )

    def _reset_measurement(self) -> None:
        self._measurement_reason = None
        self._measurement_started_at = None
        self._measurement_count = 0
        self._measurement_max = None

    def _reset_sample_stream(self) -> None:
        self._last_sample_id = None
        self._last_new_sample_at = None
        self._stale_stream_degraded = False

    def _dependency_error(self, o: LoadManagerObservation) -> str | None:
        if o.bus_owner is None:
            return (
                "Не удалось определить, какой генератор фактически питает дом; "
                "его допустимые пределы мощности неизвестны."
            )
        nominal = self._positive(o.nominal_power)
        maximum = self._positive(o.maximum_power)
        if nominal is None or maximum is None or nominal > maximum:
            return (
                "Для работающего генератора не заданы корректные номинальный "
                "и максимальный пределы мощности."
            )
        if o.meter_ready is not True:
            return "Счётчик мощности генератора недоступен."
        power = self._number(o.generator_power)
        if power is None or power < 0 or o.power_sample_id is None:
            return "Нет корректного свежего измерения мощности генератора."
        if any(o.groups.get(group) is None for group in self._RESTORE_ORDER):
            return "Неизвестно состояние одной из управляемых групп нагрузки."
        return None

    def _reconcile_manual_on(self, o: LoadManagerObservation) -> None:
        pending_group = self.pending_action.group if self.pending_action else None
        for group in self._RESTORE_ORDER:
            if (
                self.shed_by_energy_ats[group]
                and o.groups.get(group) is True
                and group != pending_group
            ):
                self.shed_by_energy_ats[group] = False

    def _remember(self, o: LoadManagerObservation) -> None:
        self.last_group_states = {
            group: o.groups.get(group) for group in self._RESTORE_ORDER
        }
        self.last_generator_power = self._number(o.generator_power)
        self.active_nominal_power = self._positive(o.nominal_power)
        self.active_maximum_power = self._positive(o.maximum_power)

    def _reset_runtime(self) -> None:
        self._reset_measurement()
        self._reset_sample_stream()
        self._reset_overload_timers()
        self._blocked_groups.clear()

    def _set_phase(
        self,
        phase: LoadManagerPhase,
        operation: str | None = None,
        reason: str | None = None,
    ) -> None:
        self.phase, self.operation, self.degraded_reason = phase, operation, reason
        if reason:
            self.last_reason = reason

    def _degrade(
        self,
        reason: str,
        events,
        notifications,
        *,
        main_message: str | None = None,
    ) -> None:
        was_degraded = (
            self.phase == LoadManagerPhase.DEGRADED
            or self.degraded_reason is not None
        )
        changed = self.degraded_reason != reason
        self._set_phase(LoadManagerPhase.DEGRADED, "degraded", reason)

        if not was_degraded:
            message = main_message or (
                "Автоматическое управление некритичными нагрузками временно "
                "недоступно. Основное управление резервным питанием продолжает работу."
            )
            events.append(SupervisorEvent("warning", message))
            notifications.append(message)

        if changed:
            events.append(
                SupervisorEvent(
                    "warning",
                    f"Диагностика Load Manager: {reason}",
                    EventVisibility.DETAIL,
                )
            )

    def _recover_from_degraded(self, events) -> None:
        if self.degraded_reason is None or self._blocked_groups:
            return
        events.append(
            SupervisorEvent(
                "info",
                "Автоматическое управление некритичными нагрузками восстановлено.",
            )
        )
        self.degraded_reason = None
        self._stale_stream_degraded = False

    def _execution_failure_message(
        self,
        action: LoadAction,
        pending_reason: str | None,
    ) -> str:
        name = self._group_name(action.group)
        if action.kind == LoadActionKind.TURN_OFF:
            if pending_reason == "pretransfer":
                return (
                    f"Не удалось отключить {name} перед переходом на генератор. "
                    "Переход на резервное питание продолжится."
                )
            if pending_reason in {"nominal_overload", "maximum_overload"}:
                return f"Не удалось отключить {name} для снижения нагрузки генератора."
        if action.kind == LoadActionKind.TURN_ON and pending_reason == "grid_restore":
            return (
                "После возврата основной сети не удалось автоматически "
                f"восстановить {name}."
            )
        return f"Не удалось автоматически изменить состояние: {name}."

    def _pending_timeout_message(self, pending: _PendingAction) -> str:
        name = self._group_name(pending.group)
        if not pending.target_on and pending.reason == "pretransfer":
            return (
                f"Не удалось подтвердить отключение {name} перед переходом "
                "на генератор. Переход на резервное питание продолжится."
            )
        if not pending.target_on and pending.reason in {
            "nominal_overload",
            "maximum_overload",
        }:
            return f"Не удалось отключить {name} для снижения нагрузки генератора."
        if pending.target_on and pending.reason == "grid_restore":
            return (
                "После возврата основной сети не удалось автоматически "
                f"восстановить {name}."
            )
        return (
            "Автоматическое управление некритичными нагрузками временно "
            "недоступно. Основное управление резервным питанием продолжает работу."
        )

    @staticmethod
    def _group_name(group: LoadGroup) -> str:
        return _GROUP_NAMES[group]

    @classmethod
    def _group_debug_name(cls, group: LoadGroup) -> str:
        return f"{group.value.upper()} ({cls._group_name(group)})"

    def _reset_overload_timers(self) -> None:
        self._nominal_overload_since = self._maximum_overload_since = None

    @staticmethod
    def _pretransfer_required(o: LoadManagerObservation) -> bool:
        return (
            o.desired_generator_supply
            and o.managed_generator_ready
            and o.house_on_generator is False
            and not o.power_transition_in_progress
        )

    @staticmethod
    def _number(value: float | None) -> float | None:
        try:
            number = float(value) if value is not None else None
        except (TypeError, ValueError):
            return None
        return number if number is not None and math.isfinite(number) else None

    @classmethod
    def _positive(cls, value: float | None) -> float | None:
        number = cls._number(value)
        return number if number is not None and number > 0 else None

    def _nominal(self, o: LoadManagerObservation) -> float:
        value = self._positive(o.nominal_power)
        assert value is not None
        return value

    def _maximum(self, o: LoadManagerObservation) -> float:
        value = self._positive(o.maximum_power)
        assert value is not None
        return value

    @staticmethod
    def _decision(
        actions,
        events,
        notifications,
        transfer_permitted: bool = True,
    ) -> LoadManagerDecision:
        return LoadManagerDecision(
            tuple(actions),
            tuple(events),
            tuple(notifications),
            transfer_permitted,
        )

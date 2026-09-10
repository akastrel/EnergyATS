"""Управление некритичными нагрузками на генераторной шине.

Load Manager — отдельная policy-машина. Она не запускает генераторы и не
управляет основными контакторами; её аппаратные действия ограничены G1/G2.
Отказ входных данных или consumer switch локален для Load Manager и не должен
переводить основную ATS-логику в recovery.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Hashable, Mapping

from domain import GeneratorSlot, SupervisorEvent


class LoadGroup(str, Enum):
    G1 = "g1"
    G2 = "g2"


class LoadActionKind(str, Enum):
    TURN_ON = "turn_on"
    TURN_OFF = "turn_off"


@dataclass(frozen=True)
class LoadAction:
    group: LoadGroup
    kind: LoadActionKind
    message: str


class LoadManagerPhase(str, Enum):
    DISABLED = "disabled"
    IDLE = "idle"
    WAITING_FOR_GENERATOR = "waiting_for_generator"
    LOAD_SHEDDING = "load_shedding"
    MEASURING_BASE_LOAD = "measuring_base_load"
    RESTORING_G1 = "restoring_g1"
    MEASURING_AFTER_G1 = "measuring_after_g1"
    RESTORING_G2 = "restoring_g2"
    MEASURING_AFTER_G2 = "measuring_after_g2"
    STABLE = "stable"
    OVERLOAD_CONTROL = "overload_control"
    RESTORING_ON_GRID = "restoring_on_grid"
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
        if self.measurement_stabilization_time < 0:
            raise ValueError("load_measurement_stabilization_time не может быть < 0")
        if not 0 <= self.restore_margin_percent < 100:
            raise ValueError("load_restore_margin_percent должен быть в диапазоне [0, 100)")
        if self.nominal_overload_time < 0:
            raise ValueError("nominal_overload_time не может быть < 0")
        if self.maximum_overload_confirmation_time < 0:
            raise ValueError("maximum_overload_confirmation_time не может быть < 0")
        if self.restore_retry_interval < 0:
            raise ValueError("load_restore_retry_interval не может быть < 0")


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
    after_phase: LoadManagerPhase

    def to_dict(self) -> dict[str, object]:
        return {
            "group": self.group.value,
            "target_on": self.target_on,
            "deadline": self.deadline,
            "reason": self.reason,
            "after_phase": self.after_phase.value,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "_PendingAction":
        target = data.get("target_on")
        if type(target) is not bool:
            raise ValueError("load_manager.pending.target_on должен быть boolean")
        return cls(
            group=LoadGroup(str(data["group"])),
            target_on=target,
            deadline=float(data["deadline"]),
            reason=str(data.get("reason", "")),
            after_phase=LoadManagerPhase(str(data["after_phase"])),
        )


class LoadManager:
    """Policy G1/G2 с локальным degraded-state и собственным OFF ownership."""

    _RESTORE_ORDER = (LoadGroup.G1, LoadGroup.G2)
    _SHED_ORDER = (LoadGroup.G2, LoadGroup.G1)

    def __init__(self, config: LoadManagerConfig | None = None) -> None:
        self.config = config or LoadManagerConfig()
        self.phase = (
            LoadManagerPhase.IDLE
            if self.config.enabled
            else LoadManagerPhase.DISABLED
        )
        self.shed_by_energy_ats: dict[LoadGroup, bool] = {
            LoadGroup.G1: False,
            LoadGroup.G2: False,
        }
        self.pending_action: _PendingAction | None = None
        self.degraded_reason: str | None = None
        self.last_reason: str | None = None
        self.next_restore_retry: float | None = None

        self.last_generator_power: float | None = None
        self.active_nominal_power: float | None = None
        self.active_maximum_power: float | None = None
        self.last_group_states: dict[LoadGroup, bool | None] = {
            LoadGroup.G1: None,
            LoadGroup.G2: None,
        }

        self._measurement_started_at: float | None = None
        self._samples: list[tuple[float, float]] = []
        self._last_sample_id: Hashable | None = None
        self._last_new_sample_at: float | None = None
        self._nominal_overload_since: float | None = None
        self._maximum_overload_since: float | None = None
        self._last_owner: GeneratorSlot | None = None
        self._pretransfer_active = False
        self._blocked_groups: set[LoadGroup] = set()
        self._last_alert_key: str | None = None

    # Public API -----------------------------------------------------

    def step(self, o: LoadManagerObservation) -> LoadManagerDecision:
        actions: list[LoadAction] = []
        events: list[SupervisorEvent] = []
        notifications: list[str] = []
        self.last_group_states = {
            group: o.groups.get(group) for group in self._RESTORE_ORDER
        }
        self.last_generator_power = self._finite_number(o.generator_power)
        self.active_nominal_power = self._finite_positive(o.nominal_power)
        self.active_maximum_power = self._finite_positive(o.maximum_power)

        # A disabled Load Manager is fully out of the control path. Persisted
        # confirmed OFF ownership is deliberately retained, but an unfinished
        # soft consumer command does not survive as an active transaction.
        if not self.config.enabled:
            self.pending_action = None
            self.phase = LoadManagerPhase.DISABLED
            self.degraded_reason = None
            self._clear_runtime_measurement()
            self._pretransfer_active = False
            self._blocked_groups.clear()
            self._reset_overload_timers()
            return self._decision(actions, events, notifications, True)

        self._reconcile_user_override(o)
        if self.pending_action is not None:
            if self._reconcile_pending(o, events, notifications):
                return self._decision(actions, events, notifications, False)

        # DISARMED mode observes but never creates new G1/G2 ownership or a
        # pending command. Core ATS is also non-actuating, so Load Manager must
        # not become an additional gate.
        if not o.actions_enabled:
            self.pending_action = None
            self._pretransfer_active = False
            self._blocked_groups.clear()
            self._reset_overload_timers()
            self._clear_runtime_measurement()
            if o.house_on_generator is True:
                reason = self._generator_dependency_error(o)
                if reason is not None:
                    self.phase = LoadManagerPhase.DEGRADED
                    self.degraded_reason = reason
                    self.last_reason = reason
                else:
                    self.phase = LoadManagerPhase.STABLE
                    self.degraded_reason = None
            elif o.desired_generator_supply:
                self.phase = LoadManagerPhase.WAITING_FOR_GENERATOR
                self.degraded_reason = None
            else:
                self.phase = LoadManagerPhase.IDLE
                self.degraded_reason = None
            return self._decision(actions, events, notifications, True)

        # Pre-transfer has precedence over the otherwise normal Grid path:
        # for a manual managed start we intentionally shed G1/G2 while Grid is
        # still powering the house, immediately before TPC starts break-before-make.
        if self._pretransfer_required(o):
            return self._step_pretransfer(o, actions, events, notifications)

        if self._grid_path_confirmed(o):
            self._pretransfer_active = False
            self._blocked_groups.clear()
            self._reset_overload_timers()
            self._clear_runtime_measurement()
            return self._step_on_grid(o, actions, events, notifications)

        self._pretransfer_active = False
        if o.house_on_generator is True:
            return self._step_on_generator(o, actions, events, notifications)

        self._last_owner = None
        self._reset_overload_timers()
        self._clear_runtime_measurement()
        if o.house_on_generator is None or o.house_on_grid is None:
            self._enter_degraded(
                "Неизвестно состояние основного источника дома; Load Manager не переключает G1/G2.",
                events,
                notifications,
            )
        elif o.desired_generator_supply:
            self.phase = LoadManagerPhase.WAITING_FOR_GENERATOR
            self.degraded_reason = None
        else:
            self.phase = LoadManagerPhase.IDLE
            self.degraded_reason = None
        return self._decision(actions, events, notifications, True)

    def report_execution_failure(
        self,
        action: LoadAction,
        error: str,
    ) -> tuple[SupervisorEvent, str]:
        """Локализовать failed HA service call, не затрагивая core ATS."""

        if self.pending_action is not None and self.pending_action.group == action.group:
            self.pending_action = None
        self._blocked_groups.add(action.group)
        reason = (
            f"Не удалось выполнить команду {action.kind.value} для {action.group.value.upper()}: "
            f"{error}"
        )
        self.phase = LoadManagerPhase.DEGRADED
        self.degraded_reason = reason
        self.last_reason = reason
        return SupervisorEvent("warning", reason), reason

    def status_attributes(self) -> dict[str, object]:
        return {
            "load_management_enabled": self.config.enabled,
            "load_manager_phase": self.phase.value,
            "load_manager_degraded_reason": self.degraded_reason,
            "generator_power": self.last_generator_power,
            "active_generator_nominal_power": self.active_nominal_power,
            "active_generator_maximum_power": self.active_maximum_power,
            "load_g1_state": self._state_text(self.last_group_states[LoadGroup.G1]),
            "load_g1_shed_by_energy_ats": self.shed_by_energy_ats[LoadGroup.G1],
            "load_g2_state": self._state_text(self.last_group_states[LoadGroup.G2]),
            "load_g2_shed_by_energy_ats": self.shed_by_energy_ats[LoadGroup.G2],
            "load_nominal_overload_since": self._nominal_overload_since,
            "load_maximum_overload_since": self._maximum_overload_since,
            "load_next_restore_retry": self.next_restore_retry,
            "load_last_reason": self.last_reason,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "phase": self.phase.value,
            "shed_by_energy_ats": {
                group.value: value for group, value in self.shed_by_energy_ats.items()
            },
            "pending_action": (
                self.pending_action.to_dict() if self.pending_action is not None else None
            ),
            "degraded_reason": self.degraded_reason,
            "last_reason": self.last_reason,
            "next_restore_retry": self.next_restore_retry,
            # Измерительные samples намеренно не сохраняются. После restart
            # power-based решение должно быть доказано новым stabilization window.
        }

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
        config: LoadManagerConfig,
    ) -> "LoadManager":
        manager = cls(config)
        manager.phase = LoadManagerPhase(str(data.get("phase", manager.phase.value)))
        ownership = data.get("shed_by_energy_ats", {})
        if not isinstance(ownership, Mapping):
            raise ValueError("load_manager.shed_by_energy_ats должен быть object")
        for group in cls._RESTORE_ORDER:
            value = ownership.get(group.value, False)
            if type(value) is not bool:
                raise ValueError(
                    f"load_manager.shed_by_energy_ats.{group.value} должен быть boolean"
                )
            manager.shed_by_energy_ats[group] = value

        pending = data.get("pending_action")
        if pending is not None:
            if not isinstance(pending, Mapping):
                raise ValueError("load_manager.pending_action должен быть object/null")
            manager.pending_action = _PendingAction.from_dict(pending)

        degraded = data.get("degraded_reason")
        manager.degraded_reason = str(degraded) if degraded is not None else None
        reason = data.get("last_reason")
        manager.last_reason = str(reason) if reason is not None else None
        retry = data.get("next_restore_retry")
        manager.next_restore_retry = float(retry) if retry is not None else None

        # Никогда не продолжаем measurement по сохранённому старому sample.
        manager._clear_runtime_measurement()
        manager._last_owner = None
        manager._pretransfer_active = False
        manager._blocked_groups.clear()
        manager._reset_overload_timers()
        return manager

    # Grid / pre-transfer --------------------------------------------

    def _step_on_grid(
        self,
        o: LoadManagerObservation,
        actions: list[LoadAction],
        events: list[SupervisorEvent],
        notifications: list[str],
    ) -> LoadManagerDecision:
        self.next_restore_retry = None
        for group in self._RESTORE_ORDER:
            state = o.groups.get(group)
            if self.shed_by_energy_ats[group] and state is True:
                self.shed_by_energy_ats[group] = False

        for group in self._RESTORE_ORDER:
            if not self.shed_by_energy_ats[group]:
                continue
            state = o.groups.get(group)
            if state is None:
                self._enter_degraded(
                    f"{group.value.upper()} недоступна; собственное отключение будет восстановлено после появления switch.",
                    events,
                    notifications,
                )
                return self._decision(actions, events, notifications, True)
            if state is False:
                self.phase = LoadManagerPhase.RESTORING_ON_GRID
                self.degraded_reason = None
                self._queue_action(
                    o.now,
                    group,
                    target_on=True,
                    reason="grid_restore",
                    after_phase=LoadManagerPhase.RESTORING_ON_GRID,
                    actions=actions,
                    message=(
                        f"Grid подтверждена: восстанавливаем {group.value.upper()}, "
                        "ранее отключённую Load Manager."
                    ),
                )
                return self._decision(actions, events, notifications, True)

        self.phase = LoadManagerPhase.IDLE
        self.degraded_reason = None
        self._last_alert_key = None
        return self._decision(actions, events, notifications, True)

    def _step_pretransfer(
        self,
        o: LoadManagerObservation,
        actions: list[LoadAction],
        events: list[SupervisorEvent],
        notifications: list[str],
    ) -> LoadManagerDecision:
        if not self._pretransfer_active:
            self._pretransfer_active = True
            self._blocked_groups.clear()
            self.degraded_reason = None
        self.phase = LoadManagerPhase.LOAD_SHEDDING

        unavailable: list[LoadGroup] = []
        for group in self._SHED_ORDER:
            state = o.groups.get(group)
            if state is None:
                unavailable.append(group)
                continue
            if state is True and group not in self._blocked_groups:
                self._queue_action(
                    o.now,
                    group,
                    target_on=False,
                    reason="pretransfer",
                    after_phase=LoadManagerPhase.LOAD_SHEDDING,
                    actions=actions,
                    message=(
                        f"Перед подключением дома к генератору отключаем {group.value.upper()} "
                        "для минимальной исходной нагрузки."
                    ),
                )
                return self._decision(actions, events, notifications, False)

        if unavailable or self._blocked_groups:
            parts: list[str] = []
            if unavailable:
                parts.append(
                    "недоступны " + ", ".join(g.value.upper() for g in unavailable)
                )
            if self._blocked_groups:
                parts.append(
                    "не подтверждено отключение "
                    + ", ".join(
                        g.value.upper()
                        for g in sorted(self._blocked_groups, key=lambda x: x.value)
                    )
                )
            self._enter_degraded(
                "Pre-transfer LOAD_SHEDDING завершён не полностью: "
                + "; ".join(parts)
                + ". Core ATS не блокируется.",
                events,
                notifications,
            )
        else:
            self.degraded_reason = None
        return self._decision(actions, events, notifications, True)

    # Generator-bus operation ---------------------------------------

    def _step_on_generator(
        self,
        o: LoadManagerObservation,
        actions: list[LoadAction],
        events: list[SupervisorEvent],
        notifications: list[str],
    ) -> LoadManagerDecision:
        owner_changed = o.bus_owner != self._last_owner
        if owner_changed:
            self._last_owner = o.bus_owner
            self._reset_measurement(o.now)
            self._reset_overload_timers()
            self.phase = LoadManagerPhase.MEASURING_BASE_LOAD

        dependency_error = self._generator_dependency_error(o)
        if dependency_error is not None:
            self._enter_degraded(dependency_error, events, notifications)
            return self._decision(actions, events, notifications, True)

        was_degraded = self.phase == LoadManagerPhase.DEGRADED
        self.degraded_reason = None
        sample_is_new = self._accept_sample(o)

        if was_degraded:
            self._reset_measurement(o.now)
            self._accept_sample(o, force=True)
            self.phase = LoadManagerPhase.MEASURING_BASE_LOAD
            sample_is_new = True

        if self._measurement_started_at is None:
            self._reset_measurement(o.now)
            self._accept_sample(o, force=True)
            self.phase = LoadManagerPhase.MEASURING_BASE_LOAD
            sample_is_new = True

        if self._samples_stale(o.now):
            self._enter_degraded(
                "Нет нескольких свежих generator-power samples; power-based действия приостановлены.",
                events,
                notifications,
            )
            return self._decision(actions, events, notifications, True)

        if self.phase in {
            LoadManagerPhase.MEASURING_AFTER_G1,
            LoadManagerPhase.MEASURING_AFTER_G2,
        }:
            return self._finish_admission_measurement(
                o, actions, events, notifications
            )

        if self.phase == LoadManagerPhase.MEASURING_BASE_LOAD:
            if not self._measurement_ready(o.now):
                return self._decision(actions, events, notifications, True)
            power = self._measurement_power()
            assert power is not None
            if power > self._required_nominal(o):
                self.phase = LoadManagerPhase.STABLE
                self._start_overload_from_measurement(o.now, power, o)
                return self._decision(actions, events, notifications, True)
            return self._maybe_begin_restore(
                o, power, actions, events, notifications
            )

        if self.phase in {
            LoadManagerPhase.RESTORING_G1,
            LoadManagerPhase.RESTORING_G2,
            LoadManagerPhase.OVERLOAD_CONTROL,
        }:
            self._reset_measurement(o.now)
            self._accept_sample(o, force=True)
            self.phase = LoadManagerPhase.MEASURING_BASE_LOAD
            return self._decision(actions, events, notifications, True)

        if self.phase not in {LoadManagerPhase.STABLE, LoadManagerPhase.IDLE}:
            self.phase = LoadManagerPhase.STABLE

        if sample_is_new:
            overload = self._evaluate_overload(o, actions, events, notifications)
            if overload is not None:
                return overload

        if (
            self.next_restore_retry is not None
            and o.now >= self.next_restore_retry
            and any(self.shed_by_energy_ats.values())
        ):
            self._reset_measurement(o.now)
            self._accept_sample(o, force=True)
            self.phase = LoadManagerPhase.MEASURING_BASE_LOAD
            return self._decision(actions, events, notifications, True)

        self.phase = LoadManagerPhase.STABLE
        return self._decision(actions, events, notifications, True)

    def _finish_admission_measurement(
        self,
        o: LoadManagerObservation,
        actions: list[LoadAction],
        events: list[SupervisorEvent],
        notifications: list[str],
    ) -> LoadManagerDecision:
        if not self._measurement_ready(o.now):
            return self._decision(actions, events, notifications, True)
        power = self._measurement_power()
        assert power is not None
        group = (
            LoadGroup.G1
            if self.phase == LoadManagerPhase.MEASURING_AFTER_G1
            else LoadGroup.G2
        )
        nominal = self._required_nominal(o)
        if power > nominal:
            state = o.groups.get(group)
            if state is True:
                self._queue_action(
                    o.now,
                    group,
                    target_on=False,
                    reason="admission_revert",
                    after_phase=LoadManagerPhase.STABLE,
                    actions=actions,
                    message=(
                        f"{group.value.upper()} не прошла admission: {power:.0f} W > "
                        f"nominal {nominal:.0f} W; отключаем группу обратно."
                    ),
                )
                self.next_restore_retry = (
                    o.now + self.config.restore_retry_interval
                )
                return self._decision(actions, events, notifications, True)

        return self._maybe_begin_restore(o, power, actions, events, notifications)

    def _maybe_begin_restore(
        self,
        o: LoadManagerObservation,
        power: float,
        actions: list[LoadAction],
        events: list[SupervisorEvent],
        notifications: list[str],
    ) -> LoadManagerDecision:
        candidate = next(
            (g for g in self._RESTORE_ORDER if self.shed_by_energy_ats[g]),
            None,
        )
        if candidate is None:
            self.phase = LoadManagerPhase.STABLE
            self.next_restore_retry = None
            self._reset_measurement(o.now)
            self._accept_sample(o, force=True)
            return self._decision(actions, events, notifications, True)

        state = o.groups.get(candidate)
        if state is True:
            self.shed_by_energy_ats[candidate] = False
            return self._maybe_begin_restore(
                o, power, actions, events, notifications
            )
        if state is None:
            self._enter_degraded(
                f"{candidate.value.upper()} недоступна; автоматическое восстановление приостановлено.",
                events,
                notifications,
            )
            return self._decision(actions, events, notifications, True)

        if self.next_restore_retry is not None and o.now < self.next_restore_retry:
            self.phase = LoadManagerPhase.STABLE
            return self._decision(actions, events, notifications, True)

        restore_limit = self._required_nominal(o) * (
            1.0 - self.config.restore_margin_percent / 100.0
        )
        if power > restore_limit:
            self.phase = LoadManagerPhase.STABLE
            self.next_restore_retry = o.now + self.config.restore_retry_interval
            self.last_reason = (
                f"{candidate.value.upper()} не добавлена: {power:.0f} W выше restore threshold "
                f"{restore_limit:.0f} W."
            )
            return self._decision(actions, events, notifications, True)

        phase = (
            LoadManagerPhase.RESTORING_G1
            if candidate == LoadGroup.G1
            else LoadManagerPhase.RESTORING_G2
        )
        after = (
            LoadManagerPhase.MEASURING_AFTER_G1
            if candidate == LoadGroup.G1
            else LoadManagerPhase.MEASURING_AFTER_G2
        )
        self.phase = phase
        self._queue_action(
            o.now,
            candidate,
            target_on=True,
            reason="admission",
            after_phase=after,
            actions=actions,
            message=(
                f"Добавляем {candidate.value.upper()} на generator bus: "
                f"{power:.0f} W <= restore threshold {restore_limit:.0f} W."
            ),
        )
        return self._decision(actions, events, notifications, True)

    def _evaluate_overload(
        self,
        o: LoadManagerObservation,
        actions: list[LoadAction],
        events: list[SupervisorEvent],
        notifications: list[str],
    ) -> LoadManagerDecision | None:
        power = self._finite_number(o.generator_power)
        if power is None:
            return None
        nominal = self._required_nominal(o)
        maximum = self._required_maximum(o)

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
                return self._shed_one_group(
                    o,
                    power,
                    maximum=True,
                    actions=actions,
                    events=events,
                    notifications=notifications,
                )
            return None

        self._maximum_overload_since = None
        if (
            o.now - self._nominal_overload_since
            >= self.config.nominal_overload_time
        ):
            return self._shed_one_group(
                o,
                power,
                maximum=False,
                actions=actions,
                events=events,
                notifications=notifications,
            )
        return None

    def _shed_one_group(
        self,
        o: LoadManagerObservation,
        power: float,
        *,
        maximum: bool,
        actions: list[LoadAction],
        events: list[SupervisorEvent],
        notifications: list[str],
    ) -> LoadManagerDecision:
        candidate = next(
            (
                group
                for group in self._SHED_ORDER
                if o.groups.get(group) is True and group not in self._blocked_groups
            ),
            None,
        )
        nominal = self._required_nominal(o)
        max_power = self._required_maximum(o)
        if candidate is None:
            name = o.generator_name or "генератор"
            if maximum:
                key = f"critical:{o.bus_owner}"
                message = (
                    f"Критическая перегрузка {name}: {power:.0f} W > maximum "
                    f"{max_power:.0f} W; все управляемые некритичные группы уже отключены. "
                    "EnergyATS не останавливает генератор только по этому основанию."
                )
                self._emit_once(
                    key, "critical", message, events, notifications, notify=False
                )
            else:
                key = f"warning:{o.bus_owner}"
                message = (
                    f"Перегрузка {name}: {power:.0f} W > nominal {nominal:.0f} W; "
                    "все управляемые некритичные группы уже отключены."
                )
                self._emit_once(
                    key, "warning", message, events, notifications, notify=True
                )
            self.phase = LoadManagerPhase.OVERLOAD_CONTROL
            return self._decision(actions, events, notifications, True)

        threshold = max_power if maximum else nominal
        reason = "maximum_overload" if maximum else "nominal_overload"
        self.phase = LoadManagerPhase.OVERLOAD_CONTROL
        self._queue_action(
            o.now,
            candidate,
            target_on=False,
            reason=reason,
            after_phase=LoadManagerPhase.MEASURING_BASE_LOAD,
            actions=actions,
            message=(
                f"LOAD_SHEDDING {candidate.value.upper()}: {power:.0f} W > "
                f"{'maximum' if maximum else 'nominal'} {threshold:.0f} W."
            ),
        )
        self.next_restore_retry = o.now + self.config.restore_retry_interval
        self._reset_overload_timers()
        self._last_alert_key = None
        return self._decision(actions, events, notifications, True)

    # Pending action / measurement ----------------------------------

    def _queue_action(
        self,
        now: float,
        group: LoadGroup,
        *,
        target_on: bool,
        reason: str,
        after_phase: LoadManagerPhase,
        actions: list[LoadAction],
        message: str,
    ) -> None:
        timeout = self._action_confirmation_timeout()
        self.pending_action = _PendingAction(
            group=group,
            target_on=target_on,
            deadline=now + timeout,
            reason=reason,
            after_phase=after_phase,
        )
        self.last_reason = message
        actions.append(
            LoadAction(
                group=group,
                kind=LoadActionKind.TURN_ON if target_on else LoadActionKind.TURN_OFF,
                message=message,
            )
        )

    def _reconcile_pending(
        self,
        o: LoadManagerObservation,
        events: list[SupervisorEvent],
        notifications: list[str],
    ) -> bool:
        pending = self.pending_action
        assert pending is not None
        state = o.groups.get(pending.group)
        if state is pending.target_on:
            self.shed_by_energy_ats[pending.group] = not pending.target_on
            self.pending_action = None
            self.phase = pending.after_phase
            self.degraded_reason = None
            self._blocked_groups.discard(pending.group)
            self._reset_measurement(o.now)
            self.last_reason = (
                f"{pending.group.value.upper()} подтверждена "
                f"{'ON' if pending.target_on else 'OFF'} ({pending.reason})."
            )
            events.append(SupervisorEvent("info", self.last_reason))
            return False

        if o.now < pending.deadline:
            return True

        self.pending_action = None
        self._blocked_groups.add(pending.group)
        reason = (
            f"Не подтверждено {'включение' if pending.target_on else 'отключение'} "
            f"{pending.group.value.upper()} за {self._action_confirmation_timeout():.0f} с; "
            "core ATS продолжает работу."
        )
        self._enter_degraded(reason, events, notifications)
        return False

    def _reconcile_user_override(self, o: LoadManagerObservation) -> None:
        pending_group = self.pending_action.group if self.pending_action else None
        for group in self._RESTORE_ORDER:
            if (
                self.shed_by_energy_ats[group]
                and o.groups.get(group) is True
                and group != pending_group
            ):
                self.shed_by_energy_ats[group] = False

    def _accept_sample(self, o: LoadManagerObservation, *, force: bool = False) -> bool:
        power = self._finite_number(o.generator_power)
        if power is None or power < 0:
            return False
        sample_id = o.power_sample_id
        if sample_id is None:
            return False
        if not force and sample_id == self._last_sample_id:
            return False
        self._last_sample_id = sample_id
        self._last_new_sample_at = o.now
        self._samples.append((o.now, power))
        if len(self._samples) > 128:
            self._samples = self._samples[-128:]
        return True

    def _reset_measurement(self, now: float) -> None:
        self._measurement_started_at = now
        self._samples = []
        self._last_sample_id = None
        self._last_new_sample_at = None

    def _clear_runtime_measurement(self) -> None:
        self._measurement_started_at = None
        self._samples = []
        self._last_sample_id = None
        self._last_new_sample_at = None

    def _measurement_ready(self, now: float) -> bool:
        if self._measurement_started_at is None:
            return False
        return (
            now - self._measurement_started_at
            >= self.config.measurement_stabilization_time
            and len(self._samples) >= 2
        )

    def _measurement_power(self) -> float | None:
        if not self._samples:
            return None
        return max(power for _timestamp, power in self._samples)

    def _samples_stale(self, now: float) -> bool:
        if self._measurement_started_at is None:
            return False
        timeout = max(5.0, self.config.measurement_stabilization_time * 2.0 + 1.0)
        if self._last_new_sample_at is None:
            return now - self._measurement_started_at >= timeout
        return now - self._last_new_sample_at >= timeout

    def _start_overload_from_measurement(
        self,
        now: float,
        power: float,
        o: LoadManagerObservation,
    ) -> None:
        self._nominal_overload_since = now
        self._maximum_overload_since = (
            now if power > self._required_maximum(o) else None
        )
        self._reset_measurement(now)

    # Conditions / validation ---------------------------------------

    @staticmethod
    def _grid_path_confirmed(o: LoadManagerObservation) -> bool:
        return o.house_on_grid is True and o.house_on_generator is False

    @staticmethod
    def _pretransfer_required(o: LoadManagerObservation) -> bool:
        return (
            o.desired_generator_supply
            and o.managed_generator_ready
            and o.house_on_generator is False
            and not o.power_transition_in_progress
        )

    def _generator_dependency_error(self, o: LoadManagerObservation) -> str | None:
        if o.bus_owner is None:
            return (
                "Generator bus owner неизвестен; Load Manager не выбирает "
                "паспортные пределы вслепую."
            )
        nominal = self._finite_positive(o.nominal_power)
        maximum = self._finite_positive(o.maximum_power)
        if nominal is None or maximum is None or nominal > maximum:
            return (
                "Nominal/Maximum Power текущего generator bus owner отсутствуют "
                "или некорректны; power-based Load Management приостановлен."
            )
        if o.meter_ready is not True:
            return (
                "Generator meter недоступен; состояние G1/G2 не меняется "
                "по показаниям мощности."
            )
        power = self._finite_number(o.generator_power)
        if power is None or power < 0:
            return (
                "Generator Power отсутствует или некорректен; "
                "power-based Load Management приостановлен."
            )
        if o.power_sample_id is None:
            return (
                "Generator Power не имеет признака свежего sample; "
                "power-based Load Management приостановлен."
            )
        if any(o.groups.get(group) is None for group in self._RESTORE_ORDER):
            return (
                "Состояние одной из управляемых групп G1/G2 неизвестно; "
                "автоматическое переключение нагрузок приостановлено."
            )
        return None

    def _required_nominal(self, o: LoadManagerObservation) -> float:
        value = self._finite_positive(o.nominal_power)
        assert value is not None
        return value

    def _required_maximum(self, o: LoadManagerObservation) -> float:
        value = self._finite_positive(o.maximum_power)
        assert value is not None
        return value

    def _action_confirmation_timeout(self) -> float:
        return max(2.0, self.config.measurement_stabilization_time)

    @staticmethod
    def _finite_number(value: float | None) -> float | None:
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    @classmethod
    def _finite_positive(cls, value: float | None) -> float | None:
        number = cls._finite_number(value)
        return number if number is not None and number > 0 else None

    # Diagnostics ----------------------------------------------------

    def _enter_degraded(
        self,
        reason: str,
        events: list[SupervisorEvent],
        notifications: list[str],
    ) -> None:
        changed = self.degraded_reason != reason
        self.phase = LoadManagerPhase.DEGRADED
        self.degraded_reason = reason
        self.last_reason = reason
        if changed:
            events.append(SupervisorEvent("warning", f"Load Manager: {reason}"))
            notifications.append(f"Load Manager: {reason}")

    def _emit_once(
        self,
        key: str,
        level: str,
        message: str,
        events: list[SupervisorEvent],
        notifications: list[str],
        *,
        notify: bool,
    ) -> None:
        if self._last_alert_key == key:
            return
        self._last_alert_key = key
        events.append(SupervisorEvent(level, message))
        if notify:
            notifications.append(message)
        self.last_reason = message

    def _reset_overload_timers(self) -> None:
        self._nominal_overload_since = None
        self._maximum_overload_since = None

    @staticmethod
    def _state_text(value: bool | None) -> str:
        if value is True:
            return "on"
        if value is False:
            return "off"
        return "unknown"

    @staticmethod
    def _decision(
        actions: list[LoadAction],
        events: list[SupervisorEvent],
        notifications: list[str],
        transfer_permitted: bool,
    ) -> LoadManagerDecision:
        return LoadManagerDecision(
            actions=tuple(actions),
            events=tuple(events),
            notifications=tuple(notifications),
            transfer_permitted=transfer_permitted,
        )

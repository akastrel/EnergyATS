"""Persisted history and aggregate statistics for physical generator runs.

This module is observational only. It never requests starts/stops and never
participates in ATS arbitration. It is also the single owner of physical RUNNING
continuity used by Scheduled Exercise to decide whether a run qualifies.

A historical run is created only when EnergyATS observes a clean OFF -> RUNNING
transition. If observation begins while a generator is already RUNNING, elapsed
runtime before that baseline is unknown and is never added to history. The
continuously observed part may still become a qualifying run after the full
configured interval has been re-proven after restart/reconnect.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping

from domain import GeneratorSlot, SupervisorEvent

SLOTS = (GeneratorSlot.A, GeneratorSlot.B)
_HISTORY_LIMIT = 100


class GeneratorRunType(str, Enum):
    AUTOMATIC = "automatic"
    MANUAL = "manual"
    EXERCISE = "exercise"
    EXTERNAL = "external"


class GeneratorRunResult(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"


@dataclass
class ActiveGeneratorRun:
    observed_since: float
    observed_since_local: str
    run_type: GeneratorRunType
    start_edge_observed: bool
    faulted: bool = False
    fault_reason: str | None = None
    qualified: bool = False


@dataclass
class GeneratorRunStats:
    total_starts: int = 0
    total_runtime_seconds: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_starts": self.total_starts,
            "total_runtime_seconds": self.total_runtime_seconds,
            "history": list(self.history[-_HISTORY_LIMIT:]),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "GeneratorRunStats":
        total_starts = _non_negative_int(data.get("total_starts", 0), "total_starts")
        total_runtime = _non_negative_int(
            data.get("total_runtime_seconds", 0),
            "total_runtime_seconds",
        )
        history = data.get("history", [])
        if not isinstance(history, list) or not all(
            isinstance(item, Mapping) for item in history
        ):
            raise ValueError("generator run history должен быть list[object]")
        normalized = [_validate_history_item(item) for item in history[-_HISTORY_LIMIT:]]
        return cls(total_starts, total_runtime, normalized)


@dataclass(frozen=True)
class GeneratorRunUpdate:
    events: tuple[SupervisorEvent, ...]
    qualifying_runs: Mapping[GeneratorSlot, datetime]


class GeneratorRunHistory:
    """Observe physical RUNNING continuity and retain run history/statistics."""

    def __init__(self) -> None:
        self.stats = {slot: GeneratorRunStats() for slot in SLOTS}
        self._previous_running: dict[GeneratorSlot, bool | None] = {
            slot: None for slot in SLOTS
        }
        self._active: dict[GeneratorSlot, ActiveGeneratorRun | None] = {
            slot: None for slot in SLOTS
        }

    def invalidate_observation_history(self) -> None:
        """Break physical continuity after restart/reconnect/unknown feedback."""
        self._previous_running = {slot: None for slot in SLOTS}
        self._active = {slot: None for slot in SLOTS}

    def step(
        self,
        *,
        now: float,
        local_now: datetime,
        running: Mapping[GeneratorSlot, bool | None],
        faults: Mapping[GeneratorSlot, str | None],
        run_types: Mapping[GeneratorSlot, GeneratorRunType],
        generator_names: Mapping[GeneratorSlot, str],
        qualifying_seconds: Mapping[GeneratorSlot, float] | None = None,
    ) -> GeneratorRunUpdate:
        """Process one physical snapshot.

        ``qualifying_seconds`` is optional because history/statistics do not
        depend on Scheduled Exercise. When provided, a slot is returned in
        ``qualifying_runs`` once after a continuous fault-free observed interval
        reaches its configured threshold.
        """
        events: list[SupervisorEvent] = []
        qualifying: dict[GeneratorSlot, datetime] = {}
        thresholds = qualifying_seconds or {}

        for slot in SLOTS:
            current = running.get(slot)
            previous = self._previous_running[slot]

            if current is None:
                # Unknown feedback invalidates continuity. Do not guess a stop
                # time or continue a qualifying timer through the gap.
                self._previous_running[slot] = None
                self._active[slot] = None
                continue

            if previous is None:
                # First valid snapshot is the new observation baseline. If the
                # generator is already RUNNING, its real start is unknown, so it
                # must never create a historical start/runtime entry. We still
                # observe from this moment so a full new qualifying interval can
                # be proven after restart/reconnect.
                self._previous_running[slot] = current
                if current:
                    self._begin_observation(
                        slot,
                        now=now,
                        local_now=local_now,
                        run_type=run_types[slot],
                        start_edge_observed=False,
                    )
                    self._capture_fault(slot, faults.get(slot))
                continue

            if not previous and current:
                self._begin_observation(
                    slot,
                    now=now,
                    local_now=local_now,
                    run_type=run_types[slot],
                    start_edge_observed=True,
                )
                self.stats[slot].total_starts += 1

            active = self._active[slot]
            if current and active is not None:
                self._capture_fault(slot, faults.get(slot))
                threshold = float(thresholds.get(slot, 0.0))
                if (
                    threshold > 0
                    and not active.qualified
                    and not active.faulted
                    and now - active.observed_since >= threshold
                ):
                    active.qualified = True
                    qualifying[slot] = local_now

            if previous and not current:
                event = self._finish_observation(
                    slot,
                    now=now,
                    local_now=local_now,
                    final_fault=faults.get(slot),
                    generator_name=generator_names[slot],
                )
                if event is not None:
                    events.append(event)

            self._previous_running[slot] = current

        return GeneratorRunUpdate(tuple(events), qualifying)

    def _begin_observation(
        self,
        slot: GeneratorSlot,
        *,
        now: float,
        local_now: datetime,
        run_type: GeneratorRunType,
        start_edge_observed: bool,
    ) -> None:
        self._active[slot] = ActiveGeneratorRun(
            observed_since=now,
            observed_since_local=local_now.isoformat(),
            run_type=run_type,
            start_edge_observed=start_edge_observed,
        )

    def _capture_fault(self, slot: GeneratorSlot, fault: str | None) -> None:
        active = self._active[slot]
        if active is None or fault is None:
            return
        active.faulted = True
        if active.fault_reason is None:
            active.fault_reason = str(fault)

    def _finish_observation(
        self,
        slot: GeneratorSlot,
        *,
        now: float,
        local_now: datetime,
        final_fault: str | None,
        generator_name: str,
    ) -> SupervisorEvent | None:
        active = self._active[slot]
        self._active[slot] = None
        if active is None or not active.start_edge_observed:
            # RUNNING existed before the current valid observation window. Its
            # physical start/runtime are unknowable, so no history is invented.
            return None

        self._capture_fault_for(active, final_fault)
        duration = max(0, int(now - active.observed_since))
        result = (
            GeneratorRunResult.FAILED
            if active.faulted
            else GeneratorRunResult.SUCCESS
        )
        record = {
            "start_time": active.observed_since_local,
            "end_time": local_now.isoformat(),
            "duration_seconds": duration,
            "type": active.run_type.value,
            "result": result.value,
            "failure_reason": active.fault_reason,
        }
        stats = self.stats[slot]
        stats.history.append(record)
        stats.history = stats.history[-_HISTORY_LIMIT:]
        stats.total_runtime_seconds += duration

        return SupervisorEvent(
            "info" if result == GeneratorRunResult.SUCCESS else "warning",
            _completed_message(
                generator_name,
                active.run_type,
                duration,
                result,
            ),
        )

    @staticmethod
    def _capture_fault_for(active: ActiveGeneratorRun, fault: str | None) -> None:
        if fault is None:
            return
        active.faulted = True
        if active.fault_reason is None:
            active.fault_reason = str(fault)

    def status_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {}
        for slot in SLOTS:
            prefix = f"generator_{slot.value.lower()}"
            stats = self.stats[slot]
            last = stats.history[-1] if stats.history else None
            attrs.update(
                {
                    f"{prefix}_total_starts": stats.total_starts,
                    f"{prefix}_total_runtime_seconds": stats.total_runtime_seconds,
                    f"{prefix}_total_runtime_hours": round(
                        stats.total_runtime_seconds / 3600,
                        1,
                    ),
                    f"{prefix}_last_run_start": (
                        last.get("start_time") if last else None
                    ),
                    f"{prefix}_last_run_end": (
                        last.get("end_time") if last else None
                    ),
                    f"{prefix}_last_run_duration_seconds": (
                        last.get("duration_seconds") if last else None
                    ),
                    f"{prefix}_last_run_type": (
                        last.get("type") if last else None
                    ),
                    f"{prefix}_last_run_result": (
                        last.get("result") if last else None
                    ),
                }
            )
        return attrs

    def to_dict(self) -> dict[str, Any]:
        # Active observations are deliberately not persisted. Restart is an
        # observation gap, so continuity must be proven again from fresh feedback.
        return {
            "slots": {
                slot.value: self.stats[slot].to_dict()
                for slot in SLOTS
            }
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "GeneratorRunHistory":
        slots = data.get("slots")
        if not isinstance(slots, Mapping):
            raise ValueError("generator_runs.slots должен быть object")
        tracker = cls()
        tracker.stats = {
            slot: GeneratorRunStats.from_dict(
                _mapping(slots.get(slot.value), f"generator_runs.slots.{slot.value}")
            )
            for slot in SLOTS
        }
        tracker.invalidate_observation_history()
        return tracker


def _completed_message(
    generator: str,
    run_type: GeneratorRunType,
    duration_seconds: int,
    result: GeneratorRunResult,
) -> str:
    type_text = {
        GeneratorRunType.AUTOMATIC: "автоматический",
        GeneratorRunType.MANUAL: "ручной",
        GeneratorRunType.EXERCISE: "пробный",
        GeneratorRunType.EXTERNAL: "внешний",
    }[run_type]
    result_text = (
        "успешно"
        if result == GeneratorRunResult.SUCCESS
        else "зафиксирована ошибка"
    )
    return (
        f"{generator}: {type_text} запуск завершён; время работы — "
        f"{_duration_text(duration_seconds)}; результат — {result_text}."
    )


def _duration_text(seconds: int) -> str:
    seconds = max(0, seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours} ч {minutes} мин" if minutes else f"{hours} ч"
    if minutes:
        return f"{minutes} мин {secs} с" if secs else f"{minutes} мин"
    return f"{secs} с"


def _validate_history_item(data: Mapping[str, Any]) -> dict[str, Any]:
    run_type = GeneratorRunType(str(data["type"]))
    result = GeneratorRunResult(str(data["result"]))
    start = str(data["start_time"])
    end = str(data["end_time"])
    datetime.fromisoformat(start)
    datetime.fromisoformat(end)
    duration = _non_negative_int(data["duration_seconds"], "duration_seconds")
    reason = data.get("failure_reason")
    return {
        "start_time": start,
        "end_time": end,
        "duration_seconds": duration,
        "type": run_type.value,
        "result": result.value,
        "failure_reason": None if reason is None else str(reason),
    }


def _non_negative_int(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} должен быть неотрицательным integer")
    return value


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} должен быть object")
    return value

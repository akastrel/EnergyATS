"""Application-level coordinator for generator run monitoring.

`GeneratorRunHistory` owns the physical RUNNING timeline and persisted statistics.
This monitor owns the feature integration around it: run classification, Scheduled
Exercise qualification, soft-state restore and generator display names.

The component is observational only. It never requests generator start/stop and
never participates in Supervisor arbitration.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from domain import GeneratorSlot, SessionReason, SupervisorEvent
from energy_supervisor import GeneratorSession, SupervisorObservation
from exercise_scheduler import ExerciseScheduler
from generator_bus import GeneratorRunContext
from generator_run_history import GeneratorRunHistory, GeneratorRunType

SLOTS = (GeneratorSlot.A, GeneratorSlot.B)


class GeneratorRunMonitor:
    """Single application boundary for the GeneratorRun feature."""

    def __init__(
        self,
        *,
        history: GeneratorRunHistory,
        exercise_scheduler: ExerciseScheduler,
        generator_name: Callable[[GeneratorSlot], str],
        logger: logging.Logger,
    ) -> None:
        self._history = history
        self._exercise_scheduler = exercise_scheduler
        self._generator_name = generator_name
        self._log = logger

    @classmethod
    def from_saved_state(
        cls,
        saved: Mapping[str, Any] | None,
        *,
        exercise_scheduler: ExerciseScheduler,
        generator_name: Callable[[GeneratorSlot], str],
        logger: logging.Logger,
    ) -> "GeneratorRunMonitor":
        """Restore optional GeneratorRun state without affecting core Recovery."""
        history = GeneratorRunHistory()
        payload = saved.get("generator_runs") if isinstance(saved, Mapping) else None
        if payload is not None:
            try:
                if not isinstance(payload, Mapping):
                    raise ValueError("generator_runs должен быть object")
                history = GeneratorRunHistory.from_dict(payload)
            except Exception as exc:
                logger.warning(
                    "Не удалось восстановить историю запусков генераторов; "
                    "используем пустую статистику: %s",
                    exc,
                )
        return cls(
            history=history,
            exercise_scheduler=exercise_scheduler,
            generator_name=generator_name,
            logger=logger,
        )

    def observe(
        self,
        *,
        now: float,
        local_now: datetime,
        observation: SupervisorObservation,
        session: GeneratorSession | None,
    ) -> tuple[SupervisorEvent, ...]:
        """Observe one physical snapshot and apply proven Exercise qualification."""
        if observation.bus is None:
            raise ValueError("GeneratorRun требует GeneratorBusStatus в observation")

        update = self._history.step(
            now=now,
            local_now=local_now,
            running={
                slot: observation.generators[slot].running
                for slot in SLOTS
            },
            faults={
                slot: observation.generators[slot].fault
                for slot in SLOTS
            },
            run_types=self._classify_run_types(
                session=session,
                run_contexts=observation.bus.run_contexts,
            ),
            generator_names={slot: self._generator_name(slot) for slot in SLOTS},
            qualifying_seconds={
                slot: self._exercise_scheduler.configs[slot].run_minutes * 60
                for slot in SLOTS
            },
        )
        self._exercise_scheduler.record_qualifying_runs(update.qualifying_runs)
        return update.events

    def invalidate_observation_history(self) -> None:
        self._history.invalidate_observation_history()

    def status_attributes(self) -> dict[str, Any]:
        return self._history.status_attributes()

    def to_dict(self) -> dict[str, Any]:
        return self._history.to_dict()

    @staticmethod
    def _classify_run_types(
        *,
        session: GeneratorSession | None,
        run_contexts: Mapping[GeneratorSlot, GeneratorRunContext],
    ) -> dict[GeneratorSlot, GeneratorRunType]:
        """Classify start edges only from already-established ATS ownership facts."""
        result: dict[GeneratorSlot, GeneratorRunType] = {}
        for slot in SLOTS:
            if run_contexts[slot] == GeneratorRunContext.TEST_RUN:
                result[slot] = GeneratorRunType.EXERCISE
            elif session is not None and session.generator == slot:
                result[slot] = (
                    GeneratorRunType.AUTOMATIC
                    if session.reason == SessionReason.GRID_OUTAGE
                    else GeneratorRunType.MANUAL
                )
            else:
                result[slot] = GeneratorRunType.EXTERNAL
        return result

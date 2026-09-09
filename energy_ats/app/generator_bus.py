"""Наблюдаемая модель общей генераторной шины.

Физический выбор A/B выполняют аппаратно заблокированные контакторы. EnergyATS
не управляет этим выбором; он только ведёт логического владельца шины по
последовательности физических RUNNING и сохраняет это знание между restart.

Здесь же хранится происхождение текущего запуска каждого двигателя. Это
позволяет отличить outage-related внешний запуск от TEST_RUN и после возврата
Grid остановить только те внешние генераторы, для которых такое право явно
разрешено требованиями.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from domain import GeneratorSlot


class GeneratorBusOwner(str, Enum):
    A = "A"
    B = "B"
    NONE = "none"
    UNKNOWN = "unknown"

    @property
    def slot(self) -> GeneratorSlot | None:
        if self == GeneratorBusOwner.A:
            return GeneratorSlot.A
        if self == GeneratorBusOwner.B:
            return GeneratorSlot.B
        return None

    @classmethod
    def for_slot(cls, slot: GeneratorSlot) -> "GeneratorBusOwner":
        return cls.A if slot == GeneratorSlot.A else cls.B


class GeneratorRunContext(str, Enum):
    NONE = "none"
    MANAGED_OUTAGE = "managed_outage"
    MANAGED_OTHER = "managed_other"
    EXTERNAL_OUTAGE = "external_outage"
    TEST_RUN = "test_run"
    OTHER_EXTERNAL = "other_external"
    UNKNOWN_EXTERNAL = "unknown_external"

    @property
    def outage_related(self) -> bool:
        return self in {
            GeneratorRunContext.MANAGED_OUTAGE,
            GeneratorRunContext.EXTERNAL_OUTAGE,
        }


@dataclass(frozen=True)
class GeneratorBusStatus:
    owner: GeneratorBusOwner
    run_contexts: Mapping[GeneratorSlot, GeneratorRunContext]

    @property
    def owner_slot(self) -> GeneratorSlot | None:
        return self.owner.slot

    @property
    def outage_related_slots(self) -> frozenset[GeneratorSlot]:
        return frozenset(
            slot
            for slot, context in self.run_contexts.items()
            if context.outage_related
        )


class GeneratorBusTracker:
    """Вести owner и происхождение запусков без управления контакторами."""

    def __init__(self) -> None:
        self.owner = GeneratorBusOwner.UNKNOWN
        self.run_contexts: dict[GeneratorSlot, GeneratorRunContext] = {
            GeneratorSlot.A: GeneratorRunContext.NONE,
            GeneratorSlot.B: GeneratorRunContext.NONE,
        }
        self.previous_running: dict[GeneratorSlot, bool | None] = {
            GeneratorSlot.A: None,
            GeneratorSlot.B: None,
        }
        self.initialized = False

    def status(self) -> GeneratorBusStatus:
        return GeneratorBusStatus(
            owner=self.owner,
            run_contexts=dict(self.run_contexts),
        )

    def update(
        self,
        running: Mapping[GeneratorSlot, bool | None],
        *,
        grid_ready: bool | None,
        test_mode: bool | None,
        managed_slot: GeneratorSlot | None,
        managed_outage: bool,
    ) -> GeneratorBusStatus:
        """Обновить модель по одному физическому снимку.

        ``test_mode`` применяется только к новому фронту OFF->ON. Уже
        классифицированный запуск не меняет происхождение из-за последующего
        переключения helper-а. Если helper недоступен, внешний новый запуск
        получает UNKNOWN_EXTERNAL: безопаснее не остановить его автоматически,
        чем ошибочно принять неизвестный запуск за outage-related.
        """
        if any(running.get(slot) is None for slot in (GeneratorSlot.A, GeneratorSlot.B)):
            return self.status()

        current = {
            slot: running[slot] is True
            for slot in (GeneratorSlot.A, GeneratorSlot.B)
        }

        if not self.initialized:
            self._initialize_current_runs(
                current,
                managed_slot=managed_slot,
                managed_outage=managed_outage,
            )
            self._infer_initial_owner(current)
            self.previous_running = dict(current)
            self.initialized = True
            return self.status()

        for slot in (GeneratorSlot.A, GeneratorSlot.B):
            was_running = self.previous_running[slot] is True
            is_running = current[slot]
            if not was_running and is_running:
                self.run_contexts[slot] = self._new_run_context(
                    slot,
                    grid_ready=grid_ready,
                    test_mode=test_mode,
                    managed_slot=managed_slot,
                    managed_outage=managed_outage,
                )
            elif was_running and not is_running:
                self.run_contexts[slot] = GeneratorRunContext.NONE

        self._update_owner(current)
        self.previous_running = dict(current)
        return self.status()

    def to_dict(self) -> dict[str, object]:
        return {
            "owner": self.owner.value,
            "run_contexts": {
                slot.value: context.value
                for slot, context in self.run_contexts.items()
            },
            "previous_running": {
                slot.value: value
                for slot, value in self.previous_running.items()
            },
            "initialized": self.initialized,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "GeneratorBusTracker":
        tracker = cls()
        tracker.owner = GeneratorBusOwner(str(data["owner"]))

        contexts = data.get("run_contexts")
        previous = data.get("previous_running")
        if not isinstance(contexts, Mapping) or not isinstance(previous, Mapping):
            raise ValueError("Некорректное состояние generator_bus")

        for slot in (GeneratorSlot.A, GeneratorSlot.B):
            tracker.run_contexts[slot] = GeneratorRunContext(
                str(contexts[slot.value])
            )
            value = previous[slot.value]
            if value is not None and type(value) is not bool:
                raise ValueError("previous_running должен быть boolean/null")
            tracker.previous_running[slot] = value

        initialized = data.get("initialized", False)
        if type(initialized) is not bool:
            raise ValueError("generator_bus.initialized должен быть boolean")
        tracker.initialized = initialized
        return tracker

    def _initialize_current_runs(
        self,
        current: Mapping[GeneratorSlot, bool],
        *,
        managed_slot: GeneratorSlot | None,
        managed_outage: bool,
    ) -> None:
        """Не приписывать происхождение уже работающему внешнему двигателю."""
        for slot in (GeneratorSlot.A, GeneratorSlot.B):
            if not current[slot]:
                self.run_contexts[slot] = GeneratorRunContext.NONE
                continue
            if managed_slot == slot:
                self.run_contexts[slot] = (
                    GeneratorRunContext.MANAGED_OUTAGE
                    if managed_outage
                    else GeneratorRunContext.MANAGED_OTHER
                )
            elif self.run_contexts[slot] == GeneratorRunContext.NONE:
                self.run_contexts[slot] = GeneratorRunContext.UNKNOWN_EXTERNAL

    def _infer_initial_owner(self, current: Mapping[GeneratorSlot, bool]) -> None:
        active = [slot for slot, value in current.items() if value]
        if not active:
            self.owner = GeneratorBusOwner.NONE
        elif len(active) == 1:
            self.owner = GeneratorBusOwner.for_slot(active[0])
        elif self.owner.slot not in active:
            self.owner = GeneratorBusOwner.UNKNOWN

    def _update_owner(self, current: Mapping[GeneratorSlot, bool]) -> None:
        active = [slot for slot, value in current.items() if value]
        if not active:
            self.owner = GeneratorBusOwner.NONE
            return
        if len(active) == 1:
            self.owner = GeneratorBusOwner.for_slot(active[0])
            return

        if self.owner.slot in active:
            return

        previously_active = [
            slot
            for slot in (GeneratorSlot.A, GeneratorSlot.B)
            if self.previous_running.get(slot) is True
        ]
        if len(previously_active) == 1 and previously_active[0] in active:
            self.owner = GeneratorBusOwner.for_slot(previously_active[0])
            return

        self.owner = GeneratorBusOwner.UNKNOWN

    def _new_run_context(
        self,
        slot: GeneratorSlot,
        *,
        grid_ready: bool | None,
        test_mode: bool | None,
        managed_slot: GeneratorSlot | None,
        managed_outage: bool,
    ) -> GeneratorRunContext:
        if managed_slot == slot:
            return (
                GeneratorRunContext.MANAGED_OUTAGE
                if managed_outage
                else GeneratorRunContext.MANAGED_OTHER
            )
        if test_mode is True:
            return GeneratorRunContext.TEST_RUN
        if test_mode is None:
            return GeneratorRunContext.UNKNOWN_EXTERNAL
        if grid_ready is False:
            return GeneratorRunContext.EXTERNAL_OUTAGE
        if grid_ready is True:
            return GeneratorRunContext.OTHER_EXTERNAL
        return GeneratorRunContext.UNKNOWN_EXTERNAL

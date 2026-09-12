"""Логический owner общей генераторной шины и контекст текущих запусков.

A/B выбираются аппаратными взаимно заблокированными контакторами. EnergyATS
только восстанавливает FIFO-owner по истории RUNNING и помнит, какие текущие
запуски относятся к outage или TEST_RUN.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from domain import GeneratorSlot

SLOTS = (GeneratorSlot.A, GeneratorSlot.B)


class GeneratorBusOwner(str, Enum):
    A = "A"
    B = "B"
    NONE = "none"
    UNKNOWN = "unknown"

    @property
    def slot(self) -> GeneratorSlot | None:
        return {
            GeneratorBusOwner.A: GeneratorSlot.A,
            GeneratorBusOwner.B: GeneratorSlot.B,
        }.get(self)

    @classmethod
    def for_slot(cls, slot: GeneratorSlot) -> "GeneratorBusOwner":
        return cls.A if slot == GeneratorSlot.A else cls.B


class GeneratorRunContext(str, Enum):
    NONE = "none"
    OUTAGE_RELATED = "outage_related"
    TEST_RUN = "test_run"
    OTHER = "other"
    UNKNOWN = "unknown"

    @property
    def outage_related(self) -> bool:
        return self == GeneratorRunContext.OUTAGE_RELATED


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
    """Вести FIFO-owner и контекст каждого непрерывного RUNNING."""

    def __init__(self) -> None:
        self.owner = GeneratorBusOwner.UNKNOWN
        self.run_contexts = {slot: GeneratorRunContext.NONE for slot in SLOTS}
        self.previous_running: dict[GeneratorSlot, bool] | None = None

    def status(self) -> GeneratorBusStatus:
        return GeneratorBusStatus(self.owner, dict(self.run_contexts))

    def invalidate_observation_history(self) -> None:
        """Забыть FIFO-историю, непрерывность которой больше не доказана.

        После gap/restart два RUNNING двигателя не позволяют восстановить порядок
        их последних стартов. До новой наблюдаемой истории owner обязан оставаться
        UNKNOWN; один RUNNING или оба OFF снова дают однозначный результат.
        """
        self.owner = GeneratorBusOwner.UNKNOWN
        self.previous_running = None
        self.run_contexts = {slot: GeneratorRunContext.NONE for slot in SLOTS}

    def update(
        self,
        running: Mapping[GeneratorSlot, bool | None],
        *,
        grid_ready: bool | None,
        test_mode: bool | None,
        managed_slot: GeneratorSlot | None = None,
        managed_outage: bool = False,
        internal_test_slots: frozenset[GeneratorSlot] = frozenset(),
    ) -> GeneratorBusStatus:
        if any(running.get(slot) is None for slot in SLOTS):
            # UNKNOWN/UNAVAILABLE разрывает доказанную непрерывность RUNNING.
            # Сохранять старый owner через такой разрыв небезопасно.
            self.invalidate_observation_history()
            return self.status()

        current = {slot: running[slot] is True for slot in SLOTS}
        if self.previous_running is None:
            self._initialize_contexts(
                current,
                managed_slot,
                managed_outage,
                internal_test_slots,
            )
        else:
            for slot in SLOTS:
                if not self.previous_running[slot] and current[slot]:
                    if slot in internal_test_slots:
                        self.run_contexts[slot] = GeneratorRunContext.TEST_RUN
                    else:
                        self.run_contexts[slot] = self._classify_new_run(
                            grid_ready,
                            test_mode,
                        )
                elif self.previous_running[slot] and not current[slot]:
                    self.run_contexts[slot] = GeneratorRunContext.NONE

        self._update_owner(current)
        self.previous_running = current
        return self.status()

    def _initialize_contexts(
        self,
        current: Mapping[GeneratorSlot, bool],
        managed_slot: GeneratorSlot | None,
        managed_outage: bool,
        internal_test_slots: frozenset[GeneratorSlot],
    ) -> None:
        for slot in SLOTS:
            if not current[slot]:
                self.run_contexts[slot] = GeneratorRunContext.NONE
            elif slot in internal_test_slots:
                self.run_contexts[slot] = GeneratorRunContext.TEST_RUN
            elif managed_slot == slot:
                self.run_contexts[slot] = (
                    GeneratorRunContext.OUTAGE_RELATED
                    if managed_outage
                    else GeneratorRunContext.OTHER
                )
            else:
                # После первого snapshot, restart или gap причина внешнего
                # непрерывного run без новой истории недоказуема.
                self.run_contexts[slot] = GeneratorRunContext.UNKNOWN

    @staticmethod
    def _classify_new_run(
        grid_ready: bool | None,
        test_mode: bool | None,
    ) -> GeneratorRunContext:
        if test_mode is True:
            return GeneratorRunContext.TEST_RUN
        if test_mode is None or grid_ready is None:
            return GeneratorRunContext.UNKNOWN
        return (
            GeneratorRunContext.OUTAGE_RELATED
            if grid_ready is False
            else GeneratorRunContext.OTHER
        )

    def _update_owner(self, current: Mapping[GeneratorSlot, bool]) -> None:
        active = [slot for slot in SLOTS if current[slot]]
        if not active:
            self.owner = GeneratorBusOwner.NONE
            return
        if len(active) == 1:
            self.owner = GeneratorBusOwner.for_slot(active[0])
            return
        if self.owner.slot in active:
            return
        if self.previous_running is not None:
            previous = [
                slot for slot in SLOTS if self.previous_running[slot]
            ]
            if len(previous) == 1 and previous[0] in active:
                self.owner = GeneratorBusOwner.for_slot(previous[0])
                return
        self.owner = GeneratorBusOwner.UNKNOWN

    def to_dict(self) -> dict[str, object]:
        return {
            "owner": self.owner.value,
            "run_contexts": {
                slot.value: self.run_contexts[slot].value for slot in SLOTS
            },
            "previous_running": (
                None
                if self.previous_running is None
                else {
                    slot.value: self.previous_running[slot]
                    for slot in SLOTS
                }
            ),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "GeneratorBusTracker":
        # Persisted owner/context остаются полезны для диагностики journal, но
        # restart сам по себе является observation gap. Поэтому runtime не имеет
        # права считать сохранённый FIFO-order доказательством непрерывности.
        # Валидируем старый payload для совместимости schema, затем стартуем с
        # недостоверной историей и восстанавливаем только из новых наблюдений.
        GeneratorBusOwner(str(data["owner"]))
        contexts = data.get("run_contexts")
        if not isinstance(contexts, Mapping):
            raise ValueError("Некорректное состояние generator_bus.run_contexts")
        for slot in SLOTS:
            GeneratorRunContext(str(contexts[slot.value]))

        previous = data.get("previous_running")
        if previous is not None:
            if not isinstance(previous, Mapping):
                raise ValueError("Некорректное состояние generator_bus.previous_running")
            for slot in SLOTS:
                if type(previous[slot.value]) is not bool:
                    raise ValueError("previous_running должен содержать boolean")

        tracker = cls()
        tracker.invalidate_observation_history()
        return tracker

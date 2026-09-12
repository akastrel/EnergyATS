"""Человеко-читаемый журнал изменений физических сигналов EnergyATS.

Команды, отправленные EnergyATS, уже записываются в Logbook отдельно. Этот
tracker фиксирует другую сторону причинно-следственной цепочки — реально
наблюдённое изменение input/feedback state. Первый snapshot после start/reconnect
только задаёт baseline и не создаёт ложных событий.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from domain import GeneratorSlot, PowerPath, PowerSource, SupervisorEvent
from generator_bus import GeneratorBusOwner


@dataclass(frozen=True)
class _GeneratorPhysicalState:
    running: bool | None
    remote_on: bool | None


@dataclass(frozen=True)
class _PhysicalState:
    grid_ready: bool | None
    automatic_transfer_enabled: bool
    power_path: PowerPath
    power_source: PowerSource
    emergency_stop: bool | None
    generators: Mapping[GeneratorSlot, _GeneratorPhysicalState]
    bus_owner: GeneratorBusOwner


class PhysicalEventTracker:
    """Фиксировать изменения наблюдаемых core physical/control signals."""

    def __init__(self) -> None:
        self._previous: _PhysicalState | None = None

    def reset(self) -> None:
        """Следующий snapshot становится baseline без генерации событий."""
        self._previous = None

    def observe(
        self,
        *,
        grid_ready: bool | None,
        automatic_transfer_enabled: bool,
        power_path: PowerPath,
        power_source: PowerSource,
        emergency_stop: bool | None,
        generators: Mapping[GeneratorSlot, tuple[bool | None, bool | None]],
        bus_owner: GeneratorBusOwner,
        generator_names: Mapping[GeneratorSlot, str],
        managed_slots: frozenset[GeneratorSlot] = frozenset(),
    ) -> tuple[SupervisorEvent, ...]:
        current = _PhysicalState(
            grid_ready=grid_ready,
            automatic_transfer_enabled=automatic_transfer_enabled,
            power_path=power_path,
            power_source=power_source,
            emergency_stop=emergency_stop,
            generators={
                slot: _GeneratorPhysicalState(running=value[0], remote_on=value[1])
                for slot, value in generators.items()
            },
            bus_owner=bus_owner,
        )
        previous = self._previous
        self._previous = current
        if previous is None:
            return ()

        events: list[SupervisorEvent] = []
        self._append_bool_change(
            events,
            previous.grid_ready,
            current.grid_ready,
            true_message="Входная сеть восстановлена (Grid Input Ready = ON).",
            false_message="Входная сеть пропала (Grid Input Ready = OFF).",
            unknown_message="Состояние входной сети стало неизвестно.",
            false_level="warning",
        )

        if previous.automatic_transfer_enabled != current.automatic_transfer_enabled:
            events.append(
                SupervisorEvent(
                    "info",
                    "Автоматический переход на резерв "
                    + ("разрешён." if current.automatic_transfer_enabled else "отключён."),
                )
            )

        if previous.power_path != current.power_path:
            events.append(
                SupervisorEvent(
                    "warning" if current.power_path == PowerPath.UNKNOWN else "info",
                    self._power_path_message(previous.power_path, current.power_path),
                )
            )

        if previous.power_source != current.power_source:
            events.append(
                SupervisorEvent(
                    "warning"
                    if current.power_source in {PowerSource.NO_POWER, PowerSource.UNKNOWN}
                    else "info",
                    self._power_source_message(current.power_source),
                )
            )

        for slot in (GeneratorSlot.A, GeneratorSlot.B):
            old = previous.generators[slot]
            new = current.generators[slot]
            name = generator_names.get(slot, f"Generator {slot.value}")
            managed = slot in managed_slots

            self._append_bool_change(
                events,
                old.remote_on,
                new.remote_on,
                true_message=f"{name}: REMOTE START включён.",
                false_message=f"{name}: REMOTE START выключен.",
                unknown_message=f"{name}: состояние REMOTE START стало неизвестно.",
            )

            if old.running is not new.running:
                if new.running is None:
                    events.append(
                        SupervisorEvent(
                            "warning",
                            f"{name}: состояние RUNNING стало неизвестно.",
                        )
                    )
                elif new.running:
                    qualifier = (
                        "managed EnergyATS"
                        if managed
                        else "внешний/неуправляемый запуск"
                    )
                    events.append(
                        SupervisorEvent(
                            "info",
                            f"{name}: RUNNING = ON — двигатель запущен ({qualifier}).",
                        )
                    )
                else:
                    qualifier = (
                        "managed EnergyATS"
                        if managed
                        else "внешняя/неуправляемая остановка"
                    )
                    events.append(
                        SupervisorEvent(
                            "info",
                            f"{name}: RUNNING = OFF — двигатель остановлен ({qualifier}).",
                        )
                    )

        if previous.bus_owner != current.bus_owner:
            events.append(
                SupervisorEvent(
                    "warning" if current.bus_owner == GeneratorBusOwner.UNKNOWN else "info",
                    "Generator bus owner изменился: "
                    f"{self._owner_text(previous.bus_owner, generator_names)} → "
                    f"{self._owner_text(current.bus_owner, generator_names)}.",
                )
            )

        self._append_bool_change(
            events,
            previous.emergency_stop,
            current.emergency_stop,
            true_message="Generators Emergency Stop активирован.",
            false_message="Generators Emergency Stop снят.",
            unknown_message="Состояние Generators Emergency Stop стало неизвестно.",
            true_level="warning",
        )
        return tuple(events)

    @staticmethod
    def _append_bool_change(
        events: list[SupervisorEvent],
        previous: bool | None,
        current: bool | None,
        *,
        true_message: str,
        false_message: str,
        unknown_message: str,
        true_level: str = "info",
        false_level: str = "info",
    ) -> None:
        if previous is current:
            return
        if current is None:
            events.append(SupervisorEvent("warning", unknown_message))
        elif current:
            events.append(SupervisorEvent(true_level, true_message))
        else:
            events.append(SupervisorEvent(false_level, false_message))

    @staticmethod
    def _power_path_message(previous: PowerPath, current: PowerPath) -> str:
        if current == PowerPath.GRID:
            return "Сетевая ветвь дома подключена (Grid path подтверждён)."
        if current == PowerPath.GENERATOR:
            return "Генераторная ветвь дома подключена (Generator path подтверждён)."
        if current == PowerPath.ISOLATED:
            if previous == PowerPath.GRID:
                return "Подача входной сети в дом отключена; силовой ввод изолирован."
            if previous == PowerPath.GENERATOR:
                return "Подача от генераторной шины в дом отключена; силовой ввод изолирован."
            return "Силовой ввод дома изолирован от Grid и generator bus."
        return "Положение силовых вводов стало неизвестно."

    @staticmethod
    def _power_source_message(source: PowerSource) -> str:
        return {
            PowerSource.GRID: "Подтверждено: дом питается от входной сети Grid.",
            PowerSource.GENERATOR: "Подтверждено: дом питается от генераторной шины.",
            PowerSource.UPS_ONLY: "Подтверждено: силовые вводы сняты; дом в режиме UPS_ONLY.",
            PowerSource.NO_POWER: "Подтверждено: питание дома отсутствует.",
            PowerSource.UNKNOWN: "Источник питания дома стал неизвестен.",
        }[source]

    @staticmethod
    def _owner_text(
        owner: GeneratorBusOwner,
        generator_names: Mapping[GeneratorSlot, str],
    ) -> str:
        if owner.slot is not None:
            return generator_names.get(owner.slot, f"Generator {owner.slot.value}")
        if owner == GeneratorBusOwner.NONE:
            return "none"
        return "unknown"

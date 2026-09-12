"""Человеко-читаемый журнал изменений физических сигналов EnergyATS.

Команды, отправленные EnergyATS, уже записываются в Logbook отдельно. Этот
tracker фиксирует другую сторону причинно-следственной цепочки — реально
наблюдённое изменение feedback/input state. Первый snapshot после start/reconnect
только задаёт baseline и не создаёт ложных событий.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from domain import GeneratorSlot, SupervisorEvent
from generator_bus import GeneratorBusOwner, GeneratorBusStatus
from ha_adapter import HardwareSnapshot


@dataclass(frozen=True)
class _PhysicalState:
    grid_ready: bool | None
    grid_connected: bool | None
    house_on_grid: bool | None
    generator_selected: bool | None
    house_on_generator: bool | None
    emergency_stop: bool | None
    generator_a_remote: bool | None
    generator_a_running: bool | None
    generator_b_remote: bool | None
    generator_b_running: bool | None
    bus_owner: GeneratorBusOwner


class PhysicalEventTracker:
    """Фиксировать только изменения наблюдаемых core physical/control signals."""

    def __init__(self) -> None:
        self._previous: _PhysicalState | None = None

    def reset(self) -> None:
        """Следующий snapshot становится baseline без генерации событий."""
        self._previous = None

    def observe(
        self,
        hardware: HardwareSnapshot,
        bus: GeneratorBusStatus,
        generator_names: Mapping[GeneratorSlot, str],
    ) -> tuple[SupervisorEvent, ...]:
        current = self._capture(hardware, bus)
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
            unknown_message=(
                "Состояние входной сети стало неизвестно "
                "(Grid Input Ready = UNKNOWN)."
            ),
            false_level="warning",
        )
        self._append_bool_change(
            events,
            previous.grid_connected,
            current.grid_connected,
            true_message="Подача Grid в дом включена (Grid Power = ON).",
            false_message="Подача Grid в дом отключена (Grid Power = OFF).",
            unknown_message="Состояние Grid Power стало неизвестно.",
        )
        self._append_bool_change(
            events,
            previous.house_on_grid,
            current.house_on_grid,
            true_message="Подтверждено: дом питается от Grid.",
            false_message="Подтверждение питания дома от Grid снято.",
            unknown_message="Feedback питания дома от Grid стал неизвестен.",
        )
        self._append_bool_change(
            events,
            previous.generator_selected,
            current.generator_selected,
            true_message=(
                "Генераторная ветвь дома выбрана "
                "(Use Generator as Power Source = ON)."
            ),
            false_message=(
                "Генераторная ветвь дома отключена "
                "(Use Generator as Power Source = OFF)."
            ),
            unknown_message="Состояние выбора генераторной ветви стало неизвестно.",
        )
        self._append_bool_change(
            events,
            previous.house_on_generator,
            current.house_on_generator,
            true_message="Подтверждено: дом питается от генераторной шины.",
            false_message="Подтверждение питания дома от генераторной шины снято.",
            unknown_message=(
                "Feedback питания дома от генераторной шины стал неизвестен."
            ),
        )

        for slot, previous_remote, current_remote, previous_running, current_running in (
            (
                GeneratorSlot.A,
                previous.generator_a_remote,
                current.generator_a_remote,
                previous.generator_a_running,
                current.generator_a_running,
            ),
            (
                GeneratorSlot.B,
                previous.generator_b_remote,
                current.generator_b_remote,
                previous.generator_b_running,
                current.generator_b_running,
            ),
        ):
            name = generator_names.get(slot, f"Generator {slot.value}")
            self._append_bool_change(
                events,
                previous_remote,
                current_remote,
                true_message=f"{name}: REMOTE START включён.",
                false_message=f"{name}: REMOTE START выключен.",
                unknown_message=f"{name}: состояние REMOTE START стало неизвестно.",
            )
            self._append_bool_change(
                events,
                previous_running,
                current_running,
                true_message=f"{name}: RUNNING = ON — двигатель запущен.",
                false_message=f"{name}: RUNNING = OFF — двигатель остановлен.",
                unknown_message=f"{name}: состояние RUNNING стало неизвестно.",
            )

        if previous.bus_owner != current.bus_owner:
            level = "warning" if current.bus_owner == GeneratorBusOwner.UNKNOWN else "info"
            events.append(
                SupervisorEvent(
                    level,
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
    def _capture(
        hardware: HardwareSnapshot,
        bus: GeneratorBusStatus,
    ) -> _PhysicalState:
        transfer = hardware.power_transfer
        return _PhysicalState(
            grid_ready=hardware.grid_ready,
            grid_connected=transfer.grid_connected,
            house_on_grid=transfer.house_on_grid,
            generator_selected=transfer.generator_selected,
            house_on_generator=transfer.house_on_generator,
            emergency_stop=hardware.emergency_stop,
            generator_a_remote=hardware.generators[GeneratorSlot.A].remote_on,
            generator_a_running=hardware.generators[GeneratorSlot.A].running,
            generator_b_remote=hardware.generators[GeneratorSlot.B].remote_on,
            generator_b_running=hardware.generators[GeneratorSlot.B].running,
            bus_owner=bus.owner,
        )

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
    def _owner_text(
        owner: GeneratorBusOwner,
        generator_names: Mapping[GeneratorSlot, str],
    ) -> str:
        if owner.slot is not None:
            return generator_names.get(owner.slot, f"Generator {owner.slot.value}")
        if owner == GeneratorBusOwner.NONE:
            return "none"
        return "unknown"

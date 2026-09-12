"""Человеко-читаемый журнал изменений физических сигналов EnergyATS.

Команды, отправленные EnergyATS, уже записываются в Logbook отдельно. Этот
tracker фиксирует другую сторону причинно-следственной цепочки — реально
наблюдённое изменение input/feedback state. Первый snapshot после start/reconnect
только задаёт baseline и не создаёт ложных событий.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from domain import (
    EventVisibility,
    GeneratorSlot,
    PowerPath,
    PowerSource,
    SupervisorEvent,
)
from generator_bus import GeneratorBusOwner
from user_messages import user_event


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
        self._append_bool_event(
            events,
            previous.grid_ready,
            current.grid_ready,
            true_key="grid_input_on",
            false_key="grid_input_off",
            unknown_key="grid_input_unknown",
            false_level="warning",
        )

        if previous.automatic_transfer_enabled != current.automatic_transfer_enabled:
            events.append(
                user_event(
                    "automatic_transfer_on"
                    if current.automatic_transfer_enabled
                    else "automatic_transfer_off"
                )
            )

        if previous.power_path != current.power_path:
            events.append(self._power_path_event(previous.power_path, current.power_path))

        if previous.power_source != current.power_source:
            events.append(self._power_source_event(current.power_source, current.power_path))

        for slot in (GeneratorSlot.A, GeneratorSlot.B):
            old = previous.generators[slot]
            new = current.generators[slot]
            name = generator_names.get(slot, f"Generator {slot.value}")
            managed = slot in managed_slots

            self._append_bool_event(
                events,
                old.remote_on,
                new.remote_on,
                true_key="generator_remote_on",
                false_key="generator_remote_off",
                unknown_key="generator_remote_unknown",
                generator=name,
                visibility=EventVisibility.DETAIL,
            )

            if old.running is not new.running:
                if new.running is None:
                    events.append(
                        user_event(
                            "generator_running_unknown",
                            level="warning",
                            generator=name,
                        )
                    )
                elif new.running:
                    events.append(
                        user_event(
                            "generator_running_managed"
                            if managed
                            else "generator_running_external",
                            generator=name,
                        )
                    )
                else:
                    events.append(
                        user_event(
                            "generator_stopped_managed"
                            if managed
                            else "generator_stopped_external",
                            generator=name,
                        )
                    )

        if previous.bus_owner != current.bus_owner:
            events.append(
                user_event(
                    "bus_owner_changed",
                    level=(
                        "warning"
                        if current.bus_owner == GeneratorBusOwner.UNKNOWN
                        else "info"
                    ),
                    visibility=EventVisibility.DETAIL,
                    old=self._owner_text(previous.bus_owner, generator_names),
                    new=self._owner_text(current.bus_owner, generator_names),
                )
            )

        self._append_bool_event(
            events,
            previous.emergency_stop,
            current.emergency_stop,
            true_key="emergency_stop_on",
            false_key="emergency_stop_off",
            unknown_key="emergency_stop_unknown",
            true_level="warning",
        )
        return tuple(events)

    @staticmethod
    def _append_bool_event(
        events: list[SupervisorEvent],
        previous: bool | None,
        current: bool | None,
        *,
        true_key: str,
        false_key: str,
        unknown_key: str,
        true_level: str = "info",
        false_level: str = "info",
        visibility: EventVisibility = EventVisibility.MAIN,
        **values: object,
    ) -> None:
        if previous is current:
            return
        if current is None:
            events.append(
                user_event(
                    unknown_key,
                    level="warning",
                    visibility=visibility,
                    **values,
                )
            )
        elif current:
            events.append(
                user_event(
                    true_key,
                    level=true_level,
                    visibility=visibility,
                    **values,
                )
            )
        else:
            events.append(
                user_event(
                    false_key,
                    level=false_level,
                    visibility=visibility,
                    **values,
                )
            )

    @staticmethod
    def _power_path_event(previous: PowerPath, current: PowerPath) -> SupervisorEvent:
        if current == PowerPath.GRID:
            return user_event("power_path_grid", visibility=EventVisibility.DETAIL)
        if current == PowerPath.GENERATOR:
            return user_event("power_path_generator", visibility=EventVisibility.DETAIL)
        if current == PowerPath.ISOLATED:
            if previous == PowerPath.GRID:
                return user_event(
                    "power_path_isolated_from_grid",
                    visibility=EventVisibility.DETAIL,
                )
            if previous == PowerPath.GENERATOR:
                return user_event(
                    "power_path_isolated_from_generator",
                    visibility=EventVisibility.DETAIL,
                )
            return user_event(
                "power_path_isolated",
                visibility=EventVisibility.DETAIL,
            )
        return user_event(
            "power_path_unknown",
            level="warning",
            visibility=EventVisibility.DETAIL,
        )

    @staticmethod
    def _power_source_event(source: PowerSource, path: PowerPath) -> SupervisorEvent:
        if source == PowerSource.GRID:
            return user_event("power_source_grid", visibility=EventVisibility.DETAIL)
        if source == PowerSource.GENERATOR:
            return user_event(
                "power_source_generator",
                visibility=EventVisibility.DETAIL,
            )
        if source == PowerSource.UPS_ONLY:
            return user_event(
                "power_source_ups_grid_path"
                if path == PowerPath.GRID
                else "power_source_ups_isolated",
                visibility=EventVisibility.DETAIL,
            )
        if source == PowerSource.NO_POWER:
            return user_event(
                "power_source_none",
                level="warning",
                visibility=EventVisibility.DETAIL,
            )
        return user_event(
            "power_source_unknown",
            level="warning",
            visibility=EventVisibility.DETAIL,
        )

    @staticmethod
    def _owner_text(
        owner: GeneratorBusOwner,
        generator_names: Mapping[GeneratorSlot, str],
    ) -> str:
        if owner.slot is not None:
            return generator_names.get(owner.slot, f"Generator {owner.slot.value}")
        if owner == GeneratorBusOwner.NONE:
            return "нет"
        return "неизвестен"

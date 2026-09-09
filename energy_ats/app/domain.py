"""Общие доменные термины EnergyATS без HA и управляющих алгоритмов."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class GeneratorSlot(str, Enum):
    A = "A"
    B = "B"


class PowerSource(str, Enum):
    """Наблюдаемый источник/режим питания дома."""

    GRID = "grid"
    GENERATOR = "generator"
    UPS_ONLY = "ups_only"
    NO_POWER = "no_power"
    UNKNOWN = "unknown"


class PowerPath(str, Enum):
    """Подтверждённое положение основных контакторов дома."""

    GRID = "grid"
    ISOLATED = "isolated"
    GENERATOR = "generator"
    UNKNOWN = "unknown"

    @classmethod
    def for_source(cls, source: PowerSource) -> "PowerPath":
        if source == PowerSource.GRID:
            return cls.GRID
        if source == PowerSource.GENERATOR:
            return cls.GENERATOR
        if source in {PowerSource.UPS_ONLY, PowerSource.NO_POWER}:
            return cls.ISOLATED
        return cls.UNKNOWN


class SessionReason(str, Enum):
    MANUAL_GENERATOR_START = "manual_generator_start"
    GRID_OUTAGE = "grid_outage"


@dataclass(frozen=True)
class SupervisorEvent:
    level: str
    message: str

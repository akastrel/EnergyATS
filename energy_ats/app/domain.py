"""Общие доменные контракты EnergyATS.

Здесь нет Home Assistant и нет алгоритмов управления оборудованием. Модуль
описывает только термины, которыми обмениваются ES, TPC и GC.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any
from uuid import uuid4


class GeneratorSlot(str, Enum):
    A = "A"
    B = "B"


class PowerSource(str, Enum):
    """Наблюдаемый источник/режим питания дома.

    Конкретный Generator A/B здесь намеренно не кодируется: физический владелец
    общей генераторной шины ведётся отдельно в ``GeneratorBusTracker``.
    ``UPS_ONLY`` — не отдельный ввод и не Battery contactor, а наблюдаемое
    состояние, когда обычная часть дома не питается от Grid/Generator.
    """

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


class TransactionStatus(str, Enum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    RECOVERY_REQUIRED = "recovery_required"


@dataclass
class Transaction:
    """Сохраняемая запись о незавершённой физической операции."""

    transaction_id: str
    kind: str
    target: str
    status: TransactionStatus
    step: str
    last_confirmed_step: str
    started_at: float
    updated_at: float
    message: str = ""

    @classmethod
    def begin(cls, kind: str, target: str, now: float, step: str) -> "Transaction":
        return cls(
            transaction_id=uuid4().hex,
            kind=kind,
            target=target,
            status=TransactionStatus.IN_PROGRESS,
            step=step,
            last_confirmed_step="created",
            started_at=now,
            updated_at=now,
        )

    def advance(self, step: str, now: float, confirmed: str | None = None) -> None:
        if confirmed is not None:
            self.last_confirmed_step = confirmed
        self.step = step
        self.updated_at = now

    def complete(self, now: float, message: str = "") -> None:
        self.status = TransactionStatus.COMPLETED
        self.last_confirmed_step = self.step
        self.updated_at = now
        self.message = message

    def interrupt(self, now: float, message: str) -> None:
        self.status = TransactionStatus.INTERRUPTED
        self.updated_at = now
        self.message = message

    def require_recovery(self, now: float, message: str) -> None:
        self.status = TransactionStatus.RECOVERY_REQUIRED
        self.updated_at = now
        self.message = message

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Transaction":
        return cls(
            transaction_id=str(data["transaction_id"]),
            kind=str(data["kind"]),
            target=str(data["target"]),
            status=TransactionStatus(str(data["status"])),
            step=str(data["step"]),
            last_confirmed_step=str(data.get("last_confirmed_step", "created")),
            started_at=float(data["started_at"]),
            updated_at=float(data["updated_at"]),
            message=str(data.get("message", "")),
        )


@dataclass(frozen=True)
class SupervisorEvent:
    """Сообщение человеку; силовой команды здесь быть не может."""

    level: str
    message: str
    entity_id: str | None = None

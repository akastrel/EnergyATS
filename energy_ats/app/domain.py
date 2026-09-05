"""Общие контракты между уровнями Energy Supervisor.

В этом файле нет алгоритмов и нет Home Assistant. Здесь собраны только
небольшие перечисления и структуры данных, которыми обмениваются контроллеры.
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
    """Фактический или желаемый источник питания дома."""

    GRID = "grid"
    BATTERY = "battery"
    GENERATOR_A = "generator_a"
    GENERATOR_B = "generator_b"
    UNKNOWN = "unknown"

    @classmethod
    def for_generator(cls, slot: GeneratorSlot) -> "PowerSource":
        return cls.GENERATOR_A if slot == GeneratorSlot.A else cls.GENERATOR_B

    @property
    def generator(self) -> GeneratorSlot | None:
        if self == PowerSource.GENERATOR_A:
            return GeneratorSlot.A
        if self == PowerSource.GENERATOR_B:
            return GeneratorSlot.B
        return None


class PowerPath(str, Enum):
    """Подтверждённое физическое положение силового переключателя.

    Источник и путь намеренно разделены. При пропавшей Grid дом питается от
    аккумуляторов МАП, хотя ввод Grid может оставаться подключённым. И наоборот,
    Battery path означает намеренно отключённый ввод Grid независимо от того,
    присутствует ли напряжение перед контактором.
    """

    GRID = "grid_path"
    BATTERY = "battery_path"
    GENERATOR = "generator_path"
    UNKNOWN = "unknown"

    @classmethod
    def for_source(cls, source: PowerSource) -> "PowerPath":
        if source == PowerSource.GRID:
            return cls.GRID
        if source == PowerSource.BATTERY:
            return cls.BATTERY
        if source.generator is not None:
            return cls.GENERATOR
        return cls.UNKNOWN


class SessionReason(str, Enum):
    NONE = "none"
    MANUAL_BACKUP = "manual_backup"
    GRID_OUTAGE = "grid_outage"
    BATTERY_CHARGE = "battery_charge"
    TEST_RUN = "test_run"


class TransactionStatus(str, Enum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    RECOVERY_REQUIRED = "recovery_required"


@dataclass
class Transaction:
    """Сохраняемая запись о незавершённой физической операции.

    Это не database transaction с rollback. Поле ``last_confirmed_step``
    позволяет после restart понять, какое физическое действие уже было
    подтверждено обратной связью, и не повторять команды вслепую.
    """

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

    def note(self, now: float, message: str) -> None:
        """Добавить пояснение, не меняя состояние незавершённой операции."""
        self.updated_at = now
        self.message = message

    def complete(self, now: float, message: str = "") -> None:
        self.status = TransactionStatus.COMPLETED
        self.last_confirmed_step = self.step
        self.updated_at = now
        self.message = message

    def fail(self, now: float, message: str) -> None:
        self.status = TransactionStatus.FAILED
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

    level: str  # info / warning / critical
    message: str
    entity_id: str | None = None

"""Высокоуровневое представление состояния АВР для пользователя.

Модуль ничего не управляет и не участвует в safety/policy. Он только переводит
уже известные факты EnergyATS в два удобных представления:

* светофорный health: green / yellow / red;
* одну человекочитаемую сводку Scheduled Exercise в неделю.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import Enum
from typing import Any, Mapping

from domain import GeneratorSlot, GridInputState, SupervisorEvent
from user_messages import user_message


class HealthLevel(str, Enum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


@dataclass(frozen=True)
class GeneratorHealth:
    name: str
    enabled: bool
    running: bool | None
    fault: str | None


@dataclass(frozen=True)
class HealthInputs:
    armed: bool
    automatic_transfer_enabled: bool
    emergency_stop: bool | None
    required_states_known: bool
    recovery_required: bool
    recovery_reason: str | None
    grid_input_state: GridInputState | None
    generators: tuple[GeneratorHealth, ...]
    bus_owner_unknown_while_running: bool = False
    ups_run_enabled: bool = False
    ups_run_degraded: bool = False
    ups_run_reason: str | None = None
    load_manager_enabled: bool = False
    load_manager_degraded: bool = False
    load_manager_reason: str | None = None
    exercise_failures: tuple[str, ...] = ()


@dataclass(frozen=True)
class HealthStatus:
    level: HealthLevel
    summary: str
    reasons: tuple[str, ...]

    @property
    def icon(self) -> str:
        return {
            HealthLevel.GREEN: "mdi:check-circle",
            HealthLevel.YELLOW: "mdi:alert-circle",
            HealthLevel.RED: "mdi:close-circle",
        }[self.level]

    def attributes(self) -> dict[str, Any]:
        return {
            "friendly_name": "АВР — состояние",
            "summary": self.summary,
            "reasons": list(self.reasons),
            "icon": self.icon,
        }


def evaluate_health(inputs: HealthInputs) -> HealthStatus:
    """Свести уже известные факты к пользовательскому светофору.

    RED означает, что автоматическое резервирование сейчас нельзя считать
    работоспособным. YELLOW означает, что core АВР остаётся работоспособным, но
    есть конкретная причина для внимания. Обычный Grid outage сам по себе не
    ухудшает health: исправный АВР именно для него и предназначен.
    """

    red: list[str] = []
    yellow: list[str] = []

    if not inputs.armed:
        red.append("АВР работает только в режиме наблюдения: аппаратные команды отключены.")
    if not inputs.automatic_transfer_enabled:
        red.append("Автоматический переход на резервное питание отключён.")

    if inputs.emergency_stop is True:
        red.append("Активирован аварийный STOP генераторов.")
    elif inputs.emergency_stop is None:
        red.append("Состояние аварийного STOP генераторов неизвестно.")

    if not inputs.required_states_known:
        red.append("Неизвестны обязательные сигналы обратной связи АВР.")

    if inputs.recovery_required:
        reason = inputs.recovery_reason or "требуется безопасное восстановление силовой схемы"
        red.append(f"АВР требует восстановления: {reason}.")

    if inputs.bus_owner_unknown_while_running:
        red.append("Нельзя достоверно определить владельца работающей генераторной шины.")

    enabled_faults = [g for g in inputs.generators if g.enabled and g.fault]
    enabled_generators = [g for g in inputs.generators if g.enabled]
    if enabled_generators and len(enabled_faults) == len(enabled_generators):
        names = ", ".join(g.name for g in enabled_faults)
        red.append(f"Все разрешённые генераторы имеют неисправность: {names}.")
    else:
        for generator in enabled_faults:
            yellow.append(
                f"{generator.name}: {generator.fault or 'зафиксирована неисправность генератора'}."
            )

    if inputs.grid_input_state == GridInputState.PARTIAL:
        yellow.append("Входная сеть частично недоступна: отсутствует одна или несколько фаз.")
    elif inputs.grid_input_state is None:
        yellow.append("Трёхсостоянийный статус входной сети неизвестен.")

    if inputs.ups_run_enabled and inputs.ups_run_degraded:
        yellow.append(
            inputs.ups_run_reason
            or "UPS Run временно недоступен; обычный алгоритм АВР продолжает работу."
        )

    if inputs.load_manager_enabled and inputs.load_manager_degraded:
        yellow.append(
            inputs.load_manager_reason
            or "Автоматическое управление некритичными нагрузками временно недоступно."
        )

    yellow.extend(inputs.exercise_failures)

    if red:
        return HealthStatus(
            HealthLevel.RED,
            "АВР неработоспособен",
            tuple(_dedupe(red + yellow)),
        )
    if yellow:
        return HealthStatus(
            HealthLevel.YELLOW,
            "АВР работает, но требуется внимание",
            tuple(_dedupe(yellow)),
        )
    return HealthStatus(HealthLevel.GREEN, "Всё в порядке", ())


@dataclass(frozen=True)
class WeeklyExerciseSummary:
    week_key: str
    event: SupervisorEvent


def build_weekly_exercise_summary(
    *,
    local_now: datetime,
    exercise_attributes: Mapping[str, Any],
    generator_names: Mapping[GeneratorSlot, str],
    last_week_key: str | None,
) -> WeeklyExerciseSummary | None:
    """Сформировать одну MAIN-сводку за ISO-неделю после понедельника 09:00.

    Если App не работал в понедельник утром, сводка публикуется при первом
    последующем tick этой же недели. Ключ недели сохраняется App, поэтому restart
    не создаёт повторную запись.
    """

    iso = local_now.isocalendar()
    week_key = f"{iso.year}-W{iso.week:02d}"
    monday = local_now.date() - timedelta(days=local_now.weekday())
    publish_after = datetime.combine(monday, time(9, 0), tzinfo=local_now.tzinfo)
    if local_now < publish_after or last_week_key == week_key:
        return None

    any_enabled = any(
        bool(exercise_attributes.get(f"generator_{slot.value.lower()}_exercise_enabled"))
        for slot in GeneratorSlot
    )
    if not any_enabled:
        return None

    parts = [
        _generator_summary(
            slot,
            local_now,
            exercise_attributes,
            generator_names.get(slot, f"Generator {slot.value}"),
        )
        for slot in GeneratorSlot
    ]
    message = user_message("exercise_weekly_summary", summary=" ".join(parts))
    return WeeklyExerciseSummary(week_key, SupervisorEvent("info", message))


def _generator_summary(
    slot: GeneratorSlot,
    local_now: datetime,
    attrs: Mapping[str, Any],
    name: str,
) -> str:
    prefix = f"generator_{slot.value.lower()}_exercise"
    enabled = bool(attrs.get(f"{prefix}_enabled"))
    last_run = _parse_datetime(attrs.get(f"{prefix}_last_qualifying_run"))

    if last_run is None:
        last_text = "успешных запусков ещё не было"
    else:
        last_text = f"последний успешный запуск — {_format_date_ru(last_run)}"

    if not enabled:
        return f"{name}: {last_text}; автоматические пробные пуски отключены."

    next_due = _parse_datetime(attrs.get(f"{prefix}_next_due"))
    if next_due is None:
        return f"{name}: {last_text}; дата следующего пробного запуска пока не определена."

    delta_days = (next_due.date() - local_now.date()).days
    relative = _relative_due_text(delta_days)
    return (
        f"{name}: {last_text}; следующий пробный запуск — "
        f"{_format_date_ru(next_due)} ({relative})."
    )


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, "", "unknown", "unavailable"):
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


_MONTHS_RU = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def _format_date_ru(value: datetime) -> str:
    return f"{value.day} {_MONTHS_RU[value.month - 1]}"


def _relative_due_text(days: int) -> str:
    if days == 0:
        return "сегодня"
    if days == 1:
        return "завтра"
    if days < 0:
        overdue = -days
        return f"просрочен на {_duration_days_ru(overdue)}"
    if days < 7:
        return f"через {_duration_days_ru(days)}"

    weeks, remainder = divmod(days, 7)
    weeks_text = _plural_ru(weeks, "неделю", "недели", "недель")
    if remainder == 0:
        return f"через {weeks} {weeks_text}"
    return f"через {weeks} {weeks_text} {_duration_days_ru(remainder)}"


def _duration_days_ru(days: int) -> str:
    return f"{days} {_plural_ru(days, 'день', 'дня', 'дней')}"


def _plural_ru(value: int, one: str, few: str, many: str) -> str:
    mod100 = value % 100
    mod10 = value % 10
    if 11 <= mod100 <= 14:
        return many
    if mod10 == 1:
        return one
    if 2 <= mod10 <= 4:
        return few
    return many


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))

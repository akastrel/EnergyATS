"""Высокоуровневое представление состояния АВР для пользователя.

Модуль ничего не управляет и не участвует в safety/policy. Он только переводит
уже известные факты EnergyATS в два удобных представления:

* светофорный health: green / yellow / red;
* одну человекочитаемую сводку состояния генераторов в неделю.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import Enum
from typing import Any, Mapping

from domain import GeneratorSlot, GridInputState, SupervisorEvent


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
            "level": self.level.value,
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
    run_attributes: Mapping[str, Any] | None = None,
) -> WeeklyExerciseSummary | None:
    """Сформировать одну MAIN-сводку за ISO-неделю после понедельника 09:00.

    Если App не работал в понедельник утром, сводка публикуется при первом
    последующем tick этой же недели. Ключ недели сохраняется App, поэтому restart
    не создаёт повторную запись. При наличии общей истории запусков показывается
    последний фактический запуск; иначе используется прежний qualifying run.
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

    run_attrs = run_attributes or {}
    parts = [
        _generator_summary(
            slot,
            local_now,
            exercise_attributes,
            run_attrs,
            generator_names.get(slot, f"Generator {slot.value}"),
        )
        for slot in GeneratorSlot
    ]
    message = f"Плановые проверки генераторов. {' '.join(parts)}"
    return WeeklyExerciseSummary(week_key, SupervisorEvent("info", message))


def _generator_summary(
    slot: GeneratorSlot,
    local_now: datetime,
    attrs: Mapping[str, Any],
    run_attrs: Mapping[str, Any],
    name: str,
) -> str:
    prefix = f"generator_{slot.value.lower()}_exercise"
    run_prefix = f"generator_{slot.value.lower()}"
    enabled = bool(attrs.get(f"{prefix}_enabled"))

    actual_start = _parse_datetime(run_attrs.get(f"{run_prefix}_last_run_start"))
    duration = _parse_non_negative_int(
        run_attrs.get(f"{run_prefix}_last_run_duration_seconds")
    )
    run_type = run_attrs.get(f"{run_prefix}_last_run_type")
    run_result = run_attrs.get(f"{run_prefix}_last_run_result")

    if actual_start is not None:
        details = [_format_date_ru(actual_start)]
        if duration is not None:
            details.append(_duration_text(duration))
        type_text = _run_type_text(run_type)
        if type_text is not None:
            details.append(type_text)
        if run_result == "failed":
            details.append("с ошибкой")
        last_text = f"последний запуск — {', '.join(details)}"
    else:
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


def _parse_non_negative_int(value: Any) -> int | None:
    if value in (None, "", "unknown", "unavailable"):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _run_type_text(value: Any) -> str | None:
    return {
        "automatic": "автоматический",
        "manual": "ручной",
        "exercise": "пробный",
        "external": "внешний",
    }.get(str(value))


def _duration_text(seconds: int) -> str:
    hours, remainder = divmod(max(0, seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours} ч {minutes} мин" if minutes else f"{hours} ч"
    if minutes:
        return f"{minutes} мин {secs} с" if secs else f"{minutes} мин"
    return f"{secs} с"


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
        return f"просрочен на {_duration_days_ru(-days)}"
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

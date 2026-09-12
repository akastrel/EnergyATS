"""Локализация пользовательских сообщений EnergyATS.

Control logic использует только стабильные message keys. Готовый текст хранится
в отдельном language catalog (`user_messages_ru.py`); добавление другого языка
не требует менять Supervisor, UPS Run, Exercise или physical event tracker.
"""

from __future__ import annotations

from typing import Any

from domain import EventVisibility, SupervisorEvent
from user_messages_ru import RU_MESSAGES


DEFAULT_LANGUAGE = "ru"
_CATALOGS: dict[str, dict[str, str]] = {
    "ru": RU_MESSAGES,
}


def user_message(key: str, *, language: str = DEFAULT_LANGUAGE, **values: Any) -> str:
    """Вернуть локализованное пользовательское сообщение по стабильному ключу."""
    catalog = _CATALOGS.get(language, _CATALOGS[DEFAULT_LANGUAGE])
    try:
        template = catalog[key]
    except KeyError as exc:
        raise KeyError(f"Unknown EnergyATS user message key: {key}") from exc
    return template.format(**values)


def user_event(
    key: str,
    *,
    level: str = "info",
    visibility: EventVisibility = EventVisibility.MAIN,
    language: str = DEFAULT_LANGUAGE,
    **values: Any,
) -> SupervisorEvent:
    """Создать SupervisorEvent из локализованного пользовательского сообщения."""
    return SupervisorEvent(
        level=level,
        message=user_message(key, language=language, **values),
        visibility=visibility,
    )


def message_catalog(language: str = DEFAULT_LANGUAGE) -> dict[str, str]:
    """Копия каталога для тестов/документации; caller не может мутировать source."""
    catalog = _CATALOGS.get(language, _CATALOGS[DEFAULT_LANGUAGE])
    return dict(catalog)

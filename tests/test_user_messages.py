"""Пользовательский Logbook не должен требовать знания внутренних FSM-терминов."""

from user_messages import message_catalog, user_message


def test_manual_takeover_wording_explains_effect_without_session_jargon():
    message = user_message("manual_takeover_automatic")
    assert message == (
        "Пользователь запросил продолжить работу от генератора. "
        "Автоматическая остановка после подзарядки UPS отменена."
    )


def test_repeat_manual_start_explains_that_nothing_extra_is_needed():
    message = user_message("manual_start_already_active")
    assert message == (
        "Пользователь повторно запросил переход на резервное питание. "
        "Ручной режим уже активен; дополнительных действий не требуется."
    )


def test_charge_messages_use_human_term_instead_of_target_soc():
    rendered = user_message(
        "ups_target_charge_reached",
        soc=80.2,
        target=80.0,
    )
    assert "целевой уровень" in rendered
    assert "Target SoC" not in rendered
    assert "target_soc" not in rendered


def test_russian_catalog_contains_no_implementation_only_terms():
    catalog = message_catalog("ru")
    text = "\n".join(catalog.values()).lower()
    for forbidden in (
        "cycle_owned",
        "target_soc",
        "returning_to_ups",
        "returning_to_grid",
        "managed-сесс",
        "outage-сесс",
    ):
        assert forbidden not in text


def test_unknown_language_falls_back_to_russian_catalog():
    assert user_message("grid_input_off", language="xx") == user_message("grid_input_off")

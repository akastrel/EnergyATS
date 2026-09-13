"""Regression tests for the operator-facing messages documented in SETTINGS_RU.md."""

from domain import GeneratorSlot
from load_manager import LoadGroup, LoadManager, LoadManagerConfig, LoadManagerObservation
from user_messages import user_message


def test_agreed_critical_message_templates() -> None:
    """Согласованные C1/C2/C6 должны оставаться пользовательским контрактом."""
    assert user_message("recovery_required", reason="ошибка переключения") == (
        "АВР остановил автоматическое управление: ошибка переключения. "
        "Требуется внимание технического специалиста и сброс ошибки."
    )
    assert user_message(
        "generator_fallback",
        failed="Вепрь",
        reason="не подтверждён запуск",
        secondary="Elemax",
    ) == (
        "Генератор Вепрь не запустился: не подтверждён запуск. "
        "Запускаем резервный генератор Elemax."
    )
    assert user_message("ha_connection_lost_transition") == (
        "Потеряна связь с Home Assistant во время переключения. АВР не может "
        "однозначно определить состояние, поэтому переходит в режим «Требуется "
        "внимание технического специалиста»."
    )


def _overload_observation(now: float, sample: int) -> LoadManagerObservation:
    return LoadManagerObservation(
        now=now,
        house_on_generator=True,
        house_on_grid=False,
        desired_generator_supply=True,
        managed_generator_ready=True,
        power_transition_in_progress=False,
        bus_owner=GeneratorSlot.A,
        nominal_power=1000,
        maximum_power=1200,
        meter_ready=True,
        generator_power=1300,
        power_sample_id=sample,
        groups={LoadGroup.G1: False, LoadGroup.G2: False},
        generator_name="Elemax",
        actions_enabled=True,
    )


def test_maximum_overload_has_one_user_critical_and_warning_detail() -> None:
    """Максимальная перегрузка не должна давать второй CRITICAL из DETAIL-диагностики."""
    manager = LoadManager(
        LoadManagerConfig(
            enabled=True,
            measurement_stabilization_time=2,
            restore_margin_percent=15,
            nominal_overload_time=3,
            maximum_overload_confirmation_time=1,
            restore_retry_interval=5,
        )
    )

    for now, sample in ((0, 1), (1, 2), (2, 3)):
        manager.step(_overload_observation(now, sample))
    decision = manager.step(_overload_observation(3, 4))

    critical = [event for event in decision.events if event.level == "critical"]
    assert len(critical) == 1
    assert critical[0].message == (
        "Перегрузка генератора Elemax: нагрузка 1300 Вт. "
        "Все управляемые некритичные нагрузки уже отключены."
    )
    assert any(
        event.level == "warning" and "максимальный предел" in event.message
        for event in decision.events
    )

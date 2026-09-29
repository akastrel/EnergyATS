from __future__ import annotations

from datetime import datetime, timezone

from domain import GeneratorSlot, GridInputState
from operator_status import (
    GeneratorHealth,
    HealthInputs,
    HealthLevel,
    build_weekly_exercise_summary,
    evaluate_health,
)


def health_inputs(**overrides):
    values = {
        "armed": True,
        "automatic_transfer_enabled": True,
        "emergency_stop": False,
        "required_states_known": True,
        "recovery_required": False,
        "recovery_reason": None,
        "grid_input_state": GridInputState.NORMAL,
        "generators": (
            GeneratorHealth("Elemax", True, False, None),
            GeneratorHealth("Вепрь", True, False, None),
        ),
    }
    values.update(overrides)
    return HealthInputs(**values)


def test_health_is_green_when_core_is_operational():
    health = evaluate_health(health_inputs(grid_input_state=GridInputState.LOST))
    assert health.level == HealthLevel.GREEN
    assert health.summary == "Всё в порядке"
    assert health.reasons == ()


def test_partial_grid_is_yellow_not_red():
    health = evaluate_health(health_inputs(grid_input_state=GridInputState.PARTIAL))
    assert health.level == HealthLevel.YELLOW
    assert any("частично недоступна" in reason for reason in health.reasons)


def test_one_failed_generator_is_yellow_if_second_remains_available():
    health = evaluate_health(
        health_inputs(
            generators=(
                GeneratorHealth("Elemax", True, False, "не подтвердил RUNNING за 90 с"),
                GeneratorHealth("Вепрь", True, True, None),
            )
        )
    )
    assert health.level == HealthLevel.YELLOW
    assert any("Elemax" in reason for reason in health.reasons)


def test_all_enabled_generators_fault_is_red():
    health = evaluate_health(
        health_inputs(
            generators=(
                GeneratorHealth("Elemax", True, False, "fault A"),
                GeneratorHealth("Вепрь", True, False, "fault B"),
            )
        )
    )
    assert health.level == HealthLevel.RED
    assert any("Все разрешённые генераторы" in reason for reason in health.reasons)


def test_recovery_estop_disarmed_or_automatic_off_are_red():
    cases = (
        health_inputs(recovery_required=True, recovery_reason="не подтверждён Grid path"),
        health_inputs(emergency_stop=True),
        health_inputs(armed=False),
        health_inputs(automatic_transfer_enabled=False),
    )
    for item in cases:
        assert evaluate_health(item).level == HealthLevel.RED


def test_optional_degraded_components_are_yellow_only():
    health = evaluate_health(
        health_inputs(
            ups_run_enabled=True,
            ups_run_degraded=True,
            ups_run_reason="Некорректны настройки UPS Run.",
            load_manager_enabled=True,
            load_manager_degraded=True,
            load_manager_reason="Нет свежих данных счётчика.",
        )
    )
    assert health.level == HealthLevel.YELLOW
    assert "Некорректны настройки UPS Run." in health.reasons
    assert "Нет свежих данных счётчика." in health.reasons


def exercise_attrs():
    return {
        "generator_a_exercise_enabled": True,
        "generator_a_exercise_last_qualifying_run": "2026-09-05T15:10:00+03:00",
        "generator_a_exercise_next_due": "2026-10-05T15:10:00+03:00",
        "generator_b_exercise_enabled": True,
        "generator_b_exercise_last_qualifying_run": "2026-09-02T15:10:00+03:00",
        "generator_b_exercise_next_due": "2026-10-17T15:10:00+03:00",
    }


def test_weekly_summary_is_human_readable_and_once_per_iso_week():
    now = datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)  # Monday
    summary = build_weekly_exercise_summary(
        local_now=now,
        exercise_attributes=exercise_attrs(),
        generator_names={GeneratorSlot.A: "Elemax", GeneratorSlot.B: "Вепрь"},
        last_week_key=None,
    )
    assert summary is not None
    assert summary.week_key == "2026-W40"
    assert "Elemax: последний успешный запуск — 5 сентября" in summary.event.message
    assert "5 октября (через 1 неделю)" in summary.event.message
    assert "Вепрь: последний успешный запуск — 2 сентября" in summary.event.message
    assert "17 октября (через 2 недели 5 дней)" in summary.event.message

    duplicate = build_weekly_exercise_summary(
        local_now=now,
        exercise_attributes=exercise_attrs(),
        generator_names={GeneratorSlot.A: "Elemax", GeneratorSlot.B: "Вепрь"},
        last_week_key=summary.week_key,
    )
    assert duplicate is None


def test_weekly_summary_waits_until_monday_0900_but_catches_up_later_in_week():
    attrs = exercise_attrs()
    monday_early = datetime(2026, 9, 28, 8, 59, tzinfo=timezone.utc)
    assert build_weekly_exercise_summary(
        local_now=monday_early,
        exercise_attributes=attrs,
        generator_names={GeneratorSlot.A: "Elemax", GeneratorSlot.B: "Вепрь"},
        last_week_key=None,
    ) is None

    tuesday = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    assert build_weekly_exercise_summary(
        local_now=tuesday,
        exercise_attributes=attrs,
        generator_names={GeneratorSlot.A: "Elemax", GeneratorSlot.B: "Вепрь"},
        last_week_key=None,
    ) is not None


def test_weekly_summary_reports_overdue_and_disabled_generator_cleanly():
    attrs = exercise_attrs()
    attrs["generator_a_exercise_next_due"] = "2026-09-26T15:00:00+03:00"
    attrs["generator_b_exercise_enabled"] = False
    now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)

    summary = build_weekly_exercise_summary(
        local_now=now,
        exercise_attributes=attrs,
        generator_names={GeneratorSlot.A: "Elemax", GeneratorSlot.B: "Вепрь"},
        last_week_key=None,
    )
    assert summary is not None
    assert "просрочен на 3 дня" in summary.event.message
    assert "Вепрь" in summary.event.message
    assert "автоматические пробные пуски отключены" in summary.event.message


def test_no_weekly_summary_when_both_exercises_disabled():
    attrs = exercise_attrs()
    attrs["generator_a_exercise_enabled"] = False
    attrs["generator_b_exercise_enabled"] = False
    now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    assert build_weekly_exercise_summary(
        local_now=now,
        exercise_attributes=attrs,
        generator_names={GeneratorSlot.A: "Elemax", GeneratorSlot.B: "Вепрь"},
        last_week_key=None,
    ) is None

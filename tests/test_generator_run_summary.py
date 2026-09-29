from __future__ import annotations

from datetime import datetime, timezone

from domain import GeneratorSlot
from operator_status import build_weekly_exercise_summary


def test_weekly_summary_prefers_actual_run_history_over_exercise_fallback():
    exercise = {
        "generator_a_exercise_enabled": True,
        "generator_a_exercise_last_qualifying_run": "2026-09-05T15:10:00+03:00",
        "generator_a_exercise_next_due": "2026-10-29T14:32:00+03:00",
        "generator_b_exercise_enabled": True,
        "generator_b_exercise_last_qualifying_run": None,
        "generator_b_exercise_next_due": "2026-10-26T15:00:00+03:00",
    }
    runs = {
        "generator_a_last_run_start": "2026-09-29T14:32:00+03:00",
        "generator_a_last_run_duration_seconds": 46 * 60,
        "generator_a_last_run_type": "automatic",
        "generator_a_last_run_result": "success",
    }

    summary = build_weekly_exercise_summary(
        local_now=datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc),
        exercise_attributes=exercise,
        run_attributes=runs,
        generator_names={GeneratorSlot.A: "Elemax", GeneratorSlot.B: "Вепрь"},
        last_week_key=None,
    )

    assert summary is not None
    assert summary.event.message.startswith("Плановые проверки генераторов. ")
    assert "Elemax: последний запуск — 29 сентября, 46 мин, автоматический" in summary.event.message
    assert "следующий пробный запуск — 29 октября" in summary.event.message
    assert "Вепрь: успешных запусков ещё не было" in summary.event.message


def test_weekly_summary_marks_failed_actual_run():
    exercise = {
        "generator_a_exercise_enabled": True,
        "generator_a_exercise_last_qualifying_run": None,
        "generator_a_exercise_next_due": "2026-10-29T14:32:00+03:00",
        "generator_b_exercise_enabled": False,
        "generator_b_exercise_last_qualifying_run": None,
        "generator_b_exercise_next_due": None,
    }
    runs = {
        "generator_a_last_run_start": "2026-09-29T14:32:00+03:00",
        "generator_a_last_run_duration_seconds": 185,
        "generator_a_last_run_type": "manual",
        "generator_a_last_run_result": "failed",
    }

    summary = build_weekly_exercise_summary(
        local_now=datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc),
        exercise_attributes=exercise,
        run_attributes=runs,
        generator_names={GeneratorSlot.A: "Elemax", GeneratorSlot.B: "Вепрь"},
        last_week_key=None,
    )

    assert summary is not None
    assert "3 мин 5 с, ручной, с ошибкой" in summary.event.message

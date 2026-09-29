from __future__ import annotations

from datetime import datetime, timedelta, timezone

from domain import GeneratorSlot
from generator_run_history import (
    GeneratorRunHistory,
    GeneratorRunResult,
    GeneratorRunType,
)


def step(
    tracker: GeneratorRunHistory,
    when: datetime,
    *,
    a_running=False,
    b_running=False,
    a_fault=None,
    b_fault=None,
    a_type=GeneratorRunType.AUTOMATIC,
    b_type=GeneratorRunType.EXTERNAL,
):
    return tracker.step(
        now=when.timestamp(),
        local_now=when,
        running={GeneratorSlot.A: a_running, GeneratorSlot.B: b_running},
        faults={GeneratorSlot.A: a_fault, GeneratorSlot.B: b_fault},
        run_types={GeneratorSlot.A: a_type, GeneratorSlot.B: b_type},
        generator_names={GeneratorSlot.A: "Elemax", GeneratorSlot.B: "Вепрь"},
    )


def test_first_running_snapshot_is_baseline_not_false_start():
    tracker = GeneratorRunHistory()
    now = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)

    step(tracker, now, a_running=True)
    update = step(tracker, now + timedelta(minutes=5), a_running=False)

    assert tracker.stats[GeneratorSlot.A].total_starts == 0
    assert tracker.stats[GeneratorSlot.A].history == []
    assert update.events == ()


def test_completed_automatic_run_is_persisted_and_emits_summary_event():
    tracker = GeneratorRunHistory()
    start = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    step(tracker, start)
    step(tracker, start + timedelta(seconds=1), a_running=True)
    update = step(tracker, start + timedelta(minutes=46, seconds=1), a_running=False)

    stats = tracker.stats[GeneratorSlot.A]
    assert stats.total_starts == 1
    assert stats.total_runtime_seconds == 46 * 60
    assert stats.history[-1]["type"] == GeneratorRunType.AUTOMATIC.value
    assert stats.history[-1]["result"] == GeneratorRunResult.SUCCESS.value
    assert stats.history[-1]["duration_seconds"] == 46 * 60
    assert len(update.events) == 1
    assert "Elemax" in update.events[0].message
    assert "автоматический" in update.events[0].message
    assert "46 мин" in update.events[0].message


def test_run_type_is_captured_at_start_edge():
    tracker = GeneratorRunHistory()
    start = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    step(tracker, start)

    cases = (
        (GeneratorRunType.MANUAL, "manual"),
        (GeneratorRunType.EXERCISE, "exercise"),
        (GeneratorRunType.EXTERNAL, "external"),
    )
    cursor = start
    for run_type, expected in cases:
        cursor += timedelta(minutes=1)
        step(tracker, cursor, a_running=True, a_type=run_type)
        cursor += timedelta(minutes=1)
        step(tracker, cursor, a_running=False, a_type=run_type)
        assert tracker.stats[GeneratorSlot.A].history[-1]["type"] == expected


def test_fault_during_run_marks_completed_run_failed():
    tracker = GeneratorRunHistory()
    start = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    step(tracker, start)
    step(tracker, start + timedelta(seconds=1), a_running=True)
    step(
        tracker,
        start + timedelta(minutes=2),
        a_running=True,
        a_fault="oil pressure",
    )
    update = step(
        tracker,
        start + timedelta(minutes=3),
        a_running=False,
        a_fault="oil pressure",
    )

    record = tracker.stats[GeneratorSlot.A].history[-1]
    assert record["result"] == GeneratorRunResult.FAILED.value
    assert record["failure_reason"] == "oil pressure"
    assert update.events[0].level == "warning"
    assert "зафиксирована ошибка" in update.events[0].message


def test_observation_gap_discards_active_continuity_instead_of_inventing_runtime():
    tracker = GeneratorRunHistory()
    start = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    step(tracker, start)
    step(tracker, start + timedelta(seconds=1), a_running=True)

    tracker.invalidate_observation_history()
    step(tracker, start + timedelta(hours=1), a_running=True)
    update = step(tracker, start + timedelta(hours=2), a_running=False)

    assert tracker.stats[GeneratorSlot.A].total_starts == 1
    assert tracker.stats[GeneratorSlot.A].total_runtime_seconds == 0
    assert tracker.stats[GeneratorSlot.A].history == []
    assert update.events == ()


def test_persistence_restores_completed_history_but_not_active_continuity():
    tracker = GeneratorRunHistory()
    start = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    step(tracker, start)
    step(tracker, start + timedelta(seconds=1), a_running=True)
    step(tracker, start + timedelta(minutes=5, seconds=1), a_running=False)

    restored = GeneratorRunHistory.from_dict(tracker.to_dict())
    attrs = restored.status_attributes()

    assert attrs["generator_a_total_starts"] == 1
    assert attrs["generator_a_total_runtime_seconds"] == 300
    assert attrs["generator_a_last_run_type"] == "automatic"
    assert attrs["generator_a_last_run_result"] == "success"
    assert attrs["generator_a_last_run_start"] is not None
    assert attrs["generator_a_last_run_end"] is not None

    # A running first snapshot after restore is only a baseline.
    step(restored, start + timedelta(hours=1), a_running=True)
    step(restored, start + timedelta(hours=2), a_running=False)
    assert len(restored.stats[GeneratorSlot.A].history) == 1


def test_history_is_limited_to_100_records_but_aggregate_counters_continue():
    tracker = GeneratorRunHistory()
    cursor = datetime(2026, 1, 1, tzinfo=timezone.utc)
    step(tracker, cursor)

    for _ in range(105):
        cursor += timedelta(seconds=1)
        step(tracker, cursor, a_running=True)
        cursor += timedelta(seconds=1)
        step(tracker, cursor, a_running=False)

    stats = tracker.stats[GeneratorSlot.A]
    assert stats.total_starts == 105
    assert stats.total_runtime_seconds == 105
    assert len(stats.history) == 100

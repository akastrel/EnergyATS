from __future__ import annotations

from datetime import datetime, timedelta, timezone

from domain import GeneratorSlot
from generator_run_history import GeneratorRunHistory, GeneratorRunType


def step(
    tracker: GeneratorRunHistory,
    when: datetime,
    *,
    a_running: bool | None = False,
    a_fault: str | None = None,
):
    return tracker.step(
        now=when.timestamp(),
        local_now=when,
        running={GeneratorSlot.A: a_running, GeneratorSlot.B: False},
        faults={GeneratorSlot.A: a_fault, GeneratorSlot.B: None},
        run_types={
            GeneratorSlot.A: GeneratorRunType.AUTOMATIC,
            GeneratorSlot.B: GeneratorRunType.EXTERNAL,
        },
        generator_names={GeneratorSlot.A: "Elemax", GeneratorSlot.B: "Вепрь"},
        qualifying_seconds={GeneratorSlot.A: 600, GeneratorSlot.B: 600},
    )


def test_tracker_reports_qualifying_run_once_after_continuous_threshold():
    tracker = GeneratorRunHistory()
    start = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)

    step(tracker, start)
    step(tracker, start + timedelta(seconds=1), a_running=True)
    before = step(tracker, start + timedelta(minutes=10), a_running=True)
    qualified_at = start + timedelta(minutes=10, seconds=1)
    qualified = step(tracker, qualified_at, a_running=True)
    repeated = step(tracker, start + timedelta(minutes=11), a_running=True)

    assert before.qualifying_runs == {}
    assert qualified.qualifying_runs == {GeneratorSlot.A: qualified_at}
    assert repeated.qualifying_runs == {}


def test_fault_before_threshold_prevents_qualifying_run():
    tracker = GeneratorRunHistory()
    start = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)

    step(tracker, start)
    step(tracker, start + timedelta(seconds=1), a_running=True)
    step(
        tracker,
        start + timedelta(minutes=5),
        a_running=True,
        a_fault="oil pressure",
    )
    after_threshold = step(
        tracker,
        start + timedelta(minutes=15),
        a_running=True,
    )

    assert after_threshold.qualifying_runs == {}


def test_running_baseline_reproves_qualification_without_inventing_history():
    tracker = GeneratorRunHistory()
    baseline = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)

    # Simulates first valid observation after restart/reconnect while the engine
    # is already running. Its real start time is unknowable.
    step(tracker, baseline, a_running=True)
    before = step(
        tracker,
        baseline + timedelta(minutes=9, seconds=59),
        a_running=True,
    )
    qualified_at = baseline + timedelta(minutes=10)
    qualified = step(tracker, qualified_at, a_running=True)
    stopped = step(tracker, baseline + timedelta(minutes=11), a_running=False)

    assert before.qualifying_runs == {}
    assert qualified.qualifying_runs == {GeneratorSlot.A: qualified_at}
    assert stopped.events == ()
    assert tracker.stats[GeneratorSlot.A].total_starts == 0
    assert tracker.stats[GeneratorSlot.A].total_runtime_seconds == 0
    assert tracker.stats[GeneratorSlot.A].history == []

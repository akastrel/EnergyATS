from __future__ import annotations

from datetime import datetime, timedelta, timezone

from domain import GeneratorSlot
from exercise_scheduler import (
    ExerciseAttemptPhase,
    ExerciseConfig,
    ExerciseGeneratorObservation,
    ExerciseObservation,
    ExerciseResult,
    ExerciseScheduler,
)


def configs(*, a=True, b=True):
    return {
        GeneratorSlot.A: ExerciseConfig(a, 30, "15:00", 10, 7),
        GeneratorSlot.B: ExerciseConfig(b, 45, "15:00", 10, 14),
    }


def observation(
    local_now: datetime,
    *,
    grid=True,
    grid_path=True,
    present=False,
    busy=False,
    actions=True,
    emergency=False,
    known=True,
    transition=False,
    a_running=False,
    a_remote=False,
    a_fault=None,
    b_running=False,
    b_remote=False,
    b_fault=None,
):
    return ExerciseObservation(
        now=local_now.timestamp(),
        local_now=local_now,
        grid_ready=grid,
        grid_path_stable=grid_path,
        family_present=present,
        emergency_stop=emergency,
        required_states_known=known,
        power_transition_in_progress=transition,
        policy_busy=busy,
        actions_enabled=actions,
        generators={
            GeneratorSlot.A: ExerciseGeneratorObservation(
                a_running, a_remote, a_fault
            ),
            GeneratorSlot.B: ExerciseGeneratorObservation(
                b_running, b_remote, b_fault
            ),
        },
        generator_names={GeneratorSlot.A: "Elemax", GeneratorSlot.B: "Вепрь"},
    )


def test_new_slot_gets_initial_reference_and_no_immediate_start():
    scheduler = ExerciseScheduler(configs())
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)

    decision = scheduler.step(observation(now))

    assert decision.owned_slot is None
    attrs = scheduler.status_attributes(now, now.timestamp())
    assert attrs["generator_a_exercise_next_due"].startswith("2026-01-31")
    assert attrs["generator_b_exercise_next_due"].startswith("2026-02-15")


def test_due_generator_starts_only_in_its_daily_window_when_family_is_away():
    scheduler = ExerciseScheduler(configs(b=False))
    initial = datetime(2026, 1, 1, 14, 0, tzinfo=timezone.utc)
    scheduler.step(observation(initial))

    before_window = datetime(2026, 1, 31, 14, 59, tzinfo=timezone.utc)
    assert scheduler.step(observation(before_window)).owned_slot is None

    in_window = datetime(2026, 1, 31, 15, 0, tzinfo=timezone.utc)
    decision = scheduler.step(observation(in_window))
    assert decision.owned_slot == GeneratorSlot.A
    assert decision.desired_running is True


def test_presence_defers_without_creating_failed_result():
    scheduler = ExerciseScheduler(configs(b=False))
    scheduler.step(
        observation(datetime(2026, 1, 1, 14, 0, tzinfo=timezone.utc))
    )

    decision = scheduler.step(
        observation(
            datetime(2026, 1, 31, 15, 0, tzinfo=timezone.utc),
            present=True,
        )
    )

    assert decision.owned_slot is None
    assert scheduler.history[-1]["result"] == ExerciseResult.DEFERRED.value
    assert scheduler.states[GeneratorSlot.A].last_result is None


def test_presence_unknown_also_defers_normal_exercise():
    scheduler = ExerciseScheduler(configs(b=False))
    scheduler.step(
        observation(datetime(2026, 1, 1, 14, 0, tzinfo=timezone.utc))
    )
    decision = scheduler.step(
        observation(
            datetime(2026, 1, 31, 15, 0, tzinfo=timezone.utc),
            present=None,
        )
    )
    assert decision.owned_slot is None
    assert "не подтверждено" in scheduler.history[-1]["failure_reason"]


def test_forced_exercise_requires_successful_warning_from_previous_hour():
    scheduler = ExerciseScheduler(configs(b=False))
    scheduler.step(
        observation(datetime(2026, 1, 1, 14, 0, tzinfo=timezone.utc))
    )

    warning_time = datetime(2026, 2, 7, 14, 0, tzinfo=timezone.utc)
    warning_decision = scheduler.step(observation(warning_time, present=True))
    assert len(warning_decision.warnings) == 1

    scheduler.confirm_warning(
        GeneratorSlot.A,
        warning_decision.warnings[0].window_date,
        "Elemax",
    )
    start = warning_time + timedelta(hours=1)
    decision = scheduler.step(observation(start, present=True))
    assert decision.owned_slot == GeneratorSlot.A


def test_missed_forced_warning_prevents_start_and_does_not_catch_up():
    scheduler = ExerciseScheduler(configs(b=False))
    scheduler.step(
        observation(datetime(2026, 1, 1, 14, 0, tzinfo=timezone.utc))
    )

    forced_window = datetime(2026, 2, 7, 15, 0, tzinfo=timezone.utc)
    decision = scheduler.step(observation(forced_window, present=True))
    assert decision.owned_slot is None
    assert "предупреждения" in scheduler.history[-1]["failure_reason"]

    later = forced_window + timedelta(hours=2)
    assert scheduler.step(observation(later, present=True)).owned_slot is None


def test_two_conflicting_exercise_windows_do_not_start_together():
    scheduler = ExerciseScheduler(configs())
    initial = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    scheduler.step(observation(initial))
    # Сделать B due в тот же день, что и A.
    scheduler.states[GeneratorSlot.B].initial_reference_time = (
        initial - timedelta(days=15)
    ).isoformat()

    decision = scheduler.step(
        observation(datetime(2026, 1, 31, 15, 0, tzinfo=timezone.utc))
    )

    assert decision.owned_slot == GeneratorSlot.A
    assert any(
        item["generator"] == "B"
        and item["result"] == ExerciseResult.DEFERRED.value
        for item in scheduler.history
    )


def test_active_exercise_runs_configured_time_then_requires_shutdown():
    scheduler = ExerciseScheduler(configs(b=False))
    scheduler.step(
        observation(datetime(2026, 1, 1, 14, 0, tzinfo=timezone.utc))
    )
    start_window = datetime(2026, 1, 31, 15, 0, tzinfo=timezone.utc)
    scheduler.step(observation(start_window))

    running = start_window + timedelta(seconds=5)
    decision = scheduler.step(
        observation(running, a_running=True, a_remote=True)
    )
    assert scheduler.active_attempt.phase == ExerciseAttemptPhase.RUNNING
    assert decision.desired_running is True

    finished = running + timedelta(minutes=10)
    decision = scheduler.step(
        observation(finished, a_running=True, a_remote=True)
    )
    assert decision.authorized_shutdown_slot == GeneratorSlot.A
    assert decision.desired_running is False

    stopped = finished + timedelta(seconds=61)
    scheduler.step(observation(stopped, a_running=False, a_remote=False))
    assert scheduler.active_attempt is None
    assert scheduler.states[GeneratorSlot.A].last_result == ExerciseResult.SUCCESS.value


def test_exercise_failure_does_not_request_second_generator():
    scheduler = ExerciseScheduler(configs())
    scheduler.step(
        observation(datetime(2026, 1, 1, 14, 0, tzinfo=timezone.utc))
    )
    start_window = datetime(2026, 1, 31, 15, 0, tzinfo=timezone.utc)
    scheduler.step(observation(start_window))

    decision = scheduler.step(
        observation(
            start_window + timedelta(seconds=10),
            a_running=True,
            a_remote=True,
            a_fault="start fault",
        )
    )

    assert decision.owned_slot == GeneratorSlot.A
    assert decision.authorized_shutdown_slot == GeneratorSlot.A
    assert all("Вепрь" not in event.message for event in decision.events)
    assert any(event.level == "critical" for event in decision.events)


def test_handoff_to_outage_releases_scheduler_ownership():
    scheduler = ExerciseScheduler(configs(b=False))
    scheduler.step(
        observation(datetime(2026, 1, 1, 14, 0, tzinfo=timezone.utc))
    )
    start_window = datetime(2026, 1, 31, 15, 0, tzinfo=timezone.utc)
    scheduler.step(observation(start_window))
    running = start_window + timedelta(seconds=5)
    current = observation(running, a_running=True, a_remote=True, grid=False)
    scheduler.step(current)

    events = scheduler.handoff_to_outage(GeneratorSlot.A, current)

    assert events
    assert scheduler.active_attempt is None
    assert scheduler.history[-1]["result"] == ExerciseResult.INTERRUPTED_BY_OUTAGE.value


def test_qualifying_outage_run_updates_next_due_without_scheduled_exercise():
    scheduler = ExerciseScheduler(configs(b=False))
    initial = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    scheduler.step(observation(initial))

    started = datetime(2026, 1, 10, 8, 0, tzinfo=timezone.utc)
    scheduler.step(
        observation(started, grid=False, grid_path=False, a_running=True, a_remote=True)
    )
    qualified = started + timedelta(minutes=10)
    scheduler.step(
        observation(qualified, grid=False, grid_path=False, a_running=True, a_remote=True)
    )

    assert scheduler.states[GeneratorSlot.A].last_qualifying_run is not None
    attrs = scheduler.status_attributes(qualified, qualified.timestamp())
    assert attrs["generator_a_exercise_next_due"].startswith("2026-02-09")


def test_scheduler_state_restores_active_attempt_without_duplicate_schedule():
    scheduler = ExerciseScheduler(configs(b=False))
    scheduler.step(
        observation(datetime(2026, 1, 1, 14, 0, tzinfo=timezone.utc))
    )
    start_window = datetime(2026, 1, 31, 15, 0, tzinfo=timezone.utc)
    scheduler.step(observation(start_window))
    running = start_window + timedelta(seconds=5)
    scheduler.step(observation(running, a_running=True, a_remote=True))

    restored = ExerciseScheduler.from_dict(scheduler.to_dict(), configs(b=False))
    decision = restored.step(
        observation(
            running + timedelta(seconds=1),
            a_running=True,
            a_remote=True,
        )
    )

    assert decision.owned_slot == GeneratorSlot.A
    assert decision.desired_running is True
    assert restored.active_attempt.started_at == scheduler.active_attempt.started_at

"""Причинный пользовательский журнал должен объяснять, почему EnergyATS действует."""

from datetime import datetime, timedelta, timezone

from domain import GeneratorSlot, SessionReason
from exercise_scheduler import (
    ExerciseConfig,
    ExerciseGeneratorObservation,
    ExerciseObservation,
    ExerciseScheduler,
)
from ups_run import (
    BatteryObservation,
    UPSRun,
    UPSRunConfig,
    UPSRunObservation,
)
from user_messages import user_message


def _messages(events):
    return [event.message for event in events]


def _ups_config(**overrides):
    values = {
        "delayed_start_enabled": True,
        "charge_cycle_enabled": True,
        "start_soc": 40,
        "target_soc": 80,
        "min_ttg_before_start": 60,
        "max_start_delay": 3600,
        "telemetry_stale_time": 300,
    }
    values.update(overrides)
    return UPSRunConfig(**values)


def _battery(soc=70, ttg=180, *, discharging=True, ready=True, sample=1):
    return BatteryObservation(soc, ttg, discharging, ready, sample)


def _ups_obs(
    now,
    *,
    battery=None,
    active=False,
    on_generator=False,
    cycle_owned=False,
    manual_pending=False,
):
    return UPSRunObservation(
        now=now,
        grid_ready=False,
        automatic_transfer_enabled=True,
        core_delay_elapsed=True,
        battery=battery or _battery(),
        session_reason=SessionReason.GRID_OUTAGE if active else None,
        session_active=active,
        session_on_generator=on_generator,
        session_cycle_owned=cycle_owned,
        manual_start_pending=manual_pending,
    )


def test_ups_wait_explains_why_generator_is_not_started():
    ups = UPSRun(_ups_config())
    decision = ups.step(_ups_obs(10))

    assert decision.defer_automatic_start
    assert _messages(decision.events) == [user_message("ups_wait_started")]


def test_ups_soc_threshold_explains_why_generator_is_required():
    ups = UPSRun(_ups_config())
    decision = ups.step(_ups_obs(10, battery=_battery(39, 180)))

    assert decision.claim_new_outage_session
    assert user_message(
        "ups_start_soc_reached", soc=39, threshold=40
    ) in _messages(decision.events)


def test_ups_ttg_threshold_explains_why_generator_is_required():
    ups = UPSRun(_ups_config())
    decision = ups.step(_ups_obs(10, battery=_battery(70, 55)))

    assert decision.claim_new_outage_session
    assert user_message(
        "ups_start_ttg_reached", ttg=55, threshold=60
    ) in _messages(decision.events)


def test_ups_target_charge_message_uses_human_charge_language():
    ups = UPSRun(_ups_config())
    decision = ups.step(
        _ups_obs(
            100,
            battery=_battery(81, None, discharging=False),
            active=True,
            on_generator=True,
            cycle_owned=True,
        )
    )

    assert decision.request_cycle_stop
    message = user_message("ups_target_charge_reached", soc=81, target=80)
    assert message in _messages(decision.events)
    assert "Target SoC" not in " ".join(_messages(decision.events))


def test_manual_request_explains_why_ups_wait_was_cancelled():
    ups = UPSRun(_ups_config())
    decision = ups.step(_ups_obs(10, manual_pending=True))

    assert user_message("ups_wait_cancelled_manual") in _messages(decision.events)


def _exercise_configs():
    return {
        GeneratorSlot.A: ExerciseConfig(True, 30, "15:00", 10, 7),
        GeneratorSlot.B: ExerciseConfig(False, 45, "15:00", 10, 14),
    }


def _exercise_obs(local_now, *, present=False, running=False, remote=False):
    return ExerciseObservation(
        now=local_now.timestamp(),
        local_now=local_now,
        grid_ready=True,
        grid_path_stable=True,
        family_present=present,
        emergency_stop=False,
        required_states_known=True,
        power_transition_in_progress=False,
        policy_busy=False,
        actions_enabled=True,
        generators={
            GeneratorSlot.A: ExerciseGeneratorObservation(running, remote, None),
            GeneratorSlot.B: ExerciseGeneratorObservation(False, False, None),
        },
        generator_names={GeneratorSlot.A: "Elemax", GeneratorSlot.B: "Вепрь"},
    )


def test_exercise_due_is_logged_once_for_one_due_interval():
    scheduler = ExerciseScheduler(_exercise_configs())
    initial = datetime(2026, 1, 1, 14, 0, tzinfo=timezone.utc)
    scheduler.step(_exercise_obs(initial))

    due = initial + timedelta(days=30)
    first = scheduler.step(_exercise_obs(due))
    second = scheduler.step(_exercise_obs(due + timedelta(seconds=1)))

    expected = user_message("exercise_due", generator="Elemax", days=30)
    assert expected in _messages(first.events)
    assert expected not in _messages(second.events)


def test_exercise_presence_defer_explains_the_real_reason():
    scheduler = ExerciseScheduler(_exercise_configs())
    scheduler.step(_exercise_obs(datetime(2026, 1, 1, 14, 0, tzinfo=timezone.utc)))

    decision = scheduler.step(
        _exercise_obs(
            datetime(2026, 1, 31, 15, 0, tzinfo=timezone.utc),
            present=True,
        )
    )

    assert user_message(
        "exercise_deferred_presence", generator="Elemax"
    ) in _messages(decision.events)


def test_exercise_start_and_running_confirmation_are_separate_events():
    scheduler = ExerciseScheduler(_exercise_configs())
    scheduler.step(_exercise_obs(datetime(2026, 1, 1, 14, 0, tzinfo=timezone.utc)))
    scheduled = datetime(2026, 1, 31, 15, 0, tzinfo=timezone.utc)

    requested = scheduler.step(_exercise_obs(scheduled))
    confirmed = scheduler.step(
        _exercise_obs(
            scheduled + timedelta(seconds=5),
            running=True,
            remote=True,
        )
    )

    assert user_message("exercise_start_time", generator="Elemax") in _messages(
        requested.events
    )
    assert user_message(
        "exercise_started", generator="Elemax", minutes=10
    ) in _messages(confirmed.events)

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from domain import GeneratorSlot, SessionReason
from energy_supervisor import GeneratorSession, SupervisorObservation
from exercise_scheduler import ExerciseConfig, ExerciseScheduler
from generator_bus import GeneratorBusOwner, GeneratorBusStatus, GeneratorRunContext
from generator_controller import GeneratorPhase, GeneratorStatus
from generator_run_history import GeneratorRunHistory
from generator_run_monitor import GeneratorRunMonitor


def scheduler() -> ExerciseScheduler:
    return ExerciseScheduler(
        {
            GeneratorSlot.A: ExerciseConfig(True, 30, "15:00", 10, 7),
            GeneratorSlot.B: ExerciseConfig(True, 45, "15:00", 10, 14),
        }
    )


def monitor() -> tuple[GeneratorRunMonitor, ExerciseScheduler]:
    exercise = scheduler()
    item = GeneratorRunMonitor(
        history=GeneratorRunHistory(),
        exercise_scheduler=exercise,
        generator_name=lambda slot: "Elemax" if slot == GeneratorSlot.A else "Вепрь",
        logger=logging.getLogger("generator-run-test"),
    )
    return item, exercise


def observation(
    *,
    a_running: bool,
    a_fault: str | None = None,
    a_context: GeneratorRunContext = GeneratorRunContext.OTHER,
) -> SupervisorObservation:
    generators = {
        GeneratorSlot.A: GeneratorStatus(
            slot=GeneratorSlot.A,
            display_name="Elemax",
            phase=GeneratorPhase.IDLE,
            running=a_running,
            remote_on=False,
            ready_for_load=False,
            fault=a_fault,
        ),
        GeneratorSlot.B: GeneratorStatus(
            slot=GeneratorSlot.B,
            display_name="Вепрь",
            phase=GeneratorPhase.IDLE,
            running=False,
            remote_on=False,
            ready_for_load=False,
            fault=None,
        ),
    }
    return SupervisorObservation(
        grid_ready=True,
        automatic_transfer_enabled=True,
        emergency_stop=False,
        power=None,  # GeneratorRunMonitor does not inspect power-transfer state.
        generators=generators,
        bus=GeneratorBusStatus(
            GeneratorBusOwner.NONE,
            {
                GeneratorSlot.A: a_context,
                GeneratorSlot.B: GeneratorRunContext.NONE,
            },
        ),
    )


def observe(
    item: GeneratorRunMonitor,
    when: datetime,
    *,
    a_running: bool,
    session: GeneratorSession | None = None,
    a_fault: str | None = None,
    a_context: GeneratorRunContext = GeneratorRunContext.OTHER,
):
    return item.observe(
        now=when.timestamp(),
        local_now=when,
        observation=observation(
            a_running=a_running,
            a_fault=a_fault,
            a_context=a_context,
        ),
        session=session,
    )


@pytest.mark.parametrize(
    ("session", "context", "expected"),
    [
        (
            GeneratorSession.begin(
                SessionReason.GRID_OUTAGE,
                GeneratorSlot.A,
                grid_was_unavailable=True,
            ),
            GeneratorRunContext.OTHER,
            "automatic",
        ),
        (
            GeneratorSession.begin(
                SessionReason.MANUAL_GENERATOR_START,
                GeneratorSlot.A,
                grid_was_unavailable=False,
            ),
            GeneratorRunContext.OTHER,
            "manual",
        ),
        (None, GeneratorRunContext.TEST_RUN, "exercise"),
        (None, GeneratorRunContext.OTHER, "external"),
    ],
)
def test_monitor_classifies_completed_runs_from_existing_runtime_facts(
    session,
    context,
    expected,
):
    item, _ = monitor()
    start = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)

    observe(item, start, a_running=False, session=session, a_context=context)
    observe(
        item,
        start + timedelta(seconds=1),
        a_running=True,
        session=session,
        a_context=context,
    )
    observe(
        item,
        start + timedelta(minutes=12, seconds=1),
        a_running=False,
        session=session,
        a_context=GeneratorRunContext.NONE,
    )

    assert item.status_attributes()["generator_a_last_run_type"] == expected


def test_monitor_applies_qualifying_run_to_exercise_scheduler_once():
    item, exercise = monitor()
    start = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)

    observe(item, start, a_running=False)
    observe(item, start + timedelta(seconds=1), a_running=True)
    qualifying = start + timedelta(minutes=10, seconds=1)
    observe(item, qualifying, a_running=True)

    first = exercise.states[GeneratorSlot.A].last_qualifying_run
    assert first == qualifying.isoformat()

    observe(item, qualifying + timedelta(minutes=5), a_running=True)
    assert exercise.states[GeneratorSlot.A].last_qualifying_run == first


def test_monitor_does_not_qualify_run_that_faulted_before_threshold():
    item, exercise = monitor()
    start = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)

    observe(item, start, a_running=False)
    observe(item, start + timedelta(seconds=1), a_running=True)
    observe(
        item,
        start + timedelta(minutes=5),
        a_running=True,
        a_fault="fault",
    )
    observe(item, start + timedelta(minutes=11), a_running=True)

    assert exercise.states[GeneratorSlot.A].last_qualifying_run is None

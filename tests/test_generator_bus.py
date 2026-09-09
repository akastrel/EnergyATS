from domain import GeneratorSlot
from generator_bus import (
    GeneratorBusOwner,
    GeneratorBusTracker,
    GeneratorRunContext,
)


def _running(a: bool, b: bool):
    return {GeneratorSlot.A: a, GeneratorSlot.B: b}


def test_one_running_generator_is_bus_owner():
    tracker = GeneratorBusTracker()

    status = tracker.update(
        _running(True, False),
        grid_ready=True,
        test_mode=False,
        managed_slot=None,
        managed_outage=False,
    )

    assert status.owner == GeneratorBusOwner.A


def test_second_running_generator_does_not_take_bus():
    tracker = GeneratorBusTracker()
    tracker.update(
        _running(True, False),
        grid_ready=False,
        test_mode=False,
        managed_slot=GeneratorSlot.A,
        managed_outage=True,
    )

    status = tracker.update(
        _running(True, True),
        grid_ready=False,
        test_mode=False,
        managed_slot=GeneratorSlot.A,
        managed_outage=True,
    )

    assert status.owner == GeneratorBusOwner.A
    assert status.run_contexts[GeneratorSlot.B] == GeneratorRunContext.EXTERNAL_OUTAGE


def test_bus_moves_to_second_generator_after_first_stops():
    tracker = GeneratorBusTracker()
    tracker.update(
        _running(True, False),
        grid_ready=False,
        test_mode=False,
        managed_slot=GeneratorSlot.A,
        managed_outage=True,
    )
    tracker.update(
        _running(True, True),
        grid_ready=False,
        test_mode=False,
        managed_slot=GeneratorSlot.A,
        managed_outage=True,
    )

    status = tracker.update(
        _running(False, True),
        grid_ready=False,
        test_mode=False,
        managed_slot=GeneratorSlot.A,
        managed_outage=True,
    )

    assert status.owner == GeneratorBusOwner.B


def test_two_simultaneous_unknown_runs_do_not_guess_owner():
    tracker = GeneratorBusTracker()

    status = tracker.update(
        _running(True, True),
        grid_ready=False,
        test_mode=False,
        managed_slot=None,
        managed_outage=False,
    )

    assert status.owner == GeneratorBusOwner.UNKNOWN
    assert status.run_contexts[GeneratorSlot.A] == GeneratorRunContext.UNKNOWN_EXTERNAL
    assert status.run_contexts[GeneratorSlot.B] == GeneratorRunContext.UNKNOWN_EXTERNAL


def test_test_mode_marks_only_new_run_as_test():
    tracker = GeneratorBusTracker()
    tracker.update(
        _running(False, False),
        grid_ready=False,
        test_mode=False,
        managed_slot=None,
        managed_outage=False,
    )

    status = tracker.update(
        _running(True, False),
        grid_ready=False,
        test_mode=True,
        managed_slot=None,
        managed_outage=False,
    )
    assert status.run_contexts[GeneratorSlot.A] == GeneratorRunContext.TEST_RUN

    status = tracker.update(
        _running(True, False),
        grid_ready=False,
        test_mode=False,
        managed_slot=None,
        managed_outage=False,
    )
    assert status.run_contexts[GeneratorSlot.A] == GeneratorRunContext.TEST_RUN


def test_external_run_started_during_outage_is_outage_related():
    tracker = GeneratorBusTracker()
    tracker.update(
        _running(False, False),
        grid_ready=False,
        test_mode=False,
        managed_slot=None,
        managed_outage=False,
    )

    status = tracker.update(
        _running(False, True),
        grid_ready=False,
        test_mode=False,
        managed_slot=None,
        managed_outage=False,
    )

    assert status.run_contexts[GeneratorSlot.B] == GeneratorRunContext.EXTERNAL_OUTAGE
    assert status.outage_related_slots == frozenset({GeneratorSlot.B})


def test_state_round_trip_preserves_owner_and_context():
    tracker = GeneratorBusTracker()
    tracker.update(
        _running(False, False),
        grid_ready=False,
        test_mode=False,
        managed_slot=None,
        managed_outage=False,
    )
    tracker.update(
        _running(True, False),
        grid_ready=False,
        test_mode=False,
        managed_slot=GeneratorSlot.A,
        managed_outage=True,
    )
    tracker.update(
        _running(True, True),
        grid_ready=False,
        test_mode=False,
        managed_slot=GeneratorSlot.A,
        managed_outage=True,
    )

    restored = GeneratorBusTracker.from_dict(tracker.to_dict())
    status = restored.status()

    assert status.owner == GeneratorBusOwner.A
    assert status.run_contexts[GeneratorSlot.A] == GeneratorRunContext.MANAGED_OUTAGE
    assert status.run_contexts[GeneratorSlot.B] == GeneratorRunContext.EXTERNAL_OUTAGE


def test_owner_persisted_through_restart_when_both_still_running():
    tracker = GeneratorBusTracker()
    tracker.update(
        _running(True, False),
        grid_ready=False,
        test_mode=False,
        managed_slot=GeneratorSlot.A,
        managed_outage=True,
    )
    tracker.update(
        _running(True, True),
        grid_ready=False,
        test_mode=False,
        managed_slot=GeneratorSlot.A,
        managed_outage=True,
    )

    restored = GeneratorBusTracker.from_dict(tracker.to_dict())
    status = restored.update(
        _running(True, True),
        grid_ready=False,
        test_mode=False,
        managed_slot=GeneratorSlot.A,
        managed_outage=True,
    )

    assert status.owner == GeneratorBusOwner.A

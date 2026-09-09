from domain import GeneratorSlot
from generator_bus import (
    GeneratorBusOwner,
    GeneratorBusTracker,
    GeneratorRunContext,
)


def running(a: bool, b: bool):
    return {GeneratorSlot.A: a, GeneratorSlot.B: b}


def test_fifo_owner_and_automatic_handoff():
    tracker = GeneratorBusTracker()

    assert tracker.update(
        running(True, False), grid_ready=False, test_mode=False
    ).owner == GeneratorBusOwner.A
    assert tracker.update(
        running(True, True), grid_ready=False, test_mode=False
    ).owner == GeneratorBusOwner.A
    assert tracker.update(
        running(False, True), grid_ready=False, test_mode=False
    ).owner == GeneratorBusOwner.B


def test_two_unknown_running_after_cold_start_do_not_guess_owner():
    status = GeneratorBusTracker().update(
        running(True, True), grid_ready=False, test_mode=False
    )

    assert status.owner == GeneratorBusOwner.UNKNOWN
    assert status.run_contexts == {
        GeneratorSlot.A: GeneratorRunContext.UNKNOWN,
        GeneratorSlot.B: GeneratorRunContext.UNKNOWN,
    }


def test_new_run_context_is_classified_once():
    tracker = GeneratorBusTracker()
    tracker.update(running(False, False), grid_ready=False, test_mode=False)

    status = tracker.update(running(True, False), grid_ready=False, test_mode=True)
    assert status.run_contexts[GeneratorSlot.A] == GeneratorRunContext.TEST_RUN

    status = tracker.update(running(True, False), grid_ready=False, test_mode=False)
    assert status.run_contexts[GeneratorSlot.A] == GeneratorRunContext.TEST_RUN


def test_outage_run_is_marked_for_later_shutdown():
    tracker = GeneratorBusTracker()
    tracker.update(running(False, False), grid_ready=False, test_mode=False)
    status = tracker.update(running(False, True), grid_ready=False, test_mode=False)

    assert status.run_contexts[GeneratorSlot.B] == GeneratorRunContext.OUTAGE_RELATED
    assert status.outage_related_slots == frozenset({GeneratorSlot.B})


def test_restored_managed_outage_run_has_known_context():
    tracker = GeneratorBusTracker()
    status = tracker.update(
        running(True, False),
        grid_ready=False,
        test_mode=False,
        managed_slot=GeneratorSlot.A,
        managed_outage=True,
    )

    assert status.run_contexts[GeneratorSlot.A] == GeneratorRunContext.OUTAGE_RELATED


def test_state_round_trip_preserves_fifo_owner_and_contexts():
    tracker = GeneratorBusTracker()
    tracker.update(running(False, False), grid_ready=False, test_mode=False)
    tracker.update(running(True, False), grid_ready=False, test_mode=False)
    tracker.update(running(True, True), grid_ready=False, test_mode=False)

    restored = GeneratorBusTracker.from_dict(tracker.to_dict())
    status = restored.update(running(True, True), grid_ready=False, test_mode=False)

    assert status.owner == GeneratorBusOwner.A
    assert status.run_contexts[GeneratorSlot.A] == GeneratorRunContext.OUTAGE_RELATED
    assert status.run_contexts[GeneratorSlot.B] == GeneratorRunContext.OUTAGE_RELATED

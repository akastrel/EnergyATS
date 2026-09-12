"""Явные пользовательские команды должны оставлять понятный след в event journal."""

from energy_supervisor import EnergySupervisor
from user_messages import user_message


def event_messages(supervisor: EnergySupervisor) -> list[str]:
    return [event.message for event in supervisor.take_events()]


def test_manual_start_request_is_logged_as_user_trigger():
    supervisor = EnergySupervisor()
    supervisor.request_manual_start()

    assert event_messages(supervisor) == [user_message("manual_start_requested")]


def test_manual_stop_request_is_logged_as_user_trigger():
    supervisor = EnergySupervisor()
    supervisor.request_manual_stop()

    assert event_messages(supervisor) == [user_message("manual_stop_requested")]


def test_recovery_reset_request_is_logged_as_user_trigger():
    supervisor = EnergySupervisor()
    supervisor.request_recovery_reset()

    assert event_messages(supervisor) == [user_message("recovery_reset_requested")]

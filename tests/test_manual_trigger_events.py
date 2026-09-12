"""Явные пользовательские команды должны оставлять понятный след в event journal."""

from energy_supervisor import EnergySupervisor


def event_messages(supervisor: EnergySupervisor) -> list[str]:
    return [event.message for event in supervisor.take_events()]


def test_manual_start_request_is_logged_as_user_trigger():
    supervisor = EnergySupervisor()
    supervisor.request_manual_start()

    assert event_messages(supervisor) == [
        "Пользователь запросил ручной переход на резервное питание."
    ]


def test_manual_stop_request_is_logged_as_user_trigger():
    supervisor = EnergySupervisor()
    supervisor.request_manual_stop()

    assert event_messages(supervisor) == [
        "Пользователь запросил завершение управляемой генераторной сессии."
    ]


def test_recovery_reset_request_is_logged_as_user_trigger():
    supervisor = EnergySupervisor()
    supervisor.request_recovery_reset()

    assert event_messages(supervisor) == [
        "Пользователь запросил безопасный Recovery reset."
    ]

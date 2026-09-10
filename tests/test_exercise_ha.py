from __future__ import annotations

import pytest

from ha_adapter import HomeAssistantAdapter
from ha_client import HomeAssistantClient, HomeAssistantConnectionError


class FakeClient:
    def __init__(self, states=None, *, fail_service=False):
        self.states = dict(states or {})
        self.calls = []
        self.fail_service = fail_service

    def get_state(self, entity_id):
        return self.states.get(entity_id)

    def has_entity(self, entity_id):
        return entity_id in self.states

    async def call_service(self, domain, service, *, service_data=None):
        if self.fail_service:
            raise RuntimeError("delivery failed")
        self.calls.append((domain, service, service_data or {}))


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("home", True),
        ("on", True),
        ("not_home", False),
        ("off", False),
        ("unknown", None),
        ("unavailable", None),
        (None, None),
    ],
)
def test_presence_entity_supports_group_and_binary_sensor_states(state, expected):
    fake = FakeClient({"group.family": state} if state is not None else {})
    adapter = HomeAssistantAdapter(
        fake,
        armed=True,
        family_presence_entity="group.family",
    )
    assert adapter.presence_state("group.family") is expected


def test_presence_entity_never_blocks_core_ats_readiness():
    fake = FakeClient()
    adapter = HomeAssistantAdapter(
        fake,
        armed=True,
        family_presence_entity="group.family",
        require_family_presence=True,
    )

    # Presence is a soft scheduler input. If it is unavailable, ordinary
    # exercise is deferred by ExerciseScheduler, but the ATS itself must still
    # be allowed to start and handle a real Grid outage.
    assert "group.family" not in adapter.missing_required_entities(
        include_control_entities=False
    )


@pytest.mark.asyncio
async def test_forced_warning_delivery_uses_existing_notification_script():
    fake = FakeClient()
    adapter = HomeAssistantAdapter(fake, armed=True)

    delivered = await adapter.publish_user_notification("Пробный запуск в 15:00")

    assert delivered is True
    assert fake.calls == [
        (
            "script",
            "notify_critical",
            {"message": "Пробный запуск в 15:00"},
        )
    ]


@pytest.mark.asyncio
async def test_failed_warning_delivery_is_not_reported_as_success():
    adapter = HomeAssistantAdapter(
        FakeClient(fail_service=True),
        armed=True,
    )
    assert await adapter.publish_user_notification("test") is False


@pytest.mark.asyncio
async def test_disarmed_mode_never_sends_exercise_notification():
    fake = FakeClient()
    adapter = HomeAssistantAdapter(fake, armed=False)
    assert await adapter.publish_user_notification("test") is False
    assert fake.calls == []


@pytest.mark.asyncio
async def test_home_assistant_timezone_is_read_from_get_config(monkeypatch):
    client = HomeAssistantClient("test")

    async def request(command_type, **payload):
        assert command_type == "get_config"
        return {"success": True, "result": {"time_zone": "Europe/Moscow"}}

    monkeypatch.setattr(client, "request", request)
    assert await client.get_time_zone() == "Europe/Moscow"


@pytest.mark.asyncio
async def test_invalid_home_assistant_timezone_response_is_rejected(monkeypatch):
    client = HomeAssistantClient("test")

    async def request(command_type, **payload):
        return {"success": True, "result": {}}

    monkeypatch.setattr(client, "request", request)
    with pytest.raises(HomeAssistantConnectionError):
        await client.get_time_zone()

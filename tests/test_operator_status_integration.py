from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from domain import GeneratorSlot
from ha_adapter import (
    ENERGY_ATS_HEALTH_ENTITY,
    ENERGY_ATS_LOG_ENTITY,
    ENERGY_ATS_STATUS_ENTITY,
    ENTITIES,
    HomeAssistantAdapter,
)
from test_app_adapter import FakeClient, attach_fake_client, make_app, populated_states


@pytest.mark.asyncio
async def test_operator_sensors_share_publisher_and_reappear_after_ha_restart():
    fake = FakeClient()
    adapter = HomeAssistantAdapter(fake, armed=True)

    await adapter.publish_status("Готов", {"kind": "status"})
    await adapter.publish_health("green", {"summary": "Всё в порядке"})
    await asyncio.sleep(0)

    assert fake.states[ENERGY_ATS_STATUS_ENTITY] == "Готов"
    assert fake.states[ENERGY_ATS_HEALTH_ENTITY] == "green"

    first_writes = len(fake.state_writes)
    await adapter.cancel_background_publications()
    fake.states.pop(ENERGY_ATS_STATUS_ENTITY, None)
    fake.states.pop(ENERGY_ATS_HEALTH_ENTITY, None)

    # После restart HA Core REST-created entities исчезают. Тот же payload
    # обязан быть отправлен повторно, несмотря на отсутствие изменения данных.
    await adapter.publish_status("Готов", {"kind": "status"})
    await adapter.publish_health("green", {"summary": "Всё в порядке"})
    await asyncio.sleep(0)

    assert fake.states[ENERGY_ATS_STATUS_ENTITY] == "Готов"
    assert fake.states[ENERGY_ATS_HEALTH_ENTITY] == "green"
    assert len(fake.state_writes) >= first_writes + 2
    await adapter.cancel_background_publications()


@pytest.mark.asyncio
async def test_app_publishes_green_health_for_fully_operational_ats(tmp_path):
    app = make_app(tmp_path, armed=True)
    fake = FakeClient()
    fake.states = populated_states()
    fake.states[ENTITIES["automatic_transfer"]] = "on"
    attach_fake_client(app, fake)

    await app._tick(datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc).timestamp())
    await asyncio.sleep(0)

    assert fake.states[ENERGY_ATS_HEALTH_ENTITY] == "green"
    health_write = next(
        item for item in reversed(fake.state_writes) if item[0] == ENERGY_ATS_HEALTH_ENTITY
    )
    assert health_write[2]["summary"] == "Всё в порядке"
    assert health_write[2]["reasons"] == []
    await app.adapter.cancel_background_publications()


@pytest.mark.asyncio
async def test_partial_grid_is_yellow_in_published_health(tmp_path):
    app = make_app(tmp_path, armed=True)
    fake = FakeClient()
    fake.states = populated_states()
    fake.states[ENTITIES["automatic_transfer"]] = "on"
    fake.states[ENTITIES["grid_ready"]] = "off"
    fake.states[ENTITIES["grid_input_state"]] = "partial"
    attach_fake_client(app, fake)

    await app._tick(datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc).timestamp())
    await asyncio.sleep(0)

    assert fake.states[ENERGY_ATS_HEALTH_ENTITY] == "yellow"
    health_write = next(
        item for item in reversed(fake.state_writes) if item[0] == ENERGY_ATS_HEALTH_ENTITY
    )
    assert any("частично недоступна" in reason for reason in health_write[2]["reasons"])
    await app.adapter.cancel_background_publications()


@pytest.mark.asyncio
async def test_weekly_exercise_summary_is_not_duplicated_after_app_restart(tmp_path):
    options = {
        "armed": True,
        "generator_a_exercise_enabled": True,
        "generator_b_exercise_enabled": True,
    }
    now = datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)

    app = make_app(tmp_path, **options)
    fake = FakeClient()
    fake.states = populated_states()
    fake.states[ENTITIES["automatic_transfer"]] = "on"
    attach_fake_client(app, fake)
    await app._tick(now.timestamp())
    await asyncio.sleep(0)

    weekly_messages = [
        call[2]["message"]
        for call in fake.calls
        if call[0] == "logbook"
        and call[1] == "log"
        and call[2].get("entity_id") == ENERGY_ATS_LOG_ENTITY
        and call[2]["message"].startswith("Плановые проверки генераторов.")
    ]
    assert len(weekly_messages) == 1
    assert "Elemax" in weekly_messages[0]
    assert "Вепрь" in weekly_messages[0]
    await app.adapter.cancel_background_publications()

    restored = make_app(tmp_path, **options)
    second_fake = FakeClient()
    second_fake.states = populated_states()
    second_fake.states[ENTITIES["automatic_transfer"]] = "on"
    attach_fake_client(restored, second_fake)
    await restored._tick((now.replace(hour=10)).timestamp())
    await asyncio.sleep(0)

    duplicate = [
        call
        for call in second_fake.calls
        if call[0] == "logbook"
        and call[1] == "log"
        and call[2].get("entity_id") == ENERGY_ATS_LOG_ENTITY
        and call[2]["message"].startswith("Плановые проверки генераторов.")
    ]
    assert duplicate == []
    assert restored._last_weekly_exercise_summary == "2026-W40"
    await restored.adapter.cancel_background_publications()

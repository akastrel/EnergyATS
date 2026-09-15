from __future__ import annotations

import asyncio

import pytest

from ha_adapter import ENERGY_ATS_STATUS_ENTITY, HomeAssistantAdapter
from test_app_adapter import FakeClient


async def _drain_background_tasks() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_status_entity_is_recreated_after_home_assistant_restart() -> None:
    """REST-created status entity must be published again after HA transport loss.

    Home Assistant does not persist an entity created only through REST set_state.
    An unchanged EnergyATS status therefore still needs a new write after reconnect.
    """
    fake = FakeClient()
    adapter = HomeAssistantAdapter(fake, armed=True)
    payload = {"phase": "normal"}

    await adapter.publish_status("Grid", payload)
    await _drain_background_tasks()
    assert fake.states[ENERGY_ATS_STATUS_ENTITY] == "Grid"
    assert len(fake.state_writes) == 1

    # HA Core restart removes the synthetic REST-created entity, while the App
    # process and its in-memory status cache survive until transport reconnects.
    fake.states.pop(ENERGY_ATS_STATUS_ENTITY, None)
    await adapter.cancel_background_publications()
    fake.state_writes.clear()

    await adapter.publish_status("Grid", payload)
    await _drain_background_tasks()

    assert fake.states[ENERGY_ATS_STATUS_ENTITY] == "Grid"
    assert fake.state_writes == [
        (ENERGY_ATS_STATUS_ENTITY, "Grid", payload),
    ]
    await adapter.cancel_background_publications()

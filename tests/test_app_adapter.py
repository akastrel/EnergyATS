from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1] / "energy_ats" / "app"
sys.path.insert(0, str(APP_DIR))

from ats_core import Action, Config  # noqa: E402
from main import DEFAULT_OPTIONS, ENTITIES, EnergyATSApp, load_options  # noqa: E402


class FakeClient:
    def __init__(self):
        self.calls = []
        self.states = {}

    async def call_service(self, domain, service, *, service_data=None):
        self.calls.append((domain, service, service_data or {}))

    def get_state(self, entity_id):
        return self.states.get(entity_id)

    def has_entity(self, entity_id):
        return entity_id in self.states


def test_load_options_merges_defaults(tmp_path):
    p = tmp_path / "options.json"
    p.write_text(json.dumps({"armed": True, "grid_failure_delay": 7}), encoding="utf-8")
    options = load_options(p)
    assert options["armed"] is True
    assert options["grid_failure_delay"] == 7
    assert options["generator_start_timeout"] == DEFAULT_OPTIONS["generator_start_timeout"]


def test_app_options_map_to_core_config():
    app = EnergyATSApp(
        {
            **DEFAULT_OPTIONS,
            "grid_failure_delay": 8,
            "choke_hold_time": 12,
            "preheat_very_cold_seconds": 333,
        },
        token="test",
    )
    cfg = app._config_from_options()
    assert isinstance(cfg, Config)
    assert cfg.grid_failure_delay == 8
    assert cfg.choke_hold_time == 12
    assert cfg.preheat_very_cold_seconds == 333


@pytest.mark.asyncio
async def test_disarmed_never_executes_service_call():
    app = EnergyATSApp({**DEFAULT_OPTIONS, "armed": False}, token="test")
    fake = FakeClient()
    app.client = fake
    await app._execute_action(Action("switch_on", target="switch.generator_a_remote_start"))
    assert fake.calls == []


@pytest.mark.asyncio
async def test_terminal_action_mapping_preserves_declared_order():
    app = EnergyATSApp({**DEFAULT_OPTIONS, "armed": True}, token="test")
    fake = FakeClient()
    app.client = fake

    actions = [
        Action("switch_off", target="switch.generator_a_remote_start"),
        Action("switch_off", target="switch.generator_b_remote_start"),
        Action("switch_off", target="switch.switch_power_to_generator"),
        Action("switch_off", target="switch.disconnect_grid_power"),
        Action("switch_on", target="switch.generators_emergency_stop"),
    ]
    for action in actions:
        await app._execute_action(action)

    assert fake.calls == [
        ("switch", "turn_off", {"entity_id": "switch.generator_a_remote_start"}),
        ("switch", "turn_off", {"entity_id": "switch.generator_b_remote_start"}),
        ("switch", "turn_off", {"entity_id": "switch.switch_power_to_generator"}),
        ("switch", "turn_off", {"entity_id": "switch.disconnect_grid_power"}),
        ("switch", "turn_on", {"entity_id": "switch.generators_emergency_stop"}),
    ]


@pytest.mark.asyncio
async def test_logbook_action_always_has_entity_id():
    app = EnergyATSApp({**DEFAULT_OPTIONS, "armed": True}, token="test")
    fake = FakeClient()
    app.client = fake
    await app._execute_action(Action("log", message="Тест"))
    domain, service, data = fake.calls[0]
    assert (domain, service) == ("logbook", "log")
    assert data["entity_id"] == ENTITIES["ats_enabled"]

@pytest.mark.asyncio
async def test_home_assistant_websocket_client_roundtrip():
    """Мини-интеграционный тест транспорта: auth -> states -> subscribe -> event -> service."""
    import aiohttp.web
    from ha_client import HomeAssistantClient

    service_calls = []
    event_sent = asyncio.Event()

    async def websocket_handler(request):
        ws = aiohttp.web.WebSocketResponse()
        await ws.prepare(request)

        await ws.send_json({"type": "auth_required", "ha_version": "2026.8.2"})
        auth = await ws.receive_json()
        assert auth == {"type": "auth", "access_token": "token"}
        await ws.send_json({"type": "auth_ok", "ha_version": "2026.8.2"})

        async for msg in ws:
            data = msg.json()
            if data["type"] == "get_states":
                await ws.send_json({
                    "id": data["id"],
                    "type": "result",
                    "success": True,
                    "result": [
                        {"entity_id": "binary_sensor.grid_input_ready", "state": "on", "attributes": {}},
                    ],
                })
            elif data["type"] == "subscribe_events":
                await ws.send_json({"id": data["id"], "type": "result", "success": True, "result": None})
                await ws.send_json({
                    "id": data["id"],
                    "type": "event",
                    "event": {
                        "event_type": "state_changed",
                        "data": {
                            "entity_id": "binary_sensor.grid_input_ready",
                            "old_state": {"entity_id": "binary_sensor.grid_input_ready", "state": "on", "attributes": {}},
                            "new_state": {"entity_id": "binary_sensor.grid_input_ready", "state": "off", "attributes": {}},
                        },
                    },
                })
                event_sent.set()
            elif data["type"] == "call_service":
                service_calls.append(data)
                await ws.send_json({"id": data["id"], "type": "result", "success": True, "result": {}})

        return ws

    webapp = aiohttp.web.Application()
    webapp.router.add_get("/api/websocket", websocket_handler)
    runner = aiohttp.web.AppRunner(webapp)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    client = HomeAssistantClient("token", url=f"ws://127.0.0.1:{port}/api/websocket")
    seen = []

    async def listener(entity, old, new):
        seen.append((entity, old, new))

    client.add_state_listener("binary_sensor.grid_input_ready", listener)
    try:
        await client.connect()
        await asyncio.wait_for(event_sent.wait(), timeout=1)
        # Дадим reader-loop обработать только что отправленное event сообщение.
        for _ in range(20):
            if client.get_state("binary_sensor.grid_input_ready") == "off":
                break
            await asyncio.sleep(0.01)

        assert client.get_state("binary_sensor.grid_input_ready") == "off"
        assert seen == [("binary_sensor.grid_input_ready", "on", "off")]

        await client.call_service("switch", "turn_on", service_data={"entity_id": "switch.test"})
        assert service_calls[-1]["domain"] == "switch"
        assert service_calls[-1]["service"] == "turn_on"
        assert service_calls[-1]["service_data"] == {"entity_id": "switch.test"}
    finally:
        await client.close()
        await runner.cleanup()

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from aiohttp import web

sys.path.insert(0, "/app")

from ha_client import HomeAssistantClient
from main import DEFAULT_OPTIONS, EnergySupervisorApp


async def _websocket(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    await ws.send_json({"type": "auth_required"})

    auth = await ws.receive_json()
    assert auth["type"] == "auth"
    assert auth["access_token"] == "test-token"
    await ws.send_json({"type": "auth_ok"})

    async for message in ws:
        data = message.json()
        request_id = data["id"]
        command = data["type"]
        if command == "subscribe_events":
            result = None
        elif command == "get_states":
            result = []
        elif command == "get_config":
            result = {"time_zone": "UTC"}
        elif command == "call_service":
            result = None
        else:
            raise AssertionError(f"unexpected HA command: {command}")
        await ws.send_json(
            {
                "id": request_id,
                "type": "result",
                "success": True,
                "result": result,
            }
        )
    return ws


async def _set_state(request: web.Request) -> web.Response:
    assert request.headers["Authorization"] == "Bearer test-token"
    payload = await request.json()
    assert "state" in payload
    return web.json_response(payload)


async def smoke_home_assistant_client() -> None:
    server = web.Application()
    server.router.add_get("/api/websocket", _websocket)
    server.router.add_post("/api/states/{entity_id}", _set_state)

    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None
    port = site._server.sockets[0].getsockname()[1]

    client = HomeAssistantClient(
        "test-token",
        url=f"ws://127.0.0.1:{port}/api/websocket",
        api_url=f"http://127.0.0.1:{port}/api",
        request_timeout=2.0,
    )
    try:
        await client.connect()
        assert client.connected.is_set()
        assert await client.get_time_zone() == "UTC"
        await client.call_service(
            "switch",
            "turn_off",
            service_data={"entity_id": "switch.test"},
        )
        await client.set_state(
            "sensor.energy_ats_status",
            "container-smoke",
            attributes={"ok": True},
        )
    finally:
        await client.close()
        await runner.cleanup()


def smoke_stdin_command_and_stop() -> None:
    state_file = Path("/tmp/energy-ats-container-smoke.json")
    state_file.unlink(missing_ok=True)
    app = EnergySupervisorApp(
        {
            **DEFAULT_OPTIONS,
            "armed": True,
            "state_file": str(state_file),
        },
        token="test-token",
    )
    app.commands_ready = True
    called: list[str] = []
    app.supervisor.request_manual_start = lambda: called.append("start")  # type: ignore[method-assign]

    app.handle_stdin_line('{"command":"start_generator"}')
    assert called == ["start"]

    app.request_stop()
    assert app.stop_event.is_set()
    state_file.unlink(missing_ok=True)


async def main() -> None:
    await smoke_home_assistant_client()
    smoke_stdin_command_and_stop()
    print("EnergyATS production container smoke: OK")


if __name__ == "__main__":
    asyncio.run(main())

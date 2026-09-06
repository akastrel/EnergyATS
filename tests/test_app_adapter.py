from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1] / "energy_ats" / "app"
sys.path.insert(0, str(APP_DIR))

from domain import (  # noqa: E402
    GeneratorSlot,
    SupervisorEvent,
)
from energy_supervisor import SupervisorPhase  # noqa: E402
from generator_controller import (  # noqa: E402
    ChokeStrategy,
    GeneratorAction,
    GeneratorActionKind,
)
from ha_adapter import (  # noqa: E402
    ENERGY_ATS_LOG_ENTITY,
    ENTITIES,
    HomeAssistantAdapter,
    UnsafeHardwareCommand,
)
from ha_client import HomeAssistantClient  # noqa: E402
import main as app_main  # noqa: E402
from main import DEFAULT_OPTIONS, EnergySupervisorApp, load_options  # noqa: E402
from power_transfer import TransferAction, TransferActionKind  # noqa: E402
from state_store import StateStore  # noqa: E402
from app_test_support import (  # noqa: E402
    FakeClient,
    populated_states,
    saved_supervisor_payload,
)

def test_app_version_matches_addon_manifest():
    """Версия в журнале App не должна расходиться с версией HA App."""
    config_path = APP_DIR.parent / "config.yaml"
    version_line = next(
        line for line in config_path.read_text(encoding="utf-8").splitlines()
        if line.startswith("version:")
    )
    manifest_version = version_line.split(":", 1)[1].strip().strip('"')

    assert app_main.APP_VERSION == manifest_version

def test_load_options_merges_small_public_configuration(tmp_path):
    path = tmp_path / "options.json"
    path.write_text(
        json.dumps({"armed": True, "grid_failure_delay": 7}),
        encoding="utf-8",
    )
    options = load_options(path)
    assert options["armed"] is True
    assert options["grid_failure_delay"] == 7
    assert options["transfer_confirmation_timeout"] == 60

def test_stdin_commands_are_dispatched_without_ha_helpers(tmp_path, monkeypatch):
    app = EnergySupervisorApp(
        {
            **DEFAULT_OPTIONS,
            "armed": True,
            "state_file": str(tmp_path / "state.json"),
        },
        token="test",
    )
    app.commands_ready = True
    received: list[str] = []
    monkeypatch.setattr(
        app.supervisor,
        "request_manual_start",
        lambda: received.append("start_generator"),
    )
    monkeypatch.setattr(
        app.supervisor,
        "request_manual_stop",
        lambda: received.append("stop_generator"),
    )
    monkeypatch.setattr(
        app.supervisor,
        "request_recovery_reset",
        lambda: received.append("reset"),
    )

    app.handle_stdin_line('{"command":"start_generator"}')
    app.handle_stdin_line('{"command":"stop_generator"}')
    app.handle_stdin_line('{"command":"reset"}')

    assert received == ["start_generator", "stop_generator", "reset"]

def test_supervisor_events_are_written_to_app_log(tmp_path, caplog):
    app = EnergySupervisorApp(
        {**DEFAULT_OPTIONS, "state_file": str(tmp_path / "state.json")},
        token="test",
    )

    with caplog.at_level("INFO", logger="energy_supervisor"):
        app._log_events(
            (
                SupervisorEvent("info", "Информация"),
                SupervisorEvent("warning", "Предупреждение"),
                SupervisorEvent("critical", "Авария"),
            )
        )

    assert "INFO     energy_supervisor" in caplog.text
    assert "WARNING  energy_supervisor" in caplog.text
    assert "CRITICAL energy_supervisor" in caplog.text
    assert "ES:" not in caplog.text

@pytest.mark.asyncio
async def test_events_use_energy_ats_logbook_entity_and_only_critical_notifies():
    fake = FakeClient()
    adapter = HomeAssistantAdapter(fake, armed=True)

    await adapter.publish_events(
        (
            SupervisorEvent("info", "Информация"),
            SupervisorEvent("warning", "Предупреждение"),
            SupervisorEvent("critical", "Авария"),
        )
    )

    assert fake.calls == [
        (
            "logbook",
            "log",
            {
                "name": "Energy ATS",
                "message": "Информация",
                "entity_id": ENERGY_ATS_LOG_ENTITY,
            },
        ),
        (
            "logbook",
            "log",
            {
                "name": "Energy ATS",
                "message": "Предупреждение",
                "entity_id": ENERGY_ATS_LOG_ENTITY,
            },
        ),
        (
            "logbook",
            "log",
            {
                "name": "Energy ATS",
                "message": "Авария",
                "entity_id": ENERGY_ATS_LOG_ENTITY,
            },
        ),
        ("script", "notify_critical", {"message": "Авария"})
    ]

def test_disarmed_app_ignores_manual_stdin_commands(tmp_path):
    app = EnergySupervisorApp(
        {
            **DEFAULT_OPTIONS,
            "armed": False,
            "state_file": str(tmp_path / "state.json"),
        },
        token="test",
    )

    app.handle_stdin_line('{"command":"start_generator"}')

    assert app.supervisor._manual_start_requested is False

def test_manual_command_is_not_queued_before_app_is_ready(tmp_path):
    app = EnergySupervisorApp(
        {
            **DEFAULT_OPTIONS,
            "armed": True,
            "state_file": str(tmp_path / "state.json"),
        },
        token="test",
    )

    app.handle_stdin_line('{"command":"start_generator"}')

    assert app.supervisor._manual_start_requested is False

@pytest.mark.asyncio
@pytest.mark.skipif(
    os.name == "nt",
    reason="Асинхронный stdin App использует Linux pipe Home Assistant OS",
)
async def test_stdin_reader_accepts_home_assistant_json(tmp_path, monkeypatch):
    app = EnergySupervisorApp(
        {
            **DEFAULT_OPTIONS,
            "armed": True,
            "state_file": str(tmp_path / "state.json"),
        },
        token="test",
    )
    read_fd, write_fd = os.pipe()
    read_stream = os.fdopen(read_fd, "r", encoding="utf-8")
    monkeypatch.setattr(app_main.sys, "stdin", read_stream)
    app.commands_ready = True

    task = asyncio.create_task(app.read_stdin_commands())
    await asyncio.sleep(0)
    os.write(write_fd, b'{"command":"start_generator"}\n')
    os.close(write_fd)
    await asyncio.wait_for(task, timeout=1.0)

    assert app.supervisor._manual_start_requested is True

def test_string_false_can_never_arm_hardware(tmp_path):
    with pytest.raises(ValueError, match="armed.*JSON boolean"):
        EnergySupervisorApp(
            {
                **DEFAULT_OPTIONS,
                "armed": "false",
                "state_file": str(tmp_path / "state.json"),
            },
            token="test",
        )

def test_generator_specific_settings_live_in_generator_controller(tmp_path):
    app = EnergySupervisorApp(
        {**DEFAULT_OPTIONS, "state_file": str(tmp_path / "state.json")},
        token="test",
    )
    assert "generator_a_name" not in DEFAULT_OPTIONS
    assert "generator_a_choke_mode" not in DEFAULT_OPTIONS
    assert app.profiles[GeneratorSlot.A].display_name == "Elemax"
    assert app.profiles[GeneratorSlot.A].choke_strategy == ChokeStrategy.ALWAYS
    assert app.profiles[GeneratorSlot.B].display_name == "Вепрь"
    assert app.profiles[GeneratorSlot.B].choke_strategy == ChokeStrategy.ALWAYS
    assert app.profiles[GeneratorSlot.B].choke_temperature == 10.0
    assert app.profiles[GeneratorSlot.A].start_timeout_seconds == 90.0
    assert app.profiles[GeneratorSlot.A].stop_timeout_seconds == 90.0
    assert app.profiles[GeneratorSlot.A].cooldown_seconds == 60.0

def test_generator_names_are_mapped_to_internal_slots(tmp_path):
    app = EnergySupervisorApp(
        {
            **DEFAULT_OPTIONS,
            "primary_generator": "Вепрь",
            "state_file": str(tmp_path / "state.json"),
        },
        token="test",
    )

    assert app.supervisor.config.primary_generator == GeneratorSlot.B

def test_legacy_generator_slot_allows_in_place_update(tmp_path):
    app = EnergySupervisorApp(
        {
            **DEFAULT_OPTIONS,
            "primary_generator": "A",
            "state_file": str(tmp_path / "state.json"),
        },
        token="test",
    )

    assert app.supervisor.config.primary_generator == GeneratorSlot.A

def test_adapter_reads_positive_grid_switch_and_external_temperature():
    fake = FakeClient()
    fake.states = populated_states()
    adapter = HomeAssistantAdapter(fake, armed=True)

    snapshot = adapter.snapshot()
    assert snapshot.automatic_transfer_enabled is False
    assert snapshot.power_transfer.grid_connected is True
    assert snapshot.power_transfer.generator_selected is False
    assert (
        snapshot.generators[GeneratorSlot.A].ambient_temperature_external == 7.5
    )

    fake.states[ENTITIES["automatic_transfer"]] = "on"
    assert adapter.snapshot().automatic_transfer_enabled is True

def test_control_entities_are_required_only_in_armed_mode():
    fake = FakeClient()
    fake.states = populated_states()
    del fake.states[ENTITIES["generator_a_choke_cold_start"]]
    adapter = HomeAssistantAdapter(fake, armed=True)

    assert ENTITIES["generator_a_choke_cold_start"] in adapter.missing_required_entities()
    assert ENTITIES["generator_a_choke_cold_start"] not in adapter.missing_required_entities(
        include_control_entities=False
    )

def test_pending_hardware_command_restores_only_to_recovery(tmp_path):
    journal = tmp_path / "state.json"
    payload = saved_supervisor_payload(
        phase=SupervisorPhase.STARTING_GENERATOR,
        transaction_complete=False,
    )
    payload["pending_actions"] = [
        {
            "controller": "generator_controller",
            "generator": "A",
            "action": "remote_on",
        }
    ]
    StateStore(journal).save(payload)

    restored = EnergySupervisorApp(
        {**DEFAULT_OPTIONS, "state_file": str(journal)},
        token="test",
    )

    assert restored.supervisor.phase == SupervisorPhase.RECOVERY_REQUIRED

def test_unknown_envelope_schema_is_not_silently_loaded(tmp_path):
    journal = tmp_path / "state.json"
    payload = saved_supervisor_payload(
        phase=SupervisorPhase.ON_GENERATOR,
        transaction_complete=True,
    )
    payload["journal_schema_version"] = 999
    StateStore(journal).save(payload)

    restored = EnergySupervisorApp(
        {**DEFAULT_OPTIONS, "state_file": str(journal)},
        token="test",
    )

    assert restored.supervisor.phase == SupervisorPhase.RECOVERY_REQUIRED

@pytest.mark.asyncio
async def test_disarmed_adapter_never_calls_hardware():
    fake = FakeClient()
    adapter = HomeAssistantAdapter(fake, armed=False)
    await adapter.execute_actions(
        [TransferAction(TransferActionKind.DISCONNECT_GRID, "test")],
        [
            GeneratorAction(
                GeneratorSlot.A,
                GeneratorActionKind.REMOTE_ON,
                "test",
            )
        ],
    )
    assert fake.calls == []

@pytest.mark.asyncio
async def test_adapter_isolates_bus_before_stopping_engine():
    fake = FakeClient()
    fake.states = populated_states()
    adapter = HomeAssistantAdapter(fake, armed=True)
    await adapter.execute_actions(
        [TransferAction(TransferActionKind.DESELECT_GENERATOR, "isolate")],
        [
            GeneratorAction(
                GeneratorSlot.A,
                GeneratorActionKind.REMOTE_OFF,
                "stop",
            ),
            GeneratorAction(
                GeneratorSlot.A,
                GeneratorActionKind.CHOKE_TO_RUN,
                "open choke",
            ),
        ],
    )

    hardware_calls = [call for call in fake.calls if call[0] != "logbook"]
    assert hardware_calls == [
        (
            "switch",
            "turn_off",
            {"entity_id": ENTITIES["source_generator"]},
        ),
        (
            "switch",
            "turn_off",
            {"entity_id": ENTITIES["generator_a_remote"]},
        ),
        (
            "button",
            "press",
            {"entity_id": ENTITIES["generator_a_choke_run"]},
        ),
    ]

@pytest.mark.asyncio
async def test_adapter_refuses_to_start_second_generator():
    fake = FakeClient()
    fake.states = populated_states()
    fake.states[ENTITIES["generator_a_running"]] = "on"
    adapter = HomeAssistantAdapter(fake, armed=True)

    with pytest.raises(UnsafeHardwareCommand, match="второго генератора"):
        await adapter.execute_actions(
            [],
            [
                GeneratorAction(
                    GeneratorSlot.B,
                    GeneratorActionKind.REMOTE_ON,
                    "start B",
                )
            ],
        )
    assert fake.calls == []

@pytest.mark.asyncio
async def test_adapter_refuses_make_before_break():
    fake = FakeClient()
    fake.states = populated_states()
    adapter = HomeAssistantAdapter(fake, armed=True)

    with pytest.raises(UnsafeHardwareCommand, match="отключения Grid"):
        await adapter.execute_actions(
            [TransferAction(TransferActionKind.SELECT_GENERATOR, "select")],
            [],
        )
    assert fake.calls == []

@pytest.mark.asyncio
async def test_adapter_refuses_remote_off_while_generator_is_loaded():
    fake = FakeClient()
    fake.states = populated_states()
    fake.states[ENTITIES["house_grid"]] = "off"
    fake.states[ENTITIES["house_generator"]] = "on"
    fake.states[ENTITIES["generator_a_running"]] = "on"
    adapter = HomeAssistantAdapter(fake, armed=True)

    with pytest.raises(UnsafeHardwareCommand, match="дом ещё"):
        await adapter.execute_actions(
            [],
            [
                GeneratorAction(
                    GeneratorSlot.A,
                    GeneratorActionKind.REMOTE_OFF,
                    "stop A",
                )
            ],
        )
    assert fake.calls == []

@pytest.mark.asyncio
async def test_home_assistant_websocket_client_roundtrip():
    """Мини-интеграция транспорта: auth -> states -> event -> service."""
    import aiohttp.web

    service_calls = []
    event_sent = asyncio.Event()

    async def websocket_handler(request):
        ws = aiohttp.web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required", "ha_version": "2026.8.2"})
        auth = await ws.receive_json()
        assert auth == {"type": "auth", "access_token": "token"}
        await ws.send_json({"type": "auth_ok", "ha_version": "2026.8.2"})

        async for message in ws:
            data = message.json()
            if data["type"] == "get_states":
                await ws.send_json(
                    {
                        "id": data["id"],
                        "type": "result",
                        "success": True,
                        "result": [
                            {
                                "entity_id": ENTITIES["grid_ready"],
                                "state": "on",
                                "attributes": {},
                            }
                        ],
                    }
                )
            elif data["type"] == "subscribe_events":
                await ws.send_json(
                    {
                        "id": data["id"],
                        "type": "result",
                        "success": True,
                        "result": None,
                    }
                )
                await ws.send_json(
                    {
                        "id": data["id"],
                        "type": "event",
                        "event": {
                            "event_type": "state_changed",
                            "data": {
                                "entity_id": ENTITIES["grid_ready"],
                                "old_state": {"state": "on"},
                                "new_state": {"state": "off"},
                            },
                        },
                    }
                )
                event_sent.set()
            elif data["type"] == "call_service":
                service_calls.append(data)
                await ws.send_json(
                    {
                        "id": data["id"],
                        "type": "result",
                        "success": True,
                        "result": {},
                    }
                )
        return ws

    webapp = aiohttp.web.Application()
    webapp.router.add_get("/api/websocket", websocket_handler)
    runner = aiohttp.web.AppRunner(webapp)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    client = HomeAssistantClient(
        "token", url=f"ws://127.0.0.1:{port}/api/websocket"
    )
    seen = []

    async def listener(entity, old, new):
        seen.append((entity, old, new))

    client.add_state_listener(ENTITIES["grid_ready"], listener)
    try:
        await client.connect()
        await asyncio.wait_for(event_sent.wait(), timeout=1)
        for _ in range(20):
            if client.get_state(ENTITIES["grid_ready"]) == "off":
                break
            await asyncio.sleep(0.01)

        assert client.get_state(ENTITIES["grid_ready"]) == "off"
        assert seen == [(ENTITIES["grid_ready"], "on", "off")]

        await client.call_service(
            "switch", "turn_on", service_data={"entity_id": "switch.test"}
        )
        assert service_calls[-1]["service_data"] == {"entity_id": "switch.test"}
    finally:
        await client.close()
        await runner.cleanup()

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import aiohttp.web
import pytest

APP_DIR = Path(__file__).resolve().parents[1] / "energy_ats" / "app"
sys.path.insert(0, str(APP_DIR))

from domain import GeneratorSlot, SupervisorEvent  # noqa: E402
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
from load_manager import LoadAction, LoadActionKind, LoadGroup  # noqa: E402
import main as app_main  # noqa: E402
from main import DEFAULT_OPTIONS, EnergySupervisorApp, load_options  # noqa: E402
from power_transfer import TransferAction, TransferActionKind  # noqa: E402
from state_store import StateStore  # noqa: E402


def test_app_version_matches_addon_manifest():
    config_path = APP_DIR.parent / "config.yaml"
    version_line = next(
        line
        for line in config_path.read_text(encoding="utf-8").splitlines()
        if line.startswith("version:")
    )
    assert app_main.APP_VERSION == version_line.split(":", 1)[1].strip().strip('"')


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.states: dict[str, str] = {}
        self.state_writes: list[tuple[str, str, dict]] = []

    async def call_service(self, domain, service, *, service_data=None):
        self.calls.append((domain, service, service_data or {}))

    async def set_state(self, entity_id, state, *, attributes=None):
        self.states[entity_id] = state
        self.state_writes.append((entity_id, state, attributes or {}))

    def get_state(self, entity_id):
        return self.states.get(entity_id)

    def has_entity(self, entity_id):
        return entity_id in self.states


class PhysicalFakeClient(FakeClient):
    """Fake HA с минимальной обратной связью управляющих цепей контакторов."""

    def __init__(self, journal_path: Path | None = None) -> None:
        super().__init__()
        self.journal_path = journal_path
        self.pending_seen_before_hardware: list[list[dict[str, str]]] = []

    async def call_service(self, domain, service, *, service_data=None):
        data = service_data or {}
        entity_id = data.get("entity_id")
        if domain in {"switch", "button"} and self.journal_path is not None:
            journal = json.loads(self.journal_path.read_text(encoding="utf-8"))
            self.pending_seen_before_hardware.append(journal["pending_actions"])

        await super().call_service(domain, service, service_data=data)
        if domain != "switch" or not isinstance(entity_id, str):
            return

        self.states[entity_id] = "on" if service == "turn_on" else "off"
        if entity_id == ENTITIES["grid_power"] and service == "turn_off":
            self.states[ENTITIES["house_grid"]] = "off"
        elif entity_id == ENTITIES["grid_power"] and service == "turn_on":
            if self.states[ENTITIES["grid_ready"]] == "on":
                self.states[ENTITIES["house_grid"]] = "on"
        elif entity_id == ENTITIES["source_generator"] and service == "turn_on":
            self.states[ENTITIES["house_generator"]] = "on"
        elif entity_id == ENTITIES["source_generator"] and service == "turn_off":
            self.states[ENTITIES["house_generator"]] = "off"


def attach_fake_client(app: EnergySupervisorApp, fake: FakeClient) -> None:
    app.client = fake
    app.adapter.client = fake


def populated_states() -> dict[str, str]:
    return {
        ENTITIES["automatic_transfer"]: "off",
        ENTITIES["test_mode"]: "off",
        ENTITIES["grid_ready"]: "on",
        ENTITIES["house_grid"]: "on",
        ENTITIES["house_generator"]: "off",
        ENTITIES["generator_a_running"]: "off",
        ENTITIES["generator_b_running"]: "off",
        ENTITIES["generator_a_remote"]: "off",
        ENTITIES["generator_b_remote"]: "off",
        ENTITIES["generator_a_name"]: "Elemax",
        ENTITIES["generator_b_name"]: "Вепрь",
        ENTITIES["generator_a_model"]: "SH7600EX 6.5 / 5.6 кВт",
        ENTITIES["generator_b_model"]: "АПБ 6-230 ВХ-БСГ 6.0 / 5.5 кВт",
        ENTITIES["primary_generator"]: "Elemax",
        ENTITIES["emergency_stop"]: "off",
        ENTITIES["ambient_temperature_external"]: "7.5",
        ENTITIES["grid_power"]: "on",
        ENTITIES["source_generator"]: "off",
        ENTITIES["generator_a_choke_cold_start"]: "unknown",
        ENTITIES["generator_a_choke_run"]: "unknown",
        ENTITIES["generator_b_choke_cold_start"]: "unknown",
        ENTITIES["generator_b_choke_run"]: "unknown",
    }


def make_app(tmp_path: Path, **overrides) -> EnergySupervisorApp:
    return EnergySupervisorApp(
        {
            **DEFAULT_OPTIONS,
            "state_file": str(tmp_path / "state.json"),
            **overrides,
        },
        token="test",
    )


def test_load_options_merges_public_configuration(tmp_path):
    path = tmp_path / "options.json"
    path.write_text(json.dumps({"armed": True, "grid_failure_delay": 7}))
    options = load_options(path)
    assert options["armed"] is True
    assert options["grid_failure_delay"] == 7
    assert options["transfer_confirmation_timeout"] == 60
    assert "primary_generator" not in options


def test_load_options_migrates_max_start_delay_from_seconds_to_hours(tmp_path):
    path = tmp_path / "options.json"
    path.write_text(json.dumps({"generator_max_start_delay": 21600}))

    options = load_options(path)

    assert options["generator_max_start_delay_hours"] == 6
    assert "generator_max_start_delay" not in options


def test_max_start_delay_hours_is_converted_for_internal_timer(tmp_path):
    app = make_app(tmp_path, generator_max_start_delay_hours=2.5)
    assert app.ups_run.config.max_start_delay == 2.5 * 60 * 60


def test_stdin_commands_dispatch_only_when_armed_and_ready(tmp_path, monkeypatch):
    app = make_app(tmp_path, armed=True)
    received: list[str] = []
    monkeypatch.setattr(app.supervisor, "request_manual_start", lambda: received.append("start"))
    monkeypatch.setattr(app.supervisor, "request_manual_stop", lambda: received.append("stop"))
    monkeypatch.setattr(app.supervisor, "request_recovery_reset", lambda: received.append("reset"))

    app.handle_stdin_line('{"command":"start_generator"}')
    assert received == []

    app.commands_ready = True
    app.handle_stdin_line('{"command":"start_generator"}')
    app.handle_stdin_line('{"command":"stop_generator"}')
    app.handle_stdin_line('{"command":"reset"}')
    assert received == ["start", "stop", "reset"]

    disarmed = make_app(tmp_path, armed=False)
    disarmed.commands_ready = True
    disarmed.handle_stdin_line('{"command":"start_generator"}')
    assert disarmed.supervisor._manual_start_requested is False


@pytest.mark.asyncio
async def test_events_log_and_only_critical_notifies():
    fake = FakeClient()
    adapter = HomeAssistantAdapter(fake, armed=True)
    await adapter.publish_events(
        (
            SupervisorEvent("info", "Информация"),
            SupervisorEvent("warning", "Предупреждение"),
            SupervisorEvent("critical", "Авария"),
        )
    )
    assert [call[2]["message"] for call in fake.calls[:3]] == [
        "Информация",
        "Предупреждение",
        "Авария",
    ]
    assert all(call[2]["entity_id"] == ENERGY_ATS_LOG_ENTITY for call in fake.calls[:3])
    assert fake.calls[3] == ("script", "notify_critical", {"message": "Авария"})


def test_adapter_snapshot_reads_physical_states_metadata_primary_and_test_mode():
    fake = FakeClient()
    fake.states = populated_states()
    fake.states[ENTITIES["automatic_transfer"]] = "on"
    fake.states[ENTITIES["test_mode"]] = "on"
    adapter = HomeAssistantAdapter(fake, armed=True)

    snapshot = adapter.snapshot()
    assert snapshot.grid_ready is True
    assert snapshot.automatic_transfer_enabled is True
    assert snapshot.test_mode is True
    assert snapshot.primary_generator == GeneratorSlot.A
    assert snapshot.generator_metadata[GeneratorSlot.A].name == "Elemax"
    assert snapshot.generator_metadata[GeneratorSlot.B].name == "Вепрь"
    assert snapshot.power_transfer.generator_selected is False
    assert snapshot.generators[GeneratorSlot.A].load_connected is False


def test_generator_metadata_and_primary_update_runtime_configuration(tmp_path):
    app = make_app(tmp_path)
    fake = FakeClient()
    fake.states = populated_states()
    attach_fake_client(app, fake)

    app._sync_generator_configuration(app.adapter.snapshot())
    assert app.generator_controllers[GeneratorSlot.A].profile.display_name == "Elemax"
    assert app.generator_controllers[GeneratorSlot.B].profile.display_name == "Вепрь"
    assert app.supervisor.config.primary_generator == GeneratorSlot.A

    fake.states[ENTITIES["primary_generator"]] = "Вепрь"
    app._sync_generator_configuration(app.adapter.snapshot())
    assert app.supervisor.config.primary_generator == GeneratorSlot.B


def test_invalid_primary_generator_is_rejected(tmp_path):
    app = make_app(tmp_path)
    fake = FakeClient()
    fake.states = populated_states()
    fake.states[ENTITIES["primary_generator"]] = "Generator C"
    attach_fake_client(app, fake)
    with pytest.raises(ValueError, match="select.primary_generator"):
        app._sync_generator_configuration(app.adapter.snapshot())


def test_default_generator_profiles_are_bootstrap_only(tmp_path):
    app = make_app(tmp_path)
    a = app.generator_controllers[GeneratorSlot.A].profile
    b = app.generator_controllers[GeneratorSlot.B].profile
    assert a.display_name == "Generator A" and a.model == ""
    assert b.display_name == "Generator B" and b.model == ""
    assert a.choke_strategy == ChokeStrategy.ALWAYS
    assert b.choke_strategy == ChokeStrategy.ALWAYS


def test_missing_control_entity_required_only_when_armed():
    fake = FakeClient()
    fake.states = populated_states()
    del fake.states[ENTITIES["generator_a_choke_cold_start"]]
    adapter = HomeAssistantAdapter(fake, armed=True)
    assert ENTITIES["generator_a_choke_cold_start"] in adapter.missing_required_entities()
    assert ENTITIES["generator_a_choke_cold_start"] not in adapter.missing_required_entities(
        include_control_entities=False
    )


def generator_action(slot: GeneratorSlot, kind: GeneratorActionKind) -> GeneratorAction:
    return GeneratorAction(slot, kind, "test")


@pytest.mark.asyncio
async def test_hardware_action_messages_are_written_to_complete_app_log(caplog):
    fake = FakeClient()
    fake.states = populated_states()
    logger = logging.getLogger("test.hardware-action-log")
    adapter = HomeAssistantAdapter(fake, armed=True, logger=logger)

    transfer = TransferAction(
        TransferActionKind.DISCONNECT_GRID,
        "Отключаем сетевой ввод.",
    )
    generator = GeneratorAction(
        GeneratorSlot.A,
        GeneratorActionKind.REMOTE_ON,
        "Подаём REMOTE START на Elemax.",
    )
    load = LoadAction(
        LoadGroup.G1,
        LoadActionKind.TURN_OFF,
        "Отключаем некритичные нагрузки 1-го этажа.",
    )

    with caplog.at_level(logging.INFO, logger=logger.name):
        await adapter.execute_actions([transfer], [generator])
        await adapter.execute_load_actions([load])

    assert "Отключаем сетевой ввод." in caplog.messages
    assert "Подаём REMOTE START на Elemax." in caplog.messages
    assert "Отключаем некритичные нагрузки 1-го этажа." in caplog.messages


@pytest.mark.asyncio
async def test_remote_on_is_allowed_when_other_generator_is_running():
    fake = FakeClient()
    fake.states = populated_states()
    fake.states[ENTITIES["generator_b_running"]] = "on"
    adapter = HomeAssistantAdapter(fake, armed=True)
    await adapter.execute_actions([], [generator_action(GeneratorSlot.A, GeneratorActionKind.REMOTE_ON)])
    assert ("switch", "turn_on", {"entity_id": ENTITIES["generator_a_remote"]}) in fake.calls


@pytest.mark.asyncio
async def test_remote_on_is_blocked_by_emergency_stop():
    fake = FakeClient()
    fake.states = populated_states()
    fake.states[ENTITIES["emergency_stop"]] = "on"
    adapter = HomeAssistantAdapter(fake, armed=True)
    with pytest.raises(UnsafeHardwareCommand):
        await adapter.execute_actions([], [generator_action(GeneratorSlot.A, GeneratorActionKind.REMOTE_ON)])


@pytest.mark.asyncio
async def test_remote_off_live_generator_is_blocked_while_house_uses_generator_bus():
    fake = FakeClient()
    fake.states = populated_states()
    fake.states[ENTITIES["house_generator"]] = "on"
    fake.states[ENTITIES["generator_a_running"]] = "on"
    adapter = HomeAssistantAdapter(fake, armed=True)
    with pytest.raises(UnsafeHardwareCommand):
        await adapter.execute_actions([], [generator_action(GeneratorSlot.A, GeneratorActionKind.REMOTE_OFF)])


@pytest.mark.asyncio
async def test_remote_off_stopped_generator_is_allowed_when_other_generator_feeds_house():
    fake = FakeClient()
    fake.states = populated_states()
    fake.states[ENTITIES["house_generator"]] = "on"
    fake.states[ENTITIES["generator_a_running"]] = "off"
    fake.states[ENTITIES["generator_b_running"]] = "on"
    adapter = HomeAssistantAdapter(fake, armed=True)
    await adapter.execute_actions([], [generator_action(GeneratorSlot.A, GeneratorActionKind.REMOTE_OFF)])
    assert ("switch", "turn_off", {"entity_id": ENTITIES["generator_a_remote"]}) in fake.calls


@pytest.mark.asyncio
async def test_transfer_guards_enforce_break_before_make():
    fake = FakeClient()
    fake.states = populated_states()
    adapter = HomeAssistantAdapter(fake, armed=True)

    select = TransferAction(TransferActionKind.SELECT_GENERATOR, "select")
    with pytest.raises(UnsafeHardwareCommand):
        await adapter.execute_actions([select], [])

    fake.states[ENTITIES["grid_power"]] = "off"
    fake.states[ENTITIES["house_grid"]] = "off"
    fake.states[ENTITIES["source_generator"]] = "on"
    fake.states[ENTITIES["house_generator"]] = "on"
    connect_grid = TransferAction(TransferActionKind.CONNECT_GRID, "grid")
    with pytest.raises(UnsafeHardwareCommand):
        await adapter.execute_actions([connect_grid], [])


def test_unsupported_top_level_state_schema_requires_recovery(tmp_path):
    journal = tmp_path / "state.json"
    StateStore(journal).save({"schema_version": 999})
    app = make_app(tmp_path)
    assert app.supervisor.phase.value == "recovery_required"


def test_pending_hardware_action_requires_recovery_after_restart(tmp_path):
    app = make_app(tmp_path)
    app._pending_action_records = [
        {"controller": "power_transfer", "action": "disconnect_grid"}
    ]
    app._save_state(force=True)

    restored = make_app(tmp_path)
    assert restored.supervisor.phase.value == "recovery_required"


@pytest.mark.asyncio
async def test_write_ahead_journal_is_saved_before_hardware_command(tmp_path):
    app = make_app(tmp_path, armed=True)
    fake = PhysicalFakeClient(tmp_path / "state.json")
    fake.states = populated_states()
    attach_fake_client(app, fake)

    action = TransferAction(TransferActionKind.DISCONNECT_GRID, "grid off")
    await app._execute_controller_actions([action], [])

    assert fake.pending_seen_before_hardware
    assert fake.pending_seen_before_hardware[0] == [
        {"controller": "power_transfer", "action": "disconnect_grid"}
    ]
    saved = StateStore(tmp_path / "state.json").load()
    assert saved["pending_actions"] == []


@pytest.mark.asyncio
async def test_home_assistant_websocket_updates_cache_and_calls_service(unused_tcp_port):
    service_calls: list[dict] = []
    event_sent = asyncio.Event()

    async def websocket_handler(request):
        ws = aiohttp.web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required", "ha_version": "2026.9"})
        assert await ws.receive_json() == {"type": "auth", "access_token": "token"}
        await ws.send_json({"type": "auth_ok", "ha_version": "2026.9"})

        async for message in ws:
            data = message.json()
            if data["type"] == "subscribe_events":
                await ws.send_json({"id": data["id"], "type": "result", "success": True, "result": None})
                await ws.send_json(
                    {
                        "id": data["id"],
                        "type": "event",
                        "event": {
                            "event_type": "state_changed",
                            "data": {
                                "entity_id": ENTITIES["grid_ready"],
                                "old_state": {"state": "on"},
                                "new_state": {"entity_id": ENTITIES["grid_ready"], "state": "off", "attributes": {}},
                            },
                        },
                    }
                )
                event_sent.set()
            elif data["type"] == "get_states":
                await ws.send_json(
                    {
                        "id": data["id"],
                        "type": "result",
                        "success": True,
                        "result": [{"entity_id": ENTITIES["grid_ready"], "state": "on", "attributes": {}}],
                    }
                )
            elif data["type"] == "call_service":
                service_calls.append(data)
                await ws.send_json({"id": data["id"], "type": "result", "success": True, "result": {}})
        return ws

    webapp = aiohttp.web.Application()
    webapp.router.add_get("/api/websocket", websocket_handler)
    runner = aiohttp.web.AppRunner(webapp)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "127.0.0.1", unused_tcp_port)
    await site.start()

    client = HomeAssistantClient("token", url=f"ws://127.0.0.1:{unused_tcp_port}/api/websocket")
    try:
        await client.connect()
        await asyncio.wait_for(event_sent.wait(), timeout=1)
        assert client.get_state(ENTITIES["grid_ready"]) == "off"
        await client.call_service("switch", "turn_on", service_data={"entity_id": "switch.test"})
        assert service_calls[-1]["service_data"] == {"entity_id": "switch.test"}
    finally:
        await client.close()
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="HA OS uses Linux stdin pipe")
async def test_stdin_reader_accepts_home_assistant_json(tmp_path, monkeypatch):
    app = make_app(tmp_path, armed=True)
    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, "r", encoding="utf-8")
    monkeypatch.setattr(app_main.sys, "stdin", stream)
    app.commands_ready = True

    task = asyncio.create_task(app.read_stdin_commands())
    await asyncio.sleep(0)
    os.write(write_fd, b'{"command":"start_generator"}\n')
    os.close(write_fd)
    await asyncio.wait_for(task, timeout=1.0)
    assert app.supervisor._manual_start_requested is True

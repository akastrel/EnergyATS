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
    PowerSource,
    SessionReason,
    Transaction,
)
from energy_supervisor import (  # noqa: E402
    EnergySupervisor,
    GeneratorSession,
    SupervisorPhase,
)
from generator_controller import (  # noqa: E402
    ChokeStrategy,
    GeneratorAction,
    GeneratorActionKind,
    GeneratorPhase,
)
from ha_adapter import (  # noqa: E402
    ENTITIES,
    HomeAssistantAdapter,
    UnsafeHardwareCommand,
)
from ha_client import HomeAssistantClient  # noqa: E402
import main as app_main  # noqa: E402
from main import DEFAULT_OPTIONS, EnergySupervisorApp, load_options  # noqa: E402
from power_transfer import TransferAction, TransferActionKind  # noqa: E402
from state_store import StateStore  # noqa: E402


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.states: dict[str, str] = {}

    async def call_service(self, domain, service, *, service_data=None):
        self.calls.append((domain, service, service_data or {}))

    def get_state(self, entity_id):
        return self.states.get(entity_id)

    def has_entity(self, entity_id):
        return entity_id in self.states


class PhysicalFakeClient(FakeClient):
    """Минимальная физическая обратная связь для сквозного теста App."""

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
        ENTITIES["grid_ready"]: "on",
        ENTITIES["house_grid"]: "on",
        ENTITIES["house_generator"]: "off",
        ENTITIES["generator_a_running"]: "off",
        ENTITIES["generator_b_running"]: "off",
        ENTITIES["generator_a_remote"]: "off",
        ENTITIES["generator_b_remote"]: "off",
        ENTITIES["emergency_stop"]: "off",
        ENTITIES["ambient_temperature_external"]: "7.5",
        ENTITIES["grid_power"]: "on",
        ENTITIES["source_generator"]: "off",
        ENTITIES["generator_a_choke_cold_start"]: "unknown",
        ENTITIES["generator_a_choke_run"]: "unknown",
        ENTITIES["generator_b_choke_cold_start"]: "unknown",
        ENTITIES["generator_b_choke_run"]: "unknown",
    }


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


def test_automatic_transfer_is_off_by_default(tmp_path):
    app = EnergySupervisorApp(
        {
            **DEFAULT_OPTIONS,
            "state_file": str(tmp_path / "state.json"),
        },
        token="test",
    )

    assert app.automatic_transfer_enabled is False


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
        lambda: received.append("start_backup"),
    )
    monkeypatch.setattr(
        app.supervisor,
        "request_manual_stop",
        lambda: received.append("stop_generator"),
    )
    monkeypatch.setattr(
        app.supervisor,
        "request_recovery_reset",
        lambda: received.append("reset_recovery"),
    )

    app.handle_stdin_line('{"command":"start_backup"}')
    app.handle_stdin_line('{"command":"stop_generator"}')
    app.handle_stdin_line('{"command":"reset_recovery"}')

    assert received == ["start_backup", "stop_generator", "reset_recovery"]


def test_automatic_transfer_command_is_persistent(tmp_path):
    journal = tmp_path / "state.json"
    options = {
        **DEFAULT_OPTIONS,
        "state_file": str(journal),
    }
    app = EnergySupervisorApp(options, token="test")

    app.handle_stdin_line('{"command":"automatic_transfer_on"}')
    assert app.automatic_transfer_enabled is True
    assert json.loads(journal.read_text(encoding="utf-8"))[
        "automatic_transfer_enabled"
    ] is True

    restored = EnergySupervisorApp(options, token="test")
    assert restored.automatic_transfer_enabled is True

    restored.handle_stdin_line('{"command":"automatic_transfer_off"}')
    assert restored.automatic_transfer_enabled is False


def test_disarmed_app_ignores_manual_stdin_commands(tmp_path):
    app = EnergySupervisorApp(
        {
            **DEFAULT_OPTIONS,
            "armed": False,
            "state_file": str(tmp_path / "state.json"),
        },
        token="test",
    )

    app.handle_stdin_line('{"command":"start_backup"}')

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

    app.handle_stdin_line('{"command":"start_backup"}')

    assert app.supervisor._manual_start_requested is False


@pytest.mark.asyncio
async def test_stdin_reader_accepts_home_assistant_json(tmp_path, monkeypatch):
    app = EnergySupervisorApp(
        {
            **DEFAULT_OPTIONS,
            "state_file": str(tmp_path / "state.json"),
        },
        token="test",
    )
    read_fd, write_fd = os.pipe()
    read_stream = os.fdopen(read_fd, "r", encoding="utf-8")
    monkeypatch.setattr(app_main.sys, "stdin", read_stream)

    task = asyncio.create_task(app.read_stdin_commands())
    await asyncio.sleep(0)
    os.write(write_fd, b'{"command":"automatic_transfer_on"}\n')
    os.close(write_fd)
    await asyncio.wait_for(task, timeout=1.0)

    assert app.automatic_transfer_enabled is True


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
    assert app.profiles[GeneratorSlot.A].cooldown_seconds == 300.0


def test_adapter_reads_positive_grid_switch_and_external_temperature():
    fake = FakeClient()
    fake.states = populated_states()
    adapter = HomeAssistantAdapter(fake, armed=True)

    snapshot = adapter.snapshot()
    assert snapshot.power_transfer.grid_connected is True
    assert snapshot.power_transfer.generator_selected is False
    assert (
        snapshot.generators[GeneratorSlot.A].ambient_temperature_external == 7.5
    )


def test_control_entities_are_required_only_in_armed_mode():
    fake = FakeClient()
    fake.states = populated_states()
    del fake.states[ENTITIES["generator_a_choke_cold_start"]]
    adapter = HomeAssistantAdapter(fake, armed=True)

    assert ENTITIES["generator_a_choke_cold_start"] in adapter.missing_required_entities()
    assert ENTITIES["generator_a_choke_cold_start"] not in adapter.missing_required_entities(
        include_control_entities=False
    )


def saved_supervisor_payload(
    *,
    phase: SupervisorPhase,
    transaction_complete: bool,
) -> dict:
    supervisor = EnergySupervisor()
    supervisor.phase = phase
    supervisor.session = GeneratorSession.begin(
        reason=SessionReason.MANUAL_BACKUP,
        generator=GeneratorSlot.A,
        now=1.0,
        grid_was_unavailable=False,
    )
    supervisor.desired_source = PowerSource.GENERATOR_A
    supervisor.desired_generators[GeneratorSlot.A] = True
    supervisor.transaction = Transaction.begin(
        "enter_generator",
        "A",
        1.0,
        "transfer_to_generator",
    )
    if transaction_complete:
        supervisor.transaction.complete(2.0, "stable")
    return {
        "journal_schema_version": 1,
        "app_version": "0.3.0",
        "supervisor": supervisor.to_dict(),
        "pending_actions": [],
    }


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


@pytest.mark.asyncio
async def test_stable_managed_session_survives_app_restart(tmp_path):
    journal = tmp_path / "state.json"
    StateStore(journal).save(
        saved_supervisor_payload(
            phase=SupervisorPhase.ON_GENERATOR,
            transaction_complete=True,
        )
    )
    app = EnergySupervisorApp(
        {
            **DEFAULT_OPTIONS,
            "armed": True,
            "state_file": str(journal),
        },
        token="test",
    )
    fake = PhysicalFakeClient(journal)
    fake.states = populated_states()
    fake.states.update(
        {
            ENTITIES["grid_power"]: "off",
            ENTITIES["house_grid"]: "off",
            ENTITIES["source_generator"]: "on",
            ENTITIES["house_generator"]: "on",
            ENTITIES["generator_a_remote"]: "on",
            ENTITIES["generator_a_running"]: "on",
        }
    )
    attach_fake_client(app, fake)

    await app._tick(3.0)

    assert app.supervisor.phase == SupervisorPhase.ON_GENERATOR
    assert (
        app.generator_controllers[GeneratorSlot.A].phase
        == GeneratorPhase.READY_FOR_LOAD
    )
    hardware_calls = [
        call for call in fake.calls if call[0] in {"switch", "button"}
    ]
    assert hardware_calls == []


@pytest.mark.asyncio
async def test_external_power_change_after_reconnect_is_never_reasserted(tmp_path):
    journal = tmp_path / "state.json"
    StateStore(journal).save(
        saved_supervisor_payload(
            phase=SupervisorPhase.ON_GENERATOR,
            transaction_complete=True,
        )
    )
    app = EnergySupervisorApp(
        {
            **DEFAULT_OPTIONS,
            "armed": True,
            "state_file": str(journal),
        },
        token="test",
    )
    fake = PhysicalFakeClient(journal)
    fake.states = populated_states()
    fake.states.update(
        {
            ENTITIES["grid_power"]: "off",
            ENTITIES["house_grid"]: "off",
            ENTITIES["source_generator"]: "on",
            ENTITIES["house_generator"]: "on",
            ENTITIES["generator_a_remote"]: "on",
            ENTITIES["generator_a_running"]: "on",
        }
    )
    attach_fake_client(app, fake)
    await app._tick(3.0)

    # Пока HA был недоступен, человек вернул дом на Grid, но оставил
    # управляемый двигатель работать. Старое намерение нельзя применить снова.
    fake.states.update(
        {
            ENTITIES["grid_power"]: "on",
            ENTITIES["house_grid"]: "on",
            ENTITIES["source_generator"]: "off",
            ENTITIES["house_generator"]: "off",
        }
    )
    fake.calls.clear()
    await app._tick(4.0)

    assert app.supervisor.phase == SupervisorPhase.RECOVERY_REQUIRED
    hardware_calls = [
        call for call in fake.calls if call[0] in {"switch", "button"}
    ]
    assert hardware_calls == []


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
async def test_disarmed_app_does_not_issue_hardware_commands(tmp_path):
    app = EnergySupervisorApp(
        {
            **DEFAULT_OPTIONS,
            "armed": False,
            "state_file": str(tmp_path / "state.json"),
        },
        token="test",
    )
    fake = PhysicalFakeClient(tmp_path / "state.json")
    fake.states = populated_states()
    attach_fake_client(app, fake)

    await app._tick(0.0)

    assert not any(
        domain in {"switch", "button"}
        for domain, _service, _data in fake.calls
    )


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
async def test_app_journals_pending_command_before_hardware_call(tmp_path):
    journal = tmp_path / "state.json"
    app = EnergySupervisorApp(
        {
            **DEFAULT_OPTIONS,
            "armed": True,
            "state_file": str(journal),
        },
        token="test",
    )
    fake = PhysicalFakeClient(journal)
    fake.states = populated_states()
    fake.states[ENTITIES["ambient_temperature_external"]] = "20"
    attach_fake_client(app, fake)

    await app._tick(0.0)
    app.supervisor.request_manual_start()
    await app._tick(1.0)

    assert fake.pending_seen_before_hardware[0] == [
        {
            "controller": "generator_controller",
            "generator": "A",
            "action": "choke_to_cold_start",
        }
    ]
    saved_after_call = json.loads(journal.read_text(encoding="utf-8"))
    assert saved_after_call["pending_actions"] == []


@pytest.mark.asyncio
async def test_manual_stop_during_start_aborts_without_touching_power_selector(
    tmp_path,
):
    journal = tmp_path / "state.json"
    app = EnergySupervisorApp(
        {
            **DEFAULT_OPTIONS,
            "armed": True,
            "state_file": str(journal),
        },
        token="test",
    )
    fake = PhysicalFakeClient(journal)
    fake.states = populated_states()
    attach_fake_client(app, fake)

    await app._tick(0.0)
    app.supervisor.request_manual_start()
    await app._tick(1.0)
    await app._tick(2.0)
    app.supervisor.request_manual_stop()
    await app._tick(3.0)

    hardware_calls = [
        (domain, service, data["entity_id"])
        for domain, service, data in fake.calls
        if domain in {"switch", "button"} and "entity_id" in data
    ]
    assert hardware_calls == [
        ("button", "press", ENTITIES["generator_a_choke_cold_start"]),
        ("switch", "turn_on", ENTITIES["generator_a_remote"]),
        ("switch", "turn_off", ENTITIES["generator_a_remote"]),
        ("button", "press", ENTITIES["generator_a_choke_run"]),
    ]
    assert fake.states[ENTITIES["source_generator"]] == "off"


@pytest.mark.asyncio
async def test_recovery_reset_succeeds_only_from_safe_normal_topology(tmp_path):
    journal = tmp_path / "state.json"
    app = EnergySupervisorApp(
        {
            **DEFAULT_OPTIONS,
            "armed": True,
            "state_file": str(journal),
        },
        token="test",
    )
    fake = PhysicalFakeClient(journal)
    fake.states = populated_states()
    attach_fake_client(app, fake)
    await app._tick(0.0)

    app.supervisor.require_recovery("test")
    app.supervisor.request_recovery_reset()
    await app._tick(1.0)

    assert app.supervisor.phase == SupervisorPhase.NORMAL
    assert all(
        controller.phase == GeneratorPhase.IDLE
        for controller in app.generator_controllers.values()
    )


@pytest.mark.asyncio
async def test_recovery_reset_is_rejected_from_battery_path(tmp_path):
    journal = tmp_path / "state.json"
    app = EnergySupervisorApp(
        {
            **DEFAULT_OPTIONS,
            "armed": True,
            "state_file": str(journal),
        },
        token="test",
    )
    fake = PhysicalFakeClient(journal)
    fake.states = populated_states()
    fake.states.update(
        {
            ENTITIES["grid_ready"]: "off",
            ENTITIES["grid_power"]: "off",
            ENTITIES["house_grid"]: "off",
        }
    )
    attach_fake_client(app, fake)
    await app._tick(0.0)

    app.supervisor.require_recovery("test")
    app.supervisor.request_recovery_reset()
    await app._tick(1.0)

    assert app.supervisor.phase == SupervisorPhase.RECOVERY_REQUIRED


@pytest.mark.asyncio
async def test_complete_manual_session_obeys_controller_boundaries(tmp_path):
    journal = tmp_path / "state.json"
    app = EnergySupervisorApp(
        {
            **DEFAULT_OPTIONS,
            "armed": True,
            "state_file": str(journal),
        },
        token="test",
    )
    fake = PhysicalFakeClient(journal)
    fake.states = populated_states()
    fake.states[ENTITIES["ambient_temperature_external"]] = "20"
    attach_fake_client(app, fake)

    await app._tick(0.0)
    app.supervisor.request_manual_start()
    await app._tick(1.0)   # заслонка -> cold
    await app._tick(2.0)   # REMOTE ON

    fake.states[ENTITIES["generator_a_running"]] = "on"
    await app._tick(3.0)   # RUNNING подтверждён
    await app._tick(13.0)  # заслонка -> run
    await app._tick(43.0)  # прогрев завершён
    await app._tick(44.0)  # Grid OFF
    await app._tick(45.0)  # selector -> Generator
    await app._tick(46.0)  # подтверждение selector
    await app._tick(47.0)  # Supervisor подтверждает питание дома

    app.supervisor.request_manual_stop()
    await app._tick(48.0)  # selector -> normal
    await app._tick(49.0)  # Grid power -> ON
    await app._tick(50.0)  # подтверждение Grid path
    await app._tick(51.0)  # начинается cooldown
    await app._tick(350.0)
    assert fake.states[ENTITIES["generator_a_remote"]] == "on"
    await app._tick(351.0)  # cooldown окончен, REMOTE OFF

    fake.states[ENTITIES["generator_a_running"]] = "off"
    await app._tick(352.0)
    await app._tick(353.0)
    assert app.supervisor.session is None

    hardware_calls = [
        (domain, service, data["entity_id"])
        for domain, service, data in fake.calls
        if domain in {"switch", "button"} and "entity_id" in data
    ]
    assert hardware_calls == [
        ("button", "press", ENTITIES["generator_a_choke_cold_start"]),
        ("switch", "turn_on", ENTITIES["generator_a_remote"]),
        ("button", "press", ENTITIES["generator_a_choke_run"]),
        ("switch", "turn_off", ENTITIES["grid_power"]),
        ("switch", "turn_on", ENTITIES["source_generator"]),
        ("switch", "turn_off", ENTITIES["source_generator"]),
        ("switch", "turn_on", ENTITIES["grid_power"]),
        ("switch", "turn_off", ENTITIES["generator_a_remote"]),
    ]


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

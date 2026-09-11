from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from domain import GeneratorSlot, PowerPath, PowerSource, SupervisorEvent
from generator_controller import GeneratorPhase, GeneratorStatus
from ha_adapter import HomeAssistantAdapter
from main import DEFAULT_OPTIONS, EnergySupervisorApp
from power_transfer import (
    PowerTransferController,
    PowerTransferObservation,
    TransferActionKind,
    TransferPhase,
)
from energy_supervisor import SupervisorPhase


class ReconnectClient:
    """Минимальный transport fake именно для проверки внешнего run/reconnect loop."""

    def __init__(self) -> None:
        self.connected = asyncio.Event()
        self.connect_count = 0
        self.close_count = 0

    async def connect(self) -> None:
        self.connect_count += 1
        self.connected.set()

    async def close(self) -> None:
        self.close_count += 1
        self.connected.clear()

    async def get_time_zone(self) -> str:
        return "UTC"


class SlowPublicationClient:
    """HA fake, который намеренно зависает на сетевой публикации."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def call_service(self, domain, service, *, service_data=None) -> None:
        self.started.set()
        await self.release.wait()

    async def set_state(self, entity_id, state, *, attributes=None) -> None:
        self.started.set()
        await self.release.wait()


def make_app(tmp_path: Path) -> EnergySupervisorApp:
    return EnergySupervisorApp(
        {
            **DEFAULT_OPTIONS,
            "armed": True,
            "state_file": str(tmp_path / "state.json"),
        },
        token="test",
    )


@pytest.mark.asyncio
async def test_run_reconnect_preserves_obligation_and_latches_transient_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RUN-01: потеря HA внутри незавершённой физической операции проходит через реальный run/reconnect loop, переводит Supervisor в Recovery и не стирает уже сохранённую обязанность Grid restore."""
    app = make_app(tmp_path)
    client = ReconnectClient()
    app.client = client
    app.adapter.client = client
    app.supervisor.grid_restore_pending = True

    async def ready() -> None:
        return None

    monkeypatch.setattr(app, "_wait_until_required_entities_ready", ready)
    monkeypatch.setattr(app.adapter, "snapshot", lambda: object())
    monkeypatch.setattr(app, "_sync_generator_configuration", lambda hardware: None)

    tick_count = 0

    async def controlled_tick(now: float) -> None:
        nonlocal tick_count
        tick_count += 1
        if tick_count == 1:
            app.supervisor.phase = SupervisorPhase.STARTING_GENERATOR
            raise RuntimeError("simulated HA transport loss during transfer")

        assert app.supervisor.phase == SupervisorPhase.RECOVERY_REQUIRED
        assert app.supervisor.grid_restore_pending is True
        app.request_stop()

    monkeypatch.setattr(app, "_tick", controlled_tick)

    async def no_delay(seconds: float) -> bool:
        return app.stop_event.is_set()

    monkeypatch.setattr(app, "_stop_requested_within", no_delay)

    await asyncio.wait_for(app.run(), timeout=1.0)

    assert tick_count == 2
    assert client.connect_count == 2
    assert client.close_count == 2
    assert app.supervisor.phase == SupervisorPhase.RECOVERY_REQUIRED
    assert app.supervisor.grid_restore_pending is True


@pytest.mark.asyncio
async def test_routine_publication_does_not_wait_for_slow_home_assistant() -> None:
    """RUN-03: обычный Logbook/status I/O не удерживает control tick, даже если HA service завис; background task можно безопасно отменить при disconnect."""
    client = SlowPublicationClient()
    adapter = HomeAssistantAdapter(client, armed=True)

    await asyncio.wait_for(
        adapter.publish_events((SupervisorEvent("info", "routine event"),)),
        timeout=0.1,
    )
    await asyncio.wait_for(client.started.wait(), timeout=0.1)

    assert adapter._publication_tasks
    await adapter.cancel_background_publications()
    assert not adapter._publication_tasks


@pytest.mark.asyncio
async def test_forced_warning_delivery_remains_synchronous() -> None:
    """RUN-04: подтверждаемое Exercise warning остаётся синхронным safety prerequisite и не считается доставленным до ответа HA."""
    client = SlowPublicationClient()
    adapter = HomeAssistantAdapter(client, armed=True)

    delivery = asyncio.create_task(adapter.publish_user_notification("forced warning"))
    await asyncio.wait_for(client.started.wait(), timeout=0.1)
    assert delivery.done() is False

    client.release.set()
    assert await asyncio.wait_for(delivery, timeout=0.1) is True


def _transfer_observation(
    *,
    house_on_generator: bool,
    generator_selected: bool,
) -> PowerTransferObservation:
    return PowerTransferObservation(
        grid_ready=False,
        house_on_grid=False,
        house_on_generator=house_on_generator,
        grid_connected=False,
        generator_selected=generator_selected,
        emergency_stop=False,
    )


def test_control_and_feedback_can_change_independently_during_fallback() -> None:
    """RUN-02: generator selector control, house feedback и RUNNING считаются независимыми сигналами; потеря voltage feedback при selector=ON разрешает только safe break, а не выдуманное подтверждение контактора."""
    transfer = PowerTransferController(confirmation_timeout=60.0)

    on_generator = _transfer_observation(
        house_on_generator=True,
        generator_selected=True,
    )
    transfer.step(0.0, on_generator, None, desired_generator_ready=False)

    # Источник A уже пропал, но selector control всё ещё физически ON.
    source_lost = _transfer_observation(
        house_on_generator=False,
        generator_selected=True,
    )
    actions = transfer.step(
        1.0,
        source_lost,
        PowerSource.UPS_ONLY,
        desired_generator_ready=False,
    )
    assert [action.kind for action in actions] == [TransferActionKind.DESELECT_GENERATOR]
    assert transfer.status().phase == TransferPhase.DISCONNECTING_GENERATOR
    # До подтверждения размыкания actual_path остаётся последним достоверным,
    # а не угадывается по исчезнувшему voltage feedback.
    assert transfer.status().actual_path == PowerPath.GENERATOR

    isolated = _transfer_observation(
        house_on_generator=False,
        generator_selected=False,
    )
    transfer.step(
        2.0,
        isolated,
        PowerSource.UPS_ONLY,
        desired_generator_ready=False,
    )
    assert transfer.status().actual_path == PowerPath.ISOLATED

    # RUNNING/REMOTE резервного двигателя не обязаны совпадать с selector/house feedback.
    secondary = GeneratorStatus(
        slot=GeneratorSlot.B,
        display_name="Вепрь",
        phase=GeneratorPhase.WAITING_FOR_RUNNING,
        running=False,
        remote_on=True,
        ready_for_load=False,
        fault=None,
    )
    assert secondary.remote_on is True
    assert secondary.running is False

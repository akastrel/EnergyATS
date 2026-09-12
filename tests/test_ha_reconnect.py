from __future__ import annotations

import logging
from pathlib import Path

import pytest

from energy_supervisor import SupervisorPhase
from ha_client import HomeAssistantConnectionError
from main import DEFAULT_OPTIONS, EnergySupervisorApp
from power_transfer import TransferPhase


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
@pytest.mark.parametrize(
    "phase",
    [
        SupervisorPhase.NORMAL,
        SupervisorPhase.ON_GENERATOR,
        SupervisorPhase.RECOVERY_REQUIRED,
    ],
)
async def test_ha_restart_is_one_outage_without_false_critical(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    phase: SupervisorPhase,
) -> None:
    """HA-REC-01: restart HA в стабильном состоянии, включая уже активный Recovery, считается одним интервалом недоступности и не создаёт ложную «физическую операцию»."""
    app = make_app(tmp_path)
    app.supervisor.phase = phase
    if phase == SupervisorPhase.RECOVERY_REQUIRED:
        app.supervisor.recovery_reason = "существовавшая ранее причина"

    published = []

    async def capture_events(events) -> None:
        published.extend(events)

    monkeypatch.setattr(app.adapter, "publish_events", capture_events)
    caplog.set_level(logging.DEBUG, logger="energy_supervisor")

    app._record_interrupted_connection(
        HomeAssistantConnectionError("WebSocket HA потерян"),
        connection_was_ready=True,
        now=100.0,
    )
    app._record_interrupted_connection(
        RuntimeError("502, message='Invalid response status'"),
        connection_was_ready=False,
        now=105.0,
    )
    app._record_interrupted_connection(
        RuntimeError("502, message='Invalid response status'"),
        connection_was_ready=False,
        now=110.0,
    )

    assert app.supervisor.phase == phase
    assert sum(record.levelno == logging.WARNING for record in caplog.records) == 1
    assert not any(record.levelno == logging.CRITICAL for record in caplog.records)
    assert sum("Попытка переподключения" in record.message for record in caplog.records) == 2

    app.client.states = {"sensor.one": {}, "sensor.two": {}}
    await app._record_connection_restored(now=120.0)

    assert len(published) == 1
    message = published[0].message
    assert "восстановлена после 20 с" in message
    assert "Попыток переподключения: 3" in message
    assert "загружено состояний HA: 2" in message
    assert "HTTP 502" in message
    if phase == SupervisorPhase.RECOVERY_REQUIRED:
        assert "существовало до потери связи" in message
    else:
        assert "существовало до потери связи" not in message


@pytest.mark.asyncio
async def test_ha_loss_during_supervisor_transition_requires_recovery_once(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HA-REC-02: реальный обрыв HA во время STARTING_GENERATOR даёт один CRITICAL и один переход в Recovery; повторные 502 новых аварий не создают."""
    app = make_app(tmp_path)
    app.supervisor.phase = SupervisorPhase.STARTING_GENERATOR

    published = []

    async def capture_events(events) -> None:
        published.extend(events)

    monkeypatch.setattr(app.adapter, "publish_events", capture_events)
    caplog.set_level(logging.DEBUG, logger="energy_supervisor")

    app._record_interrupted_connection(
        HomeAssistantConnectionError("WebSocket HA потерян"),
        connection_was_ready=True,
        now=200.0,
    )
    app._record_interrupted_connection(
        RuntimeError("502 from Home Assistant Core"),
        connection_was_ready=False,
        now=205.0,
    )
    app._record_interrupted_connection(
        RuntimeError("502 from Home Assistant Core"),
        connection_was_ready=False,
        now=210.0,
    )

    assert app.supervisor.phase == SupervisorPhase.RECOVERY_REQUIRED
    assert sum(record.levelno == logging.CRITICAL for record in caplog.records) == 1

    app.client.states = {"sensor.one": {}}
    await app._record_connection_restored(now=215.0)

    assert [event.level for event in published] == ["critical", "info"]
    assert sum("Потеряна связь" in event.message for event in published) == 0
    assert "Попыток переподключения: 3" in published[-1].message


@pytest.mark.asyncio
async def test_ha_loss_during_power_transfer_is_critical_even_if_supervisor_stable(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HA-REC-03: незавершённый силовой шаг TPC сам по себе делает потерю HA критичной, даже если Supervisor ещё находится в стабильной phase."""
    app = make_app(tmp_path)
    app.supervisor.phase = SupervisorPhase.NORMAL
    app.power_transfer.phase = TransferPhase.DISCONNECTING_GRID

    published = []

    async def capture_events(events) -> None:
        published.extend(events)

    monkeypatch.setattr(app.adapter, "publish_events", capture_events)
    caplog.set_level(logging.DEBUG, logger="energy_supervisor")

    app._record_interrupted_connection(
        HomeAssistantConnectionError("WebSocket HA потерян"),
        connection_was_ready=True,
        now=300.0,
    )

    assert app.power_transfer.phase == TransferPhase.RECOVERY_REQUIRED
    assert sum(record.levelno == logging.CRITICAL for record in caplog.records) == 1

    app.client.states = {}
    await app._record_connection_restored(now=301.0)

    assert published[-1].level == "info"
    assert "восстановлена после 1 с" in published[-1].message

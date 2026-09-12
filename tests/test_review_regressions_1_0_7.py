"""Регрессии R1–R6 из повторного review EnergyATS 1.0.7.

Каждый тест защищает конкретный воспроизведённый отказ, а не внутреннее имя
фазы. Основные assertions проверяют конечную физическую/операторскую семантику.
"""

from __future__ import annotations

import asyncio

import pytest

from domain import GeneratorSlot
from energy_supervisor import SupervisorPhase
from generator_bus import GeneratorBusOwner, GeneratorBusTracker
from ha_adapter import ENTITIES, HomeAssistantAdapter
from ha_client import HomeAssistantConnectionError
from power_transfer import (
    PowerTransferController,
    PowerTransferObservation,
    TransferActionKind,
)
from test_app_adapter import FakeClient
from test_end_to_end_scenarios import (
    accelerate_generators,
    drive_primary_a_to_house,
    make_app,
    switch_calls,
)
from test_load_manager import observation, stable_manager
from test_ups_run_app import setup, wait_on_ups


@pytest.mark.asyncio
async def test_r1_recovery_breaks_dead_generator_branch_and_reaches_grid(tmp_path):
    """Reset из selector=ON без generator voltage обязан безопасно снять ветвь и вернуть Grid.

    Сценарий воспроизводит R1: двигатель уже OFF, house-generator feedback OFF,
    selector ещё ON. Recovery должен сделать DESELECT -> CONNECT_GRID и завершиться.
    """
    app, fake = make_app(
        tmp_path,
        grid_failure_delay=0,
        transfer_confirmation_timeout=60,
    )
    accelerate_generators(app)
    now = await drive_primary_a_to_house(app, fake)

    app.supervisor.require_recovery("Unconfirmed physical operation")
    fake.states[ENTITIES["generator_a_running"]] = "off"
    fake.states[ENTITIES["generator_a_remote"]] = "off"
    fake.states[ENTITIES["house_generator"]] = "off"
    fake.states[ENTITIES["grid_ready"]] = "on"
    fake.calls.clear()

    app.supervisor.request_recovery_reset()
    for offset in range(6):
        await app._tick(now + offset)

    calls = switch_calls(fake)
    assert ("turn_off", ENTITIES["source_generator"]) in calls
    assert ("turn_on", ENTITIES["grid_power"]) in calls
    assert fake.states[ENTITIES["source_generator"]] == "off"
    assert fake.states[ENTITIES["grid_power"]] == "on"
    assert app.supervisor.phase == SupervisorPhase.NORMAL
    assert app.supervisor.recovery_reset_in_progress is False
    await app.adapter.cancel_background_publications()


def test_r1_recovery_unknown_without_safe_step_is_bounded_by_timeout():
    """Принятый Recovery не может бесконечно ждать неоднозначную известную топологию.

    Если selector уже OFF, Grid contactor OFF, но house feedback противоречив и
    безопасный следующий шаг не доказан, TPC обязан вернуть явную ошибку по timeout.
    """
    tpc = PowerTransferController(confirmation_timeout=10)
    tpc.begin_recovery_to_grid_path()
    ambiguous = PowerTransferObservation(
        grid_ready=True,
        house_on_grid=True,
        house_on_generator=False,
        grid_connected=False,
        generator_selected=False,
        emergency_stop=False,
    )

    actions, error = tpc.step_recovery_to_grid_path(0, ambiguous)
    assert actions == [] and error is None
    actions, error = tpc.step_recovery_to_grid_path(10, ambiguous)
    assert actions == []
    assert error is not None
    assert "безопасный следующий шаг" in error


@pytest.mark.asyncio
async def test_r2_grid_stability_restarts_after_home_assistant_gap(tmp_path):
    """Два ON snapshot по краям HA gap не доказывают непрерывные 60 секунд Grid.

    После reconnect отсчёт стабильности начинается заново; DESELECT разрешён
    только после полной новой выдержки, а не на первом snapshot.
    """
    app, fake = make_app(
        tmp_path,
        grid_failure_delay=0,
        grid_restore_stable_time=60,
    )
    accelerate_generators(app)
    now = await drive_primary_a_to_house(app, fake)

    fake.states[ENTITIES["grid_ready"]] = "on"
    await app._tick(now)
    assert app.supervisor.grid_ready_since == now

    app._record_interrupted_connection(
        HomeAssistantConnectionError("gap"),
        connection_was_ready=True,
        now=now + 1,
    )
    assert app.supervisor.grid_ready_since is None

    fake.calls.clear()
    await app._record_connection_restored(now=now + 120)
    await app._tick(now + 120)
    assert ("turn_off", ENTITIES["source_generator"]) not in switch_calls(fake)

    await app._tick(now + 179)
    assert ("turn_off", ENTITIES["source_generator"]) not in switch_calls(fake)

    await app._tick(now + 180)
    assert ("turn_off", ENTITIES["source_generator"]) in switch_calls(fake)
    await app.adapter.cancel_background_publications()


@pytest.mark.asyncio
async def test_r3_bus_owner_becomes_unknown_after_unobserved_dual_run_gap(tmp_path):
    """FIFO owner нельзя переносить через gap, если после reconnect оба двигателя RUNNING.

    A стартует первым, затем B. После ненаблюдаемого интервала порядок мог
    измениться, поэтому старый owner A должен быть инвалидирован до новой истории.
    """
    app, fake = make_app(tmp_path)
    await app._tick(0)
    fake.states[ENTITIES["generator_a_running"]] = "on"
    await app._tick(1)
    fake.states[ENTITIES["generator_b_running"]] = "on"
    await app._tick(2)
    assert app.generator_bus.status().owner_slot == GeneratorSlot.A

    app._record_interrupted_connection(
        HomeAssistantConnectionError("gap"),
        connection_was_ready=True,
        now=3,
    )
    assert app.generator_bus.status().owner == GeneratorBusOwner.UNKNOWN

    await app._record_connection_restored(now=100)
    await app._tick(100)
    assert app.generator_bus.status().owner == GeneratorBusOwner.UNKNOWN
    await app.adapter.cancel_background_publications()


def test_r3_persisted_fifo_owner_is_not_proof_after_restart():
    """Persisted owner диагностичен, но restart разрывает доказанную RUNNING-history.

    При первом snapshot после restart один RUNNING снова однозначен; два RUNNING
    остаются UNKNOWN вместо выбора старого сохранённого owner.
    """
    tracker = GeneratorBusTracker()
    tracker.update(
        {GeneratorSlot.A: True, GeneratorSlot.B: False},
        grid_ready=True,
        test_mode=False,
    )
    tracker.update(
        {GeneratorSlot.A: True, GeneratorSlot.B: True},
        grid_ready=True,
        test_mode=False,
    )
    assert tracker.status().owner == GeneratorBusOwner.A

    restored = GeneratorBusTracker.from_dict(tracker.to_dict())
    status = restored.update(
        {GeneratorSlot.A: True, GeneratorSlot.B: True},
        grid_ready=True,
        test_mode=False,
    )
    assert status.owner == GeneratorBusOwner.UNKNOWN


@pytest.mark.asyncio
async def test_r4_recovery_status_has_priority_over_ups_wait(tmp_path):
    """Emergency Stop во время UPS wait должен показывать Recovery, а не штатный UPS state.

    UPS Run может оставаться WAITING_ON_UPS внутренне, но основной операторский
    state обязан первым показывать блокирующее состояние Supervisor.
    """
    app, fake = setup(tmp_path)
    await wait_on_ups(app, fake)
    fake.states[ENTITIES["emergency_stop"]] = "on"
    await app._tick(3)

    assert app.supervisor.phase == SupervisorPhase.RECOVERY_REQUIRED
    state = fake.state_writes[-1][1]
    assert state == "Требуется восстановление"
    await app.adapter.cancel_background_publications()


@pytest.mark.asyncio
async def test_r5_status_publisher_retries_unchanged_payload_after_transient_failure():
    """Одиночный REST failure не должен навсегда потерять неизменившийся status.

    App может больше не присылать тот же payload, поэтому retry является
    обязанностью последовательного publisher-а адаптера.
    """
    fake = FakeClient()
    attempts: list[str] = []

    async def flaky(entity_id, state, *, attributes=None):
        attempts.append(state)
        if len(attempts) == 1:
            raise RuntimeError("temporary HTTP 503")
        fake.states[entity_id] = state

    fake.set_state = flaky
    adapter = HomeAssistantAdapter(fake, armed=True)
    await adapter.publish_status("Grid", {"phase": "normal"})

    await asyncio.sleep(1.05)
    await asyncio.sleep(0)

    assert attempts == ["Grid", "Grid"]
    assert fake.states["sensor.energy_ats_status"] == "Grid"
    await adapter.cancel_background_publications()


@pytest.mark.asyncio
async def test_r5_status_publisher_is_serial_and_latest_value_wins():
    """Медленный старый REST write не может завершиться после нового Recovery status.

    Publisher выполняет максимум один set_state за раз; если desired изменился,
    после старого write обязательно отправляется последнее значение.
    """
    fake = FakeClient()
    gate = asyncio.Event()
    writes: list[str] = []

    async def delayed(entity_id, state, *, attributes=None):
        if state == "old":
            await gate.wait()
        writes.append(state)

    fake.set_state = delayed
    adapter = HomeAssistantAdapter(fake, armed=True)

    await adapter.publish_status("old", {})
    await adapter.publish_status("RECOVERY_REQUIRED", {})
    await asyncio.sleep(0)
    assert writes == []

    gate.set()
    for _ in range(5):
        await asyncio.sleep(0)

    assert writes == ["old", "RECOVERY_REQUIRED"]
    assert writes[-1] == "RECOVERY_REQUIRED"
    await adapter.cancel_background_publications()


def test_r6_frozen_meter_stays_degraded_until_new_stabilized_samples():
    """Один frozen revision даёт один отказ, без циклов «сломалось/восстановлено».

    Восстановление начинается только с новой revision и объявляется после нового
    stabilization window; старый overload timer не используется повторно.
    """
    manager = stable_manager()
    events: list[tuple[int, str]] = []

    for t in range(3, 40):
        decision = manager.step(
            observation(t, on_generator=True, power=1300, sample=4)
        )
        events.extend((t, event.message) for event in decision.events)

    recovered = [message for _, message in events if "восстановлено" in message]
    unavailable = [
        message
        for _, message in events
        if "временно недоступно" in message
    ]
    assert recovered == []
    assert len(unavailable) == 1
    assert manager.phase.value == "degraded"

    # Новая revision начинает recovery measurement, но ещё не означает recovery.
    assert not manager.step(
        observation(40, on_generator=True, power=900, sample=5)
    ).events
    middle = manager.step(
        observation(41, on_generator=True, power=900, sample=6)
    )
    assert not any("восстановлено" in event.message for event in middle.events)

    final = manager.step(
        observation(42, on_generator=True, power=900, sample=7)
    )
    assert sum("восстановлено" in event.message for event in final.events) == 1
    assert final.actions == ()

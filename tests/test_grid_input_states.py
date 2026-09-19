from __future__ import annotations

import pytest

from domain import GeneratorSlot, GridInputState, PowerPath
from energy_supervisor import SupervisorPhase
from ha_adapter import ENTITIES
from test_end_to_end_scenarios import make_app


@pytest.mark.asyncio
async def test_partial_grid_loss_recovers_before_delay_without_generator_or_recovery(
    tmp_path,
):
    """Отключение одной фазы короче 60 с должно только пережидаться."""
    app, fake = make_app(
        tmp_path,
        grid_failure_delay=60,
        transfer_confirmation_timeout=60,
    )
    await app._tick(0)

    fake.states[ENTITIES["automatic_transfer"]] = "on"
    fake.states[ENTITIES["grid_ready"]] = "off"
    fake.states[ENTITIES["grid_input_state"]] = "partial"
    # Сетевой контактор остаётся включён: живая фаза продолжает питать часть дома.
    assert fake.states[ENTITIES["grid_power"]] == "on"
    assert fake.states[ENTITIES["house_grid"]] == "on"

    await app._tick(1)
    await app._tick(50)

    assert app.supervisor.phase == SupervisorPhase.GRID_FAILURE_DELAY
    assert app.power_transfer.status().actual_path == PowerPath.GRID
    assert app.power_transfer.status().recovery_required is False
    assert app.supervisor.session is None

    fake.states[ENTITIES["grid_ready"]] = "on"
    fake.states[ENTITIES["grid_input_state"]] = "normal"
    await app._tick(51)

    assert app.supervisor.phase == SupervisorPhase.NORMAL
    assert app.supervisor.session is None
    assert fake.states[ENTITIES["generator_a_remote"]] == "off"
    assert fake.states[ENTITIES["generator_b_remote"]] == "off"
    await app.adapter.cancel_background_publications()


@pytest.mark.asyncio
async def test_partial_grid_loss_longer_than_delay_starts_normal_outage_session(
    tmp_path,
):
    """Если фаза не вернулась за 60 с, запускается обычный резерв без Recovery."""
    app, fake = make_app(
        tmp_path,
        grid_failure_delay=60,
        transfer_confirmation_timeout=60,
    )
    await app._tick(0)

    fake.states[ENTITIES["automatic_transfer"]] = "on"
    fake.states[ENTITIES["grid_ready"]] = "off"
    fake.states[ENTITIES["grid_input_state"]] = "partial"

    await app._tick(1)
    await app._tick(60)
    assert app.supervisor.session is None

    await app._tick(61)

    assert app.supervisor.phase == SupervisorPhase.STARTING_GENERATOR
    assert app.supervisor.session is not None
    assert app.supervisor.session.generator == GeneratorSlot.A
    assert app.supervisor.desired_generators[GeneratorSlot.A] is True
    assert app.power_transfer.status().recovery_required is False
    await app.adapter.cancel_background_publications()


def test_adapter_reads_normal_partial_and_lost_grid_states():
    """Новый HA sensor передаёт три состояния без перегрузки binary ready."""
    # Используем лёгкий fake из общего adapter test, без запуска EnergyATS.
    from ha_adapter import HomeAssistantAdapter
    from test_app_adapter import FakeClient, populated_states

    fake = FakeClient()
    fake.states = populated_states()
    adapter = HomeAssistantAdapter(fake, armed=False)

    expected = {
        "normal": GridInputState.NORMAL,
        "partial": GridInputState.PARTIAL,
        "lost": GridInputState.LOST,
    }
    for raw, value in expected.items():
        fake.states[ENTITIES["grid_input_state"]] = raw
        assert adapter.snapshot().grid_input_state == value

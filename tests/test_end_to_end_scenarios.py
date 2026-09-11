"""Сквозные сценарии EnergyATS: HA-сигналы -> policy/FSM -> команды железу.

Нормативный перечень обязательных сценариев находится в REQUIREMENTS_RU §22.
Каждый integration/E2E тест здесь имеет короткое человеко-читаемое описание того,
какое пользовательское или физическое поведение он защищает.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from domain import GeneratorSlot, PowerPath, PowerSource
from energy_supervisor import SupervisorPhase
from generator_bus import GeneratorBusOwner, GeneratorRunContext
from generator_controller import GeneratorPhase
from ha_adapter import ENTITIES
from main import DEFAULT_OPTIONS, EnergySupervisorApp
from test_app_adapter import PhysicalFakeClient, attach_fake_client, populated_states


def make_app(
    tmp_path: Path,
    **option_overrides,
) -> tuple[EnergySupervisorApp, PhysicalFakeClient]:
    journal = tmp_path / "state.json"
    app = EnergySupervisorApp(
        {
            **DEFAULT_OPTIONS,
            "armed": True,
            "state_file": str(journal),
            **option_overrides,
        },
        token="test",
    )
    fake = PhysicalFakeClient(journal)
    fake.states = populated_states()
    fake.states[ENTITIES["ambient_temperature_external"]] = "20"
    fake.states[ENTITIES["test_mode"]] = "off"
    attach_fake_client(app, fake)
    return app, fake


def accelerate_generators(app: EnergySupervisorApp) -> None:
    """Ускорить таймеры, сохранив последовательность FSM."""

    for controller in app.generator_controllers.values():
        controller.profile = replace(
            controller.profile,
            choke_move_seconds=0.0,
            cold_start_choke_hold_seconds=0.0,
            start_timeout_seconds=2.0,
            stop_timeout_seconds=2.0,
            cooldown_seconds=0.0,
            warmup_warm_seconds=0.0,
            warmup_cool_seconds=0.0,
            warmup_cold_seconds=0.0,
            warmup_very_cold_seconds=0.0,
        )


def set_grid_outage(fake: PhysicalFakeClient, *, automatic: bool = True) -> None:
    fake.states[ENTITIES["automatic_transfer"]] = "on" if automatic else "off"
    fake.states[ENTITIES["grid_ready"]] = "off"
    fake.states[ENTITIES["house_grid"]] = "off"
    fake.states[ENTITIES["test_mode"]] = "off"


async def drive_primary_a_to_house(
    app: EnergySupervisorApp,
    fake: PhysicalFakeClient,
    *,
    start_now: float = 0.0,
) -> float:
    set_grid_outage(fake)
    now = start_now
    for _ in range(40):
        await app._tick(now)
        if fake.states[ENTITIES["generator_a_remote"]] == "on":
            fake.states[ENTITIES["generator_a_running"]] = "on"
        if (
            app.supervisor.phase == SupervisorPhase.ON_GENERATOR
            and app.supervisor.session is not None
            and app.supervisor.session.generator == GeneratorSlot.A
        ):
            return now + 1.0
        now += 1.0
    raise AssertionError("PRIMARY A не дошёл до устойчивого питания дома")


def switch_calls(fake: PhysicalFakeClient) -> list[tuple[str, str]]:
    return [
        (service, data.get("entity_id"))
        for domain, service, data in fake.calls
        if domain == "switch"
    ]


def _restart_app(
    tmp_path: Path,
    states: dict[str, str],
    **option_overrides,
) -> tuple[EnergySupervisorApp, PhysicalFakeClient]:
    """Создать новый процесс App на том же journal и вернуть ему те же HA states."""

    app, fake = make_app(tmp_path, **option_overrides)
    fake.states = dict(states)
    attach_fake_client(app, fake)
    return app, fake


class StuckGeneratorFeedbackFake(PhysicalFakeClient):
    """Контактор получает команду выбора generator bus, но feedback не подтверждается."""

    async def call_service(self, domain, service, *, service_data=None):
        await super().call_service(domain, service, service_data=service_data)
        data = service_data or {}
        if (
            domain == "switch"
            and service == "turn_on"
            and data.get("entity_id") == ENTITIES["source_generator"]
        ):
            self.states[ENTITIES["house_generator"]] = "off"


@pytest.mark.asyncio
async def test_manual_start_transfers_to_primary_after_ready(tmp_path):
    """Ручной переход на резерв должен сначала штатно запустить PRIMARY и только после его готовности подключить дом к генератору. Команда запуска сама по себе не считается подтверждением питания дома."""

    app, fake = make_app(tmp_path)
    await app._tick(0.0)
    app.supervisor.request_manual_start()
    await app._tick(1.0)
    await app._tick(2.0)
    fake.states[ENTITIES["generator_a_running"]] = "on"
    await app._tick(3.0)
    await app._tick(13.0)
    await app._tick(43.0)
    await app._tick(44.0)
    await app._tick(45.0)
    await app._tick(46.0)
    await app._tick(47.0)

    assert app.supervisor.phase == SupervisorPhase.ON_GENERATOR
    assert fake.states[ENTITIES["grid_power"]] == "off"
    assert fake.states[ENTITIES["source_generator"]] == "on"
    assert fake.states[ENTITIES["house_generator"]] == "on"
    assert app.generator_bus.status().owner_slot == GeneratorSlot.A
    assert app.power_transfer.status().actual_source == PowerSource.GENERATOR


@pytest.mark.asyncio
async def test_idle_manual_grid_disconnect_is_ups_only_and_not_reverted(tmp_path):
    """Намеренное отключение Grid при физически исправной внешней сети не является outage. EnergyATS должен оставить дом изолированным/UPS-only и не включать Grid обратно по собственной инициативе."""

    app, fake = make_app(tmp_path)
    await app._tick(0.0)
    fake.calls.clear()

    fake.states[ENTITIES["grid_power"]] = "off"
    fake.states[ENTITIES["house_grid"]] = "off"
    await app._tick(1.0)

    assert app.supervisor.desired_source is None
    assert app.power_transfer.status().actual_path == PowerPath.ISOLATED
    assert app.power_transfer.status().actual_source == PowerSource.UPS_ONLY
    assert ("turn_on", ENTITIES["grid_power"]) not in switch_calls(fake)


@pytest.mark.asyncio
async def test_two_running_generators_are_not_an_interlock_fault(tmp_path):
    """Два одновременно работающих двигателя допустимы реальной схемой и сами по себе не являются аварией. Первый запущенный генератор сохраняет FIFO-владение общей генераторной шиной."""

    app, fake = make_app(tmp_path)
    await app._tick(0.0)

    fake.states[ENTITIES["generator_a_running"]] = "on"
    await app._tick(1.0)
    fake.states[ENTITIES["generator_b_running"]] = "on"
    await app._tick(2.0)

    assert app.generator_bus.status().owner_slot == GeneratorSlot.A
    assert app.supervisor.phase != SupervisorPhase.RECOVERY_REQUIRED


@pytest.mark.asyncio
async def test_bus_owner_moves_to_second_running_generator_when_first_stops(tmp_path):
    """Если текущий владелец общей генераторной шины останавливается, уже работающий второй генератор должен автоматически получить шину. EnergyATS только фиксирует физический takeover и не выбирает A/B программной командой."""

    app, fake = make_app(tmp_path)
    await app._tick(0.0)
    fake.states[ENTITIES["generator_a_running"]] = "on"
    await app._tick(1.0)
    fake.states[ENTITIES["generator_b_running"]] = "on"
    await app._tick(2.0)

    fake.states[ENTITIES["generator_a_running"]] = "off"
    await app._tick(3.0)

    assert app.generator_bus.status().owner_slot == GeneratorSlot.B


@pytest.mark.asyncio
async def test_missing_required_generator_state_never_emits_hardware_commands(tmp_path):
    """Неизвестное обязательное состояние генератора не даёт права угадывать и продолжать управление. При таком входе EnergyATS не должен выдавать аппаратные команды."""

    app, fake = make_app(tmp_path)
    await app._tick(0.0)
    fake.calls.clear()

    fake.states[ENTITIES["generator_a_running"]] = "unknown"
    app.supervisor.request_manual_start()
    await app._tick(1.0)

    assert app.supervisor.session is None
    assert not any(
        domain in {"switch", "button"}
        for domain, _service, _data in fake.calls
    )


@pytest.mark.asyncio
async def test_manual_stop_without_grid_removes_house_load_before_engine_stop(tmp_path):
    """При ручной остановке без доступной Grid дом сначала снимается с генераторной ветви в UPS_ONLY. Grid contactor не включается поверх отсутствующей сети, а двигатель останавливается только после подтверждённого снятия нагрузки."""

    app, fake = make_app(tmp_path)
    fake.states[ENTITIES["grid_ready"]] = "off"
    fake.states[ENTITIES["house_grid"]] = "off"
    await app._tick(0.0)
    app.supervisor.request_manual_start()
    await app._tick(1.0)
    await app._tick(2.0)
    fake.states[ENTITIES["generator_a_running"]] = "on"
    await app._tick(3.0)
    await app._tick(13.0)
    await app._tick(43.0)
    await app._tick(44.0)
    await app._tick(45.0)
    await app._tick(46.0)
    await app._tick(47.0)
    assert app.supervisor.phase == SupervisorPhase.ON_GENERATOR

    fake.calls.clear()
    app.supervisor.request_manual_stop()
    await app._tick(48.0)
    await app._tick(49.0)
    await app._tick(50.0)

    calls = switch_calls(fake)
    assert calls[0] == ("turn_off", ENTITIES["source_generator"])
    assert ("turn_on", ENTITIES["grid_power"]) not in calls
    assert app.power_transfer.status().actual_path == PowerPath.ISOLATED
    assert app.power_transfer.status().actual_source == PowerSource.UPS_ONLY
    assert app.generator_controllers[GeneratorSlot.A].phase != GeneratorPhase.IDLE


@pytest.mark.asyncio
async def test_outage_primary_failure_falls_back_to_secondary_and_powers_house(tmp_path):
    """Если PRIMARY не запускается во время настоящего outage, разрешён один fallback на свободный SECONDARY. Исправный SECONDARY должен довести дом до генераторного питания без повторного возврата к PRIMARY."""

    app, fake = make_app(
        tmp_path,
        grid_failure_delay=0,
        grid_restore_stable_time=0,
    )
    accelerate_generators(app)
    set_grid_outage(fake)

    now = 0.0
    for _ in range(50):
        await app._tick(now)
        if fake.states[ENTITIES["generator_b_remote"]] == "on":
            fake.states[ENTITIES["generator_b_running"]] = "on"
        if (
            app.supervisor.phase == SupervisorPhase.ON_GENERATOR
            and app.supervisor.session is not None
            and app.supervisor.session.generator == GeneratorSlot.B
        ):
            break
        now += 1.0
    else:
        raise AssertionError("fallback SECONDARY не довёл дом до генераторного питания")

    assert app.supervisor.session is not None
    assert app.supervisor.session.fallback_used is True
    assert fake.states[ENTITIES["generator_a_remote"]] == "off"
    assert fake.states[ENTITIES["generator_b_remote"]] == "on"
    assert fake.states[ENTITIES["house_generator"]] == "on"
    assert app.generator_bus.status().owner_slot == GeneratorSlot.B
    assert switch_calls(fake).count(
        ("turn_on", ENTITIES["generator_b_remote"])
    ) == 1


@pytest.mark.asyncio
async def test_managed_a_dies_external_b_takes_bus_without_becoming_managed(tmp_path):
    """Внешне запущенный SECONDARY может автоматически принять генераторную шину после остановки managed PRIMARY. Сам факт физического takeover не превращает внешний запуск SECONDARY в управляемый EnergyATS запуск."""

    app, fake = make_app(
        tmp_path,
        grid_failure_delay=0,
        grid_restore_stable_time=60,
    )
    accelerate_generators(app)
    now = await drive_primary_a_to_house(app, fake)

    fake.states[ENTITIES["generator_b_remote"]] = "on"
    fake.states[ENTITIES["generator_b_running"]] = "on"
    await app._tick(now)
    now += 1.0

    assert app.generator_bus.status().owner_slot == GeneratorSlot.A
    assert (
        app.generator_bus.status().run_contexts[GeneratorSlot.B]
        == GeneratorRunContext.OUTAGE_RELATED
    )

    fake.calls.clear()
    fake.states[ENTITIES["generator_a_running"]] = "off"
    await app._tick(now)

    assert app.supervisor.phase == SupervisorPhase.ON_GENERATOR
    assert app.generator_bus.status().owner_slot == GeneratorSlot.B
    assert app.supervisor.session is not None
    assert app.supervisor.session.generator == GeneratorSlot.A
    assert fake.states[ENTITIES["generator_b_remote"]] == "on"
    assert not any(
        entity_id == ENTITIES["generator_b_remote"]
        for _service, entity_id in switch_calls(fake)
    )


@pytest.mark.asyncio
async def test_stable_grid_returns_house_and_stops_all_outage_related_generators(tmp_path):
    """После устойчивого возврата Grid дом сначала должен быть возвращён на сеть. Только затем разрешена остановка всех генераторов, которые достоверно относятся к этому outage."""

    app, fake = make_app(
        tmp_path,
        grid_failure_delay=0,
        grid_restore_stable_time=0,
    )
    accelerate_generators(app)
    now = await drive_primary_a_to_house(app, fake)

    fake.states[ENTITIES["generator_b_remote"]] = "on"
    fake.states[ENTITIES["generator_b_running"]] = "on"
    await app._tick(now)
    now += 1.0

    assert (
        app.generator_bus.status().run_contexts[GeneratorSlot.A]
        == GeneratorRunContext.OUTAGE_RELATED
    )
    assert (
        app.generator_bus.status().run_contexts[GeneratorSlot.B]
        == GeneratorRunContext.OUTAGE_RELATED
    )

    fake.calls.clear()
    fake.states[ENTITIES["grid_ready"]] = "on"
    for _ in range(30):
        await app._tick(now)
        for remote_key, running_key in (
            ("generator_a_remote", "generator_a_running"),
            ("generator_b_remote", "generator_b_running"),
        ):
            if fake.states[ENTITIES[remote_key]] == "off":
                fake.states[ENTITIES[running_key]] = "off"
        if (
            app.supervisor.session is None
            and fake.states[ENTITIES["house_grid"]] == "on"
            and fake.states[ENTITIES["generator_a_running"]] == "off"
            and fake.states[ENTITIES["generator_b_running"]] == "off"
        ):
            break
        now += 1.0
    else:
        raise AssertionError("outage-related генераторы не были полностью остановлены")

    calls = switch_calls(fake)
    deselect = calls.index(("turn_off", ENTITIES["source_generator"]))
    grid_on = calls.index(("turn_on", ENTITIES["grid_power"]))
    a_stop = calls.index(("turn_off", ENTITIES["generator_a_remote"]))
    b_stop = calls.index(("turn_off", ENTITIES["generator_b_remote"]))
    assert deselect < grid_on < a_stop
    assert deselect < grid_on < b_stop


@pytest.mark.asyncio
async def test_test_run_survives_grid_restore(tmp_path):
    """Внешний TEST_RUN сохраняет смысл своего непрерывного запуска и не становится outage-related задним числом. Возврат Grid сам по себе не должен отправлять такому генератору REMOTE OFF."""

    app, fake = make_app(
        tmp_path,
        grid_failure_delay=0,
        grid_restore_stable_time=0,
    )
    set_grid_outage(fake, automatic=False)
    await app._tick(0.0)

    fake.states[ENTITIES["test_mode"]] = "on"
    fake.states[ENTITIES["generator_b_remote"]] = "on"
    fake.states[ENTITIES["generator_b_running"]] = "on"
    await app._tick(1.0)
    assert (
        app.generator_bus.status().run_contexts[GeneratorSlot.B]
        == GeneratorRunContext.TEST_RUN
    )

    fake.states[ENTITIES["test_mode"]] = "off"
    fake.calls.clear()
    fake.states[ENTITIES["grid_ready"]] = "on"
    fake.states[ENTITIES["house_grid"]] = "on"
    for now in (2.0, 3.0, 4.0, 5.0):
        await app._tick(now)

    assert fake.states[ENTITIES["generator_b_running"]] == "on"
    assert fake.states[ENTITIES["generator_b_remote"]] == "on"
    assert ("turn_off", ENTITIES["generator_b_remote"]) not in switch_calls(fake)


# Дополнительное сквозное покрытие обязательных сценариев REQUIREMENTS_RU §22.1.


@pytest.mark.asyncio
async def test_22_1_01_normal_grid_is_stable_and_silent(tmp_path):
    """При исправной Grid EnergyATS должен только наблюдать и не «чинить» штатное состояние. Дом остаётся на сети, а switch/button calls без причины отсутствуют."""

    app, fake = make_app(tmp_path)
    await app._tick(0.0)

    assert app.supervisor.phase == SupervisorPhase.NORMAL
    assert app.power_transfer.status().actual_source == PowerSource.GRID
    assert switch_calls(fake) == []
    assert not any(domain == "button" for domain, _service, _data in fake.calls)


@pytest.mark.asyncio
async def test_22_1_02_physical_grid_loss_respects_failure_delay(tmp_path):
    """Краткая физическая потеря внешней сети не должна запускать генератор раньше `grid_failure_delay`. Только после истечения задержки создаётся outage-сессия и начинается штатный запуск PRIMARY."""

    app, fake = make_app(tmp_path, grid_failure_delay=5)
    accelerate_generators(app)
    await app._tick(0.0)
    fake.calls.clear()

    set_grid_outage(fake)
    await app._tick(1.0)
    await app._tick(5.9)

    assert app.supervisor.phase == SupervisorPhase.GRID_FAILURE_DELAY
    assert app.supervisor.session is None
    assert ("turn_on", ENTITIES["generator_a_remote"]) not in switch_calls(fake)

    await app._tick(6.0)
    assert app.supervisor.session is not None
    assert app.supervisor.session.generator == GeneratorSlot.A
    assert app.supervisor.phase == SupervisorPhase.STARTING_GENERATOR


@pytest.mark.asyncio
async def test_22_1_04_grid_loss_without_generator_is_ups_only(tmp_path):
    """При реальной потере Grid и выключенном automatic transfer генератор самопроизвольно не запускается. Обычная часть дома остаётся без внешнего источника, а система честно показывает UPS_ONLY."""

    app, fake = make_app(tmp_path)
    await app._tick(0.0)
    fake.calls.clear()

    set_grid_outage(fake, automatic=False)
    await app._tick(1.0)

    assert app.power_transfer.status().actual_source == PowerSource.UPS_ONLY
    assert app.supervisor.session is None
    assert ("turn_on", ENTITIES["generator_a_remote"]) not in switch_calls(fake)
    assert ("turn_on", ENTITIES["generator_b_remote"]) not in switch_calls(fake)


@pytest.mark.asyncio
async def test_22_1_05_automatic_outage_starts_primary_and_powers_house(tmp_path):
    """Настоящий automatic outage должен довести исправный PRIMARY до питания дома через generator bus. Успешный PRIMARY не должен провоцировать запуск SECONDARY."""

    app, fake = make_app(
        tmp_path,
        grid_failure_delay=0,
        grid_restore_stable_time=0,
    )
    accelerate_generators(app)
    await drive_primary_a_to_house(app, fake)

    assert app.supervisor.phase == SupervisorPhase.ON_GENERATOR
    assert app.supervisor.session is not None
    assert app.supervisor.session.generator == GeneratorSlot.A
    assert fake.states[ENTITIES["house_generator"]] == "on"
    assert app.generator_bus.status().owner_slot == GeneratorSlot.A
    assert ("turn_on", ENTITIES["generator_b_remote"]) not in switch_calls(fake)


@pytest.mark.asyncio
async def test_22_1_12_running_primary_stops_then_managed_fallback_starts_secondary(tmp_path):
    """Если PRIMARY останавливается уже после успешного питания дома, свободный SECONDARY должен быть запущен как единственный managed fallback. После его запуска generator supply восстанавливается без нового старта PRIMARY."""

    app, fake = make_app(
        tmp_path,
        grid_failure_delay=0,
        grid_restore_stable_time=60,
    )
    accelerate_generators(app)
    now = await drive_primary_a_to_house(app, fake)
    fake.calls.clear()

    fake.states[ENTITIES["generator_a_running"]] = "off"
    for _ in range(30):
        await app._tick(now)
        if fake.states[ENTITIES["generator_b_remote"]] == "on":
            fake.states[ENTITIES["generator_b_running"]] = "on"
        if (
            app.supervisor.phase == SupervisorPhase.ON_GENERATOR
            and app.supervisor.session is not None
            and app.supervisor.session.generator == GeneratorSlot.B
        ):
            break
        now += 1.0
    else:
        raise AssertionError("fallback после остановки PRIMARY не завершился на SECONDARY")

    assert app.supervisor.session is not None
    assert app.supervisor.session.fallback_used is True
    assert app.generator_bus.status().owner_slot == GeneratorSlot.B
    assert switch_calls(fake).count(("turn_on", ENTITIES["generator_b_remote"])) == 1


@pytest.mark.asyncio
async def test_22_1_13_secondary_failure_after_fallback_requires_recovery_without_ping_pong(tmp_path):
    """Второй отказ после уже использованного fallback должен завершить автоматическую цепочку. EnergyATS переходит в recovery и не начинает ping-pong B→A→B при продолжающемся outage."""

    app, fake = make_app(
        tmp_path,
        grid_failure_delay=0,
        grid_restore_stable_time=60,
    )
    accelerate_generators(app)
    now = await drive_primary_a_to_house(app, fake)
    fake.calls.clear()

    fake.states[ENTITIES["generator_a_running"]] = "off"
    for _ in range(30):
        await app._tick(now)
        if app.supervisor.phase == SupervisorPhase.RECOVERY_REQUIRED:
            break
        now += 1.0
    else:
        raise AssertionError("двойной отказ не привёл к RECOVERY_REQUIRED")

    assert app.supervisor.session is not None
    assert app.supervisor.session.fallback_used is True
    assert app.supervisor.phase == SupervisorPhase.RECOVERY_REQUIRED
    assert switch_calls(fake).count(("turn_on", ENTITIES["generator_b_remote"])) == 1
    assert ("turn_on", ENTITIES["generator_a_remote"]) not in switch_calls(fake)


@pytest.mark.asyncio
async def test_22_1_14_restart_with_two_running_preserves_saved_bus_owner(tmp_path):
    """Если до restart FIFO-владелец при двух RUNNING был достоверно известен и сохранён, новый процесс обязан восстановить его. Текущий снимок двух работающих двигателей не должен пересчитать owner заново."""

    app, fake = make_app(tmp_path)
    await app._tick(0.0)
    fake.states[ENTITIES["generator_a_running"]] = "on"
    fake.states[ENTITIES["generator_a_remote"]] = "on"
    await app._tick(1.0)
    fake.states[ENTITIES["generator_b_running"]] = "on"
    fake.states[ENTITIES["generator_b_remote"]] = "on"
    await app._tick(2.0)
    assert app.generator_bus.status().owner_slot == GeneratorSlot.A

    restored, restored_fake = _restart_app(tmp_path, fake.states)
    await restored._tick(3.0)

    assert restored.generator_bus.status().owner_slot == GeneratorSlot.A
    assert restored.generator_bus.status().owner == GeneratorBusOwner.A
    assert restored_fake.states[ENTITIES["generator_a_running"]] == "on"
    assert restored_fake.states[ENTITIES["generator_b_running"]] == "on"


@pytest.mark.asyncio
async def test_22_1_15_restart_with_two_running_without_history_keeps_owner_unknown(tmp_path):
    """Если после restart два генератора уже RUNNING, но достоверной истории FIFO нет, EnergyATS не имеет права угадывать владельца. Bus owner должен остаться UNKNOWN, а не зависеть от порядка обхода кода."""

    app, fake = make_app(tmp_path)
    fake.states[ENTITIES["generator_a_running"]] = "on"
    fake.states[ENTITIES["generator_a_remote"]] = "on"
    fake.states[ENTITIES["generator_b_running"]] = "on"
    fake.states[ENTITIES["generator_b_remote"]] = "on"

    await app._tick(0.0)

    assert app.generator_bus.status().owner == GeneratorBusOwner.UNKNOWN
    assert app.generator_bus.status().owner_slot is None


@pytest.mark.asyncio
async def test_22_1_16_single_external_outage_run_is_stopped_after_grid_restore(tmp_path):
    """Одиночный внешний генератор, достоверно запущенный во время outage, относится к outage-related cleanup. После устойчивого возврата Grid его можно штатно остановить, не превращая запуск задним числом в managed-сессию."""

    app, fake = make_app(
        tmp_path,
        grid_failure_delay=0,
        grid_restore_stable_time=0,
    )
    accelerate_generators(app)
    set_grid_outage(fake, automatic=False)
    await app._tick(0.0)

    fake.states[ENTITIES["generator_b_remote"]] = "on"
    fake.states[ENTITIES["generator_b_running"]] = "on"
    await app._tick(1.0)
    assert (
        app.generator_bus.status().run_contexts[GeneratorSlot.B]
        == GeneratorRunContext.OUTAGE_RELATED
    )

    fake.calls.clear()
    fake.states[ENTITIES["grid_ready"]] = "on"
    fake.states[ENTITIES["house_grid"]] = "on"
    now = 2.0
    for _ in range(10):
        await app._tick(now)
        if fake.states[ENTITIES["generator_b_remote"]] == "off":
            fake.states[ENTITIES["generator_b_running"]] = "off"
        if fake.states[ENTITIES["generator_b_running"]] == "off":
            break
        now += 1.0
    else:
        raise AssertionError("outage-related внешний Generator B не был остановлен")

    assert ("turn_off", ENTITIES["generator_b_remote"]) in switch_calls(fake)
    assert app.supervisor.session is None


@pytest.mark.asyncio
async def test_22_1_19_conflicting_house_feedback_forces_recovery_without_commands(tmp_path):
    """Одновременные подтверждения Grid и Generator являются противоречивым состоянием, а не допустимым источником. EnergyATS должен заблокировать активное управление и потребовать recovery без новых switch calls."""

    app, fake = make_app(tmp_path)
    await app._tick(0.0)
    fake.calls.clear()

    fake.states[ENTITIES["grid_power"]] = "on"
    fake.states[ENTITIES["source_generator"]] = "on"
    fake.states[ENTITIES["house_grid"]] = "on"
    fake.states[ENTITIES["house_generator"]] = "on"
    await app._tick(1.0)

    assert app.supervisor.phase == SupervisorPhase.RECOVERY_REQUIRED
    assert switch_calls(fake) == []


@pytest.mark.asyncio
async def test_22_1_20_generator_contactor_feedback_timeout_requires_recovery(tmp_path):
    """Успешный HA service call не является доказательством фактического переключения контактора. Если ожидаемая обратная связь не появляется до timeout, EnergyATS должен перейти в recovery."""

    journal = tmp_path / "state.json"
    app = EnergySupervisorApp(
        {
            **DEFAULT_OPTIONS,
            "armed": True,
            "state_file": str(journal),
            "grid_failure_delay": 0,
            "grid_restore_stable_time": 60,
            "transfer_confirmation_timeout": 2,
        },
        token="test",
    )
    fake = StuckGeneratorFeedbackFake(journal)
    fake.states = populated_states()
    fake.states[ENTITIES["ambient_temperature_external"]] = "20"
    attach_fake_client(app, fake)
    accelerate_generators(app)
    set_grid_outage(fake)

    now = 0.0
    for _ in range(30):
        await app._tick(now)
        if fake.states[ENTITIES["generator_a_remote"]] == "on":
            fake.states[ENTITIES["generator_a_running"]] = "on"
        if app.supervisor.phase == SupervisorPhase.RECOVERY_REQUIRED:
            break
        now += 1.0
    else:
        raise AssertionError("неподтверждённый generator contactor не вызвал recovery")

    assert ("turn_on", ENTITIES["source_generator"]) in switch_calls(fake)
    assert fake.states[ENTITIES["house_generator"]] == "off"
    assert app.supervisor.phase == SupervisorPhase.RECOVERY_REQUIRED


@pytest.mark.asyncio
async def test_22_1_21_grid_loss_during_return_cancels_return_and_keeps_generator(tmp_path):
    """Если Grid снова пропадает во время возврата с generator supply, исправный уже работающий генератор нельзя бессмысленно останавливать. Возврат отменяется, generator path восстанавливается без холодного перезапуска."""

    app, fake = make_app(
        tmp_path,
        grid_failure_delay=0,
        grid_restore_stable_time=0,
    )
    accelerate_generators(app)
    now = await drive_primary_a_to_house(app, fake)
    fake.calls.clear()

    fake.states[ENTITIES["grid_ready"]] = "on"
    await app._tick(now)
    now += 1.0
    assert app.supervisor.phase == SupervisorPhase.RETURNING_TO_GRID

    fake.states[ENTITIES["grid_ready"]] = "off"
    fake.states[ENTITIES["house_grid"]] = "off"
    for _ in range(5):
        await app._tick(now)
        if app.supervisor.phase == SupervisorPhase.ON_GENERATOR:
            break
        now += 1.0

    assert app.supervisor.phase == SupervisorPhase.ON_GENERATOR
    assert fake.states[ENTITIES["generator_a_running"]] == "on"
    assert ("turn_off", ENTITIES["generator_a_remote"]) not in switch_calls(fake)
    assert ("turn_on", ENTITIES["source_generator"]) in switch_calls(fake)


@pytest.mark.asyncio
async def test_22_1_22_emergency_stop_blocks_new_session_and_requires_recovery(tmp_path):
    """Активный Generators Emergency Stop должен блокировать новую managed-сессию на уровне всей App. Аппаратные команды не выдаются, а после E-stop требуется осознанный recovery."""

    app, fake = make_app(tmp_path)
    await app._tick(0.0)
    fake.calls.clear()

    fake.states[ENTITIES["emergency_stop"]] = "on"
    app.supervisor.request_manual_start()
    await app._tick(1.0)

    assert app.supervisor.phase == SupervisorPhase.RECOVERY_REQUIRED
    assert app.supervisor.session is None
    assert switch_calls(fake) == []
    assert not any(domain == "button" for domain, _service, _data in fake.calls)


@pytest.mark.asyncio
async def test_22_1_23_pending_transaction_restart_can_recover_on_confirmed_safe_grid(tmp_path):
    """Restart после незавершённой hardware transaction должен сначала заблокировать обычное управление. Явный recovery на подтверждённой безопасной Grid возвращает систему в NORMAL без слепого повторения старой команды."""

    app, _fake = make_app(tmp_path)
    app._pending_action_records = [
        {"controller": "power_transfer", "action": "disconnect_grid"}
    ]
    app._save_state(force=True)

    restored, fake = make_app(tmp_path)
    assert restored.supervisor.phase == SupervisorPhase.RECOVERY_REQUIRED
    fake.calls.clear()

    restored.supervisor.request_recovery_reset()
    await restored._tick(0.0)

    assert restored.supervisor.phase == SupervisorPhase.NORMAL
    assert restored.supervisor.recovery_reset_in_progress is False
    assert ("turn_off", ENTITIES["grid_power"]) not in switch_calls(fake)


@pytest.mark.asyncio
async def test_user_m9_external_takeover_then_manual_stop_requires_recovery(tmp_path):
    """После отказа managed PRIMARY внешний SECONDARY может принять шину, но остаётся внешним. Если пользователь затем останавливает и его, EnergyATS не имеет права сам запускать SECONDARY заново: требуется recovery."""

    app, fake = make_app(
        tmp_path,
        grid_failure_delay=0,
        grid_restore_stable_time=60,
    )
    accelerate_generators(app)
    now = await drive_primary_a_to_house(app, fake)

    fake.states[ENTITIES["generator_b_remote"]] = "on"
    fake.states[ENTITIES["generator_b_running"]] = "on"
    await app._tick(now)
    now += 1.0

    fake.states[ENTITIES["generator_a_running"]] = "off"
    await app._tick(now)
    now += 1.0
    assert app.generator_bus.status().owner_slot == GeneratorSlot.B
    assert app.supervisor.session is not None
    assert app.supervisor.session.generator == GeneratorSlot.A
    assert app.supervisor.session.external_takeover_observed is True

    fake.states[ENTITIES["generator_b_running"]] = "off"
    fake.states[ENTITIES["generator_b_remote"]] = "off"
    fake.calls.clear()
    await app._tick(now)

    assert app.supervisor.phase == SupervisorPhase.RECOVERY_REQUIRED
    assert ("turn_on", ENTITIES["generator_b_remote"]) not in switch_calls(fake)


@pytest.mark.asyncio
async def test_user_m10_missing_test_mode_helper_still_cleans_up_external_outage_run(tmp_path):
    """Если optional helper `generator_test_mode` вообще не установлен, новый внешний запуск во время outage не считается неопределённым TEST_RUN. Он классифицируется как outage-related и после возврата Grid должен быть штатно остановлен."""

    app, fake = make_app(
        tmp_path,
        grid_failure_delay=0,
        grid_restore_stable_time=0,
    )
    accelerate_generators(app)
    now = await drive_primary_a_to_house(app, fake)

    del fake.states[ENTITIES["test_mode"]]
    fake.states[ENTITIES["generator_b_remote"]] = "on"
    fake.states[ENTITIES["generator_b_running"]] = "on"
    await app._tick(now)
    now += 1.0

    assert (
        app.generator_bus.status().run_contexts[GeneratorSlot.B]
        == GeneratorRunContext.OUTAGE_RELATED
    )

    fake.calls.clear()
    fake.states[ENTITIES["grid_ready"]] = "on"
    for _ in range(30):
        await app._tick(now)
        for remote_key, running_key in (
            ("generator_a_remote", "generator_a_running"),
            ("generator_b_remote", "generator_b_running"),
        ):
            if fake.states[ENTITIES[remote_key]] == "off":
                fake.states[ENTITIES[running_key]] = "off"
        if (
            app.supervisor.session is None
            and fake.states[ENTITIES["house_grid"]] == "on"
            and fake.states[ENTITIES["generator_a_running"]] == "off"
            and fake.states[ENTITIES["generator_b_running"]] == "off"
        ):
            break
        now += 1.0
    else:
        raise AssertionError("M10: оба outage-related генератора не были остановлены")

    assert ("turn_off", ENTITIES["generator_b_remote"]) in switch_calls(fake)

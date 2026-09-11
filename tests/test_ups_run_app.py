"""UPS Run: реальные App/Supervisor/GC/TPC и HA-adapter с fake hardware.

Время задаётся тестом, контакторы подтверждаются fake HA, RUNNING меняется
отдельно от REMOTE. Короткие docstrings описывают защищаемое поведение.
"""

from dataclasses import replace
import json

import pytest

from domain import GeneratorSlot, SessionReason
from energy_supervisor import SupervisorPhase
from generator_controller import GeneratorPhase
from ha_adapter import ENTITIES
from ups_run import UPSRunState
from test_end_to_end_scenarios import (
    make_app,
    accelerate_generators,
    set_grid_outage,
    switch_calls,
    _restart_app,
)


def battery(fake, *, soc=70, ttg=180, discharging=True, ready=True):
    fake.states.update(
        {
            ENTITIES["ups_battery_soc"]: str(soc),
            ENTITIES["ups_battery_ttg_minutes"]: str(ttg),
            ENTITIES["ups_running_on_battery"]: "on" if discharging else "off",
            ENTITIES["ups_ready"]: "on" if ready else "off",
        }
    )


def setup(tmp_path, **options):
    app, fake = make_app(
        tmp_path,
        **{
            "grid_failure_delay": 2,
            "grid_restore_stable_time": 3,
            "delayed_generator_start_enabled": True,
            "generator_charge_cycle_enabled": True,
            "generator_max_start_delay": 100,
            **options,
        },
    )
    accelerate_generators(app)
    battery(fake)
    set_grid_outage(fake)
    return app, fake


async def wait_on_ups(app, fake):
    await app._tick(0)
    await app._tick(2)
    assert app.supervisor.session is None
    assert app.ups_run.state == UPSRunState.WAITING_ON_UPS


async def drive(app, fake, now, until, *, follow_running=True):
    for _ in range(60):
        await app._tick(now)
        if follow_running:
            for slot in ("a", "b"):
                fake.states[ENTITIES[f"generator_{slot}_running"]] = fake.states[
                    ENTITIES[f"generator_{slot}_remote"]
                ]
        if until():
            return now + 1
        now += 1
    raise AssertionError(
        f"Сценарий не завершился: {app.supervisor.phase}, {app.supervisor.recovery_reason}"
    )


async def on_generator(app, fake, now=3):
    battery(fake, soc=40)
    return await drive(
        app, fake, now, lambda: app.supervisor.phase == SupervisorPhase.ON_GENERATOR
    )


async def complete_cycle(app, fake, now):
    battery(fake, soc=80, ttg="unknown", discharging=False)
    now = await drive(app, fake, now, lambda: app.supervisor.session is None)
    battery(fake, soc=79)
    await app._tick(now)
    assert app.ups_run.state == UPSRunState.WAITING_ON_UPS
    return now + 1


def grid_returns(fake):
    fake.states[ENTITIES["grid_ready"]] = "on"
    if fake.states[ENTITIES["grid_power"]] == "on":
        fake.states[ENTITIES["house_grid"]] = "on"


@pytest.mark.asyncio
async def test_78_wait_on_ups_has_no_hardware_commands(tmp_path):
    """При достаточном запасе батареи подтверждённое отключение сети оставляет дом на UPS. EnergyATS не запускает генератор и не создаёт искусственный UPS-путь командами контакторам."""
    app, fake = setup(tmp_path)
    await wait_on_ups(app, fake)
    assert not switch_calls(fake)
    attrs = fake.state_writes[-1][2]
    assert attrs["source"] == "ups_only"
    assert attrs["delayed_start_elapsed_seconds"] == 0
    assert attrs["delayed_start_remaining_seconds"] == 100


@pytest.mark.parametrize("trigger", ["soc", "ttg", "max_delay"])
@pytest.mark.asyncio
async def test_79_81_each_start_threshold_runs_normal_transfer(tmp_path, trigger):
    """Каждый из трёх порогов независимо завершает ожидание: низкий SoC, малый TTG или максимальная задержка. Затем проходят обычные запуск, прогрев и подтверждённый перевод дома на генератор."""
    app, fake = setup(tmp_path)
    await wait_on_ups(app, fake)
    now = 3
    if trigger == "soc":
        battery(fake, soc=40)
    elif trigger == "ttg":
        battery(fake, ttg=60)
    else:
        now = 102
    await drive(
        app, fake, now, lambda: app.supervisor.phase == SupervisorPhase.ON_GENERATOR
    )
    assert fake.states[ENTITIES["house_generator"]] == "on"
    assert app.supervisor.session.cycle_owned
    assert app.supervisor.session.reason == SessionReason.GRID_OUTAGE


@pytest.mark.parametrize(
    "entity,value",
    [
        ("ups_battery_soc", "unavailable"),
        ("ups_battery_soc", "101"),
        ("ups_battery_ttg_minutes", "unknown"),
        ("ups_battery_ttg_minutes", "inf"),
        ("ups_running_on_battery", "unavailable"),
        ("ups_ready", "unknown"),
        ("ups_ready", "off"),
    ],
)
@pytest.mark.asyncio
async def test_82_83_invalid_or_critical_battery_starts_fail_safe(
    tmp_path, entity, value
):
    """Потеря необходимого батарейного сигнала или критическое состояние немедленно прекращает уже начатое ожидание. Core ATS остаётся работоспособным и запускает обычную автоматическую сессию."""
    app, fake = setup(tmp_path)
    await wait_on_ups(app, fake)
    fake.states[ENTITIES[entity]] = value
    await app._tick(3)
    assert app.supervisor.session is not None
    assert app.supervisor.phase == SupervisorPhase.STARTING_GENERATOR


@pytest.mark.asyncio
async def test_84_grid_return_during_wait_never_starts_generator(tmp_path):
    """Если сеть вернулась до запуска, дом продолжает работать от неё. Для завершения ожидания генератор не запускается даже при последующем низком SoC."""
    app, fake = setup(tmp_path)
    await wait_on_ups(app, fake)
    grid_returns(fake)
    battery(fake, soc=20)
    for now in (3, 6, 10):
        await app._tick(now)
    assert app.supervisor.session is None
    assert fake.states[ENTITIES["house_grid"]] == "on"
    assert not switch_calls(fake)
    assert app.ups_run.waiting_since is None


@pytest.mark.asyncio
async def test_85_manual_start_bypasses_wait_and_target(tmp_path):
    """Ручной запрос во время ожидания запускает генератор сразу и создаёт ручную сессию. Достижение целевого заряда такую сессию не останавливает."""
    app, fake = setup(tmp_path)
    await wait_on_ups(app, fake)
    app.supervisor.request_manual_start()
    battery(fake, soc=90)
    now = await drive(
        app, fake, 3, lambda: app.supervisor.phase == SupervisorPhase.ON_GENERATOR
    )
    await app._tick(now)
    assert app.supervisor.session.reason == SessionReason.MANUAL_GENERATOR_START
    assert not app.supervisor.session.cycle_owned
    assert app.supervisor.phase == SupervisorPhase.ON_GENERATOR


@pytest.mark.asyncio
async def test_86_target_releases_load_then_cools_and_stops(tmp_path):
    """По целевому SoC дом сначала отключается от генератора с подтверждением feedback. Только после снятия нагрузки проходит полный cooldown и снимается REMOTE; дом остаётся на UPS."""
    app, fake = setup(tmp_path)
    app.generator_controllers[GeneratorSlot.A].profile = replace(
        app.generator_controllers[GeneratorSlot.A].profile, cooldown_seconds=5
    )
    now = await on_generator(app, fake)
    fake.calls.clear()
    battery(fake, soc=80, discharging=False)
    await app._tick(now)
    assert switch_calls(fake) == [("turn_off", ENTITIES["source_generator"])]
    assert fake.states[ENTITIES["generator_a_remote"]] == "on"
    await app._tick(now + 1)
    assert (
        app.generator_controllers[GeneratorSlot.A].phase == GeneratorPhase.COOLING_DOWN
    )
    await app._tick(now + 5)
    assert fake.states[ENTITIES["generator_a_remote"]] == "on"
    await app._tick(now + 6)
    assert fake.states[ENTITIES["generator_a_remote"]] == "off"
    fake.states[ENTITIES["generator_a_running"]] = "off"
    await app._tick(now + 7)
    assert app.supervisor.session is None
    assert fake.states[ENTITIES["house_generator"]] == "off"
    assert fake.states[ENTITIES["house_grid"]] == "off"
    assert all(fake.pending_seen_before_hardware)


@pytest.mark.parametrize("trigger", ["soc", "ttg", "max_delay"])
@pytest.mark.asyncio
async def test_87_next_cycle_waits_even_without_initial_delayed_start(
    tmp_path, trigger
):
    """Cycling работает независимо от задержки первого запуска. После остановки новый интервал UPS не вызывает немедленного перезапуска; следующий цикл начинается по одному из тех же трёх порогов."""
    app, fake = setup(tmp_path, delayed_generator_start_enabled=False)
    now = await on_generator(app, fake)
    now = await complete_cycle(app, fake, now)
    wait_start = app.ups_run.waiting_since
    assert fake.states[ENTITIES["generator_a_remote"]] == "off"
    if trigger == "soc":
        battery(fake, soc=40)
    elif trigger == "ttg":
        battery(fake, ttg=60)
    else:
        now = wait_start + 100
    await drive(
        app, fake, now, lambda: app.supervisor.phase == SupervisorPhase.ON_GENERATOR
    )
    assert app.supervisor.session.cycle_owned
    assert (
        sum(
            call == ("turn_on", ENTITIES["generator_a_remote"])
            for call in switch_calls(fake)
        )
        == 2
    )


@pytest.mark.asyncio
async def test_88_grid_return_during_charge_does_not_wait_for_target(tmp_path):
    """Стабильное восстановление сети заканчивает зарядный цикл, даже если целевой заряд ещё не достигнут. Дом возвращается на сеть, затем генератор останавливается штатно."""
    app, fake = setup(tmp_path)
    now = await on_generator(app, fake)
    battery(fake, soc=50, discharging=False)
    grid_returns(fake)
    await drive(app, fake, now, lambda: app.supervisor.session is None)
    assert fake.states[ENTITIES["house_grid"]] == "on"
    assert fake.states[ENTITIES["generator_a_remote"]] == "off"


@pytest.mark.asyncio
async def test_89_disabled_cycling_keeps_generator_at_target(tmp_path):
    """Delayed Start можно использовать без циклической остановки. После первого автоматического запуска генератор продолжает питать дом и при заряде выше Target SoC."""
    app, fake = setup(tmp_path, generator_charge_cycle_enabled=False)
    now = await on_generator(app, fake)
    battery(fake, soc=95, discharging=False)
    await app._tick(now)
    assert app.supervisor.phase == SupervisorPhase.ON_GENERATOR
    assert not app.supervisor.session.cycle_owned


@pytest.mark.asyncio
async def test_90_manual_override_at_target_keeps_supply(tmp_path):
    """Ручной запрос на том же шаге, где батарея достигла цели, имеет приоритет над cycling. Дом остаётся на генераторе, а сессия сохраняет признак ручного управления."""
    app, fake = setup(tmp_path)
    now = await on_generator(app, fake)
    battery(fake, soc=90, discharging=False)
    app.supervisor.request_manual_start()
    await app._tick(now)
    assert app.supervisor.phase == SupervisorPhase.ON_GENERATOR
    assert app.supervisor.session.manual_override
    assert not app.supervisor.session.cycle_owned


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.asyncio
async def test_91_94_external_run_is_never_cycle_owned(tmp_path, restart):
    """Внешний генератор при отсутствии сети не становится собственностью cycling по подходящим батарейным данным. Это сохраняется после перезапуска: нет команды остановки или снятия дома с генератора по Target SoC."""
    app, fake = setup(tmp_path)
    fake.states.update(
        {
            ENTITIES["generator_a_running"]: "on",
            ENTITIES["generator_a_remote"]: "on",
            ENTITIES["source_generator"]: "on",
            ENTITIES["house_generator"]: "on",
            ENTITIES["grid_power"]: "off",
        }
    )
    await app._tick(0)
    if restart:
        app, fake = _restart_app(tmp_path, fake.states, **app.options)
        accelerate_generators(app)
    battery(fake, soc=95, discharging=False)
    fake.calls.clear()
    await app._tick(10)
    assert app.supervisor.session is None
    assert not switch_calls(fake)
    assert fake.states[ENTITIES["house_generator"]] == "on"


@pytest.mark.asyncio
async def test_92_restart_wait_preserves_elapsed_time(tmp_path):
    """Перезапуск во время ожидания не запускает генератор без причины и не обнуляет таймер. При достижении исходного предела ожидания запускается обычная сессия."""
    app, fake = setup(tmp_path)
    await wait_on_ups(app, fake)
    app, fake = _restart_app(tmp_path, fake.states, **app.options)
    await app._tick(90)
    assert app.supervisor.session is None
    assert app.ups_run.waiting_since == 2
    await app._tick(102)
    assert app.supervisor.session is not None


@pytest.mark.parametrize("manual", [False, True])
@pytest.mark.asyncio
async def test_93_94_restart_preserves_cycle_or_manual_ownership(tmp_path, manual):
    """После restart автоматическая cycle-owned сессия может штатно завершить заряд. Ручной override также сохраняется и запрещает остановку по Target SoC."""
    app, fake = setup(tmp_path)
    now = await on_generator(app, fake)
    if manual:
        app.supervisor.request_manual_start()
        await app._tick(now)
        now += 1
    app, fake = _restart_app(tmp_path, fake.states, **app.options)
    accelerate_generators(app)
    await app._tick(now)
    assert app.supervisor.phase == SupervisorPhase.ON_GENERATOR
    assert app.supervisor.session.cycle_owned is (not manual)
    battery(fake, soc=90, discharging=False)
    if manual:
        await app._tick(now + 1)
        assert app.supervisor.phase == SupervisorPhase.ON_GENERATOR
    else:
        await complete_cycle(app, fake, now + 1)


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.asyncio
async def test_grid_after_completed_cycle_reconnects_house(tmp_path, restart):
    """После завершённого цикла дом изолирован от сети, поэтому при её устойчивом возврате EnergyATS должен вернуть сетевой ввод. Обязанность сохраняется после restart в межцикловом ожидании."""
    app, fake = setup(tmp_path)
    now = await complete_cycle(app, fake, await on_generator(app, fake))
    if restart:
        app, fake = _restart_app(tmp_path, fake.states, **app.options)
    grid_returns(fake)
    await app._tick(now)
    assert fake.states[ENTITIES["grid_power"]] == "off"
    await drive(app, fake, now + 1, lambda: fake.states[ENTITIES["house_grid"]] == "on")
    assert app.supervisor.session is None
    assert fake.states[ENTITIES["generator_a_remote"]] == "off"


@pytest.mark.asyncio
async def test_grid_blip_does_not_restart_wait_clock(tmp_path):
    """Краткое появление сети не считается устойчивым восстановлением и не обнуляет уже прошедшее ожидание. Максимальная задержка по-прежнему отсчитывается от исходного outage."""
    app, fake = setup(tmp_path)
    await wait_on_ups(app, fake)
    grid_returns(fake)
    await app._tick(90)
    set_grid_outage(fake)
    await app._tick(91)
    await app._tick(102)
    assert app.supervisor.session is not None


@pytest.mark.asyncio
async def test_stop_timeout_during_cycle_requires_recovery(tmp_path):
    """Если после снятия REMOTE генератор не остановился, завершение цикла не должно зависать бесконечно. EnergyATS сообщает Recovery и не запускает второй генератор."""
    app, fake = setup(tmp_path)
    now = await on_generator(app, fake)
    battery(fake, soc=80, discharging=False)
    await drive(
        app,
        fake,
        now,
        lambda: app.supervisor.phase == SupervisorPhase.RECOVERY_REQUIRED,
        follow_running=False,
    )
    assert fake.states[ENTITIES["generator_b_remote"]] == "off"


@pytest.mark.asyncio
async def test_manual_override_after_cycle_engine_stopped_restarts_same_generator(
    tmp_path,
):
    """Если ручной запрос пришёл после фактической остановки двигателя, но до завершения cycle-сессии, нужен штатный новый запуск этого генератора. Такая гонка не должна ошибочно вызывать fallback на SECONDARY."""
    app, fake = setup(tmp_path)
    now = await on_generator(app, fake)
    battery(fake, soc=80, discharging=False)
    now = await drive(
        app, fake, now, lambda: fake.states[ENTITIES["generator_a_remote"]] == "off"
    )
    app.supervisor.request_manual_start()
    await drive(
        app,
        fake,
        now,
        lambda: app.supervisor.phase == SupervisorPhase.ON_GENERATOR
        and fake.states[ENTITIES["house_generator"]] == "on",
    )
    assert app.supervisor.session.generator == GeneratorSlot.A
    assert not app.supervisor.session.fallback_used
    assert app.supervisor.session.manual_override


@pytest.mark.parametrize("discharging", [True, False])
@pytest.mark.asyncio
async def test_stale_soc_cannot_be_masked_by_new_ttg(tmp_path, discharging):
    """Обновляющийся TTG не делает зависший SoC достоверным. Устаревший SoC прекращает ожидание и при разряде, и при заявленном режиме зарядки/поддержания."""
    app, fake = setup(tmp_path)
    app.ups_run.config = replace(app.ups_run.config, telemetry_stale_time=3)
    battery(fake, discharging=discharging)
    await wait_on_ups(app, fake)
    fake.states[ENTITIES["ups_battery_ttg_minutes"]] = "170"
    await app._tick(4)
    assert app.supervisor.session is not None
    assert "SoC" in app.ups_run.last_reason


@pytest.mark.asyncio
async def test_stale_ttg_cannot_be_masked_by_new_soc(tmp_path):
    """Свежий SoC не скрывает остановившийся TTG во время разряда. Без достоверного прогноза времени работы ожидание прекращается штатным запуском."""
    app, fake = setup(tmp_path)
    app.ups_run.config = replace(app.ups_run.config, telemetry_stale_time=3)
    await wait_on_ups(app, fake)
    fake.states[ENTITIES["ups_battery_soc"]] = "69"
    await app._tick(4)
    assert app.supervisor.session is not None
    assert "TTG" in app.ups_run.last_reason


@pytest.mark.asyncio
async def test_cached_old_battery_is_not_fresh_after_restart(tmp_path):
    """Повторное получение старого HA cache после restart не является новым измерением батареи. Устаревший timestamp SoC отменяет ожидание, даже если новый процесс только начал наблюдение."""
    app, fake = setup(tmp_path)
    await wait_on_ups(app, fake)
    app, fake = _restart_app(tmp_path, fake.states, **app.options)
    fake.get_state_updated_at = lambda entity: -400.0
    await app._tick(3)
    assert app.supervisor.session is not None
    assert "устарела" in app.ups_run.last_reason


@pytest.mark.asyncio
async def test_stale_target_does_not_stop_generator(tmp_path):
    """Старое значение SoC выше цели не даёт права снять питание дома с генератора. Только cycling становится DEGRADED; действующая ATS-сессия продолжает работу."""
    app, fake = setup(tmp_path)
    now = await on_generator(app, fake)
    battery(fake, soc=90, discharging=False)
    fake.get_state_updated_at = lambda entity: now - 400
    await app._tick(now)
    assert app.ups_run.state == UPSRunState.DEGRADED
    assert app.supervisor.phase == SupervisorPhase.ON_GENERATOR
    assert fake.states[ENTITIES["house_generator"]] == "on"


@pytest.mark.parametrize("ttg", ["unknown", "inf", "0"])
@pytest.mark.asyncio
async def test_ttg_not_used_during_charge_or_float(tmp_path, ttg):
    """Во время зарядки/поддержания TTG не служит порогом запуска и может быть неизвестным или бесконечным. При свежем достаточном SoC и готовой UPS система продолжает ожидать."""
    app, fake = setup(tmp_path)
    battery(fake, ttg=ttg, discharging=False)
    await wait_on_ups(app, fake)
    json.dumps(fake.state_writes[-1][2], allow_nan=False)


@pytest.mark.parametrize("blocked", ["emergency", "disarmed", "automatic_off"])
@pytest.mark.asyncio
async def test_critical_battery_does_not_override_control_blocks(tmp_path, blocked):
    """Критическая батарея отменяет экономию топлива, но не даёт права обойти E-stop, DISARMED или выключенную автоматику. В каждом таком режиме новых аппаратных команд запуска нет."""
    app, fake = setup(tmp_path, armed=blocked != "disarmed")
    battery(fake, soc=20, ready=False)
    if blocked == "emergency":
        fake.states[ENTITIES["emergency_stop"]] = "on"
    if blocked == "automatic_off":
        fake.states[ENTITIES["automatic_transfer"]] = "off"
    for now in (0, 2, 10):
        await app._tick(now)
    assert not switch_calls(fake)


@pytest.mark.asyncio
async def test_invalid_thresholds_disable_only_optimization(tmp_path):
    """Перепутанные Start/Target SoC отключают новую оптимизацию, сохраняя обычный ATS. Генератор запускается после стандартной задержки и не останавливается по неверной цели."""
    app, fake = setup(tmp_path, generator_start_soc=80, generator_target_charge_soc=40)
    now = await on_generator(app, fake)
    battery(fake, soc=90, discharging=False)
    await app._tick(now)
    assert app.ups_run.state == UPSRunState.DEGRADED
    assert app.supervisor.phase == SupervisorPhase.ON_GENERATOR
    assert not app.supervisor.session.cycle_owned


@pytest.mark.asyncio
async def test_cycle_waits_for_load_release_feedback(tmp_path):
    """Успешный service call отключения генераторной ветви не заменяет feedback. Пока снятие нагрузки не подтверждено, REMOTE остаётся включённым; timeout приводит к Recovery без запуска SECONDARY."""
    app, fake = setup(tmp_path, transfer_confirmation_timeout=3)
    now = await on_generator(app, fake)
    battery(fake, soc=80, discharging=False)
    await app._tick(now)
    fake.states[ENTITIES["house_generator"]] = "on"
    await drive(
        app,
        fake,
        now + 1,
        lambda: app.supervisor.phase == SupervisorPhase.RECOVERY_REQUIRED,
    )
    assert fake.states[ENTITIES["generator_a_remote"]] == "on"
    assert fake.states[ENTITIES["generator_b_remote"]] == "off"


@pytest.mark.asyncio
async def test_load_manager_ownership_survives_cycle_and_grid_return(tmp_path):
    """Cycling использует обычный Load Manager перед transfer и сохраняет его отключения на UPS. При последующем возврате сети восстанавливаются собственные G1/G2, а не теряется их ownership между циклами."""
    from load_manager import LoadGroup
    from test_load_manager_app import add_load_entities

    app, fake = setup(tmp_path, load_management_enabled=True)
    add_load_entities(fake, meter="off")
    now = await on_generator(app, fake)
    calls = switch_calls(fake)
    assert calls.index(("turn_off", ENTITIES["load_g1"])) < calls.index(
        ("turn_on", ENTITIES["source_generator"])
    )
    now = await complete_cycle(app, fake, now)
    assert all(app.load_manager.shed_by_energy_ats.values())
    fake.calls.clear()
    grid_returns(fake)
    await drive(
        app, fake, now, lambda: not any(app.load_manager.shed_by_energy_ats.values())
    )
    calls = switch_calls(fake)
    assert (
        calls.index(("turn_on", ENTITIES["grid_power"]))
        < calls.index(("turn_on", ENTITIES["load_g1"]))
        < calls.index(("turn_on", ENTITIES["load_g2"]))
    )
    assert app.load_manager.shed_by_energy_ats[LoadGroup.G1] is False


@pytest.mark.asyncio
async def test_disabling_cycling_between_runs_preserves_grid_restore(tmp_path):
    """Выключение оптимизации между циклами не снимает обязанность восстановить сетевой ввод, изолированный самим ATS. После устойчивого возврата сети дом снова получает Grid без запуска генератора."""
    app, fake = setup(tmp_path)
    now = await complete_cycle(app, fake, await on_generator(app, fake))
    app.ups_run.config = replace(
        app.ups_run.config,
        delayed_start_enabled=False,
        charge_cycle_enabled=False,
    )
    fake.states[ENTITIES["automatic_transfer"]] = "off"
    await app._tick(now)
    grid_returns(fake)
    await drive(app, fake, now + 1, lambda: fake.states[ENTITIES["house_grid"]] == "on")
    assert fake.states[ENTITIES["generator_a_remote"]] == "off"


@pytest.mark.asyncio
async def test_grid_return_during_cycle_cooldown_uses_outage_cleanup(tmp_path):
    """Возврат сети во время завершения цикла переводит остановку в обычный сценарий возврата Grid. Его причина остаётся автоматической, поэтому продолжают действовать правила outage cleanup."""
    app, fake = setup(tmp_path)
    now = await on_generator(app, fake)
    app.generator_controllers[GeneratorSlot.A].profile = replace(
        app.generator_controllers[GeneratorSlot.A].profile, cooldown_seconds=10
    )
    battery(fake, soc=80, discharging=False)
    await app._tick(now)
    grid_returns(fake)
    for t in range(now + 1, now + 5):
        await app._tick(t)
    assert app.supervisor.phase == SupervisorPhase.RETURNING_TO_GRID
    assert not app.supervisor.session.stop_requested
    await drive(app, fake, now + 5, lambda: app.supervisor.session is None)
    assert fake.states[ENTITIES["house_grid"]] == "on"

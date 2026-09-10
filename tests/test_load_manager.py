from domain import GeneratorSlot
from load_manager import (
    LoadActionKind,
    LoadGroup,
    LoadManager,
    LoadManagerConfig,
    LoadManagerObservation,
    LoadManagerPhase,
)


def config(**overrides):
    values = {
        "enabled": True,
        "measurement_stabilization_time": 2,
        "restore_margin_percent": 15,
        "nominal_overload_time": 3,
        "maximum_overload_confirmation_time": 1,
        "restore_retry_interval": 5,
    }
    values.update(overrides)
    return LoadManagerConfig(**values)


def observation(
    now=0,
    *,
    on_generator=False,
    on_grid=False,
    desired=False,
    ready=False,
    owner=GeneratorSlot.A,
    nominal=1000,
    maximum=1200,
    meter=True,
    power=500,
    sample=1,
    g1=True,
    g2=True,
    actions=True,
    transition=False,
):
    return LoadManagerObservation(
        now=now,
        house_on_generator=on_generator,
        house_on_grid=on_grid,
        desired_generator_supply=desired,
        managed_generator_ready=ready,
        power_transition_in_progress=transition,
        bus_owner=owner,
        nominal_power=nominal,
        maximum_power=maximum,
        meter_ready=meter,
        generator_power=power,
        power_sample_id=sample,
        groups={LoadGroup.G1: g1, LoadGroup.G2: g2},
        generator_name="Elemax",
        actions_enabled=actions,
    )


def stable_manager(**config_overrides):
    manager = LoadManager(config(**config_overrides))
    manager.step(observation(0, on_generator=True, power=800, sample=1))
    manager.step(observation(1, on_generator=True, power=800, sample=2))
    manager.step(observation(2, on_generator=True, power=800, sample=3))
    assert manager.phase == LoadManagerPhase.STABLE
    return manager


def test_disabled_never_commands_and_preserves_confirmed_ownership():
    """Выключенный Load Manager полностью исключён из control path, но не забывает уже подтверждённый собственный OFF."""
    manager = LoadManager(LoadManagerConfig(enabled=False))
    manager.shed_by_energy_ats[LoadGroup.G1] = True

    decision = manager.step(observation(on_grid=True, desired=True, ready=True))

    assert decision.actions == ()
    assert decision.transfer_permitted is True
    assert manager.phase == LoadManagerPhase.DISABLED
    assert manager.shed_by_energy_ats[LoadGroup.G1] is True


def test_disarmed_load_manager_never_creates_pending_or_load_command():
    """При общей DISARMED-блокировке Load Manager может наблюдать состояние, но не создаёт G1/G2-команды и не задерживает core ATS."""
    manager = LoadManager(config())

    decision = manager.step(
        observation(on_grid=True, desired=True, ready=True, actions=False)
    )

    assert decision.actions == ()
    assert decision.transfer_permitted is True
    assert manager.pending_action is None


def test_pretransfer_sheds_g1_then_g2_while_grid_still_connected():
    """Managed transfer отключает доступные G1/G2 по одной ещё на Grid и разрешает TPC только после подтверждения обоих OFF."""
    manager = LoadManager(config())

    first = manager.step(observation(0, on_grid=True, desired=True, ready=True))
    assert [(a.group, a.kind) for a in first.actions] == [
        (LoadGroup.G1, LoadActionKind.TURN_OFF)
    ]
    assert first.transfer_permitted is False

    second = manager.step(
        observation(1, on_grid=True, desired=True, ready=True, g1=False, sample=2)
    )
    assert manager.shed_by_energy_ats[LoadGroup.G1] is True
    assert [(a.group, a.kind) for a in second.actions] == [
        (LoadGroup.G2, LoadActionKind.TURN_OFF)
    ]
    assert second.transfer_permitted is False

    third = manager.step(
        observation(
            2,
            on_grid=True,
            desired=True,
            ready=True,
            g1=False,
            g2=False,
            sample=3,
        )
    )
    assert manager.shed_by_energy_ats[LoadGroup.G2] is True
    assert third.actions == ()
    assert third.transfer_permitted is True


def test_pretransfer_does_not_claim_group_that_was_already_off():
    """Группа, выключенная до Load Manager, не становится его собственным OFF и не получает будущего автоматического ON."""
    manager = LoadManager(config())

    manager.step(
        observation(0, on_grid=True, desired=True, ready=True, g1=False, g2=True)
    )
    manager.step(
        observation(
            1,
            on_grid=True,
            desired=True,
            ready=True,
            g1=False,
            g2=False,
            sample=2,
        )
    )

    assert manager.shed_by_energy_ats[LoadGroup.G1] is False
    assert manager.shed_by_energy_ats[LoadGroup.G2] is True


def test_pretransfer_unavailable_group_degrades_but_does_not_block_transfer():
    """Недоступная consumer group даёт локальный DEGRADED, но после обработки доступных групп не запрещает основной generator transfer."""
    manager = LoadManager(config())

    decision = manager.step(
        observation(0, on_grid=True, desired=True, ready=True, g1=None, g2=False)
    )

    assert decision.transfer_permitted is True
    assert decision.actions == ()
    assert manager.phase == LoadManagerPhase.DEGRADED
    assert decision.notifications


def test_unconfirmed_pretransfer_off_times_out_locally_and_releases_transfer():
    """Отсутствие feedback на OFF G1 не должно вечно держать ATS: после локального timeout Load Manager деградирует и core transfer разрешается."""
    manager = LoadManager(config())

    first = manager.step(
        observation(0, on_grid=True, desired=True, ready=True, g1=True, g2=False)
    )
    assert first.transfer_permitted is False

    waiting = manager.step(
        observation(
            1,
            on_grid=True,
            desired=True,
            ready=True,
            g1=True,
            g2=False,
            sample=2,
        )
    )
    assert waiting.transfer_permitted is False

    timed_out = manager.step(
        observation(
            2,
            on_grid=True,
            desired=True,
            ready=True,
            g1=True,
            g2=False,
            sample=3,
        )
    )
    assert timed_out.transfer_permitted is True
    assert manager.phase == LoadManagerPhase.DEGRADED


def test_missing_or_invalid_limits_only_degrade_load_manager():
    """Паспортные limits обязательны только для power-based Load Manager: core transfer gate остаётся открытым."""
    for nominal, maximum in (
        (None, None),
        (0, 1200),
        (1000, 0),
        (1300, 1200),
    ):
        manager = LoadManager(config())
        decision = manager.step(
            observation(
                0,
                on_generator=True,
                nominal=nominal,
                maximum=maximum,
            )
        )
        assert decision.transfer_permitted is True
        assert decision.actions == ()
        assert manager.phase == LoadManagerPhase.DEGRADED


def test_base_load_restores_g1_then_g2_with_separate_measurement_windows():
    """После transfer G1 и G2 возвращаются строго последовательно, и перед каждой следующей группой требуется новое окно свежих samples."""
    manager = LoadManager(config())
    manager.shed_by_energy_ats[LoadGroup.G1] = True
    manager.shed_by_energy_ats[LoadGroup.G2] = True

    assert not manager.step(
        observation(0, on_generator=True, power=500, sample=1, g1=False, g2=False)
    ).actions
    assert not manager.step(
        observation(1, on_generator=True, power=510, sample=2, g1=False, g2=False)
    ).actions
    restore_g1 = manager.step(
        observation(2, on_generator=True, power=505, sample=3, g1=False, g2=False)
    )
    assert [(a.group, a.kind) for a in restore_g1.actions] == [
        (LoadGroup.G1, LoadActionKind.TURN_ON)
    ]

    confirmed_g1 = manager.step(
        observation(3, on_generator=True, power=700, sample=4, g1=True, g2=False)
    )
    assert confirmed_g1.actions == ()
    assert not manager.step(
        observation(4, on_generator=True, power=710, sample=5, g1=True, g2=False)
    ).actions
    restore_g2 = manager.step(
        observation(5, on_generator=True, power=705, sample=6, g1=True, g2=False)
    )
    assert [(a.group, a.kind) for a in restore_g2.actions] == [
        (LoadGroup.G2, LoadActionKind.TURN_ON)
    ]


def test_restore_margin_blocks_unknown_additional_load():
    """Даже P ниже nominal недостаточно: если restore margin не выдержан, следующая неизвестная группа остаётся OFF."""
    manager = LoadManager(config())
    manager.shed_by_energy_ats[LoadGroup.G1] = True

    manager.step(observation(0, on_generator=True, power=900, sample=1, g1=False))
    manager.step(observation(1, on_generator=True, power=900, sample=2, g1=False))
    decision = manager.step(
        observation(2, on_generator=True, power=900, sample=3, g1=False)
    )

    assert decision.actions == ()
    assert manager.shed_by_energy_ats[LoadGroup.G1] is True
    assert manager.next_restore_retry == 7


def test_failed_g1_admission_reverts_g1_and_does_not_add_g2():
    """Если добавление G1 поднимает P выше nominal, G1 снимается сразу после stabilization, а G2 в этот cycle не включается."""
    manager = LoadManager(config())
    manager.shed_by_energy_ats[LoadGroup.G1] = True
    manager.shed_by_energy_ats[LoadGroup.G2] = True

    manager.step(
        observation(0, on_generator=True, power=500, sample=1, g1=False, g2=False)
    )
    manager.step(
        observation(1, on_generator=True, power=500, sample=2, g1=False, g2=False)
    )
    manager.step(
        observation(2, on_generator=True, power=500, sample=3, g1=False, g2=False)
    )

    manager.step(
        observation(3, on_generator=True, power=1100, sample=4, g1=True, g2=False)
    )
    manager.step(
        observation(4, on_generator=True, power=1110, sample=5, g1=True, g2=False)
    )
    decision = manager.step(
        observation(5, on_generator=True, power=1105, sample=6, g1=True, g2=False)
    )

    assert [(a.group, a.kind) for a in decision.actions] == [
        (LoadGroup.G1, LoadActionKind.TURN_OFF)
    ]
    assert all(a.group != LoadGroup.G2 for a in decision.actions)


def test_short_nominal_spike_does_not_shed():
    """Краткое превышение nominal короче timeout не должно дёргать consumer groups."""
    manager = stable_manager()

    assert not manager.step(
        observation(3, on_generator=True, power=1100, sample=4)
    ).actions
    assert not manager.step(
        observation(5, on_generator=True, power=900, sample=5)
    ).actions

    assert manager.phase == LoadManagerPhase.STABLE


def test_sustained_nominal_overload_sheds_g2_first():
    """Устойчивая nominal-overload сначала снимает G2 как группу меньшего приоритета."""
    manager = stable_manager()

    manager.step(observation(3, on_generator=True, power=1100, sample=4))
    manager.step(observation(5, on_generator=True, power=1100, sample=5))
    decision = manager.step(observation(6, on_generator=True, power=1100, sample=6))

    assert [(a.group, a.kind) for a in decision.actions] == [
        (LoadGroup.G2, LoadActionKind.TURN_OFF)
    ]


def test_shedding_waits_for_new_measurement_before_next_group():
    """После OFF G2 нельзя на том же измерении сразу выключить G1: требуется подтверждение OFF и новое stabilization window."""
    manager = stable_manager(nominal_overload_time=0)
    shed_g2 = manager.step(observation(3, on_generator=True, power=1100, sample=4))
    assert shed_g2.actions[0].group == LoadGroup.G2

    after_feedback = manager.step(
        observation(4, on_generator=True, power=1100, sample=5, g2=False)
    )
    assert after_feedback.actions == ()
    assert manager.phase == LoadManagerPhase.MEASURING_BASE_LOAD


def test_confirmed_maximum_overload_uses_shorter_confirmation():
    """P выше maximum использует отдельное короткое подтверждение и не ждёт nominal-overload timeout."""
    manager = stable_manager()

    assert not manager.step(
        observation(3, on_generator=True, power=1300, sample=4)
    ).actions
    decision = manager.step(
        observation(4, on_generator=True, power=1300, sample=5)
    )

    assert decision.actions[0].group == LoadGroup.G2
    assert decision.actions[0].kind == LoadActionKind.TURN_OFF


def test_all_groups_off_nominal_overload_warns_without_action():
    """Если управляемых нагрузок больше нет, Load Manager предупреждает, но не пытается выключать неизвестные потребители."""
    manager = LoadManager(config(nominal_overload_time=0))

    manager.step(
        observation(0, on_generator=True, power=1100, sample=1, g1=False, g2=False)
    )
    manager.step(
        observation(1, on_generator=True, power=1100, sample=2, g1=False, g2=False)
    )
    manager.step(
        observation(2, on_generator=True, power=1100, sample=3, g1=False, g2=False)
    )
    decision = manager.step(
        observation(3, on_generator=True, power=1100, sample=4, g1=False, g2=False)
    )

    assert decision.actions == ()
    assert decision.notifications
    assert any(event.level == "warning" for event in decision.events)


def test_all_groups_off_maximum_overload_is_critical_without_generator_action():
    """После полного shedding подтверждённое превышение maximum даёт critical event, но Load Manager не имеет команды остановки generator."""
    manager = LoadManager(config())

    manager.step(
        observation(0, on_generator=True, power=1300, sample=1, g1=False, g2=False)
    )
    manager.step(
        observation(1, on_generator=True, power=1300, sample=2, g1=False, g2=False)
    )
    manager.step(
        observation(2, on_generator=True, power=1300, sample=3, g1=False, g2=False)
    )
    decision = manager.step(
        observation(3, on_generator=True, power=1300, sample=4, g1=False, g2=False)
    )

    assert decision.actions == ()
    assert any(event.level == "critical" for event in decision.events)


def test_overload_shed_group_is_not_restored_before_retry_interval():
    """Hysteresis запрещает немедленный ON группы, только что снятой из-за overload."""
    manager = stable_manager(nominal_overload_time=0)
    shed = manager.step(observation(3, on_generator=True, power=1100, sample=4))
    assert shed.actions[0].group == LoadGroup.G2

    manager.step(observation(4, on_generator=True, power=500, sample=5, g2=False))
    manager.step(observation(5, on_generator=True, power=500, sample=6, g2=False))
    decision = manager.step(
        observation(6, on_generator=True, power=500, sample=7, g2=False)
    )

    assert decision.actions == ()
    assert manager.shed_by_energy_ats[LoadGroup.G2] is True
    assert manager.next_restore_retry == 8


def test_meter_failure_during_stable_operation_does_not_change_groups():
    """Потеря meter переводит только Load Manager в DEGRADED и не меняет устойчивое состояние G1/G2."""
    manager = stable_manager()

    decision = manager.step(
        observation(3, on_generator=True, meter=False, power=None, sample=None)
    )

    assert decision.actions == ()
    assert manager.phase == LoadManagerPhase.DEGRADED


def test_meter_recovery_requires_new_stabilization_window():
    """После восстановления meter первый sample не даёт права немедленно добавить ранее shed-группу."""
    manager = stable_manager()
    manager.shed_by_energy_ats[LoadGroup.G1] = True

    manager.step(
        observation(
            3,
            on_generator=True,
            meter=False,
            power=None,
            sample=None,
            g1=False,
        )
    )
    first = manager.step(
        observation(4, on_generator=True, power=500, sample=4, g1=False)
    )
    second = manager.step(
        observation(5, on_generator=True, power=500, sample=5, g1=False)
    )

    assert first.actions == ()
    assert second.actions == ()
    assert manager.phase == LoadManagerPhase.MEASURING_BASE_LOAD


def test_grid_restore_only_turns_on_energyats_owned_off():
    """На подтверждённой Grid автоматически возвращается только группа с shed_by_energy_ats=true; пользовательский OFF не трогается."""
    manager = LoadManager(config())
    manager.shed_by_energy_ats[LoadGroup.G2] = True

    decision = manager.step(observation(10, on_grid=True, g1=False, g2=False))

    assert [(a.group, a.kind) for a in decision.actions] == [
        (LoadGroup.G2, LoadActionKind.TURN_ON)
    ]


def test_manual_on_clears_off_ownership():
    """Если ранее shed-группа уже включена извне, Load Manager снимает своё OFF ownership и не считает её будущим объектом restore."""
    manager = LoadManager(config())
    manager.shed_by_energy_ats[LoadGroup.G1] = True

    decision = manager.step(
        observation(10, on_generator=True, g1=True, g2=True, power=500, sample=1)
    )

    assert manager.shed_by_energy_ats[LoadGroup.G1] is False
    assert decision.actions == ()


def test_owner_change_requires_new_measurement_under_new_limits():
    """Аппаратный takeover A→B немедленно сбрасывает старое измерительное окно; действия ждут samples уже с limits нового owner."""
    manager = stable_manager()
    manager.shed_by_energy_ats[LoadGroup.G1] = True

    decision = manager.step(
        observation(
            10,
            on_generator=True,
            owner=GeneratorSlot.B,
            nominal=600,
            maximum=700,
            power=400,
            sample=10,
            g1=False,
            g2=True,
        )
    )

    assert decision.actions == ()
    assert manager.phase == LoadManagerPhase.MEASURING_BASE_LOAD
    assert manager.active_nominal_power == 600
    assert manager.active_maximum_power == 700


def test_unknown_owner_is_local_degraded_and_keeps_current_groups():
    """UNKNOWN bus owner не позволяет выбрать паспортные limits и поэтому локально деградирует Load Manager без G1/G2-команд."""
    manager = stable_manager()

    decision = manager.step(
        observation(10, on_generator=True, owner=None, power=900, sample=10)
    )

    assert decision.actions == ()
    assert manager.phase == LoadManagerPhase.DEGRADED


def test_restart_preserves_off_ownership_but_not_old_measurement():
    """Persistent state сохраняет право восстановить собственный OFF, но старые power samples после restart не используются."""
    manager = stable_manager()
    manager.shed_by_energy_ats[LoadGroup.G2] = True

    restored = LoadManager.from_dict(manager.to_dict(), config())
    decision = restored.step(
        observation(
            10,
            on_generator=True,
            power=500,
            sample=99,
            g1=True,
            g2=False,
        )
    )

    assert restored.shed_by_energy_ats[LoadGroup.G2] is True
    assert decision.actions == ()
    assert restored.phase == LoadManagerPhase.MEASURING_BASE_LOAD

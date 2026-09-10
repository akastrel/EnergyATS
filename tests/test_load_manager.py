import pytest

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
    values = dict(
        enabled=True,
        measurement_stabilization_time=2,
        restore_margin_percent=15,
        nominal_overload_time=3,
        maximum_overload_confirmation_time=1,
        restore_retry_interval=5,
    )
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


def stable_manager(**overrides):
    manager = LoadManager(config(**overrides))
    for now, sample in ((0, 1), (1, 2), (2, 3)):
        manager.step(observation(now, on_generator=True, power=800, sample=sample))
    assert manager.phase == LoadManagerPhase.STABLE
    return manager


def action_pairs(decision):
    return [(action.group, action.kind) for action in decision.actions]


def test_disabled_never_commands_and_preserves_ownership():
    """Disabled полностью исключает Load Manager из control path, не стирая подтверждённый ownership."""
    manager = LoadManager(LoadManagerConfig(enabled=False))
    manager.shed_by_energy_ats[LoadGroup.G1] = True

    decision = manager.step(observation(on_grid=True, desired=True, ready=True))

    assert decision.actions == ()
    assert decision.transfer_permitted is True
    assert manager.phase == LoadManagerPhase.DISABLED
    assert manager.shed_by_energy_ats[LoadGroup.G1] is True


def test_disarmed_never_creates_load_command():
    """DISARMED может наблюдать состояние, но не создаёт G1/G2-команд и не gate-ит core ATS."""
    manager = LoadManager(config())
    decision = manager.step(observation(on_grid=True, desired=True, ready=True, actions=False))

    assert decision.actions == ()
    assert decision.transfer_permitted is True
    assert manager.pending_action is None


def test_pretransfer_sheds_g2_then_g1_and_waits_for_feedback():
    """Перед managed transfer меньший приоритет G2 снимается раньше G1; TPC разрешается только после feedback."""
    manager = LoadManager(config())

    first = manager.step(observation(0, on_grid=True, desired=True, ready=True))
    assert action_pairs(first) == [(LoadGroup.G2, LoadActionKind.TURN_OFF)]
    assert first.transfer_permitted is False

    second = manager.step(observation(1, on_grid=True, desired=True, ready=True, g2=False, sample=2))
    assert manager.shed_by_energy_ats[LoadGroup.G2] is True
    assert action_pairs(second) == [(LoadGroup.G1, LoadActionKind.TURN_OFF)]
    assert second.transfer_permitted is False

    third = manager.step(observation(2, on_grid=True, desired=True, ready=True, g1=False, g2=False, sample=3))
    assert manager.shed_by_energy_ats[LoadGroup.G1] is True
    assert third.actions == ()
    assert third.transfer_permitted is True


def test_pretransfer_does_not_claim_already_off_group():
    """Пользовательский OFF до начала сценария не превращается в shed_by_energy_ats."""
    manager = LoadManager(config())
    manager.step(observation(0, on_grid=True, desired=True, ready=True, g1=False, g2=True))
    manager.step(observation(1, on_grid=True, desired=True, ready=True, g1=False, g2=False, sample=2))

    assert manager.shed_by_energy_ats == {LoadGroup.G1: False, LoadGroup.G2: True}


def test_pretransfer_unavailable_group_is_local_degraded():
    """Недоступная consumer group даёт DEGRADED, но не блокирует generator transfer."""
    manager = LoadManager(config())
    decision = manager.step(observation(0, on_grid=True, desired=True, ready=True, g1=None, g2=False))

    assert decision.actions == ()
    assert decision.transfer_permitted is True
    assert manager.phase == LoadManagerPhase.DEGRADED
    assert decision.notifications


def test_unconfirmed_pretransfer_off_times_out_and_releases_transfer():
    """Отсутствие OFF feedback не может держать ATS бесконечно: локальный timeout освобождает transfer."""
    manager = LoadManager(config())
    assert manager.step(observation(0, on_grid=True, desired=True, ready=True, g1=True, g2=False)).transfer_permitted is False
    assert manager.step(observation(1, on_grid=True, desired=True, ready=True, g1=True, g2=False, sample=2)).transfer_permitted is False

    timed_out = manager.step(observation(2, on_grid=True, desired=True, ready=True, g1=True, g2=False, sample=3))
    assert timed_out.transfer_permitted is True
    assert manager.phase == LoadManagerPhase.DEGRADED


@pytest.mark.parametrize("nominal,maximum", [(None, None), (0, 1200), (1000, 0), (1300, 1200)])
def test_invalid_limits_only_degrade_load_manager(nominal, maximum):
    """Некорректные паспортные limits отключают только power-based policy, не core ATS."""
    manager = LoadManager(config())
    decision = manager.step(observation(0, on_generator=True, nominal=nominal, maximum=maximum))

    assert decision.actions == ()
    assert decision.transfer_permitted is True
    assert manager.phase == LoadManagerPhase.DEGRADED


def test_base_load_restores_g1_then_g2_with_separate_windows():
    """После transfer G1 и G2 возвращаются последовательно; после каждого ON требуется новое measurement window."""
    manager = LoadManager(config())
    manager.shed_by_energy_ats.update({LoadGroup.G1: True, LoadGroup.G2: True})

    manager.step(observation(0, on_generator=True, power=500, sample=1, g1=False, g2=False))
    manager.step(observation(1, on_generator=True, power=510, sample=2, g1=False, g2=False))
    restore_g1 = manager.step(observation(2, on_generator=True, power=505, sample=3, g1=False, g2=False))
    assert action_pairs(restore_g1) == [(LoadGroup.G1, LoadActionKind.TURN_ON)]

    assert manager.step(observation(3, on_generator=True, power=700, sample=4, g1=True, g2=False)).actions == ()
    assert manager.step(observation(4, on_generator=True, power=710, sample=5, g1=True, g2=False)).actions == ()
    restore_g2 = manager.step(observation(5, on_generator=True, power=705, sample=6, g1=True, g2=False))
    assert action_pairs(restore_g2) == [(LoadGroup.G2, LoadActionKind.TURN_ON)]


def test_restore_margin_blocks_next_group():
    """P ниже nominal недостаточно: без restore margin следующая группа остаётся OFF до retry."""
    manager = LoadManager(config())
    manager.shed_by_energy_ats[LoadGroup.G1] = True
    for now, sample in ((0, 1), (1, 2)):
        manager.step(observation(now, on_generator=True, power=900, sample=sample, g1=False))
    decision = manager.step(observation(2, on_generator=True, power=900, sample=3, g1=False))

    assert decision.actions == ()
    assert manager.shed_by_energy_ats[LoadGroup.G1] is True
    assert manager.next_restore_retry == 7


def test_failed_admission_reverts_same_group():
    """Если G1 после admission выводит P выше nominal, G1 снимается обратно и G2 в этот cycle не добавляется."""
    manager = LoadManager(config())
    manager.shed_by_energy_ats.update({LoadGroup.G1: True, LoadGroup.G2: True})
    for now, sample in ((0, 1), (1, 2), (2, 3)):
        manager.step(observation(now, on_generator=True, power=500, sample=sample, g1=False, g2=False))
    for now, power, sample in ((3, 1100, 4), (4, 1110, 5)):
        manager.step(observation(now, on_generator=True, power=power, sample=sample, g1=True, g2=False))
    decision = manager.step(observation(5, on_generator=True, power=1105, sample=6, g1=True, g2=False))

    assert action_pairs(decision) == [(LoadGroup.G1, LoadActionKind.TURN_OFF)]


def test_short_nominal_spike_does_not_shed():
    """Короткий spike выше nominal не должен дёргать consumer groups."""
    manager = stable_manager()
    assert not manager.step(observation(3, on_generator=True, power=1100, sample=4)).actions
    assert not manager.step(observation(5, on_generator=True, power=900, sample=5)).actions
    assert manager.phase == LoadManagerPhase.STABLE


def test_sustained_nominal_overload_sheds_g2_first():
    """Устойчивая nominal-overload снимает G2 первой."""
    manager = stable_manager()
    manager.step(observation(3, on_generator=True, power=1100, sample=4))
    manager.step(observation(5, on_generator=True, power=1100, sample=5))
    decision = manager.step(observation(6, on_generator=True, power=1100, sample=6))

    assert action_pairs(decision) == [(LoadGroup.G2, LoadActionKind.TURN_OFF)]


def test_shedding_requires_new_measurement_before_next_group():
    """После OFF G2 нельзя на старом P сразу снять G1: сначала начинается новое measurement window."""
    manager = stable_manager(nominal_overload_time=0)
    assert manager.step(observation(3, on_generator=True, power=1100, sample=4)).actions[0].group == LoadGroup.G2

    after_feedback = manager.step(observation(4, on_generator=True, power=1100, sample=5, g2=False))
    assert after_feedback.actions == ()
    assert manager.phase == LoadManagerPhase.MEASURING
    assert manager.operation == "after_shed"


def test_maximum_overload_uses_short_confirmation():
    """P выше maximum использует отдельный короткий confirmation timeout."""
    manager = stable_manager()
    assert not manager.step(observation(3, on_generator=True, power=1300, sample=4)).actions
    decision = manager.step(observation(4, on_generator=True, power=1300, sample=5))

    assert action_pairs(decision) == [(LoadGroup.G2, LoadActionKind.TURN_OFF)]


def test_all_groups_off_nominal_overload_warns_without_action():
    """Когда shedding больше невозможен, nominal overload даёт warning без неизвестных аппаратных действий."""
    manager = LoadManager(config(nominal_overload_time=0))
    for now, sample in ((0, 1), (1, 2), (2, 3)):
        manager.step(observation(now, on_generator=True, power=1100, sample=sample, g1=False, g2=False))
    decision = manager.step(observation(3, on_generator=True, power=1100, sample=4, g1=False, g2=False))

    assert decision.actions == ()
    assert decision.notifications
    assert any(event.level == "warning" for event in decision.events)


def test_all_groups_off_maximum_overload_is_critical_without_generator_action():
    """После полного shedding confirmed maximum overload даёт critical event, но не команду остановки generator."""
    manager = LoadManager(config())
    for now, sample in ((0, 1), (1, 2), (2, 3)):
        manager.step(observation(now, on_generator=True, power=1300, sample=sample, g1=False, g2=False))
    decision = manager.step(observation(3, on_generator=True, power=1300, sample=4, g1=False, g2=False))

    assert decision.actions == ()
    assert any(event.level == "critical" for event in decision.events)


def test_overload_shed_is_not_restored_before_retry():
    """Hysteresis запрещает немедленно вернуть только что снятую перегрузкой группу."""
    manager = stable_manager(nominal_overload_time=0)
    assert manager.step(observation(3, on_generator=True, power=1100, sample=4)).actions[0].group == LoadGroup.G2
    manager.step(observation(4, on_generator=True, power=500, sample=5, g2=False))
    manager.step(observation(5, on_generator=True, power=500, sample=6, g2=False))
    decision = manager.step(observation(6, on_generator=True, power=500, sample=7, g2=False))

    assert decision.actions == ()
    assert manager.shed_by_energy_ats[LoadGroup.G2] is True
    assert manager.next_restore_retry == 8


def test_meter_failure_is_local_degraded_and_keeps_groups():
    """Потеря meter во время работы не меняет G1/G2 и не создаёт core recovery."""
    manager = stable_manager()
    decision = manager.step(observation(3, on_generator=True, meter=False, power=None, sample=None))

    assert decision.actions == ()
    assert manager.phase == LoadManagerPhase.DEGRADED


def test_meter_recovery_requires_new_stabilization_window():
    """После восстановления meter первый sample не даёт права немедленно добавить shed-группу."""
    manager = stable_manager()
    manager.shed_by_energy_ats[LoadGroup.G1] = True
    manager.step(observation(3, on_generator=True, meter=False, power=None, sample=None, g1=False))
    assert manager.step(observation(4, on_generator=True, power=500, sample=4, g1=False)).actions == ()
    assert manager.step(observation(5, on_generator=True, power=500, sample=5, g1=False)).actions == ()

    assert manager.phase == LoadManagerPhase.MEASURING


def test_grid_restore_only_turns_on_owned_off():
    """После подтверждённой Grid автоматически включается только группа с shed_by_energy_ats=true."""
    manager = LoadManager(config())
    manager.shed_by_energy_ats[LoadGroup.G2] = True
    decision = manager.step(observation(10, on_grid=True, g1=False, g2=False))

    assert action_pairs(decision) == [(LoadGroup.G2, LoadActionKind.TURN_ON)]


def test_manual_on_clears_off_ownership():
    """Ручной ON ранее shed-группы снимает ownership текущего OFF."""
    manager = LoadManager(config())
    manager.shed_by_energy_ats[LoadGroup.G1] = True
    decision = manager.step(observation(10, on_generator=True, g1=True, power=500, sample=1))

    assert manager.shed_by_energy_ats[LoadGroup.G1] is False
    assert decision.actions == ()


def test_owner_change_requires_new_measurement_under_new_limits():
    """Takeover A→B сбрасывает старое окно и требует samples уже с limits нового owner."""
    manager = stable_manager()
    manager.shed_by_energy_ats[LoadGroup.G1] = True
    decision = manager.step(
        observation(10, on_generator=True, owner=GeneratorSlot.B, nominal=600, maximum=700, power=400, sample=10, g1=False)
    )

    assert decision.actions == ()
    assert manager.phase == LoadManagerPhase.MEASURING
    assert manager.active_nominal_power == 600
    assert manager.active_maximum_power == 700


def test_unknown_owner_is_local_degraded():
    """UNKNOWN owner не позволяет выбрать limits и локально деградирует policy без G1/G2-команд."""
    manager = stable_manager()
    decision = manager.step(observation(10, on_generator=True, owner=None, power=900, sample=10))

    assert decision.actions == ()
    assert manager.phase == LoadManagerPhase.DEGRADED


def test_restart_preserves_ownership_but_not_old_measurement():
    """Restart сохраняет подтверждённый OFF ownership, но power-based решение доказывается новым окном."""
    manager = stable_manager()
    manager.shed_by_energy_ats[LoadGroup.G2] = True

    restored = LoadManager.from_dict(manager.to_dict(), config())
    decision = restored.step(observation(10, on_generator=True, power=500, sample=99, g2=False))

    assert restored.shed_by_energy_ats[LoadGroup.G2] is True
    assert decision.actions == ()
    assert restored.phase == LoadManagerPhase.MEASURING

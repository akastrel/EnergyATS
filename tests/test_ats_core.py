from dataclasses import replace

from ats_core import ATSController, Config, Snapshot, Phase


def snap(**kw):
    base = Snapshot(
        grid_ready=True,
        house_grid=True,
        house_generator=False,
        generator_a_running=False,
        generator_b_running=False,
        emergency_stop=False,
        garage_temperature=20.0,
        remote_a=False,
        remote_b=False,
        grid_disconnected=False,
        source_generator=False,
        ats_enabled=True,
        session_active=False,
        session_mode="none",
    )
    return replace(base, **kw)


def kinds(actions):
    return [(a.kind, a.target, a.value) for a in actions]


def first_boot(c, s, now=0):
    c.step(now, s)


def test_happy_path_generator_a_and_return_to_grid():
    cfg = Config(grid_failure_delay=5, generator_start_timeout=90,
                 preheat_warm_seconds=30, grid_restore_stable_time=60,
                 generator_stop_delay=300, generator_stop_timeout=90,
                 transfer_confirmation_timeout=60, choke_temperature=10,
                 generator_a_choke_mode="temperature")
    c = ATSController(cfg)
    s = snap()
    first_boot(c, s)
    assert c.phase == Phase.GRID

    # Grid disappears; debounce first.
    s = replace(s, grid_ready=False, house_grid=False)
    c.step(1, s)
    assert c.phase == Phase.GRID_FAILURE_DELAY
    assert not any(a.kind == "switch_on" and a.target == "switch.generator_a_remote_start" for a in c.step(5, s))

    actions = c.step(6, s)
    assert c.phase == Phase.STARTING
    assert ("switch_on", "switch.generator_a_remote_start", None) in kinds(actions)
    # +20 C => no choke, mechanically reversed button Close = physically open.
    assert ("button", "button.generator_a_choke_close", None) in kinds(actions)

    # A starts; warm preheat 30 s.
    s = replace(s, generator_a_running=True, remote_a=True, session_active=True, session_mode="automatic")
    c.step(10, s)
    assert c.phase == Phase.PREHEATING
    assert c.deadline == 40

    # Preheat completes while Grid still absent -> disconnect Grid.
    actions = c.step(40, s)
    assert c.phase == Phase.TRANSFER_DISCONNECT_GRID
    assert ("switch_off", "switch.grid_power", None) in kinds(actions)

    # Grid isolation confirmed -> select generator.
    s = replace(s, grid_disconnected=True, source_generator=False, house_grid=False)
    actions = c.step(41, s)
    assert c.phase == Phase.TRANSFER_SELECT_GENERATOR
    assert ("switch_on", "switch.use_generator_as_power_source", None) in kinds(actions)

    # Generator supply confirmed.
    s = replace(s, source_generator=True, house_generator=True)
    c.step(42, s)
    assert c.phase == Phase.ON_GENERATOR

    # Grid returns, must remain stable for full minute.
    s = replace(s, grid_ready=True)
    c.step(100, s)
    assert c.phase == Phase.GRID_RESTORE_WAIT
    c.step(159, s)
    assert c.phase == Phase.GRID_RESTORE_WAIT
    actions = c.step(160, s)
    assert c.phase == Phase.RETURN_SELECT_GRID
    assert ("switch_off", "switch.use_generator_as_power_source", None) in kinds(actions)

    # Generator feed gone -> connect Grid.
    s = replace(s, source_generator=False, house_generator=False)
    actions = c.step(161, s)
    assert c.phase == Phase.RETURN_CONNECT_GRID
    assert ("switch_on", "switch.grid_power", None) in kinds(actions)

    # House on Grid -> 5 min cooldown.
    s = replace(s, grid_disconnected=False, house_grid=True)
    c.step(162, s)
    assert c.phase == Phase.COOLDOWN
    assert c.deadline == 462

    actions = c.step(462, s)
    assert c.phase == Phase.STOPPING
    assert ("switch_off", "switch.generator_a_remote_start", None) in kinds(actions)

    s = replace(s, generator_a_running=False, remote_a=False)
    actions = c.step(463, s)
    assert c.phase == Phase.GRID
    assert ("set_session", None, "off") in kinds(actions)


def test_cold_start_uses_choke_then_opens_after_10_seconds():
    c = ATSController(Config(grid_failure_delay=0, choke_temperature=10,
                             generator_a_choke_mode="temperature", choke_hold_time=10,
                             preheat_cold_seconds=180))
    s = snap(grid_ready=False, house_grid=False, garage_temperature=-7)
    first_boot(c, s)
    c.step(1, s)  # enter delay
    actions = c.step(2, s)  # start
    assert ("button", "button.generator_a_choke_open", None) in kinds(actions)  # physically closes choke

    s = replace(s, generator_a_running=True, remote_a=True, session_active=True, session_mode="automatic")
    c.step(5, s)
    assert c.phase == Phase.CHOKE_HOLD
    assert c.deadline == 15
    actions = c.step(15, s)
    assert ("button", "button.generator_a_choke_close", None) in kinds(actions)  # physically opens choke
    assert c.phase == Phase.PREHEATING
    assert c.deadline == 195  # -7 C -> 3 min


def test_grid_returns_during_preheat_and_flaps_before_60_seconds():
    c = ATSController(Config(grid_failure_delay=0, preheat_warm_seconds=30,
                             grid_restore_stable_time=60,
                             generator_a_choke_mode="temperature"))
    s = snap(grid_ready=False, house_grid=False)
    first_boot(c, s)
    c.step(1, s)
    c.step(2, s)
    s = replace(s, generator_a_running=True, remote_a=True, session_active=True, session_mode="automatic")
    c.step(3, s)
    assert c.phase == Phase.PREHEATING

    # Grid appears 10 s into preheat.
    s = replace(s, grid_ready=True, house_grid=True)
    c.step(13, s)
    # Preheat ends but Grid has not been stable 60 s -> wait, do not transfer.
    c.step(33, s)
    assert c.phase == Phase.WAIT_GRID_DECISION

    # Grid drops again at only 30 s stable -> immediately continue transfer.
    s = replace(s, grid_ready=False, house_grid=False)
    actions = c.step(43, s)
    assert c.phase == Phase.TRANSFER_DISCONNECT_GRID
    assert ("switch_off", "switch.grid_power", None) in kinds(actions)


def test_grid_stable_during_preheat_cancels_transfer_and_cools_generator():
    c = ATSController(Config(grid_failure_delay=0, preheat_warm_seconds=120,
                             grid_restore_stable_time=60, generator_stop_delay=300,
                             generator_a_choke_mode="temperature"))
    s = snap(grid_ready=False, house_grid=False)
    first_boot(c, s)
    c.step(1, s)
    c.step(2, s)
    s = replace(s, generator_a_running=True, remote_a=True, session_active=True, session_mode="automatic")
    c.step(3, s)

    s = replace(s, grid_ready=True, house_grid=True)
    c.step(10, s)
    actions = c.step(70, s)
    assert c.phase == Phase.COOLDOWN
    assert any(a.kind == "notify_warning" for a in actions)
    assert not any(a.target == "switch.grid_power" and a.kind == "switch_off" for a in actions)


def test_a_start_failure_falls_back_to_b():
    c = ATSController(Config(grid_failure_delay=0, generator_start_timeout=90))
    s = snap(grid_ready=False, house_grid=False)
    first_boot(c, s)
    c.step(1, s)
    c.step(2, s)
    assert c.active_generator == "A"
    actions = c.step(92, s)
    assert c.active_generator == "B"
    assert c.phase == Phase.STARTING
    assert any(a.kind == "notify_critical" and "A" in a.message for a in actions)
    assert ("switch_on", "switch.generator_b_remote_start", None) in kinds(actions)


def test_b_failure_after_a_failure_is_terminal_and_engages_estop():
    c = ATSController(Config(grid_failure_delay=0, generator_start_timeout=10))
    s = snap(grid_ready=False, house_grid=False)
    first_boot(c, s)
    c.step(1, s)
    c.step(2, s)
    c.step(12, s)  # A fails -> B starts
    assert c.active_generator == "B"
    actions = c.step(22, s)  # B fails
    assert c.phase == Phase.TERMINAL
    assert ("switch_on", "switch.generators_emergency_stop", None) in kinds(actions)
    assert ("switch_off", "switch.use_generator_as_power_source", None) in kinds(actions)
    assert ("switch_on", "switch.grid_power", None) in kinds(actions)


def test_running_generator_loses_house_power_for_a_minute_is_terminal():
    c = ATSController(Config(transfer_confirmation_timeout=60))
    c.bootstrapped = True
    c.phase = Phase.ON_GENERATOR
    c.active_generator = "A"
    c.session_mode = "automatic"
    s = snap(grid_ready=False, house_grid=False, house_generator=True,
             generator_a_running=True, remote_a=True, grid_disconnected=True,
             source_generator=True, session_active=True, session_mode="automatic")
    c.step(0, s)
    s = replace(s, house_generator=False)
    c.step(1, s)
    actions = c.step(61, s)
    assert c.phase == Phase.TERMINAL
    assert ("switch_on", "switch.generators_emergency_stop", None) in kinds(actions)


def test_generator_stop_timeout_uses_emergency_stop():
    c = ATSController(Config(generator_stop_timeout=90))
    c.bootstrapped = True
    c.phase = Phase.STOPPING
    c.active_generator = "A"
    c.session_mode = "automatic"
    c.deadline = 90
    s = snap(generator_a_running=True, remote_a=False, session_active=True, session_mode="automatic")
    actions = c.step(90, s)
    assert c.phase == Phase.TERMINAL
    assert ("switch_on", "switch.generators_emergency_stop", None) in kinds(actions)


def test_restart_on_generator_recovers_from_physical_feedback():
    c = ATSController(Config())
    s = snap(grid_ready=False, house_grid=False, house_generator=True,
             generator_a_running=True, remote_a=True, grid_disconnected=True,
             source_generator=True, session_active=True, session_mode="automatic")
    actions = c.step(0, s)
    assert c.phase == Phase.ON_GENERATOR
    assert c.active_generator == "A"
    assert not any(a.kind.startswith("switch_") for a in actions)


def test_restart_during_preheat_gives_full_preheat_again():
    c = ATSController(Config(preheat_warm_seconds=30))
    s = snap(grid_ready=False, house_grid=False, house_generator=False,
             generator_a_running=True, remote_a=True, session_active=True,
             session_mode="automatic", source_generator=False, grid_disconnected=False)
    c.step(100, s)
    assert c.phase == Phase.PREHEATING
    assert c.deadline == 130


def test_external_manual_generator_is_never_seized():
    c = ATSController(Config())
    s = snap(generator_a_running=True, remote_a=True, session_active=False,
             session_mode="none", house_generator=False)
    actions = c.step(0, s)
    assert c.phase == Phase.EXTERNAL
    assert not any(a.kind.startswith("switch_") or a.kind == "button" for a in actions)


def test_manual_start_works_even_when_ats_disabled_and_grid_present():
    c = ATSController(Config())
    s = snap(ats_enabled=False, grid_ready=True, house_grid=True)
    c.step(0, s)
    c.request_manual_start()
    actions = c.step(1, s)
    assert c.phase == Phase.STARTING
    assert c.session_mode == "manual"
    assert ("switch_on", "switch.generator_a_remote_start", None) in kinds(actions)


def test_manual_start_is_blocked_by_emergency_stop():
    c = ATSController(Config())
    s = snap(emergency_stop=True, ats_enabled=False)
    actions = c.step(0, s)
    assert c.phase == Phase.TERMINAL
    assert not any(a.kind == "switch_on" and a.target == "switch.generator_a_remote_start" for a in actions)


def test_grid_fails_again_during_cooldown_reuses_running_hot_generator():
    c = ATSController(Config(grid_failure_delay=5, generator_stop_delay=300))
    c.bootstrapped = True
    c.phase = Phase.COOLDOWN
    c.active_generator = "A"
    c.session_mode = "automatic"
    c.deadline = 300
    s = snap(grid_ready=False, house_grid=False, generator_a_running=True,
             remote_a=True, session_active=True, session_mode="automatic")
    c.step(0, s)
    actions = c.step(5, s)
    assert c.phase == Phase.TRANSFER_DISCONNECT_GRID
    assert ("switch_off", "switch.grid_power", None) in kinds(actions)
    assert not any(a.kind == "button" for a in actions)  # no choke / restart path


def test_preheat_temperature_table():
    cfg = Config()
    c = ATSController(cfg)
    assert c._preheat_seconds(20) == 30
    assert c._preheat_seconds(10) == 30
    assert c._preheat_seconds(0) == 60
    assert c._preheat_seconds(-5) == 180
    assert c._preheat_seconds(-7) == 180
    assert c._preheat_seconds(-10) == 300
    assert c._preheat_seconds(-20) == 300
    assert c._preheat_seconds(None) == 300


def test_grid_disconnect_not_confirmed_is_terminal():
    c = ATSController(Config(transfer_confirmation_timeout=60))
    c.bootstrapped = True
    c.phase = Phase.TRANSFER_DISCONNECT_GRID
    c.active_generator = "A"
    c.session_mode = "automatic"
    c.deadline = 60
    s = snap(grid_ready=False, house_grid=True, house_generator=False,
             generator_a_running=True, remote_a=True, session_active=True,
             session_mode="automatic")
    actions = c.step(60, s)
    assert c.phase == Phase.TERMINAL
    assert ("switch_on", "switch.generators_emergency_stop", None) in kinds(actions)


def test_generator_power_not_confirmed_after_transfer_is_terminal():
    c = ATSController(Config(transfer_confirmation_timeout=60))
    c.bootstrapped = True
    c.phase = Phase.TRANSFER_SELECT_GENERATOR
    c.active_generator = "A"
    c.session_mode = "automatic"
    c.deadline = 60
    s = snap(grid_ready=False, house_grid=False, house_generator=False,
             generator_a_running=True, remote_a=True, grid_disconnected=True,
             source_generator=True, session_active=True, session_mode="automatic")
    actions = c.step(60, s)
    assert c.phase == Phase.TERMINAL
    assert any(a.kind == "notify_critical" and "Терминальная" in a.message for a in actions)


def test_a_stops_under_load_falls_back_to_b_once():
    c = ATSController(Config())
    c.bootstrapped = True
    c.phase = Phase.ON_GENERATOR
    c.active_generator = "A"
    c.session_mode = "automatic"
    s = snap(grid_ready=False, house_grid=False, house_generator=False,
             generator_a_running=False, generator_b_running=False,
             remote_a=True, grid_disconnected=True, source_generator=True,
             session_active=True, session_mode="automatic")
    actions = c.step(10, s)
    assert c.phase == Phase.STARTING
    assert c.active_generator == "B"
    assert "A" in c.failed_generators
    assert ("switch_on", "switch.generator_b_remote_start", None) in kinds(actions)


def test_manual_session_does_not_auto_return_when_grid_is_present():
    c = ATSController(Config(grid_restore_stable_time=60))
    c.bootstrapped = True
    c.phase = Phase.ON_GENERATOR
    c.active_generator = "A"
    c.session_mode = "manual"
    c.grid_ready_since = 0
    s = snap(grid_ready=True, house_grid=False, house_generator=True,
             generator_a_running=True, remote_a=True, grid_disconnected=True,
             source_generator=True, ats_enabled=False, session_active=True,
             session_mode="manual")
    actions = c.step(120, s)
    assert c.phase == Phase.ON_GENERATOR
    assert not any(a.target == "switch.use_generator_as_power_source" for a in actions)


def test_manual_return_uses_same_60_second_grid_stability_wait():
    c = ATSController(Config(grid_restore_stable_time=60))
    c.bootstrapped = True
    c.phase = Phase.ON_GENERATOR
    c.active_generator = "A"
    c.session_mode = "manual"
    s = snap(grid_ready=True, house_grid=False, house_generator=True,
             generator_a_running=True, remote_a=True, grid_disconnected=True,
             source_generator=True, ats_enabled=False, session_active=True,
             session_mode="manual")
    c.request_manual_return()
    c.step(1, s)
    assert c.phase == Phase.GRID_RESTORE_WAIT
    c.step(60, s)
    assert c.phase == Phase.GRID_RESTORE_WAIT
    actions = c.step(61, s)
    assert c.phase == Phase.RETURN_SELECT_GRID
    assert ("switch_off", "switch.use_generator_as_power_source", None) in kinds(actions)


def test_restart_on_grid_with_running_owned_generator_restarts_full_cooldown():
    c = ATSController(Config(generator_stop_delay=300))
    s = snap(grid_ready=True, house_grid=True, house_generator=False,
             generator_a_running=True, remote_a=True, session_active=True,
             session_mode="automatic")
    c.step(100, s)
    assert c.phase == Phase.COOLDOWN
    assert c.active_generator == "A"
    assert c.deadline == 400


def test_terminal_actions_normalize_to_grid_before_estop_in_declared_order():
    c = ATSController(Config())
    actions = c._terminal(0, "test", "binary_sensor.house_powered_by_generator")
    seq = [(a.kind, a.target) for a in actions[:5]]
    assert seq == [
        ("switch_off", "switch.generator_a_remote_start"),
        ("switch_off", "switch.generator_b_remote_start"),
        ("switch_off", "switch.use_generator_as_power_source"),
        ("switch_on", "switch.grid_power"),
        ("switch_on", "switch.generators_emergency_stop"),
    ]


def test_per_generator_choke_policy_and_unknown_temperature():
    c = ATSController(Config())
    # Текущее as-is: Elemax всегда с закрытой заслонкой.
    assert c._needs_choke("A", 25) is True
    # Вепрь при тёплой температуре запускается с открытой заслонкой.
    assert c._needs_choke("B", 25) is False
    # При похолодании или неизвестной температуре temperature-mode выбирает choke.
    assert c._needs_choke("B", 0) is True
    assert c._needs_choke("B", None) is True
    # Явный never доступен для будущих физических экспериментов.
    c.cfg = Config(generator_b_choke_mode="never")
    assert c._needs_choke("B", None) is False
    assert c._preheat_seconds(None) == 300

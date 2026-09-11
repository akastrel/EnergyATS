import pytest

from domain import SessionReason
from ups_run import (
    BatteryObservation,
    UPSRun,
    UPSRunConfig,
    UPSRunObservation,
    UPSRunState,
)


def cfg(**overrides):
    values = {
        "delayed_start_enabled": True,
        "charge_cycle_enabled": True,
        "start_soc": 40,
        "target_soc": 80,
        "min_ttg_before_start": 60,
        "max_start_delay": 3600,
        "telemetry_stale_time": 300,
    }
    values.update(overrides)
    return UPSRunConfig(**values)


def battery(soc=70, ttg=180, *, discharging=True, ready=True, sample=1):
    return BatteryObservation(soc, ttg, discharging, ready, sample)


def obs(
    now=0,
    *,
    grid=False,
    avr=True,
    core_delay=True,
    b=None,
    reason=None,
    active=False,
    on_generator=False,
    cycle_owned=False,
    manual_override=False,
    manual_pending=False,
):
    return UPSRunObservation(
        now=now,
        grid_ready=grid,
        automatic_transfer_enabled=avr,
        core_delay_elapsed=core_delay,
        battery=b or battery(),
        session_reason=reason,
        session_active=active,
        session_on_generator=on_generator,
        session_cycle_owned=cycle_owned,
        session_manual_override=manual_override,
        manual_start_pending=manual_pending,
    )


def test_disabled_ups_run_does_not_defer_or_cycle():
    p = UPSRun(UPSRunConfig())
    d = p.step(obs())
    assert not d.defer_automatic_start
    assert not d.request_cycle_stop
    assert p.state == UPSRunState.IDLE


def test_wait_starts_only_after_core_grid_failure_delay():
    p = UPSRun(cfg())
    before = p.step(obs(10, core_delay=False))
    after = p.step(obs(60, core_delay=True, b=battery(sample=2)))
    assert not before.defer_automatic_start
    assert after.defer_automatic_start
    assert p.waiting_since == 60


def test_high_soc_and_ttg_keep_generator_off():
    p = UPSRun(cfg())
    d = p.step(obs(100, b=battery(75, 240)))
    assert d.defer_automatic_start
    assert d.outage_delay_already_satisfied
    assert p.state == UPSRunState.WAITING_ON_UPS


@pytest.mark.parametrize(
    "b, now, expected_text",
    [
        (battery(40, 240), 10, "SoC"),
        (battery(70, 60), 10, "TTG"),
    ],
)
def test_soc_or_ttg_threshold_requires_generator(b, now, expected_text):
    p = UPSRun(cfg())
    d = p.step(obs(now, b=b))
    assert not d.defer_automatic_start
    assert d.claim_new_outage_session
    assert expected_text in (d.reason or "")


def test_max_wait_requires_generator():
    p = UPSRun(cfg(max_start_delay=100))
    assert p.step(obs(0)).defer_automatic_start
    d = p.step(obs(100, b=battery(sample=2)))
    assert not d.defer_automatic_start
    assert "максимальная" in (d.reason or "")


@pytest.mark.parametrize(
    "bad_battery",
    [
        battery(soc=None),
        battery(soc=101),
        battery(ready=None),
        battery(ready=False),
        battery(discharging=None),
        battery(ttg=None, discharging=True),
    ],
)
def test_bad_required_battery_data_fails_safe_to_generator(bad_battery):
    p = UPSRun(cfg())
    d = p.step(obs(10, b=bad_battery))
    assert not d.defer_automatic_start
    assert d.claim_new_outage_session
    assert p.state == UPSRunState.GENERATOR_REQUIRED


def test_ttg_is_not_required_when_battery_is_not_discharging():
    p = UPSRun(cfg())
    d = p.step(obs(10, b=battery(70, None, discharging=False)))
    assert d.defer_automatic_start


def test_stale_telemetry_fails_safe_while_discharging():
    p = UPSRun(cfg(telemetry_stale_time=10))
    assert p.step(obs(0, b=battery(sample=5))).defer_automatic_start
    d = p.step(obs(11, b=battery(sample=5)))
    assert not d.defer_automatic_start
    assert "устарела" in (d.reason or "")


def test_fresh_sample_extends_telemetry_freshness():
    p = UPSRun(cfg(telemetry_stale_time=10))
    p.step(obs(0, b=battery(sample=5)))
    d = p.step(obs(9, b=battery(sample=6)))
    assert d.defer_automatic_start
    assert p.step(obs(18, b=battery(sample=6))).defer_automatic_start


def test_grid_return_cancels_wait():
    p = UPSRun(cfg())
    p.step(obs(10))
    d = p.step(obs(20, grid=True, b=battery(sample=2)))
    assert not d.defer_automatic_start
    assert p.waiting_since is None
    assert p.state == UPSRunState.IDLE


def test_manual_request_bypasses_wait():
    p = UPSRun(cfg())
    d = p.step(obs(10, manual_pending=True))
    assert not d.defer_automatic_start
    assert not d.claim_new_outage_session
    assert "Пользователь" in (d.reason or "")


def test_cycling_can_claim_immediate_automatic_start_when_delayed_start_disabled():
    p = UPSRun(cfg(delayed_start_enabled=False, charge_cycle_enabled=True))
    d = p.step(obs(10))
    assert not d.defer_automatic_start
    assert d.claim_new_outage_session


def test_cycle_owned_session_stops_at_target_soc():
    p = UPSRun(cfg())
    d = p.step(
        obs(
            100,
            b=battery(80, None, discharging=False),
            reason=SessionReason.GRID_OUTAGE,
            active=True,
            on_generator=True,
            cycle_owned=True,
        )
    )
    assert d.request_cycle_stop
    assert p.state == UPSRunState.TARGET_REACHED


def test_cycle_does_not_stop_before_target():
    p = UPSRun(cfg())
    d = p.step(
        obs(
            100,
            b=battery(70, None, discharging=False),
            reason=SessionReason.GRID_OUTAGE,
            active=True,
            on_generator=True,
            cycle_owned=True,
        )
    )
    assert not d.request_cycle_stop
    assert p.state == UPSRunState.CHARGING


@pytest.mark.parametrize(
    "reason,cycle_owned,manual_override",
    [
        (SessionReason.MANUAL_GENERATOR_START, False, False),
        (SessionReason.GRID_OUTAGE, False, False),
        (SessionReason.GRID_OUTAGE, True, True),
    ],
)
def test_manual_external_or_unowned_session_is_never_stopped_by_target(
    reason, cycle_owned, manual_override
):
    p = UPSRun(cfg())
    d = p.step(
        obs(
            100,
            b=battery(95, None, discharging=False),
            reason=reason,
            active=True,
            on_generator=True,
            cycle_owned=cycle_owned,
            manual_override=manual_override,
        )
    )
    assert not d.request_cycle_stop


def test_invalid_threshold_configuration_disables_optimization_only():
    p = UPSRun(cfg(start_soc=80, target_soc=40))
    d = p.step(obs())
    assert not d.defer_automatic_start
    assert not d.request_cycle_stop
    assert p.state == UPSRunState.DEGRADED


def test_waiting_since_survives_restart_but_freshness_does_not():
    p = UPSRun(cfg(max_start_delay=100))
    p.step(obs(10, b=battery(sample=1)))
    restored = UPSRun.from_dict(p.to_dict(), cfg(max_start_delay=100))
    d = restored.step(obs(50, core_delay=False, b=battery(sample=99)))
    assert d.defer_automatic_start
    assert d.outage_delay_already_satisfied
    assert restored.waiting_since == 10


def test_restart_wait_preserves_max_delay_elapsed_time():
    p = UPSRun(cfg(max_start_delay=100))
    p.step(obs(10))
    restored = UPSRun.from_dict(p.to_dict(), cfg(max_start_delay=100))
    d = restored.step(obs(111, core_delay=False, b=battery(sample=2)))
    assert not d.defer_automatic_start
    assert "максимальная" in (d.reason or "")


def test_status_exposes_wait_and_battery_information():
    p = UPSRun(cfg(max_start_delay=100))
    b = battery(71, 123)
    p.step(obs(10, b=b))
    attrs = p.status_attributes(40, b)
    assert attrs["charge_cycle_state"] == "waiting_on_ups"
    assert attrs["battery_soc"] == 71
    assert attrs["delayed_start_elapsed_seconds"] == 30
    assert attrs["delayed_start_remaining_seconds"] == 70


@pytest.mark.parametrize("waiting", [float("nan"), float("inf"), -1])
def test_invalid_persisted_wait_timestamp_is_rejected(waiting):
    with pytest.raises(ValueError, match="waiting_since"):
        UPSRun.from_dict({"waiting_since": waiting}, cfg())


def test_ha_cache_timestamp_survives_adapter_snapshot():
    from ha_adapter import ENTITIES, HomeAssistantAdapter
    from ha_client import HomeAssistantClient

    client = HomeAssistantClient("test")
    client.states[ENTITIES["ups_battery_soc"]] = {
        "state": "80",
        "last_updated": "2026-09-11T00:00:00+00:00",
    }
    snapshot = HomeAssistantAdapter(client, armed=False).snapshot()
    assert snapshot.battery.soc_updated_at == 1789084800

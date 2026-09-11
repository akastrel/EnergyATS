from __future__ import annotations

from datetime import datetime, timezone

import pytest

from domain import GeneratorSlot, PowerPath, PowerSource, SessionReason
from energy_supervisor import (
    EnergySupervisor,
    GeneratorSession,
    SupervisorConfig,
    SupervisorObservation,
    SupervisorPhase,
)
from exercise_scheduler import (
    ExerciseConfig,
    ExerciseGeneratorObservation,
    ExerciseObservation,
    ExerciseResult,
    ExerciseScheduler,
)
from generator_bus import GeneratorBusOwner, GeneratorBusStatus, GeneratorRunContext
from generator_controller import GeneratorPhase, GeneratorStatus
from load_manager import (
    LoadActionKind,
    LoadGroup,
    LoadManager,
    LoadManagerConfig,
    LoadManagerObservation,
    LoadManagerPhase,
)
from power_transfer import (
    PowerTransferController,
    PowerTransferObservation,
    PowerTransferStatus,
    TransferActionKind,
    TransferPhase,
)


def _generator(
    slot: GeneratorSlot,
    *,
    phase: GeneratorPhase = GeneratorPhase.IDLE,
    running: bool = False,
    remote_on: bool = False,
    ready: bool = False,
    fault: str | None = None,
) -> GeneratorStatus:
    return GeneratorStatus(
        slot=slot,
        display_name="Elemax" if slot == GeneratorSlot.A else "Вепрь",
        phase=phase,
        running=running,
        remote_on=remote_on,
        ready_for_load=ready,
        fault=fault,
    )


def _ready_a() -> GeneratorStatus:
    return _generator(
        GeneratorSlot.A,
        phase=GeneratorPhase.READY_FOR_LOAD,
        running=True,
        remote_on=True,
        ready=True,
    )


def _bus(owner: GeneratorBusOwner = GeneratorBusOwner.NONE) -> GeneratorBusStatus:
    return GeneratorBusStatus(
        owner=owner,
        run_contexts={
            GeneratorSlot.A: GeneratorRunContext.OUTAGE_RELATED
            if owner == GeneratorBusOwner.A
            else GeneratorRunContext.NONE,
            GeneratorSlot.B: GeneratorRunContext.OUTAGE_RELATED
            if owner == GeneratorBusOwner.B
            else GeneratorRunContext.NONE,
        },
    )


def _power(source: PowerSource, path: PowerPath) -> PowerTransferStatus:
    phase = {
        PowerPath.GRID: TransferPhase.STABLE_GRID,
        PowerPath.GENERATOR: TransferPhase.STABLE_GENERATOR,
        PowerPath.ISOLATED: TransferPhase.STABLE_ISOLATED,
    }.get(path, TransferPhase.WAITING_FOR_DATA)
    return PowerTransferStatus(
        phase=phase,
        actual_source=source,
        actual_path=path,
        target_source=None,
        transition_in_progress=False,
        recovery_required=False,
        fault=None,
    )


def _supervisor_observation(
    *,
    grid_ready: bool,
    source: PowerSource,
    path: PowerPath,
    a: GeneratorStatus | None = None,
    owner: GeneratorBusOwner = GeneratorBusOwner.NONE,
) -> SupervisorObservation:
    return SupervisorObservation(
        grid_ready=grid_ready,
        automatic_transfer_enabled=True,
        emergency_stop=False,
        power=_power(source, path),
        generators={
            GeneratorSlot.A: a or _generator(GeneratorSlot.A),
            GeneratorSlot.B: _generator(GeneratorSlot.B),
        },
        power_inputs_known=True,
        bus=_bus(owner),
    )


def _managed_supervisor(
    *,
    reason: SessionReason,
    grid_was_unavailable: bool,
    restore_time: float = 0.0,
) -> EnergySupervisor:
    supervisor = EnergySupervisor(
        SupervisorConfig(grid_restore_stable_time=restore_time)
    )
    supervisor.initialized = True
    supervisor.session = GeneratorSession.begin(
        reason,
        GeneratorSlot.A,
        grid_was_unavailable=grid_was_unavailable,
    )
    supervisor.phase = SupervisorPhase.ON_GENERATOR
    supervisor.desired_source = PowerSource.GENERATOR
    supervisor.desired_generators[GeneratorSlot.A] = True
    return supervisor


def test_f1_manual_outage_stop_restores_grid_after_grid_returns_even_after_restart() -> None:
    """F1: Grid, изолированная самим EnergyATS для manual stop во время outage, должна быть восстановлена после stable Grid даже если App перезапустился между этими событиями."""
    supervisor = _managed_supervisor(
        reason=SessionReason.GRID_OUTAGE,
        grid_was_unavailable=True,
        restore_time=3.0,
    )
    supervisor.request_manual_stop()

    supervisor.step(
        0.0,
        _supervisor_observation(
            grid_ready=False,
            source=PowerSource.GENERATOR,
            path=PowerPath.GENERATOR,
            a=_ready_a(),
            owner=GeneratorBusOwner.A,
        ),
    )
    assert supervisor.phase == SupervisorPhase.RETURNING_TO_UPS

    supervisor.step(
        1.0,
        _supervisor_observation(
            grid_ready=False,
            source=PowerSource.UPS_ONLY,
            path=PowerPath.ISOLATED,
        ),
    )
    assert supervisor.session is None

    restored = EnergySupervisor.from_dict(
        supervisor.to_dict(),
        SupervisorConfig(grid_restore_stable_time=3.0),
    )
    restored.step(
        10.0,
        _supervisor_observation(
            grid_ready=True,
            source=PowerSource.UPS_ONLY,
            path=PowerPath.ISOLATED,
        ),
    )
    decision = restored.step(
        13.1,
        _supervisor_observation(
            grid_ready=True,
            source=PowerSource.UPS_ONLY,
            path=PowerPath.ISOLATED,
        ),
    )

    assert decision.desired_source == PowerSource.GRID

    # Intentional user isolation without an EnergyATS-owned stop obligation
    # must remain untouched.
    plain = EnergySupervisor(SupervisorConfig(grid_restore_stable_time=0.0))
    decision = plain.step(
        20.0,
        _supervisor_observation(
            grid_ready=True,
            source=PowerSource.UPS_ONLY,
            path=PowerPath.ISOLATED,
        ),
    )
    assert decision.desired_source is None


def test_f2_stop_timeout_during_manual_grid_return_requires_recovery_without_fallback() -> None:
    """F2: если дом уже вернулся на Grid, но managed generator не остановился и GC дал FAULT, Supervisor обязан завершить сценарий Recovery, не запускать SECONDARY и не ждать бесконечно."""
    supervisor = _managed_supervisor(
        reason=SessionReason.MANUAL_GENERATOR_START,
        grid_was_unavailable=False,
    )
    supervisor.request_manual_stop()
    supervisor.step(
        0.0,
        _supervisor_observation(
            grid_ready=True,
            source=PowerSource.GENERATOR,
            path=PowerPath.GENERATOR,
            a=_ready_a(),
            owner=GeneratorBusOwner.A,
        ),
    )
    assert supervisor.phase == SupervisorPhase.RETURNING_TO_GRID

    decision = supervisor.step(
        1.0,
        _supervisor_observation(
            grid_ready=True,
            source=PowerSource.GRID,
            path=PowerPath.GRID,
            a=_generator(
                GeneratorSlot.A,
                phase=GeneratorPhase.FAULT,
                running=True,
                remote_on=False,
                fault="Elemax не остановился за 2 с.",
            ),
        ),
    )

    assert supervisor.phase == SupervisorPhase.RECOVERY_REQUIRED
    assert supervisor.recovery_reason is not None
    assert decision.desired_generators[GeneratorSlot.B] is False


def _transfer_observation(
    *,
    grid_ready: bool,
    house_on_grid: bool,
    house_on_generator: bool,
    grid_connected: bool,
    generator_selected: bool,
) -> PowerTransferObservation:
    return PowerTransferObservation(
        grid_ready=grid_ready,
        house_on_grid=house_on_grid,
        house_on_generator=house_on_generator,
        grid_connected=grid_connected,
        generator_selected=generator_selected,
        emergency_stop=False,
    )


def test_f3_lost_generator_voltage_allows_safe_deselect_before_slow_fallback_start() -> None:
    """F3: после исчезновения generator feedback при всё ещё ON selector TPC должен уметь выполнить безопасный break (DESELECT), чтобы разрешённый медленный fallback не проиграл более короткому transfer timeout."""
    controller = PowerTransferController(confirmation_timeout=60.0)
    on_generator = _transfer_observation(
        grid_ready=False,
        house_on_grid=False,
        house_on_generator=True,
        grid_connected=False,
        generator_selected=True,
    )
    controller.step(0.0, on_generator, None, desired_generator_ready=False)

    source_lost = _transfer_observation(
        grid_ready=False,
        house_on_grid=False,
        house_on_generator=False,
        grid_connected=False,
        generator_selected=True,
    )
    actions = controller.step(
        1.0,
        source_lost,
        PowerSource.UPS_ONLY,
        desired_generator_ready=False,
    )
    assert [action.kind for action in actions] == [TransferActionKind.DESELECT_GENERATOR]

    isolated = _transfer_observation(
        grid_ready=False,
        house_on_grid=False,
        house_on_generator=False,
        grid_connected=False,
        generator_selected=False,
    )
    controller.step(
        2.0,
        isolated,
        PowerSource.UPS_ONLY,
        desired_generator_ready=False,
    )
    assert controller.status().actual_path == PowerPath.ISOLATED

    # B may legitimately become ready later than transfer_confirmation_timeout
    # of the old ambiguous snapshot, while still inside its own start timeout.
    actions = controller.step(
        70.0,
        isolated,
        PowerSource.GENERATOR,
        desired_generator_ready=True,
    )
    assert [action.kind for action in actions] == [TransferActionKind.SELECT_GENERATOR]


def _exercise_configs() -> dict[GeneratorSlot, ExerciseConfig]:
    return {
        GeneratorSlot.A: ExerciseConfig(True, 30, "15:00", 10, 7),
        GeneratorSlot.B: ExerciseConfig(False, 45, "15:00", 10, 14),
    }


def _exercise_observation(
    local_now: datetime,
    *,
    present: bool | None,
    a_running: bool = False,
    a_remote: bool = False,
) -> ExerciseObservation:
    return ExerciseObservation(
        now=local_now.timestamp(),
        local_now=local_now,
        grid_ready=True,
        grid_path_stable=True,
        family_present=present,
        emergency_stop=False,
        required_states_known=True,
        power_transition_in_progress=False,
        policy_busy=False,
        actions_enabled=True,
        generators={
            GeneratorSlot.A: ExerciseGeneratorObservation(a_running, a_remote, None),
            GeneratorSlot.B: ExerciseGeneratorObservation(False, False, None),
        },
        generator_names={GeneratorSlot.A: "Elemax", GeneratorSlot.B: "Вепрь"},
    )


@pytest.mark.parametrize("present", [True, None])
def test_f4_presence_is_rechecked_before_ordinary_exercise_remote_on(
    present: bool | None,
) -> None:
    """F4: ordinary Exercise должен отмениться как DEFERRED, если до физического REMOTE ON семья появилась либо presence стала недостоверной."""
    scheduler = ExerciseScheduler(_exercise_configs())
    scheduler.step(
        _exercise_observation(
            datetime(2026, 1, 1, 14, 0, tzinfo=timezone.utc),
            present=False,
        )
    )

    start = datetime(2026, 1, 31, 15, 0, tzinfo=timezone.utc)
    first = scheduler.step(_exercise_observation(start, present=False))
    assert first.owned_slot == GeneratorSlot.A
    assert first.desired_running is True

    changed = scheduler.step(
        _exercise_observation(
            datetime(2026, 1, 31, 15, 0, 2, tzinfo=timezone.utc),
            present=present,
            a_running=False,
            a_remote=False,
        )
    )

    assert changed.owned_slot is None
    assert changed.desired_running is False
    assert scheduler.history[-1]["result"] == ExerciseResult.DEFERRED.value
    assert not any(event.level == "critical" for event in changed.events)


def _load_observation(
    now: float,
    *,
    power: float,
    sample: int,
) -> LoadManagerObservation:
    return LoadManagerObservation(
        now=now,
        house_on_generator=True,
        house_on_grid=False,
        desired_generator_supply=True,
        managed_generator_ready=True,
        power_transition_in_progress=False,
        bus_owner=GeneratorSlot.A,
        nominal_power=1000,
        maximum_power=1200,
        meter_ready=True,
        generator_power=power,
        power_sample_id=sample,
        groups={LoadGroup.G1: True, LoadGroup.G2: True},
        generator_name="Elemax",
        actions_enabled=True,
    )


def test_f5_stale_power_stream_degrades_and_resets_overload_continuity() -> None:
    """F5: в STABLE длительный gap без нового power sample обязан дать DEGRADED; первый sample после восстановления начинает новую stabilization, а не продолжает старый overload timer."""
    manager = LoadManager(
        LoadManagerConfig(
            enabled=True,
            measurement_stabilization_time=2,
            restore_margin_percent=15,
            nominal_overload_time=3,
            maximum_overload_confirmation_time=1,
            restore_retry_interval=300,
        )
    )
    for now, sample in ((0, 1), (1, 2), (2, 3)):
        manager.step(_load_observation(now, power=800, sample=sample))
    assert manager.phase == LoadManagerPhase.STABLE

    manager.step(_load_observation(3, power=1300, sample=4))
    gap = manager.step(_load_observation(9, power=1300, sample=4))

    assert gap.actions == ()
    assert manager.phase == LoadManagerPhase.DEGRADED
    assert manager.degraded_reason is not None

    first_new = manager.step(_load_observation(10, power=1300, sample=5))
    assert first_new.actions == ()
    assert manager.phase == LoadManagerPhase.MEASURING
    assert not any(
        action.kind == LoadActionKind.TURN_OFF for action in first_new.actions
    )

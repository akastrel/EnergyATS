from __future__ import annotations

from datetime import datetime, timedelta, timezone

from domain import GeneratorSlot, SessionReason
from energy_supervisor import GeneratorSession, SupervisorPhase
from generator_bus import GeneratorRunContext
from generator_run_history import GeneratorRunType
from ha_adapter import ENTITIES
from main import DEFAULT_OPTIONS, EnergySupervisorApp
from state_store import StateStore


class FakeClient:
    def __init__(self, states: dict[str, str]) -> None:
        self.states = states

    def get_state(self, entity_id: str) -> str | None:
        return self.states.get(entity_id)

    def has_entity(self, entity_id: str) -> bool:
        return entity_id in self.states

    async def call_service(self, domain, service, *, service_data=None):
        return None

    async def set_state(self, entity_id, state, *, attributes=None):
        return None


def grid_states() -> dict[str, str]:
    return {
        ENTITIES["automatic_transfer"]: "on",
        ENTITIES["test_mode"]: "off",
        ENTITIES["grid_ready"]: "on",
        ENTITIES["house_grid"]: "on",
        ENTITIES["house_generator"]: "off",
        ENTITIES["generator_a_running"]: "off",
        ENTITIES["generator_b_running"]: "off",
        ENTITIES["generator_a_remote"]: "off",
        ENTITIES["generator_b_remote"]: "off",
        ENTITIES["generator_a_name"]: "Elemax",
        ENTITIES["generator_b_name"]: "Вепрь",
        ENTITIES["generator_a_model"]: "SH7600EX 6.5 / 5.6 кВт",
        ENTITIES["generator_b_model"]: "АПБ 6-230 ВХ-БСГ 6.0 / 5.5 кВт",
        ENTITIES["primary_generator"]: "Elemax",
        ENTITIES["emergency_stop"]: "off",
        ENTITIES["ambient_temperature_external"]: "7.5",
        ENTITIES["grid_power"]: "on",
        ENTITIES["source_generator"]: "off",
        ENTITIES["generator_a_choke_cold_start"]: "unknown",
        ENTITIES["generator_a_choke_run"]: "unknown",
        ENTITIES["generator_b_choke_cold_start"]: "unknown",
        ENTITIES["generator_b_choke_run"]: "unknown",
    }


def make_app(tmp_path) -> EnergySupervisorApp:
    app = EnergySupervisorApp(
        {**DEFAULT_OPTIONS, "armed": True, "state_file": str(tmp_path / "state.json")},
        token="test",
    )
    fake = FakeClient(grid_states())
    app.client = fake
    app.adapter.client = fake
    return app


def complete_run(app: EnergySupervisorApp) -> None:
    start = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    common = {
        "faults": {GeneratorSlot.A: None, GeneratorSlot.B: None},
        "run_types": {
            GeneratorSlot.A: GeneratorRunType.AUTOMATIC,
            GeneratorSlot.B: GeneratorRunType.EXTERNAL,
        },
        "generator_names": {GeneratorSlot.A: "Elemax", GeneratorSlot.B: "Вепрь"},
    }
    app.generator_runs.step(
        now=start.timestamp(),
        local_now=start,
        running={GeneratorSlot.A: False, GeneratorSlot.B: False},
        **common,
    )
    app.generator_runs.step(
        now=(start + timedelta(seconds=1)).timestamp(),
        local_now=start + timedelta(seconds=1),
        running={GeneratorSlot.A: True, GeneratorSlot.B: False},
        **common,
    )
    app.generator_runs.step(
        now=(start + timedelta(minutes=46, seconds=1)).timestamp(),
        local_now=start + timedelta(minutes=46, seconds=1),
        running={GeneratorSlot.A: False, GeneratorSlot.B: False},
        **common,
    )


def normal_observation(app: EnergySupervisorApp, now: float):
    hardware = app.adapter.snapshot()
    app._sync_generator_configuration(hardware)
    hardware = app._apply_bus_model(hardware)
    app._refresh_component_views(now, hardware)
    observation = app._supervisor_observation(hardware)
    app.supervisor.step(now, observation)
    return app._supervisor_observation(hardware), hardware


def test_status_payload_exposes_compact_run_statistics(tmp_path):
    app = make_app(tmp_path)
    complete_run(app)
    observation, hardware = normal_observation(app, 100.0)

    attrs = app._status_payload(100.0, observation, hardware)["attributes"]

    assert attrs["generator_a_total_starts"] == 1
    assert attrs["generator_a_total_runtime_seconds"] == 46 * 60
    assert attrs["generator_a_total_runtime_hours"] == 0.8
    assert attrs["generator_a_last_run_start"] is not None
    assert attrs["generator_a_last_run_end"] is not None
    assert attrs["generator_a_last_run_duration_seconds"] == 46 * 60
    assert attrs["generator_a_last_run_type"] == "automatic"
    assert attrs["generator_a_last_run_result"] == "success"


def test_generator_run_history_survives_app_restart(tmp_path):
    app = make_app(tmp_path)
    complete_run(app)
    app._save_state(force=True)

    restored = EnergySupervisorApp(
        {**DEFAULT_OPTIONS, "armed": True, "state_file": str(tmp_path / "state.json")},
        token="test",
    )
    attrs = restored.generator_runs.status_attributes()

    assert attrs["generator_a_total_starts"] == 1
    assert attrs["generator_a_total_runtime_seconds"] == 46 * 60
    assert attrs["generator_a_last_run_type"] == "automatic"


def test_corrupt_run_history_is_soft_failure_not_core_recovery(tmp_path):
    app = make_app(tmp_path)
    app._save_state(force=True)
    store = StateStore(tmp_path / "state.json")
    payload = store.load()
    payload["generator_runs"] = {"slots": "broken"}
    store.save(payload)

    restored = EnergySupervisorApp(
        {**DEFAULT_OPTIONS, "armed": True, "state_file": str(tmp_path / "state.json")},
        token="test",
    )

    assert restored.supervisor.phase != SupervisorPhase.RECOVERY_REQUIRED
    assert restored.generator_runs.status_attributes()["generator_a_total_starts"] == 0


def test_run_type_uses_existing_ats_ownership_facts(tmp_path):
    app = make_app(tmp_path)

    app.supervisor.session = GeneratorSession.begin(
        SessionReason.GRID_OUTAGE,
        GeneratorSlot.A,
        grid_was_unavailable=True,
    )
    assert app._generator_run_types()[GeneratorSlot.A] == GeneratorRunType.AUTOMATIC
    assert app._generator_run_types()[GeneratorSlot.B] == GeneratorRunType.EXTERNAL

    app.supervisor.session = GeneratorSession.begin(
        SessionReason.MANUAL_GENERATOR_START,
        GeneratorSlot.A,
        grid_was_unavailable=False,
    )
    assert app._generator_run_types()[GeneratorSlot.A] == GeneratorRunType.MANUAL

    app.generator_bus.run_contexts[GeneratorSlot.A] = GeneratorRunContext.TEST_RUN
    assert app._generator_run_types()[GeneratorSlot.A] == GeneratorRunType.EXERCISE


def test_connection_gap_invalidates_active_run_continuity(tmp_path):
    app = make_app(tmp_path)
    start = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
    common = {
        "faults": {GeneratorSlot.A: None, GeneratorSlot.B: None},
        "run_types": {
            GeneratorSlot.A: GeneratorRunType.AUTOMATIC,
            GeneratorSlot.B: GeneratorRunType.EXTERNAL,
        },
        "generator_names": {GeneratorSlot.A: "Elemax", GeneratorSlot.B: "Вепрь"},
    }
    app.generator_runs.step(
        now=start.timestamp(),
        local_now=start,
        running={GeneratorSlot.A: False, GeneratorSlot.B: False},
        **common,
    )
    app.generator_runs.step(
        now=(start + timedelta(seconds=1)).timestamp(),
        local_now=start + timedelta(seconds=1),
        running={GeneratorSlot.A: True, GeneratorSlot.B: False},
        **common,
    )

    app.generator_runs.invalidate_observation_history()
    app.generator_runs.step(
        now=(start + timedelta(hours=1)).timestamp(),
        local_now=start + timedelta(hours=1),
        running={GeneratorSlot.A: True, GeneratorSlot.B: False},
        **common,
    )
    update = app.generator_runs.step(
        now=(start + timedelta(hours=2)).timestamp(),
        local_now=start + timedelta(hours=2),
        running={GeneratorSlot.A: False, GeneratorSlot.B: False},
        **common,
    )

    assert update.events == ()
    assert app.generator_runs.status_attributes()["generator_a_total_runtime_seconds"] == 0

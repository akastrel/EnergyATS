"""Общие helpers для app-level integration tests EnergyATS.

Это test infrastructure, а не отдельный test-suite: feature tests импортируют отсюда
одинаковую сборку App/Fake HA и ускорение таймеров генераторов вместо взаимных
импортов из других test_*.py.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TypeVar

from ha_adapter import ENTITIES
from main import DEFAULT_OPTIONS, EnergySupervisorApp
from test_app_adapter import PhysicalFakeClient, attach_fake_client, populated_states


FakeT = TypeVar("FakeT", bound=PhysicalFakeClient)


def make_app(
    tmp_path: Path,
    *,
    fake_type: type[FakeT] = PhysicalFakeClient,
    **option_overrides,
) -> tuple[EnergySupervisorApp, FakeT]:
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
    fake = fake_type(journal)
    fake.states = populated_states()
    fake.states[ENTITIES["ambient_temperature_external"]] = "20"
    fake.states[ENTITIES["test_mode"]] = "off"
    attach_fake_client(app, fake)
    return app, fake


def attach_existing_fake(app: EnergySupervisorApp, fake: PhysicalFakeClient) -> None:
    attach_fake_client(app, fake)


def accelerate_generators(app: EnergySupervisorApp) -> None:
    """Ускорить таймеры, сохранив последовательность Generator Controller FSM."""
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


def switch_calls(fake: PhysicalFakeClient) -> list[tuple[str, str]]:
    return [
        (service, data.get("entity_id"))
        for domain, service, data in fake.calls
        if domain == "switch"
    ]


def restart_app(
    tmp_path: Path,
    states: dict[str, str],
    **option_overrides,
) -> tuple[EnergySupervisorApp, PhysicalFakeClient]:
    """Создать новый process App на том же journal и вернуть ему те же HA states."""
    app, fake = make_app(tmp_path, **option_overrides)
    fake.states = dict(states)
    attach_fake_client(app, fake)
    return app, fake

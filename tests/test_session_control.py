from __future__ import annotations

import pytest

from domain import GeneratorSlot, SessionReason
from energy_supervisor import GeneratorSession, SessionControlMode


def test_session_control_mode_roundtrip_replaces_two_boolean_owners():
    """Persistent session хранит один control_mode вместо независимых cycle_owned/manual_override, поэтому противоречивое ownership-state невозможно создать штатным API."""
    session = GeneratorSession.begin(
        SessionReason.GRID_OUTAGE,
        GeneratorSlot.A,
        grid_was_unavailable=True,
    )
    session.control_mode = SessionControlMode.CHARGE_CYCLE

    payload = session.to_dict()
    restored = GeneratorSession.from_dict(payload)

    assert payload["control_mode"] == "charge_cycle"
    assert "cycle_owned" not in payload
    assert "manual_override" not in payload
    assert restored.control_mode == SessionControlMode.CHARGE_CYCLE
    assert restored.cycle_owned is True
    assert restored.manual_override is False


def test_070_cycle_owned_state_migrates_to_charge_cycle_mode():
    """Активная 0.7.0 cycling-session после обновления восстанавливается в новый однозначный control mode без потери ownership."""
    restored = GeneratorSession.from_dict(
        {
            "reason": "grid_outage",
            "generator": "A",
            "grid_was_unavailable": True,
            "stop_requested": False,
            "fallback_used": False,
            "external_takeover_observed": False,
            "cycle_owned": True,
            "manual_override": False,
        }
    )

    assert restored.control_mode == SessionControlMode.CHARGE_CYCLE
    assert restored.cycle_owned is True


def test_070_manual_override_state_migrates_to_manual_override_mode():
    """Ручной takeover активной outage-session сохраняется через restart и после перехода на новый формат persistence."""
    restored = GeneratorSession.from_dict(
        {
            "reason": "grid_outage",
            "generator": "B",
            "grid_was_unavailable": True,
            "stop_requested": False,
            "fallback_used": False,
            "external_takeover_observed": False,
            "cycle_owned": False,
            "manual_override": True,
        }
    )

    assert restored.control_mode == SessionControlMode.MANUAL_OVERRIDE
    assert restored.manual_override is True
    assert restored.cycle_owned is False


def test_contradictory_legacy_ownership_is_rejected():
    """Состояние одновременно Charge Cycling + manual override больше не нормализуется молча: повреждённый journal отклоняется."""
    with pytest.raises(ValueError, match="одновременно"):
        GeneratorSession.from_dict(
            {
                "reason": "grid_outage",
                "generator": "A",
                "grid_was_unavailable": True,
                "cycle_owned": True,
                "manual_override": True,
            }
        )


def test_special_control_mode_is_not_allowed_for_manual_session_reason():
    """Специальные outage-control modes нельзя записать в обычную MANUAL_GENERATOR_START session."""
    with pytest.raises(ValueError, match="только для GRID_OUTAGE"):
        GeneratorSession.from_dict(
            {
                "reason": "manual_generator_start",
                "generator": "A",
                "grid_was_unavailable": False,
                "control_mode": "charge_cycle",
            }
        )

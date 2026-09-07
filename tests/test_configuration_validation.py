from __future__ import annotations

from pathlib import Path
import sys

import pytest

APP_DIR = Path(__file__).resolve().parents[1] / "energy_ats" / "app"
sys.path.insert(0, str(APP_DIR))

from main import DEFAULT_OPTIONS, EnergySupervisorApp  # noqa: E402


def test_disabled_primary_generator_is_rejected_before_startup(tmp_path):
    with pytest.raises(ValueError, match="основной генератор Elemax.*отключён"):
        EnergySupervisorApp(
            {
                **DEFAULT_OPTIONS,
                "primary_generator": "Elemax",
                "generator_a_enabled": False,
                "generator_b_enabled": True,
                "state_file": str(tmp_path / "state.json"),
            },
            token="test",
        )

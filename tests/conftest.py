"""Общая настройка импортов для запуска любого тестового файла отдельно."""

from __future__ import annotations

import sys
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1] / "energy_ats" / "app"
sys.path.insert(0, str(APP_DIR))

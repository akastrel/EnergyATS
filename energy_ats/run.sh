#!/usr/bin/with-contenv bashio
set -e

bashio::log.info "Запуск Energy ATS..."

python3 - <<'PY'
import json
from pathlib import Path

path = Path("/data/options.json")
options = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
primary = options.get("primary_generator", "Elemax")

if primary == "Elemax" and options.get("generator_a_enabled", True) is not True:
    raise SystemExit(
        "Ошибка конфигурации: Elemax выбран как основной генератор, "
        "но Enable Elemax выключен."
    )

if primary == "Вепрь" and options.get("generator_b_enabled", True) is not True:
    raise SystemExit(
        "Ошибка конфигурации: Вепрь выбран как основной генератор, "
        "но Enable Vepr выключен."
    )
PY

exec python3 -u /app/main.py

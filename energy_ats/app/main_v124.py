from __future__ import annotations

from dataclasses import replace
from typing import Any

import main as base
from ats_core import Action
from policy_controller import PolicyATSController


OPTIONS = base.load_options()
PolicyATSController.configure_runtime(OPTIONS)

# main.py создаёт новый ATSController после каждого reconnect. Подменяем класс
# один раз до запуска lifecycle, поэтому recovery тоже получает policy-controller.
base.ATSController = PolicyATSController

GENERATOR_NAMES = {
    "A": str(OPTIONS.get("generator_a_name", "Elemax")),
    "B": str(OPTIONS.get("generator_b_name", "Вепрь")),
}


def _humanize(message: str | None) -> str | None:
    if not message:
        return message
    result = message
    # Сначала более длинные формы, чтобы не получить частичную замену.
    result = result.replace("генератора A", f"генератора {GENERATOR_NAMES['A']}")
    result = result.replace("генератор A", f"генератор {GENERATOR_NAMES['A']}")
    result = result.replace("Генератора A", f"Генератора {GENERATOR_NAMES['A']}")
    result = result.replace("Генератор A", f"Генератор {GENERATOR_NAMES['A']}")
    result = result.replace("генератора B", f"генератора {GENERATOR_NAMES['B']}")
    result = result.replace("генератор B", f"генератор {GENERATOR_NAMES['B']}")
    result = result.replace("Генератора B", f"Генератора {GENERATOR_NAMES['B']}")
    result = result.replace("Генератор B", f"Генератор {GENERATOR_NAMES['B']}")
    return result


_original_execute_action = base.EnergyATSApp._execute_action


async def _execute_action_humanized(self: base.EnergyATSApp, action: Action) -> None:
    if action.message:
        action = replace(action, message=_humanize(action.message))
    await _original_execute_action(self, action)


base.EnergyATSApp._execute_action = _execute_action_humanized


def main() -> None:
    base.main()


if __name__ == "__main__":
    main()

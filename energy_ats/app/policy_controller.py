from __future__ import annotations

from typing import Any, List

from ats_core import ATSController, Action, Phase, Snapshot


class PolicyATSController(ATSController):
    """ATSController с пользовательской политикой выбора генераторов.

    Базовый state machine остаётся независимым от Home Assistant. Этот класс
    добавляет только deployment-политику primary/enabled. Ручные команды,
    terminal recovery и безопасный порядок коммутации находятся в базовом core.
    """

    primary_generator: str = "A"
    generator_enabled: dict[str, bool] = {"A": True, "B": True}

    @classmethod
    def configure_runtime(cls, options: dict[str, Any]) -> None:
        primary = str(options.get("primary_generator", "A")).upper()
        cls.primary_generator = primary if primary in {"A", "B"} else "A"
        cls.generator_enabled = {
            "A": bool(options.get("generator_a_enabled", True)),
            "B": bool(options.get("generator_b_enabled", True)),
        }

    def _available_primary(self) -> str | None:
        primary = self.primary_generator
        other = "B" if primary == "A" else "A"
        if self.generator_enabled.get(primary, False):
            return primary
        if self.generator_enabled.get(other, False):
            return other
        return None

    def _begin_session_and_start(
        self, now: float, s: Snapshot, mode: str, generator: str
    ) -> List[Action]:
        selected = self._available_primary()
        if selected is None:
            self.phase = Phase.GRID
            self.active_generator = None
            self.session_mode = "none"
            return [
                Action(
                    "notify_warning",
                    message="Ввод резерва невозможен: оба генератора отключены в настройках ATS.",
                ),
                Action(
                    "log",
                    message="Резервная сессия не начата: нет разрешённых генераторов.",
                    entity_id="input_button.generator_reserve_start",
                ),
            ]
        return super()._begin_session_and_start(now, s, mode, selected)

    def _begin_start(self, now: float, s: Snapshot, generator: str) -> List[Action]:
        if self.generator_enabled.get(generator, False):
            return super()._begin_start(now, s, generator)

        other = "B" if generator == "A" else "A"
        if (
            self.generator_enabled.get(other, False)
            and other not in self.failed_generators
        ):
            return super()._begin_start(now, s, other)

        return self._terminal(
            now,
            f"Генератор {generator} отключён в настройках ATS, доступного резервного генератора нет.",
            self._running_entity(generator),
        )

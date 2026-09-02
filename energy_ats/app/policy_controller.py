from __future__ import annotations

from typing import Any, List

from ats_core import ATSController, Action, Phase, Snapshot


class PolicyATSController(ATSController):
    """ATSController с пользовательской политикой выбора генераторов.

    Базовый state machine остаётся независимым от Home Assistant. Этот класс
    добавляет только deployment-политику: primary/enabled и глобальный abort
    «Вернуться на основную сеть».
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

    def _handle_manual_return(self, now: float, s: Snapshot) -> List[Action]:
        """Глобальная команда оператора «Вернуться на основную сеть».

        В отличие от автоматического возврата не ждём grid_restore_stable_time:
        человек явно приказал прекратить текущий сценарий. Но Grid Input Ready
        всё равно обязан быть TRUE.
        """
        if s.grid_ready is not True:
            return [
                Action(
                    "notify_warning",
                    message="Возврат на основную сеть невозможен: Grid Input Ready = OFF.",
                ),
                Action(
                    "log",
                    message="Команда оператора вернуть Grid отклонена: сеть не готова.",
                    entity_id="binary_sensor.grid_input_ready",
                ),
            ]

        # Если уже физически на Grid — просто прекращаем служебную сессию и
        # снимаем REMOTE. Никаких силовых переключений не требуется.
        if s.house_grid is True and s.house_generator is False and s.source_generator is False:
            self.phase = Phase.GRID
            self.phase_started = now
            self.deadline = None
            self.active_generator = None
            self.failover_generator = None
            self.session_mode = "none"
            self.failed_generators.clear()
            return [
                Action("switch_off", target="switch.generator_a_remote_start"),
                Action("switch_off", target="switch.generator_b_remote_start"),
                Action("set_session", value="off"),
                Action("set_session_mode", value="none"),
                Action(
                    "log",
                    message="Команда оператора: дом уже питается от Grid; резервный сценарий прекращён.",
                    entity_id="input_button.generator_return_to_grid",
                ),
            ]

        # Прерываем START/CHOKE/PREHEAT/FAILOVER/ON_GENERATOR. Сначала снимаем
        # REMOTE обоих агрегатов и уводим силовой селектор с генераторной шины.
        # Подключение Grid выполнит штатная фаза RETURN_SELECT_GRID только после
        # подтверждения HouseGen=False.
        self.manual_start_pending = False
        self.failover_generator = None
        self.phase = Phase.RETURN_SELECT_GRID
        self.phase_started = now
        self.deadline = now + self.cfg.transfer_confirmation_timeout
        self.grid_ready_since = now

        return [
            Action("switch_off", target="switch.generator_a_remote_start"),
            Action("switch_off", target="switch.generator_b_remote_start"),
            Action("switch_off", target="switch.use_generator_as_power_source"),
            Action(
                "log",
                message=(
                    "Команда оператора: текущий резервный сценарий прерван. "
                    "Останавливаем генераторы и возвращаем силовую схему на Grid без 60-секундной выдержки."
                ),
                entity_id="input_button.generator_return_to_grid",
            ),
        ]

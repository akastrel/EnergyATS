from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ats_core import ATSController, Action, Config, Phase, Snapshot
from ha_client import HomeAssistantClient, HomeAssistantConnectionError


# ============================================================================
# ЕДИНАЯ КАРТА HOME ASSISTANT СУЩНОСТЕЙ
# ============================================================================
#
# Вся привязка Python-кода к конкретному дому находится здесь.
# Чистый ATSController entity_id не знает вообще.
#
# Если в HA когда-нибудь переименуется физическая сущность, меняется одна строка
# в этой таблице, а алгоритм state machine и его unit-тесты остаются прежними.
# ============================================================================
ENTITIES = {
    "grid_ready": "binary_sensor.grid_input_ready",
    "house_grid": "binary_sensor.house_powered_by_grid",
    "house_generator": "binary_sensor.house_powered_by_generator",
    "generator_a_running": "binary_sensor.generator_a_is_running",
    "generator_b_running": "binary_sensor.generator_b_is_running",
    "emergency_stop": "switch.generators_emergency_stop",
    "garage_temperature": "sensor.garage_temperature",
    "remote_a": "switch.generator_a_remote_start",
    "remote_b": "switch.generator_b_remote_start",
    "grid_disconnected": "switch.disconnect_grid_power",
    "source_generator": "switch.switch_power_to_generator",
    "ats_enabled": "input_boolean.automatic_generator_transfer",
    "session_active": "input_boolean.generator_reserve_session_active",
    "session_mode": "input_select.generator_reserve_session_mode",
    "manual_start": "input_button.generator_reserve_start",
    "manual_return": "input_button.generator_return_to_grid",
}


DEFAULT_OPTIONS: dict[str, Any] = {
    # ------------------------------------------------------------------------
    # Deployment-предохранитель.
    # false = приложение только наблюдает; НИКАКИХ HA service calls к железу.
    # ------------------------------------------------------------------------
    "armed": False,
    "startup_delay": 30,
    "tick_seconds": 1.0,
    "log_level": "info",

    # Основные параметры алгоритма АВР.
    "grid_failure_delay": 5,
    "grid_restore_stable_time": 60,
    "generator_start_timeout": 90,
    "generator_stop_timeout": 90,
    "transfer_confirmation_timeout": 60,
    "generator_stop_delay": 300,

    # Заслонка.
    "choke_temperature": 10,
    "choke_hold_time": 10,

    # Прогрев.
    "preheat_warm_temperature": 10,
    "preheat_cool_temperature": -5,
    "preheat_cold_temperature": -10,
    "preheat_warm_seconds": 30,
    "preheat_cool_seconds": 60,
    "preheat_cold_seconds": 180,
    "preheat_very_cold_seconds": 300,
}


class EnergyATSApp:
    """
    Standalone Home Assistant App для управления автоматическим вводом резерва.

    Архитектура intentionally простая и жёстко разделённая:

        Home Assistant
              │
              │ WebSocket: states/events/services
              ▼
        HomeAssistantClient
              │
              │ Snapshot
              ▼
         ATSController      <- чистый Python, без HA
              │
              │ Action[]
              ▼
        EnergyATSApp._execute_action()
              │
              ▼
        Home Assistant services -> ESPHome/Bolid/Template entities

    В main.py нет бизнес-логики АВР. Если здесь появляется решение вида
    «если сеть пропала, запусти A» — это архитектурная ошибка: такое решение
    должно находиться в ats_core.py и покрываться unit-тестом.

    --------------------------------------------------------------------------
    ARMED И ATS ENABLED — РАЗНЫЕ УРОВНИ ЗАЩИТЫ
    --------------------------------------------------------------------------

    `armed` — deployment-предохранитель самого приложения.
      false: приложение подключается к HA, проверяет физические состояния,
             показывает подробный диагностический лог, но не вызывает НИ ОДНОГО
             service, способного изменить силовую систему;
      true:  разрешено исполнять Action, сформированные ATSController.

    `input_boolean.automatic_generator_transfer` — штатный пользовательский
    разрешатель АВР в UI.
      OFF: автоматический запуск по пропаданию Grid запрещён;
           ручные кнопки резерва/возврата продолжают работать.
      ON:  автоматический АВР разрешён.

    На первом физическом запуске `armed` ОБЯЗАТЕЛЬНО остаётся false.
    """

    def __init__(self, options: dict[str, Any], token: str) -> None:
        self.options = {**DEFAULT_OPTIONS, **options}
        self.armed = bool(self.options["armed"])
        self.startup_delay = float(self.options["startup_delay"])
        self.tick_seconds = max(0.2, float(self.options["tick_seconds"]))

        self.log = logging.getLogger("energy_ats")
        self.client = HomeAssistantClient(token, logger=self.log)
        self.controller = ATSController(self._config_from_options())
        self.stop_event = asyncio.Event()

        self._last_phase: Phase | None = None
        self._last_snapshot_signature: tuple[Any, ...] | None = None
        self._last_observer_log_at = 0.0

        self.client.add_state_listener(
            ENTITIES["manual_start"], self._manual_start_pressed
        )
        self.client.add_state_listener(
            ENTITIES["manual_return"], self._manual_return_pressed
        )

    def request_stop(self) -> None:
        self.stop_event.set()

    async def run(self) -> None:
        """
        Главный lifecycle процесса.

        При потере WebSocket приложение НЕ продолжает работать «по памяти».
        Оно прекращает tick, переподключается, заново получает все состояния и
        создаёт новый ATSController. Новый core делает recovery только по реальной
        физической обратной связи + persistent session helper-ам в HA.

        Это важное safety-свойство: после сетевого сбоя старая фаза автомата не
        считается достоверной.
        """
        self.log.info("Energy ATS App запущен. Версия алгоритма: ATS v1.1.")
        self.log.info(
            "Режим: %s.",
            "ARMED — реальные команды разрешены"
            if self.armed
            else "DISARMED — только наблюдение, управление железом запрещено",
        )
        self.log.info("Параметры ATS: %s", asdict(self._config_from_options()))

        if self.startup_delay > 0:
            self.log.info(
                "Стартовая выдержка %.0f с: ждём восстановления HA/ESPHome/Bolid.",
                self.startup_delay,
            )
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=self.startup_delay)
                return
            except asyncio.TimeoutError:
                pass

        reconnect_delay = 5.0
        while not self.stop_event.is_set():
            try:
                await self.client.connect()
                await self._wait_until_required_entities_ready()

                # Каждое новое WebSocket-соединение начинает новую recovery-сессию
                # чистого core. Фазу из памяти процесса намеренно не переносим.
                self.controller = ATSController(self._config_from_options())
                self._last_phase = None
                self._last_snapshot_signature = None

                if self.armed:
                    await self._armed_loop()
                else:
                    await self._observer_loop()

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.stop_event.is_set():
                    break
                self.log.error(
                    "Рабочий цикл ATS прерван: %s. Переподключение через %.0f с.",
                    exc,
                    reconnect_delay,
                )
            finally:
                await self.client.close()

            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=reconnect_delay)
            except asyncio.TimeoutError:
                pass

        self.log.info("Energy ATS App остановлен.")

    async def _armed_loop(self) -> None:
        """Рабочий цикл с реальным исполнением Action."""
        self.log.warning(
            "ATS ARMED: с этого момента Action state machine могут переключать реальное оборудование."
        )

        while not self.stop_event.is_set():
            if not self.client.connected.is_set():
                raise HomeAssistantConnectionError("WebSocket HA потерян")

            snapshot = self._snapshot()
            now = asyncio.get_running_loop().time()
            phase_before = self.controller.phase
            actions = self.controller.step(now, snapshot)
            phase_after = self.controller.phase

            self._log_phase_transition(phase_before, phase_after, snapshot)

            # Действия исполняются строго последовательно в том порядке, в котором
            # их сформировал core. Для terminal-процедуры порядок особенно важен:
            # REMOTE OFF -> source GRID -> grid connected -> Emergency Stop.
            for action in actions:
                await self._execute_action(action)

            await self._sleep_or_stop(self.tick_seconds)

    async def _observer_loop(self) -> None:
        """
        Безопасный первый режим: только наблюдение.

        Здесь мы специально НЕ прогоняем ATSController по таймерам. Если бы core
        сформировал START_GENERATOR_A, а App подавил команду из-за armed=false,
        state machine затем закономерно дождался бы timeout и объявил фиктивную
        аварию. Такой dry-run был бы вводящим в заблуждение.

        Поэтому DISARMED используется для проверки:
          * доступности всех entity;
          * реальной топологии Grid/Generator;
          * состояний REMOTE/selector/E-STOP;
          * работы WebSocket и ручных input_button событий.

        Полноценное поведение автомата проверяется unit-тестами, а физическая
        интеграция — только после сознательного armed=true.
        """
        self.log.warning(
            "ATS DISARMED: state machine не выдаёт команды; выполняется только мониторинг реальных состояний."
        )

        while not self.stop_event.is_set():
            if not self.client.connected.is_set():
                raise HomeAssistantConnectionError("WebSocket HA потерян")

            snapshot = self._snapshot()
            signature = self._snapshot_signature(snapshot)
            if signature != self._last_snapshot_signature:
                self._last_snapshot_signature = signature
                self.log.info("Физическое состояние изменилось: %s", self._snapshot_text(snapshot))

            await self._sleep_or_stop(max(1.0, self.tick_seconds))

    async def _wait_until_required_entities_ready(self) -> None:
        """
        Не начинаем ATS, пока невозможно достоверно восстановить топологию.

        Температура гаража НЕ критична: при unavailable core сознательно выбирает
        cold start и максимальный прогрев.

        input_button может иметь `unknown` до первого нажатия — это нормально;
        для него проверяем только факт существования сущности.
        """
        while not self.stop_event.is_set():
            missing = self._missing_required_entities()
            if not missing:
                snapshot = self._snapshot()
                self.log.info("Все обязательные сущности доступны.")
                self.log.info("Исходная физическая картина: %s", self._snapshot_text(snapshot))
                return

            self.log.error(
                "ATS пока не может стартовать. Недоступны обязательные сущности: %s",
                ", ".join(missing),
            )
            await self._sleep_or_stop(15.0)

    def _missing_required_entities(self) -> list[str]:
        critical_known = [
            ENTITIES["grid_ready"],
            ENTITIES["house_grid"],
            ENTITIES["house_generator"],
            ENTITIES["generator_a_running"],
            ENTITIES["generator_b_running"],
            ENTITIES["emergency_stop"],
            ENTITIES["remote_a"],
            ENTITIES["remote_b"],
            ENTITIES["grid_disconnected"],
            ENTITIES["source_generator"],
            ENTITIES["ats_enabled"],
            ENTITIES["session_active"],
            ENTITIES["session_mode"],
        ]
        existence_only = [
            ENTITIES["manual_start"],
            ENTITIES["manual_return"],
        ]

        missing: list[str] = []
        for entity_id in critical_known:
            state = self.client.get_state(entity_id)
            if state is None or state in ("unknown", "unavailable"):
                missing.append(entity_id)

        for entity_id in existence_only:
            if not self.client.has_entity(entity_id):
                missing.append(entity_id)

        return missing

    def _snapshot(self) -> Snapshot:
        return Snapshot(
            grid_ready=self._bool_state(ENTITIES["grid_ready"]),
            house_grid=self._bool_state(ENTITIES["house_grid"]),
            house_generator=self._bool_state(ENTITIES["house_generator"]),
            generator_a_running=self._bool_state(ENTITIES["generator_a_running"]),
            generator_b_running=self._bool_state(ENTITIES["generator_b_running"]),
            emergency_stop=self._bool_state(ENTITIES["emergency_stop"]),
            garage_temperature=self._float_state(ENTITIES["garage_temperature"]),
            remote_a=self._bool_state(ENTITIES["remote_a"]),
            remote_b=self._bool_state(ENTITIES["remote_b"]),
            grid_disconnected=self._bool_state(ENTITIES["grid_disconnected"]),
            source_generator=self._bool_state(ENTITIES["source_generator"]),
            ats_enabled=self._bool_state(ENTITIES["ats_enabled"]) is True,
            session_active=self._bool_state(ENTITIES["session_active"]) is True,
            session_mode=self.client.get_state(ENTITIES["session_mode"]) or "none",
        )

    def _bool_state(self, entity_id: str) -> bool | None:
        state = self.client.get_state(entity_id)
        if state == "on":
            return True
        if state == "off":
            return False
        return None

    def _float_state(self, entity_id: str) -> float | None:
        state = self.client.get_state(entity_id)
        try:
            return float(state) if state is not None else None
        except (TypeError, ValueError):
            return None

    async def _manual_start_pressed(
        self, entity_id: str, old: str | None, new: str | None
    ) -> None:
        if old == new:
            return
        if not self.armed:
            self.log.warning(
                "Нажата '%s', но App DISARMED — команда сознательно проигнорирована.",
                entity_id,
            )
            return
        self.log.info("Получена ручная команда: включить резервное питание.")
        self.controller.request_manual_start()

    async def _manual_return_pressed(
        self, entity_id: str, old: str | None, new: str | None
    ) -> None:
        if old == new:
            return
        if not self.armed:
            self.log.warning(
                "Нажата '%s', но App DISARMED — команда сознательно проигнорирована.",
                entity_id,
            )
            return
        self.log.info("Получена ручная команда: вернуться на основную сеть.")
        self.controller.request_manual_return()

    async def _execute_action(self, action: Action) -> None:
        """Единственная точка, где решение core превращается в реальный HA service call."""
        if not self.armed:
            # Дополнительная защита на самом нижнем программном уровне.
            self.log.error("Попытка выполнить Action при DISARMED подавлена: %r", action)
            return

        self.log.info(
            "ATS действие: kind=%s target=%s value=%s message=%s",
            action.kind,
            action.target,
            action.value,
            action.message,
        )

        if action.kind == "switch_on":
            await self.client.call_service(
                "switch", "turn_on", service_data={"entity_id": action.target}
            )
        elif action.kind == "switch_off":
            await self.client.call_service(
                "switch", "turn_off", service_data={"entity_id": action.target}
            )
        elif action.kind == "button":
            await self.client.call_service(
                "button", "press", service_data={"entity_id": action.target}
            )
        elif action.kind == "set_session":
            service = "turn_on" if action.value == "on" else "turn_off"
            await self.client.call_service(
                "input_boolean",
                service,
                service_data={"entity_id": ENTITIES["session_active"]},
            )
        elif action.kind == "set_session_mode":
            await self.client.call_service(
                "input_select",
                "select_option",
                service_data={
                    "entity_id": ENTITIES["session_mode"],
                    "option": action.value,
                },
            )
        elif action.kind == "notify_critical":
            await self.client.call_service(
                "script", "notify_critical", service_data={"message": action.message}
            )
        elif action.kind == "notify_warning":
            await self.client.call_service(
                "script", "notify_warning", service_data={"message": action.message}
            )
        elif action.kind == "log":
            await self.client.call_service(
                "logbook",
                "log",
                service_data={
                    "name": "АВР генераторов",
                    "message": action.message or "",
                    # По принятому правилу каждая запись привязана к entity_id.
                    "entity_id": action.entity_id
                    or ENTITIES["ats_enabled"],
                },
            )
        else:
            raise RuntimeError(f"Неизвестный Action.kind: {action.kind!r}")

    def _config_from_options(self) -> Config:
        o = self.options

        def num(key: str, default: float) -> float:
            try:
                return float(o.get(key, default))
            except (TypeError, ValueError):
                return float(default)

        defaults = Config()
        return Config(
            grid_failure_delay=num("grid_failure_delay", defaults.grid_failure_delay),
            grid_restore_stable_time=num(
                "grid_restore_stable_time", defaults.grid_restore_stable_time
            ),
            generator_start_timeout=num(
                "generator_start_timeout", defaults.generator_start_timeout
            ),
            generator_stop_timeout=num(
                "generator_stop_timeout", defaults.generator_stop_timeout
            ),
            transfer_confirmation_timeout=num(
                "transfer_confirmation_timeout",
                defaults.transfer_confirmation_timeout,
            ),
            generator_stop_delay=num(
                "generator_stop_delay", defaults.generator_stop_delay
            ),
            choke_temperature=num("choke_temperature", defaults.choke_temperature),
            choke_hold_time=num("choke_hold_time", defaults.choke_hold_time),
            preheat_warm_temp=num(
                "preheat_warm_temperature", defaults.preheat_warm_temp
            ),
            preheat_cool_temp=num(
                "preheat_cool_temperature", defaults.preheat_cool_temp
            ),
            preheat_cold_temp=num(
                "preheat_cold_temperature", defaults.preheat_cold_temp
            ),
            preheat_warm_seconds=num(
                "preheat_warm_seconds", defaults.preheat_warm_seconds
            ),
            preheat_cool_seconds=num(
                "preheat_cool_seconds", defaults.preheat_cool_seconds
            ),
            preheat_cold_seconds=num(
                "preheat_cold_seconds", defaults.preheat_cold_seconds
            ),
            preheat_very_cold_seconds=num(
                "preheat_very_cold_seconds", defaults.preheat_very_cold_seconds
            ),
        )

    def _log_phase_transition(
        self, before: Phase, after: Phase, snapshot: Snapshot
    ) -> None:
        if after == before and after == self._last_phase:
            return

        if after != before:
            self.log.info(
                "ATS phase: %s -> %s | active_generator=%s | %s",
                before.value,
                after.value,
                self.controller.active_generator or "-",
                self._snapshot_text(snapshot),
            )
        elif self._last_phase is None:
            self.log.info(
                "ATS phase: %s | %s", after.value, self._snapshot_text(snapshot)
            )
        self._last_phase = after

    @staticmethod
    def _snapshot_signature(s: Snapshot) -> tuple[Any, ...]:
        return (
            s.grid_ready,
            s.house_grid,
            s.house_generator,
            s.generator_a_running,
            s.generator_b_running,
            s.emergency_stop,
            s.remote_a,
            s.remote_b,
            s.grid_disconnected,
            s.source_generator,
            s.ats_enabled,
            s.session_active,
            s.session_mode,
            s.garage_temperature,
        )

    @staticmethod
    def _snapshot_text(s: Snapshot) -> str:
        temp = "?" if s.garage_temperature is None else f"{s.garage_temperature:.1f}°C"
        return (
            f"GridReady={s.grid_ready}, HouseGrid={s.house_grid}, "
            f"HouseGen={s.house_generator}, A={s.generator_a_running}, "
            f"B={s.generator_b_running}, RemoteA={s.remote_a}, "
            f"RemoteB={s.remote_b}, GridDisconnected={s.grid_disconnected}, "
            f"SourceGenerator={s.source_generator}, EStop={s.emergency_stop}, "
            f"ATS={s.ats_enabled}, Session={s.session_active}/{s.session_mode}, "
            f"Garage={temp}"
        )

    async def _sleep_or_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass


# ============================================================================
# ЗАГРУЗКА /data/options.json
# ============================================================================
def load_options(path: str | Path = "/data/options.json") -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return dict(DEFAULT_OPTIONS)
    with p.open("r", encoding="utf-8") as file:
        loaded = json.load(file)
    if not isinstance(loaded, dict):
        raise ValueError("/data/options.json должен содержать JSON object")
    return {**DEFAULT_OPTIONS, **loaded}


def configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


async def async_main() -> None:
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise RuntimeError(
            "SUPERVISOR_TOKEN не найден. Проверьте homeassistant_api: true в config.yaml App."
        )

    options = load_options()
    configure_logging(str(options.get("log_level", "info")))
    app = EnergyATSApp(options, token)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, app.request_stop)
        except NotImplementedError:
            pass

    await app.run()


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

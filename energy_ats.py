from __future__ import annotations

import appdaemon.plugins.hass.hassapi as hass

from ats_core import ATSController, Config, Snapshot, Action


class EnergyATS(hass.Hass):
    """
    Адаптер между Home Assistant/AppDaemon и чистым ATSController.

    Здесь намеренно почти нет логики принятия решений. Ответственность адаптера:
      * прочитать реальные HA entity и собрать Snapshot;
      * прочитать настраиваемые параметры из energy_ats.yaml и собрать Config;
      * примерно раз в секунду вызвать ATSController.step();
      * превратить декларативные Action в реальные HA service calls;
      * принять нажатия ручных UI-кнопок и передать намерение в core.

    Такое разделение принципиально: алгоритм АВР можно полностью прогонять в
    unit-тестах без Home Assistant, AppDaemon и тем более без реальных генераторов.

    Защитный deployment-флаг:
      armed: false
        приложение загружается и проверяет сущности, но НЕ управляет железом;
      armed: true
        Action из ATSController действительно исполняются в Home Assistant.
    """

    # ======================================================================
    # ЕДИНАЯ КАРТА СУЩНОСТЕЙ
    # ======================================================================
    # В core нет ни одного обращения к Home Assistant. Все реальные entity_id
    # сосредоточены здесь, чтобы их было легко проверить глазами и изменить
    # при переименовании сущностей, не затрагивая state machine.
    # ======================================================================
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

    def _config_from_args(self) -> Config:
        """Собрать типизированный Config из параметров AppDaemon YAML."""
        p = self.args.get("parameters", {})
        pre = p.get("preheat", {})
        defaults = Config()

        def num(mapping, key, default):
            try:
                return float(mapping.get(key, default))
            except (TypeError, ValueError):
                return float(default)

        return Config(
            grid_failure_delay=num(p, "grid_failure_delay", defaults.grid_failure_delay),
            grid_restore_stable_time=num(p, "grid_restore_stable_time", defaults.grid_restore_stable_time),
            generator_start_timeout=num(p, "generator_start_timeout", defaults.generator_start_timeout),
            generator_stop_timeout=num(p, "generator_stop_timeout", defaults.generator_stop_timeout),
            transfer_confirmation_timeout=num(p, "transfer_confirmation_timeout", defaults.transfer_confirmation_timeout),
            generator_stop_delay=num(p, "generator_stop_delay", defaults.generator_stop_delay),
            choke_temperature=num(p, "choke_temperature", defaults.choke_temperature),
            choke_hold_time=num(p, "choke_hold_time", defaults.choke_hold_time),
            preheat_warm_temp=num(pre, "warm_temperature", defaults.preheat_warm_temp),
            preheat_cool_temp=num(pre, "cool_temperature", defaults.preheat_cool_temp),
            preheat_cold_temp=num(pre, "cold_temperature", defaults.preheat_cold_temp),
            preheat_warm_seconds=num(pre, "warm_seconds", defaults.preheat_warm_seconds),
            preheat_cool_seconds=num(pre, "cool_seconds", defaults.preheat_cool_seconds),
            preheat_cold_seconds=num(pre, "cold_seconds", defaults.preheat_cold_seconds),
            preheat_very_cold_seconds=num(pre, "very_cold_seconds", defaults.preheat_very_cold_seconds),
        )


    def initialize(self):
        # initialize() вызывается самим AppDaemon при загрузке приложения.
        # На этом этапе мы НИЧЕГО не переключаем: лишь читаем настройки,
        # подписываемся на ручные кнопки и запускаем отложенную recovery-проверку.
        self.armed = bool(self.args.get("armed", False))
        self.startup_delay = float(self.args.get("startup_delay", 30))
        self.tick_seconds = float(self.args.get("tick_seconds", 1))
        self.controller = ATSController(self._config_from_args())
        self.started = False

        self.listen_state(self._manual_start_pressed, self.ENTITIES["manual_start"])
        self.listen_state(self._manual_return_pressed, self.ENTITIES["manual_return"])

        self.log(
            "Energy ATS загружен: %s. Восстановление состояния через %.0f с.",
            "ARMED — УПРАВЛЕНИЕ РЕАЛЬНЫМ ЖЕЛЕЗОМ РАЗРЕШЕНО" if self.armed else "DISARMED — ТОЛЬКО НАБЛЮДЕНИЕ",
            self.startup_delay,
        )
        self.run_in(self._start_loop, self.startup_delay)

    def _start_loop(self, kwargs):
        # После startup_delay все критические ESPHome/Bolid/template-сущности
        # должны успеть восстановиться. Если что-то всё ещё unavailable, ATS
        # не угадывает состояние, а ждёт ещё 15 секунд и проверяет повторно.
        missing = self._missing_required_entities()
        if missing:
            self.log("ATS не может стартовать: отсутствуют/недоступны сущности: %s", ", ".join(missing), level="ERROR")
            # Повторяем проверку вместо работы по выдуманным/неполным состояниям.
            self.run_in(self._start_loop, 15)
            return

        self.started = True
        self.controller.cfg = self._config_from_args()

        if not self.armed:
            self.log("ATS работает в DISARMED: команды switch/button/service силовой системе не выполняются.", level="WARNING")
            return

        # Первый tick сразу выполняет recovery по реальной физической картине.
        # Далее используется детерминированный polling с заданным tick_seconds.
        self._tick({})
        self.run_every(self._tick, "now+1", self.tick_seconds)

    def _tick(self, kwargs):
        # Один цикл управления:
        #   HA -> Snapshot -> ATSController.step() -> Action[] -> HA
        # Параметры перечитываются каждый tick, поэтому настройку времён можно
        # менять в energy_ats.yaml без изменения Python-алгоритма.
        if not self.armed:
            return
        self.controller.cfg = self._config_from_args()
        snapshot = self._snapshot()
        now = self.get_now_ts()
        for action in self.controller.step(now, snapshot):
            self._execute(action)

    def _manual_start_pressed(self, entity, attribute, old, new, kwargs):
        # input_button меняет state при каждом нажатии. Здесь лишь ставим флаг
        # намерения; физическая процедура запуска будет выполнена core на tick.
        if old == new or not self.armed:
            if not self.armed and old != new:
                self.log("Нажата кнопка ручного резерва при DISARMED; команда проигнорирована.", level="WARNING")
            return
        self.controller.request_manual_start()

    def _manual_return_pressed(self, entity, attribute, old, new, kwargs):
        # Аналогично ручному старту: кнопка не управляет контакторами напрямую.
        if old == new or not self.armed:
            if not self.armed and old != new:
                self.log("Нажата кнопка возврата на Grid при DISARMED; команда проигнорирована.", level="WARNING")
            return
        self.controller.request_manual_return()

    # ======================================================================
    # HOME ASSISTANT -> ЧИСТЫЙ CORE
    # ======================================================================
    def _snapshot(self) -> Snapshot:
        # Собираем атомарный для логики снимок текущих состояний. Значение None
        # означает unknown/unavailable и сохраняется как отдельное состояние,
        # а не превращается автоматически в False.
        return Snapshot(
            grid_ready=self._bool_state(self.ENTITIES["grid_ready"]),
            house_grid=self._bool_state(self.ENTITIES["house_grid"]),
            house_generator=self._bool_state(self.ENTITIES["house_generator"]),
            generator_a_running=self._bool_state(self.ENTITIES["generator_a_running"]),
            generator_b_running=self._bool_state(self.ENTITIES["generator_b_running"]),
            emergency_stop=self._bool_state(self.ENTITIES["emergency_stop"]),
            garage_temperature=self._float_state(self.ENTITIES["garage_temperature"]),
            remote_a=self._bool_state(self.ENTITIES["remote_a"]),
            remote_b=self._bool_state(self.ENTITIES["remote_b"]),
            grid_disconnected=self._bool_state(self.ENTITIES["grid_disconnected"]),
            source_generator=self._bool_state(self.ENTITIES["source_generator"]),
            ats_enabled=self._bool_state(self.ENTITIES["ats_enabled"]) is True,
            session_active=self._bool_state(self.ENTITIES["session_active"]) is True,
            session_mode=self.get_state(self.ENTITIES["session_mode"]) or "none",
        )

    def _missing_required_entities(self):
        # Без этих сущностей невозможно безопасно реконструировать топологию.
        # sensor.garage_temperature сюда намеренно не входит: при его отсутствии
        # core выберет cold start и максимальный прогрев.
        required = [
            self.ENTITIES["grid_ready"],
            self.ENTITIES["house_grid"],
            self.ENTITIES["house_generator"],
            self.ENTITIES["generator_a_running"],
            self.ENTITIES["generator_b_running"],
            self.ENTITIES["emergency_stop"],
            self.ENTITIES["remote_a"],
            self.ENTITIES["remote_b"],
            self.ENTITIES["grid_disconnected"],
            self.ENTITIES["source_generator"],
            self.ENTITIES["ats_enabled"],
            self.ENTITIES["session_active"],
            self.ENTITIES["session_mode"],
            self.ENTITIES["manual_start"],
            self.ENTITIES["manual_return"],
        ]
        missing = []
        for entity in required:
            state = self.get_state(entity)
            if state is None or state in ("unknown", "unavailable"):
                missing.append(entity)
        return missing

    def _bool_state(self, entity):
        state = self.get_state(entity)
        if state == "on":
            return True
        if state == "off":
            return False
        return None

    def _float_state(self, entity):
        state = self.get_state(entity)
        try:
            return float(state)
        except (TypeError, ValueError):
            return None

    # ======================================================================
    # ЧИСТЫЙ CORE -> HOME ASSISTANT
    # ======================================================================
    # Только этот метод имеет право преобразовать Action в реальные команды.
    # Благодаря этому armed=false гарантированно отсекает управление железом
    # выше по цепочке, ещё до попадания сюда.
    # ======================================================================
    def _execute(self, a: Action):
        # Каждая команда сначала попадает в лог AppDaemon. Это позволяет затем
        # сопоставлять решение state machine с фактическими изменениями сущностей.
        self.log("ATS действие: %s target=%s value=%s message=%s", a.kind, a.target, a.value, a.message)

        if a.kind == "switch_on":
            self.turn_on(a.target)
        elif a.kind == "switch_off":
            self.turn_off(a.target)
        elif a.kind == "button":
            self.call_service("button/press", entity_id=a.target)
        elif a.kind == "set_session":
            if a.value == "on":
                self.turn_on(self.ENTITIES["session_active"])
            else:
                self.turn_off(self.ENTITIES["session_active"])
        elif a.kind == "set_session_mode":
            self.call_service(
                "input_select/select_option",
                entity_id=self.ENTITIES["session_mode"],
                option=a.value,
            )
        elif a.kind == "notify_critical":
            self.call_service("script/notify_critical", message=a.message)
        elif a.kind == "notify_warning":
            self.call_service("script/notify_warning", message=a.message)
        elif a.kind == "log":
            self.call_service(
                "logbook/log",
                name="АВР генераторов",
                message=a.message,
                entity_id=a.entity_id or "input_boolean.automatic_generator_transfer",
            )
        else:
            self.log("Неизвестный тип действия ATS: %s", a.kind, level="ERROR")

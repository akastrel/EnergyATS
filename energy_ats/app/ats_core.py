from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Set


class Phase(str, Enum):
    """
    Фазы конечного автомата АВР.

    Важно: это НЕ сохранённое состояние реального электрооборудования.
    Фаза лишь описывает, какой шаг алгоритма ATS выполняется сейчас. После
    restart Energy ATS App точная старая фаза намеренно не восстанавливается:
    контроллер заново смотрит на физические датчики и выбирает безопасное
    продолжение сценария.

    Краткая карта основного happy path:

        GRID
          -> GRID_FAILURE_DELAY
          -> STARTING
          -> CHOKE_HOLD (только при холодном старте)
          -> PREHEATING
          -> TRANSFER_DISCONNECT_GRID
          -> TRANSFER_SELECT_GENERATOR
          -> ON_GENERATOR
          -> GRID_RESTORE_WAIT
          -> RETURN_SELECT_GRID
          -> RETURN_CONNECT_GRID
          -> COOLDOWN
          -> STOPPING
          -> GRID

    TERMINAL — конечная аварийная фаза. Из неё автоматика сама не выходит.
    """
    WAITING = "waiting"
    GRID = "grid"
    EXTERNAL = "external"
    GRID_FAILURE_DELAY = "grid_failure_delay"
    STARTING = "starting"
    CHOKE_HOLD = "choke_hold"
    PREHEATING = "preheating"
    WAIT_GRID_DECISION = "wait_grid_decision"
    TRANSFER_DISCONNECT_GRID = "transfer_disconnect_grid"
    TRANSFER_SELECT_GENERATOR = "transfer_select_generator"
    ON_GENERATOR = "on_generator"
    GRID_RESTORE_WAIT = "grid_restore_wait"
    RETURN_SELECT_GRID = "return_select_grid"
    RETURN_CONNECT_GRID = "return_connect_grid"
    COOLDOWN = "cooldown"
    STOPPING = "stopping"
    TERMINAL = "terminal"


@dataclass(frozen=True)
class Snapshot:
    """
    Снимок фактического состояния системы на один цикл управления.

    Energy ATS App каждую секунду читает сущности Home Assistant и формирует один
    Snapshot. Чистый ATSController ничего не знает о HA: он видит только этот
    снимок и на его основании выдаёт список требуемых Action.

    Optional[bool] используется сознательно: None означает, что состояние
    физической сущности неизвестно/unavailable. Такой случай нельзя молча
    трактовать как False.
    """

    # Физическая обратная связь и вычисляемые признаки
    grid_ready: Optional[bool]
    house_grid: Optional[bool]
    house_generator: Optional[bool]
    generator_a_running: Optional[bool]
    generator_b_running: Optional[bool]
    emergency_stop: Optional[bool]
    garage_temperature: Optional[float]

    # Фактические состояния исполнительных механизмов
    remote_a: Optional[bool] = None
    remote_b: Optional[bool] = None
    grid_disconnected: Optional[bool] = None
    source_generator: Optional[bool] = None

    # Служебные helper-ы HA: намерение оператора и принадлежность сессии
    ats_enabled: bool = False
    session_active: bool = False
    # none      — активной сессии ATS нет
    # automatic — сессию инициировало автоматическое АВР
    # manual    — резерв включён человеком через UI; автоматический возврат
    #             на Grid в таком режиме намеренно не выполняется
    session_mode: str = "none"  # none / automatic / manual


@dataclass(frozen=True)
class Config:
    """Настраиваемые временные и температурные параметры алгоритма АВР."""

    # Сколько должна отсутствовать сеть, прежде чем АВР вообще начнёт запуск.
    grid_failure_delay: float = 5.0

    # После появления Grid Input Ready сеть должна непрерывно оставаться
    # нормальной это время, прежде чем мы решимся вернуть дом с генератора.
    grid_restore_stable_time: float = 60.0

    # DKG116 сам выполняет три попытки запуска; ATS ждёт итоговый RUNNING.
    generator_start_timeout: float = 90.0

    # После снятия REMOTE START двигатель обязан остановиться за это время.
    # Превышение timeout считается терминальной аварией.
    generator_stop_timeout: float = 90.0

    # Максимальное ожидание физического подтверждения каждого силового шага.
    transfer_confirmation_timeout: float = 60.0

    # Работа генератора без нагрузки после возврата сети перед штатной остановкой.
    generator_stop_delay: float = 300.0

    # Пока нет датчика температуры двигателя, необходимость заслонки
    # оценивается по температуре гаража. Ниже порога используем cold start.
    choke_temperature: float = 10.0

    # Текущая эмпирическая политика запуска различается по генераторам:
    #   always      — всегда физически закрывать заслонку перед запуском;
    #   temperature — закрывать ниже choke_temperature, а при неизвестной
    #                 температуре выбирать консервативный cold start;
    #   never       — всегда запускать с физически открытой заслонкой.
    generator_a_choke_mode: str = "always"
    generator_b_choke_mode: str = "temperature"

    # После появления RUNNING держим закрытую заслонку ещё это время.
    choke_hold_time: float = 10.0

    # Температурные границы и длительности прогрева перед подачей нагрузки.
    preheat_warm_temp: float = 10.0
    preheat_cool_temp: float = -5.0
    preheat_cold_temp: float = -10.0
    preheat_warm_seconds: float = 30.0
    preheat_cool_seconds: float = 60.0
    preheat_cold_seconds: float = 180.0
    preheat_very_cold_seconds: float = 300.0


@dataclass(frozen=True)
class Action:
    """
    Декларативная команда, которую core просит выполнить адаптер Energy ATS App.

    Сам ATSController никогда не вызывает Home Assistant напрямую. Благодаря
    этому весь алгоритм можно тестировать без HA и без физического железа.
    """
    kind: str
    target: Optional[str] = None
    value: Optional[str] = None
    message: Optional[str] = None
    entity_id: Optional[str] = None


@dataclass
class ATSController:
    """
    Чистый конечный автомат АВР, не зависящий от Home Assistant/Energy ATS App.

    На вход каждого шага приходит Snapshot — фактическая картина физических
    датчиков и исполнительных реле. На выходе получается список Action, которые
    уже исполняет адаптер main.py / ha_client.py.

    Главный принцип архитектуры:
      * ФИЗИЧЕСКАЯ ОБРАТНАЯ СВЯЗЬ — единственный источник истины.
      * Helper-ы HA хранят только намерение оператора: разрешён ли АВР и кому
        принадлежит текущая резервная сессия (automatic/manual).
      * Точную фазу процесса в helper не сохраняем. После restart она может
        устареть и противоречить реальному положению контакторов/генераторов.

    Поэтому restart восстанавливается консервативно: читаем датчики, понимаем
    реальную топологию и продолжаем от неё. Если генератор уже работает, но мы
    не знаем, сколько он успел прогреться до restart, прогрев запускается заново.
    Это безопаснее, чем подавать нагрузку на потенциально холодный двигатель.
    """

    cfg: Config = field(default_factory=Config)
    phase: Phase = Phase.WAITING
    active_generator: Optional[str] = None  # "A" / "B"
    session_mode: str = "none"
    failed_generators: Set[str] = field(default_factory=set)
    phase_started: float = 0.0
    deadline: Optional[float] = None
    grid_ready_since: Optional[float] = None
    generator_power_lost_since: Optional[float] = None
    grid_failed_since: Optional[float] = None
    choke_used: bool = False
    manual_start_pending: bool = False
    manual_return_pending: bool = False
    bootstrapped: bool = False

    def request_manual_start(self) -> None:
        self.manual_start_pending = True

    def request_manual_return(self) -> None:
        self.manual_return_pending = True

    # ==================================================================
    # ГЛАВНЫЙ ЦИКЛ КОНЕЧНОГО АВТОМАТА
    # ==================================================================
    # Метод вызывается примерно раз в секунду. На каждом вызове выполняется
    # максимум один логический переход/набор команд, после чего мы ждём новый
    # Snapshot с подтверждением физического результата предыдущего действия.
    # ==================================================================
    def step(self, now: float, s: Snapshot) -> List[Action]:
        actions: List[Action] = []

        if not self.bootstrapped:
            actions.extend(self._bootstrap(now, s))
            self.bootstrapped = True
            return actions

        # Emergency Stop — жёсткая граница автоматики. Если он уже активен,
        # мы не имеем права выполнять НИКАКИЕ дальнейшие команды генераторам.
        # Снять этот запрет должен человек после физической проверки системы.
        if s.emergency_stop is True and self.phase != Phase.TERMINAL:
            self.phase = Phase.TERMINAL
            self.active_generator = None
            self.session_mode = "none"
            return [
                Action("set_session", value="off"),
                Action("set_session_mode", value="none"),
                Action("log", message="ATS заблокирован: активен Generators Emergency Stop.",
                       entity_id="switch.generators_emergency_stop"),
            ]

        if self.phase == Phase.TERMINAL:
            return actions

        # Команды человека имеют приоритет над обычным продвижением автомата.
        # При этом ручной старт всё равно использует те же безопасные процедуры
        # запуска/прогрева/силового переключения, что и автоматический АВР.
        if self.manual_start_pending:
            self.manual_start_pending = False
            actions.extend(self._handle_manual_start(now, s))
            if actions:
                return actions

        if self.manual_return_pending:
            self.manual_return_pending = False
            actions.extend(self._handle_manual_return(now, s))
            if actions:
                return actions

        # Таймер стабильности Grid обновляем во ВСЕХ активных фазах. Это важно:
        # сеть может вернуться во время STARTING, CHOKE или PREHEATING, и тогда
        # отсчёт 60 секунд должен начаться сразу, а не после завершения прогрева.
        self._update_grid_stability(now, s)

        # ------------------------------------------------------------------
        # WAITING — ожидаем, пока после старта доступны критические датчики.
        # Никаких силовых действий в этой фазе не выполняется.
        # ------------------------------------------------------------------
        if self.phase == Phase.WAITING:
            if self._critical_states_known(s):
                return self._bootstrap(now, s)
            return actions

        # ------------------------------------------------------------------
        # EXTERNAL — обнаружен генератор/питание от генератора, но текущая
        # сессия НЕ принадлежит ATS. Считаем это ручным внешним управлением.
        # Единственная задача — наблюдать и не мешать человеку.
        # ------------------------------------------------------------------
        if self.phase == Phase.EXTERNAL:
            # Генератор обнаружен работающим вне нашей сессии. Не вмешиваемся:
            # его мог вручную запустить человек для обслуживания/проверки.
            if not self._any_generator_running(s) and s.house_generator is not True:
                self.phase = Phase.GRID
                self.phase_started = now
            return actions

        # ------------------------------------------------------------------
        # GRID — нормальное исходное состояние: резервная сессия не выполняется.
        # Автоматический сценарий начинается только если Grid Input Ready=OFF
        # И разрешатель automatic_generator_transfer включён.
        # ------------------------------------------------------------------
        if self.phase == Phase.GRID:
            if s.grid_ready is False and s.ats_enabled:
                self.phase = Phase.GRID_FAILURE_DELAY
                self.phase_started = now
                self.deadline = now + self.cfg.grid_failure_delay
                return [Action("log", message="Основная сеть недоступна. Запущена выдержка перед АВР.",
                               entity_id="binary_sensor.grid_input_ready")]
            return actions

        # ------------------------------------------------------------------
        # GRID_FAILURE_DELAY — антидребезг/защита от короткого провала сети.
        # Ждём grid_failure_delay. Если Grid успел вернуться — ничего не запускаем.
        # ------------------------------------------------------------------
        if self.phase == Phase.GRID_FAILURE_DELAY:
            if s.grid_ready is True:
                self.phase = Phase.GRID
                self.deadline = None
                return [Action("log", message="Сеть восстановилась до запуска генератора; АВР отменён.",
                               entity_id="binary_sensor.grid_input_ready")]
            if not s.ats_enabled:
                self.phase = Phase.GRID
                self.deadline = None
                return [Action("log", message="АВР отключён во время выдержки; автозапуск отменён.",
                               entity_id="input_boolean.automatic_generator_transfer")]
            if self.deadline is not None and now >= self.deadline:
                return self._begin_session_and_start(now, s, "automatic", "A")
            return actions

        # ------------------------------------------------------------------
        # STARTING — REMOTE уже подан выбранному генератору.
        # DKG116 сам делает до трёх попыток; мы лишь ждём физический RUNNING.
        # Если сеть успела стабильно вернуться — ввод резерва отменяем.
        # ------------------------------------------------------------------
        if self.phase == Phase.STARTING:
            if self._automatic_grid_return_is_stable(now, s):
                return self._abort_pretransfer_for_restored_grid(now, s)

            if self._active_running(s):
                if self.choke_used:
                    self.phase = Phase.CHOKE_HOLD
                    self.phase_started = now
                    self.deadline = now + self.cfg.choke_hold_time
                    return [Action("log", message=f"Генератор {self.active_generator} запущен; заслонка остаётся закрытой ещё {int(self.cfg.choke_hold_time)} с.",
                                   entity_id=self._running_entity())]
                return self._begin_preheat(now, s)

            if self.deadline is not None and now >= self.deadline:
                return self._generator_failed(now, s, f"Генератор {self.active_generator} не запустился за {int(self.cfg.generator_start_timeout)} с.")
            return actions

        # ------------------------------------------------------------------
        # CHOKE_HOLD — холодный двигатель уже запустился, но заслонку ещё
        # choke_hold_time секунд держим физически закрытой. После этого переводим
        # её в RUN и только тогда начинаем основной прогрев перед нагрузкой.
        # ------------------------------------------------------------------
        if self.phase == Phase.CHOKE_HOLD:
            if not self._active_running(s):
                return self._generator_failed(now, s, f"Генератор {self.active_generator} заглох сразу после запуска.")
            if self._automatic_grid_return_is_stable(now, s):
                actions.append(Action("button", target=self._choke_run_button()))
                actions.extend(self._abort_pretransfer_for_restored_grid(now, s))
                return actions
            if self.deadline is not None and now >= self.deadline:
                actions.append(Action("button", target=self._choke_run_button()))
                actions.extend(self._begin_preheat(now, s))
                return actions
            return actions

        # ------------------------------------------------------------------
        # PREHEATING — генератор стабильно работает без нагрузки.
        # Длительность зависит от температуры гаража. Потеря RUNNING на этом
        # этапе считается отказом генератора и запускает fallback на второй.
        # ------------------------------------------------------------------
        if self.phase == Phase.PREHEATING:
            if not self._active_running(s):
                return self._generator_failed(now, s, f"Генератор {self.active_generator} заглох во время прогрева.")

            if self._automatic_grid_return_is_stable(now, s):
                return self._abort_pretransfer_for_restored_grid(now, s)

            if self.deadline is not None and now >= self.deadline:
                # Если сеть уже появилась, но ещё не выдержала заданный
                # гистерезис стабильности, на генератор НЕ переключаемся.
                # Возможны два исхода:
                #   1) Grid выдержит 60 с -> резерв не потребуется;
                #   2) Grid снова исчезнет -> сразу продолжаем transfer.
                if self.session_mode == "automatic" and s.grid_ready is True:
                    self.phase = Phase.WAIT_GRID_DECISION
                    self.phase_started = now
                    self.deadline = None
                    return [Action("log", message="Прогрев завершён, но сеть появилась. Ждём окончания выдержки стабильности сети.",
                                   entity_id="binary_sensor.grid_input_ready")]
                return self._begin_transfer_to_generator(now, s)
            return actions

        # ------------------------------------------------------------------
        # WAIT_GRID_DECISION — прогрев уже закончен, но Grid появился и пока
        # не ясно, стабилен ли он. Не делаем лишний transfer: ждём либо 60 с
        # стабильного Grid, либо нового исчезновения сети.
        # ------------------------------------------------------------------
        if self.phase == Phase.WAIT_GRID_DECISION:
            if not self._active_running(s):
                return self._generator_failed(now, s, f"Генератор {self.active_generator} заглох во время ожидания решения по сети.")
            if self._automatic_grid_return_is_stable(now, s):
                return self._abort_pretransfer_for_restored_grid(now, s)
            if s.grid_ready is False:
                return self._begin_transfer_to_generator(now, s)
            return actions

        # ------------------------------------------------------------------
        # TRANSFER_DISCONNECT_GRID — первый силовой шаг ввода резерва.
        # «Перебздеваем»: отключаем Grid перед силовым селектором и обязательно
        # подтверждаем датчиком, что дом действительно больше не питается от Grid.
        # ------------------------------------------------------------------
        if self.phase == Phase.TRANSFER_DISCONNECT_GRID:
            if not self._active_running(s):
                return self._generator_failed(now, s, f"Генератор {self.active_generator} заглох во время переключения на резерв.")
            if s.house_grid is False:
                self.phase = Phase.TRANSFER_SELECT_GENERATOR
                self.phase_started = now
                self.deadline = now + self.cfg.transfer_confirmation_timeout
                return [
                    Action("switch_on", target="switch.use_generator_as_power_source"),
                    Action("log", message="Основная сеть отключена; силовой источник переключается на генератор.",
                           entity_id="switch.use_generator_as_power_source"),
                ]
            if self.deadline is not None and now >= self.deadline:
                return self._terminal(now, "Не подтверждено отключение дома от основной сети перед вводом генератора.",
                                      "binary_sensor.house_powered_by_grid")
            return actions

        # ------------------------------------------------------------------
        # TRANSFER_SELECT_GENERATOR — Grid уже изолирован, переводим силовой
        # селектор на генератор и ждём независимое физическое подтверждение
        # house_powered_by_generator. Неподтверждённый transfer -> TERMINAL.
        # ------------------------------------------------------------------
        if self.phase == Phase.TRANSFER_SELECT_GENERATOR:
            if not self._active_running(s):
                return self._generator_failed(now, s, f"Генератор {self.active_generator} заглох во время ввода резерва.")
            if s.house_generator is True:
                self.phase = Phase.ON_GENERATOR
                self.phase_started = now
                self.deadline = None
                self.generator_power_lost_since = None
                return [
                    Action("notify_warning", message=f"Дом переведён на резервное питание от генератора {self.active_generator}."),
                    Action("log", message=f"Ввод резерва завершён: дом питается от генератора {self.active_generator}.",
                           entity_id="binary_sensor.house_powered_by_generator"),
                ]
            if self.deadline is not None and now >= self.deadline:
                return self._terminal(now, "Генератор работает, но питание дома от генератора не подтверждено.",
                                      "binary_sensor.house_powered_by_generator")
            return actions

        # ------------------------------------------------------------------
        # ON_GENERATOR — штатная работа дома от выбранного генератора.
        # Контролируем одновременно сам RUNNING и наличие питания дома от GEN.
        # В automatic-сессии появление Grid запускает выдержку возврата.
        # ------------------------------------------------------------------
        if self.phase == Phase.ON_GENERATOR:
            if not self._active_running(s):
                return self._generator_failed(now, s, f"Работающий генератор {self.active_generator} остановился во время питания дома.")

            if s.house_generator is not True:
                if self.generator_power_lost_since is None:
                    self.generator_power_lost_since = now
                elif now - self.generator_power_lost_since >= self.cfg.transfer_confirmation_timeout:
                    return self._terminal(now, "Потеряно подтверждение питания дома от работающего генератора.",
                                          "binary_sensor.house_powered_by_generator")
            else:
                self.generator_power_lost_since = None

            # Только автоматическая сессия сама инициирует возврат на Grid.
            # В ручной сессии появление сети лишь наблюдается: решение о возврате
            # остаётся за человеком и кнопкой «Вернуться на основную сеть».
            if self.session_mode == "automatic" and s.grid_ready is True:
                self.phase = Phase.GRID_RESTORE_WAIT
                self.phase_started = now
                if self.grid_ready_since is None:
                    self.grid_ready_since = now
                return [Action("log", message="Основная сеть появилась. Начата выдержка стабильности перед возвратом.",
                               entity_id="binary_sensor.grid_input_ready")]
            return actions

        # ------------------------------------------------------------------
        # GRID_RESTORE_WAIT — Grid появился, но мы ему пока не доверяем.
        # Он должен непрерывно быть Ready grid_restore_stable_time. Любой новый
        # провал сбрасывает выдержку, дом продолжает работать от генератора.
        # ------------------------------------------------------------------
        if self.phase == Phase.GRID_RESTORE_WAIT:
            if not self._active_running(s) and s.house_generator is True:
                return self._generator_failed(now, s, f"Генератор {self.active_generator} остановился до возврата на сеть.")
            if s.grid_ready is not True:
                self.phase = Phase.ON_GENERATOR
                self.grid_ready_since = None
                return [Action("log", message="Сеть снова пропала во время выдержки возврата; остаёмся на генераторе.",
                               entity_id="binary_sensor.grid_input_ready")]
            if self.grid_ready_since is not None and now - self.grid_ready_since >= self.cfg.grid_restore_stable_time:
                return self._begin_return_to_grid(now, s)
            return actions

        # ------------------------------------------------------------------
        # RETURN_SELECT_GRID — начинаем возврат после подтверждённой стабильности.
        # Сначала силовой селектор переводится в GRID, но внешний Grid пока ещё
        # изолирован disconnect-реле. Ждём исчезновения генераторного питания.
        # ------------------------------------------------------------------
        if self.phase == Phase.RETURN_SELECT_GRID:
            # Возврат выполняем последовательно. Сначала переводим силовой
            # переключатель с GENERATOR на GRID и ЖДЁМ физического исчезновения
            # питания от генератора. Только после этого разрешаем Grid перед ним.
            if s.house_generator is False:
                self.phase = Phase.RETURN_CONNECT_GRID
                self.phase_started = now
                self.deadline = now + self.cfg.transfer_confirmation_timeout
                return [
                    Action("switch_on", target="switch.grid_power"),
                    Action("log", message="Источник установлен в GRID; подключаем основную сеть.",
                           entity_id="switch.grid_power"),
                ]
            if self.deadline is not None and now >= self.deadline:
                return self._terminal(now, "Не подтверждено отключение питания дома от генератора при возврате на сеть.",
                                      "binary_sensor.house_powered_by_generator")
            return actions

        # ------------------------------------------------------------------
        # RETURN_CONNECT_GRID — селектор уже в GRID, теперь разрешаем внешний
        # ввод сети и ждём house_powered_by_grid. Если сама сеть в этот момент
        # снова исчезла, это не terminal: при работающем GEN возвращаем резерв.
        # ------------------------------------------------------------------
        if self.phase == Phase.RETURN_CONNECT_GRID:
            # Если Grid действительно пропал прямо во время физического возврата,
            # это не считаем отказом коммутации: сеть просто снова исчезла. Если
            # генератор ещё работает, возвращаемся к процедуре ввода резерва.
            if s.grid_ready is False:
                if self._active_running(s):
                    return self._begin_transfer_to_generator(now, s)
                return self._generator_failed(now, s, f"Сеть исчезла во время возврата, а генератор {self.active_generator} уже не работает.")

            if s.house_grid is True:
                self.phase = Phase.COOLDOWN
                self.phase_started = now
                self.deadline = now + self.cfg.generator_stop_delay
                self.grid_failed_since = None
                # С этого момента дом уже безопасно питается от Grid. Ручная
                # команда возврата выполнена. Если сеть снова пропадёт во время
                # cooldown, автоматический повторный ввод резерва разрешён только
                # при включённом input_boolean.automatic_generator_transfer.
                return [
                    Action("notify_warning", message="Питание дома от основной сети восстановлено."),
                    Action("log", message=f"Дом возвращён на основную сеть. Генератор {self.active_generator} оставлен без нагрузки на охлаждение {int(self.cfg.generator_stop_delay)} с.",
                           entity_id="binary_sensor.house_powered_by_grid"),
                ]
            if self.deadline is not None and now >= self.deadline:
                return self._terminal(now, "Grid Input Ready есть, но питание дома от основной сети не подтвердилось.",
                                      "binary_sensor.house_powered_by_grid")
            return actions

        # ------------------------------------------------------------------
        # COOLDOWN — дом уже на Grid, генератор остаётся работать БЕЗ нагрузки
        # generator_stop_delay секунд. Это единое правило и для обычного возврата,
        # и когда Grid вернулся ещё во время запуска/прогрева.
        # ------------------------------------------------------------------
        if self.phase == Phase.COOLDOWN:
            # Если двигатель сам остановился раньше окончания cooldown, это уже
            # не авария АВР: дом надёжно сидит на Grid, поэтому сессию завершаем.
            if not self._active_running(s):
                return self._finish_session(now, f"Генератор {self.active_generator} остановлен после возврата на сеть.")

            # Если Grid снова исчез во время cooldown, а АВР разрешён, используем
            # уже работающий и горячий генератор. После обычных 5 секунд debounce
            # сразу возвращаемся к силовому transfer: новый START/choke/preheat
            # здесь не нужен.
            if s.grid_ready is False and s.ats_enabled:
                if self.grid_failed_since is None:
                    self.grid_failed_since = now
                elif now - self.grid_failed_since >= self.cfg.grid_failure_delay:
                    self.session_mode = "automatic"
                    actions = [Action("set_session_mode", value="automatic")]
                    actions.extend(self._begin_transfer_to_generator(now, s))
                    return actions
            else:
                self.grid_failed_since = None

            if self.deadline is not None and now >= self.deadline:
                self.phase = Phase.STOPPING
                self.phase_started = now
                self.deadline = now + self.cfg.generator_stop_timeout
                return [
                    Action("switch_off", target=self._remote_entity()),
                    Action("log", message=f"Завершено охлаждение генератора {self.active_generator}; снимаем REMOTE START.",
                           entity_id=self._remote_entity()),
                ]
            return actions

        # ------------------------------------------------------------------
        # STOPPING — REMOTE START снят, ждём физическое RUNNING=OFF.
        # Если двигатель не остановился за generator_stop_timeout -> TERMINAL
        # и аппаратный Generators Emergency Stop.
        # ------------------------------------------------------------------
        if self.phase == Phase.STOPPING:
            # Grid может исчезнуть даже когда REMOTE уже снят, но двигатель ещё
            # физически не успел остановиться. Пока RUNNING=ON, отменяем остановку,
            # снова подаём REMOTE и используем этот же генератор как резерв.
            if s.grid_ready is False and s.ats_enabled and self._active_running(s):
                if self.grid_failed_since is None:
                    self.grid_failed_since = now
                elif now - self.grid_failed_since >= self.cfg.grid_failure_delay:
                    self.session_mode = "automatic"
                    self.phase = Phase.TRANSFER_DISCONNECT_GRID
                    self.phase_started = now
                    self.deadline = now + self.cfg.transfer_confirmation_timeout
                    return [
                        Action("switch_on", target=self._remote_entity()),
                        Action("set_session_mode", value="automatic"),
                        Action("switch_off", target="switch.grid_power"),
                        Action("log", message="Сеть снова пропала во время остановки генератора; остановка отменена, возвращаем резерв.",
                               entity_id="binary_sensor.grid_input_ready"),
                    ]
            else:
                self.grid_failed_since = None

            if not self._active_running(s):
                return self._finish_session(now, f"Генератор {self.active_generator} штатно остановлен.")
            if self.deadline is not None and now >= self.deadline:
                return self._terminal(now, f"Генератор {self.active_generator} не остановился за {int(self.cfg.generator_stop_timeout)} с после снятия REMOTE START.",
                                      self._running_entity())
            return actions

        return actions

    # ==================================================================
    # ЗАПУСК / ВОССТАНОВЛЕНИЕ ПОСЛЕ RESTART
    # ==================================================================
    # Здесь принципиально НЕ восстанавливается сохранённая Phase. Сначала
    # определяем реальную картину по датчикам: чем питается дом, какой генератор
    # работает, активна ли наша резервная сессия и установлен ли Emergency Stop.
    # ==================================================================
    def _bootstrap(self, now: float, s: Snapshot) -> List[Action]:
        self.phase_started = now
        self.grid_ready_since = now if s.grid_ready is True else None
        self.failed_generators.clear()

        if not self._critical_states_known(s):
            self.phase = Phase.WAITING
            return []

        if s.emergency_stop is True:
            self.phase = Phase.TERMINAL
            self.session_mode = "none"
            return [
                Action("set_session", value="off"),
                Action("set_session_mode", value="none"),
                Action("log", message="ATS запущен в заблокированном состоянии: активен Generators Emergency Stop.",
                       entity_id="switch.generators_emergency_stop"),
            ]

        running = self._running_generators(s)
        if len(running) > 1:
            if s.session_active:
                return self._terminal(now, "После запуска ATS одновременно обнаружены два работающих генератора.",
                                      "binary_sensor.generator_a_is_running")
            self.phase = Phase.EXTERNAL
            return [Action("notify_critical", message="ATS обнаружил два одновременно работающих генератора вне своей сессии; автоматическое управление не выполняется."),
                    Action("log", message="Два генератора работают одновременно вне сессии ATS. Требуется проверка.",
                           entity_id="binary_sensor.generator_a_is_running")]

        if s.session_active:
            self.session_mode = s.session_mode if s.session_mode in ("automatic", "manual") else "automatic"
            self.active_generator = running[0] if running else None

            if s.house_generator is True and self.active_generator:
                self.phase = Phase.ON_GENERATOR
                return [Action("log", message=f"Восстановление после restart: дом уже питается от генератора {self.active_generator}. Продолжаем с фактического состояния.",
                               entity_id="binary_sensor.house_powered_by_generator")]

            if s.house_grid is True and self.active_generator:
                self.phase = Phase.COOLDOWN
                self.deadline = now + self.cfg.generator_stop_delay
                return [Action("log", message=f"Восстановление после restart: дом на Grid, генератор {self.active_generator} работает. Запускаем полный cooldown заново.",
                               entity_id="binary_sensor.house_powered_by_grid")]

            if self.active_generator:
                # Старую фазу до restart специально не угадываем. Генератор уже
                # работает, но неизвестно, сколько реально успел прогреться.
                # Поэтому безопасно даём ему полный прогрев заново и затем
                # продолжаем по физической обратной связи.
                return self._begin_preheat(now, s, recovery=True)

            if s.house_grid is True and s.grid_ready is True:
                self.phase = Phase.GRID
                self.session_mode = "none"
                return [
                    Action("set_session", value="off"),
                    Action("set_session_mode", value="none"),
                    Action("log", message="Восстановление после restart: резервная сессия уже завершена; очищены служебные флаги.",
                           entity_id="binary_sensor.house_powered_by_grid"),
                ]

            # Сессия принадлежит ATS, но ни один генератор сейчас не работает.
            # Значит после restart процедуру резервирования начинаем заново с A.
            return self._begin_start(now, s, "A")

        # Служебного признака нашей сессии нет. Если при этом генератор работает,
        # считаем его внешним/ручным запуском и принципиально не захватываем управление.
        if running or s.house_generator is True:
            self.phase = Phase.EXTERNAL
            self.active_generator = running[0] if running else None
            return [Action("log", message="Обнаружена работа генератора вне сессии ATS. Автоматика не вмешивается.",
                           entity_id="binary_sensor.generator_a_is_running" if self.active_generator == "A" else "binary_sensor.generator_b_is_running")]

        self.phase = Phase.GRID
        self.active_generator = None
        self.session_mode = "none"
        return []

    # ==================================================================
    # КОМАНДЫ И АТОМАРНЫЕ ПЕРЕХОДЫ
    # ==================================================================
    # Эти методы не выполняют I/O. Они только меняют внутреннюю фазу и формируют
    # Action. Реальные switch/button/service вызывает адаптер main.py / ha_client.py.
    # ==================================================================
    def _handle_manual_start(self, now: float, s: Snapshot) -> List[Action]:
        """Ручная кнопка «Включить резерв». Разрешатель АВР здесь не требуется."""
        if s.emergency_stop is True:
            return [Action("notify_critical", message="Ручной ввод резерва отклонён: активен Generators Emergency Stop."),
                    Action("log", message="Ручной ввод резерва заблокирован аварийным стопом.",
                           entity_id="switch.generators_emergency_stop")]
        if self.phase not in (Phase.GRID, Phase.EXTERNAL):
            return [Action("log", message=f"Ручной ввод резерва проигнорирован: ATS уже находится в фазе {self.phase.value}.",
                           entity_id="input_button.generator_reserve_start")]
        if self.phase == Phase.EXTERNAL:
            return [Action("notify_warning", message="Ручной ввод резерва отклонён: обнаружен генератор, работающий вне сессии ATS."),
                    Action("log", message="Нельзя начать управляемый резерв при внешнем/ручном запуске генератора.",
                           entity_id="input_button.generator_reserve_start")]
        return self._begin_session_and_start(now, s, "manual", "A")

    def _handle_manual_return(self, now: float, s: Snapshot) -> List[Action]:
        """Ручная команда возврата на Grid для manual-сессии."""
        if self.phase not in (Phase.ON_GENERATOR, Phase.GRID_RESTORE_WAIT):
            return [Action("log", message="Команда возврата на Grid проигнорирована: дом не находится в управляемом режиме питания от генератора.",
                           entity_id="input_button.generator_return_to_grid")]
        if s.grid_ready is not True:
            return [Action("notify_warning", message="Возврат на основную сеть невозможен: Grid Input Ready = OFF."),
                    Action("log", message="Ручной возврат на Grid отклонён: сеть не готова.",
                           entity_id="binary_sensor.grid_input_ready")]
        self.phase = Phase.GRID_RESTORE_WAIT
        self.phase_started = now
        self.grid_ready_since = now
        # Сохраняем режим manual на время явного возврата. Это не даёт обычной
        # автоматической логике принять параллельное решение до завершения команды.
        return [Action("log", message="Запрошен ручной возврат на Grid. Начата выдержка стабильности сети.",
                       entity_id="input_button.generator_return_to_grid")]

    def _begin_session_and_start(self, now: float, s: Snapshot, mode: str, generator: str) -> List[Action]:
        """Создать принадлежащую ATS сессию и начать запуск выбранного генератора."""
        self.session_mode = mode
        self.failed_generators.clear()
        actions = [
            Action("set_session", value="on"),
            Action("set_session_mode", value=mode),
            Action("log", message=f"Начата {'автоматическая' if mode == 'automatic' else 'ручная'} сессия резервного питания.",
                   entity_id="input_boolean.generator_reserve_session_active"),
        ]
        actions.extend(self._begin_start(now, s, generator))
        return actions

    def _begin_start(self, now: float, s: Snapshot, generator: str) -> List[Action]:
        """Подготовить заслонку, подать REMOTE START и запустить timeout запуска."""
        self.active_generator = generator
        self.phase = Phase.STARTING
        self.phase_started = now
        self.deadline = now + self.cfg.generator_start_timeout
        self.generator_power_lost_since = None
        choke_mode = self._choke_mode(generator)
        self.choke_used = self._needs_choke(generator, s.garage_temperature)

        other = "B" if generator == "A" else "A"
        actions: List[Action] = [Action("switch_off", target=self._remote_entity(other))]

        # ВАЖНО: названия сущностей заслонки в текущем ESPHome механически
        # перепутаны относительно реального движения:
        #   *_choke_open  -> ФИЗИЧЕСКИ ЗАКРЫТЬ заслонку (cold start)
        #   *_choke_close -> ФИЗИЧЕСКИ ОТКРЫТЬ заслонку (RUN/hot start)
        # Код сознательно следует реальной механике, а не названию entity_id.
        if self.choke_used:
            actions.append(Action("button", target=self._choke_cold_button(generator)))
        else:
            actions.append(Action("button", target=self._choke_run_button(generator)))

        actions.extend([
            Action("switch_on", target=self._remote_entity(generator)),
            Action("log", message=(
                f"Запуск генератора {generator}: "
                + ("холодный старт с закрытой заслонкой" if self.choke_used else "старт с открытой заслонкой")
                + f"; choke_mode={choke_mode}"
                + f"; timeout {int(self.cfg.generator_start_timeout)} с."
            ), entity_id=self._remote_entity(generator)),
        ])
        return actions

    def _begin_preheat(self, now: float, s: Snapshot, recovery: bool = False) -> List[Action]:
        """Рассчитать температурный прогрев и перейти в PREHEATING."""
        seconds = self._preheat_seconds(s.garage_temperature)
        self.phase = Phase.PREHEATING
        self.phase_started = now
        self.deadline = now + seconds
        prefix = "Восстановление после restart: " if recovery else ""
        return [Action("log", message=f"{prefix}генератор {self.active_generator} работает; прогрев {int(seconds)} с перед нагрузкой.",
                       entity_id=self._running_entity())]

    def _begin_transfer_to_generator(self, now: float, s: Snapshot) -> List[Action]:
        """Начать двухступенчатый перевод дома с Grid на генератор."""
        if not self._active_running(s):
            return self._generator_failed(now, s, f"Нельзя начать ввод резерва: генератор {self.active_generator} не работает.")
        self.phase = Phase.TRANSFER_DISCONNECT_GRID
        self.phase_started = now
        self.deadline = now + self.cfg.transfer_confirmation_timeout
        return [
            Action("switch_off", target="switch.grid_power"),
            Action("log", message="Начат ввод резерва: сначала отключаем Grid перед силовым переключателем.",
                   entity_id="switch.grid_power"),
        ]

    def _begin_return_to_grid(self, now: float, s: Snapshot) -> List[Action]:
        """Начать подтверждаемый возврат силовой схемы с генератора на Grid."""
        self.phase = Phase.RETURN_SELECT_GRID
        self.phase_started = now
        self.deadline = now + self.cfg.transfer_confirmation_timeout
        return [
            Action("switch_off", target="switch.use_generator_as_power_source"),
            Action("log", message="Сеть стабильна. Силовой переключатель переводится в положение GRID.",
                   entity_id="switch.use_generator_as_power_source"),
        ]

    def _abort_pretransfer_for_restored_grid(self, now: float, s: Snapshot) -> List[Action]:
        # Если до появления стабильной сети силовая схема уже успела уйти из
        # нормального положения Grid, выполняем полноценный подтверждаемый возврат.
        # Если силовая часть ещё не переключалась, дом и так на Grid: остаётся
        # только выдержать единый cooldown и штатно остановить генератор.
        if s.source_generator is True or s.grid_disconnected is True or s.house_generator is True:
            return self._begin_return_to_grid(now, s)

        self.phase = Phase.COOLDOWN
        self.phase_started = now
        self.deadline = now + self.cfg.generator_stop_delay
        return [
            Action("notify_warning", message="Основная сеть восстановилась и стабилизировалась; ввод генератора не потребовался."),
            Action("log", message=f"Grid стабилен. Генератор {self.active_generator} остаётся без нагрузки на {int(self.cfg.generator_stop_delay)} с перед остановкой.",
                   entity_id="binary_sensor.grid_input_ready"),
        ]

    def _generator_failed(self, now: float, s: Snapshot, reason: str) -> List[Action]:
        """Зафиксировать отказ активного генератора и один раз попробовать второй."""
        failed = self.active_generator
        if failed:
            self.failed_generators.add(failed)

        actions: List[Action] = [
            Action("switch_off", target=self._remote_entity(failed)),
            Action("button", target=self._choke_run_button(failed)),
            Action("notify_critical", message=reason),
            Action("log", message=reason, entity_id=self._running_entity(failed)),
        ]

        backup = "B" if failed == "A" else "A"
        if backup in self.failed_generators:
            actions.extend(self._terminal(now, f"Резервный генератор {backup} уже отмечен как отказавший; дальнейшие автоматические попытки прекращены.",
                                          self._running_entity(backup)))
            return actions

        # Отказ A при первоначальном старте или остановка активного генератора
        # под нагрузкой -> пробуем второй генератор. Каждый агрегат в одной
        # резервной сессии пробуется максимум один раз. Уже отказавший агрегат
        # повторно не трогаем: это предотвращает бесконечный A<->B ping-pong.
        actions.extend(self._begin_start(now, s, backup))
        return actions

    def _terminal(self, now: float, reason: str, entity_id: str) -> List[Action]:
        """
        Неустранимая/непонятная авария.

        Здесь больше не пытаемся диагностировать причину или «дощёлкать» схему.
        Ordinary controls best-effort возвращаются в штатную топологию Grid,
        затем включается общий аппаратный Emergency Stop. После этого ATS остаётся
        в TERMINAL до вмешательства человека.
        """
        self.phase = Phase.TERMINAL
        self.phase_started = now
        self.deadline = None
        self.session_mode = "none"
        self.active_generator = None
        return [
            # Сначала best-effort возвращаем обычные органы управления в
            # максимально штатное положение: REMOTE сняты, источник = GRID,
            # внешний Grid разрешён. Подтверждать успех уже не пытаемся — мы
            # находимся в терминальной неопределённой аварии.
            Action("switch_off", target="switch.generator_a_remote_start"),
            Action("switch_off", target="switch.generator_b_remote_start"),
            Action("switch_off", target="switch.use_generator_as_power_source"),
            Action("switch_on", target="switch.grid_power"),
            # Последний аппаратный эшелон — общий Emergency Stop.
            # Если генератор работает: он будет остановлен, DKG116 уйдёт в EMERG.
            # Если генератор уже стоит: дальнейший REMOTE START будет аппаратно
            # заблокирован. ATS после этого больше НИЧЕГО не предпринимает.
            Action("switch_on", target="switch.generators_emergency_stop"),
            Action("set_session", value="off"),
            Action("set_session_mode", value="none"),
            Action("notify_critical", message=f"Терминальная ошибка АВР: {reason} Генераторы переведены в Emergency Stop; силовая схема возвращена в положение GRID."),
            Action("log", message=f"TERMINAL ATS: {reason}", entity_id=entity_id),
        ]

    def _finish_session(self, now: float, message: str) -> List[Action]:
        """Штатно завершить резервную сессию и очистить служебные признаки."""
        entity = self._running_entity() if self.active_generator else "binary_sensor.grid_input_ready"
        self.phase = Phase.GRID
        self.phase_started = now
        self.deadline = None
        self.active_generator = None
        self.session_mode = "none"
        self.failed_generators.clear()
        self.grid_ready_since = now
        return [
            Action("set_session", value="off"),
            Action("set_session_mode", value="none"),
            Action("log", message=message, entity_id=entity),
        ]

    # ==================================================================
    # ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
    # ==================================================================
    def _critical_states_known(self, s: Snapshot) -> bool:
        return all(v is not None for v in (
            s.grid_ready,
            s.house_grid,
            s.house_generator,
            s.generator_a_running,
            s.generator_b_running,
            s.emergency_stop,
        ))

    def _update_grid_stability(self, now: float, s: Snapshot) -> None:
        if s.grid_ready is True:
            if self.grid_ready_since is None:
                self.grid_ready_since = now
        else:
            self.grid_ready_since = None

    def _automatic_grid_return_is_stable(self, now: float, s: Snapshot) -> bool:
        return (
            self.session_mode == "automatic"
            and s.grid_ready is True
            and self.grid_ready_since is not None
            and now - self.grid_ready_since >= self.cfg.grid_restore_stable_time
        )

    def _choke_mode(self, generator: Optional[str] = None) -> str:
        """Вернуть нормализованную политику заслонки выбранного генератора."""
        g = generator or self.active_generator
        mode = (
            self.cfg.generator_a_choke_mode
            if g == "A"
            else self.cfg.generator_b_choke_mode
        )
        mode = str(mode).lower()
        # Неизвестное значение трактуем консервативно: заслонка закрывается.
        return mode if mode in {"always", "temperature", "never"} else "always"

    def _needs_choke(self, generator: str, temp: Optional[float]) -> bool:
        mode = self._choke_mode(generator)
        if mode == "always":
            return True
        if mode == "never":
            return False
        # temperature: нет температуры -> считаем двигатель холодным.
        return temp is None or temp < self.cfg.choke_temperature

    def _preheat_seconds(self, temp: Optional[float]) -> float:
        # Нет температуры -> самый длинный, консервативный прогрев.
        if temp is None:
            return self.cfg.preheat_very_cold_seconds
        if temp >= self.cfg.preheat_warm_temp:
            return self.cfg.preheat_warm_seconds
        if temp > self.cfg.preheat_cool_temp:
            return self.cfg.preheat_cool_seconds
        if temp > self.cfg.preheat_cold_temp:
            return self.cfg.preheat_cold_seconds
        return self.cfg.preheat_very_cold_seconds

    def _running_generators(self, s: Snapshot) -> List[str]:
        out: List[str] = []
        if s.generator_a_running is True:
            out.append("A")
        if s.generator_b_running is True:
            out.append("B")
        return out

    def _any_generator_running(self, s: Snapshot) -> bool:
        return s.generator_a_running is True or s.generator_b_running is True

    def _active_running(self, s: Snapshot) -> bool:
        if self.active_generator == "A":
            return s.generator_a_running is True
        if self.active_generator == "B":
            return s.generator_b_running is True
        return False

    def _remote_entity(self, generator: Optional[str] = None) -> str:
        g = generator or self.active_generator
        return "switch.generator_a_remote_start" if g == "A" else "switch.generator_b_remote_start"

    def _running_entity(self, generator: Optional[str] = None) -> str:
        g = generator or self.active_generator
        return "binary_sensor.generator_a_is_running" if g == "A" else "binary_sensor.generator_b_is_running"

    def _choke_cold_button(self, generator: Optional[str] = None) -> str:
        g = generator or self.active_generator
        # Текущие ESPHome-имена механически обратны реальному движению заслонки.
        return "button.generator_a_choke_open" if g == "A" else "button.generator_b_choke_open"

    def _choke_run_button(self, generator: Optional[str] = None) -> str:
        g = generator or self.active_generator
        # Текущие ESPHome-имена механически обратны реальному движению заслонки.
        return "button.generator_a_choke_close" if g == "A" else "button.generator_b_choke_close"

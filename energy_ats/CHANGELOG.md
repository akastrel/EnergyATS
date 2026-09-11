# Changelog

## 1.0.2

Исправления пяти воспроизведённых дефектов F1–F5, найденных независимым ревью EnergyATS 1.0.1, без нового архитектурного слоя.

- **F1:** manual stop во время продолжающегося outage сохраняет EnergyATS-owned обязанность вернуть Grid path после устойчивого восстановления сети; ownership переживает завершение generator session и restart App, но не распространяется на произвольный пользовательский `grid_power=OFF`.
- **F2:** stop fault/timeout managed generator после уже подтверждённого возврата дома на Grid переводит Supervisor в `RECOVERY_REQUIRED`; fallback SECONDARY для ошибки остановки после возврата Grid не запускается.
- **F3:** TPC при исчезновении generator voltage/feedback и всё ещё подтверждённом `generator_selected=ON` разрешает безопасный break `DESELECT_GENERATOR` с физическим подтверждением. Отсутствие напряжения не считается доказательством размыкания и не съедает допустимый start interval SECONDARY старым transfer timeout.
- **F4:** ordinary Scheduled Exercise повторно проверяет presence непосредственно до физического REMOTE ON; `home` и `unknown/unavailable` отменяют ещё не начатую попытку как `DEFERRED`. Forced Exercise сохраняет отдельные правила grace/warning.
- **F5:** Load Manager контролирует freshness потока generator-power samples и в `STABLE`; stale gap переводит только Load Manager в `DEGRADED`, разрывает overload/admission continuity и требует нового stabilization после восстановления samples.
- Добавлены отдельные regression tests F1–F5 с человеко-читаемым описанием проверяемого поведения. До production fixes они воспроизводили все находки: `6 failed, 276 passed`; после исправлений полный suite проходит.
- `REQUIREMENTS_RU.md` дополнен точной семантикой ownership Grid isolation, stop fault, generator-selector feedback, presence recheck и sample freshness; добавлены `TC-CORE-24`, `TC-CORE-25`, `TC-LOAD-25`.
- `USER_TESTS_RU.md` расширен: C4 теперь проверяет окончательный возврат Grid, включая restart между stop и restore; C7 оставляет реальной схеме подтверждение feedback/contactors для F3.
- App и add-on version подняты до `1.0.2`; persistent `schema_version` остаётся `3`, так как новое поле Supervisor обратно совместимо внутри существующего payload.

---

## 1.0.1

Терминологический cleanup UPS Run без изменения поведения EnergyATS.

- `outage_power_policy.py` и семейство `OutagePower*` полностью переименованы в `ups_run.py` и `UPSRun*`.
- `main.py`, unit/integration tests и internal state используют единое имя `UPS Run` / `ups_run`; старые runtime/test identifiers удалены.
- Persistent state key переименован в `ups_run`; `schema_version` поднят до `3`. Миграции schema 2 намеренно нет.
- `ARCHITECTURE_RU.md` синхронизирован с фактическим именем модуля.
- Версия App и add-on поднята до `1.0.1`.

---

## 1.0.0

Архитектурная стабилизация EnergyATS после Exercise, Load Manager и UPS Run / Charge Cycling. Версия 1.0.0 фиксирует единую модель верхнеуровневого поведения перед физическим commissioning.

- `EnergySupervisor` теперь является единственным центром системных решений: Manual, Grid outage, fallback, возврат Grid, Recovery и пересечения с Exercise/UPS Run разрешаются в одном месте. Дополнительный `PolicyCoordinator` не используется.
- `main.py` оставлен composition/I/O/dispatch слоем: Scheduler и UPS Run передают локальные факты/условия, Supervisor принимает решение, GC/TPC безопасно исполняют его, Load Manager управляет только G1/G2.
- `REQUIREMENTS_RU.md` переработан в цельную спецификацию с `REQ-BEH-*`, стабильными `TC-*` identifiers и traceability `requirement -> Supervisor branch -> automated/physical test`.
- Уже RUNNING исправный Exercise-generator при подтверждённом outage может быть принят той же outage-session без `REMOTE OFF -> cold start`; при manual reserve request тот же run может быть передан manual managed-session.
- Exercise -> Manual handoff фиксируется как нейтральный `INTERRUPTED_BY_MANUAL`, а не как технический failure; до явного handoff Scheduler сохраняет обязанность безопасной остановки своего auto-run.
- Manual stop при продолжающемся outage теперь явно переводит дом `Generator -> UPS_ONLY`, после подтверждённого снятия нагрузки штатно останавливает managed generator и подавляет automatic restart до восстановления Grid либо нового manual start.
- Stable Grid имеет приоритет над завершением automatic charge cycle; manual override снимает cycle ownership. Исправлена same-tick гонка, при которой уже отменённый пользователем cycle мог ошибочно создать новый post-cycle UPS wait.
- Recovery имеет абсолютный приоритет, но не оставляет автоматически запущенный Exercise без ответственного за будущий stop.
- `ARCHITECTURE_RU.md` синхронизирован с моделью: Exercise Scheduler, UPS Run и Load Manager являются специализированными подсистемами, а не конкурирующими верхнеуровневыми policy.
- Удалён устаревший отдельный `GENERATOR_EXERCISE_REQUIREMENTS_RU.md`; согласованные требования находятся в общей спецификации.
- Добавлены Supervisor/integration regression tests для конфликтов Exercise / Manual / Outage / UPS Run / Recovery и manual-stop/cycle boundary case.
- Обновлён набор физических испытаний для проверки новых handoff и `Generator -> UPS_ONLY` сценариев перед вводом 1.0.0 в эксплуатацию.

---

## 0.7.0

Добавлены Delayed Generator Start и Long Outage Charge Cycling по разделу 25 требований.

- Независимые разрешатели выключены по умолчанию; добавлены Start/Target SoC, минимальный TTG и максимальная задержка с переводами HA configuration на русский и английский.
- Ожидание на UPS заканчивается по любому порогу или при недостоверной/критической батарее. SoC и TTG проверяются независимо; TTG не используется при зарядке/поддержании. Возраст HA cache учитывается и после restart.
- Только cycle-owned автоматическая outage-сессия завершается по Target SoC: подтверждённое снятие нагрузки через TPC, штатный GC cooldown/stop, новый интервал UPS. Ручные и внешние сессии не захватываются.
- Ручной override сохраняется после restart; запрос во время остановки возобновляет штатный запуск без ложного fallback. Отказ остановки приводит к Recovery.
- Устойчивый возврат Grid имеет приоритет, включая ожидание между циклами. Краткое появление Grid не обнуляет максимальное время ожидания.
- Сохраняются время ожидания, cycle ownership и ручной override; top-level journal schema остаётся совместимой. Незавершённые аппаратные переходы требуют обычного Recovery.
- Расширен status sensor; добавлены сквозные сценарии 78–94 с краткими русскими описаниями и проверки гонок, отказов telemetry/feedback, Load Manager и safety blocks.
- Применены замечания к требованиям, исправлен устаревший список фаз Load Manager, обновлены руководство и физические проверки.

---

## 0.6.0

Добавлен отдельный Load Manager для управления некритичными нагрузками при питании дома от общей генераторной шины.

- Добавлены группы `G1 = switch.non_critical_loads_first_floor` и `G2 = switch.non_critical_loads_basement_floor`; восстановление выполняется `G1 -> G2`, overload `LOAD_SHEDDING` — `G2 -> G1`.
- Перед managed transfer Load Manager после прогрева генератора поочерёдно отключает доступные некритичные группы и только затем разрешает TPC подключить дом к generator bus.
- После transfer нагрузки возвращаются по одной с отдельным stabilization window и проверкой запаса относительно Nominal Power.
- Load Manager непрерывно контролирует generator power во всё время питания дома от generator bus; sustained nominal overload и подтверждённое превышение Maximum Power вызывают поэтапный `LOAD_SHEDDING`.
- Generator meter, G1/G2 и паспортные power metadata являются soft dependencies: их отказ переводит только Load Manager в `DEGRADED` и не создаёт `RECOVERY_REQUIRED` основной ATS-логики.
- Добавлены per-generator numeric metadata `Nominal Power` / `Maximum Power`; фактические limits выбираются по текущему `GeneratorBusOwner`.
- Добавлен hysteresis/retry для повторного admission, persistence собственного `shed_by_energy_ats` ownership и восстановление только собственных отключений после подтверждённого возврата Grid.
- `sensor.energy_ats_status` дополнен фазой Load Manager, measured power, active limits, состоянием/ownership G1/G2, overload timers и retry state.
- Добавлены конфигурационные параметры Load Manager; функция по умолчанию выключена (`load_management_enabled = false`).
- Добавлены unit и app-level integration tests Load Manager, включая pre-transfer shedding, soft-dependency failures, continuous overload и Grid restore.
- Версия App и add-on поднята до `0.6.0`.

---

## 0.5.1

Исправления по результатам первого физического прогона `USER_TESTS_RU.md`.

- Default `grid_failure_delay` изменён с 5 до 60 секунд и синхронизирован между add-on manifest и runtime defaults.
- После отказа managed PRIMARY и аппаратного takeover внешним SECONDARY EnergyATS больше не пытается повторно запускать этот SECONDARY, если пользователь затем остановил его вручную; система переходит в `RECOVERY_REQUIRED`.
- Отсутствующий optional `input_boolean.generator_test_mode` трактуется как `OFF`; внешний запуск при отсутствующей Grid поэтому корректно классифицируется как `OUTAGE_RELATED` и участвует в cleanup после возврата Grid.
- Добавлены E2E regression tests для пользовательских сценариев M9 и M10.
- Версия App и add-on поднята до `0.5.1`.

---

## 0.5.0

Плановый автоматический пробный запуск генераторов после длительного простоя без изменения физической силовой топологии.

### Exercise Scheduler

- Добавлен отдельный `ExerciseScheduler`: maintenance-policy не смешивается с TPC и не дублирует Generator Controller.
- Generator A и B имеют независимые interval/start-time/run-duration/presence-grace настройки.
- Defaults после включения:
  - A: 30 дней, 15:00, 10 минут, grace 7 дней;
  - B: 45 дней, 15:00, 10 минут, grace 14 дней.
- Scheduled exercise по умолчанию выключен для обоих генераторов.
- Новый slot получает initial reference при первом наблюдении; первый exercise не запускается сразу после обновления.
- Любой достоверный непрерывный run достаточной длительности может стать qualifying run и перенести следующий due.
- Пропущенное дневное окно не создаёт произвольный catch-up запуск.

### Presence / forced exercise

- Presence читается из конфигурируемого `family_presence_entity` (default `group.family`).
- До forced-date обычный exercise запускается только при подтверждённом отсутствии семьи; `unknown/unavailable` приводит к `DEFERRED`.
- Presence является мягким Scheduler-input и не блокирует запуск/работу основной ATS-логики.
- После configured grace presence перестаёт блокировать test, но все остальные safety-preconditions сохраняются.
- Forced exercise требует реально доставленного предупреждения не менее чем за 60 минут; пропущенное warning-window не создаёт неожиданного forced catch-up.

### Lifecycle / ownership

- Exercise запускает и останавливает двигатель только через существующий GC; DKG116 по-прежнему отвечает за crank retries.
- Дом при штатном exercise остаётся на Grid; TPC не переключает нагрузку на generator bus.
- Maintenance-test не имеет fallback на второй генератор.
- Scheduler-owned RUNNING классифицируется существующим run-context `TEST_RUN` без зависимости от внешнего `input_boolean.generator_test_mode`.
- Результаты: `SUCCESS`, `FAILED`, `DEFERRED`, `INTERRUPTED_BY_OUTAGE`.
- Автоматически запущенный двигатель сохраняет явного policy-owner до подтверждённой остановки либо явного handoff.
- При реальной потере Grid уже работающий подходящий exercise-generator может быть принят обычной outage-сессией без `REMOTE OFF -> cold start PRIMARY`.
- Если handoff не состоялся, Scheduler сохраняет обязанность штатно остановить собственный generator.
- Restart сохраняет active attempt, исходный run timer и stop ownership; повторный REMOTE START не выдаётся.
- GC после restart scheduler-owned unloaded generator консервативно восстанавливает рабочее положение choke, не угадывая потерянную transient phase.

### Persistence / UI / notifications

- Top-level journal остаётся schema 2 и дополняется `exercise_scheduler`; старый 0.4 journal без scheduler-state безопасно получает новый Scheduler state.
- В journal сохраняются scheduler references, due/grace/warning state, active attempt и ограниченная history результатов.
- `sensor.energy_ats_status` дополнен per-generator exercise attributes и active exercise timer.
- FAILED создаёт critical event/notification с фактическим именем generator; SUCCESS достаточно журналируется.
- Время расписания интерпретируется в timezone Home Assistant.

### Tests

- Добавлены чистые unit/scenario tests Scheduler-а, Supervisor handoff, TEST_RUN/bus classification, presence/notification/timezone и end-to-end execution через fake Home Assistant.
- Проверены due/grace/forced warning, missed windows, safety blockers, independent A/B schedules, no-fallback, qualifying history, restart ownership, outage handoff и штатная остановка без переключения дома с Grid.

---

## 0.4.0

Полная переработка EnergyATS вокруг фактической физической топологии.

### Физическая модель

- Удалён виртуальный силовой `Battery path`; автономная работа MAP представляется как `UPS_ONLY`.
- Общая генераторная шина отделена от состояния двигателей A/B.
- Одновременный RUNNING A и B является штатным режимом.
- Добавлен `GeneratorBusTracker`, восстанавливающий аппаратный FIFO-owner по истории RUNNING.
- При остановке текущего owner и продолжающем RUNNING второго генератора owner автоматически переходит ко второму без команды EnergyATS.
- При недостаточной истории owner остаётся `UNKNOWN`; угадывание запрещено.

### Контекст запусков

Для каждого непрерывного RUNNING используется минимальная модель:

- `NONE`;
- `OUTAGE_RELATED`;
- `TEST_RUN`;
- `OTHER`;
- `UNKNOWN`.

После стабильного восстановления Grid EnergyATS сначала возвращает дом на Grid и только затем разрешает остановку всех известных `OUTAGE_RELATED` генераторов. `TEST_RUN`, `OTHER` и `UNKNOWN` этим правилом не останавливаются.

### Управление

- A/B остаются стабильными внутренними аппаратными слотами; UI/log используют фактические имена и модели из HA.
- Реализован единственный managed fallback `PRIMARY -> SECONDARY` без ping-pong.
- Уже работающий SECONDARY не захватывается в managed ownership при отказе PRIMARY.
- Power Transfer Controller управляет только основной парой Grid / общей Generator bus и не знает о A/B owner.
- Силовые переходы выполняются break-before-make с подтверждением каждого шага.
- Generator Controller отвечает только за жизненный цикл одного двигателя; локальная ошибка фиксируется `FAULT`, а системный recovery остаётся ответственностью Supervisor.
- Удалены исторические compatibility aliases и производные Supervisor phases, дублировавшие состояние GC/TPC/GeneratorBusTracker.

### Safety / recovery

- Неизвестное обязательное физическое состояние блокирует активные команды.
- E-stop имеет высший приоритет.
- Внешний генератор не становится managed автоматически.
- Recovery возвращает силовую схему к Grid path и не захватывает внешний генератор.
- Локальный GC fault снимается только у физически остановленного двигателя (`RUNNING=OFF`, `REMOTE=OFF`).

### Runtime / persistence / UI

- App version: `0.4.0`.
- Persistent journal использует top-level `schema_version = 2`.
- Миграция ошибочной внутренней модели 0.3.x намеренно не выполняется.
- `sensor.energy_ats_status` показывает фактический source, phase, bus owner, managed generator, run-context, PRIMARY, fallback и оставшееся время.
- Пользовательские состояния используют `UPS_ONLY` / `NO_POWER`, а не старые Battery-path термины.

### Tests / documentation

- `PHYSICAL_POWER_TOPOLOGY_RU.md` выделен как source of truth по физической электроустановке.
- `REQUIREMENTS_RU.md` полностью отделён от физического описания и задаёт policy EnergyATS.
- Unit и end-to-end suites переписаны под модель 0.4; старые тесты ошибочной модели не поддерживаются ради обратной совместимости.
- Документация repository, App и HA-контракт синхронизированы с 0.4.

---

## 0.3.x — историческая ветка

0.3.x была последовательностью ранних итераций EnergyATS, на которых появились App, GC/TPC/Supervisor, HA-команды, status sensor, persistence, Logbook и первые сценарные тесты.

Часть её внутренних абстракций (`Battery path`, запрет dual RUNNING, прежние transient phases и compatibility-механизмы) впоследствии оказалась несоответствующей реальной электрической схеме. Поэтому 0.4 является не совместимым продолжением этой внутренней модели, а её намеренной заменой.

Подробная история отдельных 0.3.x изменений остаётся доступна в Git history и релизных коммитах repository; runtime 0.4 не содержит compatibility-кода для этих моделей.

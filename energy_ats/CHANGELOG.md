# Changelog

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

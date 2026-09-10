# Energy ATS 0.6.0 — архитектура

## 1. Источники истины

Архитектура реализует два документа более высокого уровня:

1. `PHYSICAL_POWER_TOPOLOGY_RU.md` — что физически существует и как ведёт себя железо;
2. `REQUIREMENTS_RU.md` — как при этой физической схеме должен вести себя EnergyATS.

Этот документ описывает только способ реализации требований. Он не вводит новые физические устройства и не расширяет разрешённые сценарии.

Scheduled Exercise и Load Manager являются policy-функциями и не меняют физическую силовую топологию.

## 2. Разделение ответственности

EnergyATS — один Home Assistant App и один Python-процесс, но управляющая логика разделена на небольшие независимые части.

| Модуль | Ответственность |
|---|---|
| `domain.py` | Общие термины: A/B, `PowerSource`, `PowerPath`, причина managed-сессии |
| `generator_bus.py` | FIFO-owner общей генераторной шины и контекст непрерывных RUNNING |
| `generator_controller.py` | Жизненный цикл одного двигателя: choke, REMOTE, запуск, прогрев, cooldown, stop |
| `power_transfer.py` | Основные контакторы Grid / Generator и break-before-make |
| `energy_supervisor.py` | Основная ATS-policy: outage, managed-сессия, fallback, возврат Grid, recovery |
| `exercise_scheduler.py` | Maintenance-policy: due/grace/warning, qualifying history и ownership scheduled exercise |
| `load_manager.py` | Policy некритичных G1/G2: pre-transfer LOAD_SHEDDING, admission, continuous overload control и собственный OFF ownership |
| `ha_adapter.py` | HA states -> observations и разрешённые HA service calls |
| `main.py` | Composition root: единый tick, arbitration policy-компонентов, journal, status и log |
| `state_store.py` | Атомарное сохранение persistent state |
| `ha_client.py` | WebSocket/REST transport Home Assistant и revision входящих HA states |

Главное правило границ: **policy-компоненты решают, что требуется; GC и TPC решают, как безопасно выполнить уже разрешённую физическую операцию; HA Adapter только связывает доменную модель с реальными entities.**

Exercise Scheduler и Load Manager не управляют генераторными REMOTE/choke или основной парой Grid/Generator контакторов напрямую.

## 3. Доменные термины питания

### `PowerSource`

Фактически наблюдаемый режим питания дома:

- `GRID` — дом питается от основной сети;
- `GENERATOR` — дом подключён к общей генераторной шине;
- `UPS_ONLY` — обычная шина дома не получает внешний источник, но UPS-линия может работать от MAP;
- `NO_POWER` — питание отсутствует;
- `UNKNOWN` — состояние нельзя безопасно определить.

Нет `BATTERY`, `GENERATOR_A` или `GENERATOR_B` как отдельных силовых источников.

### `PowerPath`

Подтверждённое положение основной пары контакторов:

- `GRID`;
- `ISOLATED`;
- `GENERATOR`;
- `UNKNOWN`.

`PowerPath` и `PowerSource` различаются намеренно. Например, при выбранном Grid path и отсутствующей внешней Grid фактический режим может быть `UPS_ONLY`.

## 4. GeneratorBusTracker

`generator_bus.py` — единственное место, где определяется логический owner общей генераторной шины.

Owner:

- `A`;
- `B`;
- `NONE`;
- `UNKNOWN`.

Tracker использует историю `generator_*_is_running` и повторяет аппаратное FIFO-поведение контакторов:

- первый появившийся RUNNING получает owner;
- второй RUNNING не меняет owner;
- пока текущий owner продолжает RUNNING, owner не меняется;
- если owner остановился, а второй генератор продолжает RUNNING, owner автоматически переходит ко второму;
- если после restart/history gap оба уже RUNNING и порядок нельзя восстановить, owner = `UNKNOWN`.

Ни TPC, ни HA Adapter, ни Load Manager не пытаются повторно вычислять owner.

### Контекст непрерывного RUNNING

Для каждого двигателя Tracker хранит:

- `NONE`;
- `OUTAGE_RELATED`;
- `TEST_RUN`;
- `OTHER`;
- `UNKNOWN`.

Это не ownership двигателя. Контекст используется для классификации текущего run и узких policy-правил.

Внешний `input_boolean.generator_test_mode` может классифицировать новый внешний RUNNING как `TEST_RUN`. Если helper отсутствует, это трактуется как `OFF`; существующий `unknown/unavailable` остаётся неопределённым. Scheduled Exercise передаёт Tracker-у свой internal test slot напрямую и от helper-а не зависит.

## 5. GeneratorController

Один экземпляр GC обслуживает один физический генератор и не знает про PRIMARY/SECONDARY или причину, по которой policy требует RUNNING.

Фазы:

- `WAITING_FOR_DATA`;
- `IDLE`;
- `PREPARING`;
- `WAITING_FOR_RUNNING`;
- `HOLDING_COLD_START_CHOKE`;
- `WARMING_UP`;
- `READY_FOR_LOAD`;
- `WAITING_FOR_LOAD_RELEASE`;
- `COOLING_DOWN`;
- `WAITING_FOR_STOP`;
- `EXTERNAL_RUNNING`;
- `FAULT`.

GC выдаёт только:

- `REMOTE_ON`;
- `REMOTE_OFF`;
- `CHOKE_TO_COLD_START`;
- `CHOKE_TO_RUN`.

Ошибка жизненного цикла фиксируется локальным `FAULT`. Решение о fallback, exercise failure либо системном `RECOVERY_REQUIRED` принадлежит policy-слою.

`step_authorized_shutdown()` используется только после того, как policy уже разрешила остановить конкретный разгруженный двигатель.

При restart работающего scheduler-owned генератора точная transient phase GC не persist-ится. Поэтому GC консервативно восстанавливает choke: не выдаёт повторный REMOTE START, выдерживает безопасный choke-hold и идемпотентно переводит заслонку в RUN перед продолжением.

Имя, модель и паспортные Nominal/Maximum Power приходят из Generator Controller через Home Assistant. Паспортные мощности не являются настройками GC lifecycle и используются Load Manager по фактическому bus owner.

## 6. PowerTransferController

TPC управляет только основной парой Grid / Generator и ничего не знает о Generator A/B, scheduled exercise или допустимой мощности consumer groups.

Фазы:

- устойчивые: `STABLE_GRID`, `STABLE_ISOLATED`, `STABLE_GENERATOR`;
- переходные: `DISCONNECTING_GRID`, `SELECTING_GENERATOR`, `DISCONNECTING_GENERATOR`, `CONNECTING_GRID`;
- служебные: `WAITING_FOR_DATA`, `RECOVERY_REQUIRED`.

Grid -> Generator:

1. `grid_power -> OFF`;
2. дождаться снятия Grid control feedback;
3. `use_generator_as_power_source -> ON`;
4. дождаться generator control feedback.

Generator -> Grid симметрично:

1. `use_generator_as_power_source -> OFF`;
2. дождаться снятия generator feedback;
3. `grid_power -> ON`;
4. дождаться Grid feedback.

Каждый tick выдаёт не более одной новой силовой команды и не начинает следующий шаг до подтверждения предыдущего.

Обычный scheduled exercise TPC не использует: дом остаётся на подтверждённом Grid path.

Load Manager не меняет внутреннюю FSM TPC. `main.py` может временно не передавать TPC желание `GENERATOR`, пока доступные G1/G2 ещё не подтвердили pre-transfer OFF; локальный timeout/отказ Load Manager после этого освобождает core transfer, а не переводит TPC в recovery.

## 7. EnergySupervisor

Supervisor содержит основную ATS-policy, которую нельзя вывести из одного локального контроллера.

Фазы:

- `WAITING_FOR_DATA`;
- `NORMAL`;
- `GRID_FAILURE_DELAY`;
- `STARTING_GENERATOR`;
- `ON_GENERATOR`;
- `RETURNING_TO_GRID`;
- `EXTERNAL_RUNNING`;
- `RECOVERY_REQUIRED`.

Отдельных exercise- или Load Manager-фаз в Supervisor нет.

### Managed-сессия

Сессия хранит:

- причину (`manual_generator_start` / `grid_outage`);
- текущий managed slot;
- началась ли сессия при отсутствующей Grid;
- запрошена ли остановка;
- использован ли единственный fallback;
- наблюдался ли внешний аппаратный takeover после отказа managed generator.

### Fallback

При отказе managed PRIMARY допускается один переход на SECONDARY.

- если SECONDARY уже работает внешне, он не присваивается автоматически;
- если SECONDARY свободен и разрешён, начинается единственный managed fallback;
- после отказа SECONDARY повторного возврата к PRIMARY нет — требуется recovery;
- если внешний SECONDARY уже принял bus после отказа managed PRIMARY, а затем пользователь остановил его, Supervisor не запускает его заново как fallback и переходит в recovery.

### Возврат Grid

После стабильной Grid:

1. Supervisor требует `GRID` у TPC;
2. TPC безопасно снимает Generator и возвращает Grid;
3. только после подтверждённого Grid path разрешается остановка требуемых generator runs;
4. внешний `TEST_RUN`, `OTHER` и `UNKNOWN` узким outage-cleanup правилом не захватываются.

Load Manager не имеет права задерживать возврат Grid из-за meter/G1/G2 failure.

## 8. ExerciseScheduler

`exercise_scheduler.py` — чистый policy-компонент без Home Assistant dependency.

Он хранит отдельно для A/B:

- `initial_reference_time`;
- `last_qualifying_run`;
- `last_window_date`;
- forced-warning state;
- последний physical exercise result/failure.

Одновременно может существовать максимум один `active_attempt`:

```text
STARTING -> RUNNING -> STOPPING
```

Результаты:

- `SUCCESS`;
- `FAILED`;
- `DEFERRED`;
- `INTERRUPTED_BY_OUTAGE`.

`DEFERRED` — journal event, а не physical test failure.

### Scheduling

Для каждого slot независимо:

```text
next_due = reference + interval_days
forced_date = due_date + presence_grace_days
```

Scheduler рассматривает start только в собственном ежедневном `exercise_start_time`. Пропущенное окно не запускается позже в произвольное время.

До forced-date absence must be explicitly confirmed. На forced-date presence уже не блокирует запуск, но остаются Grid, E-stop, known-state, no-transition и no-conflicting-policy prerequisites.

Warning запрашивается заранее. Факт delivery подтверждается App и persist-ится вместе с фактическим временем; forced start проверяет lead >= 60 минут.

### Qualifying run и ownership

Scheduler наблюдает RUNNING даже вне собственного active attempt. Непрерывный достоверный run нужной длительности может обновить `last_qualifying_run` независимо от причины запуска.

После физического начала собственного auto-run Scheduler сохраняет stop ownership до одного из двух событий:

1. подтверждены `RUNNING=OFF` и `REMOTE=OFF`;
2. Supervisor явно создал outage-session на этом же already-running slot, после чего Scheduler записывает `INTERRUPTED_BY_OUTAGE` и передаёт ownership.

Простой факт исчезновения Grid сам по себе ownership не снимает. Maintenance exercise не запускает fallback на второй slot.

## 9. LoadManager

`load_manager.py` — отдельный policy-компонент управления только G1/G2. Он не зависит от Home Assistant API и получает обычный `LoadManagerObservation`.

Конфигурационный master switch — `load_management_enabled`; default `false`. При выключенной функции Load Manager не создаёт G1/G2 actions, не является transfer gate и не деградирует из-за отсутствующих soft inputs.

### 9.1. Управляемые группы и ownership

Приоритеты:

```text
restore/admission: G1 -> G2
overload LOAD_SHEDDING: G2 -> G1
```

Load Manager хранит `shed_by_energy_ats` отдельно для каждой группы. Право будущего автоматического ON появляется только после подтверждённого собственного OFF. Уже выключенная пользователем группа не захватывается в ownership.

Ручной пользовательский ON ранее shed-группы снимает ownership текущего OFF. Если Load Manager позднее снова отключит эту группу из-за overload, создаётся новое ownership.

### 9.2. Pre-transfer LOAD_SHEDDING

Для managed generator session предварительное отключение начинается только когда GC выбранного generator достиг `READY_FOR_LOAD`, а основной TPC ещё не начал generator transfer.

Load Manager:

1. по одной отключает доступные ON-группы;
2. ждёт подтверждения фактического OFF каждой команды;
3. только после обработки доступных групп возвращает `transfer_permitted=True`.

Показания generator meter для этого шага не нужны. Если consumer switch отсутствует, unavailable или не подтверждает OFF до локального timeout, Load Manager фиксирует `DEGRADED`, но освобождает core transfer.

Если Grid вернулась до generator transfer, отдельного transfer ради завершения Load Manager не происходит; после подтверждённого Grid path собственные OFF восстанавливаются обычным Grid-алгоритмом.

### 9.3. Измерение и freshness

После подключения дома к generator bus power-based действия разрешены только при известных:

- фактическом `GeneratorBusOwner`;
- корректных `0 < nominal <= maximum` текущего owner;
- `binary_sensor.generator_meter_status = ON`;
- числовом `sensor.generator_power`;
- известных состояниях G1/G2.

`ha_client.py` ведёт монотонную revision входящих HA state updates. Adapter передаёт revision `sensor.generator_power` как `power_sample_id`, поэтому policy отличает новый sample от повторного чтения кэша.

После изменения нагрузки, restart, восстановления meter либо смены bus owner начинается новое stabilization window. Для решения требуется несколько свежих samples; реализация использует максимум мощности внутри текущего measurement window как консервативный агрегат.

### 9.4. Admission

После transfer сначала измеряется base load с отключёнными собственными G1/G2. Следующая группа может быть добавлена только если:

```text
P <= nominal * (1 - restore_margin_percent / 100)
```

После ON конкретной группы начинается новое stabilization window. Если установившаяся нагрузка после собственного admission оказалась выше nominal, эта же группа возвращается OFF сразу, без ожидания общего nominal-overload timer. Следующая группа в таком cycle не добавляется.

### 9.5. Непрерывный overload control

После startup Load Manager не завершается: пока дом подтверждённо находится на generator bus, каждый новый пригодный sample участвует в контроле.

- `P <= nominal` — нормальная область;
- `nominal < P <= maximum` — запускается `nominal_overload_time`;
- `P > maximum` — используется отдельный более короткий `maximum_overload_confirmation_time`.

После подтверждённой перегрузки Load Manager снимает только одну ON-группу, ждёт подтверждения OFF и новое stabilization window, затем заново оценивает P. Если управляемых ON-групп больше нет, создаётся warning/critical event; generator автоматически только по измеренной перегрузке не останавливается.

Группа, снятая overload/admission, повторно рассматривается не раньше `load_restore_retry_interval` и только после нового restore-margin measurement.

### 9.6. Soft dependencies и DEGRADED

Meter, power metadata и G1/G2 являются soft dependencies относительно core ATS. Их неисправность:

- не блокирует App startup;
- не создаёт системный `RECOVERY_REQUIRED`;
- не останавливает generator;
- не блокирует безопасный возврат Grid.

`DEGRADED` принадлежит только Load Manager. Если meter пропал уже во время устойчивой работы, Load Manager не переключает текущие группы только из-за потери измерителя; power-based действия возобновляются после восстановления данных и нового stabilization window.

### 9.7. Bus takeover

При смене `GeneratorBusOwner` старые limits немедленно перестают использоваться. Новые actions запрещены до корректных limits нового owner и нового stabilization window. UNKNOWN owner переводит только Load Manager в `DEGRADED`.

## 10. Arbitration policy-компонентов

`main.py` является composition root, но не создаёт скрытую третью силовую policy.

При обычной Grid Scheduler может добавить только intent конкретного GC (`desired_running=True`); `desired_source` дома не меняется.

Если появляется основная ATS/ручная операция:

- unstarted exercise можно отложить;
- реальный outage имеет приоритет;
- already-running подходящий exercise-generator может быть явно принят Supervisor-ом в outage session;
- до факта такого handoff Scheduler остаётся ответственным за stop;
- при `RECOVERY_REQUIRED` active exercise переводится в FAILED/STOPPING, а безопасная остановка разрешается через GC safety checks.

Load Manager получает уже принятое Supervisor решение и может временно gate только начало managed `Grid -> Generator` transfer до завершения доступного pre-transfer shedding. Он не меняет `desired_source` Supervisor и не может препятствовать `Generator -> Grid`.

G1/G2 service calls исполняются отдельно от core Generator/TPC pending-action journal: ошибка soft consumer command возвращается Load Manager как локальный failure и не превращается в системный recovery.

## 11. HomeAssistantAdapter

Adapter:

- читает обязательные core physical/control entities;
- читает configurable presence entity;
- читает soft Load Manager entities и per-generator Nominal/Maximum Power;
- формирует observations;
- исполняет Generator/Transfer actions;
- отдельно исполняет G1/G2 actions с локализацией ошибок;
- выполняет аппаратные safety checks Generator/TPC;
- публикует status, Logbook и notifications.

Presence — мягкий Scheduler-input. `unknown/unavailable` presence не делает core ATS «не готовым»; он только не позволяет обычный presence-gated exercise.

Load Manager entities и power metadata намеренно не входят в `missing_required_entities()`. Поэтому их отсутствие не мешает core ATS дождаться собственных обязательных данных и перейти к работе.

Одновременный RUNNING A и B разрешён.

## 12. Один tick

Нормальный поток:

```text
HA snapshot
  -> GeneratorBusTracker
  -> GC/TPC observation refresh
  -> ExerciseScheduler.step()
  -> EnergySupervisor.step(exercise intent)
  -> explicit Exercise/Supervisor handoff when required
  -> LoadManager.step(supervisor decision + bus/meter/load observations)
  -> G1/G2 soft actions
  -> GC actions
  -> Load Manager gate для начала Grid -> Generator transfer
  -> TPC actions
  -> HA Adapter service calls
  -> status/log
  -> persistent journal
```

Policy не должна повторно выполняться из слоя публикации status/log.

## 13. Persistent journal

Top-level `schema_version = 2` сохраняется совместимым с 0.4/0.5.

Journal содержит:

- `app_version`;
- Supervisor;
- `GeneratorBusTracker`;
- `ExerciseScheduler`;
- `LoadManager`;
- core `pending_actions`.

Старый совместимый journal без `exercise_scheduler` или `load_manager` получает новый пустой state соответствующего policy-компонента.

Load Manager persist-ит как минимум собственное `shed_by_energy_ats`, phase/reason, pending consumer action и restore retry deadline. Measurement samples намеренно не persist-ятся: после restart power-based решение доказывается заново новым stabilization window.

Повреждение Load Manager payload локализуется: создаётся безопасный пустой Load Manager ownership, а основной Supervisor не переводится в recovery только из-за soft policy state. Это означает консервативное следствие: при потерянном ownership уже-OFF группа не будет автоматически включена.

Core `pending_actions` сохраняются перед Generator/TPC service call и очищаются только после его завершения; незавершённая core-команда после restart требует recovery. G1/G2 actions не записываются в этот core journal, чтобы ошибка consumer switch не могла заблокировать ATS.

## 14. Status sensor

`sensor.energy_ats_status` — диагностическая проекция, а не источник policy.

Базовые attributes: source/phase/generator/model/managed/bus owner/run-context/PRIMARY/fallback/session/timers/armed.

Exercise добавляет отдельно для A/B due/history/forced-warning/active/result state.

Load Manager добавляет:

- enabled и phase;
- degraded reason;
- текущую measured generator power;
- active nominal/maximum текущего owner;
- state и `shed_by_energy_ats` для G1/G2;
- overload timers;
- next restore retry;
- last reason.

Runtime log при включённом Load Manager также показывает его фазу, текущую мощность и активные limits.

## 15. Safety-инварианты реализации

1. Неизвестное обязательное физическое состояние core ATS блокирует соответствующие управляющие действия.
2. Команда никогда не считается подтверждением.
3. TPC соблюдает break-before-make независимо от policy.
4. Два RUNNING — допустимый физический режим.
5. Нельзя угадывать bus owner при недостаточной истории.
6. Внешний RUNNING не становится managed автоматически.
7. Единственный ATS fallback не превращается в ping-pong.
8. Генератор не останавливается под подтверждённой нагрузкой дома.
9. Scheduled exercise не создаёт отдельный силовой путь и не имеет fallback.
10. Автоматически запущенный exercise-generator не может остаться без policy-owner stop responsibility.
11. Presence failure не блокирует core ATS.
12. Выключенный Load Manager полностью исключён из G1/G2 control path.
13. Load Manager не управляет никакими нагрузками кроме явно заданных G1/G2.
14. Meter/G1/G2/power-metadata failure не создаёт core `RECOVERY_REQUIRED`.
15. Load Manager не включает OFF-группу без собственного подтверждённого ownership.
16. После изменения управляемой нагрузки следующее power-based решение требует нового stabilization window.
17. Generator limits всегда относятся к фактическому bus owner; старые limits после takeover не используются.
18. Generator не останавливается автоматически только по измеренному overload Load Manager.
19. UI/log/persistence не продвигают управляющие FSM.

## 16. Проверка архитектуры

Изменение считается законченным только если одновременно согласованы:

```text
PHYSICAL_POWER_TOPOLOGY_RU.md
        ↓
REQUIREMENTS_RU.md
        ↓
production code
        ↓
unit / integration / end-to-end tests
        ↓
commissioning на реальном оборудовании
```

Если меняется только policy, физическая схема не переписывается.

Зелёный CI проверяет программную модель, но не заменяет физическую проверку контакторов, G1/G2, generator-bus meter, генераторов, DKG116 и MAP.
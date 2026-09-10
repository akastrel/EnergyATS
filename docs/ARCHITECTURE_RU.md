# Energy ATS 0.5.0 — архитектура

## 1. Источники истины

Архитектура реализует два документа более высокого уровня:

1. `PHYSICAL_POWER_TOPOLOGY_RU.md` — что физически существует и как ведёт себя железо;
2. `REQUIREMENTS_RU.md` — как при этой физической схеме должен вести себя EnergyATS.

Этот документ описывает только способ реализации требований. Он не вводит новые физические устройства и не расширяет разрешённые сценарии.

Scheduled exercise в 0.5 является policy-функцией и не меняет физическую схему.

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
| `ha_adapter.py` | HA states -> observations и разрешённые HA service calls |
| `main.py` | Composition root: единый tick, arbitration ES/Exercise, journal, status и log |
| `state_store.py` | Атомарное сохранение persistent state |
| `ha_client.py` | WebSocket/REST transport Home Assistant |

Главное правило границ: **policy-компоненты решают, что требуется; GC и TPC решают, как безопасно выполнить уже разрешённую физическую операцию; HA Adapter только связывает доменную модель с реальными entities.**

Exercise Scheduler не управляет реле напрямую, не имеет собственного алгоритма crank/choke и не дублирует TPC.

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

Ни TPC, ни HA Adapter не пытаются повторно вычислять owner.

### Контекст непрерывного RUNNING

Для каждого двигателя Tracker хранит:

- `NONE`;
- `OUTAGE_RELATED`;
- `TEST_RUN`;
- `OTHER`;
- `UNKNOWN`.

Это не ownership двигателя. Контекст используется для классификации текущего run и узких policy-правил.

Внешний `input_boolean.generator_test_mode` может классифицировать новый внешний RUNNING как `TEST_RUN`. Scheduled Exercise передаёт Tracker-у свой internal test slot напрямую, поэтому не зависит от helper-а и не создаёт нового run-context.

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

## 6. PowerTransferController

TPC управляет только основной парой Grid / Generator и ничего не знает о Generator A/B или scheduled exercise.

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

Отдельных exercise-фаз в Supervisor нет.

### Managed-сессия

Сессия хранит:

- причину (`manual_generator_start` / `grid_outage`);
- текущий managed slot;
- началась ли сессия при отсутствующей Grid;
- запрошена ли остановка;
- использован ли единственный fallback.

### Fallback

При отказе managed PRIMARY допускается один переход на SECONDARY.

- если SECONDARY уже работает внешне, он не присваивается автоматически;
- если SECONDARY свободен и разрешён, начинается единственный managed fallback;
- после отказа SECONDARY повторного возврата к PRIMARY нет — требуется recovery.

### Возврат Grid

После стабильной Grid:

1. Supervisor требует `GRID` у TPC;
2. TPC безопасно снимает Generator и возвращает Grid;
3. только после подтверждённого Grid path разрешается остановка требуемых generator runs;
4. внешний `TEST_RUN`, `OTHER` и `UNKNOWN` узким outage-cleanup правилом не захватываются.

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

### Qualifying run

Scheduler наблюдает RUNNING даже вне собственного active attempt. Непрерывный достоверный run нужной длительности может обновить `last_qualifying_run` независимо от причины запуска.

### Ownership

После физического начала собственного auto-run Scheduler сохраняет stop ownership до одного из двух событий:

1. подтверждены `RUNNING=OFF` и `REMOTE=OFF`;
2. Supervisor явно создал outage-session на этом же already-running slot, после чего Scheduler записывает `INTERRUPTED_BY_OUTAGE` и передаёт ownership.

Простой факт исчезновения Grid сам по себе ownership не снимает.

Maintenance exercise не запускает fallback на второй slot.

## 9. Arbitration Supervisor / Exercise

`main.py` является местом композиции двух policy-компонентов, но не создаёт третью policy.

При обычной Grid Scheduler может добавить только intent конкретного GC (`desired_running=True`); `desired_source` дома не меняется.

Если появляется основная ATS/ручная операция:

- unstarted exercise можно отложить;
- реальный outage имеет приоритет;
- already-running подходящий exercise-generator может быть явно принят Supervisor-ом в outage session;
- до факта такого handoff Scheduler остаётся ответственным за stop;
- при `RECOVERY_REQUIRED` active exercise переводится в FAILED/STOPPING, а безопасная остановка разрешается даже при заблокированной обычной automation policy, но только через GC safety checks.

## 10. HomeAssistantAdapter

Adapter:

- читает физические/управляющие entities;
- читает configurable presence entity;
- формирует observations;
- исполняет уже сформированные Generator/Transfer actions;
- выполняет локальные аппаратные safety checks;
- публикует status, Logbook и notifications.

Presence — мягкий Scheduler-input. `unknown/unavailable` presence не делает core ATS «не готовым»; он только не позволяет обычный presence-gated exercise.

Одновременный RUNNING A и B разрешён.

## 11. Один tick

Нормальный поток 0.5:

```text
HA snapshot
  -> GeneratorBusTracker
  -> GC/TPC observation refresh
  -> ExerciseScheduler.step()
  -> EnergySupervisor.step(exercise intent)
  -> explicit handoff/arbitration when required
  -> GC/TPC actions
  -> HA Adapter service calls
  -> status/log
  -> persistent journal
```

Policy не должна повторно выполняться из слоя публикации status/log.

## 12. Persistent journal

Top-level `schema_version = 2` сохраняется совместимым с 0.4.

Journal содержит:

- `app_version`;
- Supervisor;
- `GeneratorBusTracker`;
- `ExerciseScheduler`;
- `pending_actions`.

0.4 journal без `exercise_scheduler` получает новый scheduler-state. Неподдерживаемый/повреждённый обязательный state не угадывается.

`pending_actions` сохраняются перед физическим service call и очищаются только после его завершения.

## 13. Status sensor

`sensor.energy_ats_status` — диагностическая проекция, а не источник policy.

Базовые attributes: source/phase/generator/model/managed/bus owner/run-context/PRIMARY/fallback/session/timers/armed.

Exercise добавляет отдельно для A/B:

- enabled;
- initial reference;
- last qualifying run;
- next due / overdue;
- forced date;
- warning sent time;
- active;
- configured run duration;
- last result / failure reason;
- общий active exercise slot и remaining seconds.

## 14. Safety-инварианты реализации

1. Неизвестное обязательное физическое состояние блокирует управляющие действия.
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
12. UI/log/persistence не продвигают управляющие FSM.

## 15. Проверка архитектуры

Изменение считается законченным только если одновременно согласованы:

```text
PHYSICAL_POWER_TOPOLOGY_RU.md
        ↓
REQUIREMENTS_RU.md
        ↓
production code
        ↓
unit / end-to-end tests
        ↓
commissioning на реальном оборудовании
```

Если меняется только policy, физическая схема не переписывается.

Зелёный CI проверяет программную модель, но не заменяет физическую проверку контакторов, обратных связей и реального поведения DKG116/MAP.

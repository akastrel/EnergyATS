# Energy ATS 0.4.0 — архитектура

## 1. Граница документа

Архитектура реализует два более фундаментальных документа:

- `PHYSICAL_POWER_TOPOLOGY_RU.md` — реальная электрическая схема;
- `REQUIREMENTS_RU.md` — требуемое поведение EnergyATS.

Архитектура не должна придумывать отсутствующие физические устройства и не является источником требований.

## 2. Один процесс, несколько независимых обязанностей

EnergyATS — один Home Assistant App и один Python-процесс.

| Модуль | Ответственность |
|---|---|
| `energy_supervisor.py` | Policy: outage, managed-session, fallback, возврат Grid, recovery |
| `power_transfer.py` | Основные контакторы Grid / Generator, break-before-make и подтверждения |
| `generator_controller.py` | Жизненный цикл одного двигателя: REMOTE, choke, запуск, прогрев, cooldown, stop |
| `generator_bus.py` | Наблюдаемый owner общей генераторной шины и происхождение текущих запусков |
| `ha_adapter.py` | Преобразование HA states в доменные observations и исполнение service calls |
| `main.py` | Composition root, единый tick, persistent journal, status/log |
| `state_store.py` | Атомарное сохранение состояния App |
| `ha_client.py` | WebSocket/REST transport Home Assistant |

Доменные контроллеры не используют HA entities как внутреннюю шину сообщений.

## 3. Модель питания 0.4

### 3.1. Основные контакторы дома

TPC управляет только реальными управляющими сигналами:

```text
switch.grid_power
switch.use_generator_as_power_source
```

`grid_power` — разрешение сетевой ветви. `use_generator_as_power_source` — выбор основных контакторов в сторону генераторной шины.

Основные контакторы аппаратно взаимно заблокированы.

### 3.2. Нет Battery path

МАП самостоятельно переходит на АКБ при исчезновении входного AC.

Поэтому в доменной модели нет `Battery contactor` и нет `PowerPath.BATTERY`.

Когда основная часть дома не получает Grid/Generator, EnergyATS использует наблюдаемое состояние:

```text
PowerSource.UPS_ONLY
```

Это описание пользовательского режима, а не команда на МАП.

### 3.3. Генераторная шина

A и B могут работать одновременно. Конкретный генератор к общей генераторной шине выбирают физические взаимно заблокированные контакторы.

TPC не имеет software-selector A/B и не пытается им управлять.

## 4. GeneratorBusTracker

`generator_bus.py` хранит логическую копию наблюдаемого аппаратного FIFO.

Owner:

```text
A
B
NONE
UNKNOWN
```

Правила:

- один RUNNING -> он owner;
- A стал owner, затем запустился B -> owner остаётся A;
- owner остановился при уже работающем втором -> owner переходит второму;
- restart при A+B RUNNING и отсутствии достоверной истории -> `UNKNOWN`, без угадывания.

Tracker также хранит run-context каждого слота:

```text
NONE
MANAGED_OUTAGE
MANAGED_OTHER
EXTERNAL_OUTAGE
TEST_RUN
OTHER_EXTERNAL
UNKNOWN_EXTERNAL
```

`MANAGED_OUTAGE` и `EXTERNAL_OUTAGE` являются outage-related.

Известный owner и run-context записываются в persistent journal.

## 5. Три разных ownership

В 0.4 важно не смешивать три понятия.

### 5.1. Managed generator

Двигатель, жизненным циклом которого управляет текущая сессия EnergyATS.

### 5.2. Bus owner

Генератор, физически подключённый аппаратной схемой к общей генераторной шине.

### 5.3. External run

Работающий двигатель, который не принадлежит managed-сессии EnergyATS.

Возможна штатная ситуация:

```text
managed generator = A
A RUNNING = ON
B RUNNING = ON
bus owner = A
B run context = EXTERNAL_OUTAGE
```

Сам факт двух RUNNING не является fault.

## 6. Energy Supervisor

ES выдаёт уровневые цели, но не service calls.

Он отвечает за:

- ручную managed-сессию;
- автоматическую outage-сессию;
- выбор PRIMARY из `select.primary_generator`;
- policy-флаги `generator_a_enabled` / `generator_b_enabled`;
- один fallback `PRIMARY -> SECONDARY`;
- возврат дома на Grid;
- завершение outage-related runs;
- recovery при неоднозначном безопасном продолжении.

### 6.1. Fallback

Если managed PRIMARY не запустился или неожиданно потерян и SECONDARY остановлен/доступен, ES разрешает один переход:

```text
PRIMARY -> SECONDARY
```

Повторного `SECONDARY -> PRIMARY` нет.

Если SECONDARY уже работает внешне, ES не превращает его в managed. После остановки прежнего owner аппаратная схема может передать шину внешнему SECONDARY; ES лишь наблюдает этот takeover.

## 7. Generator Controller

Один FSM используется независимо для A и B:

```text
WAITING_FOR_DATA
IDLE
PREPARING
WAITING_FOR_RUNNING
HOLDING_COLD_START_CHOKE
WARMING_UP
READY_FOR_LOAD
WAITING_FOR_LOAD_RELEASE
COOLING_DOWN
WAITING_FOR_STOP
EXTERNAL_RUNNING
FAULT
RECOVERY_REQUIRED
```

GC знает только собственный двигатель и его `load_connected`.

Он не знает о Grid и не выбирает источник дома.

При внутренней ошибке управляемого запуска GC снимает REMOTE и возвращает заслонку в рабочее положение. Это, в частности, позволяет Supervisor после неудачного PRIMARY безопасно перейти к SECONDARY.

## 8. Power Transfer Controller

TPC моделирует только основные контакторы.

Переход Grid -> Generator:

```text
Grid permission OFF
-> подтверждение сетевой управляющей цепи OFF
-> generator selector ON
-> подтверждение генераторной управляющей цепи ON
```

Возврат Generator -> Grid:

```text
generator selector OFF
-> подтверждение генераторной управляющей цепи OFF
-> Grid permission ON
-> подтверждение сетевой управляющей цепи
```

Это break-before-make.

`binary_sensor.house_powered_by_grid` и `binary_sensor.house_powered_by_generator` являются датчиками **управляющих цепей контакторов**, а не независимыми датчиками силового напряжения после контакторов. TPC учитывает именно такой смысл обратной связи.

## 9. Внешние генераторы и локальная защита Adapter

Внешний генератор не захватывается в managed ownership.

Одновременно работающие A+B принимаются как нормальный физический факт.

При этом Adapter сохраняет дополнительный локальный предохранитель: EnergyATS сам не подаёт новый `REMOTE_ON` одному генератору, пока другой подтверждённо RUNNING. Текущий policy fallback сначала фиксирует отказ/остановку PRIMARY и только затем запускает SECONDARY. Это ограничение **не запрещает внешний/локальный параллельный запуск** второго генератора и не объявляет два RUNNING аварией.

`REMOTE_OFF` работающего генератора также блокируется, пока управляющая цепь дома подтверждает генераторную ветвь и данный двигатель ещё RUNNING.

## 10. Outage-related shutdown и TEST_RUN

Новый OFF->ON фронт внешнего генератора классифицируется по состоянию Grid и `input_boolean.generator_test_mode`.

Если запуск произошёл при `grid_input_ready = OFF` и не был помечен TEST, он получает `EXTERNAL_OUTAGE`.

После стабильного восстановления Grid:

1. TPC возвращает дом на сетевую сторону;
2. подтверждается снятие генераторной ветви;
3. GC выполняет cooldown;
4. EnergyATS останавливает все известные outage-related runs.

`TEST_RUN` не останавливается только из-за возврата Grid.

Если состояние test helper недоступно в момент нового внешнего запуска, контекст становится `UNKNOWN_EXTERNAL`; такой двигатель безопаснее не остановить автоматически, чем ошибочно классифицировать как outage-related.

## 11. Tick приложения

На каждом tick `main.py` выполняет порядок:

```text
HA snapshot
-> generator metadata / primary sync
-> GeneratorBusTracker update
-> refresh GC/TPC observations без команд
-> EnergySupervisor decision
-> GC actions
-> TPC actions
-> persistent pending-actions journal
-> hardware service calls
-> events / runtime log / status sensor
```

Силовые TPC actions исполняются раньше команд двигателя.

Перед аппаратными service calls pending actions уже находятся в journal.

## 12. Persistent state 0.4

Файл:

```text
/data/energy-supervisor-state.json
```

Envelope journal использует `journal_schema_version = 2`.

Supervisor использует собственную schema version 4. Journal также хранит `generator_bus`, включая owner, run-context и предыдущие RUNNING.

Миграция ошибочной модели persistent state 0.3 в 0.4 намеренно не выполняется.

Если restart произошёл во время неподтверждённой физической транзакции, App переходит в `RECOVERY_REQUIRED` и не продолжает её вслепую.

## 13. Диагностический status

`sensor.energy_ats_status` — read-only представление.

В 0.4 используется `schema_version = 3`. Помимо source/phase и managed generator он содержит:

```text
bus_owner
bus_owner_slot
generator_a_run_context
generator_b_run_context
fallback_used
```

Пользовательские поля generator/primary содержат реальные имена из HA, машинные поля сохраняют A/B.

## 14. Конфигурация

Generator Controller / HA описывают установленное оборудование:

```text
sensor.generator_a_name
sensor.generator_b_name
sensor.generator_a_model
sensor.generator_b_model
select.primary_generator
```

EnergyATS хранит policy:

```text
armed
grid_failure_delay
grid_restore_stable_time
transfer_confirmation_timeout
generator_a_enabled
generator_b_enabled
```

Смена primary применяется к следующей новой managed-сессии и не переписывает уже активную.

## 15. Главный инвариант

> Физика определяет, что возможно. Requirements определяют, что разрешено. Supervisor решает **что делать**, TPC — **как переключить основные контакторы**, GC — **как управлять одним двигателем**, GeneratorBusTracker — **кто физически владеет общей генераторной шиной**.

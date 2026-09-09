# Home Assistant entities и команды — Energy ATS 0.4.0

Этот документ описывает фактический HA-контракт текущей реализации. Физический смысл сигналов задаёт `PHYSICAL_POWER_TOPOLOGY_RU.md`, policy — `REQUIREMENTS_RU.md`.

A/B — стабильные аппаратные слоты. Пользовательские имена и модели читаются отдельно и не меняют entity_id.

## 1. Helper-ы EnergyATS

Корневой `ats.yaml` создаёт:

```text
input_boolean.automatic_generator_transfer
input_boolean.generator_test_mode
```

### `input_boolean.automatic_generator_transfer`

Разрешает автоматическую outage-сессию после физического исчезновения Grid. Ручная команда `start_generator` от него не зависит.

### `input_boolean.generator_test_mode`

Явный классификатор **нового фронта RUNNING** как `TEST_RUN`.

Helper не запускает и не останавливает двигатель. Он только сообщает `GeneratorBusTracker`, что новый запуск является тестовым и не должен автоматически останавливаться правилом завершения outage.

## 2. Обязательные входные entities

### Grid и основные контакторы

```text
binary_sensor.grid_input_ready
binary_sensor.house_powered_by_grid
binary_sensor.house_powered_by_generator
switch.grid_power
switch.use_generator_as_power_source
```

Смысл:

- `grid_input_ready` — физическая доступность/пригодность внешней Grid;
- `house_powered_by_grid` — подтверждение цепи управления основного Grid-контактора;
- `house_powered_by_generator` — подтверждение цепи управления основного Generator-контактора;
- `grid_power` — разрешение Grid-ветви;
- `use_generator_as_power_source` — выбор основной парой контакторов Generator (`ON`) / Grid (`OFF`).

`house_powered_by_*` **не являются независимым измерением напряжения после силовых контактов**.

### Генераторы

```text
binary_sensor.generator_a_is_running
binary_sensor.generator_b_is_running
switch.generator_a_remote_start
switch.generator_b_remote_start
```

`generator_*_is_running` используется как физическое подтверждение работающего генератора/наличия его выхода.

Оба `*_is_running` могут одновременно быть `ON`. Это штатный режим и не означает одновременного подключения обоих генераторов к общей генераторной шине.

### Emergency Stop

```text
switch.generators_emergency_stop
```

E-stop имеет приоритет над обычным управлением. Неизвестное состояние этого entity также не считается безопасным разрешением на запуск.

### Метаданные и PRIMARY

```text
sensor.generator_a_name
sensor.generator_b_name
sensor.generator_a_model
sensor.generator_b_model
select.primary_generator
```

Имена должны быть непустыми и различаться. `select.primary_generator` содержит одно из фактических имён и преобразуется во внутренний слот A/B.

### Температура

```text
sensor.garage_temperature
```

Используется GC для choke/warmup. Недоступная температура обрабатывается консервативным профилем, но не подменяет обязательные RUNNING/REMOTE/E-stop данные.

## 3. Управляющие buttons

Для каждого генератора используются две физические команды заслонки:

```text
button.generator_a_choke_to_cold_start
button.generator_a_choke_to_run
button.generator_b_choke_to_cold_start
button.generator_b_choke_to_run
```

Их физический смысл уже нормализован именами: `to_cold_start` переводит заслонку в положение холодного запуска, `to_run` — в рабочее положение.

## 4. Команды App через STDIN

EnergyATS принимает:

```text
start_generator
stop_generator
reset
```

- `start_generator` — начать managed-сессию на выбранном PRIMARY;
- `stop_generator` — безопасно завершить текущую managed-сессию;
- `reset` — начать контролируемое восстановление после `RECOVERY_REQUIRED`.

Команды не обходят safety checks и в режиме DISARMED не исполняют аппаратные действия.

## 5. Status sensor

App публикует:

```text
sensor.energy_ats_status
```

Это диагностическая проекция, а не вход управляющей логики.

### State

Человекочитаемое состояние, например:

```text
Питание от основной сети
Запуск генератора
Питание от генератора
Питание от внешнего генератора
Возврат на основную сеть
В доме работает только UPS линия
Требуется восстановление
DISARMED — только наблюдение
```

### Attributes

Текущая реализация публикует:

```text
source
generator
generator_model
generator_slot
managed_generator
bus_owner
generator_a_run_context
generator_b_run_context
primary_generator
phase
remaining_seconds
session_reason
fallback_used
armed
```

Также публикуются стандартные `friendly_name` и `icon`.

В status sensor 0.4 **нет** отдельных `schema_version`, `bus_owner_slot` или `primary_generator_slot`.

### `source`

Допустимые значения доменной модели:

```text
grid
generator
ups_only
no_power
unknown
```

A/B не кодируются в `source`. Конкретный генератор определяется `generator` / `generator_slot` и `bus_owner`.

### `phase`

Фаза Supervisor:

```text
waiting_for_data
normal
grid_failure_delay
starting_generator
on_generator
returning_to_grid
external_running
recovery_required
```

### Run context

`generator_a_run_context` и `generator_b_run_context`:

```text
none
outage_related
test_run
other
unknown
```

- `outage_related` — разрешено автоматически остановить после безопасного возврата на стабильную Grid;
- `test_run` — не останавливается этим правилом;
- `other` — известный не-outage запуск;
- `unknown` — причина непрерывного RUNNING не доказана, поэтому автоматическая остановка запрещена.

## 6. Logbook и уведомления

Аппаратные действия и события Supervisor публикуются через HA Logbook. События уровня `critical` дополнительно вызывают:

```text
script.notify_critical
```

Ошибка Logbook/status/notification считается диагностической и не должна сама прерывать управляющую последовательность.

## 7. Что намеренно отсутствует

В HA-контракте EnergyATS 0.4 нет:

- отдельного Battery contactor/path;
- selector A/B генераторной шины — owner выбирает физическая взаимно заблокированная схема;
- A/B position feedback контакторов генераторов;
- helper-а, который объявляет внешний генератор managed;
- автоматического запрета второго RUNNING.

## 8. Fail-safe по неизвестным данным

Перед аппаратным управлением обязательные entities должны иметь определённые значения. `unknown`, `unavailable` или отсутствие обязательного entity не интерпретируются как удобное значение по умолчанию.

Если физическое состояние нельзя доказать, EnergyATS сохраняет наблюдаемость, но не получает права угадывать и выдавать активные команды.
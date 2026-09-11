# Home Assistant entities и команды — Energy ATS 0.7.0

Этот документ описывает фактический HA-контракт текущей реализации. Физический смысл сигналов задаёт `PHYSICAL_POWER_TOPOLOGY_RU.md`, policy — `REQUIREMENTS_RU.md`.

A/B — стабильные аппаратные слоты. Пользовательские имена, модели и паспортные мощности читаются отдельно и не меняют внутренний смысл слотов.

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

Helper не запускает и не останавливает двигатель. Он только сообщает `GeneratorBusTracker`, что новый запуск является тестовым и не должен автоматически останавливаться правилом завершения outage. Если helper вообще отсутствует, это трактуется как `OFF`; существующий `unknown/unavailable` остаётся неопределённым.

## 2. Обязательные входные entities core ATS

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

Core ATS использует:

```text
sensor.generator_a_name
sensor.generator_b_name
sensor.generator_a_model
sensor.generator_b_model
select.primary_generator
```

Имена должны быть непустыми и различаться. `select.primary_generator` содержит одно из фактических имён и преобразуется во внутренний слот A/B.

Load Manager дополнительно читает числовые read-only metadata:

```text
sensor.generator_a_nominal_power
sensor.generator_a_maximum_power
sensor.generator_b_nominal_power
sensor.generator_b_maximum_power
```

Они измеряются в W и являются **soft dependency**: отсутствие/ошибка этих sensors не блокирует core ATS. При включённом Load Manager power-based admission/shedding в таком случае приостанавливаются локальным `DEGRADED`.

### Температура

```text
sensor.garage_temperature
```

Используется GC для choke/warmup. Недоступная температура обрабатывается консервативным профилем, но не подменяет обязательные RUNNING/REMOTE/E-stop данные.

## 3. Load Manager: soft-dependency entities

Load Manager управляет только двумя группами:

```text
G1 = switch.non_critical_loads_first_floor
G2 = switch.non_critical_loads_basement_floor
```

G1 имеет более высокий приоритет. Восстановление выполняется `G1 -> G2`, overload `LOAD_SHEDDING` — `G2 -> G1`.

Счётчик общей генераторной шины:

```text
binary_sensor.generator_meter_status
sensor.generator_power
sensor.generator_current
sensor.generator_voltage
sensor.generator_apparent_power
sensor.generator_reactive_power
sensor.generator_power_factor
sensor.generator_frequency
```

Для автоматических power-based решений используется `sensor.generator_power`. Остальные meter entities публикуются как диагностическая телеметрия.

Все entities этого раздела являются soft dependencies относительно core ATS. Их отсутствие, `unknown/unavailable`, отказ Modbus или ошибка команды G1/G2 не должны сами по себе переводить Supervisor/TPC/GC в `RECOVERY_REQUIRED` либо блокировать возврат Grid.

### Батарейные soft dependencies

Delayed Start / Charge Cycling используют `sensor.ups_battery_charge_level_soc`, `sensor.ups_battery_time_remaining_minutes_ttg`, `binary_sensor.ups_running_on_battery` и `binary_sensor.ups_ready`. Их смысл и проверка достоверности приведены в [руководстве](DELAYED_START_RU.md#батарейные-сигналы).

Отсутствие этих entities не блокирует startup core ATS. При выключенных функциях они не влияют на управление.

## 4. Управляющие buttons генераторов

Для каждого генератора используются две физические команды заслонки:

```text
button.generator_a_choke_to_cold_start
button.generator_a_choke_to_run
button.generator_b_choke_to_cold_start
button.generator_b_choke_to_run
```

Их физический смысл уже нормализован именами: `to_cold_start` переводит заслонку в положение холодного запуска, `to_run` — в рабочее положение.

## 5. Команды App через STDIN

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

## 6. Status sensor

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

### Базовые attributes

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

A/B не кодируются в `source`. Конкретный генератор определяется `generator` / `generator_slot` и `bus_owner`.

`source` принимает:

```text
grid
generator
ups_only
no_power
unknown
```

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

Run context A/B:

```text
none
outage_related
test_run
other
unknown
```

### Load Manager attributes

```text
load_management_enabled
load_manager_phase
load_manager_operation
load_manager_degraded_reason
generator_power
active_generator_nominal_power
active_generator_maximum_power
load_g1_state
load_g1_shed_by_energy_ats
load_g2_state
load_g2_shed_by_energy_ats
load_nominal_overload_since
load_maximum_overload_since
load_next_restore_retry
load_last_reason
```

`load_manager_phase` может принимать:

```text
disabled
idle
load_shedding
measuring
stable
degraded
```

`degraded` относится только к Load Manager и сам по себе не означает системный `recovery_required`.

Exercise Scheduler также публикует свои due/history/active attributes в этом же status sensor; их точный набор формируется Scheduler-ом.

### Delayed Start / Charge Cycling attributes

| Attribute | Значение |
|---|---|
| `delayed_start_enabled`, `charge_cycle_enabled` | Разрешение функций |
| `charge_cycle_state`, `delayed_start_reason` | Состояние policy и причина текущего решения |
| `battery_soc`, `battery_ttg_minutes` | Заряд и оставшиеся минуты; некорректное значение — `null` |
| `battery_discharging`, `battery_ready` | Разряд и готовность UPS |
| `generator_start_soc`, `generator_target_charge_soc` | Пороги заряда |
| `delayed_start_elapsed_seconds`, `delayed_start_remaining_seconds` | Время текущего ожидания и остаток, секунды |
| `cycle_session_owned_by_energy_ats` | Право cycling завершить эту сессию |
| `session_manual_override` | Пользователь запретил автоматическое завершение по Target SoC |

Состояния policy: `idle`, `waiting_on_ups`, `generator_required`, `charging`, `target_reached`, `degraded`. `returning_to_ups` — фаза Supervisor при снятии питания дома с генератора и его остановке. Source продолжает отражать фактически наблюдаемое питание, а не цель переключения.

## 7. Logbook и уведомления

Аппаратные действия и события Supervisor/Exercise/Load Manager публикуются через HA Logbook. События уровня `critical` дополнительно вызывают:

```text
script.notify_critical
```

Человеко-читаемые предупреждения Load Manager о локальной деградации и sustained overload могут также отправляться через тот же пользовательский notification script.

Ошибка Logbook/status/notification считается диагностической и не должна сама прерывать управляющую последовательность.

## 8. Что намеренно отсутствует

В HA-контракте EnergyATS нет:

- отдельного Battery contactor/path;
- selector A/B генераторной шины — owner выбирает физическая взаимно заблокированная схема;
- A/B position feedback контакторов генераторов;
- helper-а, который объявляет внешний генератор managed;
- автоматического запрета второго RUNNING;
- права Load Manager управлять REMOTE/choke/stop генератора.

## 9. Fail-safe по неизвестным данным

Перед аппаратным управлением core ATS обязательные entities должны иметь определённые значения. `unknown`, `unavailable` или отсутствие обязательного entity не интерпретируются как удобное значение по умолчанию.

Load Manager inputs из раздела 3 отделены от этого правила как soft dependencies. При их потере Load Manager прекращает те действия, для которых данных недостаточно, но core ATS продолжает работу согласно собственным требованиям.

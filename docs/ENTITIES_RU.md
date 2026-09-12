# Home Assistant entities и команды — Energy ATS 1.0.3

Этот документ описывает фактический HA contract текущей реализации.

- физический смысл сигналов: `PHYSICAL_POWER_TOPOLOGY_RU.md`;
- нормативное поведение: `REQUIREMENTS_RU.md`;
- установка/обновление: `INSTALL_RU.md`.

A/B — стабильные внутренние physical slots. Пользовательские имена, модели и паспортные мощности читаются отдельно и не меняют смысл slot identity.

## 1. Helper-ы EnergyATS

Корневой `ats.yaml` создаёт:

```text
input_boolean.automatic_generator_transfer
input_boolean.generator_test_mode
```

### `input_boolean.automatic_generator_transfer`

Разрешает automatic outage session после физического исчезновения Grid. Manual `start_generator` от этого helper не зависит.

### `input_boolean.generator_test_mode`

Положительный маркер **нового внешнего RUNNING** как `TEST_RUN`.

Helper не запускает и не останавливает generator. Scheduled Exercise знает происхождение собственного run напрямую и не требует переключения этого helper.

Если entity вообще отсутствует, это трактуется как `OFF`; если существующий entity временно `unknown/unavailable`, его состояние остаётся неопределённым.

## 2. Core Grid / transfer entities

```text
binary_sensor.grid_input_ready
binary_sensor.house_powered_by_grid
binary_sensor.house_powered_by_generator
switch.grid_power
switch.use_generator_as_power_source
```

Смысл:

- `grid_input_ready` — физическая доступность/пригодность внешней Grid;
- `house_powered_by_grid` — feedback управляющей цепи основного Grid contactor;
- `house_powered_by_generator` — feedback управляющей цепи основного Generator contactor;
- `grid_power` — разрешение Grid branch;
- `use_generator_as_power_source` — selector основной пары контакторов: `ON` Generator / `OFF` Grid.

Важно: `house_powered_by_*` находятся в управляющих цепях и не являются независимым силовым измерением после контактора.

Успешный HA service call также не считается физическим подтверждением transfer.

## 3. Core generator entities

Для A/B:

```text
binary_sensor.generator_a_is_running
binary_sensor.generator_b_is_running
switch.generator_a_remote_start
switch.generator_b_remote_start
```

`generator_*_is_running` — физическое подтверждение работы/наличия выхода generator.

`generator_*_remote_start` — управляющий уровневый REMOTE signal. `ON` не доказывает успешный start, `OFF` не доказывает подтверждённую остановку.

Оба `*_is_running` могут одновременно быть `ON`. Это штатно и не означает, что оба generator одновременно подключены к общей bus.

### Choke commands

```text
button.generator_a_choke_to_cold_start
button.generator_a_choke_to_run
button.generator_b_choke_to_cold_start
button.generator_b_choke_to_run
```

Названия уже нормализуют физический смысл: `to_cold_start` — положение холодного запуска, `to_run` — рабочее положение.

## 4. Safety / environment

```text
switch.generators_emergency_stop
sensor.garage_temperature
```

E-stop имеет приоритет над обычным управлением. Неизвестное состояние E-stop не считается безопасным разрешением на start.

`garage_temperature` используется GC для choke/warmup. Временная недоступность температуры обрабатывается консервативно и сама по себе не блокирует core ATS.

## 5. Generator metadata и PRIMARY

Core ATS использует:

```text
sensor.generator_a_name
sensor.generator_b_name
sensor.generator_a_model
sensor.generator_b_model
select.primary_generator
```

Имена должны быть непустыми и различаться. `select.primary_generator` должен совпадать с одним из фактических generator names и преобразуется во внутренний A/B slot.

Load Manager дополнительно использует:

```text
sensor.generator_a_nominal_power
sensor.generator_a_maximum_power
sensor.generator_b_nominal_power
sensor.generator_b_maximum_power
```

Единицы — W. Эти sensors являются soft dependencies относительно core ATS.

## 6. UPS Run — battery soft dependencies

UPS Run использует:

```text
sensor.ups_battery_charge_level_soc
sensor.ups_battery_time_remaining_minutes_ttg
binary_sensor.ups_running_on_battery
binary_sensor.ups_ready
```

Смысл:

- `ups_battery_charge_level_soc` — числовой SoC 0–100 %;
- `ups_battery_time_remaining_minutes_ttg` — числовой TTG в **минутах**;
- `ups_running_on_battery` — `ON`, когда батарея реально разряжается; при charge/float должен быть `OFF`;
- `ups_ready` — способность UPS/АКБ продолжать работу, включая отсутствие critical battery state.

Все четыре — soft dependencies core ATS. Если UPS Run выключен, они не влияют на обычную ATS logic.

При включённом Delayed Start stale/invalid telemetry отменяет ожидание и приводит к fail-safe generator start. TTG учитывается только при реальном discharge.

## 7. Load Manager — soft dependencies

Управляемые группы:

```text
G1 = switch.non_critical_loads_first_floor
G2 = switch.non_critical_loads_basement_floor
```

Приоритет:

```text
restore: G1 -> G2
shed:    G2 -> G1
```

Generator-bus meter:

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

Для automatic power decisions используется `sensor.generator_power`. U/I/S/Q/PF/Frequency — диагностическая telemetry, если отдельным requirement не задано иное.

Meter/G1/G2/power metadata являются soft dependencies core ATS. Их отсутствие, `unknown/unavailable`, Modbus failure или consumer switch error не должны сами по себе создавать system `RECOVERY_REQUIRED` либо блокировать возврат Grid.

Stale stream новых `sensor.generator_power` samples также считается недостоверным measurement: Load Manager переходит в локальный `DEGRADED` и после восстановления требует нового stabilization window.

## 8. Presence для Scheduled Exercise

Entity задаётся configuration option:

```text
family_presence_entity
```

Default:

```text
group.family
```

Presence — soft Scheduler input. `unknown/unavailable` не блокирует core ATS, но ordinary Exercise до forced date требует достоверного подтверждения отсутствия семьи непосредственно перед REMOTE ON.

## 9. Команды App через STDIN

EnergyATS принимает:

```text
start_generator
stop_generator
reset
```

- `start_generator` — manual managed session / explicit manual handoff;
- `stop_generator` — безопасное завершение managed session;
- `reset` — контролируемое Recovery.

Команды не обходят safety checks. В DISARMED аппаратные действия не разрешены.

## 10. Status sensor

App публикует:

```text
sensor.energy_ats_status
```

Это read-only diagnostic projection, а не управляющий input.

### State

Примеры человекочитаемого state:

```text
Ожидание данных
Ожидание запуска генератора
Запуск генератора
Переключение на генератор
Питание от генератора
Питание от внешнего генератора
Возврат на основную сеть
Переход на питание только от UPS
В доме работает только UPS линия
Требуется восстановление
DISARMED — только наблюдение
```

### Core attributes

```text
source
phase
generator
generator_model
generator_slot
managed_generator
bus_owner
generator_a_run_context
generator_b_run_context
primary_generator
remaining_seconds
session_reason
fallback_used
cycle_session_owned_by_energy_ats
session_manual_override
armed
```

`source`:

```text
grid
generator
ups_only
no_power
unknown
```

Supervisor `phase`:

```text
waiting_for_data
normal
grid_failure_delay
starting_generator
on_generator
returning_to_grid
returning_to_ups
external_running
recovery_required
```

Run-context A/B:

```text
none
outage_related
test_run
other
unknown
```

A/B не кодируются в `source`. Конкретный generator определяется через `generator`, `generator_slot` и `bus_owner`.

## 11. UPS Run status attributes

`UPSRun.status_attributes()` публикует:

```text
delayed_start_enabled
charge_cycle_enabled
charge_cycle_state
delayed_start_reason
battery_soc
battery_ttg_minutes
battery_discharging
battery_ready
generator_start_soc
generator_target_charge_soc
delayed_start_elapsed_seconds
delayed_start_remaining_seconds
```

Дополнительно top-level status содержит:

```text
cycle_session_owned_by_energy_ats
session_manual_override
```

`charge_cycle_state`:

```text
idle
waiting_on_ups
generator_required
charging
target_reached
degraded
```

`returning_to_ups` — не UPS Run state, а Supervisor phase во время физического снятия дома с generator bus и завершения cycle.

## 12. Load Manager status attributes

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

`load_manager_phase`:

```text
disabled
idle
load_shedding
measuring
stable
degraded
```

`degraded` принадлежит только Load Manager и не означает system `recovery_required`.

## 13. Scheduled Exercise status

Exercise Scheduler добавляет per-slot attributes для:

- enabled state;
- last qualifying / initial reference;
- next due / overdue / forced date;
- warning state;
- active attempt / duration;
- last result / failure reason.

Точный набор формируется `ExerciseScheduler.status_attributes()`; это диагностическая projection, а не отдельный HA control contract.

## 14. Logbook и notifications

EnergyATS публикует runtime events в Logbook. Critical events дополнительно вызывают:

```text
script.notify_critical
```

Load Manager warnings и другие обычные user notifications могут использовать тот же script.

App log является полным журналом и содержит MAIN/DETAIL events, аппаратные команды и отправляемые сообщения. В основной поток Home Assistant Logbook публикуются только MAIN events; отдельной DETAIL-сущности нет.

В 1.0.3 обычные status/Logbook/user publications выполняются best-effort background tasks и не должны задерживать control tick на сетевой timeout. При reconnect незавершённые background publications отменяются.

Исключение — предупреждение перед forced Scheduled Exercise. Оно отправляется синхронно, потому что Scheduler не имеет права считать warning доставленным до успешного ответа Home Assistant.

Ошибки diagnostic publication не должны сами по себе менять физический source/session.

## 15. Required vs soft dependencies

Перед active hardware control обязательные core entities должны иметь определённые значения. `unknown`, `unavailable` и отсутствие required entity не интерпретируются как удобное default state.

Отдельно как soft dependencies определены:

- battery telemetry UPS Run;
- generator meter и G1/G2 Load Manager;
- Nominal/Maximum metadata;
- presence Scheduled Exercise;
- ambient temperature с консервативным fallback.

Отказ soft dependency ограничивает соответствующую локальную функцию, но не должен сам по себе превращаться в core Recovery.

## 16. Что намеренно отсутствует в HA contract

EnergyATS не моделирует как physical actuator:

- Battery contactor / `connect_battery`;
- software A/B selector общей generator bus;
- автоматический запрет второго RUNNING;
- отдельный Exercise power path;
- helper, который произвольно объявляет внешний generator managed;
- право Load Manager управлять REMOTE/choke/stop generator.

Эти ограничения следуют из реальной физической схемы и требований проекта.

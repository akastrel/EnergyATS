# Home Assistant entities и команды — Energy ATS 0.4.0

Внешний HA-контракт должен соответствовать `PHYSICAL_POWER_TOPOLOGY_RU.md` и `REQUIREMENTS_RU.md`.

A/B — стабильные аппаратные слоты, а не пользовательские названия генераторов.

## 1. Helper-ы пакета EnergyATS

Корневой `ats.yaml` создаёт:

```text
input_boolean.automatic_generator_transfer
input_boolean.generator_test_mode
```

### `input_boolean.automatic_generator_transfer`

Разрешает автоматическую outage-сессию после физического исчезновения Grid.

Ручной `start_generator` от него не зависит.

### `input_boolean.generator_test_mode`

Маркер **нового внешнего запуска**.

Если новый OFF->ON фронт генератора зафиксирован при включённом helper-е, run-context становится `TEST_RUN`. Последующее выключение helper-а уже не меняет классификацию текущего запуска.

`TEST_RUN` не останавливается автоматически только из-за возврата Grid.

Если helper недоступен в момент нового внешнего запуска, EnergyATS использует безопасный `UNKNOWN_EXTERNAL` и не считает такой запуск автоматически разрешённым к остановке.

## 2. Grid и основные контакторы

```text
binary_sensor.grid_input_ready
switch.grid_power
switch.use_generator_as_power_source
binary_sensor.house_powered_by_grid
binary_sensor.house_powered_by_generator
```

Смысл:

- `grid_input_ready` — внешняя Grid физически присутствует и пригодна;
- `grid_power` — разрешение подачи Grid в управляющей схеме;
- `use_generator_as_power_source` — положение основных контакторов: OFF = сторона Grid, ON = сторона Generator;
- `house_powered_by_grid` — наличие управляющего напряжения в сетевой ветви основных контакторов;
- `house_powered_by_generator` — наличие управляющего напряжения в генераторной ветви основных контакторов.

Важно: `house_powered_by_*` стоят **не после силовых контактов**, а на цепях управления. Поэтому это подтверждение управляющей схемы, а не независимое измерение силового напряжения на общей шине дома.

Одновременное устойчивое `ON` обоих `house_powered_by_*` недопустимо.

## 3. Генераторы

```text
binary_sensor.generator_a_is_running
binary_sensor.generator_b_is_running
switch.generator_a_remote_start
switch.generator_b_remote_start
switch.generators_emergency_stop
```

`generator_*_is_running` — физический RUNNING/наличие выходного напряжения соответствующего генератора.

`generator_*_remote_start` — состояние и команда REMOTE DKG116.

Два `RUNNING=ON` одновременно являются допустимым состоянием.

EnergyATS определяет конкретного владельца общей генераторной шины не по правилу «работает ровно один», а через `GeneratorBusTracker` и сохранённую историю аппаратного FIFO.

## 4. Имя, модель и PRIMARY

```text
sensor.generator_a_name
sensor.generator_b_name
sensor.generator_a_model
sensor.generator_b_model
select.primary_generator
```

Для текущей установки, например:

```text
A = Elemax
B = Вепрь
primary = Elemax
```

Правила:

1. имена A и B непустые и различны;
2. модели доступны как строки;
3. `select.primary_generator` совпадает с одним из имён;
4. изменение primary применяется к следующей новой managed-сессии;
5. A/B entity_id не меняются при переименовании или замене физического генератора.

## 5. Заслонка

```text
button.generator_a_choke_to_cold_start
button.generator_a_choke_to_run
button.generator_b_choke_to_cold_start
button.generator_b_choke_to_run
```

Названия описывают требуемое физическое положение заслонки.

## 6. Температура

```text
sensor.garage_temperature
```

Необязательный вход для выбора длительности прогрева. При неизвестном значении GC использует консервативную длительность.

## 7. `binary_sensor.ups_ready`

Может использоваться как диагностический признак способности UPS-линии продолжать работу от АКБ.

Он не создаёт отдельного Battery path и в текущей реализации не является исполнительным входом TPC.

## 8. Ручные команды App

Через `hassio.app_stdin`:

```text
start_generator
stop_generator
reset
```

Пример:

```yaml
action: hassio.app_stdin
data:
  app: YOUR_ENERGY_ATS_APP_ID
  input:
    command: start_generator
```

`armed: false` блокирует аппаратное исполнение команд.

## 9. Диагностический sensor

```text
sensor.energy_ats_status
```

Sensor создаётся самим App и не участвует в управляющих решениях.

Schema version 3. Основные атрибуты:

```text
source
phase
generator
generator_model
generator_slot
primary_generator
primary_generator_slot
bus_owner
bus_owner_slot
generator_a_run_context
generator_b_run_context
fallback_used
remaining_seconds
session_reason
armed
schema_version
```

Примеры `source`:

```text
grid
generator_a
generator_b
generator
ups_only
unknown
```

`generator` / `primary_generator` — человеко-читаемые имена; `*_slot` — машинные A/B.

## 10. Уведомления и Logbook

EnergyATS использует:

```text
logbook.log
script.notify_critical
```

События `info`/`warning` пишутся в журнал и Logbook; немедленное пользовательское уведомление отправляется только для `critical`.

Ошибка диагностического Logbook/уведомления не должна прерывать уже начатую аппаратную последовательность.

## 11. UI helper-ы

Производные dashboard-сенсоры, например `sensor.house_powered_by_generator_name`, не являются управляющим контрактом EnergyATS.

Их можно строить поверх `sensor.energy_ats_status` и физических entities, но они не должны использоваться как источник истины для ES/TPC/GC.

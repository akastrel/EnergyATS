# Home Assistant entities и команды — Energy ATS 0.3.13

Вся привязка Energy ATS к конкретным `entity_id` находится в
`energy_ats/app/ha_adapter.py`. Доменные автоматы не знают имён Home Assistant
entities.

## 1. Стабильные аппаратные слоты

Внутри Energy ATS генераторы обозначаются как `A` и `B`. Это стабильные
аппаратные слоты и часть машинного контракта. Они не являются именами или
моделями установленных генераторов.

Человеко-читаемая идентичность поступает из Generator Controller через Home
Assistant.

## 2. Обязательная физическая обратная связь

```text
input_boolean.automatic_generator_transfer
binary_sensor.grid_input_ready
binary_sensor.house_powered_by_grid
binary_sensor.house_powered_by_generator
binary_sensor.generator_a_is_running
binary_sensor.generator_b_is_running
switch.generator_a_remote_start
switch.generator_b_remote_start
switch.grid_power
switch.use_generator_as_power_source
switch.generators_emergency_stop
```

Назначение:

- `automatic_generator_transfer` — разрешение автоматического АВР;
- `grid_input_ready` — Grid пригодна для использования;
- `house_powered_by_*` — физическое подтверждение источника дома;
- `generator_*_is_running` — физический RUNNING соответствующего слота;
- `generator_*_remote_start` — состояние/команда REMOTE DKG116;
- `grid_power` — подключение Grid path;
- `use_generator_as_power_source` — генераторная шина;
- `generators_emergency_stop` — общий Emergency Stop.

## 3. Идентичность генераторов и primary

Energy ATS 0.3.13 обязательно читает:

```text
sensor.generator_a_name
sensor.generator_b_name
sensor.generator_a_model
sensor.generator_b_model
select.primary_generator
```

Пример текущей установки:

```text
sensor.generator_a_name   = Elemax
sensor.generator_b_name   = Вепрь
sensor.generator_a_model  = SH7600EX 6.5 / 5.6 кВт
sensor.generator_b_model  = АПБ 6-230 ВХ-БСГ 6.0 / 5.5 кВт
select.primary_generator  = Elemax
```

Правила:

1. имена A и B должны быть непустыми и различными;
2. модели должны быть доступны как строки;
3. состояние `select.primary_generator` должно в точности совпадать с одним из
   `sensor.generator_*_name`;
4. Energy ATS преобразует имя primary во внутренний `GeneratorSlot.A/B`;
5. изменение primary влияет только на выбор следующей новой управляемой
   сессии и не переключает уже работающий генератор.

Если эти entities отсутствуют или имеют `unknown/unavailable`, App не считается
готовым к аппаратным действиям.

## 4. Команды заслонки Generator Controller

```text
button.generator_a_choke_to_cold_start
button.generator_a_choke_to_run
button.generator_b_choke_to_cold_start
button.generator_b_choke_to_run
```

Названия описывают физический результат, а не электрическое направление
привода. Energy ATS не использует исторические `choke_open/choke_close`.

## 5. Внешняя температура

```text
sensor.garage_temperature
```

Преобразуется в `ambient_temperature_external`. Температура не является
источником истины для RUNNING и не участвует в силовой коммутации. При
`unknown/unavailable` Generator Controller применяет консервативный сценарий
прогрева.

## 6. Команды Energy ATS

Однократные команды передаются непосредственно в App через
`hassio.app_stdin`.

Пример:

```yaml
action: hassio.app_stdin
data:
  app: YOUR_ENERGY_ATS_APP_ID
  input:
    command: start_generator
```

Поддерживаются ровно:

```text
start_generator
stop_generator
reset
```

- `start_generator` — создать управляемую ручную сессию;
- `stop_generator` — безопасно снять генераторную нагрузку, выполнить cooldown
  и остановить управляемый генератор;
- `reset` — после осмотра выполнить ограниченную recovery-процедуру.

При `armed: false` аппаратные команды запрещены.

## 7. Диагностический sensor Energy ATS

Сам App публикует:

```text
sensor.energy_ats_status
```

Это read-only диагностика. Sensor не используется контроллерами как вход.

Основные атрибуты schema version 2:

```text
source
generator
generator_model
generator_slot
primary_generator
primary_generator_slot
phase
remaining_seconds
session_reason
armed
schema_version
```

`generator` и `generator_model` относятся к активной управляемой сессии либо
подтверждённому генераторному источнику, когда он однозначно известен.

## 8. Необязательные уведомления

```text
script.notify_warning
script.notify_critical
logbook.log
```

Ошибка диагностического уведомления или Logbook не должна прерывать уже
начатую аппаратную последовательность.

## 9. UI helper-ы

Такие сущности, как `sensor.house_powered_by_generator_name` или текстовые
label-сенсоры для dashboard, являются представлением UI и не входят в
управляющий контракт Energy ATS. Их можно строить поверх обязательных entities
без изменения алгоритма App.

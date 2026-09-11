# Установка и обновление Energy ATS 1.0.3

Этот документ описывает безопасную установку и обновление текущей версии EnergyATS.

Перед ARMED-запуском прочитайте:

- `PHYSICAL_POWER_TOPOLOGY_RU.md` — фактическая силовая схема;
- `REQUIREMENTS_RU.md` — нормативное поведение;
- `ENTITIES_RU.md` — Home Assistant contract;
- `USER_TESTS_RU.md` — физический commissioning.

## 1. Рекомендуемое исходное состояние

Для первого запуска или заметного обновления безопаснее начинать из устойчивого состояния:

```text
Grid Input Ready                 ON
House Powered by Grid           ON
House Powered by Generator      OFF
Generator A is running          OFF
Generator B is running          OFF
Generator A Remote Start        OFF
Generator B Remote Start        OFF
Use Generator as Power Source   OFF
Grid Power                      ON
Generators Emergency Stop       OFF
```

Первый запуск выполняйте с:

```yaml
armed: false
```

В DISARMED App читает состояния, восстанавливает внутреннюю модель и публикует status, но не должна выдавать реальные hardware commands.

## 2. Установка repository/App

Добавьте repository:

```text
https://github.com/akastrel/EnergyATS
```

и установите **Energy ATS** как обычный Home Assistant App.

App использует Home Assistant API:

```yaml
homeassistant_api: true
stdin: true
```

Корневой package `ats.yaml` создаёт helper-ы:

```text
input_boolean.automatic_generator_transfer
input_boolean.generator_test_mode
```

Если Home Assistant packages подключаются вручную, убедитесь, что `ats.yaml` действительно загружен.

## 3. Обязательный core HA contract

До `armed: true` должны существовать и иметь определённые состояния следующие entities.

### Grid / основные контакторы

```text
binary_sensor.grid_input_ready
binary_sensor.house_powered_by_grid
binary_sensor.house_powered_by_generator
switch.grid_power
switch.use_generator_as_power_source
```

### Генераторы

```text
binary_sensor.generator_a_is_running
binary_sensor.generator_b_is_running
switch.generator_a_remote_start
switch.generator_b_remote_start
button.generator_a_choke_to_cold_start
button.generator_a_choke_to_run
button.generator_b_choke_to_cold_start
button.generator_b_choke_to_run
```

### Safety / environment

```text
switch.generators_emergency_stop
sensor.garage_temperature
```

`garage_temperature` используется для choke/warmup. Временная недоступность температуры обрабатывается консервативно и сама по себе не является core blocker.

### Generator metadata / PRIMARY

```text
sensor.generator_a_name
sensor.generator_b_name
sensor.generator_a_model
sensor.generator_b_model
select.primary_generator
```

Имена должны быть непустыми и различаться. `select.primary_generator` должен содержать имя одного из двух генераторов.

Полный смысл entities: `ENTITIES_RU.md`.

## 4. Configuration App

Основные параметры:

```text
armed
tick_seconds
log_level
grid_failure_delay
grid_restore_stable_time
transfer_confirmation_timeout
generator_a_enabled
generator_b_enabled
```

Текущие важные defaults:

```text
armed = false
tick_seconds = 1.0
grid_failure_delay = 60 s
grid_restore_stable_time = 60 s
transfer_confirmation_timeout = 60 s
```

PRIMARY выбирается через `select.primary_generator`, а не через внутреннюю настройку A/B.

`generator_a_enabled` / `generator_b_enabled` — policy-флаги. Они позволяют исключить physical slot из новых managed sessions, не меняя entity IDs. Выбранный PRIMARY должен быть разрешён.

## 5. UPS Run

UPS Run — опциональная стратегия длительного outage. Обе функции выключены по умолчанию.

```text
delayed_generator_start_enabled = false
generator_charge_cycle_enabled = false
generator_start_soc = 40
generator_target_charge_soc = 80
generator_min_ttg_before_start = 60 min
generator_max_start_delay = 21600 s
```

Для включения необходимы soft-dependency inputs:

```text
sensor.ups_battery_charge_level_soc
sensor.ups_battery_time_remaining_minutes_ttg
binary_sensor.ups_running_on_battery
binary_sensor.ups_ready
```

Проверьте, что:

- SoC — число 0–100;
- TTG — число в **минутах**, а не форматированная строка;
- `ups_running_on_battery` реально различает discharge и charge/float;
- `ups_ready` отражает возможность продолжать работу от АКБ, включая critical state;
- sensors действительно обновляются, а не остаются бесконечно со старым значением.

При invalid/stale telemetry Delayed Start fail-safe прекращается и используется обычный generator start. Эти inputs не являются hard dependency core ATS.

Нормативные правила находятся в разделе **UPS Run** `REQUIREMENTS_RU.md`.

## 6. Scheduled Exercise

Scheduled Exercise для A/B выключен по умолчанию.

Основные настройки:

```text
family_presence_entity
generator_a_exercise_enabled
generator_a_exercise_interval_days
generator_a_exercise_start_time
generator_a_exercise_run_minutes
generator_a_exercise_presence_grace_days
# аналогично для B
```

Defaults:

```text
A: enabled=false, interval=30 дней, start=15:00, run=10 мин, grace=7 дней
B: enabled=false, interval=45 дней, start=15:00, run=10 мин, grace=14 дней
family_presence_entity=group.family
```

Presence — soft Scheduler input. `unknown/unavailable` не блокирует core ATS, но ordinary Exercise при таком состоянии откладывается.

Перед включением Exercise проверьте, что пользовательские notifications реально доставляются: forced Exercise требует заранее подтверждённого warning.

## 7. Load Manager

Load Manager выключен по умолчанию:

```text
load_management_enabled = false
```

Перед включением должны быть доступны паспортные мощности A/B:

```text
sensor.generator_a_nominal_power
sensor.generator_a_maximum_power
sensor.generator_b_nominal_power
sensor.generator_b_maximum_power
```

Текущие значения установленного оборудования в требованиях:

```text
Elemax SH7600EX:         nominal 5600 W, maximum 6500 W
Вепрь АПБ 6-230 ВХ-БСГ: nominal 5500 W, maximum 6000 W
```

Управляемые группы:

```text
G1 = switch.non_critical_loads_first_floor
G2 = switch.non_critical_loads_basement_floor
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

Для automatic decisions используется прежде всего `sensor.generator_power`; остальная meter telemetry диагностическая.

Настройки:

```text
load_measurement_stabilization_time = 10 s
load_restore_margin_percent = 15 %
nominal_overload_time = 20 s
maximum_overload_confirmation_time = 4 s
load_restore_retry_interval = 300 s
```

Meter, G1/G2 и power metadata — soft dependencies core ATS. Их отказ может перевести только Load Manager в локальный `DEGRADED`.

## 8. Первый запуск после установки/обновления

### Шаг 1 — DISARMED

```yaml
armed: false
```

Проверьте:

- A/B прочитаны с правильными именами и моделями;
- PRIMARY совпадает с `select.primary_generator`;
- Grid отображается правильно;
- при остановленных генераторах `bus_owner = none`;
- `source = grid` при штатной Grid;
- нет необъяснённого `RECOVERY_REQUIRED`;
- optional subsystems имеют ожидаемое enabled/disabled состояние.

### Шаг 2 — ARMED с optional functions OFF

Включите:

```yaml
armed: true
```

оставив UPS Run / Exercise / Load Manager выключенными, если они ещё не проверены.

Сначала пройдите core commissioning из `USER_TESTS_RU.md`: M1–M7, затем переходные/топологические проверки R1–R4 и электрический acceptance P1. Не повторяйте симметричные A/B software-сценарии только ради перестановки ролей.

### Шаг 3 — включайте функции отдельно

Не включайте одновременно все новые возможности перед первым физическим тестом.

Рекомендуемый порядок:

1. core ATS;
2. UPS Run;
3. Scheduled Exercise;
4. Load Manager.

После включения каждой функции выполните только её физически значимые commissioning-тесты из `USER_TESTS_RU.md`.

## 9. Persistent state и обновление старых версий

Текущий top-level persistent format:

```text
STATE_SCHEMA_VERSION = 3
```

Schema 3 хранит как минимум:

- Supervisor/session state;
- GeneratorBusTracker;
- Scheduled Exercise;
- UPS Run (`ups_run`);
- Load Manager ownership/state;
- `pending_actions` для незавершённых core hardware commands.

EnergyATS **не обещает автоматическую миграцию несовместимых старых schema**. Если persisted state имеет неподдерживаемый format, App не должна угадывать старое состояние; используется безопасный Recovery path.

Особенно важно:

- незавершённая core hardware command после restart не повторяется вслепую;
- устойчивую однозначную managed session можно восстановить без duplicate start/transfer;
- Load Manager measurement samples не persist-ятся — после restart нужен новый stabilization;
- corruption optional Load Manager / UPS Run state локализуется и не должна сама превращать core ATS в неисправимую систему.

Перед крупным обновлением полезно убедиться, что система находится в штатном Grid state и оба генератора остановлены.

## 10. Status sensor

После запуска должен появиться:

```text
sensor.energy_ats_status
```

Проверьте как минимум:

```text
source
phase
generator
managed_generator
bus_owner
primary_generator
session_reason
fallback_used
remaining_seconds
armed
```

Если включены дополнительные функции, status также содержит Exercise, UPS Run и Load Manager attributes.

Точный contract: `ENTITIES_RU.md`.

## 11. Recovery

Команда:

```text
reset
```

запускает управляемое восстановление.

В 1.0.3 системное решение Recovery принадлежит `EnergySupervisor`. Он определяет:

- нужен ли reset;
- блокирует ли E-stop, unknown required state, TPC inconsistency или внешний RUNNING;
- порядок `Grid path -> owned generator shutdown -> complete`;
- какие generators EnergyATS имеет право остановить.

`main.py` и TPC/GC только исполняют выбранные безопасные шаги.

Не пытайтесь использовать reset как обход внешнего RUNNING или аппаратной проблемы feedback.

## 12. Сетевые публикации и reconnect

Обычные status/Logbook/user notifications выполняются best-effort и не должны блокировать control tick на сетевой timeout.

При потере связи с Home Assistant незавершённые background publications отменяются перед reconnect.

Аппаратные service calls остаются подтверждаемыми и journaled. Warning перед forced Scheduled Exercise также остаётся синхронным, потому что успешная доставка является safety prerequisite.

Потеря HA во время transient physical operation может привести к `RECOVERY_REQUIRED`, чтобы после reconnect не продолжать силовой переход вслепую.

## 13. Production verification

CI текущей версии выполняет два независимых блока:

1. полный Python test suite — для 1.0.3 **288 passed**;
2. `addon-container-smoke` — реальная сборка add-on Docker image и проверка production `HomeAssistantClient` через локальный test WebSocket/REST endpoint внутри container.

Это проверяет packaging/runtime, но не реальные контакторы, генераторы, DKG116, MAP и проводку.

## 14. Физический commissioning

`USER_TESTS_RU.md` больше не является каталогом всех software-комбинаций. Он содержит только сценарии, которые доказывают уникальный физический риск реальной установки.

Для базового ATS перед unattended эксплуатацией:

- core ATS: M1–M7;
- transition/topology: R1–R4;
- electrical acceptance: P1;
- R5 — если нужен полностью подтверждённый на реальной feedback-схеме fallback и есть безопасный способ вызвать отказ PRIMARY;
- P2 — перед длительной unattended эксплуатацией каждого generator.

Для optional features:

- UPS Run: U1–U2;
- Scheduled Exercise: X1–X3;
- Load Manager: L1–L3; L4 optional, если meter можно безопасно сделать недоступным.

Симметричные A/B permutations, двойной отказ ради проверки ping-pong и искусственное отсутствие feedback без безопасного штатного способа остаются automated/HIL tests, а не пользовательским commissioning.

Не обходите аппаратные блокировки и не вмешивайтесь вручную в силовые контакторы ради теста.

## 15. Критерий готовности

Система подготовлена к эксплуатации, когда одновременно:

- physical topology соответствует `PHYSICAL_POWER_TOPOLOGY_RU.md`;
- HA contract соответствует `ENTITIES_RU.md`;
- App стартует без unknown обязательных core states;
- PRIMARY/metadata корректны;
- CI текущей версии зелёный;
- необходимые physical commissioning tests пройдены без необъяснённых отклонений.
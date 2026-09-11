# Установка и обновление Energy ATS 0.7.0

Перед установкой или commissioning прочитать:

- `PHYSICAL_POWER_TOPOLOGY_RU.md`;
- `REQUIREMENTS_RU.md`;
- `ENTITIES_RU.md`;
- `USER_TESTS_RU.md`.

Версия 0.6 сохраняет базовую силовую модель 0.4 и Scheduled Exercise 0.5, добавляя отдельный Load Manager для G1/G2. Физическая топология Grid/Generator/UPS и правила A/B bus owner не меняются.

## 1. Рекомендуемое исходное состояние

Для первого запуска/обновления безопаснее начинать из устойчивого состояния:

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

Перед физическими испытаниями App можно сначала запустить с `armed: false`.

Load Manager по умолчанию выключен (`load_management_enabled: false`). Поэтому обновить EnergyATS до 0.6 можно до установки его дополнительных meter/load entities: они являются soft dependencies и не блокируют core ATS.

## 2. Установка repository/App

Добавить repository EnergyATS в Home Assistant App store и установить Energy ATS обычным способом.

App требует доступ к Home Assistant API (`homeassistant_api: true` в `config.yaml`).

Корневой package `ats.yaml` создаёт:

```text
input_boolean.automatic_generator_transfer
input_boolean.generator_test_mode
```

Если package-файлы подключаются вручную, убедиться, что `ats.yaml` реально загружается Home Assistant.

## 3. Обязательный HA-контракт core ATS

До ARMED-запуска должны существовать и иметь определённые состояния обязательные core entities.

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

### Метаданные core / PRIMARY

```text
sensor.generator_a_name
sensor.generator_b_name
sensor.generator_a_model
sensor.generator_b_model
select.primary_generator
```

### Safety / temperature

```text
switch.generators_emergency_stop
sensor.garage_temperature
```

`sensor.garage_temperature` используется консервативно для choke/warmup и не является причиной блокировки core ATS при временной недоступности.

Полный смысл entities приведён в `ENTITIES_RU.md`.

## 4. Дополнительный контракт Load Manager

Перед включением `load_management_enabled: true` Generator Controller должен публиковать отдельные паспортные мощности:

```text
sensor.generator_a_nominal_power
sensor.generator_a_maximum_power
sensor.generator_b_nominal_power
sensor.generator_b_maximum_power
```

Для текущего оборудования ожидаются:

```text
Elemax SH7600EX:             nominal 5600 W, maximum 6500 W
Вепрь АПБ 6-230 ВХ-БСГ:     nominal 5500 W, maximum 6000 W
```

Параметры должны быть числовыми и удовлетворять `0 < nominal <= maximum`.

Управляемые группы:

```text
G1 = switch.non_critical_loads_first_floor
G2 = switch.non_critical_loads_basement_floor
```

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

Для решений Load Manager используется прежде всего `sensor.generator_power`; остальная телеметрия диагностическая.

Все перечисленные в этом разделе entities являются soft dependencies относительно core ATS. Если они отсутствуют или сломаны, Load Manager может перейти в локальный `DEGRADED`, но App/GC/TPC/Supervisor не должны из-за этого переходить в системный `RECOVERY_REQUIRED`.

## 5. Configuration

Основные параметры:

```text
armed
tick_seconds
log_level
grid_failure_delay
grid_restore_stable_time
generator_a_enabled
generator_b_enabled
transfer_confirmation_timeout
```

PRIMARY выбирается через `select.primary_generator` в Home Assistant, а не через внутренний A/B параметр App.

`generator_a_enabled` / `generator_b_enabled` — policy-флаги: физически установленный генератор можно временно исключить из managed-сценариев, не меняя entity_id. Если выбранный PRIMARY запрещён policy-флагом, конфигурация считается ошибочной.

### Load Manager

```text
load_management_enabled
load_measurement_stabilization_time
load_restore_margin_percent
nominal_overload_time
maximum_overload_confirmation_time
load_restore_retry_interval
```

Defaults:

```text
load_management_enabled = false
load_measurement_stabilization_time = 10 s
load_restore_margin_percent = 15 %
nominal_overload_time = 20 s
maximum_overload_confirmation_time = 4 s
load_restore_retry_interval = 300 s
```

При первом обновлении рекомендуется оставить `load_management_enabled=false`, проверить новые entities и status, затем включать функцию отдельно.

### Delayed Start / Charge Cycling

При обновлении обе функции остаются выключенными. Для включения используйте [отдельное руководство](DELAYED_START_RU.md): оно описывает все шесть параметров, необходимые батарейные сигналы и проверку их достоверности. В HA configuration доступны русские и английские названия и пояснения.

### Scheduled Exercise

Scheduled Exercise остаётся отдельно конфигурируемым для A/B; presence задаётся `family_presence_entity`.

## 6. Первый запуск после обновления

### DISARMED

Рекомендуемый первый запуск:

```yaml
armed: false
```

Проверить в log/status:

- оба генератора прочитаны с правильными именами/моделями;
- PRIMARY совпадает с `select.primary_generator`;
- Grid отображается корректно;
- bus owner при остановленных генераторах = `none`;
- нет `RECOVERY_REQUIRED`;
- при наличии новых Generator Controller sensors правильно видны Nominal/Maximum Power;
- Load Manager при default configuration показывает `disabled`.

В DISARMED App наблюдает и публикует status, но не должен выдавать реальные аппаратные команды.

### ARMED без Load Manager

После проверки:

```yaml
armed: true
load_management_enabled: false
```

Core ATS должен вести себя как до 0.6. Дополнительные meter/G1/G2/power entities не могут мешать запуску и transfer.

### ARMED с Load Manager

До включения проверить:

1. G1/G2 в Home Assistant действительно управляют только задуманными некритичными группами;
2. `generator_meter_status` отражает доступность SDM120;
3. `generator_power` измеряет общую generator bus;
4. Nominal/Maximum относятся к правильному физическому A/B;
5. состояния всех этих entities не `unknown/unavailable`.

После этого включить:

```yaml
load_management_enabled: true
```

И выполнить Load Manager-сценарии из `USER_TESTS_RU.md`.

## 7. Persistent state и обновление старых версий

Journal 0.3.x несовместим с моделью 0.4 и намеренно не мигрируется.

Обновления 0.4 -> 0.5 -> 0.6 -> 0.7 сохраняют `schema_version = 2`. Старый совместимый journal без `exercise_scheduler`, `load_manager` или `outage_power_policy` получает свежий state соответствующего policy-компонента. Старые сессии без `cycle_owned` остаются вне cycling ownership. Устойчивое ожидание и cycle-owned RUNNING восстанавливаются; restart во время незавершённого силового перехода/остановки требует Recovery по обычным правилам.

Load Manager сохраняет собственное `shed_by_energy_ats` ownership, pending consumer action и retry state. Measurement samples не восстанавливаются: после restart power-based решение должно быть доказано новым stabilization window.

Повреждение Load Manager state локализуется и не должно само по себе отправлять core ATS в recovery. В сомнительном случае ownership теряется консервативно: уже-OFF группа не включается автоматически без доказанного собственного OFF.

## 8. Проверка status sensor

После запуска должен появиться:

```text
sensor.energy_ats_status
```

Базовые атрибуты:

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
armed
```

Load Manager добавляет:

```text
load_management_enabled
load_manager_phase
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

Run-context значения:

```text
none
outage_related
test_run
other
unknown
```

## 9. Что проверить физически после 0.6

Если Load Manager остаётся выключенным, достаточно базовых M-сценариев `USER_TESTS_RU.md` для core ATS и тех сценариев, которые затрагивались обновлением.

Перед эксплуатацией с включённым Load Manager дополнительно необходимо физически проверить:

- pre-transfer отключение G1/G2 до подключения generator bus;
- последовательный admission G1 -> G2 при достаточном запасе;
- отсутствие automatic ON для группы, которая была OFF пользователем;
- непрерывный overload control во время уже установившегося питания от generator;
- локальный `DEGRADED` при отказе generator meter без системного recovery;
- возврат Grid раньше восстановления shed groups;
- восстановление только собственного `shed_by_energy_ats`;
- смену active nominal/maximum при аппаратном A/B takeover.

Подробные пользовательские шаги приведены в `USER_TESTS_RU.md`.

## 10. Recovery

Команда:

```text
reset
```

не является «стереть ошибку». Она запускает безопасное восстановление core ATS:

1. проверяет обязательные физические состояния и E-stop;
2. возвращает основные контакторы к Grid path;
3. не захватывает внешний генератор;
4. при наличии управляемого двигателя снимает нагрузку до остановки;
5. локальные GC faults сбрасываются только для физически остановленного двигателя (`RUNNING=OFF`, `REMOTE=OFF`).

Load Manager `DEGRADED` сам по себе не является причиной core recovery. Meter/G1/G2 fault устраняется восстановлением соответствующей soft dependency; power-based control после этого начинает новое measurement window.

## 11. Критерий готовности

Установка считается подготовленной к эксплуатации, когда одновременно выполнены:

- HA entities соответствуют `ENTITIES_RU.md`;
- physical topology соответствует `PHYSICAL_POWER_TOPOLOGY_RU.md`;
- App стартует без неоднозначных обязательных core состояний;
- CI текущей версии зелёный;
- базовые commissioning-сценарии подтверждены;
- если Load Manager включён — его отдельные физические сценарии также пройдены без необъяснённых отклонений.
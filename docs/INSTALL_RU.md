# Установка и обновление Energy ATS 0.4.0

0.4 — полная переработка внутренней модели под фактическую электрическую схему. Persistent state 0.3.x намеренно не мигрируется.

Перед установкой или commissioning прочитать:

- `PHYSICAL_POWER_TOPOLOGY_RU.md`;
- `REQUIREMENTS_RU.md`;
- `ENTITIES_RU.md`.

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

## 2. Установка repository/App

Добавить repository EnergyATS в Home Assistant Add-on/App store и установить Energy ATS обычным способом.

App требует доступ к Home Assistant API (`homeassistant_api: true` в `config.yaml` App).

Корневой package `ats.yaml` создаёт два helper-а:

```text
input_boolean.automatic_generator_transfer
input_boolean.generator_test_mode
```

Если package-файлы подключаются вручную, убедиться, что `ats.yaml` реально загружается Home Assistant.

## 3. Обязательный HA-контракт

До ARMED-запуска должны существовать и иметь определённые состояния следующие entities.

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

### Метаданные

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

Полный смысл entities приведён в `ENTITIES_RU.md`.

## 4. Configuration

Основные параметры 0.4:

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

`generator_a_enabled` / `generator_b_enabled` — policy-флаги: физически установленный генератор можно временно исключить из managed-сценариев, не меняя entity_id.

Если выбранный PRIMARY запрещён policy-флагом, конфигурация считается ошибочной.

## 5. Первый запуск

### DISARMED

Рекомендуемый первый запуск:

```yaml
armed: false
```

Проверить в log:

- оба генератора прочитаны с правильными именами/моделями;
- PRIMARY совпадает с `select.primary_generator`;
- Grid отображается корректно;
- bus owner при остановленных генераторах = `none`;
- нет `RECOVERY_REQUIRED`.

В DISARMED App наблюдает и публикует status, но не должен выдавать реальные аппаратные команды.

### ARMED

После проверки:

```yaml
armed: true
```

После подключения App ждёт готовности обязательных entities. `unknown` / `unavailable` не заменяются значениями по умолчанию.

## 6. Обновление с 0.3.x

Journal 0.3.x несовместим с 0.4, потому что старая модель содержала неверные для этой установки абстракции, включая Battery path.

0.4 не пытается интерпретировать старый persistent state как новый. Неподдерживаемый journal приводит к безопасной блокировке/recovery вместо угадывания.

Если после обновления App сообщает о неподдерживаемом сохранённом состоянии, выполнить контролируемый `reset` только после проверки реального положения схемы.

Не подменять recovery ручным редактированием JSON без понимания физического состояния контакторов и генераторов.

## 7. Проверка status sensor

После запуска должен появиться:

```text
sensor.energy_ats_status
```

Текущие ключевые атрибуты:

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

В 0.4 нет status-атрибутов `schema_version`, `bus_owner_slot` и `primary_generator_slot`.

Run-context значения текущей модели:

```text
none
outage_related
test_run
other
unknown
```

## 8. Минимальный commissioning после обновления

После зелёного CI программная модель всё равно должна быть проверена на реальном железе.

Рекомендуемый порядок:

1. **Grid -> PRIMARY**: пропадание Grid, запуск PRIMARY, прогрев, break-before-make, питание дома от generator bus.
2. **Dual RUNNING / FIFO**: при owner A запустить B; owner остаётся A, аварии interlock нет.
3. **Automatic handoff**: при A+B RUNNING остановить owner A; аппаратный owner должен перейти B без команды ATS на выбор B.
4. **Managed fallback**: PRIMARY не запускается/отказывает, SECONDARY свободен — ровно один fallback.
5. **Stable Grid return**: дом сначала возвращается на Grid, затем останавливаются outage-related генераторы.
6. **TEST_RUN**: тестовый генератор переживает возврат Grid и не останавливается правилом завершения outage.

Для каждого шага сравнивать:

- физическое положение/поведение контакторов;
- `house_powered_by_*` как control-circuit feedback;
- `generator_*_is_running`;
- `sensor.energy_ats_status`;
- runtime log.

## 9. Recovery

Команда:

```text
reset
```

не является «стереть ошибку». Она запускает безопасное восстановление:

1. проверяет обязательные физические состояния и E-stop;
2. возвращает основные контакторы к Grid path;
3. не захватывает внешний генератор;
4. при наличии управляемого двигателя снимает нагрузку до остановки;
5. локальные GC faults сбрасываются только для физически остановленного двигателя (`RUNNING=OFF`, `REMOTE=OFF`).

Если доказать безопасное состояние нельзя, recovery должен остаться заблокированным.

## 10. Критерий готовности

Установка 0.4 считается подготовленной к эксплуатации, когда одновременно выполнены:

- HA entities соответствуют `ENTITIES_RU.md`;
- physical topology соответствует `PHYSICAL_POWER_TOPOLOGY_RU.md`;
- App стартует без неоднозначных обязательных состояний;
- CI текущей версии зелёный;
- commissioning-сценарии на реальном оборудовании подтверждены.
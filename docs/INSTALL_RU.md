# Установка и обновление Energy ATS 0.4.0

0.4 — принципиальная переработка доменной модели под фактическую электрическую схему. Это не совместимый внутренний апгрейд 0.3.x.

Перед обновлением прочитать:

- `PHYSICAL_POWER_TOPOLOGY_RU.md`;
- `REQUIREMENTS_RU.md`.

## 1. Безопасное исходное состояние

Для обновления рекомендуется:

```text
Grid Input Ready                 ON
House Powered by Grid           ON
House Powered by Generator      OFF
Generator A/B is running        OFF
Generator A/B Remote Start      OFF
Use Generator as Power Source   OFF
Grid Power                      ON
Generators Emergency Stop       OFF
armed                           false
```

Не обновлять App во время запуска, силового переключения, cooldown или recovery.

## 2. Подготовить Generator Controller

В Home Assistant должны существовать:

```text
binary_sensor.generator_a_is_running
binary_sensor.generator_b_is_running
switch.generator_a_remote_start
switch.generator_b_remote_start
button.generator_a_choke_to_cold_start
button.generator_a_choke_to_run
button.generator_b_choke_to_cold_start
button.generator_b_choke_to_run
sensor.generator_a_name
sensor.generator_b_name
sensor.generator_a_model
sensor.generator_b_model
select.primary_generator
```

A/B — стабильные аппаратные слоты.

`select.primary_generator` должен совпадать с именем одного из генераторов.

## 3. Обновить `ats.yaml`

Скопировать актуальный корневой `ats.yaml` в Home Assistant packages.

Он создаёт два helper-а:

```text
input_boolean.automatic_generator_transfer
input_boolean.generator_test_mode
```

Первый разрешает автоматический АВР. Второй классифицирует **новый внешний запуск** как `TEST_RUN`.

После изменения package перечитать/перезапустить конфигурацию HA штатным способом.

## 4. Проверить основные entities

Обязательный силовой контракт:

```text
binary_sensor.grid_input_ready
binary_sensor.house_powered_by_grid
binary_sensor.house_powered_by_generator
switch.grid_power
switch.use_generator_as_power_source
switch.generators_emergency_stop
```

`house_powered_by_grid` и `house_powered_by_generator` должны пониматься как датчики **управляющих цепей основных контакторов**, не как независимое измерение напряжения после силовых контактов.

## 5. Установить/обновить App

Repository:

```text
https://github.com/akastrel/EnergyATS
```

Версия App:

```text
0.4.0
```

Configuration:

```yaml
armed: false
tick_seconds: 1.0
log_level: info
grid_failure_delay: 5
grid_restore_stable_time: 60
transfer_confirmation_timeout: 60
generator_a_enabled: true
generator_b_enabled: true
```

Имя, модель и primary не дублируются в Configuration App.

## 6. Важно при обновлении с 0.3.x

Persistent journal 0.3 **не мигрируется** в модель 0.4.

Файл App:

```text
/data/energy-supervisor-state.json
```

0.4 использует новый envelope journal и сохраняет не только Supervisor, но и owner/run-context генераторной шины.

Если после обновления старый journal приводит к `RECOVERY_REQUIRED`, это ожидаемая защитная реакция. Не пытаться обходить её изменением файла вслепую.

Правильная процедура:

1. убедиться физически, что Grid и контакторы находятся в безопасном устойчивом состоянии;
2. первый запуск выполнить с `armed: false` и проверить входные states/status;
3. затем разрешить `armed: true`;
4. выполнить `reset` через `hassio.app_stdin`;
5. убедиться, что `RECOVERY_REQUIRED` снят и силовая схема подтверждена.

## 7. Первый запуск 0.4 только DISARMED

Оставить:

```yaml
armed: false
```

Проверить в журнале:

- Generator A/B имеют правильные имя и модель;
- текущий PRIMARY определён правильно;
- отсутствуют unknown/unavailable у обязательных силовых states;
- `sensor.energy_ats_status` публикуется;
- Grid/Generator status соответствует фактической схеме.

DISARMED разрешает наблюдение и диагностику, но блокирует switch/button service calls.

## 8. Проверить status schema 3

`sensor.energy_ats_status` должен содержать, в частности:

```text
source
phase
primary_generator
primary_generator_slot
bus_owner
bus_owner_slot
generator_a_run_context
generator_b_run_context
fallback_used
schema_version = 3
```

При нормальной Grid ожидается пользовательский state:

```text
Питание от основной сети
```

При намеренно отключённой основной части дома и работающем МАП:

```text
В доме работает только UPS линия
```

## 9. Переход в ARMED

После проверки DISARMED установить:

```yaml
armed: true
```

Автоматический АВР для первых физических испытаний лучше оставить:

```text
input_boolean.automatic_generator_transfer = OFF
```

## 10. Базовый ручной тест

1. Grid доступна, оба генератора остановлены.
2. `start_generator`.
3. Проверить запуск текущего PRIMARY.
4. Проверить RUNNING, choke и прогрев.
5. Проверить break-before-make: Grid снимается до выбора генераторной ветви.
6. Проверить `bus_owner`.
7. `stop_generator`.
8. Проверить снятие генераторной ветви до cooldown/REMOTE OFF.

## 11. Ключевые физические испытания 0.4

После базового теста отдельно проверить:

1. A запускается первым, затем B запускается внешне: оба RUNNING, `bus_owner` остаётся A.
2. При A+B RUNNING остановить A: аппаратный owner автоматически переходит B, EnergyATS B не захватывает.
3. При outage дать PRIMARY не запуститься: EnergyATS делает ровно один fallback на SECONDARY.
4. При outage запустить второй генератор внешне; после стабильного возврата Grid дом возвращается на Grid, затем останавливаются все outage-related runs.
5. Повторить внешний запуск с `input_boolean.generator_test_mode = ON`: после возврата Grid TEST_RUN продолжает работать.

Только после этих проверок включать автоматический АВР для постоянной эксплуатации.

## 12. TEST_RUN

`generator_test_mode` читается в момент нового OFF->ON фронта внешнего генератора.

Правильная последовательность тестового запуска:

```text
generator_test_mode = ON
-> запустить генератор внешним способом
-> убедиться, что run_context = test_run
-> generator_test_mode можно вернуть OFF
```

Классификация текущего запуска сохраняется до его остановки и переживает restart через persistent journal.

## 13. PRIMARY и fallback

`select.primary_generator` влияет на следующую новую managed-сессию.

При отказе PRIMARY EnergyATS может один раз перейти на SECONDARY. Если fallback SECONDARY также отказал, автоматического возврата к PRIMARY нет: требуется recovery/решение пользователя.

Если SECONDARY уже работает внешне, EnergyATS не переводит его в managed ownership; физический takeover общей шины отслеживается отдельно.

## 14. Ручные команды

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

Фактический App ID рекомендуется выбирать через визуальный редактор Home Assistant.

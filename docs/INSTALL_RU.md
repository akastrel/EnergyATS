# Обновление до Energy ATS 0.3.13

Версия 0.3.13 сохраняет силовую логику предыдущего релиза, но меняет источник
конфигурации генераторов. Имя, модель и выбор основного генератора теперь
приходят из Generator Controller через Home Assistant. Energy ATS больше не
хранит эти значения в собственной Configuration.

Обновление выполнять при доступной основной сети, остановленных генераторах и
`armed: false`.

## 1. Исходное безопасное состояние

Перед обновлением проверить:

```text
Grid Input Ready                 ON
House Powered by Grid           ON
House Powered by Generator      OFF
Generator A/B is running        OFF
Generator A/B Remote Start      OFF
Use Generator as Power Source   OFF
Grid Power                      ON
Generators Emergency Stop       OFF
```

Не обновлять приложение посередине запуска, силового переключения, cooldown или
recovery.

## 2. Подготовить Generator Controller и Home Assistant

До запуска Energy ATS 0.3.13 в Home Assistant должны существовать следующие
сущности Generator Controller:

```text
sensor.generator_a_name
sensor.generator_b_name
sensor.generator_a_model
sensor.generator_b_model
select.primary_generator
```

Для текущей установки ожидается, например:

```text
sensor.generator_a_name   = Elemax
sensor.generator_b_name   = Вепрь
sensor.generator_a_model  = SH7600EX 6.5 / 5.6 кВт
sensor.generator_b_model  = АПБ 6-230 ВХ-БСГ 6.0 / 5.5 кВт
select.primary_generator  = Elemax
```

Значение `select.primary_generator` должно в точности совпадать с состоянием
одного из `sensor.generator_*_name`.

Также должны существовать стабильные аппаратные команды и обратные связи:

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

A/B здесь являются стабильными аппаратными слотами. Переименование физического
генератора не должно менять эти `entity_id`.

## 3. Обновить `ats.yaml`

Скопировать актуальный корневой `ats.yaml` в каталог packages Home Assistant.
Он создаёт только собственный state-helper ATS:

```text
input_boolean.automatic_generator_transfer
```

Остальные сущности в файле перечислены как внешний контракт и должны уже
существовать в Home Assistant.

После изменения package перечитать конфигурацию Home Assistant обычным штатным
способом.

## 4. Обновить Home Assistant App

Repository:

```text
https://github.com/akastrel/EnergyATS
```

После обновления списка Apps установить Energy ATS **0.3.13**.

В Configuration App больше нет `primary_generator`, `generator_a_name`,
`generator_b_name`, моделей и параметров конкретного двигателя. Компактная
конфигурация выглядит так:

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

`generator_a_enabled` и `generator_b_enabled` остаются политикой Energy ATS:
они разрешают или запрещают Supervisor использовать соответствующий физический
слот. Они не описывают физическую идентичность генератора и поэтому не
переносятся в Generator Controller.

Если Home Assistant сохранил старые значения Configuration, открыть страницу
Configuration Energy ATS и сохранить предложенную текущую схему без
`primary_generator`.

## 5. Первый запуск только DISARMED

Оставить:

```yaml
armed: false
```

Запустить App. В этом режиме switch/button service calls запрещены, но App
подключается к Home Assistant и проверяет обязательные данные.

В логах должны появиться строки вида:

```text
Generator A: Elemax; модель: SH7600EX ...; PRIMARY; ...
Generator B: Вепрь; модель: АПБ ...; SECONDARY; ...
```

Также проверить отсутствие сообщения `Ожидаем обязательные сущности Home
Assistant`.

Если имя, модель или `select.primary_generator` имеют `unknown/unavailable`,
Energy ATS не переходит в состояние готовности к аппаратным командам.

## 6. Проверить `sensor.energy_ats_status`

Energy ATS публикует диагностический:

```text
sensor.energy_ats_status
```

В версии 0.3.13 его `schema_version = 2`. Помимо прежних полей он публикует:

```text
generator_model
primary_generator
primary_generator_slot
```

Например при основном Elemax:

```text
primary_generator      = Elemax
primary_generator_slot = A
```

Sensor предназначен только для диагностики/UI и не используется как вход
управляющей логики.

## 7. Переход в ARMED

`armed: true` включать только после проверки DISARMED-режима.

Минимальный ручной тест:

1. Grid доступна, оба генератора остановлены.
2. Выполнить `start_generator` через `hassio.app_stdin`.
3. Проверить выбор именно текущего `select.primary_generator`.
4. Проверить заслонку, REMOTE, RUNNING и прогрев.
5. Убедиться, что Grid отключается до выбора генераторной шины.
6. Выполнить `stop_generator`.
7. Убедиться, что нагрузка снята до cooldown и снятия REMOTE.

Автоматический АВР на первом тесте рекомендуется оставить выключенным:

```text
input_boolean.automatic_generator_transfer = OFF
```

Ручные команды от состояния этого helper не зависят.

## 8. Изменение primary во время эксплуатации

`select.primary_generator` можно менять в Home Assistant без изменения
Configuration Energy ATS.

Новое значение влияет на **следующую новую управляемую сессию**. Уже начатая
сессия хранит свой аппаратный слот A/B и не должна переключать работающий
генератор только из-за изменения select.

Если выбранный primary запрещён соответствующим `generator_*_enabled`, Energy
ATS считает конфигурацию некорректной и не начинает новую аппаратную операцию.

## 9. Recovery Required

При потере связи или restart посередине физической транзакции App не продолжает
старую последовательность вслепую. После восстановления связи:

1. осмотреть фактическое состояние оборудования;
2. снять Emergency Stop, если он активен;
3. выполнить команду `reset`.

При однозначной физической обратной связи recovery снимает генераторную шину,
возвращает Grid path и останавливает только разгруженный генератор прерванной
управляемой сессии. Внешний генератор App не захватывает и не останавливает.

## 10. Ручные команды Home Assistant

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

Фактический `app` ID лучше выбирать через визуальный редактор Home Assistant.
Для этих команд отдельные `input_button` не нужны.

## 11. Persistent journal

Energy ATS использует:

```text
/data/energy-supervisor-state.json
```

Файл принадлежит App и не копируется в HA packages. Он хранит управляемую
сессию, транзакцию и pending hardware actions. Имена и модели генераторов в
нём не являются источником конфигурации: после подключения текущая идентичность
повторно читается из Home Assistant.

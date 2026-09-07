# Energy ATS 0.3.13

Energy ATS — Home Assistant App с Energy Supervisor, безопасным Power Transfer
и двумя независимыми Generator Controller в одном Python-процессе.

## Перед обновлением

1. Обеспечьте питание от Grid.
2. Остановите оба генератора и снимите REMOTE.
3. Установите `armed: false`.
4. Обновите Generator Controller и убедитесь, что в HA доступны metadata:
   `sensor.generator_a_name`, `sensor.generator_b_name`,
   `sensor.generator_a_model`, `sensor.generator_b_model` и
   `select.primary_generator`.
5. Обновите корневой `ats.yaml`.
6. Затем обновите и запустите Energy ATS 0.3.13.

## Конфигурация генераторов

A/B остаются стабильными аппаратными слотами. Человеко-читаемые имя и модель
больше не хранятся в Energy ATS и читаются из Home Assistant.

`select.primary_generator` также находится в HA и должен совпадать с именем A
или B. Его изменение применяется к следующей новой сессии и не переключает уже
работающий генератор.

В Configuration Energy ATS остаются:

- `generator_a_enabled`;
- `generator_b_enabled`.

Это policy-флаги Supervisor: физически установленный генератор можно временно
запретить для управляемых сессий без изменения конфигурации Generator
Controller.

## ARMED и автоматический АВР

`armed` — нижний предохранитель всего App:

- `false` — аппаратные switch/button calls запрещены;
- `true` — контроллерам разрешено выполнять подтверждаемые операции.

Разрешение автоматического АВР хранится в:

```text
input_boolean.automatic_generator_transfer
```

из корневого `ats.yaml`.

Автоматический fallback после фактического отказа выбранного генератора в
0.3.13 по-прежнему отключён.

## Ручные команды

Поддерживаются ровно:

- `start_generator`;
- `stop_generator`;
- `reset`.

Пример:

```yaml
action: hassio.app_stdin
data:
  app: YOUR_ENERGY_ATS_APP_ID
  input:
    command: start_generator
```

Фактический App ID рекомендуется выбирать через визуальный редактор Home
Assistant.

## Диагностический статус

App публикует:

```text
sensor.energy_ats_status
```

В 0.3.13 используется `schema_version: 2`. Основные атрибуты:

```text
source
phase
generator
generator_model
generator_slot
primary_generator
primary_generator_slot
remaining_seconds
session_reason
armed
```

Sensor предназначен только для UI/диагностики и не участвует в управляющих
решениях.

## Потеря связи и recovery

Если WebSocket или процесс потерян во время незавершённой физической
транзакции, автоматическое продолжение блокируется и App переходит в
`RECOVERY_REQUIRED`. Точный список pending-команд хранится в persistent journal
`/data/energy-supervisor-state.json`.

После осмотра команда `reset` выполняет ограниченное безопасное восстановление.
Внешний генератор App не захватывает и автоматически не останавливает.

Подробности:

- `docs/ARCHITECTURE_RU.md`;
- `docs/ENTITIES_RU.md`;
- `docs/INSTALL_RU.md`;
- `docs/REQUIREMENTS_RU.md`.

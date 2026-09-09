# Energy ATS 0.4.0

Energy ATS — Home Assistant App для безопасного управления резервным электроснабжением дома с двумя генераторами.

## Что изменилось в 0.4

- модель приведена к реальной физической топологии;
- удалён виртуальный `Battery path`, используется `UPS_ONLY`;
- два генератора могут штатно быть RUNNING одновременно;
- добавлен persistent `GeneratorBusTracker` с аппаратным FIFO owner;
- run-context упрощён до `OUTAGE_RELATED`, `TEST_RUN`, `OTHER`, `UNKNOWN`;
- реализован один fallback `PRIMARY -> SECONDARY` без ping-pong;
- внешний SECONDARY не захватывается в managed ownership;
- outage-related генераторы останавливаются только после безопасного возврата дома на стабильную Grid;
- `TEST_RUN` этим правилом не останавливается.

## Перед обновлением

1. Обеспечить Grid.
2. Остановить оба генератора.
3. Установить `armed: false`.
4. Обновить Generator Controller metadata/entities.
5. Обновить корневой `ats.yaml`.
6. Обновить App до 0.4.0.

Persistent journal 0.3 не мигрируется. Если после обновления получен `RECOVERY_REQUIRED`, сначала проверить физическую схему, затем выполнить безопасный `reset` по `docs/INSTALL_RU.md`.

## Helper-ы

```text
input_boolean.automatic_generator_transfer
input_boolean.generator_test_mode
```

`generator_test_mode` классифицирует новый фронт RUNNING как `TEST_RUN`. Helper сам не запускает и не останавливает двигатель.

## Конфигурация

В App остаются policy/тайминги:

- `armed`;
- `tick_seconds`;
- `log_level`;
- `grid_failure_delay`;
- `grid_restore_stable_time`;
- `transfer_confirmation_timeout`;
- `generator_a_enabled`;
- `generator_b_enabled`.

Имя, модель и PRIMARY читаются из Home Assistant.

## Ручные команды

```text
start_generator
stop_generator
reset
```

## Диагностика

App публикует:

```text
sensor.energy_ats_status
```

Ключевые attributes: фактический `source`, Supervisor `phase`, текущий generator/bus owner, managed generator, run-context A/B, PRIMARY, `fallback_used`, `remaining_seconds` и `armed`.

Status sensor не имеет отдельного version/schema attribute и не используется как управляющий вход.

## Документация

- `docs/PHYSICAL_POWER_TOPOLOGY_RU.md` — физическая схема;
- `docs/REQUIREMENTS_RU.md` — нормативное поведение;
- `docs/ARCHITECTURE_RU.md` — реализация;
- `docs/ENTITIES_RU.md` — HA contract;
- `docs/INSTALL_RU.md` — обновление и физические испытания.
# Energy ATS 0.5.0

Energy ATS — Home Assistant App для безопасного управления резервным электроснабжением дома с двумя генераторами.

## Базовая ATS-модель

Модель 0.4 сохраняется:

- реальная физическая топология первична;
- виртуального `Battery path` нет, используется `UPS_ONLY`;
- два генератора могут штатно быть RUNNING одновременно;
- persistent `GeneratorBusTracker` ведёт аппаратный FIFO owner;
- run-context: `OUTAGE_RELATED`, `TEST_RUN`, `OTHER`, `UNKNOWN`;
- один fallback `PRIMARY -> SECONDARY` без ping-pong;
- внешний SECONDARY не захватывается автоматически;
- outage-related генераторы завершаются только после безопасного возврата дома на стабильную Grid.

## Scheduled exercise в 0.5

Добавлен независимый плановый пробный запуск каждого генератора после длительного простоя.

Defaults:

```text
A: enabled=false, interval=30 дней, start=15:00, run=10 мин, grace=7 дней
B: enabled=false, interval=45 дней, start=15:00, run=10 мин, grace=14 дней
family_presence_entity=group.family
```

Exercise:

- использует обычный GC;
- не переводит дом с Grid на generator bus;
- до forced-date требует подтверждённого отсутствия семьи;
- после grace игнорирует только presence, но не safety;
- forced start требует успешно отправленного предупреждения минимум за 60 минут;
- не запускает второй генератор как fallback;
- любой достоверный run нужной длительности может стать qualifying run;
- сохраняет active attempt и ownership между restart;
- использует существующий bus-context `TEST_RUN`;
- при реальном outage допускает явный handoff уже работающего generator в outage-session;
- если handoff не состоялся, остаётся ответственным за остановку.

## Helper-ы

```text
input_boolean.automatic_generator_transfer
input_boolean.generator_test_mode
```

`generator_test_mode` остаётся маркером внешнего TEST_RUN. Scheduled exercise сам помечает собственный запуск как `TEST_RUN` и не требует включения helper-а.

## Конфигурация

Базовые policy/тайминги:

- `armed`;
- `tick_seconds`;
- `log_level`;
- `grid_failure_delay`;
- `grid_restore_stable_time`;
- `transfer_confirmation_timeout`;
- `generator_a_enabled`;
- `generator_b_enabled`.

Exercise:

- `family_presence_entity`;
- `generator_a_exercise_enabled`;
- `generator_a_exercise_interval_days`;
- `generator_a_exercise_start_time`;
- `generator_a_exercise_run_minutes`;
- `generator_a_exercise_presence_grace_days`;
- аналогичный набор для B.

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

Помимо базового source/phase/generator/bus/managed/run-context/PRIMARY/fallback status содержит per-generator exercise due/history state и active exercise timer.

Status sensor не используется как управляющий вход.

## Документация

- `docs/PHYSICAL_POWER_TOPOLOGY_RU.md` — физическая схема;
- `docs/REQUIREMENTS_RU.md` — нормативное поведение;
- `docs/ARCHITECTURE_RU.md` — реализация;
- `docs/ENTITIES_RU.md` — HA contract;
- `docs/INSTALL_RU.md` — обновление и физические испытания.

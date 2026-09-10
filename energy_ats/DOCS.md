# Energy ATS 0.6.0

Energy ATS — Home Assistant App для безопасного управления резервным электроснабжением дома с двумя генераторами.

## Базовая ATS-модель

Основные инварианты сохраняются:

- реальная физическая топология первична;
- виртуального `Battery path` нет, используется `UPS_ONLY`;
- два генератора могут штатно быть RUNNING одновременно;
- persistent `GeneratorBusTracker` ведёт аппаратный FIFO owner;
- run-context: `OUTAGE_RELATED`, `TEST_RUN`, `OTHER`, `UNKNOWN`;
- один fallback `PRIMARY -> SECONDARY` без ping-pong;
- внешний SECONDARY не захватывается автоматически;
- outage-related генераторы завершаются только после безопасного возврата дома на стабильную Grid.

## Load Manager

0.6 добавляет отдельный Load Manager для двух управляемых некритичных групп:

```text
G1 = switch.non_critical_loads_first_floor
G2 = switch.non_critical_loads_basement_floor
```

При `load_management_enabled=true`:

- после прогрева managed generator и непосредственно перед transfer G1/G2 снимаются с нагрузки;
- после подключения дома к generator bus группы возвращаются по одной: `G1 -> G2`;
- перед каждым admission и после каждого изменения выдерживается окно стабильных свежих измерений;
- рабочий предел определяется по `Nominal Power` фактического `GeneratorBusOwner`;
- sustained overload снимает нагрузки в обратном порядке: `G2 -> G1`;
- превышение `Maximum Power` имеет отдельное короткое подтверждение;
- контроль продолжается всё время питания дома от generator bus, а не только во время startup;
- meter, G1/G2 и power metadata — soft dependencies: их отказ переводит только Load Manager в `DEGRADED`, не ломая core ATS;
- после Grid восстанавливаются только группы, которые Load Manager сам ранее отключил.

Load Manager по умолчанию **выключен**.

Начальные параметры:

```text
load_management_enabled=false
load_measurement_stabilization_time=10 s
load_restore_margin_percent=15 %
nominal_overload_time=20 s
maximum_overload_confirmation_time=4 s
load_restore_retry_interval=300 s
```

Generator Controller должен публиковать:

```text
sensor.generator_a_nominal_power
sensor.generator_a_maximum_power
sensor.generator_b_nominal_power
sensor.generator_b_maximum_power
```

Текущая generator-bus load читается прежде всего из `sensor.generator_power`; `binary_sensor.generator_meter_status` используется для проверки доступности счётчика.

## Scheduled exercise

Независимый плановый пробный запуск каждого генератора после длительного простоя сохраняется без изменений.

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

`generator_test_mode` остаётся положительным маркером внешнего TEST_RUN. Scheduled exercise сам помечает собственный запуск как `TEST_RUN` и не требует включения helper-а.

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

Load Manager:

- `load_management_enabled`;
- `load_measurement_stabilization_time`;
- `load_restore_margin_percent`;
- `nominal_overload_time`;
- `maximum_overload_confirmation_time`;
- `load_restore_retry_interval`.

Exercise:

- `family_presence_entity`;
- `generator_a_exercise_enabled`;
- `generator_a_exercise_interval_days`;
- `generator_a_exercise_start_time`;
- `generator_a_exercise_run_minutes`;
- `generator_a_exercise_presence_grace_days`;
- аналогичный набор для B.

Имя, модель, паспортные мощности и PRIMARY читаются из Home Assistant / Generator Controller.

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

Помимо базового source/phase/generator/bus/managed/run-context/PRIMARY/fallback status содержит exercise state и Load Manager: phase/degraded reason, measured power, active limits, G1/G2 state и `shed_by_energy_ats`, overload/retry state.

Status sensor не используется как управляющий вход.

## Документация

- `docs/PHYSICAL_POWER_TOPOLOGY_RU.md` — физическая схема;
- `docs/REQUIREMENTS_RU.md` — нормативное поведение;
- `docs/ARCHITECTURE_RU.md` — реализация;
- `docs/ENTITIES_RU.md` — HA contract;
- `docs/USER_TESTS_RU.md` — физические пользовательские испытания.

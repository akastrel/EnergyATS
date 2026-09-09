# Energy ATS

Home Assistant App для управления резервным электроснабжением дома с двумя генераторами и подтверждаемой коммутацией основных контакторов.

Текущая версия: **0.4.0** (`experimental`).

## Источники истины

Документация разделена по смыслу:

1. [`docs/PHYSICAL_POWER_TOPOLOGY_RU.md`](docs/PHYSICAL_POWER_TOPOLOGY_RU.md) — как физически устроена электроустановка;
2. [`docs/REQUIREMENTS_RU.md`](docs/REQUIREMENTS_RU.md) — как EnergyATS обязан вести себя на этой физической схеме;
3. код и тесты — реализация этих требований.

Если код противоречит физической схеме или требованиям, исправляется код.

## Архитектура 0.4

```text
energy_supervisor.py      policy и managed-сессии
generator_bus.py          FIFO-owner общей генераторной шины
generator_controller.py  жизненный цикл одного двигателя
power_transfer.py         основные контакторы Grid / Generator
ha_adapter.py             HA states и service calls
main.py                   единый tick, journal, status/log
```

Ключевые положения:

- отдельного физического `Battery path` нет; автономная работа MAP представляется как `UPS_ONLY`;
- Generator A и Generator B могут штатно работать одновременно;
- аппаратная взаимная блокировка допускает только одного owner общей генераторной шины;
- owner следует аппаратному FIFO и сохраняется по истории RUNNING;
- при недостаточной истории owner = `UNKNOWN`, без угадывания;
- run-context минимален: `NONE`, `OUTAGE_RELATED`, `TEST_RUN`, `OTHER`, `UNKNOWN`;
- автоматический fallback ограничен одним переходом `PRIMARY -> SECONDARY`, без ping-pong;
- уже работающий SECONDARY не захватывается в managed ownership;
- после стабильного восстановления Grid дом сначала возвращается на Grid, затем останавливаются известные `OUTAGE_RELATED` генераторы;
- `TEST_RUN` этим правилом не останавливается;
- `armed: false` запрещает реальные аппаратные switch/button calls.

A/B остаются стабильными машинными слотами. Пользовательские имя и модель читаются из Home Assistant.

Подробнее: [`docs/ARCHITECTURE_RU.md`](docs/ARCHITECTURE_RU.md).

## Home Assistant contract

Основные entities:

```text
input_boolean.automatic_generator_transfer
input_boolean.generator_test_mode

binary_sensor.grid_input_ready
binary_sensor.house_powered_by_grid
binary_sensor.house_powered_by_generator
binary_sensor.generator_a_is_running
binary_sensor.generator_b_is_running

switch.grid_power
switch.use_generator_as_power_source
switch.generator_a_remote_start
switch.generator_b_remote_start
switch.generators_emergency_stop

sensor.generator_a_name
sensor.generator_b_name
sensor.generator_a_model
sensor.generator_b_model
select.primary_generator
```

`house_powered_by_grid` и `house_powered_by_generator` являются feedback цепей управления основных контакторов, а не независимым измерением напряжения после силовых контактов.

Полный контракт: [`docs/ENTITIES_RU.md`](docs/ENTITIES_RU.md).

## Установка

Repository для Home Assistant Apps:

```text
https://github.com/akastrel/EnergyATS
```

Корневой `ats.yaml` создаёт:

```text
input_boolean.automatic_generator_transfer
input_boolean.generator_test_mode
```

При обновлении с 0.3.x старый persistent journal не мигрируется: внутренняя модель 0.4 принципиально другая.

Подробно: [`docs/INSTALL_RU.md`](docs/INSTALL_RU.md).

## Ручные команды

Через `hassio.app_stdin` поддерживаются:

```text
start_generator
stop_generator
reset
```

- `start_generator` — новая managed-сессия текущего PRIMARY;
- `stop_generator` — безопасное завершение managed-сессии;
- `reset` — контролируемое восстановление после `RECOVERY_REQUIRED`.

## Диагностика

App публикует read-only:

```text
sensor.energy_ats_status
```

Ключевые attributes текущей 0.4:

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
fallback_used
remaining_seconds
session_reason
armed
```

Status sensor не имеет отдельного `schema_version`, `bus_owner_slot` или `primary_generator_slot` и не используется как управляющий вход.

## Документация

- [Физическая схема](docs/PHYSICAL_POWER_TOPOLOGY_RU.md)
- [Требования](docs/REQUIREMENTS_RU.md)
- [Архитектура](docs/ARCHITECTURE_RU.md)
- [Home Assistant entities](docs/ENTITIES_RU.md)
- [Установка и обновление](docs/INSTALL_RU.md)
- [Changelog](energy_ats/CHANGELOG.md)

## Разработка

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```

`tests/test_end_to_end_scenarios.py` моделирует HA states, ES/TPC/GC, аппаратный FIFO-owner и фактические service calls к fake Home Assistant.

Зелёный CI подтверждает программную модель, но не заменяет commissioning на реальных контакторах, генераторах, DKG116 и MAP.
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

Приложение остаётся одним процессом, но разделено по ответственности:

```text
energy_supervisor.py      policy и управляемые сессии
power_transfer.py         основные контакторы Grid / Generator
generator_controller.py  жизненный цикл одного двигателя
generator_bus.py          наблюдаемый owner общей генераторной шины
ha_adapter.py             Home Assistant entities и service calls
main.py                   composition, tick, journal, status/log
```

Ключевые положения 0.4:

- отдельного физического `Battery path` нет; при отсутствии основного источника используется состояние `UPS_ONLY`;
- Generator A и Generator B могут штатно работать одновременно;
- аппаратная взаимная блокировка не позволяет им одновременно владеть общей генераторной шиной;
- owner шины определяется аппаратным FIFO: кто первым создал напряжение и втянул контактор, тот удерживает шину до остановки;
- `GeneratorBusTracker` хранит известного owner и run-context между restart;
- автоматический fallback ограничен одним переходом `PRIMARY -> SECONDARY`, без ping-pong;
- уже работающий внешний SECONDARY не захватывается в managed ownership;
- после стабильного восстановления Grid останавливаются все известные `outage-related` генераторы, в том числе внешне запущенные;
- `TEST_RUN` является явным исключением и по одному лишь возврату Grid не останавливается;
- `armed: false` запрещает все аппаратные switch/button service calls.

A/B остаются стабильными машинными слотами. Пользовательские имя и модель читаются из Home Assistant.

## Home Assistant contract

Основные сущности:

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

`house_powered_by_grid` и `house_powered_by_generator` подключены к **цепям управления основных контакторов**, а не к независимым силовым выходам после них. Это важное ограничение текущей схемы контроля.

Полный контракт: [`docs/ENTITIES_RU.md`](docs/ENTITIES_RU.md).

## Установка

Repository для Home Assistant Apps:

```text
https://github.com/akastrel/EnergyATS
```

Корневой `ats.yaml` устанавливается как Home Assistant package и создаёт:

```text
input_boolean.automatic_generator_transfer
input_boolean.generator_test_mode
```

При обновлении с 0.3.x старый persistent journal **не мигрируется**: модель 0.4 принципиально другая. Обновление выполнять при доступной Grid, остановленных генераторах и `armed: false`.

Подробно: [`docs/INSTALL_RU.md`](docs/INSTALL_RU.md).

## Ручные команды

Через `hassio.app_stdin` поддерживаются:

```text
start_generator
stop_generator
reset
```

- `start_generator` — новая managed-сессия текущего PRIMARY;
- `stop_generator` — безопасно вернуть основные контакторы в сторону Grid и завершить managed-сессию;
- `reset` — ограниченное восстановление после `RECOVERY_REQUIRED`.

## Диагностика

App публикует read-only:

```text
sensor.energy_ats_status
```

В 0.4 используется `schema_version: 3`. Основные атрибуты:

```text
source
phase
generator
generator_model
generator_slot
primary_generator
primary_generator_slot
bus_owner
bus_owner_slot
generator_a_run_context
generator_b_run_context
fallback_used
remaining_seconds
session_reason
armed
schema_version
```

Sensor предназначен для UI и диагностики и не используется как управляющий вход.

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

Сквозные тесты в `tests/test_end_to_end_scenarios.py` моделируют HA states, работу ES/TPC/GC, аппаратный owner генераторной шины и фактические service calls к fake Home Assistant.

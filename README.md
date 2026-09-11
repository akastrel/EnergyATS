# Energy ATS

Home Assistant App для управления резервным электроснабжением дома с двумя генераторами и подтверждаемой коммутацией основных контакторов.

Текущая версия: **0.7.0** (`experimental`).

## Источники истины

Документация разделена по смыслу:

1. [`docs/PHYSICAL_POWER_TOPOLOGY_RU.md`](docs/PHYSICAL_POWER_TOPOLOGY_RU.md) — как физически устроена электроустановка;
2. [`docs/REQUIREMENTS_RU.md`](docs/REQUIREMENTS_RU.md) — как EnergyATS обязан вести себя на этой физической схеме;
3. код и тесты — реализация этих требований.

Если код противоречит физической схеме или требованиям, исправляется код.

## Архитектура

```text
energy_supervisor.py      policy и managed-сессии
generator_bus.py          FIFO-owner общей генераторной шины
generator_controller.py  жизненный цикл одного двигателя
power_transfer.py         основные контакторы Grid / Generator
exercise_scheduler.py     график и ownership пробных запусков
load_manager.py           G1/G2, admission и overload LOAD_SHEDDING
ha_adapter.py             HA states и service calls
main.py                   единый tick, arbitration, journal, status/log
```

Ключевые положения базовой ATS-логики:

- отдельного физического `Battery path` нет; автономная работа MAP представляется как `UPS_ONLY`;
- Generator A и Generator B могут штатно работать одновременно;
- аппаратная взаимная блокировка допускает только одного owner общей генераторной шины;
- owner следует аппаратному FIFO и сохраняется по истории RUNNING;
- при недостаточной истории owner = `UNKNOWN`, без угадывания;
- run-context минимален: `NONE`, `OUTAGE_RELATED`, `TEST_RUN`, `OTHER`, `UNKNOWN`;
- автоматический fallback ограничен одним переходом `PRIMARY -> SECONDARY`, без ping-pong;
- уже работающий внешний SECONDARY не захватывается в managed ownership;
- после стабильного восстановления Grid дом сначала возвращается на Grid, затем останавливаются разрешённые outage-related генераторы;
- `armed: false` запрещает реальные аппаратные switch/button calls.

A/B остаются стабильными машинными слотами. Пользовательские имя, модель и паспортные мощности читаются из Home Assistant.

## Delayed Start и Charge Cycling — 0.7

Обе функции выключены по умолчанию и включаются независимо. Delayed Start после подтверждённого исчезновения сети оставляет критическую линию на UPS до достижения Start SoC, минимального TTG или максимальной задержки. Недостоверные батарейные данные отменяют ожидание и возвращают обычный запуск ATS.

Charge Cycling завершает только собственную автоматическую outage-сессию при достижении Target SoC: TPC снимает дом с генератора, подтверждается снятие нагрузки, GC выполняет cooldown/stop, затем начинается новое ожидание на UPS. Ручной запрос отменяет cycling для текущей сессии; внешний генератор по Target SoC не останавливается. Устойчивый возврат сети имеет приоритет, в том числе между циклами.

Настройки, батарейные entities и наблюдаемость описаны в [руководстве Delayed Start / Charge Cycling](docs/DELAYED_START_RU.md). Сценарии 78–94 из требований проверяются в `tests/test_outage_power_app.py`.

## Load Manager — 0.6

Load Manager — отдельная policy-функция для двух некритичных групп:

```text
G1 = switch.non_critical_loads_first_floor
G2 = switch.non_critical_loads_basement_floor
```

При `load_management_enabled=true` он:

- после прогрева managed generator, но до generator transfer, отключает доступные G1/G2, чтобы генератор принял дом с минимальной нагрузкой;
- после transfer возвращает нагрузки по одной с отдельным measurement window: `G1 -> G2`;
- использует `Nominal Power` фактического `GeneratorBusOwner` как рабочий предел;
- непрерывно контролирует generator power во всё время питания дома от generator bus;
- при устойчивой перегрузке выполняет `LOAD_SHEDDING` в порядке `G2 -> G1`;
- использует отдельный короткий timeout для превышения `Maximum Power`;
- сохраняет ownership только собственных OFF и после возврата Grid восстанавливает только такие группы;
- при отказе meter, G1/G2 или power metadata деградирует локально и не переводит core ATS в `RECOVERY_REQUIRED`.

Load Manager по умолчанию **выключен**. Поэтому обновление App не требует немедленного наличия его soft-dependency entities; они нужны перед фактическим включением функции.

Generator Controller должен публиковать отдельные numeric sensors:

```text
sensor.generator_a_nominal_power
sensor.generator_a_maximum_power
sensor.generator_b_nominal_power
sensor.generator_b_maximum_power
```

Счётчик общей generator bus используется через `binary_sensor.generator_meter_status` и `sensor.generator_power`; дополнительная электрическая телеметрия остаётся диагностической.

## Scheduled generator exercise

Независимый плановый пробный запуск каждого генератора после длительного простоя сохраняется.

Scheduler:

- имеет отдельные interval/start-time/run-duration/grace настройки для A и B;
- обычный test начинает только при подтверждённом отсутствии семьи;
- после grace period может выполнить forced test, но только после заранее успешно доставленного предупреждения;
- при штатной Grid не переводит дом на generator bus;
- запускает и останавливает двигатель через существующий Generator Controller;
- не использует maintenance fallback на второй генератор;
- принимает любой достоверный достаточно длинный run как qualifying activity;
- сохраняет active attempt, due/history и stop ownership между restart;
- маркирует собственный RUNNING как `TEST_RUN`;
- при реальном outage может явно передать уже работающий test-generator обычной outage-сессии;
- если handoff не состоялся, остаётся ответственным за штатную остановку своего двигателя.

Автоматические exercise по умолчанию выключены.

```text
Generator A: interval 30 дней, start 15:00, run 10 мин, presence grace 7 дней
Generator B: interval 45 дней, start 15:00, run 10 мин, presence grace 14 дней
```

Presence entity задаётся параметром `family_presence_entity` (default `group.family`). Неизвестный/unavailable presence не блокирует основную ATS-логику: обычный exercise просто откладывается.

## Home Assistant contract

Core entities включают:

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

Load Manager дополнительно использует soft dependencies:

```text
sensor.generator_a_nominal_power
sensor.generator_a_maximum_power
sensor.generator_b_nominal_power
sensor.generator_b_maximum_power
binary_sensor.generator_meter_status
sensor.generator_power
switch.non_critical_loads_first_floor
switch.non_critical_loads_basement_floor
```

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

При обновлении с 0.3.x старый persistent journal не мигрируется: внутренняя модель 0.4 принципиально другая. Обновления 0.4 -> 0.5 -> 0.6 -> 0.7 используют совместимый top-level journal schema; новые policy-секции получают собственный state при отсутствии старых данных.

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

Кроме базового source/phase/generator/bus/managed/run-context/PRIMARY/fallback status содержит Exercise state и Load Manager: phase/degraded reason, generator power, active nominal/maximum, state/ownership G1/G2, overload timers и retry state.

Status sensor не используется как управляющий вход.

## Документация

- [Физическая схема](docs/PHYSICAL_POWER_TOPOLOGY_RU.md)
- [Требования](docs/REQUIREMENTS_RU.md)
- [Архитектура](docs/ARCHITECTURE_RU.md)
- [Home Assistant entities](docs/ENTITIES_RU.md)
- [Установка и обновление](docs/INSTALL_RU.md)
- [Пользовательские физические тесты](docs/USER_TESTS_RU.md)
- [Changelog](energy_ats/CHANGELOG.md)

## Разработка

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```

Тесты моделируют HA states, ES/TPC/GC, аппаратный FIFO-owner, scheduled exercise, Load Manager, Delayed Start/Charge Cycling, restart/handoff и фактические service calls к fake Home Assistant.

Зелёный CI подтверждает программную модель, но не заменяет commissioning на реальных контакторах, generator-bus meter, G1/G2, генераторах, DKG116 и MAP.
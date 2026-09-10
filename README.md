# Energy ATS

Home Assistant App для управления резервным электроснабжением дома с двумя генераторами и подтверждаемой коммутацией основных контакторов.

Текущая версия: **0.5.0** (`experimental`).

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
ha_adapter.py             HA states и service calls
main.py                   единый tick, journal, status/log
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

A/B остаются стабильными машинными слотами. Пользовательские имя и модель читаются из Home Assistant.

## Scheduled generator exercise — 0.5

Версия 0.5 добавляет независимый плановый пробный запуск каждого генератора после длительного простоя.

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
- при реальном outage может явно передать уже работающий test-generator обычной outage-сессии без бессмысленного OFF/повторного cold start;
- если handoff не состоялся, остаётся ответственным за штатную остановку своего двигателя.

Автоматические exercise по умолчанию **выключены**.

Значения по умолчанию после включения:

```text
Generator A: interval 30 дней, start 15:00, run 10 мин, presence grace 7 дней
Generator B: interval 45 дней, start 15:00, run 10 мин, presence grace 14 дней
```

Presence entity задаётся параметром `family_presence_entity` (по умолчанию `group.family`). Неизвестный/unavailable presence не блокирует основную ATS-логику: обычный exercise просто откладывается.

Подробнее: [`docs/ARCHITECTURE_RU.md`](docs/ARCHITECTURE_RU.md) и [`docs/REQUIREMENTS_RU.md`](docs/REQUIREMENTS_RU.md).

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

При обновлении с 0.3.x старый persistent journal не мигрируется: внутренняя модель 0.4 принципиально другая. Обновление 0.4 -> 0.5 сохраняет совместимый top-level journal schema и добавляет scheduler-state.

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

К базовым attributes (`source`, `phase`, generator/bus owner, managed generator, run-context A/B, PRIMARY, fallback, timers, `armed`) в 0.5 добавлены exercise attributes для A/B: initial/qualifying reference, next due, overdue, forced date, warning time, active state, planned duration, last result/failure и active exercise timer.

Status sensor не используется как управляющий вход.

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

Тесты моделируют HA states, ES/TPC/GC, аппаратный FIFO-owner, scheduled exercise, restart/handoff и фактические service calls к fake Home Assistant.

Зелёный CI подтверждает программную модель, но не заменяет commissioning на реальных контакторах, генераторах, DKG116 и MAP.

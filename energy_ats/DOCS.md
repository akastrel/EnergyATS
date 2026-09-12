# Energy ATS — руководство пользователя

Эта страница показывается во вкладке **Documentation** установленного Home Assistant App. Здесь собрана практическая информация для эксплуатации. Нормативное поведение системы задаёт `docs/REQUIREMENTS_RU.md` в repository.

## 1. Что делает App

EnergyATS управляет резервным электроснабжением дома с двумя генераторами и общей generator bus.

Основные сценарии:

- автоматический запуск резерва после потери Grid;
- ручной переход на generator и обратно;
- один fallback `PRIMARY -> SECONDARY`;
- безопасный Grid / Generator transfer с подтверждением каждого шага;
- работа только от критической UPS-линии (`UPS_ONLY`);
- UPS Run для длительных отключений;
- Scheduled Exercise для периодических пробных запусков;
- Load Manager для двух некритичных групп;
- Recovery после неоднозначной или незавершённой физической операции;
- причинно-следственный журнал ключевых решений и физических подтверждений.

## 2. Важные особенности физической схемы

- отдельного управляемого Battery contactor нет;
- MAP самостоятельно переводит UPS-линию на АКБ;
- A и B могут одновременно быть RUNNING;
- общей generator bus физически владеет только один generator благодаря аппаратной блокировке;
- `RUNNING`, `REMOTE`, selector command и feedback — разные сигналы;
- EnergyATS не угадывает неизвестный bus owner;
- внешний RUNNING generator не становится managed автоматически.

## 3. Первый запуск

Для первого запуска используйте:

```yaml
armed: false
```

Проверьте в Home Assistant и `sensor.energy_ats_status`:

- Grid определяется правильно;
- оба generator names/models корректны;
- `select.primary_generator` указывает нужный generator;
- оба RUNNING/REMOTE отображаются правильно;
- `bus_owner = none`, если оба generator остановлены;
- нет `RECOVERY_REQUIRED` без причины;
- optional functions остаются выключенными, если вы ещё не готовы их тестировать.

Только после этого переходите к:

```yaml
armed: true
```

и выполняйте физические commissioning-тесты.

## 4. Основные настройки

```text
armed
tick_seconds
log_level
grid_failure_delay
grid_restore_stable_time
transfer_confirmation_timeout
generator_a_enabled
generator_b_enabled
```

PRIMARY выбирается не по A/B-параметру App, а через:

```text
select.primary_generator
```

`generator_a_enabled` / `generator_b_enabled` позволяют временно исключить конкретный physical slot из новых managed sessions.

## 5. Ручные команды

Через `hassio.app_stdin` App принимает:

```text
start_generator
stop_generator
reset
```

### `start_generator`

Начинает manual managed session. Если уже идёт подходящий RUNNING Scheduled Exercise и безопасный handoff однозначен, EnergyATS может использовать этот же generator без stop/cold-start.

### `stop_generator`

Безопасно завершает managed session. Сначала снимается нагрузка дома, затем выполняются cooldown и REMOTE OFF.

Если Grid отсутствует, manual stop переводит дом в `UPS_ONLY` и подавляет автоматический повторный запуск. Если Grid path перед этим был изолирован самим EnergyATS, обязанность восстановить его после устойчивого возврата Grid сохраняется даже через restart App.

### `reset`

Запускает Recovery. Это не «стереть ошибку».

Recovery проверяет E-stop, физические feedback и внешний RUNNING. Supervisor задаёт порядок:

```text
Grid path
  -> остановка только тех generator, которыми EnergyATS имеет право управлять
  -> завершение Recovery
```

Внешний generator автоматически не захватывается.

## 6. UPS Run

UPS Run — стратегия длительного outage. Она состоит из двух независимых функций.

### Delayed Start

```text
delayed_generator_start_enabled = false
```

После обычного `grid_failure_delay` EnergyATS может продолжать `UPS_ONLY`, если батарея пригодна для ожидания.

Generator требуется, если выполняется хотя бы одно условие:

```text
SoC <= generator_start_soc
OR TTG <= generator_min_ttg_before_start
OR UPS wait >= generator_max_start_delay_hours
```

Defaults:

```text
generator_start_soc = 40 %
generator_min_ttg_before_start = 60 min
generator_max_start_delay_hours = 6 h
```

### Charge Cycling

```text
generator_charge_cycle_enabled = false
```

Для автоматической cycle-owned outage session generator может быть остановлен после:

```text
SoC >= generator_target_charge_soc
```

Default:

```text
generator_target_charge_soc = 80 %
```

После Target SoC порядок такой:

```text
Generator supply
  -> подтверждённый переход дома в UPS_ONLY
  -> cooldown
  -> generator OFF
  -> новый UPS wait
```

Manual request отменяет automatic Target stop для текущей session. Stable Grid имеет приоритет и возвращает дом сразу по обычному Generator -> Grid сценарию.

### Battery inputs

```text
sensor.ups_battery_charge_level_soc
sensor.ups_battery_time_remaining_minutes_ttg
binary_sensor.ups_running_on_battery
binary_sensor.ups_ready
```

Это soft dependencies core ATS. Если UPS Run выключен, они не влияют на обычный ATS.

Если UPS Run включён, stale/invalid telemetry считается основанием прекратить ожидание и использовать обычный безопасный generator start. TTG участвует в решении только при реальном discharge.

## 7. Scheduled Exercise

Scheduled Exercise настраивается отдельно для A и B и по умолчанию выключен.

Defaults:

```text
A: interval=30 дней, start=15:00, run=10 мин, grace=7 дней
B: interval=45 дней, start=15:00, run=10 мин, grace=14 дней
family_presence_entity=group.family
```

Ordinary Exercise:

- требует Grid и устойчивого Grid path;
- не переводит дом на generator bus;
- до forced date требует подтверждённого отсутствия семьи;
- повторно проверяет presence непосредственно перед REMOTE ON;
- при `home` или `unknown/unavailable` откладывается как `DEFERRED`;
- не имеет fallback на второй generator.

После grace presence больше не блокирует forced run, но safety checks сохраняются. Forced run разрешён только после заранее **успешно доставленного** warning.

Если во время RUNNING Exercise появляется manual request или реальный outage, EnergyATS может явно передать этот же generator соответствующей managed session. До подтверждённого handoff Scheduler остаётся ответственным за stop.

## 8. Load Manager

Load Manager выключен по умолчанию:

```text
load_management_enabled = false
```

Он управляет только:

```text
G1 = switch.non_critical_loads_first_floor
G2 = switch.non_critical_loads_basement_floor
```

При включении:

1. после готовности generator, но до transfer, отключает доступные G1/G2;
2. после generator transfer измеряет base load;
3. возвращает группы по одной `G1 -> G2` при достаточном запасе;
4. непрерывно контролирует generator power;
5. при overload снимает `G2 -> G1`;
6. после Grid возвращает только те группы, которые отключил сам.

Defaults:

```text
load_measurement_stabilization_time = 10 s
load_restore_margin_percent = 15 %
nominal_overload_time = 20 s
maximum_overload_confirmation_time = 4 s
load_restore_retry_interval = 300 s
```

Soft dependencies:

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

Потеря meter или load entity переводит только Load Manager в `DEGRADED`; core ATS продолжает работу. Stale power stream также считается потерей достоверного measurement и требует нового stabilization после восстановления.

## 9. Status sensor

EnergyATS публикует:

```text
sensor.energy_ats_status
```

Наиболее полезные поля:

```text
source
phase
generator
generator_model
generator_slot
managed_generator
bus_owner
primary_generator
session_reason
fallback_used
remaining_seconds
armed
```

Дополнительно status содержит:

- run-context A/B;
- Exercise due/history/active/result;
- UPS Run battery/wait/cycle state;
- Load Manager phase/reason/power/limits/G1/G2 ownership.

Status — только диагностика. Управляющая логика не использует его как input.

## 10. Logbook и notifications

Logbook строится как причинно-следственный журнал. Для ключевых сценариев события разделены на четыре типа:

```text
trigger / физическое изменение
  -> принятое решение
  -> аппаратное действие
  -> подтверждённый feedback / результат
```

Например, потеря Grid, начало ожидания на UPS, достижение SoC/TTG-порога, старт генератора, изменение RUNNING/REMOTE, выбор generator bus, возврат Grid и завершение сессии журналируются раздельно. Это позволяет понять не только «что переключилось», но и почему EnergyATS сделал именно это.

Физически наблюдаемые изменения фиксируются только при реальном изменении state. Первый snapshot после start или reconnect задаёт baseline и намеренно не создаёт ложный поток событий. Внешний запуск генератора отличается от запуска, которым управляет EnergyATS.

Новые причинные пользовательские формулировки адресуются стабильными message keys; русский текст находится в `app/user_messages_ru.py`. Благодаря этому текст можно локализовать без изменения FSM и safety policy.

Полную последовательность событий следует смотреть в App log: туда попадают MAIN/DETAIL events, команды Generator Controller, Power Transfer и Load Manager, а также отправляемые пользовательские сообщения. Основной поток Home Assistant Logbook намеренно содержит только существенные MAIN events; отдельная DETAIL-сущность не используется.

Critical events используют `script.notify_critical`. Обычные status/Logbook/user publications выполняются best-effort и не должны задерживать control tick из-за сетевого timeout. При reconnect незавершённые background publications отменяются.

Исключение — warning перед forced Scheduled Exercise: Scheduler считает его доставленным только после успешного ответа Home Assistant.

## 11. Restart и reconnect

EnergyATS сохраняет состояние, необходимое для безопасного restart:

- managed session;
- GeneratorBusTracker;
- Scheduled Exercise;
- UPS Run;
- Load Manager ownership;
- незавершённые core hardware actions.

Устойчивая однозначная session может быть восстановлена без повторного start/transfer. Restart во время неподтверждённой hardware transaction требует Recovery, если безопасное продолжение нельзя доказать.

Потеря Home Assistant во время transient physical operation также может зафиксировать Recovery вместо слепого продолжения после reconnect.

## 12. Что обязательно проверить физически

Автоматические tests и container smoke не проверяют реальные контакторы, DKG116, генераторы, MAP, meter и проводку. Поэтому физический commissioning в `USER_TESTS_RU.md` отбирается по другому принципу: тест остаётся только тогда, когда проверяет уникальный hardware/integration risk.

Для базового ATS перед unattended эксплуатацией:

- M1–M7 — core ATS;
- R1–R4 — restart, реальные переходы и топология;
- P1 — electrical acceptance каждого generator, включая MAP/заряд АКБ;
- R5 — если нужен полностью подтверждённый fallback на конкретной feedback-схеме и отказ PRIMARY можно вызвать безопасно;
- P2 — перед длительной unattended эксплуатацией каждого generator.

Для optional features:

- U1–U2 — UPS Run;
- X1–X3 — Scheduled Exercise;
- L1–L3 — Load Manager;
- L4 — optional meter-loss test, только если данные можно безопасно отключить.

Отдельно не повторяются симметричные A/B permutations, двойной отказ ради проверки software ping-pong, manual Target-override без нового аппаратного эффекта и искусственные feedback faults без безопасного штатного способа. Это automated/HIL territory.

## 13. Полная документация

- [Физическая схема](https://github.com/akastrel/EnergyATS/blob/main/docs/PHYSICAL_POWER_TOPOLOGY_RU.md)
- [Нормативные требования](https://github.com/akastrel/EnergyATS/blob/main/docs/REQUIREMENTS_RU.md)
- [Архитектура](https://github.com/akastrel/EnergyATS/blob/main/docs/ARCHITECTURE_RU.md)
- [HA entities](https://github.com/akastrel/EnergyATS/blob/main/docs/ENTITIES_RU.md)
- [Установка и обновление](https://github.com/akastrel/EnergyATS/blob/main/docs/INSTALL_RU.md)
- [Физические испытания](https://github.com/akastrel/EnergyATS/blob/main/docs/USER_TESTS_RU.md)
- [Changelog](https://github.com/akastrel/EnergyATS/blob/main/energy_ats/CHANGELOG.md)

Отдельного `DELAYED_START_RU.md` больше нет: UPS Run описывается здесь для пользователя, нормативно — в `REQUIREMENTS_RU.md`, HA contract — в `ENTITIES_RU.md`, а commissioning — в `USER_TESTS_RU.md`.
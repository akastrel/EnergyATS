# Energy ATS 0.4.0 — архитектура

## 1. Источники истины

Архитектура реализует два документа более высокого уровня:

1. `PHYSICAL_POWER_TOPOLOGY_RU.md` — что физически существует и как ведёт себя железо;
2. `REQUIREMENTS_RU.md` — как при этой физической схеме должен вести себя EnergyATS.

Этот документ описывает только способ реализации требований. Он не вводит новые физические устройства и не расширяет разрешённые сценарии.

## 2. Разделение ответственности

EnergyATS — один Home Assistant App и один Python-процесс, но управляющая логика разделена на небольшие независимые части.

| Модуль | Ответственность |
|---|---|
| `domain.py` | Общие термины: A/B, `PowerSource`, `PowerPath`, причина managed-сессии |
| `generator_bus.py` | FIFO-owner общей генераторной шины и контекст непрерывных RUNNING |
| `generator_controller.py` | Жизненный цикл одного двигателя: choke, REMOTE, запуск, прогрев, cooldown, stop |
| `power_transfer.py` | Основные контакторы Grid / Generator и break-before-make |
| `energy_supervisor.py` | Policy: outage, managed-сессия, fallback, возврат Grid, recovery |
| `ha_adapter.py` | HA states -> observations и разрешённые HA service calls |
| `main.py` | Composition root: единый tick, orchestration, journal, status и log |
| `state_store.py` | Атомарное сохранение persistent state |
| `ha_client.py` | WebSocket/REST transport Home Assistant |

Главное правило границ: **Supervisor решает, что требуется; GC и TPC решают, как безопасно выполнить уже разрешённую операцию; HA Adapter только связывает доменную модель с реальными entities.**

## 3. Доменные термины питания

### `PowerSource`

Фактически наблюдаемый режим питания дома:

- `GRID` — дом питается от основной сети;
- `GENERATOR` — дом подключён к общей генераторной шине;
- `UPS_ONLY` — обычная шина дома не получает внешний источник, но UPS-линия может работать от MAP;
- `NO_POWER` — питание отсутствует;
- `UNKNOWN` — состояние нельзя безопасно определить.

В 0.4 нет `BATTERY`, `GENERATOR_A` или `GENERATOR_B` как отдельных силовых источников.

### `PowerPath`

Подтверждённое положение основной пары контакторов:

- `GRID`;
- `ISOLATED`;
- `GENERATOR`;
- `UNKNOWN`.

`PowerPath` и `PowerSource` различаются намеренно. Например, при выбранном Grid path и отсутствующей внешней Grid фактический режим может быть `UPS_ONLY`.

## 4. GeneratorBusTracker

`generator_bus.py` — единственное место, где определяется логический owner общей генераторной шины.

Owner:

- `A`;
- `B`;
- `NONE`;
- `UNKNOWN`.

Tracker использует историю `generator_*_is_running` и повторяет аппаратное FIFO-поведение контакторов:

- первый появившийся RUNNING получает owner;
- второй RUNNING не меняет owner;
- пока текущий owner продолжает RUNNING, owner не меняется;
- если owner остановился, а второй генератор продолжает RUNNING, owner автоматически переходит ко второму;
- если после restart/history gap оба уже RUNNING и порядок нельзя восстановить, owner = `UNKNOWN`.

Ни TPC, ни HA Adapter не пытаются повторно вычислять owner.

### Контекст непрерывного RUNNING

Для каждого двигателя Tracker хранит один из следующих контекстов:

- `NONE` — двигатель не работает;
- `OUTAGE_RELATED` — RUNNING начался при отсутствующей Grid и относится к outage;
- `TEST_RUN` — RUNNING начался при явно включённом test mode;
- `OTHER` — известный не-outage запуск;
- `UNKNOWN` — причина уже существующего RUNNING не может быть доказана.

Это не ownership двигателя. Контекст нужен прежде всего для узкого правила завершения outage: после безопасного возврата дома на стабильную Grid можно остановить `OUTAGE_RELATED`, но нельзя автоматически останавливать `TEST_RUN` или неизвестный запуск.

## 5. GeneratorController

Один экземпляр GC обслуживает один физический генератор и не знает про PRIMARY/SECONDARY или общую политику ATS.

Текущие фазы:

- `WAITING_FOR_DATA`;
- `IDLE`;
- `PREPARING`;
- `WAITING_FOR_RUNNING`;
- `HOLDING_COLD_START_CHOKE`;
- `WARMING_UP`;
- `READY_FOR_LOAD`;
- `WAITING_FOR_LOAD_RELEASE`;
- `COOLING_DOWN`;
- `WAITING_FOR_STOP`;
- `EXTERNAL_RUNNING`;
- `FAULT`.

GC выдаёт только команды одного двигателя:

- `REMOTE_ON`;
- `REMOTE_OFF`;
- `CHOKE_TO_COLD_START`;
- `CHOKE_TO_RUN`.

Ошибка жизненного цикла фиксируется локальным `FAULT`. Решение о fallback или системном `RECOVERY_REQUIRED` принимает Supervisor.

`step_authorized_shutdown()` используется только после того, как Supervisor уже разрешил остановить конкретный разгруженный двигатель. Повторной policy-проверки ownership внутри GC нет.

## 6. PowerTransferController

TPC управляет только основной парой Grid / Generator и ничего не знает о Generator A/B.

Фазы:

- устойчивые: `STABLE_GRID`, `STABLE_ISOLATED`, `STABLE_GENERATOR`;
- переходные: `DISCONNECTING_GRID`, `SELECTING_GENERATOR`, `DISCONNECTING_GENERATOR`, `CONNECTING_GRID`;
- служебные: `WAITING_FOR_DATA`, `RECOVERY_REQUIRED`.

Правило Grid -> Generator:

1. `grid_power -> OFF`;
2. дождаться снятия Grid control feedback;
3. `use_generator_as_power_source -> ON`;
4. дождаться generator control feedback.

Правило Generator -> Grid симметрично:

1. `use_generator_as_power_source -> OFF`;
2. дождаться снятия generator feedback;
3. `grid_power -> ON`;
4. дождаться Grid feedback.

Каждый tick выдаёт не более одной новой силовой команды и не начинает следующий шаг до подтверждения предыдущего.

## 7. EnergySupervisor

Supervisor содержит только policy, которую нельзя вывести из одного локального контроллера.

Фазы 0.4:

- `WAITING_FOR_DATA`;
- `NORMAL`;
- `GRID_FAILURE_DELAY`;
- `STARTING_GENERATOR`;
- `ON_GENERATOR`;
- `RETURNING_TO_GRID`;
- `EXTERNAL_RUNNING`;
- `RECOVERY_REQUIRED`.

Отдельных фаз `ON_EXTERNAL_GENERATOR`, `STOPPING_GENERATORS` или `TRANSFERRING_TO_GENERATOR` нет: внешний owner, силовой переход и остановка видны из `GeneratorBusTracker`, TPC и GC и не должны дублироваться в Supervisor.

### Managed-сессия

Сессия хранит только то, что действительно является policy-state:

- причина (`manual_generator_start` / `grid_outage`);
- текущий managed slot;
- началась ли сессия при отсутствующей Grid;
- запрошена ли остановка;
- использован ли единственный fallback.

### Fallback

При отказе managed PRIMARY допускается один переход на SECONDARY.

- если SECONDARY уже работает, он остаётся внешним; EnergyATS не присваивает себе его ownership;
- если SECONDARY свободен и разрешён, начинается единственный managed fallback;
- после отказа SECONDARY повторного возврата к PRIMARY нет — требуется recovery.

### Возврат Grid

После стабильной Grid:

1. Supervisor требует `GRID` у TPC;
2. TPC безопасно снимает Generator и возвращает Grid;
3. только после подтверждённого Grid path Supervisor разрешает остановку всех известных `OUTAGE_RELATED` генераторов;
4. `TEST_RUN`, `OTHER` и `UNKNOWN` этим правилом не останавливаются.

## 8. HomeAssistantAdapter

Adapter является границей с Home Assistant и не содержит альтернативной модели системы.

Он:

- читает физические/управляющие entities;
- формирует `GeneratorObservation` и `PowerTransferObservation`;
- исполняет уже сформированные `GeneratorAction` / `TransferAction`;
- выполняет локальные аппаратные safety checks перед service call;
- публикует status, Logbook и critical notifications.

Важно: **одновременный RUNNING A и B разрешён**. Adapter не запрещает `REMOTE_ON` только из-за работы второго генератора. Аппаратная взаимная блокировка генераторных контакторов является частью физической схемы.

## 9. Один tick

Нормальный цикл имеет один направленный поток данных:

```text
HA snapshot
  -> GeneratorBusTracker
  -> GC/TPC observation refresh
  -> Supervisor.step()
  -> GC/TPC actions
  -> HA Adapter service calls
  -> status/log
  -> persistent journal
```

Policy не должна повторно выполняться из слоя публикации status/log.

## 10. Persistent journal 0.4

Top-level `schema_version = 2` относится только к текущему формату 0.4.

Journal содержит:

- `app_version`;
- состояние Supervisor;
- состояние `GeneratorBusTracker`;
- список `pending_actions`.

Отдельной вложенной schema-version Supervisor нет.

Состояние 0.3 не мигрируется. Неподдерживаемый или противоречивый journal переводит систему в безопасный `RECOVERY_REQUIRED`, а не угадывает прежний смысл данных.

`pending_actions` перед физическим service call нужны для определения restart/connection-loss в момент незавершённой операции.

## 11. Status sensor

`sensor.energy_ats_status` — диагностическая проекция текущего состояния, а не дополнительный источник истины.

Основные атрибуты текущей реализации:

- `source`;
- `phase`;
- `generator`, `generator_model`, `generator_slot`;
- `managed_generator`;
- `bus_owner`;
- `generator_a_run_context`, `generator_b_run_context`;
- `primary_generator`;
- `remaining_seconds`;
- `session_reason`;
- `fallback_used`;
- `armed`.

Отдельных `bus_owner_slot`, `primary_generator_slot` и status `schema_version` в 0.4 нет.

## 12. Safety-инварианты реализации

1. Неизвестное обязательное физическое состояние блокирует управляющие действия.
2. Команда никогда не считается подтверждением.
3. TPC соблюдает break-before-make независимо от policy Supervisor.
4. Два RUNNING — допустимый физический режим.
5. Нельзя угадывать bus owner при недостаточной истории.
6. Внешний RUNNING не становится managed автоматически.
7. Единственный fallback не превращается в ping-pong.
8. Остановка outage-related выполняется только после снятия дома с генераторной ветви.
9. Recovery не захватывает внешний генератор.
10. UI/log/persistence не должны сами продвигать управляющие FSM.

## 13. Проверка архитектуры

Изменение считается законченным только если одновременно согласованы:

```text
PHYSICAL_POWER_TOPOLOGY_RU.md
        ↓
REQUIREMENTS_RU.md
        ↓
production code
        ↓
unit / end-to-end tests
        ↓
commissioning на реальном оборудовании
```

Зелёный CI проверяет программную модель, но не заменяет физическую проверку контакторов, обратных связей и реального поведения DKG116/MAP.
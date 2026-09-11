# Energy ATS — архитектура

## 1. Назначение документа и источники истины

Этот документ описывает **как программно реализованы требования EnergyATS**. Он не является источником нового поведения и не расширяет разрешённые сценарии.

Источники истины разделены так:

1. `PHYSICAL_POWER_TOPOLOGY_RU.md` — что физически существует и как ведёт себя электроустановка;
2. `REQUIREMENTS_RU.md` — как EnergyATS обязан вести себя в наблюдаемой ситуации;
3. `ARCHITECTURE_RU.md` — какими программными компонентами это поведение реализовано;
4. `USER_TESTS_RU.md` и автоматические tests — как требования проверяются.

Если архитектура противоречит `REQUIREMENTS_RU.md`, исправляется архитектура и код, а не требование подгоняется под существующую реализацию.

Ключевой принцип этой архитектуры: **верхнеуровневое решение «что EnergyATS должна делать сейчас» принимается в одном месте — `EnergySupervisor`.** Остальные компоненты либо предоставляют ему локальные факты/условия, либо безопасно исполняют уже принятое решение.

---

## 2. Общая модель управления

EnergyATS — один Home Assistant App и один Python-процесс. Внутри него есть три разных типа компонентов, которые намеренно не смешиваются.

### 2.1. Системный Supervisor

`EnergySupervisor` — единственный компонент, который разрешает пересечения режимов и принимает общесистемные решения:

- manual request;
- реальный Grid outage;
- ожидание/charge cycling внутри outage;
- Scheduled Exercise при конфликте с более важной задачей;
- fallback;
- возврат Grid;
- Recovery.

Именно здесь реализуются системные требования `REQ-BEH-*`.

### 2.2. Специализированные подсистемы

Они знают только свою локальную область и **не являются конкурирующими верхнеуровневыми policy**:

- `ExerciseScheduler` — график, due/grace/warning, history и lifecycle maintenance-run;
- UPS Run subsystem — можно ли продолжать `UPS_ONLY`, когда пора запускать generator и когда cycle-owned run достиг Target SoC;
- `LoadManager` — G1/G2, admission, shedding и overload control.

Они передают Supervisor факты, желания или локальные результаты, но не решают конфликт `Manual vs Outage vs Exercise vs Recovery` самостоятельно.

### 2.3. Исполнительные автоматы

- `GeneratorController` — как безопасно запустить/прогреть/остановить конкретный двигатель;
- `PowerTransferController` — как безопасно переключить Grid / Generator с break-before-make.

Они не решают, **зачем** выполняется операция.

### 2.4. Composition и I/O

`main.py` собирает наблюдения, вызывает компоненты в определённом порядке и механически исполняет полученное решение. Он не содержит отдельного слоя бизнес-арбитража.

Допустимы `if` для технического dispatch, например «если Supervisor выдал handoff directive — вызвать соответствующий метод Scheduler». Недопустимо решать в `main.py`, **когда** outage важнее Exercise, должен ли Target SoC проиграть manual request или можно ли захватить уже работающий generator.

---

## 3. Модули и ответственность

| Модуль | Ответственность |
|---|---|
| `domain.py` | Общие доменные типы: A/B, `PowerSource`, `PowerPath`, причины managed-session и другие shared values |
| `generator_bus.py` | Фактический FIFO-owner общей generator bus и контекст непрерывных RUNNING |
| `generator_controller.py` | Lifecycle одного двигателя: choke, REMOTE, start, warmup, ready, cooldown, stop |
| `power_transfer.py` | Основные Grid/Generator контакторы и break-before-make |
| `energy_supervisor.py` | **Единая системная логика:** `REQ-BEH-*`, managed-session, manual/outage, fallback, return, Recovery и разрешение пересечений режимов |
| `exercise_scheduler.py` | Локальная логика Scheduled Exercise: schedule/history/warning/duration/result и ответственность за собственный auto-run до явного handoff |
| `ups_run.py` | UPS Run subsystem: оценка battery/TTG/time, ожидание в `UPS_ONLY`, условия начала/окончания charge cycle. Не является вторым Supervisor |
| `load_manager.py` | G1/G2: pre-transfer shedding, admission, continuous overload control, own-OFF ownership и локальный DEGRADED |
| `ha_adapter.py` | HA states -> observations, разрешённые service calls, safety checks и публикация runtime outputs |
| `main.py` | Composition root: snapshot, вызов компонентов, dispatch `SupervisorDecision`, journal/status/log |
| `state_store.py` | Атомарное сохранение persistent state |
| `ha_client.py` | WebSocket/REST transport Home Assistant и revisions входящих HA states |

Функциональная область и runtime-модуль называются одинаково: **UPS Run / `ups_run.py`**.

---

## 4. Доменные термины питания

### 4.1. `PowerSource`

Фактически наблюдаемый пользовательский режим питания дома:

- `GRID` — дом питается от основной сети;
- `GENERATOR` — дом подключён к общей generator bus;
- `UPS_ONLY` — обычная часть дома не получает Grid/Generator, но критическая UPS-линия может работать от МАП/АКБ;
- `NO_POWER` — питание отсутствует;
- `UNKNOWN` — состояние нельзя безопасно определить.

Нет отдельных силовых источников `BATTERY`, `GENERATOR_A` или `GENERATOR_B`: это не соответствует физической топологии.

### 4.2. `PowerPath`

Подтверждённое положение основной пары контакторов:

- `GRID`;
- `ISOLATED`;
- `GENERATOR`;
- `UNKNOWN`.

`PowerPath` и `PowerSource` различаются намеренно. Например, Grid path может быть выбран, но при физически отсутствующей Grid пользовательский source будет `UPS_ONLY`.

### 4.3. Managed session

Managed session означает, что EnergyATS явно принял ответственность за текущий generator-run: выбор slot, необходимость продолжать RUNNING, transfer дома, fallback и штатное завершение.

Причина session — как минимум manual start либо Grid outage. UPS Run не создаёт отдельный физический тип session: он определяет стратегию внутри automatic outage-session и может пометить её как cycle-owned.

Scheduled Exercise до явного handoff остаётся отдельным maintenance-run.

---

## 5. GeneratorBusTracker

`generator_bus.py` — единственное место, где определяется логический owner общей generator bus.

Owner:

- `A`;
- `B`;
- `NONE`;
- `UNKNOWN`.

Tracker повторяет аппаратное FIFO-поведение контакторов:

- первый появившийся RUNNING получает bus;
- второй RUNNING не отбирает bus;
- пока текущий owner продолжает RUNNING, owner не меняется;
- если owner остановился, а второй generator продолжает RUNNING, bus аппаратно переходит ко второму;
- если после restart/history gap оба уже RUNNING и порядок нельзя доказать, owner = `UNKNOWN`.

Ни Supervisor, ни TPC, ни Load Manager, ни HA Adapter не вычисляют owner повторно.

### 5.1. Контекст непрерывного RUNNING

Для каждого двигателя Tracker хранит run-context:

- `NONE`;
- `OUTAGE_RELATED`;
- `TEST_RUN`;
- `OTHER`;
- `UNKNOWN`.

Это **не ownership двигателя**. Контекст нужен для классификации непрерывного run и узких правил, например остановки outage-related run после стабильного возврата Grid.

Внешний `input_boolean.generator_test_mode` может классифицировать новый внешний RUNNING как `TEST_RUN`. Scheduled Exercise передаёт происхождение собственного test run напрямую и не зависит от helper-а.

---

## 6. GeneratorController

Один экземпляр GC обслуживает один физический generator slot и не знает про PRIMARY/SECONDARY либо причину, по которой Supervisor/Scheduler требует RUNNING.

Фазы:

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

GC выдаёт только локальные engine actions:

- `REMOTE_ON`;
- `REMOTE_OFF`;
- `CHOKE_TO_COLD_START`;
- `CHOKE_TO_RUN`.

GC может определить локальный `FAULT`, но не принимает системное решение о fallback, Exercise result или `RECOVERY_REQUIRED`.

`step_authorized_shutdown()` используется только после того, как верхний уровень уже разрешил остановить конкретный разгруженный двигатель.

При restart работающего scheduler-owned generator transient GC phase не обязана точно persist-иться. GC восстанавливается консервативно: не выдаёт повторный REMOTE START, безопасно нормализует choke и продолжает lifecycle из наблюдаемого физического состояния.

Имя, модель и Nominal/Maximum Power приходят через Home Assistant из Generator Controller. Power limits не относятся к engine lifecycle; их использует Load Manager по фактическому bus owner.

---

## 7. PowerTransferController

TPC управляет только основной парой Grid / Generator и ничего не знает о Generator A/B, причине session, Scheduled Exercise или допустимой мощности G1/G2.

Фазы:

- устойчивые: `STABLE_GRID`, `STABLE_ISOLATED`, `STABLE_GENERATOR`;
- переходные: `DISCONNECTING_GRID`, `SELECTING_GENERATOR`, `DISCONNECTING_GENERATOR`, `CONNECTING_GRID`;
- служебные: `WAITING_FOR_DATA`, `RECOVERY_REQUIRED`.

Grid -> Generator:

1. `grid_power -> OFF`;
2. подтвердить снятие Grid control feedback;
3. `use_generator_as_power_source -> ON`;
4. подтвердить Generator control feedback.

Generator -> Grid:

1. `use_generator_as_power_source -> OFF`;
2. подтвердить снятие Generator feedback;
3. `grid_power -> ON`;
4. подтвердить Grid feedback.

Каждый tick выдаёт не более одной новой силовой команды и не начинает следующий шаг до подтверждения предыдущего.

Обычный Scheduled Exercise TPC не использует: дом остаётся на Grid.

Load Manager не меняет FSM TPC. Для managed Grid -> Generator transfer он может только сообщить, завершено ли доступное pre-transfer shedding. Это execution gate, а не новое решение о desired source. Возврат Generator -> Grid Load Manager задерживать не имеет права.

---

## 8. EnergySupervisor — единый центр системных решений

`energy_supervisor.py` является **единственным владельцем верхнеуровневой поведенческой логики EnergyATS**. Он реализует прежде всего `REQ-BEH-01..18` и связанные core requirements.

Supervisor отвечает на вопросы:

- нужен ли сейчас managed generator-run;
- какой slot принадлежит текущей managed session;
- нужен ли один разрешённый fallback;
- какой source должен иметь дом;
- когда вернуть Grid;
- когда automatic charge cycle можно завершить;
- что делать при manual command во время outage/cycle/Exercise;
- можно ли принять already-running Exercise generator;
- когда обычное управление должно уступить Recovery.

### 8.1. Фазы Supervisor

- `WAITING_FOR_DATA`;
- `NORMAL`;
- `GRID_FAILURE_DELAY`;
- `STARTING_GENERATOR`;
- `ON_GENERATOR`;
- `RETURNING_TO_GRID`;
- `RETURNING_TO_UPS`;
- `EXTERNAL_RUNNING`;
- `RECOVERY_REQUIRED`.

Exercise и Load Manager не получают собственные фазы внутри Supervisor: их локальные FSM существуют отдельно.

### 8.2. Входы Supervisor

Supervisor получает единый набор наблюдений и локальных фактов:

- Grid/source/feedback state;
- состояния GC и фактический GeneratorBusOwner;
- manual start/stop requests;
- persisted managed session;
- состояние Recovery/E-stop;
- от Exercise Scheduler: active state, owned slot, desired running и факты, необходимые для handoff/defer;
- от UPS Run: можно ли ещё ждать в `UPS_ONLY`, требуется ли automatic start, принадлежит ли новая automatic session cycling, достигнут ли Target SoC и нужен ли новый post-cycle wait.

Scheduler и UPS Run не передают Supervisor готовое общесистемное решение вида «я победил другой режим». Они передают только факты/локальные условия своей области.

### 8.3. `SupervisorDecision`

Результат одного `step()` — единое решение для текущего tick. Оно должно содержать достаточно информации, чтобы `main.py` мог **механически**:

- передать TPC желаемый source;
- передать GC желаемый RUNNING/authorized shutdown;
- сообщить Exercise Scheduler о defer/handoff/failure/continuation, когда это требуется;
- сообщить UPS Run о начале нового post-cycle wait;
- опубликовать события/diagnostics.

Конкретная форма dataclass является implementation detail, но business conditions, определяющие эти директивы, находятся в Supervisor.

### 8.4. Требования `REQ-BEH-*` в коде

Каждая нетривиальная ветка Supervisor, реализующая системное решение, должна быть помечена requirement ID и кратким человеческим смыслом, например:

```python
# REQ-BEH-04 — Grid outage во время RUNNING Exercise.
# Не останавливаем пригодный уже работающий generator ради нового cold start;
# после подтверждённого outage явно принимаем тот же slot в managed session.
```

Комментарий нужен не для пересказа Python, а чтобы по коду было видно **почему** решение корректно.

### 8.5. Managed session

Session хранит как минимум:

- reason (`manual_generator_start` / `grid_outage`);
- managed slot;
- `grid_was_unavailable`;
- `stop_requested`;
- был ли использован единственный fallback;
- cycling ownership (`cycle_owned`);
- manual override;
- наблюдался ли внешний takeover после отказа managed generator.

Эти поля описывают ответственность и историю session. Они не создают второй decision layer.

### 8.6. Fallback

При отказе managed generator разрешён максимум один переход на другой slot:

- уже внешний SECONDARY автоматически не захватывается;
- свободный разрешённый SECONDARY может стать единственным managed fallback;
- после его отказа автоматического A -> B -> A нет;
- дальнейшее неоднозначное продолжение ведёт в Recovery.

### 8.7. Возврат Grid

После стабильной `grid_restore_stable_time` Supervisor требует `GRID`. TPC выполняет безопасный transfer. Только после подтверждённого снятия дома с generator bus Supervisor/authorized cleanup разрешает требуемые engine stops.

Load Manager не может задержать возврат Grid из-за meter/G1/G2 failure.

### 8.8. Exercise handoff

Grid outage или manual request во время already-running Exercise **не разрешаются Scheduler-ом**. Scheduler сообщает локальные факты, а Supervisor применяет `REQ-BEH-04..08`:

- unstarted Exercise может быть deferred;
- пригодный RUNNING Exercise-generator может быть явно принят в outage/manual managed session;
- до успешного handoff Scheduler сохраняет ответственность за stop собственного auto-run;
- неоднозначный handoff запрещён;
- Recovery не должен оставлять автоматически запущенный Exercise без ответственного за остановку.

### 8.9. UPS Run / Charge Cycling

UPS Run subsystem не выдаёт engine/TPC commands. Он вычисляет локальные условия battery strategy. Supervisor решает, что они означают для общей session.

В `RETURNING_TO_UPS` Supervisor требует изоляцию через TPC и только после подтверждённого снятия нагрузки разрешает GC cooldown/stop. Отказ stop требует Recovery без fallback.

Manual request во время cycle снимает automatic Target SoC stop через manual override. Устойчивый возврат Grid имеет приоритет над переходом Generator -> UPS_ONLY.

После подтверждённой cycle stop UPS Run начинает новый post-cycle wait. Следующий automatic start снова проходит обычный Supervisor/GC/LoadManager/TPC path.

---

## 9. UPS Run subsystem

Функциональность реализуется модулем `ups_run.py`; это специализированная подсистема длительного outage, а не самостоятельная верхнеуровневая policy.

Он отвечает только на локальные вопросы длительного outage:

- разрешено ли после `grid_failure_delay` продолжать `UPS_ONLY`;
- достигнут ли Start SoC;
- достигнут ли минимальный TTG;
- истёк ли max wait;
- батарейные данные валидны или нужен fail-safe generator start;
- достигнут ли Target SoC для cycle-owned session;
- когда начинается новый post-cycle wait.

Он **не** решает:

- какой generator выбрать;
- можно ли прервать Exercise;
- что делать с manual request;
- выполнять ли fallback;
- как переключить TPC;
- можно ли остановить external generator.

Эти решения принадлежат Supervisor.

UPS Run persist-ит только локальное состояние ожидания/цикла, необходимое для restart. Ownership managed generator session и manual override сохраняются Supervisor-ом.

Battery telemetry является soft dependency optimization. Stale/invalid данные отменяют оптимизацию ожидания и приводят к обычному safe generator path согласно requirements, а не к отказу core ATS.

---

## 10. ExerciseScheduler

`exercise_scheduler.py` — самостоятельная локальная FSM maintenance-run без Home Assistant dependency и без права разрешать общесистемные конфликты.

Для A/B независимо хранятся:

- `initial_reference_time`;
- `last_qualifying_run`;
- `last_window_date`;
- forced-warning state;
- последний physical exercise result/failure.

Одновременно существует максимум один `active_attempt`:

```text
STARTING -> RUNNING -> STOPPING
```

Результаты:

- `SUCCESS`;
- `FAILED`;
- `DEFERRED`;
- `INTERRUPTED_BY_OUTAGE`;
- при необходимости отдельный результат/причина прерывания manual handoff согласно требованиям.

`DEFERRED` — событие планировщика, а не physical test failure.

### 10.1. Scheduling

Для каждого slot независимо:

```text
next_due = reference + interval_days
forced_date = due_date + presence_grace_days
```

Start рассматривается только в собственном ежедневном `exercise_start_time`. Пропущенное окно не запускается позже в произвольное время.

До forced-date отсутствие семьи должно быть однозначно подтверждено. Начиная с forced-date presence больше не блокирует start, но safety/system prerequisites продолжают действовать.

Warning запрашивается заранее, а факт успешной delivery persist-ится с временем. Forced start проверяет требуемый lead.

### 10.2. Qualifying run и ответственность за stop

Scheduler наблюдает qualifying RUNNING независимо от причины запуска. Достоверный run достаточной длительности может обновить `last_qualifying_run`.

После физического старта собственного automatic Exercise Scheduler сохраняет ответственность за stop до одного из событий:

1. подтверждены `RUNNING=OFF` и `REMOTE=OFF`;
2. Supervisor явно передал этот уже RUNNING slot в другую managed session.

Простое изменение Grid state или наличие manual request само по себе ответственность не снимает. Handoff происходит только по явной директиве Supervisor.

Maintenance Exercise не создаёт fallback на второй slot.

---

## 11. LoadManager

`load_manager.py` — отдельный subsystem управления только G1/G2. Он получает `LoadManagerObservation` и не принимает решение о причине работы источника.

Master switch — `load_management_enabled`, default `false`. При выключенной функции Load Manager не создаёт G1/G2 actions, не является transfer gate и не деградирует из-за soft inputs.

### 11.1. Ownership состояния G1/G2

Приоритеты:

```text
restore/admission: G1 -> G2
overload shedding: G2 -> G1
```

`shed_by_energy_ats` хранится отдельно для каждой группы. Automatic ON разрешён только если текущий OFF был создан самим Load Manager. Пользовательский OFF не захватывается.

### 11.2. Pre-transfer shedding

Для managed Generator transfer preliminary shedding начинается после `READY_FOR_LOAD`, но до фактического TPC transfer.

Load Manager по одной обрабатывает доступные ON-группы и возвращает execution-факт `transfer_permitted`. Недоступная/unavailable группа создаёт локальный DEGRADED, но после локального timeout не блокирует core transfer бесконечно.

Если Grid вернулась до transfer, Supervisor выбирает Grid path, а собственные OFF восстанавливаются после подтверждённого Grid.

### 11.3. Measurement freshness

Power-based decisions разрешены только при известных:

- фактическом `GeneratorBusOwner`;
- корректных `0 < nominal <= maximum` owner;
- `generator_meter_status = ON`;
- свежем числовом `generator_power`;
- пригодных состояниях G1/G2.

`ha_client.py` ведёт revisions входящих state updates; Adapter передаёт revision power sample, чтобы Load Manager отличал новое измерение от повторного чтения cache.

После изменения нагрузки, restart, meter recovery или bus takeover начинается новое stabilization window. Для решения требуется несколько свежих samples.

### 11.4. Admission

После transfer измеряется base load. Следующая группа может быть добавлена только при restore margin:

```text
P <= nominal * (1 - restore_margin_percent / 100)
```

После ON группы выполняется новый stabilization cycle. Если новая установившаяся P > nominal, только что добавленная группа возвращается OFF; следующая в этом cycle не добавляется.

### 11.5. Continuous overload control

Load Manager работает всё время, пока дом подтверждённо на generator bus:

- `P <= nominal` — normal;
- `nominal < P <= maximum` — nominal overload timer;
- `P > maximum` — shorter maximum confirmation.

После confirmed overload снимается только одна группа, затем выполняется новое measurement. Если доступных groups больше нет, создаётся warning/critical event. Сам по себе overload Load Manager **не является основанием остановить generator**.

### 11.6. Soft dependencies и DEGRADED

Meter, power metadata и G1/G2 являются soft dependencies core ATS. Их failure:

- не блокирует App startup;
- не создаёт системный `RECOVERY_REQUIRED`;
- не останавливает generator;
- не блокирует возврат Grid.

`DEGRADED` принадлежит только Load Manager.

### 11.7. Bus takeover

При смене `GeneratorBusOwner` старые limits немедленно перестают использоваться. До корректных limits нового owner и нового stabilization новые power-based actions запрещены. UNKNOWN owner деградирует Load Manager, но не Supervisor.

---

## 12. HomeAssistantAdapter

Adapter:

- читает обязательные core physical/control entities;
- читает configurable presence entity;
- читает soft battery/UPS, Load Manager и power metadata inputs;
- формирует observations;
- выполняет разрешённые Generator/TPC actions;
- отдельно выполняет G1/G2 actions с локализацией ошибок;
- выполняет аппаратные safety checks;
- публикует status, Logbook и notifications.

Presence является soft Scheduler input. `unknown/unavailable` presence не блокирует core ATS, а только не позволяет обычный presence-gated Exercise.

Load Manager и UPS Run battery inputs намеренно не входят в обязательный core readiness set.

Одновременный RUNNING A и B допустим.

---

## 13. `main.py` и один tick

`main.py` — **composition root, а не второй Supervisor**.

Нормальный поток одного tick:

```text
HA snapshot
  -> GeneratorBusTracker
  -> refresh observations GC/TPC
  -> ExerciseScheduler.step()      # локальные schedule/lifecycle facts
  -> UPS Run step()                # локальные battery/wait/cycle facts
  -> EnergySupervisor.step(...)    # ЕДИНСТВЕННОЕ системное решение
  -> dispatch Supervisor directives to Exercise / UPS Run
  -> LoadManager.step(...)         # downstream G1/G2 execution constraints
  -> G1/G2 soft actions
  -> GC actions / authorized shutdown
  -> Load Manager execution gate for Grid -> Generator
  -> TPC actions
  -> HA Adapter service calls
  -> status / log / notifications
  -> persistence
```

Порядок вызова не даёт `main.py` права интерпретировать конфликт режимов. Все условия вида:

```text
Exercise + Outage
Exercise + Manual
Charge Cycle + Target SoC
Charge Cycle + Manual
Grid return + Target SoC
Recovery + Exercise
```

должны быть разрешены в `EnergySupervisor` и трассированы к `REQ-BEH-*`.

`main.py` может только исполнять результат: вызвать handoff/defer method, начать post-cycle wait, передать desired source/desired running и записать события.

Status/log/persistence не имеют права повторно выполнять управляющие FSM либо менять принятое решение.

---

## 14. Persistent state и journal

Persistent state хранит только данные, необходимые для безопасного restart и восстановления ownership/таймеров.

Сохраняются как минимум:

- `app_version`;
- Supervisor managed session/state;
- `GeneratorBusTracker`;
- `ExerciseScheduler`;
- `LoadManager`;
- UPS Run local state в ключе `ups_run`;
- core `pending_actions`.

Текущая schema persistent state — `3`. Миграция старых schema не выполняется: несовместимый state отклоняется явно.

Load Manager сохраняет own-OFF (`shed_by_energy_ats`), phase/reason, pending consumer state и restore retry data. Measurement samples не persist-ятся: после restart power-based decision доказывается новым stabilization window.

Повреждение soft Load Manager/UPS optimization state локализуется и не должно само по себе создавать core Recovery, если requirements позволяют безопасное fail-safe поведение.

Core `pending_actions` записываются до Generator/TPC service call и очищаются после завершения. Незавершённая core physical command после restart требует Recovery, если безопасное продолжение нельзя доказать.

G1/G2 soft actions не должны превращать consumer switch failure в блокировку core ATS.

---

## 15. Status sensor и наблюдаемость

`sensor.energy_ats_status` — диагностическая проекция, а не источник решений.

Core attributes включают source/phase/generator/model/managed/bus owner/run-context/PRIMARY/fallback/session/timers/armed.

Exercise публикует для A/B due/history/forced-warning/active/result state.

UPS Run публикует как минимум:

- enabled state Delayed Start / Charge Cycling;
- battery SoC/TTG validity;
- current UPS wait elapsed/remaining;
- reason ожидания либо запуска;
- cycle state / cycle ownership;
- Target/Start thresholds.

Load Manager публикует:

- enabled/phase;
- degraded reason;
- measured generator power;
- active nominal/maximum owner;
- G1/G2 state и `shed_by_energy_ats`;
- overload timers;
- next restore retry;
- last reason.

UI/log обязаны использовать реальные generator names, а не A/B там, где речь идёт о пользователе.

---

## 16. Safety-инварианты реализации

1. Неизвестное обязательное физическое состояние core ATS блокирует соответствующие управляющие действия.
2. Команда никогда не считается подтверждением.
3. TPC всегда соблюдает break-before-make независимо от причины transfer.
4. Два RUNNING generator допустимы.
5. Bus owner нельзя угадывать при недостаточной истории.
6. External RUNNING не становится managed автоматически.
7. Один fallback не превращается в A/B ping-pong.
8. Generator не останавливается под подтверждённой нагрузкой дома.
9. Scheduled Exercise не создаёт отдельный силовой путь и не имеет собственного fallback.
10. Автоматически запущенный Exercise-generator всегда имеет явного ответственного за будущий stop до подтверждённого handoff/stop.
11. UPS Run не получает права остановки manual/external/adopted run только из-за Target SoC.
12. Recovery имеет приоритет над обычной автоматизацией и не может быть обойдён локальной subsystem.
13. Presence failure не блокирует core ATS.
14. Выключенный Load Manager не участвует в G1/G2 control path.
15. Load Manager не управляет никакими loads кроме явно заданных G1/G2.
16. Meter/G1/G2/power metadata failure не создаёт core `RECOVERY_REQUIRED` сам по себе.
17. Load Manager не включает OFF-group без собственного подтверждённого ownership.
18. После изменения managed load следующее power-based решение требует нового stabilization window.
19. Generator power limits всегда относятся к фактическому bus owner.
20. Generator не останавливается автоматически только по overload Load Manager.
21. `main.py` не принимает бизнес-решения о пересечении режимов.
22. Каждая нетривиальная системная decision branch Supervisor трассируется к конкретному requirement.
23. UI/log/persistence не продвигают управляющие FSM.

---

## 17. Проверка архитектуры

Изменение считается законченным, когда согласованы:

```text
PHYSICAL_POWER_TOPOLOGY_RU.md
        ↓
REQUIREMENTS_RU.md
        ↓
ARCHITECTURE_RU.md
        ↓
production code
        ↓
unit / integration / end-to-end tests
        ↓
commissioning на реальном оборудовании
```

Для системных пересечений обязательна трассировка:

```text
REQ-BEH-* -> EnergySupervisor branch -> TC-BEH-* / integration scenario
```

Для локальных feature requirements аналогично используются соответствующие `REQ-*` и `TC-*` families.

Зелёный CI проверяет программную модель, но не заменяет физические испытания контакторов, generator bus, G1/G2, meter, генераторов, DKG116 и MAP.

# Плановый пробный запуск генераторов — требования (черновик)

Статус: **proposal / draft**.

Этот документ фиксирует требования к автоматическому периодическому пробному запуску каждого генератора после длительного простоя. До согласования он не заменяет `REQUIREMENTS_RU.md`; после финального review согласованные требования должны быть перенесены в основной нормативный документ.

## 1. Цель

EnergyATS должен периодически проверять работоспособность каждого генератора, если тот длительное время не имел полноценного успешного запуска.

Пробный запуск должен:

1. выполняться автоматически по индивидуальному графику конкретного генератора;
2. по возможности выполняться, когда дома никого нет;
3. не переводить дом с Grid на генератор только ради теста;
4. использовать штатный Generator Controller для REMOTE, choke, подтверждения RUNNING и остановки;
5. обеспечить непрерывную работу двигателя заданное время;
6. гарантированно завершиться контролируемой остановкой, если другой явно разрешённый сценарий EnergyATS не принял этот генератор под управление;
7. записать результат в журнал;
8. при неуспехе уведомить пользователя.

Generator A и Generator B имеют независимые настройки и независимую историю пробных запусков.

---

## 2. Термины

### 2.1. Exercise / пробный запуск

`exercise` — автоматически инициированный EnergyATS запуск конкретного генератора с целью проверки его работоспособности без намеренного перевода нагрузки дома на генераторную шину.

### 2.2. Qualifying run / засчитываемый запуск

Для определения длительного простоя учитывается не любая попытка пуска, а **успешный засчитываемый запуск**.

Запуск считается засчитываемым, если:

- RUNNING был подтверждён;
- двигатель непрерывно проработал не меньше настроенной `exercise_run_minutes` для данного генератора;
- за этот период не возник неустранённый fault двигателя.

Засчитываемым может быть:

- успешный scheduled exercise;
- реальная работа при outage;
- ручной managed-запуск;
- другой известный EnergyATS непрерывный run,

если он удовлетворяет тем же критериям длительности и исправности.

Неуспешная или слишком короткая попытка не сбрасывает счётчик длительного простоя.

### 2.3. Due date

`due date` — дата, начиная с которой генератор должен быть проверен exercise при первом допустимом дневном окне.

### 2.4. Presence grace period

После наступления due date EnergyATS может откладывать exercise ограниченное число дней, если дома присутствуют люди.

После окончания grace period присутствие перестаёт блокировать запуск. Все остальные safety-условия остаются обязательными.

### 2.5. Владелец автоматического запуска

Каждый генератор, автоматически запущенный EnergyATS, должен в каждый момент иметь явную текущую policy-причину продолжения работы.

Для scheduled exercise такой причиной является активный exercise. Если во время него реальный outage принимает генератор под управление, ownership передаётся outage-сессии явно.

**Автоматически запущенный генератор не должен оставаться RUNNING без активной причины продолжения работы и ответственного сценария остановки.**

---

## 3. Конфигурация

Настройки задаются **отдельно для каждого генератора**:

```text
exercise_enabled
exercise_interval_days
exercise_start_time
exercise_run_minutes
exercise_presence_grace_days
```

Пример:

```text
Generator A:
  interval = 30 days
  start_time = 15:00
  run = 10 min
  presence grace = 7 days

Generator B:
  interval = 45 days
  start_time = 14:00
  run = 15 min
  presence grace = 5 days
```

Время интерпретируется в локальной timezone Home Assistant.

Presence задаётся через HA-интерфейсный configurable parameter/entity (далее `family_presence_entity`). Конкретный `entity_id` не должен быть жёстко зашит в доменную модель EnergyATS.

Предупреждение перед forced exercise фиксировано для первой версии: **за 60 минут** до запуска. Отдельный per-generator параметр для lead time не требуется.

---

## 4. Вычисление следующего exercise

### REQ-EXERCISE-01. Независимый график

Для каждого генератора EnergyATS независимо хранит исходную точку/время последнего qualifying run и вычисляет следующий due:

```text
next_due = reference_run_time + exercise_interval_days
```

### REQ-EXERCISE-02. Новый генератор / отсутствие истории

Если EnergyATS впервые обнаружил ранее не сконфигурированный генератор и достоверной истории его запусков нет, прошлую дату запусков угадывать нельзя.

Начальной точкой scheduler-а считается момент, когда EnergyATS впервые успешно инициализировал этот генератор. Первый exercise становится due через `exercise_interval_days` от этой точки.

После первого qualifying run обычной точкой отсчёта становится фактическое время этого run.

### REQ-EXERCISE-03. Только ежедневное окно

После наступления due date EnergyATS рассматривает запуск один раз в сутки в настроенное `exercise_start_time`.

Если App не работал в момент окна, он не выполняет неожиданный catch-up запуск в произвольное время после старта. Следующая попытка — в следующее разрешённое дневное окно.

---

## 5. Проверка присутствия

### REQ-EXERCISE-04. Обычный запуск — только без людей дома

После due date, но до конца presence grace period, exercise разрешён только если `family_presence_entity` однозначно показывает отсутствие семьи дома.

### REQ-EXERCISE-05. Присутствие откладывает на сутки

Если в момент дневного окна кто-то дома, exercise не запускается. EnergyATS записывает `DEFERRED` с причиной presence и повторяет проверку в следующее дневное окно.

### REQ-EXERCISE-06. Повторная проверка непосредственно перед запуском

Перед физической командой запуска presence проверяется ещё раз.

Если grace period ещё не закончился и кто-то появился дома, запуск откладывается.

### REQ-EXERCISE-07. Forced exercise после grace period

Если за `exercise_presence_grace_days` не нашлось подходящего дня без людей дома, в следующем дневном окне exercise разрешается независимо от presence.

Слово `forced` отменяет **только presence restriction**. Оно не отменяет Grid prerequisites, E-stop, recovery, неизвестные состояния, другой активный сценарий или любые safety-блокировки.

### REQ-EXERCISE-08. Предупреждение за 60 минут

За 60 минут до forced exercise EnergyATS отправляет человеку понятную notification.

Notification должна содержать:

- реальное имя генератора;
- плановое время запуска;
- понятное объяснение, что это регулярный автоматический пробный запуск;
- плановую длительность работы;
- явное указание, что генератор после теста будет автоматически остановлен.

Пример:

> Пробный запуск генератора Elemax состоится сегодня в 15:00. Это регулярный тестовый пуск для проверки работоспособности генератора. После автоматического запуска генератор будет автоматически остановлен через 10 минут.

Не нужно объяснять пользователю внутреннюю механику presence/grace period.

Если в момент старта safety-условия не позволяют запуск, exercise не выполняется и переносится на следующее дневное окно.

---

## 6. Preconditions для физического запуска

### REQ-EXERCISE-09. Grid должна быть доступна

Новый exercise начинается только при:

```text
grid_input_ready = ON
```

и устойчивом штатном Grid path дома.

### REQ-EXERCISE-10. Дом остаётся на Grid

Во время обычного exercise целевым состоянием остаётся:

```text
switch.grid_power = ON
switch.use_generator_as_power_source = OFF
house_powered_by_grid = ON
house_powered_by_generator = OFF
```

Exercise сам по себе не использует TPC для перевода дома на generator bus.

### REQ-EXERCISE-11. Нет конфликтующей операции

Exercise не запускается, если:

- активна managed outage/manual session;
- выполняется Power Transfer transition;
- активен `RECOVERY_REQUIRED`;
- активен Generators Emergency Stop;
- обязательные физические состояния неизвестны;
- тестируемый генератор уже RUNNING или REMOTE ON;
- другой генератор участвует в managed operation;
- уже выполняется другой scheduled exercise.

### REQ-EXERCISE-12. Не запускать два scheduled exercise одновременно

Если дневные окна A и B конфликтуют, второй exercise переносится на следующее собственное дневное окно. Scheduler не сдвигает его на произвольное время в тот же день.

---

## 7. Последовательность exercise

### REQ-EXERCISE-13. Использовать штатный GC

Exercise использует обычный Generator Controller конкретного слота:

1. подготовка choke;
2. один REMOTE START;
3. ожидание RUNNING в штатный timeout;
4. штатная обработка choke после запуска;
5. контроль непрерывного RUNNING;
6. по окончании требуемого периода — штатная остановка;
7. REMOTE OFF;
8. подтверждение `RUNNING=OFF` и `REMOTE=OFF`.

DKG116 по-прежнему отвечает за собственные crank retries; scheduler не создаёт цикл REMOTE ON/OFF.

### REQ-EXERCISE-14. Длительность

`exercise_run_minutes` отсчитывается от первого подтверждённого RUNNING и означает минимальное требуемое время непрерывной работы двигателя.

Warmup/choke входят в физическое время RUNNING.

### REQ-EXERCISE-15. Никакого fallback

Failure exercise одного генератора не запускает второй генератор.

Scheduled exercise проверяет конкретный physical slot. Failure завершает только этот exercise попыткой безопасной остановки и пользовательской notification.

### REQ-EXERCISE-16. Обязательная остановка по завершении exercise

Когда configured exercise duration закончилась, EnergyATS обязан начать штатную остановку генератора, если к этому моменту другой явно разрешённый сценарий EnergyATS **не принял данный генератор под управление**.

Истечение exercise duration не может просто удалить scheduler-state и оставить двигатель RUNNING.

---

## 8. Результат и журнал

### REQ-EXERCISE-17. SUCCESS

Exercise считается `SUCCESS`, если:

- RUNNING подтверждён;
- двигатель непрерывно проработал не меньше configured duration;
- не возник fault, делающий результат недостоверным;
- exercise сохранил ownership до штатной остановки;
- остановка завершилась подтверждёнными `RUNNING=OFF` и `REMOTE=OFF`.

После SUCCESS обновляется `last_qualifying_run` и вычисляется следующий due date.

### REQ-EXERCISE-18. FAILED

Физически начатый exercise считается `FAILED`, если, например:

- RUNNING не подтвердился в start timeout;
- двигатель остановился раньше required duration;
- GC получил fault;
- штатная остановка не подтвердилась в stop timeout;
- состояние стало неоднозначным и потребовало recovery.

FAILED не обновляет `last_qualifying_run`.

### REQ-EXERCISE-19. DEFERRED не является failure

Exercise, который не был физически начат из-за presence или safety prerequisites, считается `DEFERRED`, а не `FAILED`.

### REQ-EXERCISE-20. TAKEN_OVER_BY_OUTAGE

Если во время active exercise реальный Grid outage действительно принимает уже работающий генератор как managed source для питания дома, scheduled exercise прекращает владеть двигателем.

Такой exercise фиксируется как `TAKEN_OVER_BY_OUTAGE` (или эквивалентное явно различимое состояние), а не как SUCCESS/FAILED.

После handoff дальнейшая продолжительность работы и остановка принадлежат outage policy.

### REQ-EXERCISE-21. Журнал результата

Для каждой фактической попытки сохраняются как минимум:

```text
generator
scheduled_time
actual_start_time
actual_end_time
result: SUCCESS | FAILED | TAKEN_OVER_BY_OUTAGE
required_run_minutes
actual_run_seconds
forced_after_presence_grace: true | false
failure_reason (если есть)
```

DEFERRED события журналируются отдельно с причиной.

### REQ-EXERCISE-22. Notification при failure

При `FAILED` EnergyATS отправляет notification с реальным именем генератора и краткой человеко-читаемой причиной.

SUCCESS достаточно записать в журнал; обязательная notification при успехе не требуется.

---

## 9. Взаимодействие с TEST_RUN

### REQ-EXERCISE-23. Scheduled exercise является TEST_RUN по смыслу outage cleanup

GeneratorBusTracker должен знать, что непрерывный RUNNING, созданный scheduler-ом, является тестовым и не должен ошибочно стать `OUTAGE_RELATED` только из-за последующего исчезновения Grid.

Для bus context достаточно существующего `TEST_RUN`; отдельный новый run-context не требуется.

### REQ-EXERCISE-24. Helper `generator_test_mode` остаётся внешним маркером

`input_boolean.generator_test_mode` продолжает означать: **пометить новый внешний OFF -> ON RUNNING как TEST_RUN**.

Scheduled exercise не включает этот helper и не зависит от него: происхождение собственного запуска EnergyATS известно непосредственно scheduler-у.

Существующее правило «TEST_RUN не останавливается outage-cleanup только из-за возврата Grid» не отменяет обязанность scheduler-а остановить **свой** exercise после configured duration.

---

## 10. Потеря Grid во время exercise

### REQ-EXERCISE-25. Outage имеет приоритет

Если во время exercise `grid_input_ready` становится OFF, обычная outage policy получает приоритет в принятии решения о питании дома.

Сам по себе факт исчезновения Grid ещё не означает автоматический handoff ownership двигателя от scheduler-а.

### REQ-EXERCISE-26. Явный handoff в outage session

Если outage policy реально принимает уже запущенный exercise-generator как managed source и начинает использовать его для outage-сессии, ownership двигателя явно переходит от exercise к outage session.

В этом случае:

- не выполняется бессмысленный `REMOTE OFF -> новый холодный запуск` того же генератора;
- exercise фиксируется как `TAKEN_OVER_BY_OUTAGE`;
- exercise timer больше не имеет права остановить двигатель;
- дальнейшая остановка выполняется только по правилам outage session.

### REQ-EXERCISE-27. Если handoff не произошёл — exercise обязан остановить двигатель

Если Grid исчезла, но outage policy **не приняла** exercise-generator под управление, scheduler сохраняет ответственность за него.

Например, если Grid кратковременно исчезла и восстановилась до реального перевода дома на generator bus, exercise продолжает свой контролируемый цикл и по истечении configured duration обязан штатно остановить генератор.

Не допускается состояние, при котором transient Grid event отменил exercise shutdown и оставил автоматически запущенный генератор работать без ограничения времени.

### REQ-EXERCISE-28. Реальная outage policy важнее exercise deadline после handoff

Если handoff уже произошёл и дом питается от этого генератора, окончание исходных `exercise_run_minutes` **не является основанием остановить генератор под нагрузкой**.

---

## 11. Persistence и restart

### REQ-EXERCISE-29. Scheduler state переживает restart

Для каждого generator slot сохраняются данные, достаточные для восстановления:

- first-seen/reference time;
- last qualifying run;
- next due date либо данные для её вычисления;
- overdue/grace state;
- active exercise attempt;
- actual exercise start time;
- planned minimum run-until time;
- был ли отправлен forced warning;
- текущий result/ownership state.

### REQ-EXERCISE-30. Restart во время exercise

После restart EnergyATS не должен повторно подавать REMOTE START вслепую.

Если persisted scheduler-state и физические сигналы однозначно подтверждают активный exercise, EnergyATS восстанавливает ownership и доводит его до штатной остановки либо корректного outage handoff.

Если ownership/следующее безопасное действие доказать нельзя — `RECOVERY_REQUIRED`.

Restart не может приводить к бесконтрольной длительной работе автоматически запущенного генератора.

---

## 12. Минимальные сценарные тесты

1. Новый Generator A впервые обнаружен -> first due = first-seen + interval.
2. Generator A due, дома никого нет -> exercise SUCCESS.
3. Generator B имеет независимый schedule -> запускается по собственному времени.
4. Presence ON в due day -> DEFERRED.
5. Presence OFF на следующий день -> exercise выполняется.
6. Presence ON все grace days -> warning за 60 минут -> forced exercise.
7. Warning содержит имя, время, назначение теста, длительность и обещание автоматической остановки.
8. Кто-то вернулся непосредственно перед обычным не-forced start -> DEFERRED.
9. Forced start игнорирует только presence, но блокируется E-stop/recovery/неизвестными данными.
10. PRIMARY/SECONDARY роли не влияют на individual exercise schedule.
11. A exercise failure -> B автоматически не запускается.
12. Неуспешный start -> FAILED + notification + due остаётся просроченным.
13. Unexpected stop до duration -> FAILED.
14. Stop timeout -> FAILED/recovery.
15. Успешный normal outage run достаточной duration сбрасывает exercise interval.
16. Короткая попытка не сбрасывает interval.
17. Два exercise windows конфликтуют -> не запускаются одновременно.
18. App offline в scheduled time -> нет catch-up вне следующего дневного окна.
19. Restart до scheduled time сохраняет schedule/grace state.
20. Restart во время active exercise не создаёт второй REMOTE START и сохраняет обязательство остановки.
21. Scheduled exercise при штатной Grid не переключает дом на generator bus.
22. Краткий Grid outage без handoff -> exercise всё равно заканчивается штатной остановкой.
23. Grid outage с реальным handoff -> exercise = TAKEN_OVER_BY_OUTAGE, двигатель продолжает работать по outage policy.
24. После outage handoff exercise deadline не останавливает генератор под нагрузкой.
25. TEST_RUN semantics не позволяют обычному outage cleanup преждевременно остановить active exercise.

---

## 13. Изменения в существующем `REQUIREMENTS_RU.md` после согласования

`PHYSICAL_POWER_TOPOLOGY_RU.md` менять не требуется: новых физических устройств, датчиков и силовых путей нет.

В основном requirements документе потребуется:

1. добавить maintenance/exercise scheduling как отдельную policy-ответственность, не смешивая её с TPC;
2. расширить определение `TEST_RUN`: он может быть внешне помеченным helper-ом или внутренне созданным scheduled exercise;
3. уточнить REQ-OUTRUN-04: outage cleanup не останавливает TEST_RUN только по причине возврата Grid, но scheduler обязан штатно завершить собственный exercise;
4. добавить раздел `Плановый пробный запуск генераторов` с REQ-EXERCISE-*;
5. явно установить, что обычный exercise не является power-transfer session и при штатной Grid не переключает house source;
6. добавить явный ownership/handoff `exercise -> outage`;
7. добавить инвариант: автоматически запущенный генератор не остаётся RUNNING без текущей policy-причины и ответственного сценария остановки;
8. расширить persistence/restart scheduler-state;
9. добавить status/log: last result, last qualifying run, next due, overdue/grace/active exercise state;
10. расширить минимальные сценарные тесты exercise cases.

---

## 14. Зафиксированные решения после review

1. **Inactivity timer.** Любой qualifying run достаточной длительности сбрасывает interval независимо от причины запуска.
2. **Новый генератор без истории.** Начальная точка — first-seen/инициализация EnergyATS; первый due наступает через configured interval.
3. **Grid outage во время exercise.** Уже запущенный генератор может быть явно передан outage session; если handoff не произошёл, scheduler обязан остановить его по завершении exercise.
4. **Forced warning.** Lead time фиксирован: 60 минут.
5. **Presence.** Источник presence задаётся через HA-интерфейсный configurable entity/parameter, concrete entity_id не зашивается в EnergyATS.
6. **Notification text.** Пользователь получает простое объяснение назначения теста, времени, длительности и автоматической остановки без описания внутренней presence/grace логики.

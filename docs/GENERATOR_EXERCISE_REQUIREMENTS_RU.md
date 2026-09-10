# Плановый пробный запуск генераторов — требования (черновик)

Статус: **proposal / draft**.

Этот документ фиксирует предлагаемое расширение EnergyATS для автоматического периодического пробного запуска каждого генератора после длительного простоя. До согласования он не заменяет `REQUIREMENTS_RU.md`; после ревью утверждённые пункты должны быть перенесены в основной нормативный документ.

## 1. Цель

EnergyATS должен периодически проверять работоспособность каждого генератора, если тот длительное время не имел полноценного успешного запуска.

Пробный запуск должен:

1. выполняться автоматически по индивидуальному графику конкретного генератора;
2. по возможности выполняться, когда дома никого нет;
3. не переводить дом с Grid на генератор только ради теста;
4. использовать штатный Generator Controller для REMOTE, choke, подтверждения RUNNING и остановки;
5. обеспечить непрерывную работу двигателя заданное время;
6. завершиться контролируемой остановкой;
7. записать результат в журнал;
8. при неуспехе уведомить пользователя.

Generator A и Generator B имеют независимые настройки и независимую историю пробных запусков.

## 2. Термины

### 2.1. Exercise / пробный запуск

`exercise` — автоматически инициированный EnergyATS запуск конкретного генератора с целью проверки его работоспособности без намеренного перевода нагрузки дома на генераторную шину.

### 2.2. Qualifying run / засчитываемый запуск

Для определения длительного простоя учитывается не любая кратковременная попытка пуска, а **успешный засчитываемый запуск**.

Предлагаемое правило: запуск считается засчитываемым, если:

- RUNNING был подтверждён;
- двигатель непрерывно проработал не меньше настроенной длительности exercise для данного генератора;
- за этот период не возник неустранённый fault двигателя.

Засчитываемым может быть не только плановый exercise, но и обычная реальная работа генератора при outage или ручной managed-запуск, если она удовлетворяет тем же условиям.

Неуспешная попытка exercise не должна сбрасывать счётчик длительного простоя.

### 2.3. Due date

`due date` — момент, когда с последнего засчитываемого запуска прошло настроенное количество дней простоя.

### 2.4. Grace period по присутствию

После наступления due date EnergyATS может откладывать exercise на ограниченное количество дней, если дома присутствуют люди.

По истечении grace period присутствие людей больше не является причиной откладывать test; остальные safety-условия продолжают действовать.

## 3. Конфигурация

Настройки задаются **отдельно для каждого генератора**.

Минимальный набор:

```text
exercise_enabled
exercise_interval_days
exercise_start_time
exercise_run_minutes
exercise_presence_grace_days
```

Для A и B значения независимы.

Пример смысла:

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

Общий вход присутствия семьи должен быть явно сконфигурирован как HA entity (далее `family_presence_entity`). Конкретный entity_id не должен быть жёстко зашит в доменную модель.

Предупреждение перед принудительным запуском: за **60 минут** до планового старта. Если позже потребуется, lead time можно сделать отдельным параметром; в первой версии достаточно фиксированного значения 60 минут.

## 4. Вычисление момента следующего exercise

### REQ-EXERCISE-01. Независимый график

Для каждого генератора EnergyATS независимо хранит время последнего засчитываемого запуска и вычисляет:

```text
next_due = last_qualifying_run + exercise_interval_days
```

### REQ-EXERCISE-02. Новый генератор / отсутствие истории

Если достоверной истории последнего засчитываемого запуска нет, EnergyATS не должен угадывать дату. Такое состояние должно быть явно наблюдаемым и требовать либо начальной точки, либо первого контролируемого exercise.

### REQ-EXERCISE-03. Только ежедневное окно

После наступления due date EnergyATS рассматривает запуск один раз в сутки в настроенное `exercise_start_time`.

Если App не работал в момент окна, он не должен неожиданно выполнять «догоняющий» запуск в произвольное время после старта App. Следующая попытка — в очередное разрешённое дневное окно.

## 5. Проверка присутствия

### REQ-EXERCISE-04. Обычный запуск — только без людей дома

После due date, но до конца grace period, exercise разрешён только если `family_presence_entity` однозначно показывает отсутствие семьи дома.

### REQ-EXERCISE-05. Присутствие откладывает на сутки

Если в момент дневного окна кто-то дома, exercise не запускается. EnergyATS записывает факт defer и повторяет проверку в следующее дневное окно.

### REQ-EXERCISE-06. Повторная проверка непосредственно перед запуском

Если отсутствие было подтверждено заранее, перед реальной командой запуска присутствие проверяется ещё раз. Если до истечения grace period кто-то появился дома — запуск снова откладывается.

### REQ-EXERCISE-07. Принудительный запуск после grace period

Если за `exercise_presence_grace_days` не нашлось подходящего дня без людей дома, в следующем дневном окне exercise разрешается независимо от presence.

Слово «принудительный» отменяет только ограничение по presence. Оно **не отменяет safety-блокировки**, Grid prerequisites, E-stop, recovery, неизвестные состояния или конфликт с другой активной операцией.

### REQ-EXERCISE-08. Предупреждение перед принудительным запуском

За 60 минут до принудительного запуска EnergyATS должен отправить пользователю предупреждающую notification с:

- именем генератора;
- плановым временем запуска;
- причиной: просроченный exercise после grace period;
- указанием, что запуск будет выполнен даже при присутствии людей, если safety-условия останутся допустимыми.

Если в момент старта safety-условия не позволяют запуск, тест не выполняется и переносится на следующее дневное окно.

## 6. Preconditions для физического запуска

### REQ-EXERCISE-09. Grid должна быть доступна

Новый exercise можно начинать только при:

```text
grid_input_ready = ON
```

и устойчивом штатном Grid path дома.

Пробный запуск сам по себе не должен отключать Grid или переводить дом на generator bus.

### REQ-EXERCISE-10. Дом остаётся на Grid

Во время обычного exercise:

```text
switch.grid_power = ON
switch.use_generator_as_power_source = OFF
house_powered_by_grid = ON
house_powered_by_generator = OFF
```

должны оставаться целевым состоянием.

EnergyATS не использует TPC для перевода нагрузки только ради exercise.

### REQ-EXERCISE-11. Нет другой активной силовой операции

Exercise не запускается, если:

- активна managed outage/manual session;
- выполняется Power Transfer transition;
- активен `RECOVERY_REQUIRED`;
- активен Generators Emergency Stop;
- обязательные физические состояния неизвестны;
- тестируемый генератор уже RUNNING или REMOTE ON;
- другой генератор уже участвует в managed operation.

### REQ-EXERCISE-12. Не запускать два scheduled exercise одновременно

Два плановых exercise не должны выполняться одновременно.

Если окна A и B конфликтуют, второй exercise откладывается до следующего собственного дневного окна. Scheduler не должен самостоятельно сдвигать его на произвольное время в тот же день.

## 7. Последовательность exercise

### REQ-EXERCISE-13. Использовать штатный GC

Exercise должен использовать обычный Generator Controller конкретного слота:

1. подготовка choke;
2. один REMOTE START;
3. ожидание RUNNING в штатный timeout;
4. штатная обработка choke после запуска;
5. контроль непрерывного RUNNING;
6. штатная остановка через REMOTE OFF;
7. подтверждение RUNNING OFF и REMOTE OFF.

DKG116 по-прежнему отвечает за собственные crank retries; scheduler не создаёт цикл REMOTE ON/OFF.

### REQ-EXERCISE-14. Длительность

`exercise_run_minutes` отсчитывается от первого подтверждённого RUNNING и означает минимальное требуемое время непрерывной работы двигателя.

Warmup/choke входят в физическое время RUNNING, но тест не считается успешным, пока двигатель не проработал весь configured duration.

### REQ-EXERCISE-15. Никакого fallback

Отказ одного генератора во время его exercise не должен автоматически запускать второй генератор.

Scheduled exercise проверяет именно конкретный generator slot. Его failure завершает только этот exercise попыткой безопасной остановки и пользовательской notification.

## 8. Результат и журнал

### REQ-EXERCISE-16. SUCCESS

Exercise считается `SUCCESS`, если:

- запуск подтверждён RUNNING;
- двигатель непрерывно проработал не меньше configured duration;
- не возникло fault, делающего результат недостоверным;
- штатная остановка завершилась подтверждёнными `RUNNING=OFF` и `REMOTE=OFF`.

После SUCCESS обновляется `last_qualifying_run` этого генератора и вычисляется следующий due date.

### REQ-EXERCISE-17. FAILED

Реально начатый exercise считается `FAILED`, если, например:

- RUNNING не подтвердился в start timeout;
- двигатель неожиданно остановился до требуемой duration;
- GC получил fault;
- штатная остановка не подтвердилась в stop timeout;
- выполнение стало неоднозначным и потребовало recovery.

FAILED не обновляет `last_qualifying_run`.

### REQ-EXERCISE-18. DEFERRED не является failure

Exercise, который не был физически начат из-за presence или safety prerequisites, считается `DEFERRED`, а не `FAILED`.

### REQ-EXERCISE-19. Журнал результата

Для каждой фактической попытки должны сохраняться как минимум:

```text
generator
scheduled_time
actual_start_time
actual_end_time
result: SUCCESS | FAILED
required_run_minutes
actual_run_seconds
forced_after_presence_grace: true | false
failure_reason (если есть)
```

DEFERRED события также журналируются с причиной, но не считаются результатом физического теста.

### REQ-EXERCISE-20. Notification при failure

При `FAILED` EnergyATS должен отправить пользователю notification с реальным именем генератора и краткой причиной failure.

SUCCESS достаточно записать в журнал; обязательная пользовательская notification при успехе не требуется.

## 9. Взаимодействие с TEST_RUN

### REQ-EXERCISE-21. Scheduled exercise является TEST_RUN по смыслу outage cleanup

Генератор, запущенный scheduler-ом для exercise, должен быть явно известен EnergyATS как тестовый запуск и не должен ошибочно классифицироваться как `OUTAGE_RELATED` только из-за последующих изменений Grid.

Для GeneratorBusTracker такой непрерывный RUNNING может использовать context `TEST_RUN`; отдельный новый bus run-context не требуется, если ownership exercise хранится в scheduler/session state.

### REQ-EXERCISE-22. Helper `generator_test_mode` остаётся внешним маркером

`input_boolean.generator_test_mode` продолжает означать: **пометить новый внешний OFF->ON RUNNING как TEST_RUN**.

Scheduled exercise не должен требовать включения этого helper-а: EnergyATS сам знает происхождение собственного запуска.

## 10. Потеря Grid во время exercise

Этот сценарий требует явной policy, потому что Grid может исчезнуть в любой момент.

### Предлагаемое правило для первой версии

### REQ-EXERCISE-23. Outage имеет приоритет

Если `grid_input_ready` становится OFF во время exercise, scheduler прекращает быть главным владельцем policy. Обычная outage-логика EnergyATS получает приоритет.

Поскольку работающий генератор был запущен самим EnergyATS и его происхождение известно, он **не должен считаться неизвестным внешним запуском**.

Если генератор уже RUNNING/готов, предпочтительно использовать его как текущий managed generator для outage вместо бессмысленного `REMOTE OFF -> новый холодный запуск PRIMARY`.

Если безопасно продолжить существующий запуск невозможно, система должна перейти в обычную outage/recovery policy без угадывания.

Exercise в таком случае записывается как `INTERRUPTED_BY_OUTAGE`, а не SUCCESS/FAILED, и сам по себе не должен создавать failure notification.

**Этот пункт предлагается отдельно подтвердить перед реализацией**, потому что он определяет переход между maintenance policy и аварийным ATS-сценарием.

## 11. Persistence и restart

### REQ-EXERCISE-24. Scheduler state переживает restart

Должны сохраняться независимо для A/B:

- last qualifying run;
- next due date либо данные, достаточные для её вычисления;
- факт просрочки/grace period;
- активная exercise attempt;
- был ли уже отправлен forced-warning;
- текущий result state.

### REQ-EXERCISE-25. Restart во время exercise

После restart EnergyATS не должен повторно подавать REMOTE START вслепую.

Если по persisted state и физическим сигналам можно однозначно восстановить активный exercise, он может быть безопасно продолжен/завершён. Если нет — `RECOVERY_REQUIRED`.

## 12. Минимальные сценарные тесты нового функционала

1. Generator A due, дома никого нет -> exercise SUCCESS.
2. Generator B имеет другой independent schedule -> exercise по собственному времени.
3. Presence ON в due day -> DEFERRED.
4. Presence OFF на следующий день -> exercise выполняется.
5. Presence ON все grace days -> warning за 60 минут -> forced exercise.
6. Кто-то вернулся домой непосредственно перед обычным, не forced start -> DEFERRED.
7. Forced start не отменяется одним лишь presence, но блокируется E-stop/recovery/неизвестными данными.
8. PRIMARY/SECONDARY роли не влияют на individual exercise schedule.
9. A exercise failure -> B автоматически не запускается.
10. Неуспешный start -> FAILED + notification + due остаётся просроченным.
11. Unexpected stop до duration -> FAILED.
12. Stop timeout -> FAILED/recovery.
13. Успешный normal outage run достаточной duration сбрасывает exercise due date.
14. Короткая неуспешная попытка не сбрасывает due date.
15. Два exercise windows конфликтуют -> не запускаются одновременно.
16. App offline в scheduled time -> нет неожиданного catch-up вне окна.
17. Restart до scheduled time сохраняет schedule/grace state.
18. Restart во время active exercise не создаёт второй REMOTE START.
19. Scheduled exercise не переключает дом с Grid на generator bus.
20. Grid outage во время exercise -> поведение по REQ-EXERCISE-23.

## 13. Изменения в существующем `REQUIREMENTS_RU.md` после согласования

Физический документ `PHYSICAL_POWER_TOPOLOGY_RU.md` менять не требуется: новых контакторов, датчиков или силовых путей нет.

В основном requirements документе потребуется:

1. в разделе логических уровней добавить ответственность за maintenance/exercise scheduling, не смешивая её с TPC;
2. расширить определение `TEST_RUN`: он может быть внешне помеченным helper-ом или внутренне созданным scheduled exercise;
3. уточнить REQ-OUTRUN-04: запрет outage-cleanup для TEST_RUN не запрещает scheduler-у штатно остановить **свой** exercise после configured duration;
4. добавить отдельный раздел `Плановый пробный запуск генераторов` с требованиями REQ-EXERCISE-*;
5. явно установить, что exercise не является outage/manual managed power session и не должен переключать house source при нормальной Grid;
6. добавить policy взаимодействия `exercise -> real Grid outage`;
7. расширить persistence/restart требования состоянием scheduler-а;
8. добавить новые status/log требования: last exercise result, last qualifying run, next due, overdue/forced state — точный внешний contract определить после согласования requirements;
9. расширить минимальные сценарные тесты новым набором exercise cases.

## 14. Вопросы, которые стоит подтвердить до кодирования

1. **Что именно сбрасывает месячный таймер?** В этом draft принято: любой успешный непрерывный run длительностью не меньше configured exercise duration, независимо от причины запуска.
2. **Grid outage во время exercise.** В draft предлагается не останавливать исправный уже запущенный двигатель только ради перехода в outage, а передать его обычной ATS policy как известный managed source.
3. **Forced warning.** В draft 60 минут фиксированы; при необходимости можно сделать per-generator параметром.
4. **Presence entity.** Предлагается общий configurable HA entity, а не жёстко заданный `group.*`.

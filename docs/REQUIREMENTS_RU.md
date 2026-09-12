# Требования к реализации EnergyATS

## 1. Назначение и статус документа

Этот документ определяет **требуемое поведение EnergyATS** для фактически существующей электрической схемы дома.

Источники истины разделены намеренно:

- `PHYSICAL_POWER_TOPOLOGY_RU.md` — что физически существует и как ведёт себя электроустановка;
- `REQUIREMENTS_RU.md` — как EnergyATS обязан вести себя, наблюдая и управляя этой схемой;
- `ARCHITECTURE_RU.md` — какими программными компонентами реализованы требования;
- `USER_TESTS_RU.md` — как пользователь физически проверяет систему;
- автоматические tests — исполняемая проверка требований, но не источник самих требований.

Код не является источником требований. Архитектурный документ не может расширять разрешённое поведение системы по сравнению с этим документом.

Идентификаторы `REQ-*` являются стабильными ссылками. Перемещение требования между разделами не должно менять его ID без содержательной причины.

---

## 2. Основные принципы

### REQ-PRINCIPLE-01. Запрещено всё, что явно не разрешено

EnergyATS не должен выполнять аппаратные действия или автоматически переходить в сценарии, которые явно не разрешены требованиями. Если безопасное действие нельзя однозначно вывести из требований, активное управление прекращается, наблюдаемость сохраняется, а пользователю сообщается причина.

### REQ-PRINCIPLE-02. Физическая схема первична

Если программная модель противоречит физическому поведению оборудования, исправляется программная модель.

### REQ-PRINCIPLE-03. Команда и подтверждение — разные факты

Состояние управляющего реле означает требуемое состояние управляющей схемы. Успешный HA service call сам по себе не является подтверждением физического переключения.

### REQ-PRINCIPLE-04. Не угадывать неоднозначную физическую ситуацию

Если по доступным данным невозможно однозначно определить состояние, существенное для безопасного управления, EnergyATS не должен его угадывать.

### REQ-PRINCIPLE-05. Аппаратные блокировки являются частью модели

Аппаратные взаимные блокировки основных контакторов `Сеть / Генератор` и генераторных контакторов `A / B` являются частью реальной схемы. Штатное состояние, допускаемое физической схемой, не должно объявляться программной аварией.

### REQ-PRINCIPLE-06. Никакого незапланированного зацикливания

После неожиданного отказа EnergyATS не должен бесконечно повторять запуск, fallback или силовое переключение. Автоматическая цепочка fallback в одной managed-сессии ограничена одним переходом `PRIMARY -> SECONDARY`, если отдельным требованием явно не задано иное.

### REQ-PRINCIPLE-07. Внешний RUNNING не означает право управления

Сам факт `RUNNING` генератора не даёт EnergyATS права управлять его REMOTE, заслонкой или остановкой. Явные исключения, например завершение outage-related run после устойчивого восстановления Grid, должны быть отдельно разрешены требованиями.

### REQ-PRINCIPLE-08. Автоматически запущенный двигатель всегда имеет ответственного

Если EnergyATS автоматически запустил генератор, в каждый момент дальнейшей работы должна существовать однозначная причина продолжения RUNNING и сценарий EnergyATS, ответственный за последующую остановку.

Нельзя допускать состояние, при котором автоматически начатый сценарий завершился или потерял ответственность, а двигатель продолжает работать без другого явно принявшего эту ответственность сценария. Передача ответственности должна быть явной; пока она не состоялась, исходный сценарий остаётся ответственным за безопасное завершение собственного запуска.

---

## 3. Модель системы и термины

### 3.1. Пользовательские состояния питания

EnergyATS различает:

- `GRID` — обычная часть дома питается от Grid;
- `GENERATOR <name>` — дом подключён к генераторной шине, её физический owner известен;
- `GENERATOR UNKNOWN` — generator bus выбрана, но owner нельзя достоверно установить;
- `UPS_ONLY` — обычная часть дома не получает внешний источник, критическая UPS-линия продолжает работать от МАП/АКБ;
- `NO_POWER` — есть основания считать, что питание отсутствует полностью;
- `UNKNOWN` — данных недостаточно либо они противоречивы.

Пользовательское русское описание `UPS_ONLY`: **«В доме работает только UPS линия»**.

Scheduled Exercise при штатной Grid не меняет источник питания дома: даже если генератор физически RUNNING без подключения generator bus к дому, пользовательский источник остаётся `GRID`.

### 3.2. Managed session

`Managed session` — работа генератора, за которую EnergyATS явно принял ответственность: выбор генератора, необходимость продолжать RUNNING, transfer дома, fallback и штатное завершение.

Основные причины managed session:

- ручной запрос пользователя;
- реальный Grid outage;
- outage-session, выполняющая автоматический UPS Run / charge cycle.

Scheduled Exercise до явной передачи ответственности в power-session остаётся отдельным maintenance-run.

### 3.3. External run

Generator считается внешне работающим, если он RUNNING, но его запуск не принадлежит текущей managed session и не принадлежит активному Scheduled Exercise. Внешний RUNNING не становится managed только из-за того, что физически получил generator bus.

### 3.4. Generator bus

Generator A и B могут одновременно быть RUNNING. Общая generator bus физически имеет только одного owner благодаря аппаратной блокировке контакторов.

### 3.5. UPS Run

**UPS Run** — функциональная логика длительного outage: сколько можно оставаться в `UPS_ONLY`, когда нужен generator для подзаряда/питания и когда автоматически созданный charge cycle можно завершить по Target SoC.

UPS Run не управляет МАП как отдельным силовым контактором и не создаёт нового физического пути. Он только влияет на решение, требуется ли generator в текущем outage.

### 3.6. Scheduled Exercise

Scheduled Exercise — maintenance-сценарий периодического пробного запуска конкретного generator slot. Он определяет due/grace/warning/duration/result, но не определяет общесистемное поведение при конфликте с outage или ручной командой — такие пересечения описаны в разделе 4.

### 3.7. Load Management

Load Manager управляет только явно заданными некритичными группами `G1/G2`. Он не решает, нужен ли generator, не выбирает A/B и не создаёт managed session. Его degraded state не является системным Recovery.

### 3.8. Разделение ответственности реализации

Требования этого документа описывают поведение EnergyATS, а не структуру Python-классов. Архитектура должна обеспечивать следующую границу:

- верхнеуровневое решение «что должна делать EnergyATS сейчас» принимается в одном месте;
- жизненный цикл отдельного двигателя отвечает за безопасное выполнение start/warmup/cooldown/stop;
- переключение Grid/Generator отвечает за безопасную силовую коммутацию;
- Exercise, UPS Run и Load Management предоставляют свои локальные факты/условия, но не создают конкурирующие общесистемные центры принятия решения.

---

## 4. Верхнеуровневое поведение EnergyATS

Этот раздел является основным описанием **пересечения режимов**. Feature-разделы ниже задают локальные детали, но не должны переопределять эти правила.

Базовый порядок решений:

```text
Safety / Recovery
        -> явная команда пользователя
        -> реальная необходимость электроснабжения при Grid outage
        -> UPS Run как стратегия внутри outage
        -> Scheduled Exercise
```

Этот порядок не означает произвольный «захват» уже работающего двигателя. Если generator уже RUNNING, EnergyATS обязан явно решить судьбу существующего run: продолжить его, передать ответственность либо безопасно завершить.

### REQ-BEH-01. Обычная потеря Grid

**Ситуация:** Grid физически пропала, специальный сценарий не активен.

**Решение:** EnergyATS сначала выдерживает `grid_failure_delay`. После этого либо остаётся в `UPS_ONLY`, если UPS Run разрешает ожидание, либо начинает обычную managed outage-session.

**Зависит от:** разрешения АВР, безопасности системы, состояния генераторов и решения UPS Run о допустимости дальнейшей работы от батареи.

### REQ-BEH-02. Ручной запрос во время ожидания на UPS

**Ситуация:** Grid отсутствует, EnergyATS намеренно ждёт в `UPS_ONLY`, а пользователь запрашивает резервное питание.

**Решение:** автоматическое ожидание немедленно прекращается и начинается ручная managed session. Пользователь не должен ждать SoC/TTG/max-delay.

### REQ-BEH-03. Exercise ещё не начался, появилась более важная задача

**Ситуация:** Exercise due, но двигатель ещё физически не начал Scheduled Exercise; одновременно появился manual request, outage, Recovery или другая блокирующая системная задача.

**Решение:** Exercise не стартует и фиксируется как отложенный. Более важная задача обрабатывается обычным системным сценарием.

### REQ-BEH-04. Grid outage во время работающего Exercise

**Ситуация:** Exercise-generator уже подтверждённо RUNNING, после чего Grid пропала.

**Решение:** EnergyATS не должен бессмысленно останавливать исправный generator ради нового холодного запуска. После обычного подтверждения outage тот же generator может быть явно принят в managed outage-session, если его состояние позволяет безопасно продолжить работу.

**Если передача не состоялась:** Scheduled Exercise сохраняет ответственность за собственный run и обязан его штатно завершить по своим правилам.

### REQ-BEH-05. Неоднозначный Exercise -> Outage переход

**Ситуация:** Grid пропала, пока Exercise находится в неподтверждённой start/stop-фазе либо состояние не позволяет доказать безопасную передачу ответственности.

**Решение:** запрещены duplicate REMOTE, новый холодный запуск поверх существующей операции и угадывание ownership. Текущий безопасно подтверждаемый шаг доводится до однозначного состояния; если безопасное продолжение не доказуемо, используется Recovery.

### REQ-BEH-06. Manual request до физического старта Exercise

**Ситуация:** Scheduled Exercise должен начаться, но пользователь до фактического запуска generator запросил резервное питание.

**Решение:** Exercise откладывается; начинается обычная manual managed session.

### REQ-BEH-07. Manual request во время RUNNING Exercise

**Ситуация:** Exercise-generator уже RUNNING, а пользователь явно запросил переход дома на резервное питание.

**Решение:** если существующий run исправен и передача ответственности однозначна, EnergyATS использует уже работающий generator вместо REMOTE OFF и нового запуска. Scheduled Exercise передаёт дальнейшую ответственность manual managed session; maintenance-attempt фиксируется как прерванный ручным использованием, а не как техническая неисправность.

Если безопасная передача не доказуема, EnergyATS не захватывает run молча и применяет обычные safety/recovery правила.

### REQ-BEH-08. Recovery во время Exercise

**Ситуация:** системный Recovery стал необходим во время автоматически начатого Exercise.

**Решение:** новые старты и силовые переходы запрещаются. При этом автоматически запущенный exercise-generator не должен потерять владельца обязанности остановки: EnergyATS сохраняет явную ответственность за его безопасное завершение при первой допустимой возможности.

### REQ-BEH-09. Восстановление Grid во время generator supply

**Ситуация:** дом питается от generator, Grid появилась.

**Решение:** первое появление Grid не означает немедленный transfer. После непрерывной `grid_restore_stable_time` EnergyATS возвращает дом на Grid и только после подтверждённого снятия нагрузки разрешает штатную остановку соответствующих generator runs.

### REQ-BEH-10. Grid снова пропала во время возврата

**Ситуация:** возврат Generator -> Grid уже начался либо готовится, но Grid снова пропала.

**Решение:** EnergyATS не разворачивает неподтверждённую физическую операцию вслепую. Текущий безопасный шаг доводится до подтверждённого состояния; если управляемый generator ещё доступен, он повторно используется без ненужного холодного запуска.

### REQ-BEH-11. Target SoC во время автоматического charge cycle

**Ситуация:** продолжается outage, текущая generator session однозначно принадлежит UPS Run, достигнут Target SoC.

**Решение:** если нет manual override или иной причины продолжать generator supply, EnergyATS безопасно снимает дом с generator bus, переходит в `UPS_ONLY` и штатно останавливает generator.

### REQ-BEH-12. Target SoC и восстановление Grid

**Ситуация:** Target SoC достигнут примерно одновременно с устойчивым возвратом Grid.

**Решение:** возврат на реальную Grid имеет приоритет над началом очередного UPS-only периода. Не требуется сначала выполнять Generator -> UPS_ONLY, чтобы сразу после этого перейти на Grid.

### REQ-BEH-13. Manual override автоматического charge cycle

**Ситуация:** generator работает в автоматическом outage charge cycle, пользователь явно запрашивает оставить/использовать generator.

**Решение:** автоматическая остановка по Target SoC отменяется. Текущая session становится ручной по смыслу управления и продолжается до явной manual stop либо другого разрешённого основания, например устойчивого возврата Grid.

### REQ-BEH-14. Ручная остановка во время продолжающегося outage

**Ситуация:** Grid всё ещё отсутствует, но пользователь явно запросил завершить managed generator session.

**Решение:** EnergyATS снимает дом с generator supply, штатно останавливает управляемый generator и остаётся на доступном `UPS_ONLY`. Автоматический повторный запуск подавляется до восстановления Grid либо до нового явного manual start.

Если для выполнения этой команды **сам EnergyATS** отключил/оставил отключённым Grid path, обязанность вернуть сетевой ввод не исчезает после завершения generator session и должна переживать restart App. После непрерывной `grid_restore_stable_time` EnergyATS возвращает Grid path и снимает эту обязанность только после физического подтверждения Grid path. Это право относится только к изоляции, принадлежащей EnergyATS; произвольный пользовательский или внешний `grid_power=OFF` автоматически не отменяется.

### REQ-BEH-15. Отказ managed generator

**Ситуация:** выбранный managed generator не запустился либо отказал во время session.

**Решение:** разрешён максимум один managed fallback на другой slot, если он свободен и доступен. Автоматический ping-pong A->B->A запрещён.

### REQ-BEH-16. SECONDARY уже работает внешне

**Ситуация:** managed generator отказал, а второй generator уже RUNNING независимо от EnergyATS.

**Решение:** физическая схема может автоматически передать ему bus, но EnergyATS не превращает внешний generator в managed только по этому факту и не получает право управлять его REMOTE/stop.

### REQ-BEH-17. Неоднозначное физическое состояние

**Ситуация:** owner, силовая топология, feedback либо незавершённая транзакция не позволяют однозначно определить безопасное продолжение.

**Решение:** обычное автоматическое управление прекращается и используется `RECOVERY_REQUIRED`. Exercise, UPS Run и Load Manager не имеют права обойти Recovery.

### REQ-BEH-18. Load Manager не меняет причину работы источника

**Ситуация:** Load Manager выполняет shedding/admission, находится в DEGRADED либо не может измерить нагрузку.

**Решение:** это не создаёт новую generator session и не отменяет решение о необходимости generator/Grid. Load Manager ограничивает только управляемые нагрузки; основная ATS-логика продолжает работу по своим требованиям.

### 4.1. Тест-кейсы пересечения режимов

Каждый тест описывает **что проверяется**, а не способ реализации.

- `TC-BEH-01` — manual request во время UPS-only ожидания немедленно отменяет Delayed Start и начинает manual managed session.
- `TC-BEH-02` — manual request до физического старта Exercise откладывает Exercise и не создаёт два конкурирующих запуска.
- `TC-BEH-03` — Grid outage во время пригодного RUNNING Exercise использует тот же generator без OFF/new cold start.
- `TC-BEH-04` — если Exercise -> Outage handoff не состоялся, Scheduler не теряет обязанность остановить собственный generator.
- `TC-BEH-05` — manual request во время RUNNING Exercise использует существующий исправный run и передаёт ответственность manual session без перезапуска.
- `TC-BEH-06` — Recovery во время Exercise запрещает новые действия, но не оставляет автоматически запущенный двигатель бесконтрольным.
- `TC-BEH-07` — Grid restore во время cycle-owned session имеет приоритет над Target SoC/UPS-only transition.
- `TC-BEH-08` — manual override charge cycle отменяет автоматическую остановку по Target SoC.
- `TC-BEH-09` — manual stop при продолжающемся outage завершает session, подавляет automatic restart и, если Grid path был изолирован самим EnergyATS, после stable Grid восстанавливает именно этот собственный ввод даже после restart App; чужое намеренное отключение Grid не отменяется.
- `TC-BEH-10` — Load Manager DEGRADED не меняет решение Supervisor о generator/Grid и не создаёт системный Recovery.

---

## 5. Grid, managed session и ручное управление

### 5.1. Grid

#### REQ-GRID-01. Штатная Grid

Штатное состояние:

```text
grid_input_ready = ON
grid_power = ON
use_generator_as_power_source = OFF
house_powered_by_grid = ON
house_powered_by_generator = OFF
```

#### REQ-GRID-02. Намеренное отключение Grid

`grid_input_ready = ON` при `grid_power = OFF` означает исправную внешнюю Grid, намеренно отключённую управляющей схемой. Это не является физическим outage и само по себе не запускает automatic outage-session. Исключение — сохраняемая EnergyATS-owned обязанность восстановления после REQ-BEH-14: она не превращает произвольный `grid_power=OFF` в право auto-reconnect.

#### REQ-GRID-03. Outage определяется физическим входом

Automatic outage начинается только по `binary_sensor.grid_input_ready = OFF`.

#### REQ-GRID-04. UPS появляется автоматически

При исчезновении входного AC МАП автоматически переводит UPS-линию на АКБ. EnergyATS не формирует `connect_battery` и не моделирует отдельный Battery contactor.

#### REQ-GRID-05. Observation gap разрывает непрерывность Grid

Любой таймер, смысл которого требует **непрерывно наблюдаемого** состояния Grid (`grid_failure_delay`, `grid_restore_stable_time` и аналогичные safety delays), считается доказанным только при непрерывной последовательности валидных observations. Потеря связи с Home Assistant, `unknown/unavailable` обязательного Grid input либо другой observation gap сбрасывает уже накопленную непрерывность; после восстановления валидных данных отсчёт начинается заново. Истёкшее wall-clock время внутри gap само по себе не доказывает стабильность Grid.

### 5.2. Автоматический outage start

#### REQ-AUTO-01. Grid failure delay

При `grid_input_ready = OFF` и разрешённом АВР EnergyATS выдерживает конфигурируемый `grid_failure_delay` (текущий default 60 s). Непрерывность этого интервала определяется REQ-GRID-05.

#### REQ-AUTO-02. Состояние во время задержки

До появления внешнего источника обычная часть дома остаётся без обычного питания, а критическая UPS-линия работает от МАП/АКБ; наблюдаемое состояние — `UPS_ONLY`.

#### REQ-AUTO-03. Выбор generator slot

Новая managed outage-session использует текущий configured PRIMARY, если slot разрешён и отсутствует уже работающий EnergyATS generator, который по REQ-BEH-04 явно передаётся из Exercise.

#### REQ-AUTO-04. Один уровневый REMOTE START

EnergyATS подаёт один уровневый REMOTE START выбранному slot. Внутренние crank retries выполняет DKG116; EnergyATS не создаёт собственный цикл REMOTE ON/OFF.

#### REQ-AUTO-05. Transfer только после готовности

Дом не переводится на generator bus до подтверждённых RUNNING и готовности managed generator. Если Load Manager включён, допустимый pre-transfer shedding выполняется после готовности generator и непосредственно перед transfer.

#### REQ-AUTO-06. Break-before-make

Переход Grid -> Generator выполняется только последовательностью с подтверждениями: снять Grid control, подтвердить снятие, выбрать generator source, подтвердить generator feedback. Недоступность generator meter не блокирует core transfer.

### 5.3. Manual Start

#### REQ-MANUAL-01. Ручная команда создаёт managed session

При отсутствии уже принятого подходящего EnergyATS run manual command создаёт managed session текущего PRIMARY. Пересечения с RUNNING Exercise определяются REQ-BEH-07.

#### REQ-MANUAL-02. Исправная Grid не отменяет manual session

Manual generator session может существовать при физически доступной Grid до явного завершения пользователем.

#### REQ-MANUAL-03. Transfer после готовности

Manual session переводит дом на generator только после успешного запуска и готовности generator; к ней применяются те же безопасные transfer и Load Management prerequisites.

#### REQ-MANUAL-04. Повторная команда не создаёт вторую session

Повторный manual start при уже активной managed session не создаёт параллельную managed session.

### 5.4. Возврат Grid

#### REQ-RETURN-01. Stable time

После `grid_input_ready = ON` outage-session ждёт непрерывную `grid_restore_stable_time` (default 60 s). Непрерывность этого интервала определяется REQ-GRID-05; reconnect Home Assistant не позволяет засчитать время, в течение которого состояние Grid не наблюдалось.

#### REQ-RETURN-02. До окончания stable time сохраняется доступный источник

Если generator supply уже существует и остаётся исправным, дом не снимается с него по кратковременному появлению Grid.

#### REQ-RETURN-03. Break-before-make при возврате

Generator -> Grid выполняется безопасно: снять generator branch, подтвердить, выбрать Grid side/разрешить Grid, подтвердить Grid feedback.

#### REQ-RETURN-04. Generator stop после снятия нагрузки

После подтверждённого Grid path останавливаются только те managed/outage-related runs, на остановку которых EnergyATS имеет право. `TEST_RUN` не останавливается только по факту Grid restore.

#### REQ-RETURN-05. Load restore после Grid

Группы, отключённые Load Manager, восстанавливаются только после подтверждённого Grid path.

### 5.5. Manual Stop

#### REQ-STOP-01. Сначала снять дом с generator branch

Штатная manual stop не должна начинать остановку управляемого двигателя под нагрузкой дома.

#### REQ-STOP-02. Не останавливать managed generator под нагрузкой

REMOTE OFF управляемого generator разрешается только после подтверждённого снятия соответствующей нагрузки/источника согласно текущему сценарию.

#### REQ-STOP-03. Cooldown и подтверждённая остановка

После снятия нагрузки выполняются штатный cooldown и REMOTE OFF; завершение требует подтверждённых `RUNNING=OFF` и `REMOTE=OFF`. Если после подтверждённого снятия нагрузки managed generator получает stop fault либо не подтверждает остановку в установленный lifecycle timeout, текущий сценарий завершается системным Recovery/critical indication. Запуск SECONDARY как fallback для ошибки остановки после уже выполненного возврата дома на Grid запрещён.

#### REQ-STOP-04. Manual stop не расширяет право на внешний generator

Остановка managed session сама по себе не даёт права остановить внешний generator, если он отдельно не подпадает под разрешённое outage-related cleanup.

### 5.6. Тест-кейсы Grid / managed session

- `TC-CORE-01` (legacy #1) — штатная Grid распознаётся как нормальный `GRID`, без generator-команд.
- `TC-CORE-02` (legacy #2) — физическая потеря Grid запускает именно outage-путь после требуемой задержки.
- `TC-CORE-03` (legacy #3) — `grid_power = OFF` при физически исправной Grid не считается outage.
- `TC-CORE-04` (legacy #4) — при отсутствии внешнего источника корректно наблюдается `UPS_ONLY` без вымышленного battery contactor.
- `TC-CORE-05` (legacy #5) — automatic outage запускает PRIMARY и переводит дом только после готовности.
- `TC-CORE-06` (legacy #6) — manual start создаёт managed PRIMARY session и выполняет штатный transfer.
- `TC-CORE-07` (legacy #7) — после устойчивого Grid restore дом сначала возвращается на Grid, затем generator штатно останавливается.
- `TC-CORE-21` (legacy #21) — повторная потеря Grid во время возврата не приводит к слепому развороту незавершённой силовой операции.
- `TC-CORE-24` — после подтверждённого возврата manual session на Grid stop fault managed generator переводит систему в Recovery без fallback на второй generator.
- `TC-CORE-27` — observation gap/reconnect разрывает накопленную непрерывность Grid; `grid_failure_delay`/`grid_restore_stable_time` после восстановления валидных observations доказываются заново.

---

## 6. Generator bus, external runs и fallback

### 6.1. Generator bus

#### REQ-BUS-01. Два RUNNING допустимы

`A RUNNING = ON` и `B RUNNING = ON` одновременно — штатно допустимое состояние и не fault.

#### REQ-BUS-02. Owner bus один

Если работает один generator, он owner. Если RUNNING оба, owner остаётся generator, первым реально создавший напряжение и втянувший свой аппаратно заблокированный contactor.

#### REQ-BUS-03. Одновременное владение bus невозможно

Аппаратная электрическая/механическая блокировка исключает одновременное подключение A и B к общей bus.

#### REQ-BUS-04. Второй RUNNING не отбирает bus

Позже запустившийся generator не меняет owner, пока текущий owner способен удерживать bus.

#### REQ-BUS-05. Аппаратный takeover

Если текущий owner остановился, а второй generator уже RUNNING, второй физически получает bus без команды EnergyATS.

#### REQ-BUS-06. Логический owner

EnergyATS различает `A | B | NONE | UNKNOWN`. Persisted owner/context может сохраняться для диагностики journal, но **restart или observation gap не являются доказательством непрерывности FIFO-истории**. Runtime owner после такого разрыва восстанавливается только из новых однозначных observations.

#### REQ-BUS-07. Не угадывать owner после потери истории

Если после history gap/restart оба generator уже RUNNING и достоверный порядок нельзя восстановить, owner = `UNKNOWN`.

#### REQ-BUS-08. Отдельная нагрузка SECONDARY вне области EnergyATS

Нагрузка резервного электрокотла на отдельной линии второго generator не меняет ownership generator bus дома и не управляется EnergyATS.

### 6.2. External runs

#### REQ-EXT-01. Определение external run

Generator, который RUNNING, но не принадлежит current managed session и не принадлежит active Scheduled Exercise, считается external.

#### REQ-EXT-02. По умолчанию не управлять external generator

EnergyATS не управляет REMOTE/choke/stop внешнего generator без отдельного явно разрешённого требования.

#### REQ-EXT-03. External SECONDARY во время managed session допустим

Сам факт второго RUNNING не является нарушением interlock, не требует изоляции bus и не даёт права остановить внешний generator.

#### REQ-EXT-04. Получение bus не создаёт managed ownership

External SECONDARY, аппаратно получивший bus после остановки первого generator, остаётся external по происхождению запуска.

### 6.3. Automatic fallback

#### REQ-FALLBACK-01. PRIMARY не запустился

Если managed PRIMARY не подтвердил успешный start/ready, а SECONDARY свободен и разрешён, EnergyATS выполняет один fallback на SECONDARY.

#### REQ-FALLBACK-02. PRIMARY отказал до устойчивой нагрузки

Если managed PRIMARY неожиданно остановился до устойчивого generator supply, а SECONDARY свободен и разрешён, допускается один fallback.

#### REQ-FALLBACK-03. PRIMARY отказал под нагрузкой

Если PRIMARY был managed owner bus и отказал, свободный SECONDARY может стать единственным managed fallback. Уже работающий внешний SECONDARY обрабатывается по REQ-FALLBACK-05.

#### REQ-FALLBACK-04. Без ping-pong

После использования fallback второй отказ не вызывает автоматический возврат к предыдущему generator. Требуется Recovery/решение пользователя.

#### REQ-FALLBACK-05. Уже внешний SECONDARY нельзя захватывать

Если SECONDARY уже RUNNING external при отказе PRIMARY, EnergyATS учитывает возможный аппаратный bus takeover, но не включает его REMOTE и не записывает его как managed fallback.

### 6.4. Run context

#### REQ-OUTRUN-01. Классификация run после restart консервативна

Для каждого продолжающегося run должно быть возможно различить как минимум `OUTAGE_RELATED`, `TEST_RUN`, `OTHER`, `UNKNOWN`. Restart/observation gap, после которого происхождение непрерывного RUNNING нельзя доказать новыми observations, переводит его context в `UNKNOWN`; persisted context сам по себе не восстанавливает право управления или cleanup.

#### REQ-OUTRUN-02. Stable Grid завершает outage-related runs

После устойчивого Grid restore и подтверждённого снятия дома с generator bus EnergyATS завершает все продолжающиеся outage-related runs, на которые распространяется это правило, с нормальным cooldown.

#### REQ-OUTRUN-03. Ограниченное право остановки external outage-related generator

Cleanup внешнего outage-related generator после устойчивого Grid restore — явное исключение из REQ-EXT-02. Оно не превращает этот generator в managed и не даёт иных прав управления.

#### REQ-OUTRUN-04. TEST_RUN не останавливается только из-за Grid restore

TEST_RUN не должен прекращаться только потому, что Grid стала доступна. Scheduled Exercise может отдельно завершить собственный TEST_RUN по maintenance-причине.

#### REQ-OUTRUN-05. `generator_test_mode` — только внешний маркер

HA helper `input_boolean.generator_test_mode` маркирует новый внешний OFF->ON RUNNING как TEST_RUN. Scheduled Exercise знает происхождение собственного запуска напрямую и не обязан переключать этот helper.

### 6.5. Тест-кейсы bus / external / fallback

- `TC-CORE-08` (legacy #8) — одновременный RUNNING A/B не считается fault, owner остаётся однозначным по FIFO-истории.
- `TC-CORE-09` (legacy #9) — external SECONDARY может запуститься во время managed session без автоматического захвата EnergyATS.
- `TC-CORE-10` (legacy #10) — после остановки PRIMARY уже работающий external SECONDARY аппаратно получает bus, но остаётся external.
- `TC-CORE-11` (legacy #11) — PRIMARY не стартует, SECONDARY свободен: выполняется один managed fallback.
- `TC-CORE-12` (legacy #12) — PRIMARY отказывает во время session, SECONDARY свободен: выполняется один fallback.
- `TC-CORE-13` (legacy #13) — SECONDARY после fallback также отказывает: Recovery без A->B->A ping-pong.
- `TC-CORE-14` (legacy #14) — restart при двух уже RUNNING генераторах, даже если прежний owner сохранён в journal, разрывает доказанную FIFO-историю и даёт `UNKNOWN` до новой однозначной наблюдаемой истории.
- `TC-CORE-15` (legacy #15) — restart при двух RUNNING без достоверного owner даёт `UNKNOWN`, а не угадывание.
- `TC-CORE-16` (legacy #16) — внешний outage-related generator после stable Grid корректно завершается разрешённым cleanup.
- `TC-CORE-17` (legacy #17) — два outage-related generator после stable Grid оба корректно завершаются, если подпадают под cleanup.
- `TC-CORE-18` (legacy #18) — внешний TEST_RUN не останавливается только из-за возврата Grid.
- `TC-CORE-25` — после потери напряжения generator bus при всё ещё подтверждённо ON selector разрешён только безопасный break/de-select; это не должно преждевременно сорвать допустимый медленный fallback SECONDARY.

---

## 7. UPS Run: длительный outage и charge cycling

### 7.1. Назначение

UPS Run предназначен для двух связанных решений:

1. не запускать generator сразу после `grid_failure_delay`, если UPS/АКБ позволяют разумно продолжить `UPS_ONLY`;
2. при многосуточном outage периодически запускать generator для подзаряда, а после Target SoC снова возвращаться к `UPS_ONLY`.

UPS Run не дублирует start/stop lifecycle generator, силовой transfer, fallback или Load Manager.

### 7.2. Delayed Start

#### REQ-DELAY-01. Delayed Start opt-in

`delayed_generator_start_enabled = false` по умолчанию. При `false` после `grid_failure_delay` используется обычный automatic generator start.

#### REQ-DELAY-02. UPS Run не управляет МАП

МАП сам поддерживает UPS-линию от АКБ. EnergyATS только наблюдает батарейные данные и описывает состояние как `UPS_ONLY`.

#### REQ-DELAY-03. Когда можно ждать в UPS_ONLY

После `grid_failure_delay` ожидание разрешено только если Delayed Start включён, Grid всё ещё отсутствует, батарейные данные пригодны, нет critical battery state и пользователь не потребовал generator сейчас.

#### REQ-DELAY-04. Условия automatic start

Во время UPS-only wait generator требуется, если выполняется хотя бы одно условие:

```text
battery_soc <= generator_start_soc
OR battery_ttg <= generator_min_ttg_before_start
OR current_ups_wait >= generator_max_start_delay_hours
```

Для следующего charge cycle новый wait начинается после завершения предыдущего cycle и возврата в `UPS_ONLY`.

#### REQ-DELAY-05. Fail-safe при плохих battery data

Delayed Start является оптимизацией, а не hard dependency core ATS. Если необходимые battery data отсутствуют, stale, `unknown/unavailable` либо явно некорректны, бесконечно ждать запрещено: используется обычный safe managed generator start.

#### REQ-DELAY-06. Critical battery немедленно отменяет ожидание

Critical battery state требует начать обычный generator start без ожидания SoC/TTG/max-delay, если запуск не запрещён более высоким safety-state.

#### REQ-DELAY-07. TTG учитывается только при реальном разряде

TTG участвует в решении только при реальном discharge и конечном числовом значении. Зарядка/float/бесконечный/неосмысленный TTG не должен создавать ложный start condition.

#### REQ-DELAY-08. Grid restore во время ожидания

Если Grid устойчиво восстановилась до generator start, UPS-only wait прекращается и generator не запускается только ради завершения Delayed Start.

#### REQ-DELAY-09. Manual request имеет приоритет

Manual reserve request во время UPS-only wait немедленно прекращает автоматическую задержку согласно REQ-BEH-02.

### 7.3. Charge Cycling

#### REQ-CYCLE-01. Cycling разрешается отдельно

`generator_charge_cycle_enabled = false` по умолчанию. Delayed Start и Charge Cycling — независимые настройки.

#### REQ-CYCLE-02. Target SoC

При включённом cycling задаётся `generator_target_charge_soc`. Target сам по себе не даёт права остановить любой RUNNING generator.

#### REQ-CYCLE-03. Корректность порогов

Конфигурация должна удовлетворять `0 < generator_start_soc < generator_target_charge_soc <= 100`. Ошибка этих значений отключает именно UPS Run optimization, но не должна делать core ATS неработоспособным.

#### REQ-CYCLE-04. Stop по Target только для cycle-owned session

Автоматическая остановка по Target разрешена только для managed outage-session, происхождение и продолжающаяся ответственность UPS Run однозначно известны. Manual/external/Exercise run не становится cycle-owned только из-за подходящих SoC/TTG.

#### REQ-CYCLE-05. Manual override

Manual request во время UPS wait/charge cycle имеет приоритет: Target SoC больше не является основанием автоматически завершить текущую user-controlled session.

#### REQ-CYCLE-06. Завершение automatic charge cycle

При продолжающемся outage cycle-owned session после Target SoC и при отсутствии manual override безопасно проходит Generator -> UPS_ONLY, затем cooldown/stop и возвращается к UPS wait.

#### REQ-CYCLE-07. Следующий cycle использует те же start criteria

После automatic stop следующий generator start снова определяется REQ-DELAY-04, а не отдельным shortcut.

#### REQ-CYCLE-08. Stable Grid важнее Target

Grid restore во время charge cycle использует обычный Generator -> Grid return; ждать Target либо сначала переходить в UPS_ONLY не требуется.

#### REQ-CYCLE-09. External/manual run не захватывается cycling

SoC/TTG/outage не создают право UPS Run остановить external либо manual generator. Отдельный outage-related cleanup после Grid restore остаётся независимым правилом.

### 7.4. Default configuration

```text
delayed_generator_start_enabled = false
generator_charge_cycle_enabled = false
generator_start_soc = 40 %
generator_target_charge_soc = 80 %
generator_min_ttg_before_start = 60 min
generator_max_start_delay_hours = 6 h
```

### 7.5. Тест-кейсы UPS Run

- `TC-UPS-01` (legacy #78) — при достаточном battery reserve после `grid_failure_delay` EnergyATS остаётся в `UPS_ONLY`, generator не стартует.
- `TC-UPS-02` (legacy #79) — достижение Start SoC начинает обычный managed generator start.
- `TC-UPS-03` (legacy #80) — достижение TTG threshold раньше SoC также начинает generator start.
- `TC-UPS-04` (legacy #81) — истечение max UPS wait запускает generator даже при достаточном SoC/TTG.
- `TC-UPS-05` (legacy #82) — потеря достоверности обязательных battery data прекращает Delayed Start fail-safe запуском generator.
- `TC-UPS-06` (legacy #83) — critical battery немедленно отменяет автоматическое ожидание.
- `TC-UPS-07` (legacy #84) — stable Grid restore во время UPS wait предотвращает ненужный generator start.
- `TC-UPS-08` (legacy #85) — manual reserve request во время UPS wait начинает generator без дальнейшей delayed pause.
- `TC-UPS-09` (legacy #86) — cycle-owned session на Target SoC безопасно возвращает дом в `UPS_ONLY` и штатно останавливает generator.
- `TC-UPS-10` (legacy #87) — после automatic stop новый Start SoC/TTG/max-delay запускает следующий cycle.
- `TC-UPS-11` (legacy #88) — Grid restore во время charge cycle возвращает дом на Grid без ожидания Target.
- `TC-UPS-12` (legacy #89) — при disabled Charge Cycling обычный automatic outage generator не останавливается по Target SoC.
- `TC-UPS-13` (legacy #90) — manual run во время outage не завершается автоматически по Target SoC.
- `TC-UPS-14` (legacy #91) — external generator не захватывается UPS Run и не останавливается по Target.
- `TC-UPS-15` (legacy #92) — restart во время UPS-only wait сохраняет истёкшее время и не создаёт необоснованный немедленный start.
- `TC-UPS-16` (legacy #93) — restart cycle-owned session сохраняет достаточную ответственность для безопасного Target stop.
- `TC-UPS-17` (legacy #94) — restart не превращает manual/external run в cycle-owned.

---

## 8. Scheduled Exercise

### 8.1. Назначение и конфигурация

Scheduled Exercise автоматически проверяет конкретный generator после длительного простоя. A/B имеют независимые schedule/history и не зависят от роли PRIMARY/SECONDARY.

Default:

```text
Generator A:
  exercise_enabled = false
  exercise_interval_days = 30
  exercise_start_time = 15:00
  exercise_run_minutes = 10
  exercise_presence_grace_days = 7

Generator B:
  exercise_enabled = false
  exercise_interval_days = 45
  exercise_start_time = 15:00
  exercise_run_minutes = 10
  exercise_presence_grace_days = 14
```

`exercise_start_time` интерпретируется в local timezone Home Assistant. Forced warning lead = 60 minutes.

### 8.2. Qualifying history и schedule

#### REQ-EXERCISE-01. Независимый график

Для каждого slot хранится собственный reference. После qualifying run: `next_due = last_qualifying_run + exercise_interval_days`.

#### REQ-EXERCISE-02. Новый generator не запускается немедленно

При отсутствии history создаётся `initial_reference_time`; первый due наступает только через configured interval.

#### REQ-EXERCISE-03. Только ежедневное окно

После due запуск рассматривается в configured daily start time. Offline/missed window не создаёт случайный catch-up позже в тот же день.

#### REQ-EXERCISE-04. До forced date — только при подтверждённом отсутствии семьи

Presence должна однозначно разрешать ordinary exercise.

#### REQ-EXERCISE-05. Presence откладывает, а не ломает exercise

Blocked-by-presence attempt фиксируется как `DEFERRED`; следующий шанс — следующее scheduled window.

#### REQ-EXERCISE-06. Presence проверяется непосредственно перед start

До окончания grace period появление семьи **либо потеря достоверного подтверждения отсутствия** (`unknown/unavailable`) до фактического REMOTE ON отменяет текущую ordinary attempt как `DEFERRED`. Forced Exercise использует отдельное правило REQ-EXERCISE-07/08.

#### REQ-EXERCISE-07. Forced date отменяет только presence restriction

После `due_date + exercise_presence_grace_days` presence больше не блокирует запуск, но safety/Grid/Recovery/conflict prerequisites продолжают действовать.

#### REQ-EXERCISE-08. Warning перед forced exercise

Forced exercise разрешён только если соответствующее предупреждение реально доставлено не менее чем за 60 минут. Пропущенный warning window не создаёт неожиданного forced catch-up.

### 8.3. Физический exercise lifecycle

#### REQ-EXERCISE-09. Grid должна быть доступна

Новый Exercise начинается только при доступной Grid и устойчивом Grid path.

#### REQ-EXERCISE-10. Дом остаётся на Grid

Обычный Scheduled Exercise не переводит дом на generator bus.

#### REQ-EXERCISE-11. Safety prerequisites

Exercise не стартует при active managed session, Power Transfer transition, Recovery, E-stop, неизвестных обязательных states, уже RUNNING/REMOTE ON тестируемом generator либо конфликтующей operation.

#### REQ-EXERCISE-12. Не запускать два scheduled exercise одновременно

При конфликте окон второй slot ждёт следующего собственного scheduled window.

#### REQ-EXERCISE-13. Использовать обычный generator lifecycle

Exercise использует тот же безопасный start/choke/RUNNING/cooldown/stop механизм, что и другие сценарии; отдельного maintenance start path нет.

#### REQ-EXERCISE-14. Duration от подтверждённого RUNNING

`exercise_run_minutes` — минимальное непрерывное RUNNING время. Success подтверждается только после требуемой duration и штатной остановки.

#### REQ-EXERCISE-15. У Exercise нет fallback

Failure тестируемого slot не запускает второй generator; цель maintenance — проверить именно выбранный engine.

### 8.4. Result и ответственность

#### REQ-EXERCISE-16. SUCCESS

SUCCESS требует подтверждённого RUNNING, требуемой duration, отсутствия invalidating fault и подтверждённой штатной остановки. После SUCCESS обновляется qualifying history.

#### REQ-EXERCISE-17. FAILED

Start timeout, unexpected stop до duration, GC fault, stop timeout либо неоднозначность/recovery после физического старта дают FAILED. History обновляется только если qualifying duration независимо была достоверно достигнута.

#### REQ-EXERCISE-18. DEFERRED не FAILED

Не начатая физически попытка из-за presence/safety/window/warning/conflict — DEFERRED.

#### REQ-EXERCISE-19. Journal результата

Для физической attempt сохраняются generator, scheduled/actual times, required/actual duration, forced flag, result и failure reason. Допустимые нейтральные interrupted results должны различать как минимум передачу в outage и передачу по явной manual command.

#### REQ-EXERCISE-20. Failure notification

FAILED требует notification с реальным generator name и краткой причиной; SUCCESS достаточно journal entry.

#### REQ-EXERCISE-21. Scheduled Exercise является TEST_RUN

Run, начатый Scheduler, не становится OUTAGE_RELATED только из-за последующего изменения Grid.

#### REQ-EXERCISE-22. Duration создаёт обязанность stop

После истечения duration Scheduler обязан завершить собственный run, если ответственность явно не передана другому системному сценарию.

#### REQ-EXERCISE-23. Автоматический run не может потерять ответственного

До confirmed stop либо explicit handoff Scheduler остаётся ответственным за собственный automatically started engine.

#### REQ-EXERCISE-24. Real outage важнее maintenance

При Grid outage общесистемное решение определяется REQ-BEH-04/05; maintenance не требует сначала остановить пригодный RUNNING generator.

#### REQ-EXERCISE-25. Exercise-generator не external при явном handoff

Происхождение Scheduled Exercise известно EnergyATS, поэтому пригодный run может быть явно передан outage/manual session без классификации как неизвестный external.

#### REQ-EXERCISE-26. Явный Exercise -> Outage handoff

После безопасной передачи Scheduler прекращает stop ownership, outage session принимает дальнейшую работу/transfer/fallback/stop, а исходный exercise duration больше не останавливает generator под outage load.

#### REQ-EXERCISE-27. Нет handoff — Scheduler обязан завершить run

Краткий outage, не завершившийся явной передачей, не отменяет исходную обязанность Scheduler stop.

#### REQ-EXERCISE-28. Неоднозначный handoff запрещён

Duplicate start и угадывание ownership запрещены; применяется REQ-BEH-05.

#### REQ-EXERCISE-29. Interrupted maintenance не является техническим failure

Exercise, переданный outage, фиксируется как `INTERRUPTED_BY_OUTAGE`; переданный явной manual command — как отдельный нейтральный interrupted result. Сам факт такого handoff не создаёт failure notification. Если qualifying duration была достоверно достигнута до handoff, history может обновиться независимо от maintenance result.

### 8.5. Тест-кейсы Scheduled Exercise

- `TC-EX-01` (legacy #24) — новый slot без history получает первый due только после configured interval.
- `TC-EX-02` (legacy #25) — due A при подтверждённом отсутствии семьи выполняет успешный Exercise.
- `TC-EX-03` (legacy #26) — B использует собственный independent schedule.
- `TC-EX-04` (legacy #27) — presence ON в ordinary due window даёт DEFERRED, а не start/failure.
- `TC-EX-05` (legacy #28) — после исчезновения presence в следующем разрешённом window Exercise выполняется.
- `TC-EX-06` (legacy #29) — перед forced Exercise реально отправляется warning за требуемый lead.
- `TC-EX-07` (legacy #30) — пропущенный из-за offline warning не создаёт неожиданного forced catch-up.
- `TC-EX-08` (legacy #31) — появление человека либо `unknown/unavailable` presence непосредственно перед ordinary REMOTE ON даёт DEFERRED и не запускает generator.
- `TC-EX-09` (legacy #32) — forced start игнорирует presence, но не Safety/E-stop/Recovery/unknown-state.
- `TC-EX-10` (legacy #33) — PRIMARY/SECONDARY role не меняет индивидуальный schedule.
- `TC-EX-11` (legacy #34) — failure A Exercise не запускает B как fallback.
- `TC-EX-12` (legacy #35) — RUNNING timeout даёт FAILED + notification и не сбрасывает overdue без qualifying run.
- `TC-EX-13` (legacy #36) — unexpected stop до duration даёт FAILED.
- `TC-EX-14` (legacy #37) — stop timeout даёт FAILED и при необходимости Recovery.
- `TC-EX-15` (legacy #38) — достаточно длинный normal outage run засчитывается как qualifying activity.
- `TC-EX-16` (legacy #39) — короткая/неуспешная попытка не сбрасывает due.
- `TC-EX-17` (legacy #40) — конфликтующие A/B windows не запускаются одновременно.
- `TC-EX-18` (legacy #41) — App offline в scheduled time не делает произвольный same-day catch-up.
- `TC-EX-19` (legacy #42) — restart до scheduled time сохраняет schedule/grace state.
- `TC-EX-20` (legacy #43) — restart active Exercise не выдаёт второй REMOTE START.
- `TC-EX-21` (legacy #44) — restart после duration при RUNNING сохраняет обязанность Scheduler stop.
- `TC-EX-22` (legacy #45) — ordinary Exercise на Grid не переводит дом на generator bus.
- `TC-EX-23` (legacy #46) — внешний `generator_test_mode` не подменяет ownership Scheduled Exercise.
- `TC-EX-24` (legacy #47) — brief Grid loss без handoff не отменяет scheduled stop по duration.
- `TC-EX-25` (legacy #48) — real outage принимает пригодный RUNNING Exercise-generator без OFF/new cold start.
- `TC-EX-26` (legacy #49) — после handoff исходная exercise duration не останавливает generator, питающий outage.
- `TC-EX-27` (legacy #50) — outage в неоднозначной start-phase не создаёт duplicate REMOTE и безопасно разрешается/recovery.
- `TC-EX-28` (legacy #51) — Exercise -> Outage записывается как interrupted, а не failure notification.
- `TC-EX-29` (legacy #52) — qualifying duration до handoff обновляет history независимо от maintenance result.
- `TC-EX-30` (legacy #53) — automatically started Exercise-generator никогда не остаётся RUNNING без явного ответственного сценария.
- `TC-EX-31` — RUNNING Exercise + manual reserve request передаёт существующий исправный run manual session без stop/start цикла.

---

## 9. Load Management

### 9.1. Назначение

Load Manager управляет только:

```text
G1 = switch.non_critical_loads_first_floor
G2 = switch.non_critical_loads_basement_floor
```

Приоритет:

```text
restore: G1 -> G2
shed:    G2 -> G1
```

### 9.2. Scope и ownership consumer state

#### REQ-LOAD-01. Только G1/G2

Не разрешено автоматически управлять неизвестными consumer loads.

#### REQ-LOAD-02. Не управлять generator/TPC

Load Manager не выбирает slot, не меняет PRIMARY/SECONDARY, не выдаёт REMOTE/choke и не выполняет Grid/Generator transfer.

#### REQ-LOAD-03. Limits по фактическому bus owner

Nominal/Maximum Power выбираются по actual `GeneratorBusOwner`, а не по configured PRIMARY или managed session slot.

#### REQ-LOAD-04. Паспортные power metadata отдельны

Generator Controller публикует read-only Nominal/Maximum Power sensors в W для каждого slot.

#### REQ-LOAD-05. Валидность limits

Для power-based решений требуется `0 < nominal_power <= maximum_power`. Invalid/missing limits дают локальный DEGRADED и запрещают power-based admission/shedding, но не блокируют core ATS. Для установленного оборудования: Elemax 5600/6500 W; Вепрь 5500/6000 W.

#### REQ-LOAD-06. Запоминать исходное состояние

Группа, уже OFF до команды EnergyATS, не считается shed by EnergyATS.

#### REQ-LOAD-07. Включать автоматически только собственные OFF

Automatic restore разрешён только при достоверном `shed_by_energy_ats = true`.

#### REQ-LOAD-08. User override важнее

Ручное изменение G1/G2 не должно быть впоследствии отменено EnergyATS на основании старого ownership.

### 9.3. Pre-transfer shedding

#### REQ-LOAD-09. Shed как можно позднее

Pre-transfer shedding начинается после generator ready, непосредственно перед generator transfer, а не на всё время start/warmup.

#### REQ-LOAD-10. Meter не нужен для pre-transfer shed

До подключения дома к bus полезную house load по generator meter измерить нельзя; доступные ON groups могут быть сняты и подтверждены без meter.

#### REQ-LOAD-11. Consumer switch failure локален

Unavailable/failed G1/G2 даёт DEGRADED/notification, но не блокирует generator start/transfer/Grid return. Не подтверждённый OFF не получает `shed_by_energy_ats`.

#### REQ-LOAD-12. Grid вернулась до transfer

Generator transfer не продолжается только ради завершения Load Manager. После confirmed Grid path восстанавливаются лишь собственные shed groups.

### 9.4. Измерение generator load

#### REQ-LOAD-13. Active Power — управляющий критерий

Admission/overload/shedding используют `sensor.generator_power` в W. U/I/S/Q/PF/Frequency остаются диагностическими без отдельных policy thresholds.

#### REQ-LOAD-14. Условия валидного measurement

Power decision разрешено только при confirmed generator supply, known bus owner, valid limits, healthy meter, **непрерывно свежем** numeric power stream и завершённом stabilization после последнего load change. Свежесть относится не только к значению entity, но и к появлению новых samples.

#### REQ-LOAD-15. Несколько свежих samples

Один случайный sample не подтверждает steady load; в stabilization window требуется несколько свежих измерений. Gap длиннее допустимого freshness interval разрывает непрерывность measurement/overload timer и требует доказательства заново.

#### REQ-LOAD-16. Meter — soft dependency

Meter failure/stale/unknown, включая остановившийся поток новых samples при остающемся доступным entity, не блокирует App, GC/TPC/core decision, не создаёт system Recovery и не является основанием stop generator. Load Manager переходит в локальный DEGRADED и запрещает новые power-based решения.

#### REQ-LOAD-17. Meter unavailable до restore groups

Shed groups остаются OFF; вслепую добавлять их на generator bus запрещено.

#### REQ-LOAD-18. Meter отказал при устойчивой работе

Текущее G1/G2 state не меняется только из-за потери meter; measurement-based decisions приостанавливаются. Любые незавершённые overload/admission timers, непрерывность которых нельзя доказать через gap, сбрасываются.

#### REQ-LOAD-19. Meter recovered

Recovery начинается только после появления **новой revision/sample**, отличимой от последнего принятого измерения; неизменившийся frozen sample не считается восстановлением потока. После этого meter проходит новый stabilization window; первый новый sample не используется как shortcut и не продолжает старый overload/admission timer. Пользовательское событие «Load Manager восстановлен» допустимо только после успешного завершения нового stabilization window.

### 9.5. Admission

#### REQ-LOAD-20. Base load

После generator transfer при shed groups сначала определяется valid stable base generator load.

#### REQ-LOAD-21. Restore margin

Перед добавлением группы требуется `P <= nominal_power * (1 - load_restore_margin_percent / 100)`.

#### REQ-LOAD-22. Admission G1

G1 добавляется первой, затем после stabilization либо остаётся ON при `P <= nominal`, либо возвращается OFF. Failed G1 admission не разрешает сразу добавлять G2.

#### REQ-LOAD-23. Admission G2

G2 рассматривается только после завершённого решения по G1 и проходит тот же measurement cycle.

#### REQ-LOAD-24. Не включать две группы по одному measurement

Между ON разных groups всегда есть отдельное stabilization/measurement window.

### 9.6. Continuous overload control

#### REQ-LOAD-25. Контроль работает всё время generator supply

Load Manager не заканчивается после startup/admission; overload может возникнуть позже.

#### REQ-LOAD-26. External generator не исключает consumer protection

При confirmed generator bus и known owner G1/G2 можно защищать независимо от managed/external происхождения engine, без получения прав на engine control.

#### REQ-LOAD-27. Нормальная область

При stable `P <= nominal_power` overload shedding не выполняется.

#### REQ-LOAD-28. Sustained nominal overload

`nominal < P <= maximum` допускается кратковременно; shedding начинается только после `nominal_overload_time` **непрерывно наблюдаемого** превышения. Gap/stale stream разрывает continuity и сбрасывает этот timer.

#### REQ-LOAD-29. Maximum overload

`P > maximum` после отдельного короткого confirmation запускает ускоренный shedding; одиночный spike не должен переключать groups. Confirmation требует непрерывно свежих samples и также сбрасывается при stale gap.

#### REQ-LOAD-30. Shed по одной группе

Сначала G2, затем при необходимости G1; после каждого confirmed OFF выполняется новый stabilization и повторная оценка.

#### REQ-LOAD-31. Все controlled groups уже OFF

Если P всё ещё > nominal, неизвестные loads не отключаются; отправляется понятное warning.

#### REQ-LOAD-32. Critical overload после полного shed

Если после доступного shedding P подтверждённо > maximum, отправляется critical notification, но generator не останавливается только по этому основанию.

### 9.7. Hysteresis, takeover, Grid return, persistence

#### REQ-LOAD-33. Не допускать ON/OFF дребезг

Повторный restore возможен только после `load_restore_retry_interval` и stable restore margin.

#### REQ-LOAD-34. Retry использует обычный admission

После retry нет shortcut; каждая group снова проходит standard admission.

#### REQ-LOAD-35. Bus owner change меняет limits

При A<->B takeover старые limits немедленно перестают использоваться; после stabilization нагрузка оценивается по metadata нового owner.

#### REQ-LOAD-36. UNKNOWN bus owner

При unknown owner Load Manager DEGRADED, не добавляет load и не меняет существующий G1/G2 state только из-за unknown. Core ATS продолжает работу.

#### REQ-LOAD-37. Сначала Grid, затем consumer restore

Факт `grid_input_ready=ON` недостаточен; restore shed groups начинается только после confirmed Grid path.

#### REQ-LOAD-38. На Grid generator limits не нужны

Собственные shed groups восстанавливаются G1->G2 без generator power margin; чужой/user OFF не включается.

#### REQ-LOAD-39. Минимальный persisted state

Сохраняются как минимум phase, `shed_by_energy_ats` для G1/G2, последняя причина и retry/deadline state.

#### REQ-LOAD-40. Restart не создаёт новое право на ON

Только persisted own-off разрешает future auto restore. Неизвестный OFF считается внешним/user state.

#### REQ-LOAD-41. Restart во время generator supply

После restart сначала восстанавливаются observed state, owner, meter validity и новый stabilization; старое measurement не используется для immediate action.

#### REQ-LOAD-42. Enable flag и soft dependencies

`load_management_enabled=false` запрещает новые G1/G2 commands и не влияет на core ATS. Выключение не стирает persisted own-off, но пока manager disabled это ownership не используется для новых действий.

### 9.8. Default configuration

```text
load_management_enabled = false
load_measurement_stabilization_time = 10 s
load_restore_margin_percent = 15 %
nominal_overload_time = 20 s
maximum_overload_confirmation_time = 4 s
load_restore_retry_interval = 300 s
```

### 9.9. Тест-кейсы Load Management

- `TC-LOAD-01` (legacy #54) — ON G1/G2 снимаются непосредственно перед managed generator transfer.
- `TC-LOAD-02` (legacy #55) — group, которая была user-OFF заранее, не получает future automatic ON.
- `TC-LOAD-03` (legacy #56) — после transfer сначала измеряется base load и G1 добавляется только при достаточном margin.
- `TC-LOAD-04` (legacy #57) — успешный G1 admission сохраняет G1 и только потом разрешает оценивать G2.
- `TC-LOAD-05` (legacy #58) — G1, поднявшая P выше nominal, возвращается OFF; G2 в этом cycle не включается.
- `TC-LOAD-06` (legacy #59) — G2 включается только после завершённого решения по G1 и собственного measurement cycle.
- `TC-LOAD-07` (legacy #60) — sustained nominal overload снимает сначала G2, затем при необходимости G1.
- `TC-LOAD-08` (legacy #61) — spike короче confirmation/timeout не дёргает groups.
- `TC-LOAD-09` (legacy #62) — confirmed maximum overload использует ускоренный shedding.
- `TC-LOAD-10` (legacy #63) — если OFF G2 нормализовал P, G1 остаётся ON.
- `TC-LOAD-11` (legacy #64) — все controlled groups OFF и P>nominal: warning без попытки отключать неизвестные loads.
- `TC-LOAD-12` (legacy #65) — все controlled groups OFF и P>maximum: critical notification, но core не останавливает generator автоматически.
- `TC-LOAD-13` (legacy #66) — после retry interval low load разрешает новый последовательный admission.
- `TC-LOAD-14` (legacy #67) — meter unavailable до restore оставляет own-shed groups OFF, core session продолжается.
- `TC-LOAD-15` (legacy #68) — meter failure при stable G1/G2 не меняет их состояние и не ломает core ATS.
- `TC-LOAD-16` (legacy #69) — meter recovery требует нового stabilization до возобновления decisions.
- `TC-LOAD-17` (legacy #70) — restart сохраняет own-off и не присваивает user OFF.
- `TC-LOAD-18` (legacy #71) — stable Grid сначала подтверждается физически, затем восстанавливаются только own-shed groups.
- `TC-LOAD-19` (legacy #72) — Grid restore между pre-shed и generator transfer отменяет ненужный transfer и восстанавливает own-shed groups на Grid.
- `TC-LOAD-20` (legacy #73) — bus takeover A<->B меняет nominal/maximum limits и инициирует новую оценку.
- `TC-LOAD-21` (legacy #74) — overload через долгое время после startup всё равно обнаруживается.
- `TC-LOAD-22` (legacy #75) — unavailable load switch/meter не переводит core ATS в Recovery и не блокирует Grid return.
- `TC-LOAD-23` (legacy #76) — disabled Load Manager не выдаёт G1/G2 commands и не меняет обычный ATS flow.
- `TC-LOAD-24` (legacy #77) — invalid/missing owner power metadata даёт Load Manager DEGRADED без остановки core ATS.
- `TC-LOAD-25` — stale/frozen поток power samples в `STABLE` переводит Load Manager в `DEGRADED`, сбрасывает overload continuity; неизменившийся sample id не считается recovery, а после появления новой revision требуется полный новый stabilization без immediate shedding/admission.

---

## 10. Restart, Recovery и неконсистентные состояния

### 10.1. Restart

#### REQ-START-01. Ожидание обязательных данных

До получения обязательных физических/config inputs аппаратные команды не выдаются. Load meter/G1/G2 и generator power metadata — soft dependencies core ATS.

#### REQ-START-02. Устойчивая managed session переживает restart

Если persisted session и observed physical state однозначно совпадают, ownership восстанавливается без повторного start или ненужного transfer.

#### REQ-START-03. Незавершённую операцию нельзя продолжать вслепую

Restart во время неподтверждённой физической transition требует Recovery, если безопасное следующее действие нельзя доказать.

#### REQ-START-04. Старый persistence format не обязателен

При существенном изменении модели backward compatibility persistent state не требуется; unsupported state отклоняется безопасно.

#### REQ-START-05. Exercise persistence

Для A/B независимо сохраняется достаточно данных для schedule/history/grace/active attempt/run timing/warning/result и обязанности stop собственного run.

#### REQ-START-06. Restart не создаёт uncontrolled Exercise

Не разрешён blind repeat REMOTE START. Если active Exercise однозначно продолжается, ответственность восстанавливается; если duration истекла, собственный run должен быть завершён; если ownership доказать нельзя — Recovery.

### 10.2. Fault / consistency

#### REQ-FAULT-01. Оба house feedback ON недопустимы

Одновременный устойчивый `house_powered_by_grid=ON` и `house_powered_by_generator=ON` — contradiction/recovery condition после допустимого physical settling time.

#### REQ-FAULT-02. Generator control выбран, feedback не пришёл

Если generator source command устойчиво активна, **generator source доступен**, а expected feedback не появился за timeout, это transfer/feedback fault. Исчезновение generator voltage/RUNNING само по себе не доказывает положение generator selector/contact: отсутствие напряжения нельзя трактовать как подтверждение размыкания.

Если при потере generator source control state достоверно показывает `generator_selected=ON`, а требуемое направление — снять generator source/изолировать дом перед fallback или возвратом, разрешена только безопасная **break**-команда `DESELECT_GENERATOR` с последующим физическим подтверждением. Отсутствующий feedback не разрешает make-команду и не считается подтверждением уже выполненного размыкания.

#### REQ-FAULT-03. Grid control выбран, feedback не пришёл

Аналогично confirmed available Grid должна дать expected Grid feedback в timeout.

#### REQ-FAULT-04. Grid feedback противоречит command

Устойчивый `grid_power=OFF` при `house_powered_by_grid=ON` рассматривается как contradiction.

#### REQ-FAULT-05. Generator feedback без RUNNING source

Устойчивый `house_powered_by_generator=ON` при обоих RUNNING=OFF — inconsistency.

#### REQ-FAULT-06. Два RUNNING не fault

Одновременный RUNNING A/B разрешён физической моделью.

#### REQ-FAULT-07. Load Manager faults локальны

Meter/power metadata/G1/G2 failures не являются сами по себе системным Recovery.

### 10.3. Emergency Stop и Recovery

Emergency Stop имеет приоритет над обычными сценариями; новые start commands запрещены. `RECOVERY_REQUIRED` используется, когда безопасное продолжение нельзя доказать: lost transition, contradictory feedback, unknown critical owner, fallback exhaustion, unrecoverable automatically-started-run ownership и аналогичные состояния.

Recovery не расширяет право на external generator, кроме уже явно разрешённых cleanup rules. Recovery Load Manager не существует: его DEGRADED локален.

#### REQ-RECOVERY-01. Recovery использует те же безопасные break-before-make правила

Recovery не получает отдельного права на небезопасную силовую команду. Если generator selector/control достоверно остаётся ON, а generator voltage/house feedback уже исчезли, Recovery может выполнить безопасный `DESELECT_GENERATOR` по REQ-FAULT-02 и только после подтверждения переходить к следующему шагу. Отсутствие напряжения само по себе не считается доказательством размыкания.

#### REQ-RECOVERY-02. Принятый Recovery Reset обязан завершиться или явно истечь по timeout

После принятия reset ни одна физическая операция Recovery не может ждать подтверждение бесконечно. Каждая TPC/GC operation имеет конечный deadline; отсутствие требуемого feedback оставляет систему в `RECOVERY_REQUIRED` с явной причиной/ошибкой и не разрешает слепо выполнять следующий make/stop шаг.

### 10.4. Тест-кейсы restart/recovery

- `TC-CORE-19` (legacy #19) — contradictory house feedback распознаётся как unsafe inconsistency.
- `TC-CORE-20` (legacy #20) — transfer confirmation timeout приводит к контролируемому Recovery, а не бесконечному ожиданию/повтору.
- `TC-CORE-22` (legacy #22) — Emergency Stop блокирует обычные automatic/manual starts.
- `TC-CORE-23` (legacy #23) — restart после незавершённой physical transaction не продолжает её вслепую.
- `TC-CORE-24` — stop fault managed generator после уже подтверждённого manual return на Grid даёт Recovery без запуска fallback generator.
- `TC-CORE-25` — потеря generator voltage при `generator_selected=ON` разрешает безопасный DESELECT и не должна обрывать допустимый SECONDARY start более коротким stale-topology timeout.
- `TC-CORE-26` — Recovery Reset при `generator_selected=ON` и потерянном generator feedback сначала выполняет безопасный break/DESELECT; неподтверждённая операция завершается timeout/Recovery, а не бесконечным ожиданием или make-командой.

Feature-specific restart cases дополнительно описаны `TC-EX-19..21`, `TC-LOAD-17`, `TC-UPS-15..17`.

---

## 11. Наблюдаемость, журнал и уведомления

### REQ-OBS-01. Recovery нельзя маскировать функциональным статусом

Если верхнеуровневый Supervisor находится в `RECOVERY_REQUIRED`, основной пользовательский status и `sensor.energy_ats_status` должны сообщать именно о необходимости Recovery независимо от локального состояния UPS Run, Exercise или Load Manager. Технические атрибуты этих подсистем могут сохраняться, но штатная надпись вроде «Питание от UPS» не должна скрывать Recovery.

### REQ-OBS-02. Текущий status публикуется последовательно и по latest-wins

`sensor.energy_ats_status` представляет одно текущее состояние, поэтому его REST publications не должны выполняться параллельно так, чтобы старый медленный write завершился после нового и затёр более свежий status. Временная ошибка доставки не отменяет последнее desired состояние: retry/coalescing должны в итоге публиковать актуальный payload, а устаревшие промежуточные значения могут быть пропущены.

Пользователь должен иметь возможность ответить минимум на четыре вопроса:

1. откуда сейчас питается дом;
2. почему EnergyATS хочет/не хочет generator;
3. кто отвечает за продолжающийся automatically-started run;
4. какая операция выполняется или почему automation остановлена.

UI/journal используют реальные generator names, а A/B остаются внутренним slot identity там, где это технически нужно.

Минимально наблюдаются:

```text
Grid availability
Power source: Grid / Generator <name> / Generator Unknown / UPS ONLY / No Power / Unknown
Generator A/B RUNNING
GeneratorBusOwner
managed session reason / generator
recovery state / reason
```

Для UPS Run:

```text
delayed_start_enabled
charge_cycle_enabled
battery_soc
battery_ttg_minutes
start/target thresholds
current UPS wait elapsed/remaining
reason why generator is or is not required
cycle ownership / manual override
```

Для Scheduled Exercise отдельно A/B:

```text
enabled
last qualifying run / initial reference
next due / overdue
forced date / grace state
active attempt
planned duration
last result / failure reason
```

Для Load Manager:

```text
enabled
phase / degraded reason
generator power
active owner nominal/maximum
G1/G2 state and shed_by_energy_ats
overload timer
next restore retry
```

Journal должен позволять установить причину каждого существенного решения, передачу ответственности между сценариями и физический результат start/transfer/stop.

Failure/critical notifications должны быть человеко-читаемыми и использовать generator display name.

### 11.1. Тест-кейсы наблюдаемости

- `TC-OBS-01` — `RECOVERY_REQUIRED` имеет приоритет над UPS Run/другими штатными labels в основном тексте status.
- `TC-OBS-02` — медленный/ошибочный старый status write не может затереть более новый desired status; publisher последовательно доставляет latest state и повторяет временную ошибку.

---

## 12. Home Assistant interface и конфигурация

### 12.1. Основные inputs/controls

```text
binary_sensor.grid_input_ready
switch.grid_power
switch.use_generator_as_power_source
binary_sensor.house_powered_by_grid
binary_sensor.house_powered_by_generator

binary_sensor.generator_a_is_running
binary_sensor.generator_b_is_running
switch.generator_a_remote_start
switch.generator_b_remote_start
select.primary_generator

sensor.generator_a_name
sensor.generator_b_name
sensor.generator_a_model
sensor.generator_b_model
sensor.generator_a_nominal_power
sensor.generator_a_maximum_power
sensor.generator_b_nominal_power
sensor.generator_b_maximum_power
```

`RUNNING` — физическое подтверждение работы/выходного напряжения generator; `REMOTE` — control signal и не доказывает успешный start.

### 12.2. UPS / battery inputs

Отдельного Battery contactor нет. Battery/UPS telemetry является soft dependency для optimization UPS Run; её отказ не должен делать core ATS неработоспособным.

### 12.3. Exercise presence

`family_presence_entity` — configurable HA entity. Domain logic не должна hard-code конкретный family group.

### 12.4. Load Manager inputs

```text
G1 = switch.non_critical_loads_first_floor
G2 = switch.non_critical_loads_basement_floor
binary_sensor.generator_meter_status
sensor.generator_power
sensor.generator_current
sensor.generator_voltage
sensor.generator_apparent_power
sensor.generator_reactive_power
sensor.generator_power_factor
sensor.generator_frequency
```

Power meter и G1/G2 — soft dependencies core ATS.

### 12.5. Что не должно моделироваться как физический механизм

Запрещено вводить как реально существующие actuators:

- Battery contactor / `connect_battery`;
- software selector A/B общей generator bus;
- запрет одновременного RUNNING A/B;
- отдельный Exercise contactor/path;
- Load Manager как часть generator contactor topology.

Допустимы derived states `UPS_ONLY`, `bus_owner`, `run_context`, schedule/ownership/degraded states, если они описывают реальное наблюдаемое либо логическое состояние и не выдаются за физические устройства.

---

## 13. Порядок изменения и трассировка реализации

Если меняется физическая схема:

```text
PHYSICAL_POWER_TOPOLOGY_RU.md
        -> REQUIREMENTS_RU.md
        -> ARCHITECTURE_RU.md / code
        -> automated tests
        -> physical tests
```

Если меняется только поведение автоматики, physical topology не переписывается.

### REQ-TRACE-01. System decision должен ссылаться на requirement

Каждая нетривиальная ветка верхнеуровневого решения в реализации должна быть трассируема к конкретному `REQ-BEH-*` либо другому узкому `REQ-*`.

Для кода, реализующего системное решение, предпочтителен человеко-читаемый комментарий с ID и кратким смыслом требования, а не только технический комментарий вроде `handle handoff`.

### REQ-TRACE-02. Tests описывают что проверяется

Каждый integration/scenario test должен иметь короткое человеческое описание того, **какое поведение он проверяет**. Описание не обязано объяснять fixture, mocks и последовательность внутренних вызовов — это видно в коде теста.

### REQ-TRACE-03. Новый конфликт режимов сначала описывается как behavior

Если новая feature пересекается с manual/outage/Exercise/UPS Run, сначала добавляется системное `REQ-BEH-*` и тест-кейс, затем меняется архитектура/код. Нельзя решать новое пересечение только локальным `if` в composition root без требования.

---

## 14. Проверка требований и traceability

Подробное описание того, **что проверяется**, расположено рядом с соответствующей feature в разделах 4–10. `USER_TESTS_RU.md` содержит минимальный набор физических испытаний и их procedure.

Старые сценарии 1–94 не удалены: они получили стабильные `TC-*` IDs, а legacy number сохранён рядом с описанием.

| Область требований | Test cases | Основная автоматическая проверка | Физическая проверка |
|---|---|---|---|
| `REQ-BEH-01..18` — пересечения режимов | `TC-BEH-01..10` | Supervisor/integration scenarios | для transfer/start/stop конфликтов — да |
| `REQ-GRID-*`, `REQ-AUTO-*`, `REQ-MANUAL-*`, `REQ-RETURN-*`, `REQ-STOP-*` | `TC-CORE-01..07`, `TC-CORE-21/24/27` | core ATS integration | да |
| `REQ-BUS-*`, `REQ-EXT-*`, `REQ-FALLBACK-*`, `REQ-OUTRUN-*` | `TC-CORE-08..18`, `TC-CORE-25` | bus/supervisor/TPC integration | да для owner/takeover/fallback |
| `REQ-DELAY-*`, `REQ-CYCLE-*` | `TC-UPS-01..17`, `TC-BEH-01/07/08/09` | UPS Run + app integration | да для реального Generator->UPS/Grid cycle |
| `REQ-EXERCISE-*` | `TC-EX-01..31`, `TC-BEH-02..06` | scheduler + app integration | минимальный physical Exercise set обязателен |
| `REQ-LOAD-*` | `TC-LOAD-01..25`, `TC-BEH-10` | LoadManager + app integration | да для pre-shed/admission/overload |
| `REQ-START-*`, `REQ-FAULT-*`, `REQ-RECOVERY-*` | `TC-CORE-14/15/19/20/22..26` + feature restart cases | persistence/recovery integration | часть recovery cases физически |
| `REQ-OBS-*` | `TC-OBS-01..02` | status/runtime publication tests | не применяется |
| `REQ-TRACE-*` | review/CI convention | наличие descriptions/coverage | не применяется |

### 14.1. Правило полноты

Feature нельзя считать завершённой только потому, что happy-path работает. Для каждого поведения должны быть рассмотрены как минимум:

- нормальный путь;
- отмена/изменение условия на границе таймера;
- manual override, если он применим;
- restart/persistence, если состояние длительное;
- неизвестные/stale inputs;
- failure/timeout;
- пересечение с Recovery;
- пересечение с уже работающим generator, если feature способна повлиять на generator lifecycle.

### 14.2. Ценность test catalog

Каталог `TC-*` является таким же поддерживаемым активом проекта, как requirements и code. При изменении поведения test description обновляется вместе с requirement; удаление тест-кейса требует явного объяснения, какое требование больше не существует либо каким новым test case оно покрыто.
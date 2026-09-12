# Energy ATS

Home Assistant App для управления резервным электроснабжением дома с двумя генераторами, общей генераторной шиной, UPS-линией и подтверждаемой коммутацией Grid / Generator.

Текущая версия: **1.0.4**. Статус add-on: `experimental` — программная модель и автоматические проверки стабильны, но окончательный ввод конкретной установки требует физических commissioning-тестов.

## Что умеет EnergyATS

- автоматический переход на резерв при физическом исчезновении Grid;
- ручной запуск/остановка managed generator session;
- выбор PRIMARY generator из Home Assistant и один fallback `PRIMARY -> SECONDARY` без ping-pong;
- корректная работа физической схемы, в которой A и B могут одновременно быть RUNNING, но общей generator bus владеет только один аппаратно выбранный generator;
- безопасный Grid / Generator transfer с break-before-make и подтверждением каждого шага;
- `UPS_ONLY` без вымышленного Battery contactor: МАП самостоятельно поддерживает критическую UPS-линию;
- **UPS Run** для длительных outage: Delayed Start и опциональные charge cycles по SoC/TTG/времени;
- **Scheduled Exercise** для периодических пробных запусков A/B с presence/grace/warning и сохранением ownership через restart;
- **Load Manager** для двух некритичных групп G1/G2 с pre-transfer shedding, последовательным admission и continuous overload control;
- Recovery при неоднозначном физическом состоянии или незавершённой hardware transaction;
- persisted session/bus/exercise/load/UPS Run state;
- причинно-следственный Logbook: trigger -> решение -> аппаратная команда -> подтверждённое физическое состояние;
- диагностический `sensor.energy_ats_status`, Logbook и notifications.

Все дополнительные функции — UPS Run, Scheduled Exercise и Load Manager — не создают отдельного центра принятия решений. Системный конфликт Manual / Outage / Exercise / Recovery разрешает только `EnergySupervisor`.

## Физическая модель

Ключевые инварианты:

- отдельного управляемого Battery path нет; автономный режим критической линии называется `UPS_ONLY`;
- Generator A и Generator B могут штатно работать одновременно;
- аппаратная взаимная блокировка допускает только одного owner общей generator bus;
- `GeneratorBusTracker` восстанавливает FIFO-owner по истории RUNNING; если историю доказать нельзя, owner остаётся `UNKNOWN`;
- RUNNING двигателя, REMOTE command, selector state и feedback — разные факты;
- внешний RUNNING generator не становится managed автоматически;
- автоматический fallback ограничен одним переходом на другой slot;
- generator не останавливается под подтверждённой нагрузкой дома;
- пользовательское/внешнее отключение `grid_power` не должно автоматически отменяться EnergyATS без собственного сохранённого права на восстановление.

Подробная физическая схема: [`docs/PHYSICAL_POWER_TOPOLOGY_RU.md`](docs/PHYSICAL_POWER_TOPOLOGY_RU.md).

## Архитектура

```text
EnergySupervisor          единственный владелец системных решений и Recovery policy
    |
    +-- ExerciseScheduler локальная maintenance FSM
    +-- UPS Run           локальная battery/wait/cycle policy
    +-- LoadManager       локальное управление G1/G2
    |
    +-- GeneratorController A/B   lifecycle двигателя
    +-- PowerTransferController   Grid / Generator break-before-make

GeneratorBusTracker       фактический FIFO-owner общей generator bus
HomeAssistantAdapter      HA observations, hardware actions, background diagnostics
main.py                   composition/runtime dispatch, без второго policy layer
```

В 1.0.3 Recovery arbitration окончательно перенесён в `EnergySupervisor`: Supervisor решает, можно ли выполнять reset, какие owned generators допустимо остановить и задаёт порядок `Grid path -> owned shutdown -> complete`. `main.py` только исполняет директивы TPC/GC.

Обычные status/Logbook/user publications выполняются best-effort вне критического control path и не должны задерживать аппаратную FSM на сетевой timeout. Исключение — предупреждение перед forced Scheduled Exercise: его доставка является safety prerequisite и подтверждается синхронно.

Подробно: [`docs/ARCHITECTURE_RU.md`](docs/ARCHITECTURE_RU.md).

## Причинный журнал событий

Начиная с 1.0.4 Logbook предназначен не только для фиксации команд, но и для восстановления причинно-следственной цепочки события через месяцы после его возникновения.

Для ключевых сценариев журнал различает:

```text
что изменилось физически
  -> почему EnergyATS принял решение
  -> какое действие было начато
  -> какой feedback подтвердил результат
```

Отдельно журналируются потеря/возврат Grid, ручные команды, переходы UPS Run, Scheduled Exercise, Recovery, изменения RUNNING/REMOTE, PowerPath/PowerSource, generator bus owner и Emergency Stop. Первый snapshot после start/reconnect считается baseline и не создаёт ложных событий.

Внешний запуск генератора явно отличается от запуска, которым управляет EnergyATS. Пользовательские причинные сообщения формируются через стабильные message keys и русский каталог `energy_ats/app/user_messages_ru.py`; control logic не зависит от конкретной русской формулировки.

App log является полным последовательным журналом и содержит основные и диагностические события, аппаратные команды и отправляемые пользовательские сообщения. Основной поток Home Assistant Logbook намеренно короче: в него публикуются только существенные MAIN events.

## UPS Run

UPS Run — стратегия работы при длительном отсутствии Grid. В неё входят две независимые opt-in функции:

- **Delayed Start** — после `grid_failure_delay` продолжать `UPS_ONLY`, пока батарея позволяет;
- **Charge Cycling** — автоматически завершать только собственную cycle-owned outage session после Target SoC, возвращаться в `UPS_ONLY` и при необходимости запускать следующий цикл.

Условия автоматического запуска во время UPS wait:

```text
SoC <= generator_start_soc
OR TTG <= generator_min_ttg_before_start
OR UPS wait >= generator_max_start_delay_hours
```

Недостоверная или stale battery telemetry отменяет экономию топлива и приводит к обычному безопасному generator start. Manual request имеет приоритет. Stable Grid имеет приоритет над Target SoC.

Нормативное поведение и все параметры находятся в [`docs/REQUIREMENTS_RU.md`](docs/REQUIREMENTS_RU.md), HA entities — в [`docs/ENTITIES_RU.md`](docs/ENTITIES_RU.md), физические проверки — в [`docs/USER_TESTS_RU.md`](docs/USER_TESTS_RU.md), их результаты — в [`docs/USER_TEST_RESULTS_RU.md`](docs/USER_TEST_RESULTS_RU.md). Отдельного feature-документа для Delayed Start больше нет, чтобы не поддерживать вторую копию тех же правил.

## Scheduled Exercise

Для A/B можно независимо включить автоматические maintenance-запуски. По умолчанию функция выключена.

```text
A: interval 30 дней, start 15:00, run 10 мин, presence grace 7 дней
B: interval 45 дней, start 15:00, run 10 мин, presence grace 14 дней
```

Ordinary Exercise требует подтверждённого отсутствия семьи непосредственно до REMOTE ON. После grace presence перестаёт блокировать forced run, но safety prerequisites сохраняются; forced run требует заранее подтверждённой доставки warning. Exercise не переводит дом на generator bus и не имеет fallback на второй generator.

Если во время уже RUNNING Exercise возникает manual request или реальный outage, пригодный generator может быть явно передан соответствующей managed session без бессмысленного stop/cold-start.

## Load Manager

Load Manager управляет только двумя явно заданными некритичными группами:

```text
G1 = switch.non_critical_loads_first_floor
G2 = switch.non_critical_loads_basement_floor
```

При включении он:

- перед managed generator transfer снимает доступные некритичные нагрузки;
- после transfer возвращает их по одной: `G1 -> G2`;
- использует `Nominal Power` и `Maximum Power` фактического `GeneratorBusOwner`;
- непрерывно контролирует generator power, а не только startup;
- при overload отключает нагрузки в порядке `G2 -> G1`;
- не включает автоматически то, что пользователь оставил OFF;
- при потере meter/metadata/G1/G2 деградирует локально и не создаёт core `RECOVERY_REQUIRED`.

По умолчанию `load_management_enabled=false`.

## Home Assistant contract

Core contract включает Grid/Generator feedback, RUNNING/REMOTE A/B, choke buttons, E-stop, generator names/models и `select.primary_generator`. Дополнительные UPS Run, Exercise и Load Manager inputs разделены на обязательные и soft dependencies.

Полный список и точный смысл entities: [`docs/ENTITIES_RU.md`](docs/ENTITIES_RU.md).

App публикует read-only:

```text
sensor.energy_ats_status
```

Status показывает фактический source, Supervisor phase, generator/bus owner, managed session, PRIMARY, fallback, Exercise, UPS Run и Load Manager state. Он не является входом управляющей логики.

## Установка

Repository для Home Assistant Apps:

```text
https://github.com/akastrel/EnergyATS
```

Рекомендуемый первый запуск — `armed: false`. После проверки entities/status/PRIMARY можно переходить к `armed: true` и физическим испытаниям.

Подробно: [`docs/INSTALL_RU.md`](docs/INSTALL_RU.md).

## Ручные команды

Через `hassio.app_stdin` поддерживаются:

```text
start_generator
stop_generator
reset
```

- `start_generator` — начать/принять manual managed session;
- `stop_generator` — безопасно завершить managed session;
- `reset` — выполнить контролируемый Recovery, а не просто «стереть ошибку».

## Документация

| Документ | Роль |
|---|---|
| [`docs/PHYSICAL_POWER_TOPOLOGY_RU.md`](docs/PHYSICAL_POWER_TOPOLOGY_RU.md) | фактическая силовая схема и физические сигналы |
| [`docs/REQUIREMENTS_RU.md`](docs/REQUIREMENTS_RU.md) | **нормативное поведение EnergyATS**, `REQ-*` / `TC-*` |
| [`docs/ARCHITECTURE_RU.md`](docs/ARCHITECTURE_RU.md) | программные компоненты и границы ответственности |
| [`docs/ENTITIES_RU.md`](docs/ENTITIES_RU.md) | Home Assistant contract и status attributes |
| [`docs/INSTALL_RU.md`](docs/INSTALL_RU.md) | установка, обновление и безопасный первый запуск |
| [`docs/USER_TESTS_RU.md`](docs/USER_TESTS_RU.md) | физический commissioning |
| [`energy_ats/DOCS.md`](energy_ats/DOCS.md) | пользовательская справка, показываемая вместе с HA App |
| [`energy_ats/CHANGELOG.md`](energy_ats/CHANGELOG.md) | история изменений |

Правило документации: один факт должен иметь один нормативный источник. README и HA docs объясняют использование, но не создают отдельные требования.

## Проверка разработки

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```

Для 1.0.4 полный Python suite: **314 passed**. CI также собирает реальный add-on Docker image и внутри него выполняет production smoke через локальный test Home Assistant WebSocket/REST endpoint.

Зелёный CI подтверждает программную модель и production packaging, но не заменяет commissioning на реальных генераторах, контакторах, DKG116, MAP, meter и G1/G2.

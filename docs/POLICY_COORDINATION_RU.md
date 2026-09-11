# Координация policies в EnergyATS

Этот документ фиксирует правила взаимодействия высокоуровневых policies. Его цель — не допустить, чтобы при добавлении новых функций `main.py` снова превратился в набор попарных `if PolicyA + PolicyB`.

## 1. Policies бывают разных типов

Не все функции EnergyATS конкурируют друг с другом.

### 1.1. Safety gate

`RECOVERY_REQUIRED`, неизвестные обязательные физические состояния, Emergency Stop и незавершённая hardware transaction — это не обычные policies.

Safety gate имеет абсолютный приоритет: автоматические планы не могут его переопределить.

### 1.2. Владельцы generator-run / managed-session

К этой группе относятся сценарии, которые могут инициировать или продолжать работу двигателя:

- ручная managed-session;
- реальный Grid outage;
- Charge Cycling как режим автоматической outage-session;
- scheduled Exercise.

Для автоматически запущенного двигателя в каждый момент должен существовать один однозначный owner, ответственный в том числе за безопасную остановку.

### 1.3. Ограничивающие downstream policies

`LoadManager` не является владельцем generator-run и не выбирает источник дома.

Он получает уже принятое решение о generator supply и может:

- задержать подключение generator bus до pre-transfer shedding;
- управлять G1/G2;
- выполнять admission/retry;
- снимать нагрузку при перегрузке.

Отказ LoadManager не передаёт ему управление генератором и не отменяет core outage-policy.

## 2. Где разрешаются конфликты

High-level пересечения разрешает один `PolicyCoordinator`.

Отдельные policy-компоненты (`ExerciseScheduler`, `OutagePowerPolicy`) не вызывают друг друга и не знают внутренние состояния друг друга. Они вычисляют собственное решение, после чего Coordinator выполняет только:

- arbitration приоритетов;
- явную передачу ownership;
- преобразование policy-request в запрос Supervisor;
- объединение policy events.

`EnergySupervisor` остаётся владельцем managed-session и силовой последовательности уровня source/generator. `GeneratorController` и `PowerTransferController` по-прежнему ничего не знают о бизнес-policy.

## 3. Приоритеты

Базовый порядок:

`Safety / Recovery` → `оператор` → `реальный outage` → `scheduled Exercise`.

Это не означает, что более высокий уровень может произвольно захватить уже работающий двигатель. Любая передача ownership должна иметь отдельное безопасное правило.

## 4. Матрица основных пересечений

| Текущее состояние | Новое событие | Результат |
|---|---|---|
| idle | Manual Start | создаётся обычная ручная managed-session |
| idle | Grid outage | после `grid_failure_delay` применяется OutagePowerPolicy: ждать на UPS либо создать outage-session |
| idle + стабильная Grid | Exercise due | Scheduler может начать Exercise при выполнении всех его safety/preconditions |
| Exercise ещё не успел физически запустить двигатель | Manual Start | Exercise получает `DEFERRED`, manual-session может начаться |
| Exercise уже RUNNING | Manual Start | работающий Exercise не захватывается молча; Scheduler сохраняет ownership, обычный managed-start не создаётся |
| Exercise RUNNING | Grid outage | после обычного outage delay Supervisor может принять этот же генератор; затем выполняется явный `Exercise -> Outage` handoff |
| Exercise | Recovery | Exercise становится FAILED/STOPPING, но Scheduler сохраняет shutdown ownership до безопасной остановки |
| automatic outage-session | Charge Cycling enabled | только новая обычная automatic outage-session может получить `CHARGE_CYCLE` control mode |
| Charge Cycle | Target SoC | OutagePowerPolicy просит завершение; Supervisor выполняет `generator bus -> isolated/UPS -> cooldown -> stop` |
| Charge Cycle | Manual Start | текущая outage-session переходит в `MANUAL_OVERRIDE`; автоматический Target SoC stop отменяется |
| Charge Cycle | Manual Stop | cycling ownership снимается; выполняется обычная пользовательская последовательность stop/return |
| Charge Cycle | стабильная Grid | возврат Grid имеет приоритет над Target SoC и завершает outage-session штатным Grid-return |
| любой generator scenario | LoadManager overload | меняются только G1/G2; ownership генератора/source не меняется |
| generator готов к transfer | LoadManager pre-transfer shedding | TPC ждёт разрешения LoadManager; generator-session остаётся активной |
| LoadManager DEGRADED | core ATS требует generator | core transfer/return не превращается из-за этого в `RECOVERY_REQUIRED` |

## 5. Session ownership

Внутри `GeneratorSession` ownership режима кодируется одним `SessionControlMode`, а не комбинацией независимых boolean:

- `STANDARD` — обычная managed-session;
- `CHARGE_CYCLE` — automatic outage-session принадлежит cycling policy и может быть завершена по Target SoC;
- `MANUAL_OVERRIDE` — пользователь принял active outage-session под ручное управление; автоматический Target SoC stop запрещён.

Факт пользовательской команды остановки остаётся отдельным `stop_requested`; это действие, а не новый owner.

Так невозможно получить противоречивое состояние вроде одновременно `cycle_owned=True` и `manual_override=True`.

## 6. Как добавлять новую policy

Перед интеграцией новой функции сначала определяется, каким ресурсом она действительно управляет.

Если новая policy хочет запускать/удерживать/останавливать generator или создавать managed-session, она является high-level owner candidate. Для неё добавляется явное правило в `PolicyCoordinator` и тесты только необходимых пересечений с существующими owner-сценариями.

Если функция только ограничивает уже принятое решение — например, load shedding, проверка допустимой мощности или локальный interlock — она должна оставаться downstream constraint и не входить в generator ownership arbitration.

Если функция лишь наблюдает/уведомляет, она вообще не должна участвовать в arbitration.

## 7. Чего намеренно нет

EnergyATS не вводит универсальный event bus, generic rule engine или граф приоритетов policies. При текущем числе функций это добавило бы больше состояний и косвенности, чем устранило.

`PolicyCoordinator` намеренно конкретный и небольшой: правила пересечения safety-critical сценариев должны быть видны в одном месте и покрыты прямыми тестами.

# Changelog

## 0.4.0

Полная переработка EnergyATS вокруг фактической физической топологии.

### Физическая модель

- Удалён виртуальный силовой `Battery path`; автономная работа MAP представляется как `UPS_ONLY`.
- Общая генераторная шина отделена от состояния двигателей A/B.
- Одновременный RUNNING A и B является штатным режимом.
- Добавлен `GeneratorBusTracker`, восстанавливающий аппаратный FIFO-owner по истории RUNNING.
- При остановке текущего owner и продолжающем RUNNING второго генератора owner автоматически переходит ко второму без команды EnergyATS.
- При недостаточной истории owner остаётся `UNKNOWN`; угадывание запрещено.

### Контекст запусков

Для каждого непрерывного RUNNING используется минимальная модель:

- `NONE`;
- `OUTAGE_RELATED`;
- `TEST_RUN`;
- `OTHER`;
- `UNKNOWN`.

После стабильного восстановления Grid EnergyATS сначала возвращает дом на Grid и только затем разрешает остановку всех известных `OUTAGE_RELATED` генераторов. `TEST_RUN`, `OTHER` и `UNKNOWN` этим правилом не останавливаются.

### Управление

- A/B остаются стабильными внутренними аппаратными слотами; UI/log используют фактические имена и модели из HA.
- Реализован единственный managed fallback `PRIMARY -> SECONDARY` без ping-pong.
- Уже работающий SECONDARY не захватывается в managed ownership при отказе PRIMARY.
- Power Transfer Controller управляет только основной парой Grid / общей Generator bus и не знает о A/B owner.
- Силовые переходы выполняются break-before-make с подтверждением каждого шага.
- Generator Controller отвечает только за жизненный цикл одного двигателя; локальная ошибка фиксируется `FAULT`, а системный recovery остаётся ответственностью Supervisor.
- Удалены исторические compatibility aliases и производные Supervisor phases, дублировавшие состояние GC/TPC/GeneratorBusTracker.

### Safety / recovery

- Неизвестное обязательное физическое состояние блокирует активные команды.
- E-stop имеет высший приоритет.
- Внешний генератор не становится managed автоматически.
- Recovery возвращает силовую схему к Grid path и не захватывает внешний генератор.
- Локальный GC fault снимается только у физически остановленного двигателя (`RUNNING=OFF`, `REMOTE=OFF`).

### Runtime / persistence / UI

- App version: `0.4.0`.
- Persistent journal использует top-level `schema_version = 2`.
- Миграция ошибочной внутренней модели 0.3.x намеренно не выполняется.
- `sensor.energy_ats_status` показывает фактический source, phase, bus owner, managed generator, run-context, PRIMARY, fallback и оставшееся время.
- Пользовательские состояния используют `UPS_ONLY` / `NO_POWER`, а не старые Battery-path термины.

### Tests / documentation

- `PHYSICAL_POWER_TOPOLOGY_RU.md` выделен как source of truth по физической электроустановке.
- `REQUIREMENTS_RU.md` полностью отделён от физического описания и задаёт policy EnergyATS.
- Unit и end-to-end suites переписаны под модель 0.4; старые тесты ошибочной модели не поддерживаются ради обратной совместимости.
- Документация repository, App и HA-контракт синхронизированы с 0.4.

---

## 0.3.x — историческая ветка

0.3.x была последовательностью ранних итераций EnergyATS, на которых появились App, GC/TPC/Supervisor, HA-команды, status sensor, persistence, Logbook и первые сценарные тесты.

Часть её внутренних абстракций (`Battery path`, запрет dual RUNNING, прежние transient phases и compatibility-механизмы) впоследствии оказалась несоответствующей реальной электрической схеме. Поэтому 0.4 является не совместимым продолжением этой внутренней модели, а её намеренной заменой.

Подробная история отдельных 0.3.x изменений остаётся доступна в Git history и релизных коммитах repository; runtime 0.4 не содержит compatibility-кода для этих моделей.
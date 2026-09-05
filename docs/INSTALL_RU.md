# Обновление до Energy ATS 0.3.1

0.3.1 синхронизирует реализацию с `REQUIREMENTS_RU.md`, разделяет Grid path и
Battery path и уточняет контракт команд заслонки. Обновление
лучше выполнять при доступной основной сети, остановленных генераторах и
`armed: false`.

## 1. Исходное безопасное состояние

Перед обновлением проверить:

```text
Grid Input Ready                 ON
House Powered by Grid           ON
House Powered by Generator      OFF
Generator A/B is running        OFF
Generator A/B Remote Start      OFF
Use Generator as Power Source   OFF
Grid Power                      ON
Generators Emergency Stop       OFF
```

Не обновлять приложение посередине запуска, переключения или cooldown.

## 2. Сначала прошивка Generator Controller 0.3.1

Обновлённый `generator-controller.yaml` сохраняет всю прежнюю телеметрию и
старые кнопки, но использует однозначный контракт:

```text
button.generator_a_choke_to_cold_start
button.generator_a_choke_to_run
button.generator_b_choke_to_cold_start
button.generator_b_choke_to_run
```

Версия прошивки объявлена как `esphome.project.version: 0.3.1` и видна в
информации об ESPHome-устройстве.

После прошивки вручную проверить в Home Assistant, что новые четыре кнопки
существуют. На этом шаге не требуется запускать двигатель: достаточно
проверить наличие entities. Физическую проверку направлений привода выполнять
только в подготовленном безопасном сценарии.

## 3. Обновить Home Assistant App

Репозиторий:

```text
https://github.com/akastrel/EnergyATS
```

После обновления списка Apps установить версию 0.3.1.

Из Configuration удалены параметры, которые теперь принадлежат профилям
двигателей:

```text
generator_a_name
generator_b_name
generator_start_timeout
generator_stop_timeout
generator_stop_delay
generator_a_choke_mode
generator_b_choke_mode
choke_temperature
choke_hold_time
preheat_*
```

Если Home Assistant сохранил их от 0.2.5, открыть Configuration, сохранить
предложенную конфигурацию 0.3.1 и убедиться, что старых полей больше нет.

Новая компактная конфигурация:

```yaml
armed: false
startup_delay: 30
tick_seconds: 1.0
log_level: info
grid_failure_delay: 5
grid_restore_stable_time: 60
manual_idle_warning_seconds: 600
transfer_confirmation_timeout: 60
primary_generator: A
generator_a_enabled: true
generator_b_enabled: true
```

## 4. Первый запуск только DISARMED

Оставить:

```yaml
armed: false
```

Запустить App. Ожидаемый UI-статус:

```text
DISARMED — только наблюдение
```

В этом режиме автоматическое и ручное управление железом запрещено. App может
читать состояния и обновлять диагностический UI-статус.

В логах проверить:

- версии и профили Elemax/Вепря;
- `supervisor=normal`;
- `transfer=stable_grid_path/grid/grid_path`;
- `A=idle`, `B=idle`;
- отсутствие missing required entities.

## 5. Журнал транзакций

0.3.1 использует внутренний файл:

```text
/data/energy-supervisor-state.json
```

Это persistent storage самого App. Копировать его в Home Assistant package не
нужно. При первом обновлении с 0.2.5 файла ещё нет — штатная ситуация.

Если 0.3.1 впервые запущен при уже работающем генераторе без своего журнала,
запуск будет классифицирован как внешний. App только сообщит о нём и не станет
переключать или останавливать оборудование.

## 6. Переход в ARMED

`armed: true` включать только после проверки новых entity и логов DISARMED.
Первый реальный тест выполнять поэтапно с человеком у генераторов и силового
щита.

Минимальная последовательность проверки:

1. Grid доступна, оба генератора остановлены.
2. Нажать `Включить резервное питание`.
3. Проверить заслонку, REMOTE, RUNNING и статус прогрева.
4. Убедиться, что Grid отключается до выбора генераторной шины.
5. Нажать `Остановить генератор`.
6. Убедиться, что дом снят с генератора до начала cooldown и снятия REMOTE.

Автоматический АВР пока оставить выключенным:

```text
input_boolean.automatic_generator_transfer = OFF
```

Ручные кнопки при этом работают.

## 7. Recovery Required

Если связь потеряна посередине физической транзакции, App не продолжает её
автоматически. После восстановления связи:

1. осмотреть фактическое состояние;
2. вручную остановить оба генератора;
3. снять оба REMOTE;
4. снять generator selector и включить `Grid Power`, то есть вернуть Grid path;
5. снять Emergency Stop, если он был включён;
6. нажать `Сбросить ошибку ATS`.

Сброс принимается только при однозначно подтверждённом безопасном состоянии.

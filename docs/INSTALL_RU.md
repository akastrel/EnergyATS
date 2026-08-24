# Установка Energy ATS App — первый безопасный запуск

## 0. Что устанавливаем

Комплект состоит из двух частей:

1. Home Assistant package `energy` — UI helper-ы, текущие энергетические template/automation.
2. Local Home Assistant App `Energy ATS` — Python state machine АВР.

App использует штатный Home Assistant Core WebSocket proxy Supervisor. В `config.yaml` стоит
`homeassistant_api: true`, поэтому Supervisor передаёт `SUPERVISOR_TOKEN` автоматически —
long-lived token вручную создавать не нужно.

## 1. Обновить package energy

Скопировать:

```text
homeassistant/packages/energy/energy.yaml
homeassistant/packages/energy/automations.yaml
```

в существующий `/config/packages/energy/`.

В `energy.yaml` добавляются пять ATS helper-ов:

```text
input_boolean.automatic_generator_transfer
input_boolean.generator_reserve_session_active
input_select.generator_reserve_session_mode
input_button.generator_reserve_start
input_button.generator_return_to_grid
```

После проверки конфигурации перезапустить Home Assistant.

## 2. Установить Local App

Папку:

```text
energy_ats/
```

целиком скопировать в:

```text
/addons/energy_ats/
```

На HA OS это можно сделать через Samba или SSH App.

Далее в UI:

```text
Settings -> Apps -> App store -> ... -> Check for updates
```

Должен появиться раздел `Local apps` и приложение `Energy ATS`.

Установить приложение.

## 3. ПЕРВЫЙ ЗАПУСК — НЕ ВКЛЮЧАТЬ ARMED

В Configuration оставить:

```yaml
armed: false
```

Запустить App и открыть его Logs.

Ожидаем увидеть:

- успешное соединение с Home Assistant;
- отсутствие списка missing entity;
- исходную физическую картину;
- `DISARMED — только наблюдение`;
- изменения физических состояний при ручной проверке датчиков/реле.

В этом режиме App **не вызывает switch/button/script/logbook services**.

## 4. Что проверить глазами до armed=true

В обычном режиме Grid:

```text
GridReady=True
HouseGrid=True
HouseGen=False
A=False
B=False
RemoteA=False
RemoteB=False
GridDisconnected=False
SourceGenerator=False
EStop=False
```

Допускаются отличия только если физическое состояние дома реально другое.

Особенно проверить семантику:

```text
switch.grid_power = ON -> Grid подключён
switch.use_generator_as_power_source = OFF -> selector Grid
```

и заслонку:

```text
button.generator_*_choke_open  -> физически ЗАКРЫТЬ choke
button.generator_*_choke_close -> физически ОТКРЫТЬ choke
```

## 5. Пока НЕ делать

Не менять `armed` на `true` до отдельного согласованного сценария первого физического теста.
Первый реальный тест лучше проводить не с полным отключением Grid, а поэтапно с человеком
у генераторов/щита и с возможностью немедленного ручного вмешательства.

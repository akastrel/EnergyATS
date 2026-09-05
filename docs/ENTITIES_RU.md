# Home Assistant entities и команды — Energy ATS 0.3.2

Вся привязка к конкретным `entity_id` находится в `ha_adapter.py`. Остальные
Python-контроллеры этих имён не знают.

## Обязательная физическая обратная связь

- `binary_sensor.grid_input_ready`
- `binary_sensor.house_powered_by_grid`
- `binary_sensor.house_powered_by_generator`
- `binary_sensor.generator_a_is_running`
- `binary_sensor.generator_b_is_running`
- `switch.generator_a_remote_start`
- `switch.generator_b_remote_start`
- `switch.grid_power` — ON означает подключённый Grid path
- `switch.use_generator_as_power_source` — ON означает генераторную шину
- `switch.generators_emergency_stop`

## Команды Generator Controller firmware 0.3.1

- `button.generator_a_choke_to_cold_start`
- `button.generator_a_choke_to_run`
- `button.generator_b_choke_to_cold_start`
- `button.generator_b_choke_to_run`

Названия описывают физический результат. В отличие от старых
`choke_open/choke_close`, здесь не требуется помнить обратную механику привода.

## Внешняя температура

- `sensor.garage_temperature` → поле Python
  `ambient_temperature_external`

Датчик optional. При `unknown/unavailable` температурная стратегия выбирает
холодный запуск и максимальный прогрев. В ESPHome Generator Controller этот
датчик не импортируется.

## Команды Energy ATS

Energy ATS 0.3.2 не требует пользовательских HA-helper-ов. Home Assistant
передаёт однократную команду непосредственно в App через
`hassio.app_stdin`:

```yaml
action: hassio.app_stdin
data:
  app: YOUR_ENERGY_ATS_APP_ID
  input:
    command: start_backup
```

Поддерживаются пять команд:

- `start_backup` — создать управляемую ручную сессию и ввести резерв;
- `stop_generator` — безопасно снять нагрузку и остановить управляемый
  генератор; при отсутствующей Grid перейти на МАП;
- `reset_recovery` — после осмотра запросить выход из `RECOVERY_REQUIRED`;
- `automatic_transfer_on` — разрешить новый автоматический запуск при
  пропадании Grid;
- `automatic_transfer_off` — запретить новый автоматический запуск.

Положение АВР хранится в persistent-журнале App и по умолчанию равно `OFF`.
Ручные команды запуска, остановки и recovery при `armed: false` игнорируются.
Команды изменения положения АВР разрешены и в DISARMED, но аппаратных действий
сами по себе не выполняют.

Текущие состояния Supervisor, TPC, обоих GC и положение АВР записываются в
журнал App. Energy ATS не создаёт для них отдельные HA-сущности.

`app` — фактический ID установленного Energy ATS. Надёжнее всего добавить
действие через визуальный редактор Home Assistant и выбрать **Energy ATS** из
списка: HA подставит нужный ID самостоятельно.

## Уведомления

- `script.notify_warning`
- `script.notify_critical`
- `logbook.log`

Ошибка необязательного уведомления или Logbook не прерывает уже начатую
аппаратную последовательность.

## Устаревший интерфейс заслонки

Прошивка 0.3.1 временно оставляет старые кнопки, чтобы обновление с Energy ATS
0.2.5 можно было выполнить безопасно:

- `button.generator_a_choke_open`
- `button.generator_a_choke_close`
- `button.generator_b_choke_open`
- `button.generator_b_choke_close`

Energy ATS 0.3.2 их не вызывает. Удалить их можно в следующем согласованном
релизе после обновления работающей установки.

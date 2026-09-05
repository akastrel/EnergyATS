# Home Assistant entities — Energy ATS 0.3.1

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

## UI и политика

- `input_boolean.automatic_generator_transfer` — разрешение автоматического
  запуска при пропадании Grid; ручные кнопки от него не зависят
- `input_button.generator_reserve_start` — управляемый ввод резерва
- `input_button.generator_return_to_grid` — безопасно снять нагрузку и
  остановить управляемый генератор; при отсутствующей Grid перейти на МАП
- `input_button.generator_ats_reset` — optional recovery reset после осмотра
- `input_text.generator_ats_status` — optional человекочитаемый статус
- `input_boolean.generator_reserve_session_active` — optional UI-проекция
  наличия сессии
- `input_select.generator_reserve_session_mode` — optional UI-проекция
  `none/manual/automatic`

Helper-ы являются интерфейсом и отображением. Физическим источником истины они
не считаются.

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

Energy ATS 0.3.1 их не вызывает. Удалить их можно в следующем согласованном
релизе после обновления работающей установки.

# Entity map Energy ATS

## Физическая обратная связь

- `binary_sensor.grid_input_ready`
- `binary_sensor.house_powered_by_grid`
- `binary_sensor.house_powered_by_generator`
- `binary_sensor.generator_a_is_running`
- `binary_sensor.generator_b_is_running`
- `sensor.garage_temperature` — optional; при unavailable cold start + max preheat

## Исполнительные механизмы

- `switch.generator_a_remote_start`
- `switch.generator_b_remote_start`
- `button.generator_a_choke_open` — физически ЗАКРЫТЬ choke
- `button.generator_a_choke_close` — физически ОТКРЫТЬ choke
- `button.generator_b_choke_open` — физически ЗАКРЫТЬ choke
- `button.generator_b_choke_close` — физически ОТКРЫТЬ choke
- `switch.grid_power` — ON = Grid подключён, OFF = Grid отключён
- `switch.use_generator_as_power_source` — ON = Generator, OFF = Grid
- `switch.generators_emergency_stop`

## UI / session helpers

- `input_boolean.automatic_generator_transfer`
- `input_boolean.generator_reserve_session_active`
- `input_select.generator_reserve_session_mode`
- `input_button.generator_reserve_start`
- `input_button.generator_return_to_grid` — остановить управляемый генератор;
  при наличии Grid вернуть дом на сеть, без Grid оставить UPS-линию на МАП
- `input_button.generator_ats_reset` — безопасный выход из TERMINAL
- `input_text.generator_ats_status` — optional человекочитаемая фаза для UI

## Notifications

- `script.notify_critical`
- `script.notify_warning`
- `logbook.log`

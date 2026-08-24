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
- `switch.disconnect_grid_power`
- `switch.switch_power_to_generator`
- `switch.generators_emergency_stop`

## UI / session helpers

- `input_boolean.automatic_generator_transfer`
- `input_boolean.generator_reserve_session_active`
- `input_select.generator_reserve_session_mode`
- `input_button.generator_reserve_start`
- `input_button.generator_return_to_grid`

## Notifications

- `script.notify_critical`
- `script.notify_warning`
- `logbook.log`

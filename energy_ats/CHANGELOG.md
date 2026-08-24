# Changelog

## 0.2.2

- Добавлена отдельная политика заслонки для Generator A / Elemax и
  Generator B / Вепрь: `always`, `temperature` или `never`.
- Текущие безопасные defaults: Elemax = `always`, Вепрь = `temperature`.
- Температурный порог остаётся экспериментальным; неизвестная температура
  в режиме `temperature` приводит к cold start.
- Решение по заслонке и выбранный режим теперь явно записываются в лог запуска.

## 0.2.1

- Исправлены реальные entity_id силовых logical switch:
  `switch.grid_power` и `switch.use_generator_as_power_source`.
- Учтена положительная полярность `switch.grid_power`:
  ON подключает Grid, OFF отключает Grid.
- Добавлен adapter-тест инверсии `grid_power -> grid_disconnected`.

## 0.2.0

- AppDaemon удалён из runtime-архитектуры.
- ATS оформлен как standalone Home Assistant App.
- Добавлен Home Assistant WebSocket client через Supervisor Core proxy.
- Добавлены русские переводы параметров App.
- `armed=false` реализован как жёсткий нижний safety gate.
- В DISARMED режиме выполняется только наблюдение, без ложной симуляции timeout state machine.
- Сохранён и повторно протестирован ATS core v1.1.
- Добавлены adapter/WebSocket tests; всего 29 тестов.

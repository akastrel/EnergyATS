# Changelog

## 0.2.3

- Исправлен аварийный fallback при остановке активного генератора под
  нагрузкой: перед запуском второго агрегата ATS снимает REMOTE с отказавшего,
  переводит силовой селектор из GENERATOR и ждёт подтверждений
  `Remote=False`, `SourceGenerator=False`, `HouseGen=False`.
- Резервный генератор теперь всегда запускается и прогревается без нагрузки;
  при неподтверждённой изоляции ATS переходит в terminal и включает Emergency
  Stop, не пытаясь запускать backup.
- Аппаратные команды failover выполняются до HA-уведомлений, поэтому медленный
  notify больше не сокращает фактический timeout запуска второго генератора.
- В режиме `ARMED` теперь логируются изменения всей физической картины, включая
  `ATS=True/False`, даже если текущая фаза state machine не изменилась.

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

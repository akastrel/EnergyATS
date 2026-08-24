# Changelog

## 0.2.0

- AppDaemon удалён из runtime-архитектуры.
- ATS оформлен как standalone Home Assistant App.
- Добавлен Home Assistant WebSocket client через Supervisor Core proxy.
- Добавлены русские переводы параметров App.
- `armed=false` реализован как жёсткий нижний safety gate.
- В DISARMED режиме выполняется только наблюдение, без ложной симуляции timeout state machine.
- Сохранён и повторно протестирован ATS core v1.1.
- Добавлены adapter/WebSocket tests; всего 29 тестов.

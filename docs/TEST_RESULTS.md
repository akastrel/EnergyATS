# Energy ATS App 0.2.1 — результаты тестирования

Дата: 2026-08-24

## Автоматические тесты

```text
30 passed
```

Проверены 23 сценария чистого ATS state machine из версии v1.1:

1. happy path Generator A + нормальный возврат Grid;
2. cold start с заслонкой и открытием через 10 s;
3. Grid вернулась во время preheat и снова пропала раньше 60 s;
4. Grid стабилизировалась во время preheat — transfer отменяется, generator cooldown;
5. Generator A не запустился -> fallback B;
6. A и B не запустились -> terminal + Emergency Stop;
7. генератор работает, но дом потерял питание от Generator -> terminal;
8. generator stop timeout -> Emergency Stop;
9. restart/recovery во время питания от Generator;
10. restart во время preheat -> полный preheat заново;
11. внешний ручной генератор не захватывается ATS;
12. manual reserve работает при ATS Enabled = OFF;
13. manual reserve блокируется Emergency Stop;
14. Grid снова пропала во время cooldown -> повторно используется уже работающий горячий генератор;
15. температурная таблица preheat;
16. Grid disconnect не подтвердился -> terminal;
17. Generator power после transfer не подтвердился -> terminal;
18. A заглох под нагрузкой -> один fallback на B;
19. manual session не возвращается на Grid автоматически;
20. ручной возврат использует тот же 60 s hysteresis;
21. restart на Grid при owned running generator -> полный cooldown заново;
22. terminal actions идут в фиксированном безопасном порядке;
23. неизвестная температура -> cold choke + максимальный preheat.

Дополнительно проверены 7 тестов standalone App/адаптера:

24. `/data/options.json` корректно объединяется с defaults;
25. App options правильно преобразуются в `ats_core.Config`;
26. `armed=false` физически подавляет любой Action на нижнем слое;
27. terminal Action преобразуются в HA service calls без изменения порядка;
28. `logbook.log` всегда получает `entity_id`;
29. реальный mock WebSocket round-trip: auth -> get_states -> subscribe_events -> state_changed -> call_service.
30. положительный `switch.grid_power` корректно инвертируется во внутренний
    признак `grid_disconnected`, включая сохранение `unavailable -> None`.

## Статические проверки

Успешно:

```text
python -m py_compile ats_core.py ha_client.py main.py
YAML parse: config.yaml, translations, energy.yaml, automations.yaml
```

## Что НЕ было возможно проверить в текущей среде

В среде сборки отсутствует Docker/Supervisor, поэтому здесь не выполнялись:

- фактическая Docker-сборка Home Assistant App;
- установка через Home Assistant Supervisor;
- соединение с реальным `ws://supervisor/core/websocket`;
- реальные HA service calls;
- физическое переключение генераторов/контакторов.

Именно поэтому `armed` по умолчанию = `false`. Первый запуск предназначен только для
проверки интеграции с реальным Home Assistant и чтения физической картины.

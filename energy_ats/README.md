# Energy ATS 0.5.0

Home Assistant App для управления резервным электроснабжением дома.

Базовая модель 0.4 сохраняется без изменения физической топологии:

- `UPS_ONLY` вместо виртуального Battery path;
- аппаратный FIFO owner общей генераторной шины;
- штатные два RUNNING;
- один managed fallback `PRIMARY -> SECONDARY`;
- внешний SECONDARY без автоматического захвата ownership;
- outage-related shutdown после возврата Grid;
- persistent owner/run-context между restart.

Версия 0.5 добавляет **Scheduled Generator Exercise** — независимый автоматический пробный запуск A и B после длительного простоя. Exercise использует существующий Generator Controller, оставляет дом на Grid, сохраняет ownership/timer между restart, не имеет maintenance fallback и может явно передать уже работающий test-generator обычной outage-сессии при реальной потере Grid.

Автоматические exercise по умолчанию выключены. Defaults после включения:

```text
A: 30 дней / 15:00 / 10 мин / grace 7 дней
B: 45 дней / 15:00 / 10 мин / grace 14 дней
```

Presence задаётся через `family_presence_entity` (default `group.family`). Presence является входом только Scheduler-а и не блокирует основную ATS-логику.

Основные документы:

- `../docs/PHYSICAL_POWER_TOPOLOGY_RU.md` — физическая схема;
- `../docs/REQUIREMENTS_RU.md` — требования;
- `../docs/ARCHITECTURE_RU.md` — архитектура;
- `../docs/ENTITIES_RU.md` — Home Assistant contract;
- `../docs/INSTALL_RU.md` — установка, обновление и физические испытания.

Для первого запуска использовать `armed: false`. Старый journal 0.3 автоматически в модель 0.4+ не мигрируется.

# Energy ATS 0.4.0

Home Assistant App для управления резервным электроснабжением дома.

Версия 0.4 перестраивает внутреннюю модель вокруг фактической физической схемы:

- `UPS_ONLY` вместо виртуального Battery path;
- аппаратный FIFO owner общей генераторной шины;
- штатные два RUNNING;
- один managed fallback `PRIMARY -> SECONDARY`;
- внешний SECONDARY без автоматического захвата ownership;
- outage-related shutdown после возврата Grid;
- исключение `TEST_RUN`;
- persistent owner/run-context между restart.

Основные документы:

- `../docs/PHYSICAL_POWER_TOPOLOGY_RU.md` — физическая схема;
- `../docs/REQUIREMENTS_RU.md` — требования;
- `../docs/ARCHITECTURE_RU.md` — архитектура;
- `../docs/ENTITIES_RU.md` — Home Assistant contract;
- `../docs/INSTALL_RU.md` — установка, обновление и физические испытания.

Для первого запуска и после обновления с 0.3.x использовать `armed: false`. Старый persistent journal автоматически в новую модель не мигрируется.

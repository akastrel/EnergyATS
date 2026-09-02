# Energy ATS

**Energy ATS** — Home Assistant App для автоматического ввода генераторного резерва (АВР) с физической обратной связью.

Проект предназначен для конкретной инженерной системы дома и пока находится в экспериментальной стадии. Управление построено как детерминированный конечный автомат на Python, а Home Assistant используется как источник физических состояний, UI и транспорт для service calls.

## Основные принципы

- физическая обратная связь — источник истины;
- primary generator выбирается в настройках App, второй разрешённый агрегат используется как backup;
- запуск генератора учитывает необходимость воздушной заслонки;
- время прогрева зависит от температуры;
- возврат Grid выполняется только после выдержки стабильности;
- после возврата генератор работает без нагрузки перед штатной остановкой;
- terminal failure переводит силовую часть в положение Grid и активирует общий `Generators Emergency Stop`;
- после ручной проверки terminal можно сбросить отдельной кнопкой без restart App;
- ручная остановка генератора работает и без внешней сети: UPS-линия остаётся на аккумуляторах МАП;
- текущая фаза (`Запускается`, `Прогрев`, `Питание от генератора` и т. п.) публикуется в UI;
- сложная логика ATS вынесена из Home Assistant YAML в отдельный Python state machine;
- первый запуск выполняется только в режиме `armed: false`.

## Архитектура

```text
Home Assistant package energy
        │
        │ states / UI helpers
        ▼
Home Assistant WebSocket API
        │
        ▼
Energy ATS App
├── main.py        # lifecycle, armed, recovery, логирование
├── ha_client.py   # Home Assistant WebSocket API
└── ats_core.py    # чистый конечный автомат АВР
        │
        ▼
ESPHome / Bolid / реальные реле и датчики
```

## Установка как Home Assistant Apps repository

Добавьте этот GitHub repository в Home Assistant Apps store:

```text
https://github.com/akastrel/EnergyATS
```

После обновления списка приложений появится **Energy ATS**.

> **Важно:** после установки оставить `armed: false`. В этом режиме App только наблюдает за реальными сущностями и не выполняет управляющих service calls.

Подробная инструкция: [`docs/INSTALL_RU.md`](docs/INSTALL_RU.md).

## Документация

- [`docs/ARCHITECTURE_RU.md`](docs/ARCHITECTURE_RU.md) — алгоритм и safety policy;
- [`docs/ENTITIES_RU.md`](docs/ENTITIES_RU.md) — используемые Home Assistant entities;
- [`docs/INSTALL_RU.md`](docs/INSTALL_RU.md) — первый безопасный запуск;
- [`docs/TEST_RESULTS.md`](docs/TEST_RESULTS.md) — результаты тестов;
- [`energy_ats/DOCS.md`](energy_ats/DOCS.md) — документация самого Home Assistant App.

## Home Assistant package `energy`

App ожидает несколько helper-ов Home Assistant для разрешения АВР, ручного ввода резерва и восстановления ownership после restart. Пример актуального package находится в:

```text
examples/homeassistant/packages/energy/
```

Это **пример для текущего дома**, а не универсальная часть устанавливаемого App.

## Разработка и тесты

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```

Тесты проверяют state machine отдельно от Home Assistant и транспортный адаптер WebSocket.

## Текущий статус

Версия App: **0.2.5**

ATS core: **v1.3**

Stage: **experimental**

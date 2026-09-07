# Energy ATS

Home Assistant App для управления источниками энергии частного дома с
подтверждаемой силовой коммутацией и отдельными автоматами двух генераторов.

Текущая версия: **0.3.13** (`experimental`).

Приложение остаётся одним процессом, но внутри разделено по ответственности:

```text
energy_supervisor.py      энергетическая политика и управляемые сессии
power_transfer.py         безопасный break-before-make
generator_controller.py  запуск, заслонка, прогрев и cooldown
ha_adapter.py             контракт Home Assistant и service calls
main.py                   lifecycle, синхронизация конфигурации и journal
```

## Основные принципы

- физическая обратная связь является источником истины;
- A/B — стабильные аппаратные слоты, а не пользовательские имена генераторов;
- имя и модель каждого генератора читаются из Home Assistant;
- основной генератор выбирается в `select.primary_generator`;
- смена primary влияет на следующую новую сессию и не меняет генератор уже
  начатой сессии;
- `generator_a_enabled` / `generator_b_enabled` остаются политикой Energy ATS
  и позволяют временно запретить использование физического слота;
- Power Transfer всегда выполняет break-before-make;
- внешний/локальный запуск распознаётся, но App его не захватывает;
- автоматический fallback после фактического отказа генератора пока отключён;
- перед аппаратными действиями записывается persistent transaction journal;
- потеря HA во время незавершённой транзакции приводит к
  `RECOVERY_REQUIRED`;
- `armed: false` полностью запрещает аппаратные команды.

## Контракт Generator Controller → Home Assistant → Energy ATS

Energy ATS 0.3.13 требует следующие сущности идентичности генераторов:

```text
sensor.generator_a_name
sensor.generator_b_name
sensor.generator_a_model
sensor.generator_b_model
select.primary_generator
```

Значение `select.primary_generator` должно совпадать с состоянием одного из
`sensor.generator_*_name`. Внутри Python эти значения преобразуются обратно в
стабильный слот `A` или `B`.

Имена и модели больше не хранятся в Configuration Energy ATS и не зашиты в
`generator_controller.py`.

## Установка

Добавьте repository в Home Assistant Apps store:

```text
https://github.com/akastrel/EnergyATS
```

Добавьте корневой `ats.yaml` как Home Assistant package. Он создаёт helper:

```text
input_boolean.automatic_generator_transfer
```

и документирует полный внешний HA-контракт Energy ATS 0.3.13.

Перед первым запуском или обновлением:

1. Grid доступна;
2. оба генератора остановлены;
3. генераторная шина отключена;
4. `armed: false`;
5. все обязательные HA entities имеют известные состояния.

## Ручные команды

Через `hassio.app_stdin` поддерживаются ровно три команды:

```text
start_generator
stop_generator
reset
```

Положение автоматического АВР хранится в
`input_boolean.automatic_generator_transfer`.

## Диагностика

App публикует `sensor.energy_ats_status`. Его state содержит
человеко-читаемый статус, а атрибуты включают:

```text
source
phase
generator
generator_model
generator_slot
primary_generator
primary_generator_slot
remaining_seconds
session_reason
armed
```

Этот sensor диагностический и не участвует в управляющих решениях.

## Документация

- [Архитектура](docs/ARCHITECTURE_RU.md)
- [Требования и сценарии](docs/REQUIREMENTS_RU.md)
- [Home Assistant entities](docs/ENTITIES_RU.md)
- [Установка и обновление](docs/INSTALL_RU.md)
- [Changelog](energy_ats/CHANGELOG.md)

## Разработка

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```

Чистые автоматы не импортируют Home Assistant и тестируются
детерминированными снимками физических состояний.

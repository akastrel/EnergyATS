# Energy ATS

Home Assistant App для управления источниками энергии дома с подтверждаемой
силовой коммутацией и отдельными автоматами Elemax и Вепря.

Версия **0.3.2** сохраняет проверенные сценарии из
`docs/REQUIREMENTS_RU.md`, но переносит ручные команды и настройку АВР из
пользовательских HA-helper-ов внутрь App. Приложение остаётся одним процессом:

```text
energy_supervisor.py      энергетическая политика и сессии
power_transfer.py        безопасный break-before-make
generator_controller.py  запуск, заслонка, прогрев и cooldown
ha_adapter.py             Home Assistant entities и service calls
main.py                   lifecycle и журнал транзакций
```

## Основные свойства

- физическая обратная связь является источником истины;
- Generator Controller не знает о Grid, МАП и контакторах дома;
- Power Transfer не знает, зачем выбран источник, и не запускает двигатели;
- внешний/локальный запуск только распознаётся — App его не захватывает;
- ручная остановка при отсутствующей Grid штатно возвращает дом на аккумуляторы
  МАП;
- после такой ручной остановки АВР подавлен до возврата Grid или новой
  ручной команды запуска;
- при возврате Grid после ручного outage-запуска дом возвращается на сеть, но
  двигатель без отдельной команды не останавливается;
- автоматический fallback на второй генератор в 0.3.2 отключён;
- АВР по умолчанию выключен, хранится внутри App и управляется stdin-командами;
- пользовательские HA-helper-ы для Energy ATS не требуются;
- перед аппаратными действиями записывается persistent transaction journal;
- потеря HA во время транзакции требует ручного recovery, устойчивое состояние
  не меняется;
- `armed: false` полностью запрещает аппаратные команды.

## Установка

Добавьте repository в Home Assistant Apps store:

```text
https://github.com/akastrel/EnergyATS
```

Для 0.3.2 требуется Generator Controller 0.3.1 с однозначными кнопками
`choke_to_cold_start/choke_to_run`. Обновляться следует при
работающей Grid, остановленных генераторах и `armed: false`.

Подробности: [установка и миграция](docs/INSTALL_RU.md).

## Документация

- [архитектура и поведение](docs/ARCHITECTURE_RU.md)
- [утверждённые сценарии и требования](docs/REQUIREMENTS_RU.md)
- [Home Assistant entities](docs/ENTITIES_RU.md)
- [установка и первый запуск](docs/INSTALL_RU.md)
- [проверки релиза](docs/TEST_RESULTS.md)

## Разработка

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```

Чистые автоматы не импортируют Home Assistant и тестируются обычными
детерминированными снимками состояния.

Текущий статус: **0.3.2**, experimental.

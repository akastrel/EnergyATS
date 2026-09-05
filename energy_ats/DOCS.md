# Energy ATS 0.3.2

Energy ATS теперь содержит Energy Supervisor, безопасный Power Transfer и два
независимых Generator Controller в одном Python-процессе.

## Перед обновлением

1. Обеспечьте питание от Grid.
2. Остановите оба генератора и снимите оба REMOTE.
3. Установите `armed: false`.
4. Прошейте `generator-controller.yaml` 0.3.1: App ожидает новые entities
   `button.generator_*_choke_to_cold_start` и `choke_to_run`.
5. Затем обновите и запустите этот App.

## ARMED и автоматический АВР

`armed` — нижний предохранитель всего App:

- `false`: аппаратные switch/button calls запрещены;
- `true`: контроллерам разрешено исполнять подтверждаемые операции.

Положение АВР хранится внутри App, переживает restart и по умолчанию равно
`OFF`. Оно меняется командами `automatic_transfer_on` и
`automatic_transfer_off` через `hassio.app_stdin`.

Автоматический fallback с A на B в версии 0.3.2 отключён.

## Ручные команды

- `start_backup` — создать управляемую сессию, запустить
  выбранный генератор, прогреть и безопасно ввести его.
- `stop_generator` — сначала вернуть дом на Grid либо аккумуляторы МАП,
  затем выполнить cooldown и снять REMOTE.
- `reset_recovery` — после ручного осмотра выйти из
  `RECOVERY_REQUIRED`, только если оба двигателя и REMOTE выключены, а силовая
  схема подтверждена в Grid path.

Пример вызова:

```yaml
action: hassio.app_stdin
data:
  app: YOUR_ENERGY_ATS_APP_ID
  input:
    command: start_backup
```

Пользовательские HA-helper-ы для Energy ATS не требуются. Текущее состояние
контроллеров и АВР записывается в журнал App.

Фактический `app` ID лучше выбрать в визуальном редакторе действия Home
Assistant, выбрав **Energy ATS** из списка.

Внешне/локально запущенный двигатель App только отображает и не останавливает.

## Потеря связи

Устойчивая работа сохраняется как есть. Если WebSocket или процесс потерян во
время незавершённой физической транзакции, автоматическое продолжение
блокируется. Точный список pending-команд хранится в persistent журнале App.

Подробная спецификация находится в `docs/ARCHITECTURE_RU.md` repository.

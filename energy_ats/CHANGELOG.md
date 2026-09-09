# Changelog

## 0.4.0

- Перестроена доменная модель EnergyATS вокруг фактической физической топологии электроснабжения.
- Удалён виртуальный силовой `Battery path`; состояние работы только UPS-линии представляется как `UPS_ONLY`.
- Добавлена явная модель владельца общей генераторной шины с аппаратным FIFO-поведением и сохранением состояния между restart.
- Одновременная работа Generator A и Generator B теперь является штатным состоянием и не считается нарушением interlock.
- Реализован единственный автоматический fallback `PRIMARY -> SECONDARY` без ping-pong.
- Уже работающий внешний SECONDARY не захватывается EnergyATS в managed ownership.
- Добавлены run-context `OUTAGE_RELATED`, `TEST_RUN` и `EXTERNAL`.
- После стабильного восстановления Grid EnergyATS завершает outage и останавливает все outage-related генераторы; `TEST_RUN` не останавливается этим правилом.
- Обновлены status/log: bus owner, run context, fallback state и `UPS_ONLY`.
- Persistent journal переведён на новую схему v0.4 без миграции ошибочной модели v0.3.
- Переписаны сценарные тесты под физическую модель v0.4.


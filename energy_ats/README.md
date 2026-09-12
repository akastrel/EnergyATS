# Energy ATS

Home Assistant App для управления резервным электроснабжением дома с двумя генераторами, общей generator bus и отдельной UPS-линией через МАП.

## Основные возможности

- автоматический и ручной переход Grid -> Generator и обратно;
- безопасный break-before-make transfer с подтверждением каждого шага;
- выбор PRIMARY generator в Home Assistant и один fallback на SECONDARY;
- корректная работа с двумя одновременно RUNNING генераторами и аппаратным FIFO-owner общей шины;
- `UPS_ONLY` без отдельного Battery contactor;
- **UPS Run**: Delayed Start и charge cycling для длительных отключений;
- **Scheduled Exercise** для периодических пробных запусков A/B;
- **Load Manager** для двух некритичных групп G1/G2;
- Recovery при неоднозначном физическом состоянии;
- причинно-следственный Logbook с отдельными trigger/action/feedback/result событиями;
- persistent state и диагностический `sensor.energy_ats_status`.

## Важно перед включением ARMED

EnergyATS управляет реальными генераторами и контакторами. Для первого запуска рекомендуется:

```yaml
armed: false
```

В этом режиме App читает Home Assistant, восстанавливает внутреннюю модель и публикует status, но не должна выдавать аппаратные команды.

Перед `armed: true` проверьте:

- `binary_sensor.grid_input_ready`;
- `binary_sensor.house_powered_by_grid` / `_by_generator`;
- RUNNING/REMOTE обоих генераторов;
- `switch.grid_power` и `switch.use_generator_as_power_source`;
- Emergency Stop;
- имена/модели генераторов и `select.primary_generator`.

После этого выполните физические commissioning-тесты из `USER_TESTS_RU.md`.

## UPS Run

UPS Run определяет стратегию длительного outage. Две функции включаются независимо и по умолчанию выключены:

- **Delayed Start** — после обычного `grid_failure_delay` можно продолжать работу только от UPS до порога SoC/TTG/max-delay;
- **Charge Cycling** — cycle-owned automatic outage session можно завершить при Target SoC, перейти в `UPS_ONLY`, остановить generator и позже запустить следующий цикл.

Manual request имеет приоритет над ожиданием/cycle stop. Stable Grid имеет приоритет над Target SoC. При плохой battery telemetry EnergyATS отказывается от задержки и использует обычный безопасный generator start.

## Scheduled Exercise

Плановые пробные запуски настраиваются отдельно для A и B и по умолчанию выключены. Ordinary Exercise выполняется только при подтверждённом отсутствии семьи; forced run после grace требует заранее успешно доставленного warning. Exercise не переводит дом с Grid на generator bus и не имеет maintenance fallback.

## Load Manager

При `load_management_enabled: true` EnergyATS управляет только:

```text
G1 = switch.non_critical_loads_first_floor
G2 = switch.non_critical_loads_basement_floor
```

Load Manager снимает некритичные нагрузки перед generator transfer, возвращает их по одной при достаточном запасе мощности и выполняет shedding при устойчивой перегрузке. Meter/G1/G2/power metadata являются soft dependencies: их отказ не должен ломать core ATS.

## Причинный Logbook

Журнал разделяет причину, команду и реально наблюдённый результат. Для ключевых сценариев можно восстановить последовательность:

```text
изменение входного/physical signal
  -> решение EnergyATS
  -> аппаратное действие
  -> подтверждённый feedback
```

Отдельно фиксируются Grid, PowerPath/PowerSource, RUNNING/REMOTE обоих генераторов, generator bus owner, Emergency Stop, manual commands, UPS Run, Scheduled Exercise и Recovery. Первый snapshot после start/reconnect используется только как baseline и не создаёт ложных событий.

Внешний запуск генератора отличим от managed-запуска EnergyATS. Новые причинные пользовательские тексты формируются через стабильные message keys и русский каталог `app/user_messages_ru.py`, поэтому формулировки можно локализовать без изменения FSM.

App log является полным последовательным журналом: он содержит MAIN/DETAIL events, аппаратные команды и отправляемые пользовательские сообщения. Основной поток Home Assistant Logbook содержит только существенные MAIN events и не использует отдельную DETAIL-сущность.

## Управление

Поддерживаются команды App:

```text
start_generator
stop_generator
reset
```

`reset` запускает контролируемое Recovery. Это не безусловный сброс ошибки: внешний generator, E-stop или неоднозначное физическое состояние могут блокировать восстановление.

## Status

App публикует:

```text
sensor.energy_ats_status
```

Он показывает фактический source, phase, generator/bus owner, PRIMARY, managed session, Exercise, UPS Run и Load Manager state. Status является диагностикой и не используется как управляющий input.

## Документация

Вкладка **Documentation** этого App содержит практическое руководство (`DOCS.md`). Полные документы проекта:

- [Физическая схема](https://github.com/akastrel/EnergyATS/blob/main/docs/PHYSICAL_POWER_TOPOLOGY_RU.md)
- [Требования](https://github.com/akastrel/EnergyATS/blob/main/docs/REQUIREMENTS_RU.md)
- [Архитектура](https://github.com/akastrel/EnergyATS/blob/main/docs/ARCHITECTURE_RU.md)
- [Home Assistant entities](https://github.com/akastrel/EnergyATS/blob/main/docs/ENTITIES_RU.md)
- [Установка и обновление](https://github.com/akastrel/EnergyATS/blob/main/docs/INSTALL_RU.md)
- [Физические тесты](https://github.com/akastrel/EnergyATS/blob/main/docs/USER_TESTS_RU.md)
- [Changelog](https://github.com/akastrel/EnergyATS/blob/main/energy_ats/CHANGELOG.md)

CI выполняет полный Python suite и production-container smoke. Это не заменяет проверку на реальной электроустановке.

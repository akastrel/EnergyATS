# Energy ATS — архитектура и спецификация поведения

## 1. Источник истины

ATS не хранит положение силовой системы в собственном persistent state.
После restart/reconnect он заново читает физическую обратную связь:

- `binary_sensor.grid_input_ready`
- `binary_sensor.house_powered_by_grid`
- `binary_sensor.house_powered_by_generator`
- `binary_sensor.generator_a_is_running`
- `binary_sensor.generator_b_is_running`
- `switch.disconnect_grid_power`
- `switch.switch_power_to_generator`
- `switch.generator_a_remote_start`
- `switch.generator_b_remote_start`
- `switch.generators_emergency_stop`

Helper-ы Home Assistant хранят только **намерение/владение сессией**, а не физическую истину.

## 2. Автоматический ввод резерва

Happy path:

```text
GRID
  -> Grid Input Ready OFF непрерывно 5 s
  -> START Generator A
  -> при необходимости cold choke
  -> RUNNING подтверждён
  -> choke hold 10 s
  -> открыть choke
  -> temperature-dependent PREHEAT
  -> отключить Grid перед силовым selector (дополнительная перестраховка)
  -> подтвердить House Powered by Grid = OFF
  -> selector -> Generator
  -> подтвердить House Powered by Generator = ON
  -> ON GENERATOR
```

Если Generator A не стартовал за 90 s, ATS отправляет CRITICAL и один раз пробует Generator B.
Если оба не стартовали — terminal failure.

## 3. Заслонка

Пока датчика температуры двигателя нет, решение принимается по `sensor.garage_temperature`.
Если температура недоступна — используется самый консервативный cold start.

В текущем ESPHome entity_id названы наоборот относительно механического действия:

- `button.generator_*_choke_open` = **физически закрыть** заслонку, cold start;
- `button.generator_*_choke_close` = **физически открыть** заслонку, RUN/hot start.

По умолчанию cold choke используется при T < +10 °C. Порог экспериментальный и будет
настраиваться по реальным запускам.

## 4. Прогрев

По температуре гаража:

- T >= +10 °C: 30 s
- -5 °C < T < +10 °C: 60 s
- -10 °C < T <= -5 °C: 180 s
- T <= -10 °C: 300 s
- температура unavailable: 300 s

Preheat отсчитывается после перевода заслонки в нормальное рабочее положение.

## 5. Возврат Grid

После появления `Grid Input Ready = ON` сеть должна оставаться стабильной 60 s.
Краткий возврат на 10–20 s ничего не переключает: таймер сбрасывается при новом провале.

После стабильных 60 s:

```text
selector -> Grid
подтвердить House Powered by Generator = OFF
подключить Grid
подтвердить House Powered by Grid = ON
Generator работает без нагрузки 300 s
REMOTE START -> OFF
подтвердить RUNNING = OFF до 90 s
```

Даже если Grid вернулась ещё во время START/CHOKE/PREHEAT, используется тот же единый
cooldown 300 s.

## 6. Потеря работающего генератора

Если текущий генератор перестал работать/питать дом, ATS пытается использовать второй
генератор, если он ещё не был признан отказавшим в этой резервной сессии.

Бесконечного A -> B -> A -> B нет: уже отказавший генератор повторно в рамках той же
сессии не используется.

## 7. Terminal failure

При неопределённой или неустранимой аварии ATS перестаёт экспериментировать с системой.
Общий fail-safe порядок:

```text
REMOTE A -> OFF
REMOTE B -> OFF
силовой selector -> GRID
Grid disconnect -> OFF  (нормальное положение — Grid подключён)
Generators Emergency Stop -> ON
CRITICAL notification
Logbook
TERMINAL: никаких дальнейших автоматических действий
```

`switch.generators_emergency_stop` имеет двойную семантику:

- на работающем генераторе: останавливает двигатель и переводит DKG116 в EMERG;
- на неработающем: блокирует дальнейший remote/manual start; попытка запуска приводит DKG116 в EMERG.

ATS никогда автоматически не снимает этот switch после terminal failure.

## 8. Restart/reconnect

После restart Home Assistant App или потери WebSocket старая фаза state machine не считается
достоверной. App создаёт новый `ATSController`, читает физические датчики и выполняет recovery.

Если owned generator уже работает, но время предыдущего прогрева неизвестно, полный preheat
выполняется заново. Потеря нескольких минут безопаснее подключения нагрузки к потенциально
непрогретому двигателю.

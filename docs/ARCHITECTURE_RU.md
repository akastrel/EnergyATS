# Energy ATS — архитектура и спецификация поведения

## 1. Источник истины

ATS не хранит положение силовой системы в собственном persistent state.
После restart/reconnect он заново читает физическую обратную связь:

- `binary_sensor.grid_input_ready`
- `binary_sensor.house_powered_by_grid`
- `binary_sensor.house_powered_by_generator`
- `binary_sensor.generator_a_is_running`
- `binary_sensor.generator_b_is_running`
- `switch.grid_power` — ON = Grid подключён, OFF = Grid отключён
- `switch.use_generator_as_power_source` — ON = Generator, OFF = Grid
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

Политика задаётся отдельно для каждого генератора:

- `always` — всегда физически закрывать заслонку перед запуском;
- `temperature` — закрывать при T ниже `choke_temperature`; неизвестная
  температура означает cold start;
- `never` — всегда запускать с открытой заслонкой.

Текущее эмпирическое состояние системы:

- Generator A / Elemax: `always`;
- Generator B / Вепрь: `temperature`, начальный порог +10 °C.

Порог и сама модель остаются экспериментальными. Зависимость от длительности
простоя пока не реализована: после restart нельзя достоверно определить реальное
время с последнего запуска двигателя.

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

### Ручная остановка

`input_button.generator_return_to_grid` в 0.2.5 означает не только «вернуться
на Grid», а глобальную команду **остановить управляемый генератор**. Она может
прервать запуск, прогрев, переключение или штатную работу.

Порядок всегда подтверждаемый:

```text
selector -> Grid
подтвердить SourceGenerator = OFF и HouseGen = OFF
Grid power -> ON
подтвердить нормальное положение Grid power
если Grid доступен — подтвердить HouseGrid = ON
Generator без нагрузки: cooldown
REMOTE A/B -> OFF
подтвердить RUNNING A/B = OFF
```

Если `Grid Input Ready = OFF`, команда не отклоняется и не создаёт warning.
Selector и Grid power всё равно возвращаются в нормальное положение. Внешнее
питание при этом отсутствует, а критические нагрузки продолжают работать от
аккумуляторов МАП. Исчезновение Grid во время ручной остановки не отменяет
явное намерение оператора и не возвращает дом на генератор. Даже если
`automatic_generator_transfer = ON`, генератор не запускается повторно до
возврата Grid или новой ручной команды `Включить резервное питание`.

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
силовой selector -> GRID (`switch.use_generator_as_power_source` -> OFF)
Grid power -> ON  (нормальное положение — Grid подключён)
Generators Emergency Stop -> ON
CRITICAL notification
Logbook
TERMINAL: никаких дальнейших автоматических действий
```

`switch.generators_emergency_stop` имеет двойную семантику:

- на работающем генераторе: останавливает двигатель и переводит DKG116 в EMERG;
- на неработающем: блокирует дальнейший remote/manual start; попытка запуска приводит DKG116 в EMERG.

ATS никогда автоматически не снимает этот switch после terminal failure.

В 0.2.5 выйти из TERMINAL можно без restart App кнопкой
`input_button.generator_ats_reset`. Перед reset оператор вручную устраняет
причину и снимает Emergency Stop. Контроллер принимает reset только если:

- `Generators Emergency Stop = OFF`;
- оба `generator_*_is_running = OFF`;
- оба `generator_*_remote_start = OFF`;
- `use_generator_as_power_source = OFF` и `HouseGen = OFF`;
- `grid_power = ON`.

После успешной проверки старые phase/session данные очищаются, и контроллер
сразу возвращается в безопасную исходную фазу `GRID`. Перезапуск процесса не
требуется.

## 8. UI-статус

Если в package создан `input_text.generator_ats_status`, App публикует туда
человекочитаемую текущую фазу: `Запускается: Elemax`, `Прогрев: Elemax`,
`Питание от генератора: Elemax`, `Ручная остановка: ...`,
`Авария ATS — требуется сброс` и т. п. Helper используется только для UI и
никогда не участвует в принятии силовых решений.

## 9. Restart/reconnect

После restart Home Assistant App или потери WebSocket старая фаза state machine не считается
достоверной. App создаёт новый `ATSController`, читает физические датчики и выполняет recovery.

Если owned generator уже работает, но время предыдущего прогрева неизвестно, полный preheat
выполняется заново. Потеря нескольких минут безопаснее подключения нагрузки к потенциально
непрогретому двигателю.

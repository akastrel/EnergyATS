"""Граница между чистыми контроллерами и Home Assistant.

Все entity_id и HA service calls собраны здесь. Доменные автоматы получают
обычные dataclass-снимки и не зависят от протокола Home Assistant.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Hashable

from domain import GeneratorSlot, SupervisorEvent
from generator_controller import (
    GeneratorAction,
    GeneratorActionKind,
    GeneratorObservation,
)
from ha_client import HomeAssistantClient
from load_manager import LoadAction, LoadActionKind, LoadGroup
from outage_power_policy import BatteryObservation
from power_transfer import (
    PowerTransferObservation,
    TransferAction,
    TransferActionKind,
)


ENTITIES = {
    "automatic_transfer": "input_boolean.automatic_generator_transfer",
    "test_mode": "input_boolean.generator_test_mode",
    "grid_ready": "binary_sensor.grid_input_ready",
    "house_grid": "binary_sensor.house_powered_by_grid",
    "house_generator": "binary_sensor.house_powered_by_generator",
    "generator_a_running": "binary_sensor.generator_a_is_running",
    "generator_b_running": "binary_sensor.generator_b_is_running",
    "generator_a_remote": "switch.generator_a_remote_start",
    "generator_b_remote": "switch.generator_b_remote_start",
    "generator_a_choke_cold_start": "button.generator_a_choke_to_cold_start",
    "generator_a_choke_run": "button.generator_a_choke_to_run",
    "generator_b_choke_cold_start": "button.generator_b_choke_to_cold_start",
    "generator_b_choke_run": "button.generator_b_choke_to_run",
    "generator_a_name": "sensor.generator_a_name",
    "generator_b_name": "sensor.generator_b_name",
    "generator_a_model": "sensor.generator_a_model",
    "generator_b_model": "sensor.generator_b_model",
    "generator_a_nominal_power": "sensor.generator_a_nominal_power",
    "generator_a_maximum_power": "sensor.generator_a_maximum_power",
    "generator_b_nominal_power": "sensor.generator_b_nominal_power",
    "generator_b_maximum_power": "sensor.generator_b_maximum_power",
    "primary_generator": "select.primary_generator",
    "emergency_stop": "switch.generators_emergency_stop",
    "ambient_temperature_external": "sensor.garage_temperature",
    "grid_power": "switch.grid_power",
    "source_generator": "switch.use_generator_as_power_source",
    "generator_meter_status": "binary_sensor.generator_meter_status",
    "generator_power": "sensor.generator_power",
    "generator_current": "sensor.generator_current",
    "generator_voltage": "sensor.generator_voltage",
    "generator_apparent_power": "sensor.generator_apparent_power",
    "generator_reactive_power": "sensor.generator_reactive_power",
    "generator_power_factor": "sensor.generator_power_factor",
    "generator_frequency": "sensor.generator_frequency",
    "load_g1": "switch.non_critical_loads_first_floor",
    "load_g2": "switch.non_critical_loads_basement_floor",
    # Battery inputs Delayed Start / Charge Cycling. Это soft dependencies:
    # отсутствие любого из них не должно блокировать core ATS.
    "ups_battery_soc": "sensor.ups_battery_charge_level_soc",
    "ups_battery_ttg_minutes": "sensor.ups_battery_time_remaining_minutes_ttg",
    "ups_running_on_battery": "binary_sensor.ups_running_on_battery",
    "ups_ready": "binary_sensor.ups_ready",
}

ENERGY_ATS_LOG_ENTITY = "update.energy_ats_update"
ENERGY_ATS_STATUS_ENTITY = "sensor.energy_ats_status"


@dataclass(frozen=True)
class GeneratorMetadata:
    name: str
    model: str
    nominal_power: float | None = None
    maximum_power: float | None = None


@dataclass(frozen=True)
class LoadManagementSnapshot:
    meter_ready: bool | None
    generator_power: float | None
    power_sample_id: Hashable | None
    generator_current: float | None
    generator_voltage: float | None
    generator_apparent_power: float | None
    generator_reactive_power: float | None
    generator_power_factor: float | None
    generator_frequency: float | None
    groups: dict[LoadGroup, bool | None]


@dataclass(frozen=True)
class HardwareSnapshot:
    grid_ready: bool | None
    automatic_transfer_enabled: bool
    test_mode: bool | None
    emergency_stop: bool | None
    family_present: bool | None
    generators: dict[GeneratorSlot, GeneratorObservation]
    generator_metadata: dict[GeneratorSlot, GeneratorMetadata | None]
    primary_generator: GeneratorSlot | None
    power_transfer: PowerTransferObservation
    load_management: LoadManagementSnapshot
    battery: BatteryObservation


class UnsafeHardwareCommand(RuntimeError):
    """Команда нарушает локальное физическое предусловие адаптера."""


class HomeAssistantAdapter:
    def __init__(
        self,
        client: HomeAssistantClient,
        *,
        armed: bool,
        logger: logging.Logger | None = None,
        family_presence_entity: str | None = None,
        require_family_presence: bool = False,
    ) -> None:
        self.client = client
        self.armed = armed
        self.log = logger or logging.getLogger(__name__)
        self.family_presence_entity = (
            family_presence_entity.strip()
            if isinstance(family_presence_entity, str) and family_presence_entity.strip()
            else None
        )
        # Presence — мягкий input только для Exercise Scheduler. Даже когда
        # scheduled exercise включён, unknown/unavailable presence не должен
        # блокировать запуск самого ATS: Scheduler просто отложит обычный test.
        self.require_family_presence = require_family_presence

    def snapshot(self) -> HardwareSnapshot:
        grid_ready = self.bool_state(ENTITIES["grid_ready"])
        house_grid = self.bool_state(ENTITIES["house_grid"])
        house_generator = self.bool_state(ENTITIES["house_generator"])
        emergency_stop = self.bool_state(ENTITIES["emergency_stop"])
        running = {
            GeneratorSlot.A: self.bool_state(ENTITIES["generator_a_running"]),
            GeneratorSlot.B: self.bool_state(ENTITIES["generator_b_running"]),
        }
        ambient_temperature = self.float_state(
            ENTITIES["ambient_temperature_external"]
        )

        names = {
            GeneratorSlot.A: self.text_state(ENTITIES["generator_a_name"]),
            GeneratorSlot.B: self.text_state(ENTITIES["generator_b_name"]),
        }
        models = {
            GeneratorSlot.A: self.text_state(ENTITIES["generator_a_model"]),
            GeneratorSlot.B: self.text_state(ENTITIES["generator_b_model"]),
        }
        nominal_powers = {
            GeneratorSlot.A: self.float_state(ENTITIES["generator_a_nominal_power"]),
            GeneratorSlot.B: self.float_state(ENTITIES["generator_b_nominal_power"]),
        }
        maximum_powers = {
            GeneratorSlot.A: self.float_state(ENTITIES["generator_a_maximum_power"]),
            GeneratorSlot.B: self.float_state(ENTITIES["generator_b_maximum_power"]),
        }
        metadata = {
            slot: (
                GeneratorMetadata(
                    name=names[slot],
                    model=models[slot],
                    nominal_power=nominal_powers[slot],
                    maximum_power=maximum_powers[slot],
                )
                if names[slot] is not None and models[slot] is not None
                else None
            )
            for slot in (GeneratorSlot.A, GeneratorSlot.B)
        }

        primary_name = self.text_state(ENTITIES["primary_generator"])
        primary_matches = [
            slot
            for slot, name in names.items()
            if primary_name is not None and name == primary_name
        ]
        primary_generator = (
            primary_matches[0] if len(primary_matches) == 1 else None
        )

        # generator_test_mode — только положительный маркер внешнего TEST_RUN.
        # Если helper вообще не установлен, это эквивалентно OFF: запуск не был
        # явно помечен тестовым. Но если существующий helper временно
        # unknown/unavailable, сохраняем None и не угадываем его состояние.
        test_mode_entity = ENTITIES["test_mode"]
        test_mode = (
            self.bool_state(test_mode_entity)
            if self.client.has_entity(test_mode_entity)
            else False
        )

        # По RUNNING нельзя определять владельца общей генераторной шины, когда
        # работают оба двигателя. До GeneratorBusTracker здесь известен только
        # факт, что при снятой генераторной ветви нагрузки точно нет.
        load_connected = False if house_generator is False else None
        generators = {
            GeneratorSlot.A: GeneratorObservation(
                running=running[GeneratorSlot.A],
                remote_on=self.bool_state(ENTITIES["generator_a_remote"]),
                load_connected=load_connected,
                emergency_stop=emergency_stop,
                ambient_temperature_external=ambient_temperature,
            ),
            GeneratorSlot.B: GeneratorObservation(
                running=running[GeneratorSlot.B],
                remote_on=self.bool_state(ENTITIES["generator_b_remote"]),
                load_connected=load_connected,
                emergency_stop=emergency_stop,
                ambient_temperature_external=ambient_temperature,
            ),
        }

        load_management = LoadManagementSnapshot(
            meter_ready=self.bool_state(ENTITIES["generator_meter_status"]),
            generator_power=self.float_state(ENTITIES["generator_power"]),
            power_sample_id=self.state_revision(ENTITIES["generator_power"]),
            generator_current=self.float_state(ENTITIES["generator_current"]),
            generator_voltage=self.float_state(ENTITIES["generator_voltage"]),
            generator_apparent_power=self.float_state(
                ENTITIES["generator_apparent_power"]
            ),
            generator_reactive_power=self.float_state(
                ENTITIES["generator_reactive_power"]
            ),
            generator_power_factor=self.float_state(ENTITIES["generator_power_factor"]),
            generator_frequency=self.float_state(ENTITIES["generator_frequency"]),
            groups={
                LoadGroup.G1: self.bool_state(ENTITIES["load_g1"]),
                LoadGroup.G2: self.bool_state(ENTITIES["load_g2"]),
            },
        )

        battery = BatteryObservation(
            soc=self.float_state(ENTITIES["ups_battery_soc"]),
            ttg_minutes=self.float_state(ENTITIES["ups_battery_ttg_minutes"]),
            discharging=self.bool_state(ENTITIES["ups_running_on_battery"]),
            ready=self.bool_state(ENTITIES["ups_ready"]),
            sample_id=self.state_revision(ENTITIES["ups_battery_soc"]),
            ttg_sample_id=self.state_revision(ENTITIES["ups_battery_ttg_minutes"]),
            soc_updated_at=self.state_updated_at(ENTITIES["ups_battery_soc"]),
            ttg_updated_at=self.state_updated_at(ENTITIES["ups_battery_ttg_minutes"]),
        )

        return HardwareSnapshot(
            grid_ready=grid_ready,
            automatic_transfer_enabled=(
                self.bool_state(ENTITIES["automatic_transfer"]) is True
            ),
            test_mode=test_mode,
            emergency_stop=emergency_stop,
            family_present=(
                self.presence_state(self.family_presence_entity)
                if self.family_presence_entity is not None
                else None
            ),
            generators=generators,
            generator_metadata=metadata,
            primary_generator=primary_generator,
            power_transfer=PowerTransferObservation(
                grid_ready=grid_ready,
                house_on_grid=house_grid,
                house_on_generator=house_generator,
                grid_connected=self.bool_state(ENTITIES["grid_power"]),
                generator_selected=self.bool_state(ENTITIES["source_generator"]),
                emergency_stop=emergency_stop,
            ),
            load_management=load_management,
            battery=battery,
        )

    def missing_required_entities(
        self,
        *,
        include_control_entities: bool = True,
    ) -> list[str]:
        # Load Manager, battery-policy entities and Nominal/Maximum metadata
        # намеренно не входят сюда: это soft dependencies и они не могут
        # блокировать старт core ATS.
        state_required = [
            ENTITIES["automatic_transfer"],
            ENTITIES["grid_ready"],
            ENTITIES["house_grid"],
            ENTITIES["house_generator"],
            ENTITIES["generator_a_running"],
            ENTITIES["generator_b_running"],
            ENTITIES["generator_a_remote"],
            ENTITIES["generator_b_remote"],
            ENTITIES["generator_a_name"],
            ENTITIES["generator_b_name"],
            ENTITIES["generator_a_model"],
            ENTITIES["generator_b_model"],
            ENTITIES["primary_generator"],
            ENTITIES["emergency_stop"],
            ENTITIES["grid_power"],
            ENTITIES["source_generator"],
        ]

        existence_only: list[str] = []
        if include_control_entities:
            existence_only = [
                ENTITIES["generator_a_choke_cold_start"],
                ENTITIES["generator_a_choke_run"],
                ENTITIES["generator_b_choke_cold_start"],
                ENTITIES["generator_b_choke_run"],
            ]

        missing = [
            entity_id
            for entity_id in state_required
            if self.client.get_state(entity_id) in (None, "unknown", "unavailable")
        ]
        missing.extend(
            entity_id
            for entity_id in existence_only
            if not self.client.has_entity(entity_id)
        )
        return missing

    async def execute_actions(
        self,
        transfer_actions: list[TransferAction],
        generator_actions: list[GeneratorAction],
    ) -> None:
        """Выполнить силовые команды, затем best-effort Logbook."""

        log_entries: list[tuple[str, str]] = []
        for action in transfer_actions:
            if not self.armed:
                self.log.info("DISARMED: подавлена команда %s", action)
                continue
            self._assert_transfer_action_safe(action)
            entity_id, service = self._transfer_service(action.kind)
            await self.client.call_service(
                "switch",
                service,
                service_data={"entity_id": entity_id},
            )
            log_entries.append((action.message, entity_id))

        for action in generator_actions:
            if not self.armed:
                self.log.info("DISARMED: подавлена команда %s", action)
                continue
            self._assert_generator_action_safe(action)
            entity_id, domain, service = self._generator_service(action)
            await self.client.call_service(
                domain,
                service,
                service_data={"entity_id": entity_id},
            )
            log_entries.append((action.message, entity_id))

        await self._publish_log_entries(log_entries)

    async def execute_load_actions(
        self,
        actions: list[LoadAction],
    ) -> list[tuple[LoadAction, str]]:
        """Выполнить G1/G2-команды как soft dependency.

        Ошибка consumer switch не должна ронять рабочий цикл и превращаться в
        системный Recovery. Поэтому failures возвращаются Load Manager-у как
        локальные ошибки вместо исключения наружу.
        """

        failures: list[tuple[LoadAction, str]] = []
        log_entries: list[tuple[str, str]] = []
        for action in actions:
            if not self.armed:
                self.log.info("DISARMED: подавлена команда Load Manager %s", action)
                continue
            entity_id = (
                ENTITIES["load_g1"]
                if action.group == LoadGroup.G1
                else ENTITIES["load_g2"]
            )
            service = (
                "turn_on" if action.kind == LoadActionKind.TURN_ON else "turn_off"
            )
            try:
                await self.client.call_service(
                    "switch",
                    service,
                    service_data={"entity_id": entity_id},
                )
            except Exception as exc:
                self.log.warning(
                    "Load Manager не выполнил %s для %s: %s",
                    service,
                    entity_id,
                    exc,
                )
                failures.append((action, str(exc)))
                continue
            log_entries.append((action.message, entity_id))

        await self._publish_log_entries(log_entries)
        return failures

    async def publish_events(self, events: tuple[SupervisorEvent, ...]) -> None:
        for event in events:
            try:
                await self._logbook(event.message, ENERGY_ATS_LOG_ENTITY)
            except Exception as exc:
                self.log.warning(
                    "Не удалось записать событие Energy ATS в Logbook: %s",
                    exc,
                )
            if not self.armed or event.level != "critical":
                continue
            try:
                await self.client.call_service(
                    "script",
                    "notify_critical",
                    service_data={"message": event.message},
                )
            except Exception as exc:
                self.log.warning(
                    "Не удалось отправить критическое уведомление: %s",
                    exc,
                )

    async def publish_user_notification(self, message: str) -> bool:
        """Доставить обычное пользовательское уведомление через общий HA script."""
        if not self.armed:
            self.log.info("DISARMED: подавлено уведомление: %s", message)
            return False
        try:
            await self.client.call_service(
                "script",
                "notify_critical",
                service_data={"message": message},
            )
        except Exception as exc:
            self.log.warning("Не удалось отправить уведомление: %s", exc)
            return False
        return True

    async def publish_status(self, state: str, attributes: dict[str, Any]) -> bool:
        try:
            await self.client.set_state(
                ENERGY_ATS_STATUS_ENTITY,
                state,
                attributes=attributes,
            )
        except Exception as exc:
            self.log.warning(
                "Не удалось опубликовать %s: %s",
                ENERGY_ATS_STATUS_ENTITY,
                exc,
            )
            return False
        return True

    def bool_state(self, entity_id: str) -> bool | None:
        state = self.client.get_state(entity_id)
        if state == "on":
            return True
        if state == "off":
            return False
        return None

    def presence_state(self, entity_id: str | None) -> bool | None:
        if entity_id is None:
            return None
        state = self.client.get_state(entity_id)
        if state in {"home", "on"}:
            return True
        if state in {"not_home", "off"}:
            return False
        return None

    def float_state(self, entity_id: str) -> float | None:
        state = self.client.get_state(entity_id)
        try:
            return float(state) if state is not None else None
        except (TypeError, ValueError):
            return None

    def text_state(self, entity_id: str) -> str | None:
        state = self.client.get_state(entity_id)
        if state in (None, "unknown", "unavailable"):
            return None
        return str(state)

    def state_revision(self, entity_id: str) -> Hashable | None:
        """Вернуть признак нового HA sample без привязки domain к HA internals."""

        getter = getattr(self.client, "get_state_revision", None)
        if callable(getter):
            revision = getter(entity_id)
            if revision is not None:
                return revision

        # Простые test/fake clients не имеют revision counter. Изменение самого
        # state всё же считается новым sample; одинаковое повторное чтение — нет.
        state = self.client.get_state(entity_id)
        return state if state not in (None, "unknown", "unavailable") else None

    def state_updated_at(self, entity_id: str) -> float | None:
        getter = getattr(self.client, "get_state_updated_at", None)
        return getter(entity_id) if callable(getter) else None

    @staticmethod
    def _generator_service(
        action: GeneratorAction,
    ) -> tuple[str, str, str]:
        prefix = "generator_a" if action.slot == GeneratorSlot.A else "generator_b"
        if action.kind == GeneratorActionKind.REMOTE_ON:
            return ENTITIES[f"{prefix}_remote"], "switch", "turn_on"
        if action.kind == GeneratorActionKind.REMOTE_OFF:
            return ENTITIES[f"{prefix}_remote"], "switch", "turn_off"
        if action.kind == GeneratorActionKind.CHOKE_TO_COLD_START:
            return ENTITIES[f"{prefix}_choke_cold_start"], "button", "press"
        return ENTITIES[f"{prefix}_choke_run"], "button", "press"

    def _assert_generator_action_safe(self, action: GeneratorAction) -> None:
        if (
            action.kind == GeneratorActionKind.REMOTE_ON
            and self.bool_state(ENTITIES["emergency_stop"]) is not False
        ):
            raise UnsafeHardwareCommand(
                f"REMOTE ON {action.slot.value} запрещён при активном/неизвестном Emergency Stop."
            )

        # Два RUNNING разрешены физической схемой. REMOTE OFF опасен только
        # если отключаемый двигатель всё ещё работает и дом может быть на
        # генераторной шине. Уже остановившемуся двигателю REMOTE можно снять,
        # даже когда второй генератор продолжает питать дом.
        if action.kind == GeneratorActionKind.REMOTE_OFF:
            running_entity = (
                ENTITIES["generator_a_running"]
                if action.slot == GeneratorSlot.A
                else ENTITIES["generator_b_running"]
            )
            target_running = self.bool_state(running_entity)
            house_generator = self.bool_state(ENTITIES["house_generator"])
            if target_running is not False and house_generator is not False:
                raise UnsafeHardwareCommand(
                    f"REMOTE OFF {action.slot.value} запрещён: отключаемый генератор "
                    "может ещё питать дом."
                )

    def _assert_transfer_action_safe(self, action: TransferAction) -> None:
        if action.kind == TransferActionKind.SELECT_GENERATOR:
            if (
                self.bool_state(ENTITIES["grid_power"]) is not False
                or self.bool_state(ENTITIES["house_grid"]) is not False
            ):
                raise UnsafeHardwareCommand(
                    "Ввод генератора запрещён до подтверждённого отключения Grid."
                )

        if action.kind == TransferActionKind.CONNECT_GRID:
            if (
                self.bool_state(ENTITIES["source_generator"]) is not False
                or self.bool_state(ENTITIES["house_generator"]) is not False
            ):
                raise UnsafeHardwareCommand(
                    "Подключение Grid запрещено до подтверждённой изоляции генераторной ветви."
                )

    @staticmethod
    def _transfer_service(kind: TransferActionKind) -> tuple[str, str]:
        if kind == TransferActionKind.CONNECT_GRID:
            return ENTITIES["grid_power"], "turn_on"
        if kind == TransferActionKind.DISCONNECT_GRID:
            return ENTITIES["grid_power"], "turn_off"
        if kind == TransferActionKind.SELECT_GENERATOR:
            return ENTITIES["source_generator"], "turn_on"
        return ENTITIES["source_generator"], "turn_off"

    async def _logbook(self, message: str, entity_id: str | None) -> None:
        service_data = {"name": "Energy ATS", "message": message}
        if entity_id is not None:
            service_data["entity_id"] = entity_id
        await self.client.call_service(
            "logbook",
            "log",
            service_data=service_data,
        )

    async def _publish_log_entries(
        self,
        entries: list[tuple[str, str]],
    ) -> None:
        for message, entity_id in entries:
            try:
                await self._logbook(message, entity_id)
            except Exception as exc:
                self.log.warning("Не удалось записать событие в Logbook: %s", exc)

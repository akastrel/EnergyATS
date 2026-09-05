"""Граница между чистыми контроллерами и Home Assistant.

Все entity_id и все HA service calls собраны здесь. Доменные автоматы получают
обычные dataclass-снимки и не зависят от протокола Home Assistant.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from domain import GeneratorSlot, SupervisorEvent
from generator_controller import (
    GeneratorAction,
    GeneratorActionKind,
    GeneratorObservation,
)
from ha_client import HomeAssistantClient
from power_transfer import (
    PowerTransferObservation,
    TransferAction,
    TransferActionKind,
)


ENTITIES = {
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
    "emergency_stop": "switch.generators_emergency_stop",
    "ambient_temperature_external": "sensor.garage_temperature",
    "grid_power": "switch.grid_power",
    "source_generator": "switch.use_generator_as_power_source",
    "automatic_transfer": "input_boolean.automatic_generator_transfer",
    "session_active": "input_boolean.generator_reserve_session_active",
    "session_mode": "input_select.generator_reserve_session_mode",
    "manual_start": "input_button.generator_reserve_start",
    "manual_stop": "input_button.generator_return_to_grid",
    "recovery_reset": "input_button.generator_ats_reset",
    "status": "input_text.generator_ats_status",
}


@dataclass(frozen=True)
class HardwareSnapshot:
    grid_ready: bool | None
    automatic_transfer_enabled: bool
    emergency_stop: bool | None
    generators: dict[GeneratorSlot, GeneratorObservation]
    power_transfer: PowerTransferObservation


class UnsafeHardwareCommand(RuntimeError):
    """Команда нарушает локальное физическое предусловие адаптера."""


class HomeAssistantAdapter:
    def __init__(
        self,
        client: HomeAssistantClient,
        *,
        armed: bool,
        logger: logging.Logger | None = None,
    ) -> None:
        self.client = client
        self.armed = armed
        self.log = logger or logging.getLogger(__name__)
        self._last_status: str | None = None
        self._last_session_active: bool | None = None
        self._last_session_mode: str | None = None

    def snapshot(self) -> HardwareSnapshot:
        grid_ready = self.bool_state(ENTITIES["grid_ready"])
        house_grid = self.bool_state(ENTITIES["house_grid"])
        house_generator = self.bool_state(ENTITIES["house_generator"])
        running_a = self.bool_state(ENTITIES["generator_a_running"])
        running_b = self.bool_state(ENTITIES["generator_b_running"])
        emergency_stop = self.bool_state(ENTITIES["emergency_stop"])
        ambient_temperature = self.float_state(
            ENTITIES["ambient_temperature_external"]
        )

        active_generator = None
        if running_a is True and running_b is not True:
            active_generator = GeneratorSlot.A
        elif running_b is True and running_a is not True:
            active_generator = GeneratorSlot.B

        generators = {
            GeneratorSlot.A: GeneratorObservation(
                running=running_a,
                remote_on=self.bool_state(ENTITIES["generator_a_remote"]),
                load_connected=self._generator_load(
                    GeneratorSlot.A,
                    house_generator,
                    active_generator,
                ),
                emergency_stop=emergency_stop,
                ambient_temperature_external=ambient_temperature,
            ),
            GeneratorSlot.B: GeneratorObservation(
                running=running_b,
                remote_on=self.bool_state(ENTITIES["generator_b_remote"]),
                load_connected=self._generator_load(
                    GeneratorSlot.B,
                    house_generator,
                    active_generator,
                ),
                emergency_stop=emergency_stop,
                ambient_temperature_external=ambient_temperature,
            ),
        }

        return HardwareSnapshot(
            grid_ready=grid_ready,
            automatic_transfer_enabled=(
                self.bool_state(ENTITIES["automatic_transfer"]) is True
            ),
            emergency_stop=emergency_stop,
            generators=generators,
            power_transfer=PowerTransferObservation(
                grid_ready=grid_ready,
                house_on_grid=house_grid,
                house_on_generator=house_generator,
                grid_connected=self.bool_state(ENTITIES["grid_power"]),
                generator_selected=self.bool_state(ENTITIES["source_generator"]),
                active_generator=active_generator,
                emergency_stop=emergency_stop,
            ),
        )

    def missing_required_entities(
        self, *, include_control_entities: bool = True
    ) -> list[str]:
        state_required = [
            ENTITIES["grid_ready"],
            ENTITIES["house_grid"],
            ENTITIES["house_generator"],
            ENTITIES["generator_a_running"],
            ENTITIES["generator_b_running"],
            ENTITIES["generator_a_remote"],
            ENTITIES["generator_b_remote"],
            ENTITIES["emergency_stop"],
            ENTITIES["grid_power"],
            ENTITIES["source_generator"],
            ENTITIES["automatic_transfer"],
        ]
        existence_only = [ENTITIES["manual_start"], ENTITIES["manual_stop"]]
        if include_control_entities:
            existence_only.extend([
                ENTITIES["generator_a_choke_cold_start"],
                ENTITIES["generator_a_choke_run"],
                ENTITIES["generator_b_choke_cold_start"],
                ENTITIES["generator_b_choke_run"],
            ])

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
        """Сначала выполнить все силовые команды, затем вспомогательный Logbook.

        Изоляция генераторной шины имеет приоритет перед командами двигателя.
        Ошибка необязательного Logbook не может оборвать аппаратную
        последовательность посередине.
        """
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

    async def publish_events(self, events: tuple[SupervisorEvent, ...]) -> None:
        for event in events:
            if not self.armed:
                self.log.info("DISARMED: %s", event.message)
                continue
            # Уведомление выполняется после всех силовых команд текущего tick.
            try:
                if event.level == "warning":
                    await self.client.call_service(
                        "script",
                        "notify_warning",
                        service_data={"message": event.message},
                    )
                elif event.level == "critical":
                    await self.client.call_service(
                        "script",
                        "notify_critical",
                        service_data={"message": event.message},
                    )
                await self._logbook(
                    event.message,
                    event.entity_id or ENTITIES["automatic_transfer"],
                )
            except Exception as exc:
                self.log.warning("Не удалось опубликовать событие в HA: %s", exc)

    async def publish_status(self, status: str) -> None:
        if status == self._last_status or not self.client.has_entity(ENTITIES["status"]):
            return
        await self.client.call_service(
            "input_text",
            "set_value",
            service_data={"entity_id": ENTITIES["status"], "value": status},
        )
        self._last_status = status

    async def publish_session(self, active: bool, mode: str) -> None:
        """Синхронизировать прежние HA helper-ы для dashboard и диагностики."""
        if (
            active != self._last_session_active
            and self.client.has_entity(ENTITIES["session_active"])
        ):
            await self.client.call_service(
                "input_boolean",
                "turn_on" if active else "turn_off",
                service_data={"entity_id": ENTITIES["session_active"]},
            )
            self._last_session_active = active
        if (
            mode != self._last_session_mode
            and self.client.has_entity(ENTITIES["session_mode"])
        ):
            await self.client.call_service(
                "input_select",
                "select_option",
                service_data={"entity_id": ENTITIES["session_mode"], "option": mode},
            )
            self._last_session_mode = mode

    def bool_state(self, entity_id: str) -> bool | None:
        state = self.client.get_state(entity_id)
        if state == "on":
            return True
        if state == "off":
            return False
        return None

    def float_state(self, entity_id: str) -> float | None:
        state = self.client.get_state(entity_id)
        try:
            return float(state) if state is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _generator_load(
        slot: GeneratorSlot,
        house_generator: bool | None,
        active_generator: GeneratorSlot | None,
    ) -> bool | None:
        if house_generator is False:
            return False
        if house_generator is None:
            return None
        if active_generator is None:
            return None
        return active_generator == slot

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
        if action.kind == GeneratorActionKind.REMOTE_ON:
            other = (
                GeneratorSlot.B
                if action.slot == GeneratorSlot.A
                else GeneratorSlot.A
            )
            prefix = "generator_a" if other == GeneratorSlot.A else "generator_b"
            if (
                self.bool_state(ENTITIES[f"{prefix}_running"]) is not False
                or self.bool_state(ENTITIES[f"{prefix}_remote"]) is not False
            ):
                raise UnsafeHardwareCommand(
                    f"REMOTE {action.slot.value} запрещён: состояние второго "
                    "генератора не подтверждено как OFF."
                )

        if (
            action.kind == GeneratorActionKind.REMOTE_OFF
            and self.bool_state(ENTITIES["house_generator"]) is True
        ):
            running_key = (
                "generator_a_running"
                if action.slot == GeneratorSlot.A
                else "generator_b_running"
            )
            if self.bool_state(ENTITIES[running_key]) is True:
                raise UnsafeHardwareCommand(
                    f"REMOTE OFF {action.slot.value} запрещён: дом ещё "
                    "подтверждённо питается от работающего генератора."
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
                    "Подключение Grid запрещено до подтверждённой изоляции "
                    "генераторной шины."
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

    async def _logbook(self, message: str, entity_id: str) -> None:
        await self.client.call_service(
            "logbook",
            "log",
            service_data={
                "name": "Energy Supervisor",
                "message": message,
                "entity_id": entity_id,
            },
        )

    async def _publish_log_entries(
        self, entries: list[tuple[str, str]]
    ) -> None:
        """Логирование не должно прерывать последовательность команд железу."""
        for message, entity_id in entries:
            try:
                await self._logbook(message, entity_id)
            except Exception as exc:
                self.log.warning("Не удалось записать событие в Logbook: %s", exc)

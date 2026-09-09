"""Композиция EnergyATS и связь с Home Assistant.

Решения о политике находятся в EnergySupervisor, физическая коммутация — в
PowerTransferController, жизненный цикл двигателя — в GeneratorController.
main.py только собирает эти уровни, обновляет наблюдаемую модель общей
генераторной шины и исполняет сформированные команды.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import signal
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from domain import GeneratorSlot, PowerPath, PowerSource, SupervisorEvent
from energy_supervisor import (
    EnergySupervisor,
    SupervisorConfig,
    SupervisorObservation,
    SupervisorPhase,
)
from generator_bus import GeneratorBusOwner, GeneratorBusTracker
from generator_controller import (
    GeneratorAction,
    GeneratorController,
    GeneratorPhase,
    default_generator_profiles,
)
from ha_adapter import HardwareSnapshot, HomeAssistantAdapter
from ha_client import HomeAssistantClient, HomeAssistantConnectionError
from power_transfer import PowerTransferController, TransferAction, TransferPhase
from state_store import StateStore


APP_VERSION = "0.4.0"
JOURNAL_SCHEMA_VERSION = 2
GENERATOR_TEST_MODE_ENTITY = "input_boolean.generator_test_mode"


DEFAULT_OPTIONS: dict[str, Any] = {
    "armed": False,
    "tick_seconds": 1.0,
    "log_level": "info",
    "grid_failure_delay": 5,
    "grid_restore_stable_time": 60,
    "generator_a_enabled": True,
    "generator_b_enabled": True,
    "transfer_confirmation_timeout": 60,
    "state_file": "/data/energy-supervisor-state.json",
}


_GENERATOR_PHASE_TEXT = {
    GeneratorPhase.WAITING_FOR_DATA: "ожидание данных",
    GeneratorPhase.IDLE: "остановлен",
    GeneratorPhase.PREPARING: "подготовка к запуску",
    GeneratorPhase.WAITING_FOR_RUNNING: "запуск",
    GeneratorPhase.HOLDING_COLD_START_CHOKE: "запущен, заслонка",
    GeneratorPhase.WARMING_UP: "прогрев",
    GeneratorPhase.READY_FOR_LOAD: "готов",
    GeneratorPhase.WAITING_FOR_LOAD_RELEASE: "ожидание снятия нагрузки",
    GeneratorPhase.COOLING_DOWN: "охлаждение",
    GeneratorPhase.WAITING_FOR_STOP: "остановка",
    GeneratorPhase.EXTERNAL_RUNNING: "внешний запуск",
    GeneratorPhase.FAULT: "АВАРИЯ",
    GeneratorPhase.RECOVERY_REQUIRED: "требуется восстановление",
}


class EnergySupervisorApp:
    """Один процесс с независимыми ES/TPC/GC и HA adapter."""

    def __init__(self, options: dict[str, Any], token: str) -> None:
        self.options = {**DEFAULT_OPTIONS, **options}
        self.armed = _boolean_option(self.options, "armed")
        self.tick_seconds = max(0.2, float(self.options["tick_seconds"]))

        self.log = logging.getLogger("energy_supervisor")
        self.client = HomeAssistantClient(token, logger=self.log)
        self.adapter = HomeAssistantAdapter(
            self.client,
            armed=self.armed,
            logger=self.log,
        )

        self.profiles = default_generator_profiles()
        self.generator_controllers = {
            slot: GeneratorController(profile)
            for slot, profile in self.profiles.items()
        }
        self.power_transfer = PowerTransferController(
            confirmation_timeout=float(
                self.options["transfer_confirmation_timeout"]
            )
        )

        self.state_store = StateStore(str(self.options["state_file"]))
        self._restore_error: str | None = None
        self._restored_journal = self._load_journal_for_restore()
        self.generator_bus = self._restore_generator_bus()
        self.supervisor = self._restore_supervisor()

        self._saved_state_signature: str | None = None
        self._pending_action_records: list[dict[str, str]] = []

        self.stop_event = asyncio.Event()
        self.commands_ready = False
        self._last_runtime_signature: tuple[Any, ...] | None = None
        self._last_generator_config_signature: tuple[Any, ...] | None = None
        self._last_status_payload: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # Process / commands.
    # ------------------------------------------------------------------

    def request_stop(self) -> None:
        self.stop_event.set()

    async def read_stdin_commands(self) -> None:
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        transport = None
        try:
            loop = asyncio.get_running_loop()
            transport, _ = await loop.connect_read_pipe(lambda: protocol, sys.stdin)
            while not self.stop_event.is_set():
                line = await reader.readline()
                if not line:
                    self.log.warning("STDIN закрыт; ручные команды HA недоступны.")
                    return
                self.handle_stdin_line(line.decode("utf-8"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.log.error("Обработчик STDIN остановлен: %s", exc)
        finally:
            if transport is not None:
                transport.close()

    def handle_stdin_line(self, line: str) -> None:
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            self.log.warning("Отклонена некорректная JSON-команда: %s", exc)
            return

        if not isinstance(message, dict) or not isinstance(message.get("command"), str):
            self.log.warning("Отклонена команда без строкового поля command: %r", message)
            return

        command = message["command"]
        handlers = {
            "start_generator": self.supervisor.request_manual_start,
            "stop_generator": self.supervisor.request_manual_stop,
            "reset": self.supervisor.request_recovery_reset,
        }
        handler = handlers.get(command)
        if handler is None:
            self.log.warning("Неизвестная команда Energy ATS: %s", command)
            return
        if not self.armed:
            self.log.info("DISARMED: команда %s проигнорирована.", command)
            return
        if not self.commands_ready:
            self.log.warning(
                "Команда %s отклонена: App ещё не получил обязательные состояния.",
                command,
            )
            return
        handler()
        self.log.info("Принята команда Energy ATS: %s", command)

    async def run(self) -> None:
        self.log.info("Energy ATS %s запущен.", APP_VERSION)
        self.log.info(
            "Режим: %s.",
            "ARMED — реальные команды разрешены"
            if self.armed
            else "DISARMED — только наблюдение",
        )

        reconnect_delay = 5.0
        while not self.stop_event.is_set():
            try:
                await self.client.connect()
                self._last_status_payload = None
                await self._wait_until_required_entities_ready()
                if self.stop_event.is_set():
                    break
                hardware = self.adapter.snapshot()
                self._sync_generator_configuration(hardware)
                self.commands_ready = True
                await self._connected_loop()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self.stop_event.is_set():
                    await self._record_interrupted_connection(exc)
                    self.log.error(
                        "Рабочий цикл прерван: %s. Переподключение через %.0f с.",
                        exc,
                        reconnect_delay,
                    )
            finally:
                self.commands_ready = False
                await self.client.close()

            await self._stop_requested_within(reconnect_delay)

        self.log.info("Energy ATS остановлен.")

    async def _connected_loop(self) -> None:
        while not self.stop_event.is_set():
            if not self.client.connected.is_set():
                raise HomeAssistantConnectionError("WebSocket HA потерян")
            await self._tick(time.time())
            await self._stop_requested_within(self.tick_seconds)

    # ------------------------------------------------------------------
    # One tick.
    # ------------------------------------------------------------------

    async def _tick(self, now: float) -> None:
        hardware = self.adapter.snapshot()
        self._sync_generator_configuration(hardware)

        test_mode = self.adapter.bool_state(GENERATOR_TEST_MODE_ENTITY)
        hardware = self._apply_generator_bus_model(hardware, test_mode=test_mode)
        self._refresh_component_views(now, hardware)

        if self.supervisor.consume_recovery_reset_request():
            self._start_recovery_reset(now, hardware)

        if self.supervisor.recovery_reset_in_progress:
            await self._tick_recovery_reset(now, hardware)
            return

        observation = self._supervisor_observation(hardware, test_mode=test_mode)
        decision = self.supervisor.step(now, observation)
        actions_allowed = self.armed and decision.actions_allowed

        generator_actions: list[GeneratorAction] = []
        outage_stop_errors: list[str] = []
        for slot, controller in self.generator_controllers.items():
            if slot in decision.stop_outage_generators:
                # Явное ограниченное исключение REQ-OUTRUN-03: после стабильной
                # Grid разрешено штатно остановить и внешний outage-related run.
                actions, error = controller.step_recovery_shutdown(
                    now,
                    hardware.generators[slot],
                    owned_by_interrupted_session=True,
                )
                if error is not None:
                    outage_stop_errors.append(error)
                generator_actions.extend(actions)
                continue

            generator_actions.extend(
                controller.step(
                    now,
                    hardware.generators[slot],
                    desired_running=decision.desired_generators[slot],
                    actions_allowed=actions_allowed,
                    stable_managed_session=(
                        decision.stable_managed_generator == slot
                    ),
                )
            )

        if outage_stop_errors:
            self.supervisor.require_recovery("; ".join(outage_stop_errors))

        generator_statuses = {
            slot: controller.status(hardware.generators[slot])
            for slot, controller in self.generator_controllers.items()
        }
        desired_generator_ready = self._desired_generator_ready(
            decision.desired_source,
            hardware,
            generator_statuses,
        )
        transfer_actions = self.power_transfer.step(
            now,
            hardware.power_transfer,
            decision.desired_source,
            desired_generator_ready=desired_generator_ready,
            actions_allowed=actions_allowed,
        )

        await self._execute_controller_actions(transfer_actions, generator_actions)

        updated_observation = self._supervisor_observation(
            hardware,
            test_mode=test_mode,
        )
        self._log_events(decision.events)
        await self.adapter.publish_events(decision.events)
        self._log_runtime_if_changed(updated_observation)
        await self._publish_status(now, updated_observation)

    def _apply_generator_bus_model(
        self,
        hardware: HardwareSnapshot,
        *,
        test_mode: bool | None,
    ) -> HardwareSnapshot:
        session = self.supervisor.session
        managed_slot = session.generator if session is not None else None
        managed_outage = bool(session is not None and session.grid_was_unavailable)
        bus = self.generator_bus.update(
            {
                slot: hardware.generators[slot].running
                for slot in (GeneratorSlot.A, GeneratorSlot.B)
            },
            grid_ready=hardware.grid_ready,
            test_mode=test_mode,
            managed_slot=managed_slot,
            managed_outage=managed_outage,
        )

        owner = bus.owner_slot
        house_on_generator = hardware.power_transfer.house_on_generator
        generators = {}
        for slot, observation in hardware.generators.items():
            if house_on_generator is False:
                load_connected: bool | None = False
            elif house_on_generator is True and owner is not None:
                load_connected = owner == slot
            else:
                load_connected = None
            generators[slot] = replace(
                observation,
                load_connected=load_connected,
            )

        power_transfer = replace(
            hardware.power_transfer,
            active_generator=owner,
        )
        return replace(
            hardware,
            generators=generators,
            power_transfer=power_transfer,
        )

    def _desired_generator_ready(
        self,
        desired_source: PowerSource | None,
        hardware: HardwareSnapshot,
        statuses: dict[GeneratorSlot, Any],
    ) -> bool:
        if desired_source is None or not desired_source.is_generator:
            return False
        slot = desired_source.generator
        if slot is None:
            slot = self.generator_bus.status().owner_slot
        if slot is None:
            return False
        if statuses[slot].ready_for_load:
            return True
        # Внешний bus owner не становится managed, но сам факт owner + RUNNING
        # является достаточным физическим условием для уже существующей
        # генераторной шины.
        return (
            self.generator_bus.status().owner_slot == slot
            and hardware.generators[slot].running is True
        )

    # ------------------------------------------------------------------
    # Generator metadata/config.
    # ------------------------------------------------------------------

    def _sync_generator_configuration(self, hardware: HardwareSnapshot) -> None:
        metadata_a = hardware.generator_metadata[GeneratorSlot.A]
        metadata_b = hardware.generator_metadata[GeneratorSlot.B]
        if metadata_a is None or metadata_b is None:
            raise ValueError("Не удалось прочитать имя или модель генераторов из HA.")
        if not metadata_a.name or not metadata_b.name:
            raise ValueError("Имена генераторов не могут быть пустыми.")
        if metadata_a.name == metadata_b.name:
            raise ValueError("generator_a_name и generator_b_name должны различаться.")
        if hardware.primary_generator is None:
            raise ValueError(
                "select.primary_generator должен совпадать с именем Generator A или B."
            )

        for slot, metadata in (
            (GeneratorSlot.A, metadata_a),
            (GeneratorSlot.B, metadata_b),
        ):
            profile = replace(
                self.generator_controllers[slot].profile,
                display_name=metadata.name,
                model=metadata.model,
            )
            self.generator_controllers[slot].profile = profile
            self.profiles[slot] = profile

        config = self._supervisor_config(hardware.primary_generator)
        if not config.generator_enabled(hardware.primary_generator):
            name = self.profiles[hardware.primary_generator].display_name
            raise ValueError(f"Основной генератор {name} запрещён политикой EnergyATS.")
        self.supervisor.config = config

        signature = (
            metadata_a.name,
            metadata_a.model,
            metadata_b.name,
            metadata_b.model,
            hardware.primary_generator,
        )
        if signature != self._last_generator_config_signature:
            self._last_generator_config_signature = signature
            for slot in (GeneratorSlot.A, GeneratorSlot.B):
                profile = self.profiles[slot]
                marker = "PRIMARY" if slot == hardware.primary_generator else "SECONDARY"
                self.log.info(
                    "Generator %s: %s; модель: %s; %s; choke: %s.",
                    slot.value,
                    profile.display_name,
                    profile.model,
                    marker,
                    profile.choke_strategy.value,
                )

    # ------------------------------------------------------------------
    # Component observation refresh.
    # ------------------------------------------------------------------

    def _refresh_component_views(
        self,
        now: float,
        hardware: HardwareSnapshot,
    ) -> None:
        for slot, controller in self.generator_controllers.items():
            controller.step(
                now,
                hardware.generators[slot],
                desired_running=self.supervisor.desired_generators[slot],
                actions_allowed=False,
                stable_managed_session=self.supervisor.manages_stable_generator(slot),
            )

        statuses = {
            slot: controller.status(hardware.generators[slot])
            for slot, controller in self.generator_controllers.items()
        }
        ready = self._desired_generator_ready(
            self.supervisor.desired_source,
            hardware,
            statuses,
        )
        self.power_transfer.step(
            now,
            hardware.power_transfer,
            self.supervisor.desired_source,
            desired_generator_ready=ready,
            actions_allowed=False,
        )

    def _supervisor_observation(
        self,
        hardware: HardwareSnapshot,
        *,
        test_mode: bool | None = None,
    ) -> SupervisorObservation:
        return SupervisorObservation(
            grid_ready=hardware.grid_ready,
            automatic_transfer_enabled=(
                hardware.automatic_transfer_enabled and self.armed
            ),
            emergency_stop=hardware.emergency_stop,
            power=self.power_transfer.status(),
            generators={
                slot: controller.status(hardware.generators[slot])
                for slot, controller in self.generator_controllers.items()
            },
            power_inputs_known=hardware.power_transfer.required_states_known,
            bus=self.generator_bus.status(),
            test_mode=test_mode is True,
        )

    # ------------------------------------------------------------------
    # Recovery.
    # ------------------------------------------------------------------

    def _start_recovery_reset(
        self,
        now: float,
        hardware: HardwareSnapshot,
    ) -> None:
        if self.supervisor.recovery_reset_in_progress:
            self.supervisor.begin_recovery_reset(now)
            return

        needs_reset = (
            self.supervisor.phase == SupervisorPhase.RECOVERY_REQUIRED
            or self.power_transfer.status().recovery_required
            or any(
                controller.phase
                in {GeneratorPhase.FAULT, GeneratorPhase.RECOVERY_REQUIRED}
                for controller in self.generator_controllers.values()
            )
        )
        if not needs_reset:
            self.supervisor.report_recovery_reset_not_needed()
            return

        blocker = self._recovery_blocker(hardware)
        if blocker is not None:
            self.supervisor.reject_recovery_reset(f"Сброс отклонён: {blocker}")
            return

        self.supervisor.begin_recovery_reset(now)
        self.power_transfer.begin_recovery_to_grid_path()

    async def _tick_recovery_reset(
        self,
        now: float,
        hardware: HardwareSnapshot,
    ) -> None:
        blocker = self._recovery_blocker(hardware)
        if blocker is not None:
            self.supervisor.fail_recovery_reset(now, blocker)
            await self._finish_recovery_tick(now, hardware)
            return

        transfer_actions, transfer_error = self.power_transfer.step_recovery_to_grid_path(
            now,
            hardware.power_transfer,
        )
        if transfer_error is not None:
            self.supervisor.fail_recovery_reset(now, transfer_error)
            await self._finish_recovery_tick(now, hardware)
            return

        if transfer_actions:
            self.supervisor.advance_recovery_reset(
                now,
                "restore_grid_path",
                confirmed="recovery_action_requested",
            )
            await self._execute_controller_actions(transfer_actions, [])
            await self._finish_recovery_tick(now, hardware)
            return

        if not self._grid_path_confirmed(hardware):
            self._save_state()
            await self._finish_recovery_tick(now, hardware)
            return

        self.supervisor.advance_recovery_reset(
            now,
            "stop_managed_generator",
            confirmed="grid_path_confirmed",
        )

        managed_slot = (
            self.supervisor.session.generator
            if self.supervisor.session is not None
            else None
        )
        generator_actions: list[GeneratorAction] = []
        for slot, controller in self.generator_controllers.items():
            actions, error = controller.step_recovery_shutdown(
                now,
                hardware.generators[slot],
                owned_by_interrupted_session=slot == managed_slot,
            )
            if error is not None:
                self.supervisor.fail_recovery_reset(now, error)
                await self._finish_recovery_tick(now, hardware)
                return
            generator_actions.extend(actions)

        if generator_actions:
            await self._execute_controller_actions([], generator_actions)
            await self._finish_recovery_tick(now, hardware)
            return

        generators_stopped = all(
            hardware.generators[slot].running is False
            and hardware.generators[slot].remote_on is False
            and controller.phase == GeneratorPhase.IDLE
            for slot, controller in self.generator_controllers.items()
        )
        if not generators_stopped:
            self._save_state()
            await self._finish_recovery_tick(now, hardware)
            return

        if not self.power_transfer.request_recovery_reset(hardware.power_transfer):
            self.supervisor.fail_recovery_reset(
                now,
                "Grid path не получил окончательного подтверждения.",
            )
            await self._finish_recovery_tick(now, hardware)
            return

        self.supervisor.complete_recovery_reset(now)
        self._save_state(force=True)
        await self._finish_recovery_tick(now, hardware)

    def _recovery_blocker(self, hardware: HardwareSnapshot) -> str | None:
        if hardware.emergency_stop is not False:
            return "сначала снимите Generators Emergency Stop."

        power_blocker = self.power_transfer.recovery_blocker(hardware.power_transfer)
        if power_blocker is not None:
            return power_blocker

        for observation in hardware.generators.values():
            if not observation.required_states_known:
                return "неизвестны обязательные состояния генераторов."

        active_slots = {
            slot
            for slot, observation in hardware.generators.items()
            if observation.running is True or observation.remote_on is True
        }
        managed_slot = (
            self.supervisor.session.generator
            if self.supervisor.session is not None
            else None
        )
        external_slots = active_slots - ({managed_slot} if managed_slot else set())
        if external_slots:
            names = ", ".join(
                self.profiles[slot].display_name
                for slot in sorted(external_slots, key=lambda item: item.value)
            )
            return f"обнаружен внешний запуск ({names}); recovery им не управляет."
        return None

    def _grid_path_confirmed(self, hardware: HardwareSnapshot) -> bool:
        status = self.power_transfer.status()
        return (
            status.actual_path == PowerPath.GRID
            and not status.transition_in_progress
            and hardware.power_transfer.generator_selected is False
            and hardware.power_transfer.house_on_generator is False
        )

    # ------------------------------------------------------------------
    # Actions and journal.
    # ------------------------------------------------------------------

    async def _execute_controller_actions(
        self,
        transfer_actions: list[TransferAction],
        generator_actions: list[GeneratorAction],
    ) -> None:
        self._pending_action_records = self._describe_actions(
            transfer_actions,
            generator_actions,
        )
        self._save_state(force=bool(self._pending_action_records))

        if not self._pending_action_records:
            return
        await self.adapter.execute_actions(transfer_actions, generator_actions)
        self._pending_action_records = []
        self._save_state(force=True)

    async def _finish_recovery_tick(
        self,
        now: float,
        hardware: HardwareSnapshot,
    ) -> None:
        observation = self._supervisor_observation(hardware)
        decision = self.supervisor.step(now, observation)
        self._log_events(decision.events)
        await self.adapter.publish_events(decision.events)
        self._log_runtime_if_changed(observation)
        await self._publish_status(now, observation)

    async def _wait_until_required_entities_ready(self) -> None:
        last_log_at = 0.0
        while not self.stop_event.is_set():
            missing = self.adapter.missing_required_entities(
                include_control_entities=self.armed
            )
            if not missing:
                return
            now = time.monotonic()
            if now - last_log_at >= 30.0:
                self.log.warning(
                    "Ожидаем обязательные сущности Home Assistant: %s",
                    ", ".join(missing),
                )
                last_log_at = now
            if await self._stop_requested_within(1.0):
                return
            if not self.client.connected.is_set():
                raise HomeAssistantConnectionError("WebSocket HA потерян")

    async def _record_interrupted_connection(self, exc: Exception) -> None:
        now = time.time()
        self.supervisor.mark_connection_lost(now)
        if self.supervisor.phase == SupervisorPhase.RECOVERY_REQUIRED:
            self.power_transfer.mark_interrupted(
                now,
                "Потеряна связь с Home Assistant.",
            )
        try:
            self._save_state(force=True)
        except Exception as save_exc:
            self.log.critical(
                "Не удалось сохранить отметку о прерванной транзакции: %s",
                save_exc,
            )
        if self.supervisor.phase == SupervisorPhase.RECOVERY_REQUIRED:
            self.log.critical(
                "Связь потеряна во время физической транзакции; "
                "автоматическое продолжение заблокировано: %s",
                exc,
            )

    def _load_journal_for_restore(self) -> dict[str, Any] | None:
        try:
            saved = self.state_store.load()
            if saved is None:
                return None
            if saved.get("journal_schema_version") != JOURNAL_SCHEMA_VERSION:
                raise ValueError(
                    "Неподдерживаемая версия журнала. Миграция v0.3 -> v0.4 "
                    "намеренно не выполняется."
                )
            return saved
        except Exception as exc:
            self._restore_error = str(exc)
            return None

    def _restore_generator_bus(self) -> GeneratorBusTracker:
        if self._restored_journal is None:
            return GeneratorBusTracker()
        payload = self._restored_journal.get("generator_bus")
        if not isinstance(payload, dict):
            self._restore_error = "В журнале отсутствует generator_bus"
            return GeneratorBusTracker()
        try:
            return GeneratorBusTracker.from_dict(payload)
        except Exception as exc:
            self._restore_error = f"generator_bus: {exc}"
            return GeneratorBusTracker()

    def _restore_supervisor(self) -> EnergySupervisor:
        config = self._supervisor_config(GeneratorSlot.A)
        if self._restore_error is not None:
            supervisor = EnergySupervisor(config)
            supervisor.require_recovery(
                f"Не удалось прочитать сохранённый журнал: {self._restore_error}"
            )
            return supervisor
        if self._restored_journal is None:
            return EnergySupervisor(config)
        try:
            payload = self._restored_journal.get("supervisor")
            if not isinstance(payload, dict):
                raise ValueError("В журнале отсутствует supervisor")
            supervisor = EnergySupervisor.from_dict(payload, config)
            if self._restored_journal.get("pending_actions"):
                supervisor.require_recovery(
                    "После restart обнаружены команды без подтверждения исполнения."
                )
            return supervisor
        except Exception as exc:
            supervisor = EnergySupervisor(config)
            supervisor.require_recovery(
                f"Не удалось прочитать состояние Supervisor: {exc}"
            )
            return supervisor

    def _supervisor_config(self, primary_generator: GeneratorSlot) -> SupervisorConfig:
        return SupervisorConfig(
            grid_failure_delay=float(self.options["grid_failure_delay"]),
            grid_restore_stable_time=float(self.options["grid_restore_stable_time"]),
            primary_generator=primary_generator,
            generator_a_enabled=_boolean_option(self.options, "generator_a_enabled"),
            generator_b_enabled=_boolean_option(self.options, "generator_b_enabled"),
        )

    def _save_state(self, *, force: bool = False) -> None:
        payload = {
            "journal_schema_version": JOURNAL_SCHEMA_VERSION,
            "app_version": APP_VERSION,
            "supervisor": self.supervisor.to_dict(),
            "generator_bus": self.generator_bus.to_dict(),
            "pending_actions": list(self._pending_action_records),
            "runtime_snapshot": {
                "generator_a_phase": self.generator_controllers[GeneratorSlot.A].phase.value,
                "generator_b_phase": self.generator_controllers[GeneratorSlot.B].phase.value,
                "power_transfer_phase": self.power_transfer.phase.value,
                "bus_owner": self.generator_bus.status().owner.value,
            },
        }
        signature = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if not force and signature == self._saved_state_signature:
            return
        self.state_store.save(payload)
        self._saved_state_signature = signature

    @staticmethod
    def _describe_actions(
        transfer_actions: list[TransferAction],
        generator_actions: list[GeneratorAction],
    ) -> list[dict[str, str]]:
        described = [
            {"controller": "power_transfer", "action": action.kind.value}
            for action in transfer_actions
        ]
        described.extend(
            {
                "controller": "generator_controller",
                "generator": action.slot.value,
                "action": action.kind.value,
            }
            for action in generator_actions
        )
        return described

    # ------------------------------------------------------------------
    # Status sensor and human log.
    # ------------------------------------------------------------------

    async def _publish_status(
        self,
        now: float,
        observation: SupervisorObservation,
    ) -> None:
        payload = self._status_payload(now, observation)
        if payload == self._last_status_payload:
            return
        published = await self.adapter.publish_status(
            payload["state"],
            payload["attributes"],
        )
        if published:
            self._last_status_payload = payload

    def _status_payload(
        self,
        now: float,
        observation: SupervisorObservation,
    ) -> dict[str, Any]:
        session = self.supervisor.session
        bus = self.generator_bus.status()
        slot = bus.owner_slot if observation.power.actual_source.is_generator else None
        if slot is None and session is not None:
            slot = session.generator

        generator_name = self.profiles[slot].display_name if slot is not None else None
        generator_model = self.profiles[slot].model if slot is not None else None
        primary_slot = self.supervisor.config.primary_generator
        primary_name = self.profiles[primary_slot].display_name
        state = (
            self.supervisor.status_text(observation)
            if self.armed
            else "DISARMED — только наблюдение"
        )

        owner_name = (
            self.profiles[bus.owner_slot].display_name
            if bus.owner_slot is not None
            else bus.owner.value
        )
        return {
            "state": state,
            "attributes": {
                "friendly_name": "Energy ATS Status",
                "icon": "mdi:transfer-switch",
                "source": observation.power.actual_source.value,
                "phase": self.supervisor.phase.value,
                "generator": generator_name,
                "generator_model": generator_model,
                "generator_slot": slot.value if slot is not None else None,
                "bus_owner": owner_name,
                "bus_owner_slot": (
                    bus.owner_slot.value if bus.owner_slot is not None else None
                ),
                "generator_a_run_context": bus.run_contexts[GeneratorSlot.A].value,
                "generator_b_run_context": bus.run_contexts[GeneratorSlot.B].value,
                "primary_generator": primary_name,
                "primary_generator_slot": primary_slot.value,
                "remaining_seconds": self._remaining_seconds(now, observation),
                "session_reason": session.reason.value if session is not None else None,
                "fallback_used": session.fallback_used if session is not None else False,
                "armed": self.armed,
                "schema_version": 3,
            },
        }

    def _remaining_seconds(
        self,
        now: float,
        observation: SupervisorObservation,
    ) -> int | None:
        if observation.power.transition_in_progress and self.power_transfer.deadline is not None:
            return _seconds_left(self.power_transfer.deadline - now)

        session = self.supervisor.session
        if session is not None:
            controller = self.generator_controllers[session.generator]
            if controller.deadline is not None:
                return _seconds_left(controller.deadline - now)

        if (
            self.supervisor.phase == SupervisorPhase.GRID_FAILURE_DELAY
            and self.supervisor.grid_failed_since is not None
        ):
            elapsed = now - self.supervisor.grid_failed_since
            return _seconds_left(self.supervisor.config.grid_failure_delay - elapsed)

        if (
            session is not None
            and session.grid_was_unavailable
            and observation.grid_ready is True
            and self.supervisor.grid_ready_since is not None
            and self.supervisor.phase
            in {SupervisorPhase.ON_GENERATOR, SupervisorPhase.ON_EXTERNAL_GENERATOR}
        ):
            elapsed = now - self.supervisor.grid_ready_since
            return _seconds_left(
                self.supervisor.config.grid_restore_stable_time - elapsed
            )
        return None

    def _format_power(self, observation: SupervisorObservation) -> str:
        source = observation.power.actual_source
        if source == PowerSource.GRID:
            return "Grid"
        if source == PowerSource.UPS_ONLY:
            return "UPS only"
        if source == PowerSource.NO_POWER:
            return "NO POWER"
        if source.is_generator:
            slot = self.generator_bus.status().owner_slot
            if slot is None:
                return "Generator Unknown"
            return f"Generator {self.profiles[slot].display_name}"
        return "Unknown"

    def _format_transfer(self, observation: SupervisorObservation) -> str | None:
        phase = observation.power.phase
        if phase == TransferPhase.DISCONNECTING_GRID:
            return "disconnecting Grid"
        if phase == TransferPhase.CONNECTING_GRID:
            return "connecting Grid"
        if phase == TransferPhase.RECOVERY_REQUIRED:
            return "recovery required"
        if phase not in {
            TransferPhase.SELECTING_GENERATOR,
            TransferPhase.DISCONNECTING_GENERATOR,
        }:
            return None

        slot = observation.power.target_source.generator if observation.power.target_source else None
        if slot is None:
            slot = self.generator_bus.status().owner_slot
        if slot is None and self.supervisor.session is not None:
            slot = self.supervisor.session.generator
        name = self.profiles[slot].display_name if slot is not None else "generator"
        action = "connecting" if phase == TransferPhase.SELECTING_GENERATOR else "disconnecting"
        return f"{action} {name}"

    def _format_generator_state(
        self,
        slot: GeneratorSlot,
        observation: SupervisorObservation,
    ) -> str:
        if (
            observation.power.actual_path == PowerPath.GENERATOR
            and self.generator_bus.status().owner_slot == slot
        ):
            return "под нагрузкой"
        return _GENERATOR_PHASE_TEXT[observation.generators[slot].phase]

    def _format_bus_owner(self) -> str:
        owner = self.generator_bus.status().owner
        if owner.slot is not None:
            return self.profiles[owner.slot].display_name
        if owner == GeneratorBusOwner.NONE:
            return "none"
        return "unknown"

    def _log_runtime_if_changed(self, observation: SupervisorObservation) -> None:
        status = (
            self.supervisor.status_text(observation)
            if self.armed
            else "DISARMED — только наблюдение"
        )
        grid = (
            "ON" if observation.grid_ready is True
            else "OFF" if observation.grid_ready is False
            else "UNKNOWN"
        )
        avr = "ON" if observation.automatic_transfer_enabled else "OFF"
        power = self._format_power(observation)
        transfer = self._format_transfer(observation)
        generator_a = self._format_generator_state(GeneratorSlot.A, observation)
        generator_b = self._format_generator_state(GeneratorSlot.B, observation)
        primary = self.profiles[self.supervisor.config.primary_generator].display_name
        bus_owner = self._format_bus_owner()

        signature = (
            status, grid, avr, power, transfer,
            generator_a, generator_b, primary, bus_owner,
        )
        if signature == self._last_runtime_signature:
            return
        self._last_runtime_signature = signature

        parts = [
            f"Состояние: {status}",
            f"Grid={grid}",
            f"AVR={avr}",
            f"power={power}",
            f"bus={bus_owner}",
        ]
        if transfer is not None:
            parts.append(f"transfer={transfer}")
        parts.extend(
            [
                f"{self.profiles[GeneratorSlot.A].display_name}: {generator_a}",
                f"{self.profiles[GeneratorSlot.B].display_name}: {generator_b}",
                f"primary={primary}",
            ]
        )
        self.log.info("%s.", "; ".join(parts))

    def _log_events(self, events: tuple[SupervisorEvent, ...]) -> None:
        methods = {
            "info": self.log.info,
            "warning": self.log.warning,
            "critical": self.log.critical,
        }
        for event in events:
            methods.get(event.level, self.log.info)("%s", event.message)

    async def _stop_requested_within(self, seconds: float) -> bool:
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False


def _seconds_left(value: float) -> int:
    return max(0, int(math.ceil(value)))


def _boolean_option(options: dict[str, Any], name: str) -> bool:
    value = options[name]
    if not isinstance(value, bool):
        raise ValueError(f"Параметр {name} должен быть JSON boolean")
    return value


def load_options(path: str | Path = "/data/options.json") -> dict[str, Any]:
    options_path = Path(path)
    if not options_path.exists():
        return dict(DEFAULT_OPTIONS)
    with options_path.open("r", encoding="utf-8") as stream:
        loaded = json.load(stream)
    if not isinstance(loaded, dict):
        raise ValueError("options.json должен содержать JSON object")
    return {**DEFAULT_OPTIONS, **loaded}


def configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


async def async_main() -> None:
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise RuntimeError(
            "SUPERVISOR_TOKEN не найден. Проверьте homeassistant_api: true в config.yaml."
        )

    options = load_options()
    configure_logging(str(options.get("log_level", "info")))
    app = EnergySupervisorApp(options, token)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, app.request_stop)
        except NotImplementedError:
            pass

    command_task = asyncio.create_task(
        app.read_stdin_commands(),
        name="energy-ats-stdin",
    )
    try:
        await app.run()
    finally:
        command_task.cancel()
        await asyncio.gather(command_task, return_exceptions=True)


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

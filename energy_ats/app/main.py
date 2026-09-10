"""Composition root EnergyATS: HA -> observations -> policy -> hardware commands."""

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from domain import (
    GeneratorSlot,
    PowerPath,
    PowerSource,
    SessionReason,
    SupervisorEvent,
)
from energy_supervisor import (
    EnergySupervisor,
    SupervisorConfig,
    SupervisorDecision,
    SupervisorObservation,
    SupervisorPhase,
)
from exercise_scheduler import (
    ExerciseConfig,
    ExerciseDecision,
    ExerciseGeneratorObservation,
    ExerciseObservation,
    ExerciseScheduler,
)
from generator_bus import GeneratorBusOwner, GeneratorBusTracker
from generator_controller import (
    GeneratorAction,
    GeneratorController,
    GeneratorPhase,
    GeneratorProfile,
    default_generator_profiles,
)
from ha_adapter import HardwareSnapshot, HomeAssistantAdapter
from ha_client import HomeAssistantClient, HomeAssistantConnectionError
from load_manager import (
    LoadManager,
    LoadManagerConfig,
    LoadManagerObservation,
)
from power_transfer import PowerTransferController, TransferAction, TransferPhase
from state_store import StateStore

APP_VERSION = "0.6.0"
STATE_SCHEMA_VERSION = 2

DEFAULT_OPTIONS: dict[str, Any] = {
    "armed": False,
    "tick_seconds": 1.0,
    "log_level": "info",
    "grid_failure_delay": 60,
    "grid_restore_stable_time": 60,
    "generator_a_enabled": True,
    "generator_b_enabled": True,
    "transfer_confirmation_timeout": 60,
    "load_management_enabled": False,
    "load_measurement_stabilization_time": 10,
    "load_restore_margin_percent": 15,
    "nominal_overload_time": 20,
    "maximum_overload_confirmation_time": 4,
    "load_restore_retry_interval": 300,
    "family_presence_entity": "group.family",
    "generator_a_exercise_enabled": False,
    "generator_a_exercise_interval_days": 30,
    "generator_a_exercise_start_time": "15:00",
    "generator_a_exercise_run_minutes": 10,
    "generator_a_exercise_presence_grace_days": 7,
    "generator_b_exercise_enabled": False,
    "generator_b_exercise_interval_days": 45,
    "generator_b_exercise_start_time": "15:00",
    "generator_b_exercise_run_minutes": 10,
    "generator_b_exercise_presence_grace_days": 14,
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
}


class EnergySupervisorApp:
    """Связывает чистые FSM с Home Assistant; собственной policy не содержит."""

    def __init__(self, options: dict[str, Any], token: str) -> None:
        self.options = {**DEFAULT_OPTIONS, **options}
        self.armed = _boolean_option(self.options, "armed")
        self.tick_seconds = max(0.2, float(self.options["tick_seconds"]))
        self.local_time_zone = timezone.utc

        self.log = logging.getLogger("energy_supervisor")
        self.client = HomeAssistantClient(token, logger=self.log)
        exercise_configs = self._exercise_configs()
        load_manager_config = self._load_manager_config()
        self.adapter = HomeAssistantAdapter(
            self.client,
            armed=self.armed,
            logger=self.log,
            family_presence_entity=str(self.options["family_presence_entity"]),
        )
        self.generator_controllers = {
            slot: GeneratorController(profile)
            for slot, profile in default_generator_profiles().items()
        }
        self.power_transfer = PowerTransferController(
            confirmation_timeout=float(self.options["transfer_confirmation_timeout"])
        )
        self.state_store = StateStore(str(self.options["state_file"]))

        saved, load_error = self._load_state()
        (
            self.generator_bus,
            self.supervisor,
            self.exercise_scheduler,
            self.load_manager,
        ) = self._restore_state(
            saved,
            load_error,
            exercise_configs,
            load_manager_config,
        )

        self._pending_action_records: list[dict[str, str]] = []
        self._saved_state_signature: str | None = None
        self._last_runtime_signature: tuple[str, ...] | None = None
        self._last_generator_config_signature: tuple[Any, ...] | None = None
        self._last_status_payload: dict[str, Any] | None = None
        self.stop_event = asyncio.Event()
        self.commands_ready = False

    # Process ---------------------------------------------------------

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
        handler = {
            "start_generator": self.supervisor.request_manual_start,
            "stop_generator": self.supervisor.request_manual_stop,
            "reset": self.supervisor.request_recovery_reset,
        }.get(command)
        if handler is None:
            self.log.warning("Неизвестная команда Energy ATS: %s", command)
        elif not self.armed:
            self.log.info("DISARMED: команда %s проигнорирована.", command)
        elif not self.commands_ready:
            self.log.warning("Команда %s отклонена: App ещё не готов.", command)
        else:
            handler()

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
                self._set_home_assistant_timezone(await self.client.get_time_zone())
                await self._wait_until_required_entities_ready()
                if self.stop_event.is_set():
                    break
                self._sync_generator_configuration(self.adapter.snapshot())
                self.commands_ready = True
                while not self.stop_event.is_set():
                    if not self.client.connected.is_set():
                        raise HomeAssistantConnectionError("WebSocket HA потерян")
                    await self._tick(time.time())
                    await self._stop_requested_within(self.tick_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self.stop_event.is_set():
                    self._record_interrupted_connection(exc)
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

    def _set_home_assistant_timezone(self, value: str) -> None:
        try:
            self.local_time_zone = ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise HomeAssistantConnectionError(
                f"Home Assistant сообщил неизвестную time_zone: {value!r}"
            ) from exc

    # One control tick ------------------------------------------------

    async def _tick(self, now: float) -> None:
        hardware = self._apply_bus_model(self.adapter.snapshot())
        self._sync_generator_configuration(hardware)
        self._refresh_component_views(now, hardware)

        if self.supervisor.consume_recovery_reset_request():
            self._start_recovery_reset(hardware)
        if self.supervisor.recovery_reset_in_progress:
            await self._tick_recovery_reset(now, hardware)
            return

        observation = self._supervisor_observation(hardware)
        exercise_observation = self._exercise_observation(now, hardware, observation)
        exercise_decision = self.exercise_scheduler.step(exercise_observation)
        exercise_events = list(exercise_decision.events)

        for warning in exercise_decision.warnings:
            if await self.adapter.publish_user_notification(warning.message):
                sent_at = datetime.fromtimestamp(now, self.local_time_zone)
                exercise_events.append(
                    self.exercise_scheduler.confirm_warning(
                        warning.slot,
                        warning.window_date,
                        self._profile(warning.slot).display_name,
                        sent_at,
                    )
                )
                # Delivery is a safety prerequisite for a future forced start.
                # Persist it immediately instead of waiting for the end of tick.
                self._save_state(force=True)

        decision = self.supervisor.step(
            now,
            observation,
            exercise_owned_slot=exercise_decision.owned_slot,
            exercise_desired_running=exercise_decision.desired_running,
        )

        # An outage session may explicitly adopt the already running exercise
        # generator. Only after the Supervisor session exists do we release the
        # Scheduler's stop ownership.
        if (
            self.exercise_scheduler.owned_slot is not None
            and self.supervisor.session is not None
            and self.supervisor.session.reason == SessionReason.GRID_OUTAGE
            and self.supervisor.session.generator == self.exercise_scheduler.owned_slot
            and hardware.grid_ready is False
        ):
            exercise_events.extend(
                self.exercise_scheduler.handoff_to_outage(
                    self.supervisor.session.generator,
                    exercise_observation,
                )
            )
            exercise_decision = ExerciseDecision(
                owned_slot=None,
                desired_running=False,
                authorized_shutdown_slot=None,
                warnings=(),
                events=(),
            )

        # A manual session has priority. An exercise that has not physically
        # started yet can be deferred without touching the engine.
        if (
            self.exercise_scheduler.owned_slot is not None
            and self.supervisor.session is not None
            and self.supervisor.session.reason != SessionReason.GRID_OUTAGE
        ):
            exercise_events.extend(
                self.exercise_scheduler.cancel_unstarted(
                    exercise_observation,
                    "начата пользовательская managed-сессия",
                )
            )

        # Если общая policy уже требует Recovery, Scheduler не имеет права
        # потерять автоматически запущенный двигатель. Он переводит собственный
        # attempt в FAILED/STOPPING и сохраняет обязанность безопасной остановки.
        if (
            self.supervisor.phase == SupervisorPhase.RECOVERY_REQUIRED
            and self.exercise_scheduler.owned_slot is not None
        ):
            exercise_events.extend(
                self.exercise_scheduler.fail_active(
                    exercise_observation,
                    "EnergyATS перешёл в RECOVERY_REQUIRED во время пробного запуска.",
                )
            )

        actions_allowed = self.armed and decision.actions_allowed
        load_decision = self.load_manager.step(
            self._load_manager_observation(
                now,
                hardware,
                decision,
                actions_enabled=actions_allowed,
            )
        )
        load_events = list(load_decision.events)
        for message in load_decision.notifications:
            await self.adapter.publish_user_notification(message)

        # Load Manager commands are local soft-dependency actions. Its pending
        # transaction is persisted in load_manager state, but never placed into
        # the core pending_actions journal that would force system Recovery.
        if load_decision.actions and actions_allowed:
            self._save_state(force=True)
            failures = await self.adapter.execute_load_actions(list(load_decision.actions))
            for action, error in failures:
                event, notification = self.load_manager.report_execution_failure(
                    action, error
                )
                load_events.append(event)
                await self.adapter.publish_user_notification(notification)
            if failures:
                self._save_state(force=True)

        generator_actions: list[GeneratorAction] = []
        shutdown_errors: list[str] = []
        exercise_shutdown_slot = self.exercise_scheduler.authorized_shutdown_slot
        for slot, controller in self.generator_controllers.items():
            if slot in decision.stop_outage_generators and actions_allowed:
                actions, error = controller.step_authorized_shutdown(
                    now, hardware.generators[slot]
                )
                generator_actions.extend(actions)
                if error is not None:
                    shutdown_errors.append(error)
                continue

            if slot == exercise_shutdown_slot and self.armed:
                actions, error = controller.step_authorized_shutdown(
                    now,
                    hardware.generators[slot],
                )
                generator_actions.extend(actions)
                if error is not None:
                    shutdown_errors.append(error)
                continue

            generator_actions.extend(
                controller.step(
                    now,
                    hardware.generators[slot],
                    desired_running=decision.desired_generators[slot],
                    actions_allowed=actions_allowed,
                    stable_managed_session=(
                        decision.stable_managed_generator == slot
                        or self.exercise_scheduler.owns(slot)
                    ),
                )
            )

        if shutdown_errors:
            self.supervisor.require_recovery("; ".join(shutdown_errors))

        transfer_desired_source = decision.desired_source
        if (
            transfer_desired_source == PowerSource.GENERATOR
            and not load_decision.transfer_permitted
        ):
            # Generator is ready, but available managed consumer groups have not
            # yet confirmed pre-transfer OFF. TPC simply waits; no core fault.
            transfer_desired_source = None

        transfer_actions = self.power_transfer.step(
            now,
            hardware.power_transfer,
            transfer_desired_source,
            desired_generator_ready=self._desired_generator_ready(
                decision.desired_source, hardware
            ),
            actions_allowed=actions_allowed,
        )
        await self._execute_controller_actions(transfer_actions, generator_actions)
        await self._finish_tick(
            now,
            hardware,
            tuple((*decision.events, *exercise_events, *load_events)),
        )

    def _apply_bus_model(self, hardware: HardwareSnapshot) -> HardwareSnapshot:
        session = self.supervisor.session
        bus = self.generator_bus.update(
            {slot: hardware.generators[slot].running for slot in GeneratorSlot},
            grid_ready=hardware.grid_ready,
            test_mode=hardware.test_mode,
            managed_slot=session.generator if session is not None else None,
            managed_outage=bool(session is not None and session.grid_was_unavailable),
            internal_test_slots=self.exercise_scheduler.internal_test_slots,
        )

        generators = {}
        for slot, item in hardware.generators.items():
            if hardware.power_transfer.house_on_generator is False:
                load_connected: bool | None = False
            elif (
                hardware.power_transfer.house_on_generator is True
                and bus.owner_slot is not None
            ):
                load_connected = bus.owner_slot == slot
            else:
                load_connected = None
            generators[slot] = replace(item, load_connected=load_connected)
        return replace(hardware, generators=generators)

    def _desired_generator_ready(
        self,
        desired_source: PowerSource | None,
        hardware: HardwareSnapshot,
    ) -> bool:
        if desired_source != PowerSource.GENERATOR:
            return False

        session = self.supervisor.session
        if session is not None:
            status = self.generator_controllers[session.generator].status(
                hardware.generators[session.generator]
            )
            if status.ready_for_load:
                return True

        owner = self.generator_bus.status().owner_slot
        return owner is not None and hardware.generators[owner].running is True

    # Configuration / observations ----------------------------------

    def _sync_generator_configuration(self, hardware: HardwareSnapshot) -> None:
        metadata = hardware.generator_metadata
        if any(metadata[slot] is None for slot in GeneratorSlot):
            raise ValueError("Не удалось прочитать имя или модель генераторов из HA.")

        names = [metadata[slot].name for slot in GeneratorSlot]  # type: ignore[union-attr]
        if not all(names) or len(set(names)) != len(names):
            raise ValueError("Имена Generator A/B должны быть непустыми и различаться.")
        if hardware.primary_generator is None:
            raise ValueError(
                "select.primary_generator должен совпадать с именем Generator A или B."
            )

        signature = tuple(
            value
            for slot in GeneratorSlot
            for value in (
                metadata[slot].name,  # type: ignore[union-attr]
                metadata[slot].model,  # type: ignore[union-attr]
                metadata[slot].nominal_power,  # type: ignore[union-attr]
                metadata[slot].maximum_power,  # type: ignore[union-attr]
            )
        ) + (hardware.primary_generator,)
        if signature == self._last_generator_config_signature:
            return

        for slot in GeneratorSlot:
            item = metadata[slot]
            assert item is not None
            controller = self.generator_controllers[slot]
            controller.profile = replace(
                controller.profile,
                display_name=item.name,
                model=item.model,
            )

        config = self._supervisor_config(hardware.primary_generator)
        if not config.generator_enabled(hardware.primary_generator):
            raise ValueError(
                f"Основной генератор {self._profile(hardware.primary_generator).display_name} "
                "запрещён политикой EnergyATS."
            )
        self.supervisor.config = config
        self._last_generator_config_signature = signature

        for slot in GeneratorSlot:
            profile = self._profile(slot)
            item = metadata[slot]
            assert item is not None
            marker = "PRIMARY" if slot == hardware.primary_generator else "SECONDARY"
            power_text = (
                f"nominal={item.nominal_power:.0f} W, maximum={item.maximum_power:.0f} W"
                if item.nominal_power is not None and item.maximum_power is not None
                else "power metadata unavailable"
            )
            self.log.info(
                "Generator %s: %s; модель: %s; %s; %s; choke: %s.",
                slot.value,
                profile.display_name,
                profile.model,
                marker,
                power_text,
                profile.choke_strategy.value,
            )

    def _profile(self, slot: GeneratorSlot) -> GeneratorProfile:
        return self.generator_controllers[slot].profile

    def _refresh_component_views(self, now: float, hardware: HardwareSnapshot) -> None:
        exercise_slot = self.exercise_scheduler.owned_slot
        for slot, controller in self.generator_controllers.items():
            exercise_wants_running = bool(
                exercise_slot == slot
                and self.exercise_scheduler.active_attempt is not None
                and self.exercise_scheduler.active_attempt.phase.value != "stopping"
            )
            controller.step(
                now,
                hardware.generators[slot],
                desired_running=(
                    self.supervisor.desired_generators[slot]
                    or exercise_wants_running
                ),
                actions_allowed=False,
                stable_managed_session=(
                    self.supervisor.manages_stable_generator(slot)
                    or self.exercise_scheduler.owns(slot)
                ),
            )
        self.power_transfer.step(
            now,
            hardware.power_transfer,
            self.supervisor.desired_source,
            desired_generator_ready=self._desired_generator_ready(
                self.supervisor.desired_source, hardware
            ),
            actions_allowed=False,
        )

    def _supervisor_observation(self, hardware: HardwareSnapshot) -> SupervisorObservation:
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
        )

    def _exercise_observation(
        self,
        now: float,
        hardware: HardwareSnapshot,
        supervisor_observation: SupervisorObservation,
    ) -> ExerciseObservation:
        power = supervisor_observation.power
        policy_busy = (
            self.supervisor.session is not None
            or self.supervisor.phase
            not in {SupervisorPhase.NORMAL, SupervisorPhase.WAITING_FOR_DATA}
            or self.supervisor.has_pending_session_request
            or self.supervisor.recovery_reset_in_progress
        )
        return ExerciseObservation(
            now=now,
            local_now=datetime.fromtimestamp(now, self.local_time_zone),
            grid_ready=hardware.grid_ready,
            grid_path_stable=(
                power.actual_path == PowerPath.GRID
                and not power.transition_in_progress
                and hardware.power_transfer.generator_selected is False
                and hardware.power_transfer.house_on_generator is False
            ),
            family_present=hardware.family_present,
            emergency_stop=hardware.emergency_stop,
            required_states_known=supervisor_observation.required_states_known,
            power_transition_in_progress=power.transition_in_progress,
            policy_busy=policy_busy,
            actions_enabled=self.armed,
            generators={
                slot: ExerciseGeneratorObservation(
                    running=status.running,
                    remote_on=status.remote_on,
                    fault=status.fault,
                )
                for slot, status in supervisor_observation.generators.items()
            },
            generator_names={
                slot: self._profile(slot).display_name for slot in GeneratorSlot
            },
        )

    def _load_manager_observation(
        self,
        now: float,
        hardware: HardwareSnapshot,
        decision: SupervisorDecision,
        *,
        actions_enabled: bool,
    ) -> LoadManagerObservation:
        bus_owner = self.generator_bus.status().owner_slot
        session_slot = self.supervisor.session.generator if self.supervisor.session else None
        limit_slot = bus_owner if hardware.power_transfer.house_on_generator is True else session_slot
        metadata = (
            hardware.generator_metadata.get(limit_slot)
            if limit_slot is not None
            else None
        )
        managed_ready = False
        if session_slot is not None:
            managed_ready = self.generator_controllers[session_slot].status(
                hardware.generators[session_slot]
            ).ready_for_load

        load = hardware.load_management
        return LoadManagerObservation(
            now=now,
            house_on_generator=hardware.power_transfer.house_on_generator,
            house_on_grid=hardware.power_transfer.house_on_grid,
            desired_generator_supply=(
                decision.desired_source == PowerSource.GENERATOR
                and self.supervisor.session is not None
            ),
            managed_generator_ready=managed_ready,
            power_transition_in_progress=self.power_transfer.status().transition_in_progress,
            bus_owner=bus_owner,
            nominal_power=metadata.nominal_power if metadata is not None else None,
            maximum_power=metadata.maximum_power if metadata is not None else None,
            meter_ready=load.meter_ready,
            generator_power=load.generator_power,
            power_sample_id=load.power_sample_id,
            groups=load.groups,
            generator_name=(
                self._profile(limit_slot).display_name if limit_slot is not None else None
            ),
            actions_enabled=actions_enabled,
        )

    # Recovery --------------------------------------------------------

    def _start_recovery_reset(self, hardware: HardwareSnapshot) -> None:
        needs_reset = (
            self.supervisor.phase == SupervisorPhase.RECOVERY_REQUIRED
            or self.power_transfer.status().recovery_required
            or any(
                controller.phase == GeneratorPhase.FAULT
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

        self.supervisor.begin_recovery_reset()
        self.power_transfer.begin_recovery_to_grid_path()

    async def _tick_recovery_reset(self, now: float, hardware: HardwareSnapshot) -> None:
        blocker = self._recovery_blocker(hardware)
        if blocker is not None:
            self.supervisor.fail_recovery_reset(blocker)
            await self._finish_tick(now, hardware, self.supervisor.take_events())
            return

        transfer_actions, error = self.power_transfer.step_recovery_to_grid_path(
            now, hardware.power_transfer
        )
        if error is not None:
            self.supervisor.fail_recovery_reset(error)
            await self._finish_tick(now, hardware, self.supervisor.take_events())
            return
        if transfer_actions:
            await self._execute_controller_actions(transfer_actions, [])
            await self._finish_tick(now, hardware, self.supervisor.take_events())
            return
        if not self._grid_path_confirmed(hardware):
            await self._finish_tick(now, hardware, self.supervisor.take_events())
            return

        managed_slot = self.supervisor.session.generator if self.supervisor.session else None
        scheduler_slot = self.exercise_scheduler.owned_slot
        stop_slots = tuple(
            dict.fromkeys(
                slot for slot in (managed_slot, scheduler_slot) if slot is not None
            )
        )
        for slot in stop_slots:
            actions, error = self.generator_controllers[slot].step_authorized_shutdown(
                now,
                hardware.generators[slot],
            )
            if error is not None:
                self.supervisor.fail_recovery_reset(error)
                await self._finish_tick(now, hardware, self.supervisor.take_events())
                return
            if actions:
                await self._execute_controller_actions([], actions)
                await self._finish_tick(now, hardware, self.supervisor.take_events())
                return
            item = hardware.generators[slot]
            if item.running is not False or item.remote_on is not False:
                await self._finish_tick(now, hardware, self.supervisor.take_events())
                return

        for slot, controller in self.generator_controllers.items():
            controller.reset_if_safe(hardware.generators[slot])
        if not self.power_transfer.request_recovery_reset(hardware.power_transfer):
            self.supervisor.fail_recovery_reset(
                "Grid path не получил окончательного подтверждения."
            )
            await self._finish_tick(now, hardware, self.supervisor.take_events())
            return

        self.supervisor.complete_recovery_reset()
        self._save_state(force=True)
        await self._finish_tick(now, hardware, self.supervisor.take_events())

    def _recovery_blocker(self, hardware: HardwareSnapshot) -> str | None:
        if hardware.emergency_stop is not False:
            return "сначала снимите Generators Emergency Stop."
        blocker = self.power_transfer.recovery_blocker(hardware.power_transfer)
        if blocker is not None:
            return blocker
        if any(not item.required_states_known for item in hardware.generators.values()):
            return "неизвестны обязательные состояния генераторов."

        managed = self.supervisor.session.generator if self.supervisor.session else None
        scheduler = self.exercise_scheduler.owned_slot
        external = [
            slot
            for slot, item in hardware.generators.items()
            if (item.running is True or item.remote_on is True)
            and slot not in {managed, scheduler}
        ]
        if external:
            names = ", ".join(self._profile(slot).display_name for slot in external)
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

    # Hardware execution / persistence -------------------------------

    async def _execute_controller_actions(
        self,
        transfer_actions: list[TransferAction],
        generator_actions: list[GeneratorAction],
    ) -> None:
        self._pending_action_records = [
            {"controller": "power_transfer", "action": action.kind.value}
            for action in transfer_actions
        ] + [
            {
                "controller": "generator_controller",
                "generator": action.slot.value,
                "action": action.kind.value,
            }
            for action in generator_actions
        ]
        self._save_state(force=bool(self._pending_action_records))
        if not self._pending_action_records:
            return

        await self.adapter.execute_actions(transfer_actions, generator_actions)
        self._pending_action_records = []
        self._save_state(force=True)

    def _load_state(self) -> tuple[dict[str, Any] | None, str | None]:
        try:
            saved = self.state_store.load()
            if saved is None:
                return None, None
            if saved.get("schema_version") != STATE_SCHEMA_VERSION:
                raise ValueError(
                    "Неподдерживаемый формат состояния EnergyATS 0.4/0.5; "
                    "миграция не выполняется."
                )
            return saved, None
        except Exception as exc:
            return None, str(exc)

    def _restore_state(
        self,
        saved: dict[str, Any] | None,
        load_error: str | None,
        exercise_configs: dict[GeneratorSlot, ExerciseConfig],
        load_manager_config: LoadManagerConfig,
    ) -> tuple[
        GeneratorBusTracker,
        EnergySupervisor,
        ExerciseScheduler,
        LoadManager,
    ]:
        config = self._supervisor_config(GeneratorSlot.A)
        fresh_scheduler = ExerciseScheduler(exercise_configs)
        fresh_load_manager = LoadManager(load_manager_config)
        if load_error is not None:
            supervisor = EnergySupervisor(config)
            supervisor.require_recovery(
                f"Не удалось прочитать сохранённое состояние: {load_error}"
            )
            return (
                GeneratorBusTracker(),
                supervisor,
                fresh_scheduler,
                fresh_load_manager,
            )
        if saved is None:
            return (
                GeneratorBusTracker(),
                EnergySupervisor(config),
                fresh_scheduler,
                fresh_load_manager,
            )

        try:
            bus_payload = saved.get("generator_bus")
            supervisor_payload = saved.get("supervisor")
            if not isinstance(bus_payload, dict):
                raise ValueError("отсутствует generator_bus")
            if not isinstance(supervisor_payload, dict):
                raise ValueError("отсутствует supervisor")
            bus = GeneratorBusTracker.from_dict(bus_payload)
            supervisor = EnergySupervisor.from_dict(supervisor_payload, config)

            scheduler_payload = saved.get("exercise_scheduler")
            scheduler = (
                ExerciseScheduler.from_dict(scheduler_payload, exercise_configs)
                if isinstance(scheduler_payload, dict)
                else fresh_scheduler
            )
            if scheduler_payload is not None and not isinstance(scheduler_payload, dict):
                raise ValueError("некорректный exercise_scheduler")

            if saved.get("pending_actions"):
                supervisor.require_recovery(
                    "После restart обнаружены команды без подтверждения исполнения."
                )
        except Exception as exc:
            supervisor = EnergySupervisor(config)
            supervisor.require_recovery(
                f"Не удалось восстановить persistent state: {exc}"
            )
            return (
                GeneratorBusTracker(),
                supervisor,
                fresh_scheduler,
                fresh_load_manager,
            )

        # Load Manager persistent state is deliberately isolated from core ATS.
        # Corruption here must never make Supervisor/TPC/GC unrecoverable. Losing
        # OFF ownership is conservative: EnergyATS then simply will not turn an
        # already-OFF group back ON automatically.
        manager = fresh_load_manager
        manager_payload = saved.get("load_manager")
        if manager_payload is not None:
            try:
                if not isinstance(manager_payload, dict):
                    raise ValueError("load_manager должен быть object")
                manager = LoadManager.from_dict(manager_payload, load_manager_config)
            except Exception as exc:
                self.log.warning(
                    "Не удалось восстановить Load Manager state; используем безопасное "
                    "пустое ownership: %s",
                    exc,
                )
                manager = fresh_load_manager

        return bus, supervisor, scheduler, manager

    def _save_state(self, *, force: bool = False) -> None:
        payload = {
            "schema_version": STATE_SCHEMA_VERSION,
            "app_version": APP_VERSION,
            "supervisor": self.supervisor.to_dict(),
            "generator_bus": self.generator_bus.to_dict(),
            "exercise_scheduler": self.exercise_scheduler.to_dict(),
            "load_manager": self.load_manager.to_dict(),
            "pending_actions": list(self._pending_action_records),
        }
        signature = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if not force and signature == self._saved_state_signature:
            return
        self.state_store.save(payload)
        self._saved_state_signature = signature

    def _record_interrupted_connection(self, exc: Exception) -> None:
        self.supervisor.mark_connection_lost()
        self.power_transfer.mark_interrupted(
            time.time(), "Потеряна связь с Home Assistant."
        )
        self._save_state(force=True)
        if self.supervisor.phase == SupervisorPhase.RECOVERY_REQUIRED:
            self.log.critical(
                "Связь потеряна во время физической операции; управление заблокировано: %s",
                exc,
            )

    def _supervisor_config(self, primary: GeneratorSlot) -> SupervisorConfig:
        return SupervisorConfig(
            grid_failure_delay=float(self.options["grid_failure_delay"]),
            grid_restore_stable_time=float(self.options["grid_restore_stable_time"]),
            primary_generator=primary,
            generator_a_enabled=_boolean_option(self.options, "generator_a_enabled"),
            generator_b_enabled=_boolean_option(self.options, "generator_b_enabled"),
        )

    def _load_manager_config(self) -> LoadManagerConfig:
        return LoadManagerConfig(
            enabled=_boolean_option(self.options, "load_management_enabled"),
            measurement_stabilization_time=float(
                self.options["load_measurement_stabilization_time"]
            ),
            restore_margin_percent=float(self.options["load_restore_margin_percent"]),
            nominal_overload_time=float(self.options["nominal_overload_time"]),
            maximum_overload_confirmation_time=float(
                self.options["maximum_overload_confirmation_time"]
            ),
            restore_retry_interval=float(self.options["load_restore_retry_interval"]),
        )

    def _exercise_configs(self) -> dict[GeneratorSlot, ExerciseConfig]:
        return {
            GeneratorSlot.A: ExerciseConfig(
                enabled=_boolean_option(
                    self.options,
                    "generator_a_exercise_enabled",
                ),
                interval_days=int(self.options["generator_a_exercise_interval_days"]),
                start_time=str(self.options["generator_a_exercise_start_time"]),
                run_minutes=int(self.options["generator_a_exercise_run_minutes"]),
                presence_grace_days=int(
                    self.options["generator_a_exercise_presence_grace_days"]
                ),
            ),
            GeneratorSlot.B: ExerciseConfig(
                enabled=_boolean_option(
                    self.options,
                    "generator_b_exercise_enabled",
                ),
                interval_days=int(self.options["generator_b_exercise_interval_days"]),
                start_time=str(self.options["generator_b_exercise_start_time"]),
                run_minutes=int(self.options["generator_b_exercise_run_minutes"]),
                presence_grace_days=int(
                    self.options["generator_b_exercise_presence_grace_days"]
                ),
            ),
        }

    # Status / log ----------------------------------------------------

    async def _finish_tick(
        self,
        now: float,
        hardware: HardwareSnapshot,
        events: tuple[SupervisorEvent, ...],
    ) -> None:
        observation = self._supervisor_observation(hardware)
        self._log_events(events)
        await self.adapter.publish_events(events)
        self._log_runtime_if_changed(observation)
        await self._publish_status(now, observation)
        self._save_state()

    async def _publish_status(
        self,
        now: float,
        observation: SupervisorObservation,
    ) -> None:
        payload = self._status_payload(now, observation)
        if payload == self._last_status_payload:
            return
        if await self.adapter.publish_status(payload["state"], payload["attributes"]):
            self._last_status_payload = payload

    def _status_payload(
        self,
        now: float,
        observation: SupervisorObservation,
    ) -> dict[str, Any]:
        bus = self.generator_bus.status()
        actual_slot = (
            bus.owner_slot
            if observation.power.actual_source == PowerSource.GENERATOR
            else None
        )
        managed_slot = self.supervisor.session.generator if self.supervisor.session else None
        primary = self.supervisor.config.primary_generator
        attributes = {
            "friendly_name": "Energy ATS Status",
            "icon": "mdi:transfer-switch",
            "source": observation.power.actual_source.value,
            "phase": self.supervisor.phase.value,
            "generator": (
                self._profile(actual_slot).display_name if actual_slot else None
            ),
            "generator_model": self._profile(actual_slot).model if actual_slot else None,
            "generator_slot": actual_slot.value if actual_slot else None,
            "managed_generator": (
                self._profile(managed_slot).display_name if managed_slot else None
            ),
            "bus_owner": self._format_bus_owner(),
            "generator_a_run_context": bus.run_contexts[GeneratorSlot.A].value,
            "generator_b_run_context": bus.run_contexts[GeneratorSlot.B].value,
            "primary_generator": self._profile(primary).display_name,
            "remaining_seconds": self._remaining_seconds(now, observation),
            "session_reason": (
                self.supervisor.session.reason.value
                if self.supervisor.session
                else None
            ),
            "fallback_used": bool(
                self.supervisor.session and self.supervisor.session.fallback_used
            ),
            "armed": self.armed,
        }
        local_now = datetime.fromtimestamp(now, self.local_time_zone)
        attributes.update(self.exercise_scheduler.status_attributes(local_now, now))
        attributes.update(self.load_manager.status_attributes())
        exercise_slot = self.exercise_scheduler.owned_slot
        attributes["exercise_active_generator"] = (
            self._profile(exercise_slot).display_name if exercise_slot else None
        )
        return {
            "state": (
                self.supervisor.status_text(observation)
                if self.armed
                else "DISARMED — только наблюдение"
            ),
            "attributes": attributes,
        }

    def _remaining_seconds(
        self,
        now: float,
        observation: SupervisorObservation,
    ) -> int | None:
        if observation.power.transition_in_progress and self.power_transfer.deadline is not None:
            return _seconds_left(self.power_transfer.deadline - now)

        if self.supervisor.session is not None:
            deadline = self.generator_controllers[self.supervisor.session.generator].deadline
            if deadline is not None:
                return _seconds_left(deadline - now)

        if (
            self.supervisor.phase == SupervisorPhase.GRID_FAILURE_DELAY
            and self.supervisor.grid_failed_since is not None
        ):
            return _seconds_left(
                self.supervisor.config.grid_failure_delay
                - (now - self.supervisor.grid_failed_since)
            )

        if (
            self.supervisor.session is not None
            and self.supervisor.session.grid_was_unavailable
            and observation.grid_ready is True
            and self.supervisor.grid_ready_since is not None
            and self.supervisor.phase == SupervisorPhase.ON_GENERATOR
        ):
            return _seconds_left(
                self.supervisor.config.grid_restore_stable_time
                - (now - self.supervisor.grid_ready_since)
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
        if source == PowerSource.GENERATOR:
            owner = self.generator_bus.status().owner_slot
            return (
                f"Generator {self._profile(owner).display_name}"
                if owner
                else "Generator Unknown"
            )
        return "Unknown"

    @staticmethod
    def _format_transfer(observation: SupervisorObservation) -> str | None:
        return {
            TransferPhase.DISCONNECTING_GRID: "disconnecting Grid",
            TransferPhase.CONNECTING_GRID: "connecting Grid",
            TransferPhase.SELECTING_GENERATOR: "connecting generator bus",
            TransferPhase.DISCONNECTING_GENERATOR: "disconnecting generator bus",
            TransferPhase.RECOVERY_REQUIRED: "recovery required",
        }.get(observation.power.phase)

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
            return self._profile(owner.slot).display_name
        return "none" if owner == GeneratorBusOwner.NONE else "unknown"

    def _log_runtime_if_changed(self, observation: SupervisorObservation) -> None:
        status = (
            self.supervisor.status_text(observation)
            if self.armed
            else "DISARMED — только наблюдение"
        )
        grid = (
            "ON"
            if observation.grid_ready is True
            else "OFF"
            if observation.grid_ready is False
            else "UNKNOWN"
        )
        parts = [
            f"Состояние: {status}",
            f"Grid={grid}",
            f"AVR={'ON' if observation.automatic_transfer_enabled else 'OFF'}",
            f"power={self._format_power(observation)}",
            f"bus={self._format_bus_owner()}",
        ]
        transfer = self._format_transfer(observation)
        if transfer:
            parts.append(f"transfer={transfer}")
        if self.load_manager.config.enabled:
            parts.append(f"load_manager={self.load_manager.phase.value}")
        exercise_slot = self.exercise_scheduler.owned_slot
        if exercise_slot is not None and self.exercise_scheduler.active_attempt is not None:
            parts.append(
                f"exercise={self._profile(exercise_slot).display_name}:"
                f"{self.exercise_scheduler.active_attempt.phase.value}"
            )
        parts.extend(
            f"{self._profile(slot).display_name}: "
            f"{self._format_generator_state(slot, observation)}"
            for slot in GeneratorSlot
        )
        parts.append(
            f"primary={self._profile(self.supervisor.config.primary_generator).display_name}"
        )

        signature = tuple(parts)
        if signature != self._last_runtime_signature:
            self._last_runtime_signature = signature
            self.log.info("%s.", "; ".join(parts))

    def _log_events(self, events: tuple[SupervisorEvent, ...]) -> None:
        methods = {
            "info": self.log.info,
            "warning": self.log.warning,
            "critical": self.log.critical,
        }
        for event in events:
            methods.get(event.level, self.log.info)("%s", event.message)

    # Process helpers -------------------------------------------------

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
    if type(value) is not bool:
        raise ValueError(f"Параметр {name} должен быть JSON boolean")
    return value


def load_options(path: str | Path = "/data/options.json") -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return dict(DEFAULT_OPTIONS)
    with path.open("r", encoding="utf-8") as stream:
        loaded = json.load(stream)
    if not isinstance(loaded, dict):
        raise ValueError("options.json должен содержать JSON object")
    return {**DEFAULT_OPTIONS, **loaded}


def configure_logging(level_name: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level_name.upper(), logging.INFO),
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

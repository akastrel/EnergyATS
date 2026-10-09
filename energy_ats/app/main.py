"""Composition root АВР: HA -> observe -> decide -> plan -> execute -> publish."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from domain import GeneratorSlot, PowerPath, PowerSource, SupervisorEvent
from energy_supervisor import (
    ExerciseDirective,
    RecoveryDirective,
    SupervisorConfig,
    SupervisorDecision,
    SupervisorObservation,
    SupervisorPhase,
)
from exercise_scheduler import ExerciseConfig, ExerciseObservation
from generator_controller import (
    GeneratorAction,
    GeneratorController,
    GeneratorProfile,
    default_generator_profiles,
)
from generator_run_monitor import GeneratorRunMonitor
from ha_adapter import HardwareSnapshot, HomeAssistantAdapter
from ha_client import HomeAssistantClient, HomeAssistantConnectionError
from load_manager import LoadManagerConfig
from operator_status import OperatorOutput, build_operator_output
from power_transfer import PowerTransferController, TransferAction
from runtime_observations import (
    build_exercise_observation,
    build_load_manager_observation,
    build_supervisor_observation,
    build_ups_run_observation,
)
from state_store import StateStore
from ups_run import UPSRunConfig

APP_VERSION = "1.3.1"
STATE_SCHEMA_VERSION = 3

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
    "delayed_generator_start_enabled": False,
    "generator_charge_cycle_enabled": False,
    "generator_start_soc": 40,
    "generator_target_charge_soc": 80,
    "generator_max_start_delay_hours": 6,
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


class EnergySupervisorApp:
    """Связывает доменные компоненты с HA; собственной policy не содержит."""

    def __init__(self, options: dict[str, Any], token: str) -> None:
        self.options = _merge_options(options)
        self.armed = _boolean_option(self.options, "armed")
        self.tick_seconds = max(0.2, float(self.options["tick_seconds"]))
        self.local_time_zone = timezone.utc

        self.log = logging.getLogger("energy_supervisor")
        self.client = HomeAssistantClient(token, logger=self.log)
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

        restored = self.state_store.restore_app_state(
            schema_version=STATE_SCHEMA_VERSION,
            supervisor_config=self._supervisor_config(GeneratorSlot.A),
            exercise_configs=self._exercise_configs(),
            load_manager_config=self._load_manager_config(),
            ups_run_config=self._ups_run_config(),
            logger=self.log,
        )
        saved = restored.saved
        self.generator_bus = restored.generator_bus
        self.supervisor = restored.supervisor
        self.exercise_scheduler = restored.exercise_scheduler
        self.load_manager = restored.load_manager
        self.ups_run = restored.ups_run
        self.generator_runs = GeneratorRunMonitor.from_saved_state(
            saved,
            exercise_scheduler=self.exercise_scheduler,
            generator_name=lambda slot: self._profile(slot).display_name,
            logger=self.log,
        )

        operator_state = (
            saved.get("operator_status")
            if isinstance(saved, dict) and isinstance(saved.get("operator_status"), dict)
            else {}
        )
        weekly_key = operator_state.get("last_weekly_exercise_summary")
        self._last_weekly_exercise_summary = (
            str(weekly_key) if weekly_key is not None else None
        )

        self._pending_action_records: list[dict[str, str]] = []
        self._last_runtime_signature: tuple[str, ...] | None = None
        self._last_generator_config_signature: tuple[Any, ...] | None = None
        self._last_status_payload: dict[str, Any] | None = None
        self._last_health_payload: dict[str, Any] | None = None

        # Один интервал недоступности HA должен давать одно safety-событие,
        # независимо от числа последующих reconnect attempts.
        self._ha_outage_started_at: float | None = None
        self._ha_reconnect_failures = 0
        self._ha_outage_saw_http_502 = False
        self._ha_outage_preexisting_recovery = False
        self._ha_deferred_events: list[SupervisorEvent] = []
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
                self._last_health_payload = None
                self._set_home_assistant_timezone(await self.client.get_time_zone())
                await self._wait_until_required_entities_ready()
                if self.stop_event.is_set():
                    break
                self._sync_generator_configuration(self.adapter.snapshot())
                await self._record_connection_restored()
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
                    self._record_interrupted_connection(
                        exc,
                        connection_was_ready=self.commands_ready,
                    )
            finally:
                self.commands_ready = False
                await self.adapter.cancel_background_publications()
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
        self._observe_controllers(now, hardware)

        observation = self._supervisor_observation(hardware)
        local_now = datetime.fromtimestamp(now, self.local_time_zone)
        run_events = self.generator_runs.observe(
            now=now,
            local_now=local_now,
            observation=observation,
            session=self.supervisor.session,
        )

        if await self._tick_recovery(now, hardware, run_events):
            return

        exercise_observation = build_exercise_observation(
            now=now,
            local_now=local_now,
            hardware=hardware,
            supervisor_observation=observation,
            supervisor=self.supervisor,
            armed=self.armed,
            generator_names=self._generator_names(),
        )
        exercise_decision = self.exercise_scheduler.step(exercise_observation)
        exercise_events = list(exercise_decision.events)

        for warning in exercise_decision.warnings:
            if await self.adapter.publish_user_notification(warning.message):
                exercise_events.append(
                    self.exercise_scheduler.confirm_warning(
                        warning.slot,
                        warning.window_date,
                        self._profile(warning.slot).display_name,
                        local_now,
                    )
                )
                # Delivery is a prerequisite for a future forced Exercise start.
                self._save_state(force=True)

        ups_run_decision = self.ups_run.step(
            build_ups_run_observation(
                now=now,
                hardware=hardware,
                supervisor_observation=observation,
                supervisor=self.supervisor,
                bus_status=self.generator_bus.status(),
            )
        )

        # Единственная системная arbitration point — EnergySupervisor.
        decision = self.supervisor.step(
            now,
            observation,
            exercise_owned_slot=exercise_decision.owned_slot,
            exercise_desired_running=exercise_decision.desired_running,
            defer_automatic_start=ups_run_decision.defer_automatic_start,
            outage_delay_already_satisfied=(
                ups_run_decision.outage_delay_already_satisfied
            ),
            claim_new_outage_session=ups_run_decision.claim_new_outage_session,
            request_cycle_stop=ups_run_decision.request_cycle_stop,
            restore_grid_after_cycle=ups_run_decision.restore_grid_after_cycle,
        )
        exercise_events.extend(
            self._dispatch_exercise_directive(decision, exercise_observation)
        )
        if decision.begin_post_cycle_wait:
            self.ups_run.begin_post_cycle_wait(now)

        actions_allowed = self.armed and decision.actions_allowed
        load_decision = self.load_manager.step(
            build_load_manager_observation(
                now=now,
                hardware=hardware,
                decision=decision,
                supervisor=self.supervisor,
                power_status=self.power_transfer.status(),
                bus_status=self.generator_bus.status(),
                generator_statuses=observation.generators,
                generator_names=self._generator_names(),
                actions_enabled=actions_allowed,
            )
        )
        load_events = list(load_decision.events)
        for message in load_decision.notifications:
            self.adapter.publish_user_notification_background(message)

        # Load Manager — soft dependency; его transaction не попадает в core
        # pending_actions journal и не должен сам по себе вызывать Recovery.
        if load_decision.actions and actions_allowed:
            self._save_state(force=True)
            failures = await self.adapter.execute_load_actions(list(load_decision.actions))
            for action, error in failures:
                event, notification = self.load_manager.report_execution_failure(
                    action,
                    error,
                )
                load_events.append(event)
                self.adapter.publish_user_notification_background(notification)
            if failures:
                self._save_state(force=True)

        generator_actions, shutdown_errors = self._plan_generator_actions(
            now,
            hardware,
            decision,
            actions_allowed=actions_allowed,
        )
        if shutdown_errors:
            self.supervisor.require_recovery("; ".join(shutdown_errors))

        transfer_desired_source = decision.desired_source
        if (
            transfer_desired_source == PowerSource.GENERATOR
            and not load_decision.transfer_permitted
        ):
            transfer_desired_source = None

        transfer_actions = self.power_transfer.plan(
            now,
            hardware.power_transfer,
            transfer_desired_source,
            desired_generator_ready=self._desired_generator_ready(
                decision.desired_source,
                hardware,
            ),
            actions_allowed=actions_allowed,
        )
        await self._execute_controller_actions(
            transfer_actions,
            generator_actions,
        )
        await self._finish_tick(
            now,
            hardware,
            tuple(
                (
                    *run_events,
                    *decision.events,
                    *exercise_events,
                    *ups_run_decision.events,
                    *load_events,
                )
            ),
        )

    def _observe_controllers(self, now: float, hardware: HardwareSnapshot) -> None:
        """Один physical observation pass перед системной arbitration."""
        for slot, controller in self.generator_controllers.items():
            controller.observe(
                now,
                hardware.generators[slot],
                stable_managed_session=(
                    self.supervisor.manages_stable_generator(slot)
                    or self.exercise_scheduler.owns(slot)
                ),
            )
        self.power_transfer.observe(now, hardware.power_transfer)

    # Compatibility alias for older tests; no separate decision pass lives here.
    def _refresh_component_views(self, now: float, hardware: HardwareSnapshot) -> None:
        self._observe_controllers(now, hardware)

    def _plan_generator_actions(
        self,
        now: float,
        hardware: HardwareSnapshot,
        decision: SupervisorDecision,
        *,
        actions_allowed: bool,
    ) -> tuple[list[GeneratorAction], list[str]]:
        authorized_shutdown_slots: set[GeneratorSlot] = set()
        if actions_allowed:
            authorized_shutdown_slots.update(decision.stop_outage_generators)
        exercise_shutdown_slot = self.exercise_scheduler.authorized_shutdown_slot
        if self.armed and exercise_shutdown_slot is not None:
            authorized_shutdown_slots.add(exercise_shutdown_slot)

        actions: list[GeneratorAction] = []
        errors: list[str] = []
        for slot, controller in self.generator_controllers.items():
            if slot in authorized_shutdown_slots:
                shutdown_actions, error = controller.step_authorized_shutdown(
                    now,
                    hardware.generators[slot],
                )
                actions.extend(shutdown_actions)
                if error is not None:
                    errors.append(error)
                continue

            actions.extend(
                controller.plan(
                    now,
                    hardware.generators[slot],
                    desired_running=decision.desired_generators[slot],
                    actions_allowed=actions_allowed,
                )
            )
        return actions, errors

    def _dispatch_exercise_directive(
        self,
        decision: SupervisorDecision,
        observation: ExerciseObservation,
    ) -> tuple[SupervisorEvent, ...]:
        """Механически применить решение Supervisor к локальной FSM Exercise."""
        directive = decision.exercise_directive
        if directive == ExerciseDirective.NONE:
            return ()

        slot = decision.exercise_slot
        if slot is None:
            raise RuntimeError(
                f"Supervisor вернул {directive.value} без exercise_slot"
            )
        reason = decision.exercise_reason or directive.value

        if directive == ExerciseDirective.CANCEL_UNSTARTED:
            return self.exercise_scheduler.cancel_unstarted(observation, reason)
        if directive == ExerciseDirective.HANDOFF_TO_OUTAGE:
            return self.exercise_scheduler.handoff_to_outage(slot, observation)
        if directive == ExerciseDirective.HANDOFF_TO_MANUAL:
            return self.exercise_scheduler.handoff_to_manual(slot, observation)
        if directive == ExerciseDirective.FAIL_ACTIVE:
            return self.exercise_scheduler.fail_active(observation, reason)
        raise RuntimeError(f"Неизвестная ExerciseDirective: {directive!r}")

    # Generator bus / configuration ----------------------------------

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
                "запрещён политикой АВР."
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

    def _generator_names(self) -> dict[GeneratorSlot, str]:
        return {
            slot: self._profile(slot).display_name
            for slot in GeneratorSlot
        }

    # Observation -----------------------------------------------------

    def _supervisor_observation(
        self,
        hardware: HardwareSnapshot,
    ) -> SupervisorObservation:
        return build_supervisor_observation(
            hardware=hardware,
            armed=self.armed,
            power_status=self.power_transfer.status(),
            generator_statuses={
                slot: controller.status(hardware.generators[slot])
                for slot, controller in self.generator_controllers.items()
            },
            bus_status=self.generator_bus.status(),
        )

    # Recovery --------------------------------------------------------

    async def _tick_recovery(
        self,
        now: float,
        hardware: HardwareSnapshot,
        run_events: tuple[SupervisorEvent, ...] = (),
    ) -> bool:
        """Исполнить RecoveryDecision; выбор recovery-policy остаётся Supervisor."""
        observation = self._supervisor_observation(hardware)
        recovery = self.supervisor.recovery_step(
            observation,
            transfer_blocker=self.power_transfer.recovery_blocker(
                hardware.power_transfer
            ),
            exercise_owned_slot=self.exercise_scheduler.owned_slot,
            grid_path_confirmed=self._grid_path_confirmed(hardware),
        )
        directive = recovery.directive
        if directive == RecoveryDirective.NONE:
            return False

        if directive == RecoveryDirective.BEGIN_GRID_RECOVERY:
            self.power_transfer.begin_recovery_to_grid_path()

        elif directive == RecoveryDirective.DRIVE_GRID_RECOVERY:
            transfer_actions, error = self.power_transfer.step_recovery_to_grid_path(
                now,
                hardware.power_transfer,
            )
            if error is not None:
                self.supervisor.fail_recovery_reset(error)
            elif transfer_actions:
                await self._execute_controller_actions(transfer_actions, [])

        elif directive == RecoveryDirective.STOP_GENERATORS:
            generator_actions: list[GeneratorAction] = []
            errors: list[str] = []
            for slot in recovery.stop_generators:
                actions, error = self.generator_controllers[slot].step_authorized_shutdown(
                    now,
                    hardware.generators[slot],
                )
                generator_actions.extend(actions)
                if error is not None:
                    errors.append(error)
            if errors:
                self.supervisor.fail_recovery_reset("; ".join(errors))
            elif generator_actions:
                await self._execute_controller_actions([], generator_actions)

        elif directive == RecoveryDirective.COMPLETE:
            for slot, controller in self.generator_controllers.items():
                controller.reset_if_safe(hardware.generators[slot])
            if not self.power_transfer.request_recovery_reset(hardware.power_transfer):
                self.supervisor.fail_recovery_reset(
                    "Grid path не получил окончательного подтверждения."
                )
            else:
                self.supervisor.complete_recovery_reset()
                self._save_state(force=True)

        await self._finish_tick(
            now,
            hardware,
            (*run_events, *self.supervisor.take_events()),
        )
        return True

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

    def _save_state(self, *, force: bool = False) -> None:
        self.state_store.save_app_state(
            schema_version=STATE_SCHEMA_VERSION,
            app_version=APP_VERSION,
            supervisor=self.supervisor,
            generator_bus=self.generator_bus,
            generator_runs=self.generator_runs,
            exercise_scheduler=self.exercise_scheduler,
            load_manager=self.load_manager,
            ups_run=self.ups_run,
            last_weekly_exercise_summary=self._last_weekly_exercise_summary,
            pending_actions=self._pending_action_records,
            force=force,
        )

    # HA connection lifecycle ----------------------------------------

    def _record_interrupted_connection(
        self,
        exc: Exception,
        *,
        connection_was_ready: bool,
        now: float | None = None,
    ) -> None:
        """Зафиксировать границу одного интервала недоступности Home Assistant."""
        timestamp = time.time() if now is None else now

        if self._ha_outage_started_at is not None:
            self._ha_reconnect_failures += 1
            self._ha_outage_saw_http_502 |= "502" in str(exc)
            self.log.debug(
                "Попытка переподключения к Home Assistant не удалась (%d): %s",
                self._ha_reconnect_failures,
                exc,
            )
            return

        if not connection_was_ready:
            self.log.error(
                "Не удалось установить рабочее соединение с Home Assistant: %s",
                exc,
            )
            return

        phase_before = self.supervisor.phase
        transfer_was_in_progress = self.power_transfer.transition_in_progress
        interrupted_physical_operation = (
            phase_before
            in {
                SupervisorPhase.STARTING_GENERATOR,
                SupervisorPhase.RETURNING_TO_GRID,
                SupervisorPhase.RETURNING_TO_UPS,
            }
            or transfer_was_in_progress
        )

        self._ha_outage_started_at = timestamp
        self._ha_reconnect_failures = 0
        self._ha_outage_saw_http_502 = "502" in str(exc)
        self._ha_outage_preexisting_recovery = (
            phase_before == SupervisorPhase.RECOVERY_REQUIRED
        )

        self._ha_deferred_events = list(self.supervisor.take_events())
        self.supervisor.mark_connection_lost()

        # Observation gap invalidates any proof of physical continuity.
        self.supervisor.grid_ready_since = None
        self.supervisor.grid_failed_since = None
        self.generator_bus.invalidate_observation_history()
        self.generator_runs.invalidate_observation_history()

        self.power_transfer.mark_interrupted(
            timestamp,
            "Потеряна связь с Home Assistant.",
        )
        generated = list(self.supervisor.take_events())
        if generated:
            generated = generated[1:]
        self._ha_deferred_events.extend(generated)
        self._save_state(force=True)

        if interrupted_physical_operation:
            self.log.critical(
                "Связь с Home Assistant потеряна во время незавершённой физической "
                "операции; управление АВР заблокировано до восстановления связи: %s",
                exc,
            )
        else:
            self.log.warning(
                "Home Assistant недоступен: %s. Управляющие команды АВР временно "
                "заблокированы.",
                exc,
            )

    async def _record_connection_restored(self, *, now: float | None = None) -> None:
        """Завершить интервал недоступности одним сводным пользовательским событием."""
        if self._ha_outage_started_at is None:
            return

        timestamp = time.time() if now is None else now
        duration = max(0, int(round(timestamp - self._ha_outage_started_at)))
        reconnect_attempts = self._ha_reconnect_failures + 1
        state_count = len(getattr(self.client, "states", {}))

        parts = [
            f"Связь с Home Assistant восстановлена после {duration} с.",
            (
                f"Попыток переподключения: {reconnect_attempts}; "
                f"загружено состояний HA: {state_count}."
            ),
            "Физические состояния оборудования перечитаны.",
        ]
        if self._ha_outage_saw_http_502:
            parts.append(
                "Во время недоступности Home Assistant Core возвращал HTTP 502."
            )
        if self._ha_outage_preexisting_recovery:
            parts.append(
                "Состояние «Требуется восстановление» существовало до потери связи "
                "и не было вызвано этим отключением."
            )

        events = tuple(
            (*self._ha_deferred_events, SupervisorEvent("info", " ".join(parts)))
        )
        self._ha_outage_started_at = None
        self._ha_reconnect_failures = 0
        self._ha_outage_saw_http_502 = False
        self._ha_outage_preexisting_recovery = False
        self._ha_deferred_events = []
        await self.adapter.publish_events(events)

    # Configuration ---------------------------------------------------

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

    def _ups_run_config(self) -> UPSRunConfig:
        return UPSRunConfig(
            delayed_start_enabled=_boolean_option(
                self.options,
                "delayed_generator_start_enabled",
            ),
            charge_cycle_enabled=_boolean_option(
                self.options,
                "generator_charge_cycle_enabled",
            ),
            start_soc=float(self.options["generator_start_soc"]),
            target_soc=float(self.options["generator_target_charge_soc"]),
            max_start_delay=round(
                float(self.options["generator_max_start_delay_hours"]) * 60 * 60,
                6,
            ),
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

    # Operator output -------------------------------------------------

    def _build_operator_output(
        self,
        now: float,
        observation: SupervisorObservation,
        hardware: HardwareSnapshot,
    ) -> OperatorOutput:
        return build_operator_output(
            now=now,
            local_now=datetime.fromtimestamp(now, self.local_time_zone),
            armed=self.armed,
            observation=observation,
            hardware=hardware,
            supervisor=self.supervisor,
            bus_status=self.generator_bus.status(),
            generator_controllers=self.generator_controllers,
            power_transfer=self.power_transfer,
            exercise_scheduler=self.exercise_scheduler,
            generator_runs=self.generator_runs,
            load_manager=self.load_manager,
            ups_run=self.ups_run,
            last_weekly_exercise_summary=self._last_weekly_exercise_summary,
        )

    async def _finish_tick(
        self,
        now: float,
        hardware: HardwareSnapshot,
        events: tuple[SupervisorEvent, ...],
    ) -> None:
        observation = self._supervisor_observation(hardware)
        output = self._build_operator_output(now, observation, hardware)

        if output.weekly_summary is not None:
            events = (*events, output.weekly_summary.event)
            self._last_weekly_exercise_summary = output.weekly_summary.week_key

        await self.adapter.publish_events(events)

        if output.runtime_signature != self._last_runtime_signature:
            self._last_runtime_signature = output.runtime_signature
            self.log.info("%s", output.runtime_message)

        status_payload = {
            "state": output.status_state,
            "attributes": dict(output.status_attributes),
        }
        if status_payload != self._last_status_payload:
            await self.adapter.publish_status(
                output.status_state,
                dict(output.status_attributes),
            )
            self._last_status_payload = status_payload

        health_payload = {
            "state": output.health_state,
            "attributes": dict(output.health_attributes),
        }
        if health_payload != self._last_health_payload:
            await self.adapter.publish_health(
                output.health_state,
                dict(output.health_attributes),
            )
            self._last_health_payload = health_payload

        self._save_state()

    # Compatibility helper used by existing tests.
    def _status_payload(
        self,
        now: float,
        observation: SupervisorObservation,
        hardware: HardwareSnapshot,
    ) -> dict[str, Any]:
        output = self._build_operator_output(now, observation, hardware)
        return {
            "state": output.status_state,
            "attributes": dict(output.status_attributes),
        }

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
    return _merge_options(loaded)


def _merge_options(options: dict[str, Any]) -> dict[str, Any]:
    """Объединить options с defaults и перенести старое значение секунд в часы."""
    normalized = dict(options)
    # Старый параметр больше не влияет на решения UPS Run.
    normalized.pop("generator_min_ttg_before_start", None)
    if "generator_max_start_delay_hours" not in normalized:
        legacy_seconds = normalized.pop("generator_max_start_delay", None)
        if legacy_seconds is not None:
            normalized["generator_max_start_delay_hours"] = (
                float(legacy_seconds) / 3600
            )
    else:
        normalized.pop("generator_max_start_delay", None)
    return {**DEFAULT_OPTIONS, **normalized}


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

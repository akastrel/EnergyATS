"""Композиция Energy Supervisor и его связь с Home Assistant.

Здесь нет решений вида «когда запускать двигатель» или «в каком порядке
переключать контакторы». ``main.py`` только собирает независимые контроллеры,
передаёт им снимок физических состояний и исполняет уже сформированные команды.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from pathlib import Path
from typing import Any

from domain import GeneratorSlot, SessionReason
from energy_supervisor import (
    EnergySupervisor,
    SupervisorConfig,
    SupervisorObservation,
    SupervisorPhase,
)
from generator_controller import (
    GeneratorAction,
    GeneratorController,
    GeneratorPhase,
    default_generator_profiles,
)
from ha_adapter import ENTITIES, HardwareSnapshot, HomeAssistantAdapter
from ha_client import HomeAssistantClient, HomeAssistantConnectionError
from power_transfer import PowerTransferController, TransferAction
from state_store import StateStore


APP_VERSION = "0.3.1"


DEFAULT_OPTIONS: dict[str, Any] = {
    # Главный deployment-предохранитель. При false аппаратные service calls
    # запрещены, но физические состояния и UI-статус продолжают читаться.
    "armed": False,
    "startup_delay": 30,
    "tick_seconds": 1.0,
    "log_level": "info",

    # Энергетическая политика.
    "grid_failure_delay": 5,
    "grid_restore_stable_time": 60,
    "manual_idle_warning_seconds": 600,
    "primary_generator": "A",
    "generator_a_enabled": True,
    "generator_b_enabled": True,

    # Безопасная силовая коммутация.
    "transfer_confirmation_timeout": 60,

    # Внутренний файл App. Путь вынесен в options только ради тестов.
    "state_file": "/data/energy-supervisor-state.json",
}


class EnergySupervisorApp:
    """Один процесс, четыре явно разделённых слоя управления."""

    def __init__(self, options: dict[str, Any], token: str) -> None:
        self.options = {**DEFAULT_OPTIONS, **options}
        self.armed = _boolean_option(self.options, "armed")
        self.startup_delay = float(self.options["startup_delay"])
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
        self.supervisor = self._restore_supervisor()
        self._saved_state_signature: str | None = None
        self._pending_action_records: list[dict[str, str]] = []

        self.stop_event = asyncio.Event()
        self._last_runtime_signature: tuple[Any, ...] | None = None

        self.client.add_state_listener(
            ENTITIES["manual_start"], self._manual_start_pressed
        )
        self.client.add_state_listener(
            ENTITIES["manual_stop"], self._manual_stop_pressed
        )
        self.client.add_state_listener(
            ENTITIES["recovery_reset"], self._recovery_reset_pressed
        )

    def request_stop(self) -> None:
        self.stop_event.set()

    async def run(self) -> None:
        self.log.info("Energy ATS %s запущен.", APP_VERSION)
        self.log.info(
            "Режим: %s.",
            "ARMED — реальные команды разрешены"
            if self.armed
            else "DISARMED — только наблюдение",
        )
        for profile in self.profiles.values():
            self.log.info(
                "Generator %s: %s; модель: %s; choke: %s.",
                profile.slot.value,
                profile.display_name,
                profile.model,
                profile.choke_strategy.value,
            )

        if self.startup_delay > 0:
            self.log.info(
                "Стартовая выдержка %.0f с для восстановления HA и ESPHome.",
                self.startup_delay,
            )
            if await self._stop_requested_within(self.startup_delay):
                return

        reconnect_delay = 5.0
        while not self.stop_event.is_set():
            try:
                await self.client.connect()
                await self._wait_until_required_entities_ready()
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
                await self.client.close()

            await self._stop_requested_within(reconnect_delay)

        self.log.info("Energy ATS остановлен.")

    async def _connected_loop(self) -> None:
        while not self.stop_event.is_set():
            if not self.client.connected.is_set():
                raise HomeAssistantConnectionError("WebSocket HA потерян")

            await self._tick(time.time())
            await self._stop_requested_within(self.tick_seconds)

    async def _tick(self, now: float) -> None:
        hardware = self.adapter.snapshot()
        self._refresh_component_views(now, hardware)

        if self.supervisor.consume_recovery_reset_request():
            self._attempt_recovery_reset(now, hardware)

        observation = self._supervisor_observation(hardware)
        decision = self.supervisor.step(now, observation)
        actions_allowed = self.armed and decision.actions_allowed

        generator_actions: list[GeneratorAction] = []
        for slot, controller in self.generator_controllers.items():
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

        generator_statuses = {
            slot: controller.status(hardware.generators[slot])
            for slot, controller in self.generator_controllers.items()
        }
        desired_generator = decision.desired_source.generator
        desired_generator_ready = (
            desired_generator is not None
            and generator_statuses[desired_generator].ready_for_load
        )
        transfer_actions = self.power_transfer.step(
            now,
            hardware.power_transfer,
            decision.desired_source,
            desired_generator_ready=desired_generator_ready,
            actions_allowed=actions_allowed,
        )

        # Журнал с точным списком pending-команд записывается ДО первого
        # service call. После успешного исполнения список очищается отдельной
        # атомарной записью.
        self._pending_action_records = self._describe_actions(
            transfer_actions,
            generator_actions,
        )
        self._save_state(force=bool(self._pending_action_records))

        if self._pending_action_records:
            await self.adapter.execute_actions(transfer_actions, generator_actions)
            self._pending_action_records = []
            self._save_state(force=True)

        updated_observation = self._supervisor_observation(hardware)
        await self.adapter.publish_events(decision.events)
        await self._publish_ui(updated_observation)
        self._log_runtime_if_changed(updated_observation)

    def _refresh_component_views(
        self, now: float, hardware: HardwareSnapshot
    ) -> None:
        """До решения Supervisor обновить автоматы только наблюдениями.

        Первый вызов восстанавливает физическую картину. Последующие нужны,
        чтобы после разрыва HA или внешнего ручного действия Supervisor увидел
        новое положение до формирования цели и не применил старую цель снова.
        ``actions_allowed=False`` гарантирует отсутствие service calls.
        """
        for slot, controller in self.generator_controllers.items():
            controller.step(
                now,
                hardware.generators[slot],
                desired_running=self.supervisor.desired_generators[slot],
                actions_allowed=False,
                stable_managed_session=self.supervisor.manages_stable_generator(slot),
            )

        desired_generator = self.supervisor.desired_source.generator
        ready = (
            desired_generator is not None
            and self.generator_controllers[desired_generator]
            .status(hardware.generators[desired_generator])
            .ready_for_load
        )
        self.power_transfer.step(
            now,
            hardware.power_transfer,
            self.supervisor.desired_source,
            desired_generator_ready=ready,
            actions_allowed=False,
        )

    def _supervisor_observation(
        self, hardware: HardwareSnapshot
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
        )

    def _attempt_recovery_reset(
        self, now: float, hardware: HardwareSnapshot
    ) -> None:
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
            self.supervisor.recovery_reset_result(
                False,
                "Сброс не требуется: контроллеры не находятся в аварийном состоянии.",
            )
            return

        generators_safe = all(
            item.running is False
            and item.remote_on is False
            and item.load_connected is not True
            for item in hardware.generators.values()
        )
        if hardware.emergency_stop is not False or not generators_safe:
            self.supervisor.recovery_reset_result(
                False,
                "Сброс отклонён: сначала снимите Emergency Stop, остановите оба "
                "генератора и отключите генераторную шину.",
            )
            return

        grid_path_confirmed = (
            hardware.power_transfer.generator_selected is False
            and hardware.power_transfer.house_on_generator is False
            and hardware.power_transfer.grid_connected is True
            and (
                (
                    hardware.power_transfer.grid_ready is True
                    and hardware.power_transfer.house_on_grid is True
                )
                or (
                    hardware.power_transfer.grid_ready is False
                    and hardware.power_transfer.house_on_grid is False
                )
            )
        )
        if not grid_path_confirmed:
            self.supervisor.recovery_reset_result(
                False,
                "Сброс отклонён: сначала вручную верните схему в Grid path.",
            )
            return

        if not self.power_transfer.request_recovery_reset(hardware.power_transfer):
            self.supervisor.recovery_reset_result(
                False,
                "Сброс отклонён: силовая топология не подтверждена как Grid/МАП.",
            )
            return

        for slot, controller in self.generator_controllers.items():
            controller.request_fault_reset()
            controller.step(
                now,
                hardware.generators[slot],
                desired_running=False,
                actions_allowed=False,
            )

        reset_succeeded = all(
            controller.phase == GeneratorPhase.IDLE
            for controller in self.generator_controllers.values()
        )
        self.supervisor.recovery_reset_result(
            reset_succeeded,
            "Аварийная транзакция сброшена; управление снова разрешено."
            if reset_succeeded
            else "Сброс отклонён одним из контроллеров генераторов.",
        )

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

    async def _publish_ui(self, observation: SupervisorObservation) -> None:
        try:
            status = (
                self.supervisor.status_text(observation)
                if self.armed
                else "DISARMED — только наблюдение"
            )
            await self.adapter.publish_status(status)

            # Эти helper-ы — только UI-проекция, а не аппаратные команды.
            # Обновляем их и в DISARMED, чтобы после перехода с 0.2.5 не
            # оставались ложные признаки старой сессии.
            active = self.supervisor.session is not None
            await self.adapter.publish_session(active, self._session_mode())
        except Exception as exc:
            # UI helper-ы не являются частью силовой транзакции.
            self.log.warning("Не удалось обновить UI helper-ы: %s", exc)

    def _session_mode(self) -> str:
        if self.supervisor.session is None:
            return "none"
        if self.supervisor.session.reason == SessionReason.MANUAL_BACKUP:
            return "manual"
        return "automatic"

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

    def _restore_supervisor(self) -> EnergySupervisor:
        config = self._supervisor_config()
        try:
            saved = self.state_store.load()
            if saved is None:
                return EnergySupervisor(config)

            if "supervisor" in saved:
                journal_version = saved.get("journal_schema_version")
                if not isinstance(journal_version, int) or isinstance(
                    journal_version,
                    bool,
                ):
                    raise ValueError("Некорректная версия общего журнала")
                if journal_version != 1:
                    raise ValueError(
                        "Неподдерживаемая версия общего журнала "
                        f"{journal_version}"
                    )
            payload = saved.get("supervisor", saved)
            if not isinstance(payload, dict):
                raise ValueError("В журнале отсутствует объект supervisor")
            supervisor = EnergySupervisor.from_dict(payload, config)

            pending = saved.get("pending_actions", [])
            if pending:
                supervisor.require_recovery(
                    "После restart обнаружены команды без подтверждения исполнения."
                )
            return supervisor
        except Exception as exc:
            supervisor = EnergySupervisor(config)
            supervisor.require_recovery(
                f"Не удалось прочитать сохранённый журнал: {exc}"
            )
            return supervisor

    def _supervisor_config(self) -> SupervisorConfig:
        return SupervisorConfig(
            grid_failure_delay=float(self.options["grid_failure_delay"]),
            grid_restore_stable_time=float(
                self.options["grid_restore_stable_time"]
            ),
            manual_idle_warning_seconds=float(
                self.options["manual_idle_warning_seconds"]
            ),
            primary_generator=GeneratorSlot(str(self.options["primary_generator"])),
            generator_a_enabled=_boolean_option(
                self.options,
                "generator_a_enabled",
            ),
            generator_b_enabled=_boolean_option(
                self.options,
                "generator_b_enabled",
            ),
        )

    def _save_state(self, *, force: bool = False) -> None:
        payload = {
            "journal_schema_version": 1,
            "app_version": APP_VERSION,
            "supervisor": self.supervisor.to_dict(),
            "pending_actions": list(self._pending_action_records),
            "runtime_snapshot": {
                "generator_a_phase": self.generator_controllers[
                    GeneratorSlot.A
                ].phase.value,
                "generator_b_phase": self.generator_controllers[
                    GeneratorSlot.B
                ].phase.value,
                "power_transfer_phase": self.power_transfer.phase.value,
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

    def _log_runtime_if_changed(self, observation: SupervisorObservation) -> None:
        signature = (
            self.supervisor.phase,
            observation.power.phase,
            observation.power.actual_source,
            observation.power.actual_path,
            observation.generators[GeneratorSlot.A].phase,
            observation.generators[GeneratorSlot.B].phase,
        )
        if signature == self._last_runtime_signature:
            return
        self._last_runtime_signature = signature
        self.log.info(
            "Состояние: supervisor=%s; transfer=%s/%s/%s; A=%s; B=%s.",
            self.supervisor.phase.value,
            observation.power.phase.value,
            observation.power.actual_source.value,
            observation.power.actual_path.value,
            observation.generators[GeneratorSlot.A].phase.value,
            observation.generators[GeneratorSlot.B].phase.value,
        )

    async def _manual_start_pressed(
        self, entity_id: str, old_state: str | None, new_state: str | None
    ) -> None:
        if not self.armed:
            self.log.info("DISARMED: ручная команда ввода резерва проигнорирована.")
            return
        self.supervisor.request_manual_start()

    async def _manual_stop_pressed(
        self, entity_id: str, old_state: str | None, new_state: str | None
    ) -> None:
        if not self.armed:
            self.log.info("DISARMED: ручная команда остановки проигнорирована.")
            return
        self.supervisor.request_manual_stop()

    async def _recovery_reset_pressed(
        self, entity_id: str, old_state: str | None, new_state: str | None
    ) -> None:
        if not self.armed:
            self.log.info("DISARMED: команда recovery reset проигнорирована.")
            return
        self.supervisor.request_recovery_reset()

    async def _stop_requested_within(self, seconds: float) -> bool:
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False


# Имя оставлено как совместимый alias для внешних тестов/импортов 0.2.x.
EnergyATSApp = EnergySupervisorApp


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

    await app.run()


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

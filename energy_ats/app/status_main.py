"""Energy ATS entry point with a published Home Assistant status sensor.

The core control logic remains in ``main.py``.  This thin application layer
publishes the already calculated state of the controller as
``sensor.energy_ats_status``.  It deliberately does not make any control
choices of its own.
"""

from __future__ import annotations

import asyncio
import math
import os
import signal
import time
from typing import Any

import aiohttp

from domain import GeneratorSlot, PowerSource
from energy_supervisor import SupervisorObservation, SupervisorPhase
from main import EnergySupervisorApp, configure_logging, load_options


STATUS_ENTITY_ID = "sensor.energy_ats_status"
STATUS_API_URL = f"http://supervisor/core/api/states/{STATUS_ENTITY_ID}"


class EnergyATSStatusApp(EnergySupervisorApp):
    """EnergySupervisorApp plus a read-only status entity in Home Assistant."""

    def __init__(self, options: dict[str, Any], token: str) -> None:
        super().__init__(options, token)
        self._status_token = token
        self._status_session: aiohttp.ClientSession | None = None
        self._last_status_payload: dict[str, Any] | None = None

    async def run(self) -> None:
        self._status_session = aiohttp.ClientSession(
            headers={
                "Authorization": f"Bearer {self._status_token}",
                "Content-Type": "application/json",
            }
        )
        try:
            await super().run()
        finally:
            if self._status_session is not None:
                await self._status_session.close()
                self._status_session = None

    async def _connected_loop(self) -> None:
        """Run the normal control tick and then publish its read-only view."""
        while not self.stop_event.is_set():
            if not self.client.connected.is_set():
                from ha_client import HomeAssistantConnectionError

                raise HomeAssistantConnectionError("WebSocket HA потерян")

            now = time.time()
            await self._tick(now)

            # Build the public view only after the normal control tick.  No
            # decisions or hardware commands are made in this path.
            hardware = self.adapter.snapshot()
            observation = self._supervisor_observation(hardware)
            await self._publish_status(now, observation)

            await self._stop_requested_within(self.tick_seconds)

    async def _publish_status(
        self,
        now: float,
        observation: SupervisorObservation,
    ) -> None:
        session = self.supervisor.session
        slot = session.generator if session is not None else None

        # If there is no managed session but the house is physically confirmed
        # on a generator, expose that generator as well.
        if slot is None:
            slot = observation.power.actual_source.generator

        # External/manual generator starts are useful diagnostically even when
        # the house is not connected to that generator.
        if slot is None:
            external_slots = [
                candidate
                for candidate, status in observation.generators.items()
                if status.externally_started
            ]
            if len(external_slots) == 1:
                slot = external_slots[0]

        generator_name = self.profiles[slot].display_name if slot is not None else None
        state = (
            self.supervisor.status_text(observation)
            if self.armed
            else "DISARMED — только наблюдение"
        )

        payload = {
            "state": state,
            "attributes": {
                "friendly_name": "Energy ATS Status",
                "icon": "mdi:transfer-switch",
                "source": observation.power.actual_source.value,
                "phase": self.supervisor.phase.value,
                "generator": generator_name,
                "generator_slot": slot.value if slot is not None else None,
                "remaining_seconds": self._remaining_seconds(now, observation),
                "session_reason": (
                    session.reason.value if session is not None else None
                ),
                "armed": self.armed,
                "schema_version": 1,
            },
        }

        # Avoid duplicate writes when neither state nor attributes changed.
        if payload == self._last_status_payload:
            return

        if self._status_session is None:
            return

        try:
            async with self._status_session.post(
                STATUS_API_URL,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status not in {200, 201}:
                    text = await response.text()
                    self.log.warning(
                        "Не удалось опубликовать %s: HTTP %s: %s",
                        STATUS_ENTITY_ID,
                        response.status,
                        text[:300],
                    )
                    return
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            # Status publishing is diagnostic only and must never interrupt the
            # ATS control loop.
            self.log.warning("Ошибка публикации %s: %s", STATUS_ENTITY_ID, exc)
            return

        self._last_status_payload = payload

    def _remaining_seconds(
        self,
        now: float,
        observation: SupervisorObservation,
    ) -> int | None:
        """Return the countdown of the currently meaningful ATS wait.

        The value is derived only from deadlines/timestamps already owned by
        the real state machines.  There is intentionally no separate timer for
        the Home Assistant entity.
        """

        # Physical switching confirmation has the highest priority while a
        # transfer is in progress.
        if (
            observation.power.transition_in_progress
            and self.power_transfer.deadline is not None
        ):
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

        # While running on generator after Grid has returned, this is the
        # remaining stability window before automatic return is allowed.
        if (
            self.supervisor.phase == SupervisorPhase.ON_GENERATOR
            and session is not None
            and session.grid_was_unavailable
            and observation.grid_ready is True
            and self.supervisor.grid_ready_since is not None
        ):
            elapsed = now - self.supervisor.grid_ready_since
            return _seconds_left(
                self.supervisor.config.grid_restore_stable_time - elapsed
            )

        return None


def _seconds_left(value: float) -> int:
    return max(0, int(math.ceil(value)))


async def async_main() -> None:
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise RuntimeError(
            "SUPERVISOR_TOKEN не найден. Проверьте homeassistant_api: true в config.yaml."
        )

    options = load_options()
    configure_logging(str(options.get("log_level", "info")))
    app = EnergyATSStatusApp(options, token)

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

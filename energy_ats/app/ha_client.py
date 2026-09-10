from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp


class HomeAssistantConnectionError(RuntimeError):
    """Ошибка соединения или протокола Home Assistant WebSocket API."""


class HomeAssistantClient:
    """Минимальный асинхронный клиент Home Assistant для EnergyATS.

    WebSocket используется для state cache и service calls, REST State API —
    только для диагностического ``sensor.energy_ats_status``. Чтением WebSocket
    занимается единственный ``_reader_loop``; request ждут ответы по id.
    """

    def __init__(
        self,
        token: str,
        *,
        url: str = "ws://supervisor/core/websocket",
        api_url: str = "http://supervisor/core/api",
        logger: logging.Logger | None = None,
        request_timeout: float = 15.0,
    ) -> None:
        self.token = token
        self.url = url
        self.api_url = api_url.rstrip("/")
        self.log = logger or logging.getLogger(__name__)
        self.request_timeout = request_timeout

        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._buffer_state_events = False
        self._buffered_state_events: list[dict[str, Any]] = []

        self.states: dict[str, dict[str, Any]] = {}
        self.connected = asyncio.Event()

    async def connect(self) -> None:
        """Авторизоваться, загрузить state cache и подписаться на state_changed."""

        await self.close()
        self._session = aiohttp.ClientSession()
        try:
            self._ws = await self._session.ws_connect(
                self.url,
                heartbeat=30,
                timeout=aiohttp.ClientWSTimeout(ws_receive=90, ws_close=10),
            )

            first = await self._receive_json_direct()
            if first.get("type") != "auth_required":
                raise HomeAssistantConnectionError(
                    f"Ожидался auth_required, получено: {first!r}"
                )

            await self._ws.send_json({"type": "auth", "access_token": self.token})
            auth = await self._receive_json_direct()
            if auth.get("type") != "auth_ok":
                raise HomeAssistantConnectionError(
                    f"Авторизация Home Assistant не удалась: {auth!r}"
                )

            # Подписываемся до snapshot. События этого короткого окна
            # буферизуются и затем накладываются поверх get_states.
            self._buffer_state_events = True
            self._buffered_state_events = []
            self._reader_task = asyncio.create_task(
                self._reader_loop(),
                name="ha-websocket-reader",
            )

            await self.request("subscribe_events", event_type="state_changed")
            states = await self.request("get_states")
            result = states.get("result")
            if not isinstance(result, list):
                raise HomeAssistantConnectionError(
                    f"get_states вернул неожиданный результат: {states!r}"
                )
            self.states = {
                item["entity_id"]: item
                for item in result
                if isinstance(item, dict) and "entity_id" in item
            }

            while self._buffered_state_events:
                buffered = self._buffered_state_events
                self._buffered_state_events = []
                for event in buffered:
                    self._handle_event(event)
            self._buffer_state_events = False

            self.connected.set()
            self.log.info(
                "Соединение с Home Assistant установлено; получено %d состояний.",
                len(self.states),
            )
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        self.connected.clear()
        self._buffer_state_events = False
        self._buffered_state_events = []

        if self._reader_task is not None:
            task = self._reader_task
            self._reader_task = None
            if task is not asyncio.current_task():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass

        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None

        error = HomeAssistantConnectionError("Home Assistant WebSocket закрыт")
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    def has_entity(self, entity_id: str) -> bool:
        return entity_id in self.states

    def get_state(self, entity_id: str) -> str | None:
        item = self.states.get(entity_id)
        if item is None:
            return None
        value = item.get("state")
        return value if isinstance(value, str) else None

    async def request(self, command_type: str, **payload: Any) -> dict[str, Any]:
        if self._ws is None or self._ws.closed:
            raise HomeAssistantConnectionError("WebSocket не подключён")

        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[request_id] = future

        try:
            await self._ws.send_json(
                {"id": request_id, "type": command_type, **payload}
            )
            response = await asyncio.wait_for(
                future,
                timeout=self.request_timeout,
            )
        except Exception:
            self._pending.pop(request_id, None)
            raise

        if response.get("success") is not True:
            raise HomeAssistantConnectionError(
                f"Команда HA {command_type!r} завершилась ошибкой: {response!r}"
            )
        return response

    async def call_service(
        self,
        domain: str,
        service: str,
        *,
        service_data: dict[str, Any] | None = None,
    ) -> None:
        await self.request(
            "call_service",
            domain=domain,
            service=service,
            service_data=service_data or {},
        )

    async def get_time_zone(self) -> str:
        """Вернуть IANA timezone Home Assistant для локального scheduler-time."""
        response = await self.request("get_config")
        result = response.get("result")
        if not isinstance(result, dict):
            raise HomeAssistantConnectionError(
                f"get_config вернул неожиданный результат: {response!r}"
            )
        value = result.get("time_zone")
        if not isinstance(value, str) or not value.strip():
            raise HomeAssistantConnectionError(
                "Home Assistant не сообщил корректную time_zone."
            )
        return value.strip()

    async def set_state(
        self,
        entity_id: str,
        state: str,
        *,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        if self._session is None or self._session.closed:
            raise HomeAssistantConnectionError(
                "HTTP-сессия Home Assistant не создана"
            )

        url = f"{self.api_url}/states/{entity_id}"
        headers = {"Authorization": f"Bearer {self.token}"}
        payload = {"state": state, "attributes": attributes or {}}
        try:
            async with self._session.post(
                url,
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=self.request_timeout),
            ) as response:
                if response.status not in {200, 201}:
                    text = await response.text()
                    raise HomeAssistantConnectionError(
                        f"State API {entity_id} вернул HTTP {response.status}: "
                        f"{text[:300]}"
                    )
        except asyncio.TimeoutError as exc:
            raise HomeAssistantConnectionError(
                f"Timeout публикации состояния {entity_id}"
            ) from exc
        except aiohttp.ClientError as exc:
            raise HomeAssistantConnectionError(
                f"Ошибка HTTP при публикации состояния {entity_id}: {exc}"
            ) from exc

    async def _reader_loop(self) -> None:
        assert self._ws is not None
        try:
            async for message in self._ws:
                if message.type == aiohttp.WSMsgType.TEXT:
                    data = message.json()
                    msg_type = data.get("type")

                    if msg_type == "result":
                        request_id = data.get("id")
                        future = self._pending.pop(request_id, None)
                        if future is not None and not future.done():
                            future.set_result(data)
                        continue

                    if msg_type == "event":
                        if self._buffer_state_events:
                            self._buffered_state_events.append(data)
                        else:
                            self._handle_event(data)
                        continue

                    self.log.debug("WebSocket сообщение HA: %r", data)

                elif message.type in {
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.ERROR,
                }:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.log.error("Ошибка чтения Home Assistant WebSocket: %s", exc)
        finally:
            self.connected.clear()
            error = HomeAssistantConnectionError(
                "Соединение Home Assistant потеряно"
            )
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(error)
            self._pending.clear()

    def _handle_event(self, data: dict[str, Any]) -> None:
        event = data.get("event")
        if not isinstance(event, dict) or event.get("event_type") != "state_changed":
            return

        event_data = event.get("data")
        if not isinstance(event_data, dict):
            return
        entity_id = event_data.get("entity_id")
        if not isinstance(entity_id, str):
            return

        new_state = event_data.get("new_state")
        if isinstance(new_state, dict):
            self.states[entity_id] = new_state
        else:
            self.states.pop(entity_id, None)

    async def _receive_json_direct(self) -> dict[str, Any]:
        if self._ws is None:
            raise HomeAssistantConnectionError("WebSocket не создан")
        message = await self._ws.receive()
        if message.type != aiohttp.WSMsgType.TEXT:
            raise HomeAssistantConnectionError(
                f"Неожиданный WebSocket frame при авторизации: {message.type}"
            )
        data = message.json()
        if not isinstance(data, dict):
            raise HomeAssistantConnectionError("Некорректный JSON от Home Assistant")
        return data

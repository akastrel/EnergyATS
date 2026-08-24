from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp


StateListener = Callable[[str, str | None, str | None], Awaitable[None] | None]


class HomeAssistantConnectionError(RuntimeError):
    """Ошибка соединения или протокола Home Assistant WebSocket API."""


class HomeAssistantClient:
    """
    Минимальный асинхронный клиент Home Assistant WebSocket API.

    Почему здесь WebSocket, а не REST polling:
      * ATS должен быстро видеть смену физических датчиков;
      * в доме много сущностей, поэтому запрашивать `/api/states` каждую секунду
        было бы избыточно;
      * WebSocket даёт один начальный снимок и затем только события state_changed.

    Клиент намеренно ничего не знает про алгоритм АВР. Его обязанности:
      1. авторизоваться через SUPERVISOR_TOKEN;
      2. получить исходный список состояний;
      3. подписаться на state_changed;
      4. поддерживать локальный cache entity_id -> state;
      5. выполнять HA service calls по запросу Energy ATS App.

    Важный принцип надёжности:
    чтением WebSocket занимается только `_reader_loop()`. Все остальные корутины
    отправляют request и ждут Future по id. Это исключает ситуацию, когда два
    параллельных участка кода пытаются одновременно читать одно соединение.
    """

    def __init__(
        self,
        token: str,
        *,
        url: str = "ws://supervisor/core/websocket",
        logger: logging.Logger | None = None,
        request_timeout: float = 15.0,
    ) -> None:
        self.token = token
        self.url = url
        self.log = logger or logging.getLogger(__name__)
        self.request_timeout = request_timeout

        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._listeners: dict[str, list[StateListener]] = defaultdict(list)

        # Сохраняем весь объект state из HA, а не только строку `state`.
        # Сейчас ATS использует в основном строковые состояния, но attributes
        # могут пригодиться позже без изменения транспорта.
        self.states: dict[str, dict[str, Any]] = {}
        self.connected = asyncio.Event()

    async def connect(self) -> None:
        """Подключиться, авторизоваться, загрузить state cache и подписаться на события."""
        await self.close()

        self._session = aiohttp.ClientSession()
        try:
            self._ws = await self._session.ws_connect(
                self.url,
                heartbeat=30,
                timeout=aiohttp.ClientWSTimeout(ws_receive=90, ws_close=10),
            )

            # Home Assistant первым присылает auth_required.
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

            # После авторизации запускается единственный постоянный reader.
            self._reader_task = asyncio.create_task(
                self._reader_loop(), name="ha-websocket-reader"
            )

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

            # Подписываемся после initial snapshot. Возможна очень короткая гонка
            # между get_states и subscribe_events, но следующий физический фронт
            # всё равно придёт. Для ATS это приемлемо; после reconnect core также
            # полностью реконструирует состояние из физической картины.
            await self.request("subscribe_events", event_type="state_changed")
            self.connected.set()
            self.log.info(
                "Соединение с Home Assistant установлено; получено %d состояний.",
                len(self.states),
            )
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        """Корректно закрыть соединение и разбудить ожидающие request с ошибкой."""
        self.connected.clear()

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

    def add_state_listener(self, entity_id: str, callback: StateListener) -> None:
        """Подписать локальный callback на изменение конкретной HA-сущности."""
        self._listeners[entity_id].append(callback)

    def has_entity(self, entity_id: str) -> bool:
        return entity_id in self.states

    def get_state(self, entity_id: str) -> str | None:
        item = self.states.get(entity_id)
        if item is None:
            return None
        value = item.get("state")
        return value if isinstance(value, str) else None

    def get_attributes(self, entity_id: str) -> dict[str, Any]:
        item = self.states.get(entity_id) or {}
        attrs = item.get("attributes")
        return attrs if isinstance(attrs, dict) else {}

    async def request(self, command_type: str, **payload: Any) -> dict[str, Any]:
        """Отправить WebSocket command и дождаться соответствующего result по id."""
        if self._ws is None or self._ws.closed:
            raise HomeAssistantConnectionError("WebSocket не подключён")

        request_id = self._next_id
        self._next_id += 1

        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = future

        message = {"id": request_id, "type": command_type, **payload}
        try:
            await self._ws.send_json(message)
            response = await asyncio.wait_for(future, timeout=self.request_timeout)
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
        """Вызвать обычный Home Assistant service через WebSocket API."""
        await self.request(
            "call_service",
            domain=domain,
            service=service,
            service_data=service_data or {},
        )

    async def _reader_loop(self) -> None:
        """Единственный постоянный consumer WebSocket входящего потока."""
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
                        await self._handle_event(data)
                        continue

                    # auth_* здесь уже не ожидаются; прочие системные сообщения
                    # оставляем только в DEBUG, чтобы не заспамливать эксплуатационный лог.
                    self.log.debug("WebSocket сообщение HA: %r", data)

                elif message.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.ERROR,
                ):
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.log.error("Ошибка чтения Home Assistant WebSocket: %s", exc)
        finally:
            self.connected.clear()
            error = HomeAssistantConnectionError("Соединение Home Assistant потеряно")
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(error)
            self._pending.clear()

    async def _handle_event(self, data: dict[str, Any]) -> None:
        event = data.get("event")
        if not isinstance(event, dict) or event.get("event_type") != "state_changed":
            return

        event_data = event.get("data")
        if not isinstance(event_data, dict):
            return

        entity_id = event_data.get("entity_id")
        if not isinstance(entity_id, str):
            return

        old_state_obj = event_data.get("old_state")
        new_state_obj = event_data.get("new_state")

        old_state = (
            old_state_obj.get("state")
            if isinstance(old_state_obj, dict)
            else None
        )
        new_state = (
            new_state_obj.get("state")
            if isinstance(new_state_obj, dict)
            else None
        )

        if isinstance(new_state_obj, dict):
            self.states[entity_id] = new_state_obj
        else:
            # Удаление сущности из registry/state machine.
            self.states.pop(entity_id, None)

        for callback in list(self._listeners.get(entity_id, ())):
            try:
                result = callback(entity_id, old_state, new_state)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                self.log.exception(
                    "Ошибка listener для %s; событие не влияет на WebSocket reader.",
                    entity_id,
                )

    async def _receive_json_direct(self) -> dict[str, Any]:
        """Используется только на auth-этапе, до запуска `_reader_loop()`."""
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

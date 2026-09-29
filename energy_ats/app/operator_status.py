"""Высокоуровневое представление состояния АВР для пользователя.

Модуль ничего не управляет и не участвует в safety/policy. Он только переводит
уже известные факты АВР в пользовательские status/health/log представления и
периодическую сводку генераторов.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import Enum
import math
from typing import Any, Mapping

from domain import GeneratorSlot, GridInputState, PowerPath, PowerSource, SupervisorEvent
from energy_supervisor import EnergySupervisor, SupervisorObservation, SupervisorPhase
from generator_bus import GeneratorBusOwner, GeneratorBusStatus
from generator_controller import GeneratorController, GeneratorPhase
from ha_adapter import HardwareSnapshot
from load_manager import LoadManager, LoadManagerPhase
from power_transfer import PowerTransferController, TransferPhase
from ups_run import UPSRun, UPSRunState


class HealthLevel(str, Enum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


@dataclass(frozen=True)
class GeneratorHealth:
    name: str
    enabled: bool
    running: bool | None
    fault: str | None


@dataclass(frozen=True)
class HealthInputs:
    armed: bool
    automatic_transfer_enabled: bool
    emergency_stop: bool | None
    required_states_known: bool
    recovery_required: bool
    recovery_reason: str | None
    grid_input_state: GridInputState | None
    generators: tuple[GeneratorHealth, ...]
    bus_owner_unknown_while_running: bool = False
    ups_run_enabled: bool = False
    ups_run_degraded: bool = False
    ups_run_reason: str | None = None
    load_manager_enabled: bool = False
    load_manager_degraded: bool = False
    load_manager_reason: str | None = None
    exercise_failures: tuple[str, ...] = ()


@dataclass(frozen=True)
class HealthStatus:
    level: HealthLevel
    summary: str
    reasons: tuple[str, ...]

    @property
    def icon(self) -> str:
        return {
            HealthLevel.GREEN: "mdi:check-circle",
            HealthLevel.YELLOW: "mdi:alert-circle",
            HealthLevel.RED: "mdi:close-circle",
        }[self.level]

    def attributes(self) -> dict[str, Any]:
        return {
            "friendly_name": "АВР — состояние",
            "level": self.level.value,
            "summary": self.summary,
            "reasons": list(self.reasons),
            "icon": self.icon,
        }


def evaluate_health(inputs: HealthInputs) -> HealthStatus:
    """Свести уже известные факты к пользовательскому светофору.

    RED означает, что автоматическое резервирование сейчас нельзя считать
    работоспособным. YELLOW означает, что core АВР остаётся работоспособным, но
    есть конкретная причина для внимания. Обычный Grid outage сам по себе не
    ухудшает health: исправный АВР именно для него и предназначен.
    """

    red: list[str] = []
    yellow: list[str] = []

    if not inputs.armed:
        red.append("АВР работает только в режиме наблюдения: аппаратные команды отключены.")
    if not inputs.automatic_transfer_enabled:
        red.append("Автоматический переход на резервное питание отключён.")

    if inputs.emergency_stop is True:
        red.append("Активирован аварийный STOP генераторов.")
    elif inputs.emergency_stop is None:
        red.append("Состояние аварийного STOP генераторов неизвестно.")

    if not inputs.required_states_known:
        red.append("Неизвестны обязательные сигналы обратной связи АВР.")

    if inputs.recovery_required:
        reason = inputs.recovery_reason or "требуется безопасное восстановление силовой схемы"
        red.append(f"АВР требует восстановления: {reason}.")

    if inputs.bus_owner_unknown_while_running:
        red.append("Нельзя достоверно определить владельца работающей генераторной шины.")

    enabled_faults = [g for g in inputs.generators if g.enabled and g.fault]
    enabled_generators = [g for g in inputs.generators if g.enabled]
    if enabled_generators and len(enabled_faults) == len(enabled_generators):
        names = ", ".join(g.name for g in enabled_faults)
        red.append(f"Все разрешённые генераторы имеют неисправность: {names}.")
    else:
        for generator in enabled_faults:
            yellow.append(
                f"{generator.name}: {generator.fault or 'зафиксирована неисправность генератора'}."
            )

    if inputs.grid_input_state == GridInputState.PARTIAL:
        yellow.append("Входная сеть частично недоступна: отсутствует одна или несколько фаз.")
    elif inputs.grid_input_state is None:
        yellow.append("Трёхсостоянийный статус входной сети неизвестен.")

    if inputs.ups_run_enabled and inputs.ups_run_degraded:
        yellow.append(
            inputs.ups_run_reason
            or "UPS Run временно недоступен; обычный алгоритм АВР продолжает работу."
        )

    if inputs.load_manager_enabled and inputs.load_manager_degraded:
        yellow.append(
            inputs.load_manager_reason
            or "Автоматическое управление некритичными нагрузками временно недоступно."
        )

    yellow.extend(inputs.exercise_failures)

    if red:
        return HealthStatus(
            HealthLevel.RED,
            "АВР неработоспособен",
            tuple(_dedupe(red + yellow)),
        )
    if yellow:
        return HealthStatus(
            HealthLevel.YELLOW,
            "АВР работает, но требуется внимание",
            tuple(_dedupe(yellow)),
        )
    return HealthStatus(HealthLevel.GREEN, "Всё в порядке", ())


@dataclass(frozen=True)
class WeeklyExerciseSummary:
    week_key: str
    event: SupervisorEvent


@dataclass(frozen=True)
class OperatorOutput:
    status_state: str
    status_attributes: Mapping[str, Any]
    health_state: str
    health_attributes: Mapping[str, Any]
    runtime_signature: tuple[str, ...]
    runtime_message: str
    weekly_summary: WeeklyExerciseSummary | None


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


def build_operator_output(
    *,
    now: float,
    local_now: datetime,
    armed: bool,
    observation: SupervisorObservation,
    hardware: HardwareSnapshot,
    supervisor: EnergySupervisor,
    bus_status: GeneratorBusStatus,
    generator_controllers: Mapping[GeneratorSlot, GeneratorController],
    power_transfer: PowerTransferController,
    exercise_scheduler: Any,
    generator_runs: Any,
    load_manager: LoadManager,
    ups_run: UPSRun,
    last_weekly_exercise_summary: str | None,
) -> OperatorOutput:
    """Построить все операторские представления одного tick без side effects."""
    generator_names = {
        slot: controller.profile.display_name
        for slot, controller in generator_controllers.items()
    }
    exercise_attributes = exercise_scheduler.status_attributes(local_now, now)
    run_attributes = generator_runs.status_attributes()

    weekly = build_weekly_exercise_summary(
        local_now=local_now,
        exercise_attributes=exercise_attributes,
        run_attributes=run_attributes,
        generator_names=generator_names,
        last_week_key=last_weekly_exercise_summary,
    )

    status_state = _status_text(
        armed=armed,
        supervisor=supervisor,
        observation=observation,
        ups_run=ups_run,
    )
    status_attributes = _status_attributes(
        now=now,
        local_now=local_now,
        armed=armed,
        observation=observation,
        hardware=hardware,
        supervisor=supervisor,
        bus_status=bus_status,
        generator_controllers=generator_controllers,
        power_transfer=power_transfer,
        exercise_scheduler=exercise_scheduler,
        exercise_attributes=exercise_attributes,
        run_attributes=run_attributes,
        generator_runs=generator_runs,
        load_manager=load_manager,
        ups_run=ups_run,
    )

    health = evaluate_health(
        _health_inputs(
            armed=armed,
            observation=observation,
            hardware=hardware,
            supervisor=supervisor,
            bus_status=bus_status,
            generator_controllers=generator_controllers,
            load_manager=load_manager,
            ups_run=ups_run,
        )
    )
    runtime_signature = _runtime_signature(
        armed=armed,
        observation=observation,
        supervisor=supervisor,
        bus_status=bus_status,
        generator_controllers=generator_controllers,
        load_manager=load_manager,
        ups_run=ups_run,
        exercise_scheduler=exercise_scheduler,
    )
    return OperatorOutput(
        status_state=status_state,
        status_attributes=status_attributes,
        health_state=health.level.value,
        health_attributes=health.attributes(),
        runtime_signature=runtime_signature,
        runtime_message="; ".join(runtime_signature) + ".",
        weekly_summary=weekly,
    )


def _status_attributes(
    *,
    now: float,
    local_now: datetime,
    armed: bool,
    observation: SupervisorObservation,
    hardware: HardwareSnapshot,
    supervisor: EnergySupervisor,
    bus_status: GeneratorBusStatus,
    generator_controllers: Mapping[GeneratorSlot, GeneratorController],
    power_transfer: PowerTransferController,
    exercise_scheduler: Any,
    exercise_attributes: Mapping[str, Any],
    run_attributes: Mapping[str, Any],
    generator_runs: Any,
    load_manager: LoadManager,
    ups_run: UPSRun,
) -> dict[str, Any]:
    del local_now, generator_runs
    actual_slot = (
        bus_status.owner_slot
        if observation.power.actual_source == PowerSource.GENERATOR
        else None
    )
    managed_slot = (
        supervisor.session.generator if supervisor.session is not None else None
    )
    primary = supervisor.config.primary_generator
    session = supervisor.session
    attrs: dict[str, Any] = {
        "friendly_name": "Energy ATS Status",
        "icon": "mdi:transfer-switch",
        "source": observation.power.actual_source.value,
        "grid_input_state": (
            hardware.grid_input_state.value
            if hardware.grid_input_state is not None
            else "unknown"
        ),
        "phase": supervisor.phase.value,
        "generator": (
            generator_controllers[actual_slot].profile.display_name
            if actual_slot is not None
            else None
        ),
        "generator_model": (
            generator_controllers[actual_slot].profile.model
            if actual_slot is not None
            else None
        ),
        "generator_slot": actual_slot.value if actual_slot is not None else None,
        "managed_generator": (
            generator_controllers[managed_slot].profile.display_name
            if managed_slot is not None
            else None
        ),
        "bus_owner": _format_bus_owner(bus_status, generator_controllers),
        "generator_a_run_context": bus_status.run_contexts[GeneratorSlot.A].value,
        "generator_b_run_context": bus_status.run_contexts[GeneratorSlot.B].value,
        "primary_generator": generator_controllers[primary].profile.display_name,
        "remaining_seconds": _remaining_seconds(
            now=now,
            observation=observation,
            supervisor=supervisor,
            generator_controllers=generator_controllers,
            power_transfer=power_transfer,
            ups_run=ups_run,
        ),
        "session_reason": session.reason.value if session is not None else None,
        "fallback_used": bool(session and session.fallback_used),
        "cycle_session_owned_by_energy_ats": bool(session and session.cycle_owned),
        "session_manual_override": bool(session and session.manual_override),
        "armed": armed,
    }
    attrs.update(exercise_attributes)
    attrs.update(run_attributes)
    attrs.update(load_manager.status_attributes())
    attrs.update(ups_run.status_attributes(now, hardware.battery))
    exercise_slot = exercise_scheduler.owned_slot
    attrs["exercise_active_generator"] = (
        generator_controllers[exercise_slot].profile.display_name
        if exercise_slot is not None
        else None
    )
    return attrs


def _health_inputs(
    *,
    armed: bool,
    observation: SupervisorObservation,
    hardware: HardwareSnapshot,
    supervisor: EnergySupervisor,
    bus_status: GeneratorBusStatus,
    generator_controllers: Mapping[GeneratorSlot, GeneratorController],
    load_manager: LoadManager,
    ups_run: UPSRun,
) -> HealthInputs:
    any_running = any(
        status.running is True for status in observation.generators.values()
    )
    return HealthInputs(
        armed=armed,
        automatic_transfer_enabled=hardware.automatic_transfer_enabled,
        emergency_stop=hardware.emergency_stop,
        required_states_known=observation.required_states_known,
        recovery_required=(supervisor.phase == SupervisorPhase.RECOVERY_REQUIRED),
        recovery_reason=supervisor.recovery_reason,
        grid_input_state=hardware.grid_input_state,
        generators=tuple(
            GeneratorHealth(
                name=generator_controllers[slot].profile.display_name,
                enabled=supervisor.config.generator_enabled(slot),
                running=status.running,
                fault=status.fault,
            )
            for slot, status in observation.generators.items()
        ),
        bus_owner_unknown_while_running=(
            any_running and bus_status.owner == GeneratorBusOwner.UNKNOWN
        ),
        ups_run_enabled=ups_run.enabled,
        ups_run_degraded=(ups_run.state == UPSRunState.DEGRADED),
        ups_run_reason=ups_run.last_reason,
        load_manager_enabled=load_manager.config.enabled,
        load_manager_degraded=(load_manager.phase == LoadManagerPhase.DEGRADED),
        load_manager_reason=(
            load_manager.degraded_reason or load_manager.last_reason
        ),
    )


def _runtime_signature(
    *,
    armed: bool,
    observation: SupervisorObservation,
    supervisor: EnergySupervisor,
    bus_status: GeneratorBusStatus,
    generator_controllers: Mapping[GeneratorSlot, GeneratorController],
    load_manager: LoadManager,
    ups_run: UPSRun,
    exercise_scheduler: Any,
) -> tuple[str, ...]:
    status = _status_text(
        armed=armed,
        supervisor=supervisor,
        observation=observation,
        ups_run=ups_run,
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
        f"power={_format_power(observation, bus_status, generator_controllers)}",
        f"bus={_format_bus_owner(bus_status, generator_controllers)}",
    ]
    transfer = _format_transfer(observation)
    if transfer:
        parts.append(f"transfer={transfer}")
    if load_manager.config.enabled:
        parts.append(f"load_manager={load_manager.phase.value}")
    if ups_run.enabled:
        parts.append(f"ups_run={ups_run.state.value}")
    exercise_slot = exercise_scheduler.owned_slot
    if exercise_slot is not None and exercise_scheduler.active_attempt is not None:
        parts.append(
            f"exercise={generator_controllers[exercise_slot].profile.display_name}:"
            f"{exercise_scheduler.active_attempt.phase.value}"
        )
    parts.extend(
        f"{generator_controllers[slot].profile.display_name}: "
        f"{_format_generator_state(slot, observation, bus_status)}"
        for slot in GeneratorSlot
    )
    parts.append(
        f"primary={generator_controllers[supervisor.config.primary_generator].profile.display_name}"
    )
    return tuple(parts)


def _status_text(
    *,
    armed: bool,
    supervisor: EnergySupervisor,
    observation: SupervisorObservation,
    ups_run: UPSRun,
) -> str:
    if not armed:
        return "DISARMED — только наблюдение"
    if supervisor.phase == SupervisorPhase.RECOVERY_REQUIRED:
        return supervisor.status_text(observation)
    if ups_run.state == UPSRunState.WAITING_ON_UPS:
        return "Питание от UPS"
    return supervisor.status_text(observation)


def _remaining_seconds(
    *,
    now: float,
    observation: SupervisorObservation,
    supervisor: EnergySupervisor,
    generator_controllers: Mapping[GeneratorSlot, GeneratorController],
    power_transfer: PowerTransferController,
    ups_run: UPSRun,
) -> int | None:
    if observation.power.transition_in_progress and power_transfer.deadline is not None:
        return _seconds_left(power_transfer.deadline - now)

    if supervisor.session is not None:
        deadline = generator_controllers[supervisor.session.generator].deadline
        if deadline is not None:
            return _seconds_left(deadline - now)

    if ups_run.state == UPSRunState.WAITING_ON_UPS and ups_run.waiting_since is not None:
        return _seconds_left(
            ups_run.config.max_start_delay - (now - ups_run.waiting_since)
        )

    if (
        supervisor.phase == SupervisorPhase.GRID_FAILURE_DELAY
        and supervisor.grid_failed_since is not None
    ):
        return _seconds_left(
            supervisor.config.grid_failure_delay
            - (now - supervisor.grid_failed_since)
        )

    if (
        supervisor.session is not None
        and supervisor.session.grid_was_unavailable
        and observation.grid_ready is True
        and supervisor.grid_ready_since is not None
        and supervisor.phase == SupervisorPhase.ON_GENERATOR
    ):
        return _seconds_left(
            supervisor.config.grid_restore_stable_time
            - (now - supervisor.grid_ready_since)
        )
    return None


def _format_power(
    observation: SupervisorObservation,
    bus_status: GeneratorBusStatus,
    generator_controllers: Mapping[GeneratorSlot, GeneratorController],
) -> str:
    source = observation.power.actual_source
    if source == PowerSource.GRID:
        return "Grid"
    if source == PowerSource.UPS_ONLY:
        return "UPS only"
    if source == PowerSource.NO_POWER:
        return "NO POWER"
    if source == PowerSource.GENERATOR:
        owner = bus_status.owner_slot
        return (
            f"Generator {generator_controllers[owner].profile.display_name}"
            if owner is not None
            else "Generator Unknown"
        )
    return "Unknown"


def _format_transfer(observation: SupervisorObservation) -> str | None:
    return {
        TransferPhase.DISCONNECTING_GRID: "disconnecting Grid",
        TransferPhase.CONNECTING_GRID: "connecting Grid",
        TransferPhase.SELECTING_GENERATOR: "connecting generator bus",
        TransferPhase.DISCONNECTING_GENERATOR: "disconnecting generator bus",
        TransferPhase.RECOVERY_REQUIRED: "recovery required",
    }.get(observation.power.phase)


def _format_generator_state(
    slot: GeneratorSlot,
    observation: SupervisorObservation,
    bus_status: GeneratorBusStatus,
) -> str:
    if (
        observation.power.actual_path == PowerPath.GENERATOR
        and bus_status.owner_slot == slot
    ):
        return "под нагрузкой"
    return _GENERATOR_PHASE_TEXT[observation.generators[slot].phase]


def _format_bus_owner(
    bus_status: GeneratorBusStatus,
    generator_controllers: Mapping[GeneratorSlot, GeneratorController],
) -> str:
    if bus_status.owner.slot is not None:
        return generator_controllers[bus_status.owner.slot].profile.display_name
    return "none" if bus_status.owner == GeneratorBusOwner.NONE else "unknown"


def _seconds_left(value: float) -> int:
    return max(0, int(math.ceil(value)))


def build_weekly_exercise_summary(
    *,
    local_now: datetime,
    exercise_attributes: Mapping[str, Any],
    generator_names: Mapping[GeneratorSlot, str],
    last_week_key: str | None,
    run_attributes: Mapping[str, Any] | None = None,
) -> WeeklyExerciseSummary | None:
    """Сформировать одну MAIN-сводку за ISO-неделю после понедельника 09:00.

    Если App не работал в понедельник утром, сводка публикуется при первом
    последующем tick этой же недели. Ключ недели сохраняется App, поэтому restart
    не создаёт повторную запись. При наличии общей истории запусков показывается
    последний фактический запуск; иначе используется прежний qualifying run.
    """

    iso = local_now.isocalendar()
    week_key = f"{iso.year}-W{iso.week:02d}"
    monday = local_now.date() - timedelta(days=local_now.weekday())
    publish_after = datetime.combine(monday, time(9, 0), tzinfo=local_now.tzinfo)
    if local_now < publish_after or last_week_key == week_key:
        return None

    any_enabled = any(
        bool(exercise_attributes.get(f"generator_{slot.value.lower()}_exercise_enabled"))
        for slot in GeneratorSlot
    )
    if not any_enabled:
        return None

    run_attrs = run_attributes or {}
    parts = [
        _generator_summary(
            slot,
            local_now,
            exercise_attributes,
            run_attrs,
            generator_names.get(slot, f"Generator {slot.value}"),
        )
        for slot in GeneratorSlot
    ]
    message = f"Плановые проверки генераторов. {' '.join(parts)}"
    return WeeklyExerciseSummary(week_key, SupervisorEvent("info", message))


def _generator_summary(
    slot: GeneratorSlot,
    local_now: datetime,
    attrs: Mapping[str, Any],
    run_attrs: Mapping[str, Any],
    name: str,
) -> str:
    prefix = f"generator_{slot.value.lower()}_exercise"
    run_prefix = f"generator_{slot.value.lower()}"
    enabled = bool(attrs.get(f"{prefix}_enabled"))

    actual_start = _parse_datetime(run_attrs.get(f"{run_prefix}_last_run_start"))
    duration = _parse_non_negative_int(
        run_attrs.get(f"{run_prefix}_last_run_duration_seconds")
    )
    run_type = run_attrs.get(f"{run_prefix}_last_run_type")
    run_result = run_attrs.get(f"{run_prefix}_last_run_result")

    if actual_start is not None:
        details = [_format_date_ru(actual_start)]
        if duration is not None:
            details.append(_duration_text(duration))
        type_text = _run_type_text(run_type)
        if type_text is not None:
            details.append(type_text)
        if run_result == "failed":
            details.append("с ошибкой")
        last_text = f"последний запуск — {', '.join(details)}"
    else:
        last_run = _parse_datetime(attrs.get(f"{prefix}_last_qualifying_run"))
        if last_run is None:
            last_text = "успешных запусков ещё не было"
        else:
            last_text = f"последний успешный запуск — {_format_date_ru(last_run)}"

    if not enabled:
        return f"{name}: {last_text}; автоматические пробные пуски отключены."

    next_due = _parse_datetime(attrs.get(f"{prefix}_next_due"))
    if next_due is None:
        return f"{name}: {last_text}; дата следующего пробного запуска пока не определена."

    delta_days = (next_due.date() - local_now.date()).days
    relative = _relative_due_text(delta_days)
    return (
        f"{name}: {last_text}; следующий пробный запуск — "
        f"{_format_date_ru(next_due)} ({relative})."
    )


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, "", "unknown", "unavailable"):
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _parse_non_negative_int(value: Any) -> int | None:
    if value in (None, "", "unknown", "unavailable"):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _run_type_text(value: Any) -> str | None:
    return {
        "automatic": "автоматический",
        "manual": "ручной",
        "exercise": "пробный",
        "external": "внешний",
    }.get(str(value))


def _duration_text(seconds: int) -> str:
    hours, remainder = divmod(max(0, seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours} ч {minutes} мин" if minutes else f"{hours} ч"
    if minutes:
        return f"{minutes} мин {secs} с" if secs else f"{minutes} мин"
    return f"{secs} с"


_MONTHS_RU = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def _format_date_ru(value: datetime) -> str:
    return f"{value.day} {_MONTHS_RU[value.month - 1]}"


def _relative_due_text(days: int) -> str:
    if days == 0:
        return "сегодня"
    if days == 1:
        return "завтра"
    if days < 0:
        return f"просрочен на {_duration_days_ru(-days)}"
    if days < 7:
        return f"через {_duration_days_ru(days)}"

    weeks, remainder = divmod(days, 7)
    weeks_text = _plural_ru(weeks, "неделю", "недели", "недель")
    if remainder == 0:
        return f"через {weeks} {weeks_text}"
    return f"через {weeks} {weeks_text} {_duration_days_ru(remainder)}"


def _duration_days_ru(days: int) -> str:
    return f"{days} {_plural_ru(days, 'день', 'дня', 'дней')}"


def _plural_ru(value: int, one: str, few: str, many: str) -> str:
    mod100 = value % 100
    mod10 = value % 10
    if 11 <= mod100 <= 14:
        return many
    if mod10 == 1:
        return one
    if 2 <= mod10 <= 4:
        return few
    return many


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))

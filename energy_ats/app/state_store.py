"""Атомарное persistent state приложения АВР.

StateStore является единственной persistence-boundary приложения: он читает и
валидирует state-файл, восстанавливает persistent компоненты с их различной
критичностью и атомарно сохраняет общий snapshot. Доменные решения здесь не
принимаются: при ошибке core-state Supervisor только переводится в Recovery,
а soft dependencies восстанавливаются пустыми с warning.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
from typing import Any, Mapping

from domain import GeneratorSlot
from energy_supervisor import EnergySupervisor, SupervisorConfig
from exercise_scheduler import ExerciseConfig, ExerciseScheduler
from generator_bus import GeneratorBusTracker
from load_manager import LoadManager, LoadManagerConfig
from ups_run import UPSRun, UPSRunConfig


@dataclass(frozen=True)
class RestoredAppState:
    saved: dict[str, Any] | None
    generator_bus: GeneratorBusTracker
    supervisor: EnergySupervisor
    exercise_scheduler: ExerciseScheduler
    load_manager: LoadManager
    ups_run: UPSRun


class StateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._last_signature: str | None = None

    def load(self) -> dict[str, Any] | None:
        """Низкоуровневое чтение JSON; сохранено для тестов/служебных утилит."""
        if not self.path.exists():
            return None
        with self.path.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, dict):
            raise ValueError("Файл состояния должен содержать JSON object")
        return data

    def restore_app_state(
        self,
        *,
        schema_version: int,
        supervisor_config: SupervisorConfig,
        exercise_configs: Mapping[GeneratorSlot, ExerciseConfig],
        load_manager_config: LoadManagerConfig,
        ups_run_config: UPSRunConfig,
        logger: logging.Logger,
    ) -> RestoredAppState:
        """Прочитать state и восстановить persistent компоненты приложения."""
        fresh_scheduler = ExerciseScheduler(dict(exercise_configs))
        fresh_load_manager = LoadManager(load_manager_config)
        fresh_ups_run = UPSRun(ups_run_config)

        try:
            saved = self.load()
            if saved is not None and saved.get("schema_version") != schema_version:
                raise ValueError(
                    "Неподдерживаемый формат сохранённого состояния АВР; "
                    "миграция не выполняется."
                )
        except Exception as exc:
            supervisor = EnergySupervisor(supervisor_config)
            supervisor.require_recovery(
                f"Не удалось прочитать сохранённое состояние: {exc}"
            )
            return RestoredAppState(
                None,
                GeneratorBusTracker(),
                supervisor,
                fresh_scheduler,
                fresh_load_manager,
                fresh_ups_run,
            )

        if saved is None:
            return RestoredAppState(
                None,
                GeneratorBusTracker(),
                EnergySupervisor(supervisor_config),
                fresh_scheduler,
                fresh_load_manager,
                fresh_ups_run,
            )

        try:
            bus_payload = saved.get("generator_bus")
            supervisor_payload = saved.get("supervisor")
            if not isinstance(bus_payload, dict):
                raise ValueError("отсутствует generator_bus")
            if not isinstance(supervisor_payload, dict):
                raise ValueError("отсутствует supervisor")

            bus = GeneratorBusTracker.from_dict(bus_payload)
            supervisor = EnergySupervisor.from_dict(
                supervisor_payload,
                supervisor_config,
            )

            scheduler_payload = saved.get("exercise_scheduler")
            scheduler = (
                ExerciseScheduler.from_dict(
                    scheduler_payload,
                    dict(exercise_configs),
                )
                if isinstance(scheduler_payload, dict)
                else fresh_scheduler
            )
            if scheduler_payload is not None and not isinstance(
                scheduler_payload, dict
            ):
                raise ValueError("некорректный exercise_scheduler")

            if saved.get("pending_actions"):
                supervisor.require_recovery(
                    "После restart обнаружены команды без подтверждения исполнения."
                )
        except Exception as exc:
            supervisor = EnergySupervisor(supervisor_config)
            supervisor.require_recovery(
                f"Не удалось восстановить persistent state: {exc}"
            )
            return RestoredAppState(
                None,
                GeneratorBusTracker(),
                supervisor,
                fresh_scheduler,
                fresh_load_manager,
                fresh_ups_run,
            )

        # Load Manager — soft dependency. Потеря его ownership безопасно
        # означает, что уже выключенная группа не будет включена автоматически.
        manager = fresh_load_manager
        manager_payload = saved.get("load_manager")
        if manager_payload is not None:
            try:
                if not isinstance(manager_payload, dict):
                    raise ValueError("load_manager должен быть object")
                manager = LoadManager.from_dict(
                    manager_payload,
                    load_manager_config,
                )
            except Exception as exc:
                logger.warning(
                    "Не удалось восстановить Load Manager state; используем безопасное "
                    "пустое ownership: %s",
                    exc,
                )

        # UPS Run — оптимизация, а не core dependency. Потеря state возвращает
        # обычный generator-start и не переводит систему в Recovery.
        ups_run = fresh_ups_run
        ups_run_payload = saved.get("ups_run")
        if ups_run_payload is not None:
            try:
                if not isinstance(ups_run_payload, dict):
                    raise ValueError("ups_run должен быть object")
                ups_run = UPSRun.from_dict(ups_run_payload, ups_run_config)
            except Exception as exc:
                logger.warning(
                    "Не удалось восстановить UPS Run state; используем безопасное "
                    "начальное состояние: %s",
                    exc,
                )

        return RestoredAppState(
            saved,
            bus,
            supervisor,
            scheduler,
            manager,
            ups_run,
        )

    def save_app_state(
        self,
        *,
        schema_version: int,
        app_version: str,
        supervisor: EnergySupervisor,
        generator_bus: GeneratorBusTracker,
        generator_runs: Any,
        exercise_scheduler: ExerciseScheduler,
        load_manager: LoadManager,
        ups_run: UPSRun,
        last_weekly_exercise_summary: str | None,
        pending_actions: list[dict[str, str]],
        force: bool = False,
    ) -> None:
        """Собрать и атомарно сохранить единый persistent snapshot приложения."""
        payload = {
            "schema_version": schema_version,
            "app_version": app_version,
            "supervisor": supervisor.to_dict(),
            "generator_bus": generator_bus.to_dict(),
            "generator_runs": generator_runs.to_dict(),
            "exercise_scheduler": exercise_scheduler.to_dict(),
            "load_manager": load_manager.to_dict(),
            "ups_run": ups_run.to_dict(),
            "operator_status": {
                "last_weekly_exercise_summary": last_weekly_exercise_summary,
            },
            "pending_actions": list(pending_actions),
        }
        signature = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if not force and signature == self._last_signature:
            return
        self.save(payload)
        self._last_signature = signature

    def save(self, data: Mapping[str, Any]) -> None:
        """Атомарно записать state до/после физических команд."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)

        # fsync файла гарантирует его содержимое, fsync каталога — сам факт
        # атомарного rename после внезапной потери питания хоста. Windows не
        # поддерживает открытие каталога через O_DIRECTORY; сам App работает
        # в Linux, а на Windows этот дополнительный шаг безопасно пропускается.
        directory_flag = getattr(os, "O_DIRECTORY", None)
        if directory_flag is None:
            return

        directory = os.open(self.path.parent, os.O_RDONLY | directory_flag)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

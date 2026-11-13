"""Un trabajador asíncrono con checkpoints cooperativos; nunca cancela clics por timeout."""
from __future__ import annotations

import asyncio
import logging
import math
import json
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ..errors import BrowserClosed, ConnectionLost, CoreError, LoginRequired, ReadError
from ..storage.snapshots import save_snapshot
from .settings import SchedulerSettings

logger = logging.getLogger(__name__)
Callback = Callable[[], Awaitable[Any]]


def read_status(settings: SchedulerSettings) -> dict[str, Any]:
    try:
        data = json.loads(settings.status_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("jobs"), list):
            raise ValueError("Estado inválido")
        expiry = datetime.fromisoformat(data["heartbeat_valid_until"])
        data["heartbeat_is_recent"] = datetime.now(timezone.utc) <= expiry
        return data
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise CoreError("No hay un estado válido del scheduler; inicia el modo continuo para generarlo.") from error


class DependencyPending(Exception):
    """No hay datos suficientes: difiere sin consumir el presupuesto de errores."""


@dataclass
class Job:
    name: str
    priority: int
    callback: Callback
    interval: Callable[[], tuple[float, float]]
    allowed: Callable[[], str | None]
    next_due: float
    critical: bool = False
    running: bool = False
    requested: bool = False
    runs: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    deferred: int = 0
    last_result: str | None = None
    last_error_type: str | None = None
    last_duration_seconds: float | None = None
    last_started_at: str | None = None
    last_finished_at: str | None = None


class Scheduler:
    def __init__(self, settings: SchedulerSettings, *, clock: Callable[[], float] = time.monotonic, uniform: Callable[[float, float], float] = random.uniform, watchdog: Callable[[], None] | None = None, on_recoverable: Callable[[str, Exception], None] | None = None) -> None:
        self.settings, self.clock, self.uniform = settings, clock, uniform
        self.watchdog, self.on_recoverable = watchdog, on_recoverable
        self.jobs: dict[str, Job] = {}
        self.stack: list[Job] = []
        self.state = "created"
        self.last_error_type: str | None = None
        self._wake = asyncio.Event()
        self._stop = False
        self._last_publish = float("-inf")

    def add(self, name: str, callback: Callback, interval: Callable[[], tuple[float, float]], *, allowed: Callable[[], str | None] = lambda: None, critical: bool = False, immediate: bool = True) -> None:
        if name in self.jobs or name not in self.settings.priorities:
            raise ValueError("Trabajo duplicado o desconocido.")
        job = Job(name, self.settings.priorities[name], callback, interval, allowed, self.clock(), critical)
        if not immediate:
            job.next_due += self._delay(job)
        self.jobs[name] = job

    def _delay(self, job: Job) -> float:
        minimum, maximum = job.interval()
        if not 0 < minimum <= maximum or not all(map(math.isfinite, (minimum, maximum))):
            raise CoreError("Intervalo inválido en el trabajo " + job.name)
        return self.uniform(minimum, maximum)

    def trigger(self, name: str) -> None:
        job = self.jobs.get(name)
        if job is None:
            return
        if job.running:
            job.requested = True
        else:
            job.next_due = min(job.next_due, self.clock())
        self._wake.set()

    def request_stop(self) -> None:
        self._stop = True
        self._wake.set()

    def _due(self, *, before_priority: int | None = None) -> Job | None:
        candidates = [job for job in self.jobs.values() if not job.running and job.next_due <= self.clock() and job.allowed() is None and (before_priority is None or job.priority < before_priority)]
        return min(candidates, key=lambda job: (job.priority, job.next_due, job.name)) if candidates else None

    async def checkpoint(self) -> None:
        """Solo antes de adquirir el navegador, nunca dentro de una transacción."""
        if self._stop:
            raise asyncio.CancelledError()
        if not self.stack:
            return
        priority = self.stack[-1].priority
        while (job := self._due(before_priority=priority)) is not None:
            await self._execute(job)

    async def run_now(self, name: str) -> bool:
        job = self.jobs[name]
        if job.running or job.allowed() is not None:
            return False
        if self.stack and job.priority >= self.stack[-1].priority:
            raise CoreError("Una dependencia debe tener mayor prioridad que su consumidor.")
        await self._execute(job)
        return job.last_result == "ok"

    async def _execute(self, job: Job) -> None:
        if job.running:
            raise CoreError("Se ha intentado solapar un trabajo consigo mismo.")
        job.running = True
        job.requested = False
        job.last_started_at = datetime.now(timezone.utc).isoformat()
        started = self.clock()
        self.stack.append(job)
        try:
            self.publish(force=True)
            await job.callback()
            job.runs += 1
            job.consecutive_failures = 0
            job.last_result = "ok"
            job.last_error_type = None
            job.next_due = self.clock() if job.requested else self.clock() + self._delay(job)
        except DependencyPending:
            job.deferred += 1
            job.last_result = "waiting_for_data"
            job.next_due = self.clock() + self.settings.dependency_retry_seconds
        except (ReadError, LoginRequired, ConnectionLost) as error:
            job.failures += 1
            job.consecutive_failures += 1
            job.last_error_type = type(error).__name__
            job.last_result = "retrying"
            if self.on_recoverable is not None:
                self.on_recoverable(job.name, error)
            if job.critical or job.consecutive_failures >= self.settings.max_consecutive_failures:
                job.last_result = "failed"
                raise CoreError("Se agotó la recuperación del trabajo " + job.name) from error
            ceiling = min(self.settings.retry_max_seconds, self.settings.retry_initial_seconds * 2 ** (job.consecutive_failures - 1))
            job.next_due = self.clock() + self.uniform(ceiling / 2, ceiling)
            logger.warning("Scheduler: %s se reintentará tras %s; intento %s.", job.name, job.last_error_type, job.consecutive_failures)
        except asyncio.CancelledError:
            job.last_result = "cancelled"
            raise
        except Exception as error:
            job.failures += 1
            job.last_result = "failed"
            job.last_error_type = type(error).__name__
            raise
        finally:
            self.stack.pop()
            job.running = False
            job.last_duration_seconds = max(0, self.clock() - started)
            job.last_finished_at = datetime.now(timezone.utc).isoformat()
            self.publish(force=True)

    async def run_due(self) -> int:
        count = 0
        while not self._stop and (job := self._due()) is not None:
            await self._execute(job)
            count += 1
        return count

    def status(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        return {"schema_version": 1, "state": self.state, "heartbeat_at": now.isoformat(),
            "heartbeat_valid_until": (now + timedelta(seconds=max(self.settings.heartbeat_seconds * 3, self.settings.idle_poll_seconds * 3))).isoformat(),
            "last_error_type": self.last_error_type, "active_jobs": [job.name for job in self.stack],
            "jobs": [{"name": job.name, "priority": job.priority, "running": job.running, "blocked_reason": job.allowed(),
                "next_due_at": (now + timedelta(seconds=max(0, job.next_due - self.clock()))).isoformat(),
                "runs": job.runs, "failures": job.failures, "consecutive_failures": job.consecutive_failures, "deferred": job.deferred,
                "last_result": job.last_result, "last_error_type": job.last_error_type,
                "last_duration_seconds": job.last_duration_seconds, "last_started_at": job.last_started_at, "last_finished_at": job.last_finished_at}
                for job in sorted(self.jobs.values(), key=lambda job: (job.priority, job.name))]}

    def publish(self, *, force: bool = False) -> None:
        if force or self.clock() - self._last_publish >= self.settings.heartbeat_seconds:
            save_snapshot(self.settings.status_file, self.status())
            self._last_publish = self.clock()

    async def run(self) -> None:
        self.state = "running"
        try:
            self.publish(force=True)
            while not self._stop:
                if self.watchdog is not None:
                    self.watchdog()
                await self.run_due()
                self.publish()
                if self._stop:
                    break
                candidates = [max(0, job.next_due - self.clock()) for job in self.jobs.values() if not job.running and job.allowed() is None]
                delay = min(self.settings.idle_poll_seconds, min(candidates, default=self.settings.idle_poll_seconds))
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=max(0.001, delay))
                except TimeoutError:
                    pass
            self.state = "stopped"
        except (asyncio.CancelledError, BrowserClosed):
            self.state = "stopped"
            raise
        except Exception as error:
            self.state = "failed"
            self.last_error_type = type(error).__name__
            raise
        finally:
            self.publish(force=True)

"""
Minimal asyncio scheduler — no external dependency.

Jobs are interval-based (run every N seconds) or time-of-day based
(run daily at HH:MM UTC). Scheduler keeps a lightweight run history
in memory + writes `kind="scheduler_run"` rows to the audit ledger.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger(__name__)

HandlerT = Callable[[], Awaitable[Any]]


@dataclass
class Job:
    name: str
    handler: HandlerT
    interval_s: Optional[int] = None
    at_utc: Optional[str] = None        # "HH:MM"
    description: str = ""
    last_run_ms: int = 0
    next_run_ms: int = 0
    run_count: int = 0
    fail_count: int = 0

    def schedule_next(self, now_ms: int) -> None:
        if self.interval_s is not None:
            self.next_run_ms = now_ms + int(self.interval_s * 1000)
        elif self.at_utc:
            self.next_run_ms = _next_at_utc_ms(self.at_utc, now_ms)
        else:
            self.next_run_ms = now_ms + 3_600_000


@dataclass
class JobRun:
    name: str
    ok: bool
    started_ms: int
    finished_ms: int
    error: Optional[str] = None
    result: Any = None


def _next_at_utc_ms(at_utc: str, now_ms: int) -> int:
    hh, mm = [int(x) for x in at_utc.split(":")]
    now = datetime.fromtimestamp(now_ms / 1000.0, tz=timezone.utc)
    today = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if today.timestamp() * 1000 <= now_ms:
        today = today.replace(day=today.day) + _one_day()
    return int(today.timestamp() * 1000)


def _one_day():
    from datetime import timedelta
    return timedelta(days=1)


class Scheduler:
    def __init__(self, *, ledger: Optional[Any] = None, tick_s: float = 1.0):
        self._jobs: dict[str, Job] = {}
        self._ledger = ledger
        self._tick_s = tick_s
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._history: list[JobRun] = []
        self._max_history = 200
        self._lock = asyncio.Lock()

    def register(
        self,
        name: str,
        handler: HandlerT,
        *,
        interval_s: Optional[int] = None,
        at_utc: Optional[str] = None,
        description: str = "",
    ) -> Job:
        if name in self._jobs:
            raise ValueError(f"duplicate job name: {name}")
        j = Job(name=name, handler=handler, interval_s=interval_s,
                at_utc=at_utc, description=description)
        j.schedule_next(int(time.time() * 1000))
        self._jobs[name] = j
        return j

    def list_jobs(self) -> list[dict]:
        return [
            {
                "name": j.name,
                "interval_s": j.interval_s,
                "at_utc": j.at_utc,
                "description": j.description,
                "last_run_ms": j.last_run_ms,
                "next_run_ms": j.next_run_ms,
                "run_count": j.run_count,
                "fail_count": j.fail_count,
            }
            for j in self._jobs.values()
        ]

    def history(self, limit: int = 50) -> list[dict]:
        return [
            {"name": r.name, "ok": r.ok, "started_ms": r.started_ms,
             "finished_ms": r.finished_ms, "error": r.error}
            for r in self._history[-limit:]
        ]

    async def run_now(self, name: str) -> JobRun:
        job = self._jobs[name]
        return await self._run_job(job)

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="openclaw.scheduler")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=2)
            except asyncio.TimeoutError:
                self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        try:
            while not self._stop.is_set():
                now_ms = int(time.time() * 1000)
                for j in list(self._jobs.values()):
                    if j.next_run_ms <= now_ms:
                        await self._run_job(j)
                await asyncio.sleep(self._tick_s)
        except asyncio.CancelledError:
            return

    async def _run_job(self, job: Job) -> JobRun:
        started = int(time.time() * 1000)
        job.run_count += 1
        run = JobRun(name=job.name, ok=True, started_ms=started, finished_ms=started)
        try:
            async with self._lock:
                run.result = await job.handler()
            run.finished_ms = int(time.time() * 1000)
        except Exception as e:  # pragma: no cover (broad)
            run.ok = False
            run.error = str(e)
            run.finished_ms = int(time.time() * 1000)
            job.fail_count += 1
            log.exception("job %s failed", job.name)
        finally:
            job.last_run_ms = run.finished_ms
            job.schedule_next(run.finished_ms)
            self._history.append(run)
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history:]
            if self._ledger is not None:
                self._ledger.record(
                    kind="scheduler_run",
                    phase=job.name,
                    severity="info" if run.ok else "error",
                    result={
                        "ok": run.ok,
                        "started_ms": run.started_ms,
                        "finished_ms": run.finished_ms,
                        "error": run.error,
                    },
                )
        return run

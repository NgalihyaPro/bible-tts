"""Generation job tracking and de-duplication.

Solves the thundering-herd case from the spec: three users requesting John 3 at
once must produce one generation, not three. The first request creates the job;
the rest observe its state.

In-process, so correct for a single API replica. The interface is deliberately
narrow (claim / get / finish) so a Redis-backed implementation can replace it
without touching the routes.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field

from app.schemas import JobStatus

log = logging.getLogger(__name__)


@dataclass
class Job:
    key: str
    status: JobStatus = JobStatus.GENERATING
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    error: str | None = None

    @property
    def elapsed_s(self) -> float:
        return (self.finished_at or time.monotonic()) - self.started_at


class JobRegistry:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = asyncio.Lock()

    async def claim(self, key: str) -> tuple[Job, bool]:
        """Return (job, is_owner).

        is_owner is True only for the caller that created the job; that caller is
        responsible for doing the work and calling finish().
        """
        async with self._lock:
            existing = self._jobs.get(key)
            if existing and existing.status is JobStatus.GENERATING:
                return existing, False
            job = Job(key=key)
            self._jobs[key] = job
            return job, True

    async def get(self, key: str) -> Job | None:
        async with self._lock:
            return self._jobs.get(key)

    async def finish(self, key: str, status: JobStatus, error: str | None = None) -> None:
        async with self._lock:
            job = self._jobs.get(key)
            if job is None:
                job = Job(key=key)
                self._jobs[key] = job
            job.status = status
            job.error = error
            job.finished_at = time.monotonic()
        log.info("job %s -> %s%s", key, status.value, f" ({error})" if error else "")

    async def forget(self, key: str) -> None:
        async with self._lock:
            self._jobs.pop(key, None)


job_registry = JobRegistry()

# Guards the engine itself. Piper here is single-threaded Flask on a 1-CPU cap:
# parallel requests serialize and simply time out, so excess load is refused at
# the door instead of being queued.
_synthesis_semaphore: asyncio.Semaphore | None = None


def get_synthesis_semaphore(limit: int) -> asyncio.Semaphore:
    global _synthesis_semaphore
    if _synthesis_semaphore is None:
        _synthesis_semaphore = asyncio.Semaphore(limit)
    return _synthesis_semaphore

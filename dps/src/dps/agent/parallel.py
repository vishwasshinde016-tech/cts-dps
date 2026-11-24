"""Parallel AI reads inside one job (improvement plan S1, 24 Nov 2026).

On 72 real documents 83 % of the time was waiting for the model: a document's reads (the four covenant parts, their
page batches, the schedule-term batches) were sent one after another at about 40 s each. They are independent, so
they run on a small thread pool. Each read keeps what it had on the job's own thread:

- the job's token budget (``Gateway.bound``), so the per-job cap still applies to every read;
- the trace context, the usage counter and the cancel check (``contextvars.copy_context``), so every model call
  stays in the job's trace and cost, and a cancelled job still stops before its next call;
- the order: results come back in the order the reads were listed, so the result is the same as before.

How many model calls run at once is capped by the gateway (``Gateway.parallel``, ``DPS_AI_PARALLEL_READS``,
default 1), within each route's requests-per-minute bucket. With one slot (tests, replay) nothing changes.
"""

from __future__ import annotations

import contextvars
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

T = TypeVar("T")


def slots(gateway) -> int:
    return max(1, int(getattr(gateway, "parallel", 1) or 1))


def run_all(gateway, tasks: list[Callable[[], T]]) -> list[T]:
    """Runs ``tasks`` (each a read of one batch or one part) and returns their results in order. Sequential when the
    gateway allows one call at a time or there is one task. The first error raised by a task is raised here, after
    every task has ended (a cancel or a crash never leaves a read running behind the job's back)."""
    width = min(slots(gateway), len(tasks))
    if width <= 1:
        return [task() for task in tasks]
    budget = gateway.current_budget() if hasattr(gateway, "current_budget") else None

    def wrap(task: Callable[[], T]) -> Callable[[], T]:
        context = contextvars.copy_context()

        def inner() -> T:
            if budget is not None and hasattr(gateway, "bound"):
                with gateway.bound(budget):
                    return task()
            return task()

        return lambda: context.run(inner)

    with ThreadPoolExecutor(max_workers=width, thread_name_prefix="dps-ai-read") as pool:
        futures = [pool.submit(wrap(task)) for task in tasks]
        results, first_error = [], None
        for future in futures:
            try:
                results.append(future.result())
            except BaseException as ex:  # noqa: BLE001 - re-raised below, after every read has ended
                results.append(None)
                first_error = first_error or ex
    if first_error is not None:
        raise first_error
    return results

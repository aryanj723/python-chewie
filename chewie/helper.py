"""
helper.py – minimal, dependency-free replacements for the Eventlet objects
used in the project.

Provided symbols
----------------
sleep        – alias to `asyncio.sleep`; MUST be awaited.
GreenPool    – drop-in stand-in for eventlet.GreenPool (spawn, waitall).
Queue        – subclass/alias of `asyncio.Queue` (same semantics).
Empty/Full   – re-exported for code that catches queue.Empty / queue.Full.
socket       – re-exported std-lib socket module (matches eventlet.green.socket)
"""

import asyncio
import queue   as _queue
import socket  as _socket
from typing import Any, Callable, Coroutine, List

__all__ = ["sleep", "GreenPool", "Queue", "Empty", "Full", "socket"]


# ────────────────────────────────────────────────────────────────────────────
# 1) sleep  – cooperative yield (must be awaited)
# ────────────────────────────────────────────────────────────────────────────
async def sleep(seconds: float = 0.0) -> None:
    """
    Drop-in replacement for `eventlet.sleep`.

    Example
    -------
        await sleep(0)
    """
    await asyncio.sleep(seconds)


# ────────────────────────────────────────────────────────────────────────────
# 2) GreenPool  – very small subset implemented with asyncio
# ────────────────────────────────────────────────────────────────────────────
class GreenPool:
    """
    Stand-in for `eventlet.GreenPool` covering only the API the project uses:
        • spawn(fn, *a, **kw)  → returns `asyncio.Task`
        • waitall()            → coroutine

    Behaviour:
        • Coroutine functions are scheduled directly.
        • Synchronous (blocking) functions are executed in a worker thread
          via `asyncio.to_thread`, keeping the event-loop responsive.
    """

    def __init__(self) -> None:
        self._tasks: List[asyncio.Task] = []

    # ---- task creation ----------------------------------------------------
    def spawn(
        self,
        fn: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> asyncio.Task:
        loop = asyncio.get_running_loop()
        
        if asyncio.iscoroutinefunction(fn):
            coro: Coroutine = fn(*args, **kwargs)
        else:
            # Use run_in_executor for blocking calls
            coro = loop.run_in_executor(
                None,  # Uses default executor
                lambda: fn(*args, **kwargs)
            )

        task = loop.create_task(coro)
        self._tasks.append(task)
        return task

    # ---- synchronisation --------------------------------------------------
    async def waitall(self) -> None:
        """
        Coroutine that resolves after every spawned task has terminated.
        """
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=False)

    # ---- async context-manager support ------------------------------------
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.waitall()


# ────────────────────────────────────────────────────────────────────────────
# 3) Queue  – thin wrapper around asyncio.Queue
# ────────────────────────────────────────────────────────────────────────────
class Queue(asyncio.Queue):
    """
    Replacement for `eventlet.queue.Queue`.

    `asyncio.Queue(maxsize=0)` (the default) is unbounded – same as Eventlet.
    """
    def __init__(self, maxsize: int = 0, *args, **kwargs):
        super().__init__(maxsize, *args, **kwargs)


# Re-export the queue exceptions so existing `except queue.Empty` keeps working
Empty = _queue.Empty
Full  = _queue.Full


# ────────────────────────────────────────────────────────────────────────────
# 4) socket  – re-export of the standard-library socket module
#              (matches `eventlet.green.socket`)
# ────────────────────────────────────────────────────────────────────────────
socket = _socket
